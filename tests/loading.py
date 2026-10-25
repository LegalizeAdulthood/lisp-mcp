"""Tracked file loading; --live-v1 uses new files in a disposable source root."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zlib

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from configuration import ROOT, SETTINGS, request, sbcl_command
from concurrency import client, receive, request as native_request, send
from deadlines import identity
from evaluation import receive_request
from interrupt import call
from protocol import lisp_string
from session import reply, response
from stdio import Bridge, bridge, genera_settings, peer, structured


def advertise(hello):
    hello[":features"].append(":load")


def loaded(work, failed=False):
    packet = response(work, "T")
    for key in (":original-path", ":path-kind", ":host-path", ":path", ":source-root"):
        if key in work:
            packet[key] = work[key]
    packet.update({":resolved-root": "FIXTURE-HOST:>test>", ":resolved-path": "FIXTURE-HOST:>test>file.lisp.1"})
    if failed:
        packet.update({":ok": None, ":request-state": ":failed", ":stdout": "before failure",
                       ":error": {":code": ":load-error", ":condition-type": "ERROR", ":message": "load failed"}})
    return packet


@contextmanager
def delayed_bridge(settings):
    delay = ('(let ((original (symbol-function (quote lisp-mcp::genera-load-fields)))) '
             '(setf (symbol-function (quote lisp-mcp::genera-load-fields)) '
             '(lambda (&rest args) (sleep .7) (apply original args))))')
    mcp = Bridge(settings, command=sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"),
                 "--eval", delay, "--eval", "(lisp-mcp:main)"])
    try:
        yield mcp
    finally:
        mcp.close()


class LoadingChecks(unittest.TestCase):
    def settings(self, address):
        return dict(genera_settings(address), GENERA_SOURCE_GENERA_ROOT="SYS:TEST;")

    def load(self, mcp, request_id="load", **changes):
        mcp.call("genera_load", request_id, **dict(dict(path="SYS:TEST;file.lisp", path_kind="genera"), **changes))

    def test_portable_load_identity_and_retention(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/loading.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Load unit checks: 12 passed.", result.stdout)

    def test_schema_and_unsupported_peer_send_no_load(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            for changes in ({"path": ""}, {"path_kind": "auto"}, {"path": None},
                            {"timeout_ms": 0}, {"extra": True}):
                self.load(mcp, **changes)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            self.load(mcp)
            result = structured(mcp.receive())
            self.assertEqual(result["error"]["code"], "unsupported")
            self.assertEqual(result["original_path"], "SYS:TEST;file.lisp")

    def test_native_load_uses_one_execution_with_common_results(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            self.assertEqual(work[":op"], ":load")
            self.assertNotIn(":form", work)
            self.assertEqual(work[":path"], "SYS:TEST;file.lisp")
            reply(stream, loaded(work))
            reply(stream, response(receive_request(stream), "42"))
        with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.load(mcp)
            result = structured(mcp.receive())
            self.assertTrue(result["ok"])
            self.assertEqual(result["resolved_path"], "FIXTURE-HOST:>test>file.lisp.1")
            self.assertEqual(result["request_state"], "completed")
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
        self.assertEqual(len(seen), 1)

    def test_mapped_load_fingerprint_and_missing_file_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.lisp"
            source.write_bytes(b"(+ 1 2)\r\n")
            def serve(stream, hello):
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":load")
                self.assertEqual(work[":source-size"], 8)
                self.assertEqual(work[":source-hash"], zlib.adler32(b"(+ 1 2)\n"))
                self.assertEqual(work[":host-path"], str(source))
                reply(stream, loaded(work))
                reply(stream, response(receive_request(stream), "42"))
            with peer(serve, advertise) as address, bridge(dict(self.settings(address), GENERA_SOURCE_ROOT=directory)) as mcp:
                mcp.initialize()
                mcp.ready()
                self.load(mcp, path_kind="relative", path="missing.lisp")
                missing = structured(mcp.receive())
                self.assertEqual(missing["error"]["code"], "file-missing")
                self.assertEqual(missing["request_state"], "not-submitted")
                self.assertEqual(missing["path"], "SYS:TEST;missing.lisp")
                self.load(mcp, path_kind="relative", path="file.lisp")
                self.assertTrue(structured(mcp.receive())["ok"])
                self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    def test_load_error_and_malformed_path_reply(self):
        for malformed in (False, True):
            def serve(stream, hello):
                work = receive_request(stream)
                packet = loaded(work, failed=not malformed)
                if malformed:
                    packet[":original-path"] = "another file"
                reply(stream, packet)
            with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
                mcp.initialize()
                mcp.ready()
                self.load(mcp)
                packet = mcp.receive()
                result = structured(packet)
                self.assertTrue(packet["result"]["isError"])
                if malformed:
                    self.assertEqual(result["error"]["code"], "protocol")
                    self.assertEqual(result["request_state"], "unresolved")
                else:
                    self.assertEqual(result["error"]["code"], "load-error")
                    self.assertEqual(result["stdout"], "before failure")

    def test_lost_reply_lookup_never_reloads(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            lookup = receive_request(stream)
            self.assertEqual(lookup[":op"], ":request-status")
            terminal = loaded(work)
            reply(stream, dict(response(lookup), **{":request-state": ":completed", ":result": terminal}))
            reply(stream, terminal)
            reply(stream, response(receive_request(stream), "42"))
        with peer(serve, advertise) as address, bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.load(mcp, timeout_ms=80)
            result = structured(mcp.receive())
            self.assertEqual(result["request_state"], "unresolved")
            self.load(mcp, "blocked")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            found = call(mcp, "genera_request_status", **identity(result))
            self.assertEqual(found["result"]["original_path"], "SYS:TEST;file.lisp")
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
        self.assertEqual(len(seen), 1)

    def test_preparation_reserves_execution_and_timeout_sends_nothing(self):
        def serve(stream, hello):
            status = receive_request(stream)
            self.assertEqual(status[":op"], ":status")
            reply(stream, response(status))
            work = receive_request(stream)
            self.assertEqual(work[":op"], ":eval")
            reply(stream, response(work, "42"))
        with peer(serve, advertise) as address, delayed_bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.load(mcp, timeout_ms=120)
            mcp.call("genera_eval", "busy", form="99")
            mcp.call("genera_session_status", "status", refresh=True)
            mcp.send(request("ping", "ping"))
            packets = {p["id"]: p for p in (mcp.receive(), mcp.receive(), mcp.receive(), mcp.receive())}
            self.assertEqual(structured(packets["busy"])["error"]["code"], "busy")
            expired = structured(packets["load"])
            self.assertEqual(expired["error"]["code"], "timeout")
            self.assertEqual(expired["request_state"], "not-submitted")
            self.assertIn("result", packets["ping"])
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    def test_eof_during_preparation_never_transmits_load(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve, advertise) as address, delayed_bridge(self.settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            self.load(mcp)
            time.sleep(.05)

    def native_settings(self, directory):
        self.assertIn("GENERA_SOURCE_GENERA_ROOT", os.environ)
        return dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=os.environ["GENERA_HOST"],
                    GENERA_PORT=os.environ.get("GENERA_PORT", "8888"), GENERA_SOURCE_ROOT=directory,
                    GENERA_SOURCE_GENERA_ROOT=os.environ["GENERA_SOURCE_GENERA_ROOT"])

    def fixture(self, mcp, directory, code):
        name = "mcp-load-" + uuid.uuid4().hex + ".lisp"
        root = os.environ["GENERA_SOURCE_GENERA_ROOT"]
        path = root + name
        result = call(mcp, "genera_eval", package="COMMON-LISP-USER", form=
                      f'(with-open-file (out {lisp_string(path)} :direction :output :if-exists :error '
                      f':if-does-not-exist :create) (write-line {lisp_string(code)} out) t)')
        self.assertTrue(result["ok"], result)
        Path(directory, name).write_text(code + "\n", encoding="ascii")
        return name, path

    @unittest.skipUnless(LIVE, "requires --live-v1, source root, and filesystem login")
    def test_live_definitions_output_warnings_errors_hash_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory, bridge(self.native_settings(directory)) as mcp:
            mcp.initialize()
            initial = mcp.ready()
            name = "mcp-loaded-" + uuid.uuid4().hex
            count = "*" + name + "-count*"
            self.assertTrue(call(mcp, "genera_eval", form=f"(setq {count} 0)")["ok"])
            leaf, path = self.fixture(mcp, directory, f'(progn (incf {count}) (defun {name} () 42) '
                                      '(write-string "loaded") (ansi-common-lisp:warn "load fixture warning") t)')
            result = call(mcp, "genera_load", path_kind="genera", path=path)
            self.assertTrue(result["ok"], result)
            self.assertIn("loaded", result["stdout"])
            self.assertTrue(result["warnings"], result)
            self.assertTrue(any("load fixture warning" in warning.lower() for warning in result["warnings"]), result)
            self.assertIn(leaf.lower(), result["resolved_path"].lower())
            self.assertEqual(call(mcp, "genera_eval", form=f"(list ({name}) {count})")["values"], ["(42 1)"])
            found = call(mcp, "genera_request_status", **identity(result))
            self.assertEqual(found["result"]["resolved_path"], result["resolved_path"])
            self.assertTrue(call(mcp, "genera_load", path_kind="relative", path=leaf)["ok"])
            Path(directory, leaf).write_text("; stale counterpart\n", encoding="ascii")
            self.assertEqual(call(mcp, "genera_load", path_kind="relative", path=leaf)["error"]["code"], "source-mismatch")
            missing = call(mcp, "genera_load", path_kind="genera", path=path + "-missing")
            self.assertEqual(missing["error"]["code"], "file-error", missing)
            _, bad = self.fixture(mcp, directory, '(progn (write-string "before-failure") (error "load fixture failure"))')
            failed = call(mcp, "genera_load", path_kind="genera", path=bad)
            self.assertEqual(failed["error"]["code"], "load-error", failed)
            self.assertIn("before-failure", failed["stdout"])
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":load", "dedupe-" + uuid.uuid4().hex,
                                      original_path=path, path_kind="genera", path=path,
                                      source_root=os.environ["GENERA_SOURCE_GENERA_ROOT"])
                send(stream, work)
                once = receive(stream)
                self.assertTrue(once[":ok"], once)
                send(stream, work)
                self.assertEqual(receive(stream), once)
            self.assertEqual(call(mcp, "genera_eval", form=f"(list ({name}) {count})")["values"], ["(42 3)"])
            self.assertEqual(call(mcp, "genera_session_status", refresh=True)["boot_id"], initial["boot_id"])

    @unittest.skipUnless(LIVE, "requires --live-v1, source root, and filesystem login")
    def test_live_timed_out_load_finishes_once_and_retains_identity(self):
        with tempfile.TemporaryDirectory() as directory, bridge(self.native_settings(directory)) as mcp:
            mcp.initialize()
            mcp.ready()
            count = "*mcp-load-timeout-" + uuid.uuid4().hex + "*"
            call(mcp, "genera_eval", form=f"(setq {count} 0)")
            _, path = self.fixture(mcp, directory, f'(progn (write-string "started") (sleep .8) (incf {count}))')
            self.load(mcp, path=path, timeout_ms=80)
            timed = structured(mcp.receive())
            self.assertEqual(timed["request_state"], "unresolved", timed)
            self.assertEqual(call(mcp, "genera_load", path_kind="genera", path=path)["error"]["code"], "busy")
            deadline = time.monotonic() + 5
            while True:
                found = call(mcp, "genera_request_status", **identity(timed))
                if found["request_state"] == "completed":
                    break
                self.assertLess(time.monotonic(), deadline, found)
                time.sleep(.05)
            self.assertIn("started", found["result"]["stdout"])
            self.assertEqual(found["result"]["request_id"], timed["request_id"])
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["1"])


if __name__ == "__main__":
    unittest.main()
