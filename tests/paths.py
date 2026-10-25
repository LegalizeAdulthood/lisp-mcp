"""Explicit source domains and canonical containment; optional live v1 proof."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from configuration import ROOT, SETTINGS, request, run_bridge, sbcl_command
from evaluation import receive_request
from protocol import lisp_string
from session import reply, response
from stdio import peer


class PathChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="source paths ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "sub").mkdir()
        (self.root / "sub/example.lisp").write_bytes(b"(+ 1 2)\r\n")
        self.settings = dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1",
                             GENERA_HOST="192.0.2.2", GENERA_SOURCE_ROOT=str(self.root),
                             GENERA_SOURCE_WINDOWS_ROOT="C:\\project\\src",
                             GENERA_SOURCE_GENERA_ROOT="SYS:SOURCE;")

    def run_lisp(self, body, settings=None):
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
        env.update(self.settings if settings is None else settings)
        body = ('(progn (defun path-test-json (value) (yason:encode value *standard-output*)) '
                + body.replace('(yason:encode ', '(path-test-json ') + ')')
        code = '(let ((*package* (find-package "LISP-MCP"))) (eval (read-from-string ' + lisp_string(body) + ')))'
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"),
                                                 "--eval", code], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def mapping(self, kind, text, mapping="*genera-source-mapping*"):
        return f'(genera-map-source {lisp_string(kind)} {lisp_string(text)} {mapping})'

    def test_windows_unix_relative_and_native_are_explicit(self):
        forms = [self.mapping("host", "c:/PROJECT/src/sub/example.lisp"),
                 self.mapping("host", str(self.root / "sub/example.lisp")),
                 self.mapping("relative", "sub/example.lisp"),
                 self.mapping("genera", "sys:source;sub;example.lisp")]
        result = self.run_lisp('(progn (configure) (yason:encode (mapcar (lambda (p) '
            '(list (genera-source-path-host-path p) (genera-source-path-genera-path p))) (list '
            + " ".join(forms) + '))))')
        for path in result[:3]:
            self.assertEqual(path, [str(self.root / "sub/example.lisp"), "SYS:SOURCE;sub;example.lisp"])
        self.assertEqual(result[3], [None, "sys:source;sub;example.lisp"])

    def test_traversal_alias_boundaries_missing_and_directory_reject(self):
        (self.root / "outside").symlink_to(self.root.parent)
        bad = [("relative", "../escape.lisp"), ("relative", "sub/../sub/example.lisp"),
               ("relative", "/sub/example.lisp"), ("relative", "sub//example.lisp"),
               ("relative", "sub\\example.lisp"), ("relative", "sub/*.lisp"),
               ("relative", "sub/missing.lisp"), ("relative", "sub"),
               ("relative", "outside/elsewhere.lisp"),
               ("host", "C:/project/src-other/example.lisp"),
               ("host", "D:/project/src/sub/example.lisp"),
               ("host", str(self.root) + "-other/file.lisp"),
               ("genera", "SYS:SOURCE;..;file.lisp"), ("genera", "SYS:SOURCE;file.*")]
        outside = self.root.parent / (self.root.name + "-outside.lisp")
        outside.write_text("outside", encoding="ascii")
        self.addCleanup(outside.unlink)
        (self.root / "escape.lisp").symlink_to(outside)
        bad.append(("relative", "escape.lisp"))
        body = '(progn (configure) (yason:encode (list ' + " ".join(
            '(handler-case (progn ' + self.mapping(k, p) + ' "accepted") '
            '(genera-path-error (e) (list (symbol-name (genera-path-error-kind e)) (princ-to-string e))))'
            for k, p in bad) + ')))'
        result = self.run_lisp(body)
        for (kind, original), rejection in zip(bad, result):
            self.assertIsInstance(rejection, list, (original, rejection))
            self.assertIn(original.replace("\\", "\\\\"), rejection[1])
            self.assertIn("Host attempt:", rejection[1])
            self.assertIn("Genera attempt:", rejection[1])
        self.assertEqual(result[-1][0], "OUTSIDE-ROOT")

    def test_absent_mappings_and_unverified_paths_never_fall_back(self):
        result = self.run_lisp('(progn (configure) (yason:encode (list '
            '(handler-case (genera-map-source "relative" "sub/example.lisp" nil) '
            '(genera-path-error (e) (symbol-name (genera-path-error-kind e)))) '
            '(handler-case (genera-resolve-source nil "relative" "sub/example.lisp") '
            '(genera-path-error (e) (symbol-name (genera-path-error-kind e)))))))')
        self.assertEqual(result, ["MAPPING-MISSING", "VERIFICATION-REQUIRED"])

    def test_configuration_is_syntax_only_and_rejects_invalid_roots(self):
        result = run_bridge(dict(self.settings, GENERA_SOURCE_ROOT="/absent/source/root"), [request("tools/list")])
        self.assertEqual(result.returncode, 0, result.stderr)
        for changes in ({"GENERA_SOURCE_ROOT": "relative"}, {"GENERA_SOURCE_ROOT": "/tmp/../src"},
                        {"GENERA_SOURCE_WINDOWS_ROOT": "C:relative"},
                        {"GENERA_SOURCE_GENERA_ROOT": "SYS:SOURCE"},
                        {"GENERA_SOURCE_GENERA_ROOT": "ID:/tmp/*/"},
                        {"LISP_MCP_PROFILE": "generic", "GENERA_PROTOCOL": "legacy"}):
            result = run_bridge(dict(self.settings, **changes), [request("tools/list")])
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(result.stdout, "")
            self.assertIn("Invalid configuration:", result.stderr)

    def resolve(self, address, target="sub/example.lisp", kind="relative", settings=None):
        options = dict(self.settings if settings is None else settings)
        options.update(GENERA_HOST=address[0], GENERA_PORT=str(address[1]))
        return self.run_lisp('(progn (configure) (let ((s (genera-open-session :host *genera-host* :port *genera-port*))) '
            '(unwind-protect (yason:encode (handler-case (let ((p (genera-resolve-source s '
            + lisp_string(kind) + ' ' + lisp_string(target) + '))) '
            '(jso "original" (genera-source-path-original p) "host" (genera-source-path-host-path p) '
            '"native" (genera-source-path-canonical-path p) "boot" (genera-source-path-boot-id p))) '
            '(genera-path-error (e) (jso "error" (symbol-name (genera-path-error-kind e)) "detail" (princ-to-string e))))) '
            '(genera-close-session s))))', options)

    def test_native_proof_and_content_mismatch_are_checked(self):
        checksum = zlib.adler32(b"(+ 1 2)\n")
        for checksum_value, expected in ((checksum, None), (checksum + 1, "SOURCE-MISMATCH")):
            def serve(stream, hello):
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":eval")
                self.assertEqual(work[":package"], "COMMON-LISP-USER")
                self.assertIn('(mcp-probe-source "SYS:SOURCE;" "SYS:SOURCE;sub;example.lisp")', work[":form"])
                value = f'("FIXTURE-HOST:>source>" "FIXTURE-HOST:>source>sub>example.lisp.1" 8 {checksum_value})'
                reply(stream, response(work, value))
            with peer(serve) as address:
                result = self.resolve(address)
            if expected:
                self.assertEqual(result["error"], expected)
                self.assertIn("SYS:SOURCE;sub;example.lisp", result["detail"])
            else:
                self.assertEqual(result["native"], "FIXTURE-HOST:>source>sub>example.lisp.1")
                self.assertEqual(result["boot"], "boot")

    def test_canonical_escape_and_malformed_proofs_fail(self):
        for value in ('("FIXTURE-HOST:>source>" "FIXTURE-HOST:>other>file.lisp.1" 8 1)',
                      '("FIXTURE-HOST:>source>" "FIXTURE-HOST:>source>..>file.lisp.1" 8 1)',
                      '("FIXTURE-HOST:>source>" "FIXTURE-HOST:>source>file.lisp.1" -1 1)',
                      '("FIXTURE-HOST:>source>" "FIXTURE-HOST:>source>file.lisp.1")', '#.(error "read")'):
            def serve(stream, hello):
                reply(stream, response(receive_request(stream), value))
            with peer(serve) as address:
                self.assertEqual(self.resolve(address)["error"], "VERIFICATION-FAILED")

    def test_uncertain_execution_blocks_probe_without_losing_identity(self):
        def serve(stream, hello):
            work = receive_request(stream)
            self.assertEqual(work[":form"], "pending")
            self.assertEqual(stream.recv(1), b"")
        with peer(serve) as address:
            settings = dict(self.settings, GENERA_HOST=address[0], GENERA_PORT=str(address[1]))
            result = self.run_lisp('(progn (configure) '
                '(let* ((s (genera-open-session :host *genera-host* :port *genera-port*)) '
                '(p (genera-submit-request s :eval :form "pending" :host-timeout .05))) '
                '(unwind-protect (progn (handler-case (genera-await-request s p) (genera-session-error () nil)) '
                '(path-test-json (list (handler-case (genera-resolve-source s "relative" "sub/example.lisp") '
                '(genera-path-error (e) (symbol-name (genera-path-error-kind e)))) '
                '(if (eq p (genera-session-execution s)) yason:true yason:false)))) '
                '(genera-close-session s))))', settings)
            self.assertEqual(result, ["VERIFICATION-FAILED", True])

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_existing_source_mirror_and_native_root(self):
        settings = dict(self.settings)
        for name in ("GENERA_SOURCE_ROOT", "GENERA_SOURCE_WINDOWS_ROOT", "GENERA_SOURCE_GENERA_ROOT"):
            self.assertIn(name, os.environ, "live paths require explicit source mappings")
            settings[name] = os.environ[name]
        native = settings["GENERA_SOURCE_GENERA_ROOT"]
        address = (os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888")))
        canonical = None
        for kind, path in (("host", settings["GENERA_SOURCE_WINDOWS_ROOT"].rstrip("/\\") + "\\genera-boot.lisp"),
                           ("host", str(Path(settings["GENERA_SOURCE_ROOT"]) / "genera-boot.lisp")),
                           ("relative", "genera-boot.lisp"), ("genera", native + "genera-boot.lisp")):
            result = self.resolve(address, path, kind, settings)
            self.assertNotIn("error", result, result)
            self.assertIn("genera-boot.lisp", result["native"].lower(), result)
            canonical = result["native"]
        alias = self.resolve(address, canonical, "genera", settings)
        self.assertEqual(alias["native"], canonical, alias)
        # Make a stale host counterpart without changing any Genera file.
        (self.root / "genera-boot.lisp").write_bytes(b"; deliberately stale source\n")
        mismatch = self.resolve(address, "genera-boot.lisp",
                                settings=dict(settings, GENERA_SOURCE_ROOT=str(self.root)))
        self.assertEqual(mismatch["error"], "SOURCE-MISMATCH", mismatch)
        outside = self.resolve(address, canonical, "genera",
                               dict(settings, GENERA_SOURCE_GENERA_ROOT=native + "restricted" + native[-1]))
        self.assertEqual(outside["error"], "VERIFICATION-FAILED", outside)


if __name__ == "__main__":
    unittest.main()
