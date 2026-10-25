"""Tracked compilation; --live-v1 creates only fresh disposable files."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
from loading import delayed_bridge, loaded
from protocol import lisp_string
from session import reply, response
from stdio import bridge, genera_settings, peer, structured


def advertise(hello):
    hello[":features"].append(":compile-file")


def compiled(work, failure=False):
    packet = loaded(work)
    packet.update({":load": work.get(":load"), ":compile-completed": True,
                   ":warnings-p": failure or None, ":failure-p": failure or None,
                   ":loaded": (bool(work.get(":load")) and not failure) or None,
                   ":output-path": "FIXTURE-HOST:>test>fresh.vbin.1"})
    if failure:
        packet.update({":ok": None, ":request-state": ":failed", ":warnings": ["compiler warning"],
                       ":error": {":code": ":compile-error", ":condition-type": "ERROR", ":message": "failed"}})
    return packet


class CompilingChecks(unittest.TestCase):
    def settings(self, address):
        return dict(genera_settings(address), GENERA_SOURCE_GENERA_ROOT="SYS:TEST;")

    def compile(self, mcp, request_id="compile", **changes):
        mcp.call("genera_compile_file", request_id,
                 **dict(dict(path="SYS:TEST;file.lisp", path_kind="genera"), **changes))

    def test_portable_compile_identity_outcomes_and_retention(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/compiling.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Compile unit checks: 18 passed.", result.stdout)

    def test_schema_and_unsupported_peer_send_no_compile(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            for changes in ({"load": "true"}, {"load": 1}, {"load": None}, {"path": ""},
                            {"path_kind": "auto"}, {"output_path": "world.vlod"}, {"timeout_ms": 0}):
                self.compile(mcp, **changes)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            self.compile(mcp)
            result = structured(mcp.receive())
            self.assertEqual(result["error"]["code"], "unsupported")
            self.assertEqual(result["original_path"], "SYS:TEST;file.lisp")

    def test_one_execution_and_exact_boolean_outcomes(self):
        def serve(stream, hello):
            for load in (False, True):
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":compile-file")
                self.assertEqual(bool(work[":load"]), load)
                self.assertNotIn(":form", work)
                reply(stream, compiled(work))
            work = receive_request(stream)
            reply(stream, compiled(work, failure=True))
        with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            for load in (False, True):
                self.compile(mcp, load=load)
                result = structured(mcp.receive())
                self.assertTrue(result["ok"])
                self.assertIs(result["load"], load)
                self.assertIs(result["loaded"], load)
                self.assertIs(result["failure_p"], False)
                self.assertIs(result["warnings_p"], False)
                self.assertTrue(result["compile_completed"])
                self.assertEqual(result["output_path"], "FIXTURE-HOST:>test>fresh.vbin.1")
            self.compile(mcp, load=True)
            result = structured(mcp.receive())
            self.assertFalse(result["ok"])
            self.assertTrue(result["failure_p"])
            self.assertTrue(result["warnings_p"])
            self.assertFalse(result["loaded"])
            self.assertEqual(result["warnings"], ["compiler warning"])

    def test_malformed_outcome_or_load_option_is_unresolved(self):
        for change in (lambda p: p.pop(":failure-p"), lambda p: p.update({":load": True}),
                       lambda p: p.update({":loaded": True}), lambda p: p.update({":failure-p": True})):
            def serve(stream, hello):
                work = receive_request(stream)
                packet = compiled(work)
                change(packet)
                reply(stream, packet)
            with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
                mcp.initialize()
                mcp.ready()
                self.compile(mcp)
                result = structured(mcp.receive())
                self.assertEqual(result["error"]["code"], "protocol")
                self.assertEqual(result["request_state"], "unresolved")

    def test_lost_reply_lookup_never_recompiles(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            lookup = receive_request(stream)
            self.assertEqual(lookup[":op"], ":request-status")
            terminal = compiled(work)
            reply(stream, dict(response(lookup), **{":request-state": ":completed", ":result": terminal}))
            reply(stream, terminal)
            reply(stream, response(receive_request(stream), "42"))
        with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.compile(mcp, timeout_ms=80, load=True)
            result = structured(mcp.receive())
            self.assertEqual(result["request_state"], "unresolved")
            self.compile(mcp, "busy")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            found = call(mcp, "genera_request_status", **identity(result))
            self.assertTrue(found["result"]["loaded"])
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
        self.assertEqual(len(seen), 1)

    def test_preparation_deadline_and_missing_file_send_no_compile(self):
        def serve(stream, hello):
            work = receive_request(stream)
            self.assertEqual(work[":op"], ":eval")
            reply(stream, response(work, "42"))
        with tempfile.TemporaryDirectory() as directory, peer(serve, advertise) as address:
            with delayed_bridge(dict(self.settings(address), GENERA_SOURCE_ROOT=directory)) as mcp:
                mcp.initialize()
                mcp.ready()
                self.compile(mcp, timeout_ms=100)
                self.assertEqual(structured(mcp.receive())["request_state"], "not-submitted")
                self.compile(mcp, path_kind="relative", path="missing.lisp")
                result = structured(mcp.receive())
                self.assertEqual(result["error"]["code"], "file-missing")
                self.assertEqual(result["request_state"], "not-submitted")
                self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    def native_settings(self, directory):
        return dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=os.environ["GENERA_HOST"],
                    GENERA_PORT=os.environ.get("GENERA_PORT", "8888"), GENERA_SOURCE_ROOT=directory,
                    GENERA_SOURCE_GENERA_ROOT=os.environ["GENERA_SOURCE_GENERA_ROOT"])

    def fixture(self, mcp, directory, code):
        leaf = "mcp-compile-source-" + uuid.uuid4().hex + ".lisp"
        path = os.environ["GENERA_SOURCE_GENERA_ROOT"] + leaf
        header = ";;; -*- Syntax: Common-Lisp; Package: USER; Base: 10 -*-"
        result = call(mcp, "genera_eval", form=
                      f'(with-open-file (out {lisp_string(path)} :direction :output :if-exists :error '
                      f':if-does-not-exist :create) (write-line {lisp_string(header)} out) '
                      f'(write-line {lisp_string(code)} out) t)')
        self.assertTrue(result["ok"], result)
        Path(directory, leaf).write_text(header + "\n" + code + "\n", encoding="ascii")
        return leaf, path

    @unittest.skipUnless(LIVE, "requires --live-v1, source root, and filesystem login")
    def test_live_success_optional_load_fresh_output_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory, bridge(self.native_settings(directory)) as mcp:
            mcp.initialize()
            mcp.ready()
            fn = "mcp-compiled-" + uuid.uuid4().hex
            count = "*" + fn + "-loads*"
            self.assertTrue(call(mcp, "genera_eval", form=f"(defvar {count} 0)")["ok"])
            leaf, path = self.fixture(mcp, directory, f'(progn (incf {count}) (defun {fn} () 42))')
            first = call(mcp, "genera_compile_file", path_kind="genera", path=path)
            self.assertTrue(first["ok"], first)
            self.assertFalse(first["loaded"])
            self.assertFalse(first["warnings_p"])
            self.assertFalse(first["failure_p"])
            self.assertTrue(first["compile_completed"])
            self.assertIn(".vbin.", first["output_path"].lower())
            self.assertEqual(call(mcp, "genera_eval", form=f"(list (fboundp '{fn}) {count})")["values"], ["(NIL 0)"])
            second = call(mcp, "genera_compile_file", path_kind="relative", path=leaf, load=True)
            self.assertTrue(second["ok"], second)
            self.assertTrue(second["loaded"])
            self.assertNotEqual(first["output_path"], second["output_path"])
            self.assertEqual(call(mcp, "genera_eval", form=f"(list ({fn}) {count})")["values"], ["(42 1)"])
            found = call(mcp, "genera_request_status", **identity(second))
            self.assertEqual(found["result"]["output_path"], second["output_path"])
            Path(directory, leaf).write_text("(error \"stale\")\n", encoding="ascii")
            stale = call(mcp, "genera_compile_file", path_kind="relative", path=leaf, load=True)
            self.assertEqual(stale["error"]["code"], "source-mismatch")
            self.assertFalse(stale["compile_completed"])
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":compile-file", "compile-dedupe-" + uuid.uuid4().hex,
                                      original_path=path, path_kind="genera", path=path,
                                      source_root=os.environ["GENERA_SOURCE_GENERA_ROOT"], load=True)
                send(stream, work)
                result = receive(stream)
                self.assertTrue(result[":ok"], result)
                send(stream, work)
                self.assertEqual(receive(stream), result)
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["2"])

    @unittest.skipUnless(LIVE, "requires --live-v1, source root, and filesystem login")
    def test_live_style_warning_failure_and_load_failure(self):
        with tempfile.TemporaryDirectory() as directory, bridge(self.native_settings(directory)) as mcp:
            mcp.initialize()
            mcp.ready()
            style = "mcp-style-warning-" + uuid.uuid4().hex
            defined = call(mcp, "genera_eval", form=
                           f'(ansi-common-lisp:define-condition {style} (ansi-common-lisp:style-warning) () '
                           '(:report (lambda (condition stream) (declare (ignore condition)) '
                           '(write-string "style warning fixture" stream))))')
            self.assertTrue(defined["ok"], defined)
            for code, expected_ok, completed in (
                    (f"(eval-when (:compile-toplevel) (ansi-common-lisp:warn '{style}))", True, True),
                    ('(eval-when (:compile-toplevel) (ansi-common-lisp:warn "compiler warning"))', False, True),
                    ("(defun mcp-invalid-compiler-fixture () (if))", False, True),
                    ('(eval-when (:compile-toplevel) (error "compile failure"))', False, False)):
                fn = "mcp-warning-" + uuid.uuid4().hex
                leaf, path = self.fixture(mcp, directory, f"(progn {code} (defun {fn} () 42))")
                result = call(mcp, "genera_compile_file", path_kind="genera", path=path, load=True)
                self.assertEqual(result["ok"], expected_ok, result)
                self.assertEqual(result["compile_completed"], completed, result)
                self.assertEqual(result["loaded"], expected_ok, result)
                self.assertEqual(result["failure_p"], not expected_ok, result)
                if completed:
                    self.assertTrue(result["warnings_p"], result)
                    self.assertTrue(result["warnings"], result)
                    self.assertTrue(result["output_path"], result)
                self.assertEqual(call(mcp, "genera_eval", form=f"(not (null (fboundp '{fn})))")["values"],
                                 ["T" if expected_ok else "NIL"])
            leaf, path = self.fixture(mcp, directory, '(error "load failure")')
            result = call(mcp, "genera_compile_file", path_kind="genera", path=path, load=True)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"]["code"], "load-error", result)
            self.assertFalse(result["failure_p"], result)
            self.assertTrue(result["compile_completed"])
            self.assertFalse(result["loaded"])

    @unittest.skipUnless(LIVE, "requires --live-v1, source root, and filesystem login")
    def test_live_timeout_and_disconnect_keep_one_compilation(self):
        with tempfile.TemporaryDirectory() as directory, bridge(self.native_settings(directory)) as mcp:
            mcp.initialize()
            mcp.ready()
            count = "*mcp-compile-count-" + uuid.uuid4().hex + "*"
            self.assertTrue(call(mcp, "genera_eval", form=f"(defvar {count} 0)")["ok"])
            leaf, path = self.fixture(mcp, directory,
                                     f"(eval-when (:compile-toplevel) (incf {count}) (sleep .6))")
            result = call(mcp, "genera_compile_file", path_kind="genera", path=path, timeout_ms=80)
            self.assertEqual(result["request_state"], "unresolved", result)
            self.compile(mcp, "busy")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            deadline = time.monotonic() + 5
            while True:
                found = call(mcp, "genera_request_status", **identity(result))
                if found.get("result"):
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.04)
            self.assertFalse(found["result"]["loaded"])
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["1"])
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":compile-file", "compile-disconnect-" + uuid.uuid4().hex,
                                      original_path=path, path_kind="genera", path=path,
                                      source_root=os.environ["GENERA_SOURCE_GENERA_ROOT"])
                send(stream, work)
                time.sleep(.1)
            deadline = time.monotonic() + 5
            while True:
                found = call(mcp, "genera_request_status", request_id=work[":id"],
                             boot_id=work[":boot-id"], service_id=work[":service-id"])
                if found.get("result"):
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.04)
            self.assertTrue(found["result"]["ok"], found)
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["2"])


if __name__ == "__main__":
    unittest.main()
