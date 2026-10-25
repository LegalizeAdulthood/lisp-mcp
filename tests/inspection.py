"""Bounded existing-symbol inspection; --live-v1 uses running Genera."""
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
from deadlines import identity
from evaluation import receive_request
from interrupt import call
from protocol import lisp_string
from session import reply, response
from stdio import bridge, genera_settings, peer, request, structured


def advertise(hello):
    hello[":features"].extend([":describe", ":apropos"])


class InspectionChecks(unittest.TestCase):
    def test_portable_search_limits_no_interning_admission_and_retention(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/inspection.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Inspection unit checks: 28 passed.", result.stdout)

    def test_schema_annotations_and_unsupported_listener(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.send(request("tools/list", "tools"))
            tools = {t["name"]: t for t in mcp.receive()["result"]["tools"]}
            for name in ("genera_describe", "genera_apropos"):
                self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
                self.assertTrue(tools[name]["annotations"]["idempotentHint"])
                self.assertFalse(tools[name]["annotations"]["destructiveHint"])
                self.assertFalse(tools[name]["annotations"]["openWorldHint"])
            for name, args in (
                    ("genera_describe", {}), ("genera_describe", {"symbol": ""}),
                    ("genera_describe", {"symbol": "A" * 257}),
                    ("genera_describe", {"symbol": "CAR", "limit": 2}),
                    ("genera_describe", {"symbol": "CAR", "package": "\n"}),
                    ("genera_describe", {"symbol": "CAR", "print_length": 1}),
                    ("genera_apropos", {"pattern": "a", "limit": True}),
                    ("genera_apropos", {"pattern": "a", "limit": 101}),
                    ("genera_apropos", {"pattern": "a", "limit": 0}),
                    ("genera_apropos", {"pattern": "a", "timeout_ms": 0}),
                    ("genera_apropos", {"pattern": "a", "extra": 1})):
                mcp.call(name, "bad", **args)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            for name, args in (("genera_describe", {"symbol": "CAR"}), ("genera_apropos", {"pattern": "car"})):
                self.assertEqual(call(mcp, name, **args)["error"]["code"], "unsupported")

    def test_literal_data_and_common_terminal_contract(self):
        def serve(stream, hello):
            for op, text in ((":describe", "#.(error)"), (":apropos", "car")):
                work = receive_request(stream)
                self.assertEqual(work[":op"], op)
                self.assertEqual(work[":form"], text)
                self.assertEqual(work[":package"], "USER")
                self.assertNotIn(":print-length", work)
                if op == ":apropos":
                    self.assertEqual(work[":limit"], 2)
                reply(stream, response(work, "(:FOUND NIL)" if op == ":describe" else "(:MATCHES NIL :SEARCH-COMPLETE T)"))
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.assertTrue(call(mcp, "genera_describe", symbol="#.(error)", package="USER")["ok"])
            result = call(mcp, "genera_apropos", pattern="car", package="USER", limit=2)
            self.assertTrue(result["ok"])
            self.assertEqual(result["request_state"], "completed")

    def test_both_inspections_respect_uncertainty_and_retained_results(self):
        for tool, arguments in (("genera_describe", {"symbol": "CAR"}), ("genera_apropos", {"pattern": "car"})):
            seen = []
            def serve(stream, hello):
                work = receive_request(stream)
                seen.append(work)
                lookup = receive_request(stream)
                self.assertEqual(lookup[":op"], ":request-status")
                terminal = response(work, "inspection")
                reply(stream, dict(response(lookup), **{":request-state": ":completed", ":result": terminal}))
                reply(stream, terminal)
                reply(stream, response(receive_request(stream)))
            with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
                mcp.initialize()
                mcp.ready()
                result = call(mcp, tool, timeout_ms=80, **arguments)
                self.assertEqual(result["request_state"], "unresolved")
                for blocked, args in (("genera_describe", {"symbol": "CAR"}),
                                      ("genera_apropos", {"pattern": "car"}),
                                      ("genera_eval", {"form": "42"})):
                    self.assertEqual(call(mcp, blocked, **args)["error"]["code"], "busy")
                found = call(mcp, "genera_request_status", **identity(result))
                self.assertEqual(found["result"]["values"], ["inspection"])
                self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
            self.assertEqual(len(seen), 1)

    def test_incomplete_terminal_result_remains_unresolved(self):
        def serve(stream, hello):
            packet = response(receive_request(stream))
            del packet[":values"]
            del packet[":value-count"]
            reply(stream, packet)
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            result = call(mcp, "genera_describe", symbol="CAR")
            self.assertEqual(result["error"]["code"], "protocol")
            self.assertEqual(result["request_state"], "unresolved")

    def native_settings(self):
        return dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=os.environ["GENERA_HOST"],
                    GENERA_PORT=os.environ.get("GENERA_PORT", "8888"))

    def evaluate(self, form):
        with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
            send(stream, native_request(hello, ":eval", "inspect-control-" + uuid.uuid4().hex, form=form))
            result = receive(stream)
            self.assertTrue(result[":ok"], result)
            return result

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_known_unknown_nil_missing_package_and_literal_input(self):
        missing = "NO-SYMBOL-" + uuid.uuid4().hex.upper()
        literal = "#.(ERROR \"INSPECTION MUST NOT READ\")"
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            known = call(mcp, "genera_describe", symbol="CAR", package="USER")
            self.assertTrue(known["ok"], known)
            self.assertIn(":FOUND T", known["values"][0])
            self.assertIn(":FBOUND T", known["values"][0])
            self.assertIn(":FOUND T", call(mcp, "genera_describe", symbol="NIL")["values"][0])
            for name in (missing, literal):
                result = call(mcp, "genera_describe", symbol=name, package="USER")
                self.assertTrue(result["ok"], result)
                self.assertIn(":FOUND NIL", result["values"][0])
                absent = call(mcp, "genera_eval", form=f'(multiple-value-bind (symbol status) (find-symbol {lisp_string(name)} "USER") (declare (ignore symbol)) (not status))')
                self.assertTrue(absent["ok"], absent)
                self.assertEqual(absent["values"], ["T"])
            for tool, args in (("genera_describe", {"symbol": "CAR"}), ("genera_apropos", {"pattern": "car"})):
                result = call(mcp, tool, package=missing, **args)
                self.assertEqual(result["error"]["code"], "package-not-found", result)
                self.assertEqual(result["request_state"], "failed")

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_documentation_and_large_match_set_are_bounded(self):
        tag = "INSPECT-" + uuid.uuid4().hex.upper()
        variable = "*" + tag + "*"
        self.evaluate(f'(progn (defvar {variable} 0 "fixture") '
                      f"(setf (documentation '{variable} 'variable) (make-string 9000 :initial-element #\\a)) "
                      f'(dotimes (i 125) (intern (format nil "{tag}-~D" i) "USER")) t)')
        try:
            with bridge(self.native_settings()) as mcp:
                mcp.initialize()
                mcp.ready()
                description = call(mcp, "genera_describe", symbol=variable, package="USER")
                self.assertTrue(description["ok"], description)
                self.assertTrue(description["truncated"])
                self.assertLess(len(description["values"][0]), 5000)
                found = call(mcp, "genera_apropos", pattern=tag.lower(), package="USER", limit=3)
                self.assertTrue(found["ok"], found)
                self.assertTrue(found["truncated"])
                self.assertIn(":SEARCH-COMPLETE NIL", found["values"][0])
                self.assertLessEqual(found["values"][0].count('"' + tag), 3)
                empty = call(mcp, "genera_apropos", pattern="ABSENT-" + tag, package="USER")
                self.assertTrue(empty["ok"], empty)
                self.assertIn(":MATCHES NIL", empty["values"][0])
                self.assertLessEqual(sum(map(len, found["values"])), 65536)
        finally:
            self.evaluate(f"(progn (makunbound '{variable}) "
                          f'(dotimes (i 125) (unintern (find-symbol (format nil "{tag}-~D" i) "USER") "USER")) '
                          f"(unintern '{variable} \"USER\") t)")

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_shared_worker_busy_and_duplicate_identity(self):
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            pending = call(mcp, "genera_eval", form="(progn (sleep 1) 42)", timeout_ms=80)
            for tool, args in (("genera_describe", {"symbol": "CAR"}), ("genera_apropos", {"pattern": "car"})):
                self.assertEqual(call(mcp, tool, **args)["error"]["code"], "busy")
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                for op in (":describe", ":apropos"):
                    busy = native_request(hello, op, "inspect-busy-" + uuid.uuid4().hex, form="CAR")
                    send(stream, busy)
                    self.assertIn(":busy", receive(stream)[":error"])
            deadline = time.monotonic() + 5
            while not call(mcp, "genera_request_status", **identity(pending)).get("result"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.04)
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":apropos", "inspect-once-" + uuid.uuid4().hex, form="CAR", limit=2)
                send(stream, work)
                first = receive(stream)
                self.assertTrue(first[":ok"], first)
                send(stream, work)
                self.assertEqual(first, receive(stream))
                changed = dict(work, **{":limit": 3})
                send(stream, changed)
                self.assertIn(":request-id-conflict", receive(stream)[":error"])
            found = call(mcp, "genera_request_status", request_id=work[":id"], boot_id=work[":boot-id"],
                         service_id=work[":service-id"])
            self.assertEqual(found["result"]["request_state"], "completed")
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])


if __name__ == "__main__":
    unittest.main()
