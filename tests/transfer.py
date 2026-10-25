"""Opt-in bounded source transfer; --live-v1 uses an existing native root."""
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

from configuration import ROOT, request, run_bridge, sbcl_command
from concurrency import client, receive, request as native_request, send
from deadlines import identity
from evaluation import receive_request
from interrupt import call
from loading import delayed_bridge, loaded
from compiling import compiled
from protocol import lisp_string
from session import reply, response
from stdio import Bridge, bridge, genera_settings, peer, structured


def advertise(hello):
    hello[":features"].extend([":load", ":compile-file", ":source-transfer"])


def transferred(work):
    packet = compiled(work) if work[":op"] == ":compile-file" else loaded(work)
    packet.update({":transfer": True, ":cleanup": "retained",
                   ":temporary-path": packet[":resolved-path"]})
    return packet


class TransferChecks(unittest.TestCase):
    def settings(self, address, directory):
        return dict(genera_settings(address), GENERA_SOURCE_ROOT=directory,
                    GENERA_SOURCE_TRANSFER_ROOT="SYS:TEMP;")

    def load(self, mcp, **changes):
        mcp.call("genera_load", "transfer", **dict(dict(path="file.lisp", path_kind="relative", transfer=True), **changes))

    def test_portable_transfer_contract_and_retained_identity(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/transfer.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Transfer unit checks: 17 passed.", result.stdout)

    def test_configuration_is_explicit_and_syntax_only(self):
        base = dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST="192.0.2.2")
        for changes in ({"GENERA_SOURCE_TRANSFER_ROOT": "SYS:TEMP;"},
                        {"GENERA_SOURCE_ROOT": "/source", "GENERA_SOURCE_TRANSFER_ROOT": "relative"},
                        {"LISP_MCP_PROFILE": "generic", "GENERA_PROTOCOL": "legacy", "GENERA_SOURCE_ROOT": "/source",
                         "GENERA_SOURCE_TRANSFER_ROOT": "SYS:TEMP;"}):
            result = run_bridge(dict(base, **changes), [])
            self.assertNotEqual(result.returncode, 0)
        result = run_bridge(dict(base, GENERA_SOURCE_ROOT="/absent/source", GENERA_SOURCE_TRANSFER_ROOT="SYS:TEMP;"),
                            [request("initialize", 1), request("tools/list", 2)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("transfer", result.stdout)

    def test_transfer_load_and_compile_share_one_prepared_request(self):
        def serve(stream, hello):
            for operation in (":load", ":compile-file"):
                work = receive_request(stream)
                self.assertEqual(work[":op"], operation)
                self.assertIs(work[":transfer"], True)
                self.assertEqual(work[":source-text"], "; comment\n(+ 1 2)\n")
                self.assertEqual(work[":source-size"], len(work[":source-text"]))
                self.assertEqual(work[":source-root"], "SYS:TEMP;")
                reply(stream, transferred(work))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "file.lisp").write_bytes(b"; comment\r\n(+ 1 2)\r")
            with peer(serve, advertise) as address, bridge(self.settings(address, directory)) as mcp:
                mcp.initialize()
                mcp.ready()
                for tool in ("genera_load", "genera_compile_file"):
                    result = call(mcp, tool, path="file.lisp", path_kind="relative", transfer=True)
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(result["transfer"])
                    self.assertEqual(result["cleanup"], "retained")
                    self.assertEqual(result["temporary_path"], result["resolved_path"])
                    self.assertNotIn("source_text", result)
            self.assertNotIn("; comment", "".join(mcp.errors))

    def test_disabled_or_unsupported_transfer_never_sends_a_file(self):
        for disabled in (True, False):
            def serve(stream, hello):
                self.assertEqual(stream.recv(1), b"")
            with tempfile.TemporaryDirectory() as directory:
                settings = self.settings(("127.0.0.2", 8888), directory)
                Path(directory, "file.lisp").write_text("(+ 1 2)", encoding="ascii")
                if disabled:
                    del settings["GENERA_SOURCE_TRANSFER_ROOT"]
                    settings["GENERA_SOURCE_GENERA_ROOT"] = "SYS:TEST;"
                # LOAD is advertised so the transfer capability is the gate.
                with peer(serve, lambda h: h[":features"].append(":load")) as enabled_address:
                    settings.update(genera_settings(enabled_address))
                    with bridge(settings) as mcp:
                        mcp.initialize()
                        mcp.ready()
                        self.load(mcp)
                        result = structured(mcp.receive())
                        self.assertEqual(result["request_state"], "not-submitted")
                        self.assertEqual(result["error"]["code"], "mapping-missing" if disabled else "unsupported")

    def test_rejects_bounds_encoding_traversal_and_symlink_escape(self):
        def serve(stream, hello):
            work = receive_request(stream)
            self.assertEqual(work[":op"], ":eval")
            reply(stream, response(work, "42"))
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "big.lisp").write_bytes(b"a" * 65537)
            (root / "unicode.lisp").write_bytes(b"\xc3\xa9")
            Path(outside, "escape.lisp").write_text("(+ 1 2)", encoding="ascii")
            (root / "escape.lisp").symlink_to(Path(outside, "escape.lisp"))
            with peer(serve, advertise) as address, bridge(self.settings(address, directory)) as mcp:
                mcp.initialize()
                mcp.ready()
                for path in ("big.lisp", "unicode.lisp", "../file.lisp", "escape.lisp"):
                    self.load(mcp, path=path)
                    result = structured(mcp.receive())
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["request_state"], "not-submitted")
                self.load(mcp, transfer="yes")
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
                self.load(mcp, path_kind="genera", path="SYS:TEMP;file.lisp")
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
                self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    def test_no_implicit_transfer_after_native_file_failure(self):
        def serve(stream, hello):
            work = receive_request(stream)
            self.assertNotIn(":transfer", work)
            self.assertNotIn(":source-text", work)
            reply(stream, loaded(work, failed=True))
            self.assertEqual(stream.recv(1), b"")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "file.lisp").write_text("(+ 1 2)", encoding="ascii")
            with peer(serve, advertise) as address:
                settings = dict(self.settings(address, directory), GENERA_SOURCE_GENERA_ROOT="SYS:SHARED;")
                with bridge(settings) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    self.load(mcp, transfer=False)
                    self.assertFalse(structured(mcp.receive())["ok"])

    def test_replaced_symlink_is_rejected_after_open(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            Path(directory, "file.lisp").write_text("inside", encoding="ascii")
            escape = Path(outside, "file.lisp")
            escape.write_text("outside contents must not transfer", encoding="ascii")
            # Replace the validated pathname before its file is opened.
            race = ('(let ((original (symbol-function (quote lisp-mcp::genera-map-source)))) '
                    '(setf (symbol-function (quote lisp-mcp::genera-map-source)) '
                    '(lambda (&rest args) (let* ((path (apply original args)) '
                    '(file (lisp-mcp::genera-source-path-host-path path))) '
                    '(delete-file file) (sb-posix:symlink ' + lisp_string(str(escape)) + ' file) path))))')
            with peer(serve, advertise) as address:
                mcp = Bridge(self.settings(address, directory), command=sbcl_command() +
                             ["--load", str(ROOT / "lisp-mcp.lisp"), "--eval", race, "--eval", "(lisp-mcp:main)"])
                try:
                    mcp.initialize()
                    mcp.ready()
                    self.load(mcp)
                    result = structured(mcp.receive())
                    self.assertEqual(result["error"]["code"], "source-unreadable", result)
                    self.assertEqual(result["request_state"], "not-submitted")
                finally:
                    mcp.close()

    def test_preparation_timeout_and_lost_reply_never_retransfer(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            lookup = receive_request(stream)
            terminal = transferred(work)
            reply(stream, dict(response(lookup), **{":request-state": ":completed", ":result": terminal}))
            reply(stream, terminal)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "file.lisp").write_text("(+ 1 2)", encoding="ascii")
            with peer(serve, advertise) as address, delayed_bridge(self.settings(address, directory)) as mcp:
                mcp.initialize()
                mcp.ready()
                self.load(mcp, timeout_ms=100)
                self.assertEqual(structured(mcp.receive())["request_state"], "not-submitted")
                self.load(mcp, timeout_ms=850)
                result = structured(mcp.receive())
                self.assertEqual(result["request_state"], "unresolved", result)
                found = call(mcp, "genera_request_status", **identity(result))
                self.assertTrue(found["result"]["transfer"])
        self.assertEqual(len(seen), 1)

    @unittest.skipUnless(LIVE, "requires --live-v1 and an existing writable native transfer root")
    def test_live_transferred_load_compile_failure_and_timeout(self):
        host = os.environ["GENERA_HOST"]
        port = int(os.environ.get("GENERA_PORT", "8888"))
        root = os.environ["GENERA_SOURCE_TRANSFER_ROOT"]
        def evaluate(form):
            with client(host, port) as (stream, hello):
                send(stream, native_request(hello, ":eval", "transfer-control-" + uuid.uuid4().hex, form=form))
                result = receive(stream)
                self.assertTrue(result[":ok"], result)
                return result
        self.assertEqual(evaluate("(null *mcp-transfer-root*)")[":values"], ["T"])
        evaluate(f"(progn (setq *mcp-transfer-root* {lisp_string(root)}) t)")
        try:
            with tempfile.TemporaryDirectory() as directory:
                settings = dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=host,
                                GENERA_PORT=str(port), GENERA_SOURCE_ROOT=directory, GENERA_SOURCE_TRANSFER_ROOT=root)
                with bridge(settings) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    fn = "mcp-transferred-" + uuid.uuid4().hex
                    count = "*" + fn + "-count*"
                    self.assertTrue(call(mcp, "genera_eval", form=f"(defvar {count} 0)")["ok"])
                    source = Path(directory, "host-only.lisp")
                    code = f'(progn (incf {count}) (defun {fn} () 42) (write-string "transferred"))'
                    source.write_bytes(("; transfer fixture\r\n" + code + "\r\n").encode("ascii"))
                    result = call(mcp, "genera_load", path_kind="relative", path=source.name, transfer=True)
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(result["transfer"])
                    self.assertEqual(result["cleanup"], "retained")
                    self.assertEqual(result["temporary_path"], result["resolved_path"])
                    self.assertIn("mcp-transfer-", result["temporary_path"].lower())
                    self.assertIn("transferred", result["stdout"])
                    self.assertEqual(call(mcp, "genera_eval", form=f"(list ({fn}) {count})")["values"], ["(42 1)"])
                    saved = call(mcp, "genera_request_status", **identity(result))
                    self.assertEqual(saved["result"]["temporary_path"], result["temporary_path"])
                    other = call(mcp, "genera_compile_file", path_kind="host", path=str(source), transfer=True, load=True)
                    self.assertTrue(other["ok"], other)
                    self.assertTrue(other["loaded"])
                    self.assertNotEqual(other["temporary_path"], result["temporary_path"])
                    source.write_text('(error "transferred failure")', encoding="ascii")
                    failed = call(mcp, "genera_load", path_kind="relative", path=source.name, transfer=True)
                    self.assertEqual(failed["error"]["code"], "load-error", failed)
                    self.assertTrue(failed["temporary_path"])
                    source.write_text(f"(progn (sleep .6) (incf {count}))", encoding="ascii")
                    uncertain = call(mcp, "genera_load", path_kind="relative", path=source.name, transfer=True, timeout_ms=80)
                    self.assertEqual(uncertain["request_state"], "unresolved", uncertain)
                    deadline = time.monotonic() + 5
                    while True:
                        found = call(mcp, "genera_request_status", **identity(uncertain))
                        if found.get("result"):
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.04)
                    self.assertTrue(found["result"]["transfer"])
                    self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["3"])
                    text = f"(incf {count})"
                    with client(host, port) as (stream, hello):
                        work = native_request(hello, ":load", "transfer-dedupe-" + uuid.uuid4().hex,
                                              original_path=source.name, path_kind="relative", host_path=str(source),
                                              path=root + source.name, source_root=root, transfer=True,
                                              source_text=text, source_size=len(text), source_hash=zlib.adler32(text.encode("ascii")))
                        send(stream, work)
                        terminal = receive(stream)
                        self.assertTrue(terminal[":ok"], terminal)
                        send(stream, work)
                        self.assertEqual(receive(stream), terminal)
                        refused = dict(work, **{":id": "wrong-root-" + uuid.uuid4().hex,
                                                ":source-root": root + "unconfigured;"})
                        send(stream, refused)
                        failed = receive(stream)
                        self.assertFalse(failed[":ok"], failed)
                        self.assertIn(":transfer-error", failed[":error"])
                        self.assertNotIn(":temporary-path", failed)
                    self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["4"])
                    retained = call(mcp, "genera_eval", form=
                                    f'(not (null (probe-file {lisp_string(terminal[":temporary-path"])})))')
                    self.assertEqual(retained["values"], ["T"])
        finally:
            evaluate("(progn (setq *mcp-transfer-root* nil) t)")


if __name__ == "__main__":
    unittest.main()
