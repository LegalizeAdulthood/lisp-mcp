"""MCP concurrency, profiles, structured results, validation, and EOF."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from configuration import ROOT, SETTINGS, request, run_bridge, sbcl_command
from concurrency import client, receive, request as bridge_request, send
from evaluation import receive_request
from framing import frame, peer as frame_peer
from protocol import legacy
from session import peer as session_peer, reply, response

peer = partial(session_peer, accept_timeout=15)


class Bridge:
    def __init__(self, settings, command=None):
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
        env.update(settings)
        self.process = subprocess.Popen(
            command or sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"), "--eval", "(lisp-mcp:main)"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.output = queue.Queue()
        self.received = []
        self.errors = []

        def read():
            for line in self.process.stdout:
                try:
                    packet = json.loads(line)
                except ValueError:
                    packet = {"bad_stdout": line}
                self.received.append(packet)
                self.output.put(packet)

        self.reader = threading.Thread(target=read)
        self.reader.start()
        self.diagnostic = threading.Thread(target=lambda: self.errors.append(self.process.stderr.read()))
        self.diagnostic.start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, name, rpc_id, **arguments):
        self.send(request("tools/call", rpc_id, name=name, arguments=arguments))

    def receive(self, timeout=4):
        packet = self.output.get(timeout=timeout)
        if "bad_stdout" in packet:
            raise AssertionError(packet)
        return packet

    def initialize(self, version="2025-06-18"):
        self.send(request("initialize", "init", protocolVersion=version,
                          capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
        initialized = self.receive(timeout=15)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return initialized

    def ready(self):
        deadline = time.monotonic() + 4
        while True:
            self.call("genera_session_status", "ready")
            status = self.receive()["result"]
            data = json.loads(status["content"][0]["text"])
            if data["state"] == "ready":
                return data
            if time.monotonic() >= deadline:
                raise AssertionError(data)
            time.sleep(.02)

    def close(self, timeout=3):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait(timeout=timeout)
        self.reader.join(timeout=1)
        self.diagnostic.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()
        if self.process.returncode:
            raise AssertionError("".join(self.errors))
        if any("bad_stdout" in packet for packet in self.received):
            raise AssertionError(self.received)
        if "Unhandled" in "".join(self.errors):
            raise AssertionError("".join(self.errors))


@contextmanager
def bridge(settings):
    instance = Bridge(settings)
    try:
        yield instance
    finally:
        try:
            instance.close()
        except BaseException:
            instance.process.kill()
            instance.process.wait()
            raise


def genera_settings(address):
    return {"LISP_MCP_PROFILE": "genera", "GENERA_PROTOCOL": "v1",
            "GENERA_HOST": address[0], "GENERA_PORT": str(address[1])}


def structured(packet):
    result = packet["result"]
    data = json.loads(result["content"][0]["text"])
    if "structuredContent" in result and result["structuredContent"] != data:
        raise AssertionError("Structured and text results differ")
    return data


class StdioChecks(unittest.TestCase):
    def test_profile_validation_and_discovery_do_not_connect(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(.1)
            with bridge(genera_settings(listener.getsockname())) as mcp:
                mcp.send(request("initialize", 0, protocolVersion="2025-06-18"))
                self.assertEqual(mcp.receive()["id"], 0)
                mcp.send(request("tools/list", "list"))
                tools = mcp.receive()["result"]["tools"]
                self.assertEqual({t["name"] for t in tools},
                                 {"genera_eval", "eval_genera", "genera_session_status",
                                  "genera_request_status", "genera_recover_session", "genera_ensure_session"})
                self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
                mcp.call("genera_session_status", "status")
                self.assertEqual(structured(mcp.receive())["phase"], "not-started")
                mcp.call("genera_eval", "early", form="42")
                early = mcp.receive()
                self.assertTrue(early["result"]["isError"])
                self.assertEqual(structured(early)["error"]["code"], "not-ready")
                with self.assertRaises(socket.timeout):
                    listener.accept()
        for settings, variable in [({"LISP_MCP_PROFILE": "genera"}, "LISP_MCP_PROFILE"),
                                   ({"LISP_MCP_PROFILE": "genera", "GENERA_PROTOCOL": "v1"}, "GENERA_HOST"),
                                   ({"GENERA_PROTOCOL": "v1"}, "GENERA_PROTOCOL"),
                                   ({"GENERA_TOKEN": "secret"}, "GENERA_TOKEN")]:
            result = run_bridge(settings, [request("tools/list")])
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("Invalid configuration: " + variable, result.stderr)

    def test_pending_eval_keeps_rpc_status_and_alias_responsive(self):
        released = threading.Event()
        requests = []

        def serve(stream, hello):
            first = receive_request(stream)
            requests.append(first)
            self.assertTrue(released.wait(4))
            reply(stream, response(first))
            second = receive_request(stream)
            requests.append(second)
            reply(stream, response(second, value=":ALIAS"))
            self.assertNotEqual(first[":id"], second[":id"])
            self.assertNotEqual(first[":id"], "eval")
            self.assertEqual(hello[":op"], ":hello")

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "eval", form="(values 42)", package="USER", print_level=3)
            started = time.monotonic()
            mcp.send(request("initialize", "again", protocolVersion="2025-06-18"))
            mcp.send(request("tools/list", "list"))
            mcp.call("genera_session_status", "status")
            fast = [mcp.receive() for _ in range(3)]
            self.assertEqual({r["id"] for r in fast}, {"again", "list", "status"})
            self.assertLess(time.monotonic() - started, 1)
            status = structured(next(r for r in fast if r["id"] == "status"))
            self.assertEqual(status["state"], "executing")
            self.assertTrue(status["busy"])
            self.assertTrue(status["remote_status_stale"])
            mcp.call("genera_eval", "busy", form="99")
            busy = mcp.receive()
            self.assertTrue(busy["result"]["isError"])
            self.assertEqual(structured(busy)["error"]["code"], "busy")
            mcp.call("genera_eval", "eval", form="duplicate MCP ID")
            self.assertEqual(mcp.receive()["error"]["code"], -32600)
            released.set()
            done = mcp.receive()
            self.assertEqual(done["id"], "eval")
            self.assertFalse(done["result"]["isError"])
            self.assertIn("structuredContent", done["result"])
            self.assertEqual(structured(done)["values"], ["42"])
            self.assertEqual(structured(done)["warnings"], [])
            self.assertEqual(structured(done)["timeout_enforced_by"], "host")
            mcp.call("eval_genera", "alias", expression="(values :alias)")
            self.assertEqual(structured(mcp.receive())["values"], [":ALIAS"])
        self.assertEqual(requests[0][":package"], "USER")
        self.assertEqual(requests[0][":print-level"], 3)

    def test_legacy_mcp_revision_uses_json_text_and_reports_eval_failure(self):
        def serve(stream, _):
            request = receive_request(stream)
            failed = response(request)
            failed.update({":ok": None, ":request-state": ":failed", ":values": None,
                           ":value-count": 0, ":stdout": "prior",
                           ":error": {":code": ":evaluation-error", ":condition-type": "ERROR",
                                      ":message": "fixture failure"}})
            reply(stream, failed)

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            result = mcp.initialize("2024-11-05")
            self.assertEqual(result["result"]["protocolVersion"], "2024-11-05")
            mcp.ready()
            mcp.send(request("tools/list", 1))
            self.assertNotIn("annotations", mcp.receive()["result"]["tools"][0])
            mcp.call("genera_eval", 2, form='(error "fixture failure")')
            failed = mcp.receive()
            self.assertTrue(failed["result"]["isError"])
            self.assertNotIn("structuredContent", failed["result"])
            data = structured(failed)
            self.assertEqual(data["request_state"], "failed")
            self.assertEqual(data["stdout"], "prior")
            self.assertEqual(data["values"], [])
            self.assertEqual(data["error"]["condition_type"], "ERROR")

    def test_validation_parse_errors_notifications_and_generic_ordering(self):
        with bridge({}) as mcp:
            initialized = mcp.initialize("unsupported")
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
            for number, params in enumerate([
                    {"name": "missing", "arguments": {}},
                    {"name": "eval_host_cl", "arguments": []},
                    {"name": "eval_host_cl", "arguments": {"expression": 42}},
                    {"name": "eval_host_cl", "arguments": {"expression": "42", "extra": True}}]):
                mcp.send(request("tools/call", number + 1, **params))
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            mcp.process.stdin.write("{broken\n\n{} trailing\n[]\n")
            mcp.process.stdin.flush()
            self.assertEqual([mcp.receive()["error"]["code"] for _ in range(3)],
                             [-32700, -32700, -32600])
            mcp.send({"jsonrpc": "2.0", "method": "unknown-notification"})
            mcp.call("eval_host_cl", "slow", expression='(progn (sleep .3) (setq mcp-stdio-value 7))')
            mcp.send(request("ping", "ping"))
            self.assertEqual(mcp.receive()["id"], "ping")
            self.assertEqual(mcp.receive()["id"], "slow")
            mcp.call("eval_host_cl", "ordered", expression="mcp-stdio-value")
            self.assertEqual(mcp.receive()["result"]["content"][0]["text"], "7")
            mcp.call("eval_host_cl", "error", expression='(error "expected")')
            self.assertTrue(mcp.receive()["result"]["isError"])
            mcp.call("eval_host_cl", "terminal", expression=
                     '(progn (write-line "terminal probe" *terminal-io*) '
                     '(read-line *standard-input* nil :eof))')
            self.assertEqual(mcp.receive()["result"]["content"][0]["text"], ":EOF\nT")

    def test_bad_genera_arguments_do_not_submit(self):
        with bridge(genera_settings(("127.0.0.2", 1))) as mcp:
            for arguments in [{"form": 1}, {"form": "42", "print_level": False},
                              {"form": "42", "print_length": -1},
                              {"form": "42", "package": ""},
                              {"form": "42", "timeout_ms": 0},
                              {"form": "42", "timeout_ms": True}]:
                mcp.send(request("tools/call", "bad", name="genera_eval", arguments=arguments))
                self.assertEqual(mcp.receive()["error"]["code"], -32602)

    def test_eof_closes_pending_eval_without_waiting_or_replay(self):
        accepted = threading.Event()

        def serve(stream, _):
            receive_request(stream)
            accepted.set()

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", 1, form="(sleep 100)")
            self.assertTrue(accepted.wait(3))
            started = time.monotonic()
            mcp.close()
            self.assertLess(time.monotonic() - started, 1)

    def test_eof_interrupts_host_handshake_without_remote_commands(self):
        with socket.socket() as listener, ThreadPoolExecutor(1) as pool:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(15)
            accepted = threading.Event()

            def serve():
                with listener.accept()[0] as stream:
                    stream.settimeout(3)
                    self.assertEqual(receive_request(stream)[":op"], ":hello")
                    accepted.set()
                    self.assertEqual(stream.recv(1), b"")

            future = pool.submit(serve)
            with bridge(genera_settings(listener.getsockname())) as mcp:
                mcp.initialize()
                self.assertTrue(accepted.wait(3))
                mcp.call("genera_session_status", 1)
                self.assertEqual(structured(mcp.receive())["phase"], "hello")
                started = time.monotonic()
                mcp.close()
                self.assertLess(time.monotonic() - started, 1)
            future.result(timeout=3)

    def test_authentication_failure_has_tool_error_status_and_no_token_leak(self):
        def reject(hello):
            hello.clear()
            hello.update({":version": 1, ":id": "unused", ":ok": None,
                          ":error": {":code": ":authentication-failed", ":message": "Authentication failed"}})
        # Keep the response ID correct; an authentication error is data, not a
        # malformed envelope. A custom peer needs the incoming hello ID.
        with socket.socket() as listener, ThreadPoolExecutor(1) as pool:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(15)

            def serve():
                with listener.accept()[0] as stream:
                    stream.settimeout(3)
                    hello = receive_request(stream)
                    rejected = {}
                    reject(rejected)
                    rejected[":id"] = hello[":id"]
                    reply(stream, rejected)
                    self.assertEqual(stream.recv(1), b"")

            future = pool.submit(serve)
            settings = genera_settings(listener.getsockname()) | {"GENERA_TOKEN": "private-fixture-token"}
            with bridge(settings) as mcp:
                mcp.initialize()
                future.result(timeout=3)
                deadline = time.monotonic() + 3
                while True:
                    mcp.call("genera_session_status", "failed-status")
                    status = structured(mcp.receive())
                    if status["phase"] == "failed":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                mcp.call("genera_eval", 1, form="42")
                failed = mcp.receive()
                self.assertTrue(failed["result"]["isError"])
                self.assertEqual(structured(failed)["error"]["code"], "authentication-failed")
                self.assertEqual(structured(failed)["session"]["phase"], "failed")
            self.assertNotIn("private-fixture-token", "".join(mcp.errors) + json.dumps(mcp.received))

    def test_transport_failure_retains_bridge_identity_in_tool_error(self):
        captured = []

        def serve(stream, _):
            captured.append(receive_request(stream))
            stream.shutdown(socket.SHUT_WR)

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "lost", form="side effect")
            failed = mcp.receive()
            self.assertTrue(failed["result"]["isError"])
            data = structured(failed)
            self.assertEqual(data["error"]["code"], "transport")
            self.assertEqual(data["request_id"], captured[0][":id"])
            self.assertEqual(data["request_state"], "unresolved")
            self.assertEqual(data["session"]["state"], "outcome-unknown")
            mcp.call("genera_eval", "blocked", form="another side effect")
            self.assertTrue(mcp.receive()["result"]["isError"])

    def test_generic_remote_failures_set_tool_error_status(self):
        swank = b'(:return (:abort "fixture failure") 1)'
        cases = [("genera", frame(b"") + frame(b"") + frame(b"fixture failure")),
                 ("swank", f"{len(swank):06x}".encode() + swank)]
        for protocol, payload in cases:
            with self.subTest(protocol=protocol):
                with frame_peer(payload, protocol=protocol) as (host, port):
                    settings = ({"GENERA_HOST": host, "GENERA_PORT": str(port)} if protocol == "genera"
                                else {"SWANK_HOST": host, "SWANK_PORT": str(port)})
                    result = run_bridge(settings, [request("tools/call", 1, name="eval_" + protocol,
                                                          arguments={"expression": "42"})])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["result"]["isError"])
                self.assertIn("fixture failure", json.loads(result.stdout)["result"]["content"][0]["text"])

    def test_eof_does_not_wait_for_a_blocked_stdout_writer(self):
        returned = threading.Event()

        def serve(stream, _):
            evaluation = receive_request(stream)
            reply(stream, response(evaluation, value="x" * 65000))
            returned.set()

        with peer(serve) as address:
            env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
            env.update(genera_settings(address))
            process = subprocess.Popen(
                sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"), "--eval", "(lisp-mcp:main)"],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            try:
                def exchange(message):
                    process.stdin.write(json.dumps(message) + "\n")
                    process.stdin.flush()
                    return json.loads(process.stdout.readline())

                exchange(request("initialize", 0, protocolVersion="2025-06-18"))
                process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                deadline = time.monotonic() + 4
                while structured(exchange(request("tools/call", 1, name="genera_session_status",
                                                  arguments={})))["state"] != "ready":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                process.stdin.write(json.dumps(request("tools/call", 2, name="genera_eval",
                                                      arguments={"form": "large result"})) + "\n")
                process.stdin.flush()
                self.assertTrue(returned.wait(4))
                time.sleep(.1)
                # Leave stdout unread while its result is larger than the pipe.
                process.stdin.close()
                process.wait(timeout=2)
                self.assertEqual(process.returncode, 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_mcp_eval_status_alias_and_eof_leave_genera_running(self):
        address = (os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888")))
        with socket.create_connection(address, timeout=5) as control:
            control.settimeout(10)
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with bridge(genera_settings(address)) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    mcp.call("genera_eval", "native", form='(progn (setq *mcp-stdio-check* 1) '
                             '(sleep 1) (values 42 :native))')
                    mcp.call("genera_session_status", "status")
                    self.assertEqual(structured(mcp.receive())["state"], "executing")
                    done = mcp.receive()
                    self.assertEqual(done["id"], "native")
                    self.assertEqual(structured(done)["values"], ["42", ":NATIVE"])
                    mcp.call("eval_genera", "alias", expression="*mcp-stdio-check*")
                    self.assertEqual(structured(mcp.receive())["values"], ["1"])
                    mcp.call("genera_eval", "failed", form='(error "native expected failure")')
                    self.assertTrue(mcp.receive()["result"]["isError"])
                    mcp.call("genera_eval", "detached", form='(progn (sleep 1) '
                             '(setq *mcp-stdio-check* 2))')
                    mcp.call("genera_session_status", "accepted")
                    active = structured(mcp.receive())
                    self.assertTrue(active["busy"])
                    with client(*address) as (observer, hello):
                        deadline = time.monotonic() + 3
                        while True:
                            send(observer, bridge_request(hello, ":status", "accepted"))
                            if receive(observer).get(":active-request-id") == active["active_request_id"]:
                                break
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.01)
                    mcp.close()
                time.sleep(1.1)
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy *mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, '*mcp-stdio-check*'), "2")
            self.assertEqual(legacy(control, '(+ 20 22)'), "42")
        print("Live MCP: structured eval, status, alias, execution failure, EOF, Genera preserved.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
