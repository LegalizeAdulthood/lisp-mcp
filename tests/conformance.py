"""Cross-tool MCP contracts and recovery; --live-v1 checks installed Genera."""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from compiling import compiled
from configuration import ROOT, SETTINGS, sbcl_command
from concurrency import client, receive, request as native_request, send
from deadlines import connections, identity
from definitions import located
from evaluation import receive_request, wire
from framing import frame
from interrupt import call
from loading import loaded
from protocol import lisp_string
from runners import tested
from session import greeting, reply, response
from stdio import bridge, genera_settings, peer, request, structured

CASES = (
    ("genera_eval", ":eval", {"form": "42"}),
    ("eval_genera", ":eval", {"expression": "42"}),
    ("genera_reset_listener_context", ":reset-listener-context", {"boot_id": "boot", "service_id": "service"}),
    ("genera_load", ":load", {"path": "SYS:TEST;fixture.lisp", "path_kind": "genera"}),
    ("genera_compile_file", ":compile-file", {"path": "SYS:TEST;fixture.lisp", "path_kind": "genera", "load": True}),
    ("genera_run_tests", ":run-tests", {"form": "'(:test-status :passed)"}),
    ("genera_describe", ":describe", {"symbol": "CAR"}),
    ("genera_apropos", ":apropos", {"pattern": "car", "limit": 2}),
    ("genera_find_definition", ":find-definition", {"symbol": "CAR"}),
)


def advertise(hello):
    hello[":features"] = [":status", ":request-status", ":eval", ":reset-listener-context",
                          ":load", ":compile-file", ":run-tests", ":describe", ":apropos",
                          ":find-definition", ":interrupt"]
    hello[":test-runners"] = ["form"]
    hello[":definition-kinds"] = ["function", "variable"]


def settings(address):
    return dict(genera_settings(address), GENERA_SOURCE_GENERA_ROOT="SYS:TEST;", GENERA_TIMEOUT="1")


def terminal(work):
    if work[":op"] == ":load":
        return loaded(work)
    if work[":op"] == ":compile-file":
        return compiled(work)
    if work[":op"] == ":run-tests":
        return tested(work)
    if work[":op"] == ":find-definition":
        return located(work)
    return response(work)


def accept(listener, service="service"):
    stream = listener.accept()[0]
    stream.settimeout(8)
    hello = receive_request(stream)
    packet = greeting(hello)
    advertise(packet)
    packet[":service-id"] = service
    reply(stream, packet)
    return stream


def tools(mcp, rpc_id="tools"):
    mcp.send(request("tools/list", rpc_id))
    return {tool["name"]: tool for tool in mcp.receive()["result"]["tools"]}


class ConformanceChecks(unittest.TestCase):
    def test_manifest_schemas_versions_and_generic_profile(self):
        for version in ("2024-11-05", "2025-06-18"):
            with peer(lambda stream, hello: self.assertEqual(stream.recv(1), b""), advertise) as address:
                with bridge(settings(address)) as mcp:
                    self.assertEqual(mcp.initialize(version)["result"]["protocolVersion"], version)
                    mcp.ready()
                    manifest = tools(mcp)
                    self.assertEqual(len(manifest), 14)
                    self.assertTrue(set(name for name, _, _ in CASES).issubset(manifest))
                    for name, tool in manifest.items():
                        schema = tool["inputSchema"]
                        self.assertEqual(schema["type"], "object")
                        self.assertFalse(schema["additionalProperties"])
                        self.assertTrue(set(schema["required"]).issubset(schema["properties"]))
                        if version == "2024-11-05":
                            self.assertNotIn("annotations", tool)
                        else:
                            readonly = name in ("genera_session_status", "genera_request_status",
                                                "genera_describe", "genera_apropos", "genera_find_definition")
                            self.assertEqual(tool["annotations"]["readOnlyHint"], readonly)
                            self.assertEqual(tool["annotations"]["destructiveHint"], not readonly)
        with bridge({}) as mcp:
            mcp.initialize()
            self.assertEqual(set(tools(mcp)), {"eval_host_cl"})
            result = call(mcp, "eval_host_cl", expression="(+ 20 22)")
            # Generic tools retain their plain text contract.
            self.assertEqual(result, 42)

    def test_every_execution_tool_timeout_late_result_and_shared_admission(self):
        for name, operation, arguments in CASES:
            with self.subTest(tool=name):
                seen = []
                def serve(stream, hello):
                    work = receive_request(stream)
                    seen.append(work)
                    self.assertEqual(work[":op"], operation)
                    final = terminal(work)
                    while True:
                        query = receive_request(stream)
                        if query[":op"] != ":request-status":
                            break
                        reply(stream, dict(response(query), **{":request-state": ":completed", ":result": final}))
                        if len(seen) == 1:
                            reply(stream, final)
                            seen.append("late")
                    self.assertEqual(query[":op"], ":eval")
                    reply(stream, response(query))
                    self.assertEqual(stream.recv(1), b"")
                with peer(serve, advertise) as address, bridge(settings(address)) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    mcp.call(name, "once", **dict(arguments, **({} if name == "eval_genera" else {"timeout_ms": 80})))
                    failed = structured(mcp.receive(timeout=10))
                    self.assertEqual(failed["error"]["code"], "timeout")
                    self.assertEqual(failed["request_state"], "unresolved")
                    self.assertEqual(failed["timeout_enforced_by"], "host")
                    for blocked, _, args in CASES:
                        self.assertEqual(call(mcp, blocked, **args)["error"]["code"], "busy")
                    found = call(mcp, "genera_request_status", **identity(failed))
                    self.assertEqual(found["result"]["request_id"], failed["request_id"])
                    self.assertEqual(found["result"]["request_state"], "completed")
                    self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
                    self.assertEqual(sum(p.get("id") == "once" for p in mcp.received), 1)
                self.assertEqual(len([work for work in seen if isinstance(work, dict)]), 1)

    def test_every_execution_tool_partial_reply_reconnects_without_replay(self):
        for name, operation, arguments in CASES:
            with self.subTest(tool=name):
                release = threading.Event()
                seen = []
                def serve(listener):
                    with accept(listener) as stream:
                        work = receive_request(stream)
                        seen.append(work)
                        packet = frame(wire(terminal(work)).encode("ascii"))
                        stream.sendall(packet[:len(packet) // 2])
                    with accept(listener) as stream:
                        while True:
                            query = receive_request(stream)
                            if query[":op"] != ":request-status":
                                break
                            packet = dict(response(query), **{":request-state": ":running"})
                            if release.is_set():
                                packet.update({":request-state": ":completed", ":result": terminal(work)})
                            reply(stream, packet)
                        self.assertEqual(query[":op"], ":eval")
                        seen.append(query)
                        reply(stream, response(query))
                        self.assertEqual(stream.recv(1), b"")
                with connections(serve) as address, bridge(settings(address)) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    failed = call(mcp, name, **arguments)
                    self.assertEqual(failed["request_state"], "unresolved", failed)
                    self.assertIn(failed["error"]["code"], ("protocol", "transport"))
                    deadline = time.monotonic() + 6
                    while not call(mcp, "genera_session_status")["connected"]:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    self.assertEqual(call(mcp, "genera_eval", form="42")["error"]["code"], "busy")
                    release.set()
                    deadline = time.monotonic() + 6
                    while True:
                        found = call(mcp, "genera_request_status", **identity(failed))
                        if found.get("result"):
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    self.assertEqual(found["result"]["request_id"], failed["request_id"])
                    self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0][":op"], operation)
                self.assertNotEqual(seen[0][":id"], seen[1][":id"])

    def test_every_execution_tool_preserves_unknown_across_generation_change(self):
        for name, operation, arguments in CASES:
            with self.subTest(tool=name):
                seen = []
                def serve(listener):
                    with accept(listener) as stream:
                        seen.append(receive_request(stream))
                    with accept(listener, service="replacement") as stream:
                        status = receive_request(stream)
                        self.assertEqual(status[":op"], ":status")
                        packet = response(status)
                        packet[":service-id"] = "replacement"
                        reply(stream, packet)
                        work = receive_request(stream)
                        self.assertEqual(work[":op"], ":eval")
                        seen.append(work)
                        packet = response(work)
                        packet[":service-id"] = "replacement"
                        reply(stream, packet)
                        self.assertEqual(stream.recv(1), b"")
                with connections(serve) as address, bridge(settings(address)) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    failed = call(mcp, name, **arguments)
                    target = identity(failed)
                    self.assertEqual(failed["request_state"], "unresolved")
                    deadline = time.monotonic() + 6
                    while True:
                        status = call(mcp, "genera_session_status")
                        if status.get("generation_change"):
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    self.assertTrue(status["generation_change"]["history_lost"])
                    self.assertFalse(status["generation_change"]["world_state_lost"])
                    self.assertEqual(call(mcp, "genera_eval", form="42")["error"]["code"], "busy")
                    recovered = call(mcp, "genera_recover_session", **target,
                                     expected_boot_id="boot", expected_service_id="replacement",
                                     acknowledge_unknown=True)
                    self.assertEqual(recovered["request_state"], "unknown")
                    self.assertTrue(recovered["acknowledged"])
                    self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0][":op"], operation)


class DiagnosticChecks(unittest.TestCase):
    def test_metadata_excludes_credentials_forms_results_and_client_ids(self):
        secret = "credential-marker-27"
        markers = ["private-form-27", "private-value-27", "private-stdout-27",
                   "private-warning-27", "private-error-27", "private-client-id-27"]
        def hello(packet):
            advertise(packet)
            packet[":boot-id"] = secret
        def serve(stream, packet):
            self.assertEqual(packet[":token"], secret)
            for index in range(2):
                work = receive_request(stream)
                self.assertEqual(work[":form"], markers[0])
                result = response(work, markers[1])
                result.update({":boot-id": secret, ":stdout": markers[2],
                               ":warnings": [markers[3]]})
                if index:
                    result.update({":ok": None, ":request-state": ":failed",
                                   ":error": {":code": ":evaluation-error",
                                              ":condition-type": "ERROR", ":message": markers[4]}})
                reply(stream, result)
            self.assertEqual(stream.recv(1), b"")
        with peer(serve, hello) as address:
            with bridge(dict(settings(address), GENERA_TOKEN=secret)) as mcp:
                mcp.initialize()
                mcp.ready()
                for index in range(2):
                    mcp.call("genera_eval", markers[5], form=markers[0])
                    packet = structured(mcp.receive())
                    self.assertEqual(packet["values"], [markers[1]])
                    self.assertEqual(packet["stdout"], markers[2])
                    self.assertEqual(packet["ok"], not index)
            diagnostic = "".join(mcp.errors)
        for marker in [secret] + markers:
            self.assertNotIn(marker, diagnostic)
        events = [json.loads(line) for line in diagnostic.splitlines() if line.startswith("{")]
        results = [event for event in events if event["operation"] == "genera_eval"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["boot_id"], "[redacted]")
        self.assertGreater(results[0]["request_bytes"], 0)
        self.assertGreater(results[0]["result_bytes"], 0)
        self.assertGreaterEqual(results[0]["host_elapsed_ms"], 0)
        self.assertEqual(results[1]["outcome"], "failed")
        self.assertEqual(results[1]["error_code"], "evaluation-error")

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux pipes")
    def test_unread_stderr_is_bounded_and_does_not_hold_responses_or_eof(self):
        import fcntl
        read_fd, write_fd = os.pipe()
        fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
        env = {key: value for key, value in os.environ.items() if key not in SETTINGS}
        env.update(genera_settings(("127.0.0.2", 1)))
        process = subprocess.Popen(
            sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"), "--eval", "(lisp-mcp:main)"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=write_fd, text=True, bufsize=1)
        os.close(write_fd)
        output = queue.Queue()
        reader = threading.Thread(target=lambda: [output.put(json.loads(line))
                                                 for line in process.stdout], daemon=True)
        reader.start()
        def exchange(packet):
            process.stdin.write(json.dumps(packet) + "\n")
            process.stdin.flush()
            return output.get(timeout=15)
        try:
            exchange(request("initialize", "init", protocolVersion="2025-06-18"))
            # Local snapshots require no connection or initialized notification.
            for index in range(180):
                packet = exchange(request("tools/call", index, name="genera_session_status", arguments={}))
            self.assertGreater(structured(packet)["diagnostics_dropped"], 0)
            self.assertEqual(exchange(request("ping", "ping"))["result"], {})
            started = time.monotonic()
            process.stdin.close()
            process.wait(timeout=3)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=1)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
            os.close(read_fd)


@unittest.skipUnless(LIVE, "requires --live-v1 and a disposable writable Genera source root")
class NativeConformanceChecks(unittest.TestCase):
    def test_all_execution_tools_and_cross_client_retained_results(self):
        host = os.environ["GENERA_HOST"]
        port = int(os.environ.get("GENERA_PORT", "8888"))
        source_root = os.environ["GENERA_SOURCE_GENERA_ROOT"]
        name = "MCP-CONFORMANCE-" + uuid.uuid4().hex.upper()
        path = source_root + name + ".lisp"
        configuration = dict(genera_settings((host, port)), GENERA_SOURCE_GENERA_ROOT=source_root)
        with bridge(configuration) as mcp:
            mcp.initialize()
            initial = mcp.ready()
            self.assertEqual(len(tools(mcp)), 14)
            code = "(defun " + name + " () 42)"
            header = ";;; -*- Syntax: Common-Lisp; Package: USER; Base: 10 -*-"
            created = call(mcp, "genera_eval", form=
                           f'(with-open-file (out {lisp_string(path)} :direction :output :if-exists :error '
                           f':if-does-not-exist :create) (write-line {lisp_string(header)} out) '
                           f'(write-line {lisp_string(code)} out) t)')
            self.assertTrue(created["ok"], created)
            results = []
            for tool, operation, arguments in CASES:
                args = dict(arguments)
                if operation == ":reset-listener-context":
                    args = {key: initial[key] for key in ("boot_id", "service_id")}
                elif operation in (":load", ":compile-file"):
                    args["path"] = path
                elif operation in (":describe", ":find-definition"):
                    args.update(symbol=name, package="USER")
                elif operation == ":apropos":
                    args.update(pattern=name, package="USER")
                elif operation == ":run-tests":
                    args["form"] = "'(:test-status :passed :passed-count 1 :failed-count 0)"
                result = call(mcp, tool, **args)
                self.assertTrue(result["ok"], (tool, result))
                self.assertEqual(result["request_state"], "completed")
                self.assertEqual(result["boot_id"], initial["boot_id"])
                self.assertEqual(result["service_id"], initial["service_id"])
                results.append(result)
            truncated = call(mcp, "genera_eval",
                             form='(progn (write-string (make-string 70000 :initial-element #\\x)) 42)',
                             timeout_ms=60000)
            self.assertTrue(truncated["ok"], truncated)
            self.assertTrue(truncated["truncated"])
            self.assertLessEqual(len(truncated["stdout"]), 65536)
        # A new host process may retrieve every completed result without replay.
        with bridge(configuration) as mcp:
            mcp.initialize()
            current = mcp.ready()
            self.assertEqual(current["boot_id"], initial["boot_id"])
            self.assertEqual(current["service_id"], initial["service_id"])
            for result in results:
                found = call(mcp, "genera_request_status", **identity(result))
                for key, value in found["result"].items():
                    self.assertEqual(result[key], value)
            self.assertEqual(call(mcp, "genera_eval", form=f"({name})")["values"], ["42"])

    def test_native_generation_rejection_eviction_and_frame_limit(self):
        host = os.environ["GENERA_HOST"]
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with client(host, port) as (stream, hello):
            self.assertNotIn(":source-transfer", hello[":features"])
            self.assertEqual(hello[":test-runners"], ["form"])
            for operation in dict.fromkeys(case[1] for case in CASES):
                fields = dict(form="42")
                if operation in (":load", ":compile-file"):
                    fields = dict(original_path="SYS:SITE;unused.lisp", path_kind="genera",
                                  path="SYS:SITE;unused.lisp", source_root="SYS:SITE;")
                elif operation == ":run-tests":
                    fields = dict(runner="form", form="42")
                elif operation in (":describe", ":find-definition"):
                    fields = dict(form="CAR")
                    if operation == ":find-definition":
                        fields["definition_kind"] = "function"
                elif operation == ":apropos":
                    fields = dict(form="CAR", limit=2)
                elif operation == ":reset-listener-context":
                    fields = {}
                work = native_request(hello, operation, "stale-" + uuid.uuid4().hex,
                                      service_id="stale-generation", **fields)
                send(stream, work)
                rejected = receive(stream)
                self.assertFalse(rejected[":ok"], rejected)
                error = rejected[":error"]
                self.assertEqual(dict(zip(error[::2], error[1::2]))[":code"], ":stale-generation", rejected)
            first = None
            for index in range(hello[":history-limit"] + 1):
                work = native_request(hello, ":eval", "eviction-" + uuid.uuid4().hex, form="42")
                first = first or work
                send(stream, work)
                self.assertTrue(receive(stream)[":ok"])
            lookup = native_request(hello, ":request-status", "lookup-" + uuid.uuid4().hex,
                                    target_id=first[":id"])
            send(stream, lookup)
            self.assertEqual(receive(stream)[":request-state"], ":unknown")
        with socket.create_connection((host, port), timeout=5) as stream:
            stream.sendall(b"1048577 ")
            try:
                self.assertEqual(stream.recv(1), b"")
            except ConnectionResetError:
                pass
        with client(host, port) as (stream, hello):
            send(stream, native_request(hello, ":eval", "after-limit-" + uuid.uuid4().hex, form="42"))
            self.assertEqual(receive(stream)[":values"], ["42"])


if __name__ == "__main__":
    unittest.main()
