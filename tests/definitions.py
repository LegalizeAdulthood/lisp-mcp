"""Verified file-level definition lookup; --live-v1 uses running Genera."""
import os
import subprocess
import sys
import time
import unittest
import uuid

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from configuration import ROOT, sbcl_command
from concurrency import client, receive, request as native_request, send
from deadlines import connections, identity
from evaluation import receive_request
from interrupt import call
from protocol import lisp_string
from session import greeting, reply, response
from stdio import bridge, genera_settings, peer, request, structured


def advertise(hello):
    hello[":features"].extend([":find-definition", ":describe", ":apropos"])
    hello[":definition-kinds"] = ["function", "variable"]


def located(work, status="found"):
    packet = response(work)
    packet.update({":symbol-name": work[":form"], ":definition-kind": work[":definition-kind"],
                   ":lookup-status": status, ":values": None, ":value-count": 0})
    if status == "found":
        packet.update({":source-path": "SYS:SITE;ACTUAL-SOURCE", ":location-kind": "file",
                       ":source-type": "DEFUN" if work[":definition-kind"] == "function" else "DEFVAR"})
    return packet


class DefinitionChecks(unittest.TestCase):
    def test_portable_adapter_capability_metadata_and_retention(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/definitions.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"Definition unit checks: \d+ passed\.")
        print(result.stdout.strip())

    def test_discovery_requires_capability_and_supported_kind_before_submission(self):
        def idle(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(idle) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.send(request("tools/list", "tools"))
            self.assertNotIn("genera_find_definition", [t["name"] for t in mcp.receive()["result"]["tools"]])
            mcp.call("genera_find_definition", "disabled", symbol="CAR")
            self.assertEqual(mcp.receive()["error"]["code"], -32602)
        with peer(idle, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            status = mcp.ready()
            self.assertEqual(status["definition_kinds"], ["function", "variable"])
            mcp.send(request("tools/list", "tools"))
            tool = next(t for t in mcp.receive()["result"]["tools"] if t["name"] == "genera_find_definition")
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            for args in ({}, {"symbol": ""}, {"symbol": "A" * 257}, {"symbol": "CAR", "kind": None},
                         {"symbol": "CAR", "package": "\n"}, {"symbol": "CAR", "timeout_ms": 0},
                         {"symbol": "CAR", "print_length": 1}):
                mcp.call("genera_find_definition", "bad", **args)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            result = call(mcp, "genera_find_definition", symbol="CAR", kind="method")
            self.assertEqual(result["error"]["code"], "unsupported-kind")
            self.assertNotIn("request_id", result)

    def test_reconnect_removes_lookup_when_capability_is_lost(self):
        def serve(listener):
            with listener.accept()[0] as stream:
                hello = receive_request(stream)
                packet = greeting(hello)
                advertise(packet)
                reply(stream, packet)
                self.assertEqual(receive_request(stream)[":op"], ":status")
            with listener.accept()[0] as stream:
                hello = receive_request(stream)
                reply(stream, greeting(hello))
                self.assertEqual(stream.recv(1), b"")
        with connections(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.send(request("tools/list", "before"))
            self.assertIn("genera_find_definition", [t["name"] for t in mcp.receive()["result"]["tools"]])
            call(mcp, "genera_session_status", refresh=True)
            deadline = time.monotonic() + 5
            while True:
                mcp.send(request("tools/list", "after"))
                if "genera_find_definition" not in [t["name"] for t in mcp.receive()["result"]["tools"]]:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.03)

    def test_literal_request_and_explicit_missing_source(self):
        def serve(stream, hello):
            for status in ("found", "source-unavailable", "symbol-not-found"):
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":find-definition")
                self.assertEqual(work[":definition-kind"], "variable" if status == "found" else "function")
                self.assertEqual(work[":form"], "#.(error)" if status == "symbol-not-found" else "KNOWN")
                self.assertNotIn(":timeout-ms", work)
                reply(stream, located(work, status))
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            for status in ("found", "source-unavailable", "symbol-not-found"):
                result = call(mcp, "genera_find_definition", symbol="#.(error)" if status == "symbol-not-found" else "KNOWN",
                              kind="variable" if status == "found" else "function")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["lookup_status"], status)
                self.assertEqual("source_path" in result, status == "found")
                self.assertNotIn("line", result)

    def test_malformed_catalogue_and_location_are_not_trusted(self):
        def idle(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        def invalid(hello):
            advertise(hello)
            del hello[":definition-kinds"]
        with peer(idle, invalid) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            deadline = time.monotonic() + 5
            while True:
                status = call(mcp, "genera_session_status")
                if status.get("last_error"):
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.assertEqual(status["last_error"]["code"], "protocol")
        for change in (lambda p: p.pop(":source-path"), lambda p: p.pop(":lookup-status"),
                       lambda p: p.update({":symbol-name": "WRONG"}),
                       lambda p: p.update({":definition-kind": "variable", ":source-type": "DEFVAR"}),
                       lambda p: p.update({":source-type": "DEFMACRO"}),
                       lambda p: p.update({":lookup-status": "source-unavailable"}),
                       lambda p: p.update({":location-kind": "line"}),
                       lambda p: p.update({":source-path": "x" * 4097})):
            def serve(stream, hello):
                packet = located(receive_request(stream))
                change(packet)
                reply(stream, packet)
            with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
                mcp.initialize()
                mcp.ready()
                result = call(mcp, "genera_find_definition", symbol="KNOWN")
                self.assertEqual(result["error"]["code"], "protocol")
                self.assertEqual(result["request_state"], "unresolved")

    def test_timeout_blocks_all_execution_until_original_lookup_result(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            query = receive_request(stream)
            terminal = located(work)
            reply(stream, dict(response(query), **{":request-state": ":completed", ":result": terminal}))
            reply(stream, terminal)
            reply(stream, response(receive_request(stream)))
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            pending = call(mcp, "genera_find_definition", symbol="KNOWN", timeout_ms=80)
            self.assertEqual(pending["request_state"], "unresolved")
            for tool, args in (("genera_find_definition", {"symbol": "CAR"}),
                               ("genera_describe", {"symbol": "CAR"}), ("genera_apropos", {"pattern": "car"}),
                               ("genera_eval", {"form": "42"})):
                self.assertEqual(call(mcp, tool, **args)["error"]["code"], "busy")
            found = call(mcp, "genera_request_status", **identity(pending))
            self.assertEqual(found["result"]["source_path"], "SYS:SITE;ACTUAL-SOURCE")
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
        self.assertEqual(len(seen), 1)

    def native_settings(self):
        return dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=os.environ["GENERA_HOST"],
                    GENERA_PORT=os.environ.get("GENERA_PORT", "8888"))

    def evaluate(self, form):
        with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
            send(stream, native_request(hello, ":eval", "definition-control-" + uuid.uuid4().hex, form=form))
            result = receive(stream)
            self.assertTrue(result[":ok"], result)
            return result

    @unittest.skipUnless(LIVE, "requires --live-v1, updated listener, and filesystem login")
    def test_live_actual_source_for_function_macro_and_variable(self):
        name = "MCP-DEF-" + uuid.uuid4().hex.upper()
        self.assertIn("GENERA_SOURCE_GENERA_ROOT", os.environ)
        path = os.environ["GENERA_SOURCE_GENERA_ROOT"] + name + ".LISP"
        forms = ["(in-package 'common-lisp-user)", f"(defun {name} () 42)",
                 f"(defmacro {name}-MACRO () 42)", f"(defvar *{name}* 42)"]
        self.evaluate(f'(progn (with-open-file (out {lisp_string(path)} :direction :output '
                      ':if-exists :error :if-does-not-exist :create) '
                      + " ".join(f"(write-line {lisp_string(form)} out)" for form in forms) + ") "
                      + f'(load {lisp_string(path)} :verbose nil :print nil :set-default-pathname nil) t)')
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            for symbol, kind, source_type in ((name, "function", "DEFUN"), (name + "-MACRO", "function", "DEFUN"),
                                              ("*" + name + "*", "variable", "DEFVAR")):
                result = call(mcp, "genera_find_definition", symbol=symbol, package="USER", kind=kind)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["lookup_status"], "found")
                self.assertEqual(result["source_path"].upper(), path[:-5].upper())
                self.assertEqual(result["source_type"], source_type)
                self.assertEqual(result["location_kind"], "file")
                self.assertNotIn("line", result)
                self.assertNotIn("column", result)
                retained = call(mcp, "genera_request_status", **identity(result))
                self.assertEqual(retained["result"]["source_path"], result["source_path"])
            self.assertEqual(call(mcp, "genera_eval", form=f"({name})")["values"], ["42"])

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_missing_source_package_kind_and_literal_name(self):
        name = "MCP-NO-SOURCE-" + uuid.uuid4().hex.upper()
        self.evaluate(f"(defun {name} () 17)")
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            result = call(mcp, "genera_find_definition", symbol=name)
            self.assertEqual(result["lookup_status"], "source-unavailable", result)
            literal = '#.(ERROR "LOOKUP MUST NOT READ")'
            result = call(mcp, "genera_find_definition", symbol=literal)
            self.assertEqual(result["lookup_status"], "symbol-not-found", result)
            self.assertNotIn("source_path", result)
            probe = call(mcp, "genera_eval", form=f'(multiple-value-bind (symbol status) '
                         f'(find-symbol {lisp_string(literal)} "USER") (declare (ignore symbol)) (not status))')
            self.assertEqual(probe["values"], ["T"])
            self.assertEqual(call(mcp, "genera_find_definition", symbol="CAR", package=name)["error"]["code"],
                             "package-not-found")
            self.assertEqual(call(mcp, "genera_find_definition", symbol="CAR", kind="method")["error"]["code"],
                             "unsupported-kind")
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_shared_worker_and_retained_duplicate_identity(self):
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            pending = call(mcp, "genera_eval", form="(progn (sleep 1) 42)", timeout_ms=80)
            self.assertEqual(call(mcp, "genera_find_definition", symbol="CAR")["error"]["code"], "busy")
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":find-definition", "definition-busy-" + uuid.uuid4().hex,
                                      form="CAR", definition_kind="function")
                send(stream, work)
                self.assertIn(":busy", receive(stream)[":error"])
            deadline = time.monotonic() + 5
            while not call(mcp, "genera_request_status", **identity(pending)).get("result"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.04)
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":find-definition", "definition-once-" + uuid.uuid4().hex,
                                      form="CAR", definition_kind="function")
                send(stream, work)
                first = receive(stream)
                self.assertEqual(first[":lookup-status"], "found", first)
                send(stream, work)
                self.assertEqual(first, receive(stream))
                send(stream, dict(work, **{":definition-kind": "variable"}))
                self.assertIn(":request-id-conflict", receive(stream)[":error"])


if __name__ == "__main__":
    unittest.main()
