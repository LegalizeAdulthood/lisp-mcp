"""Read-only launch preflight, with filesystem fixtures and explicit OS facts."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LIVE = "--live-runtime" in sys.argv
if LIVE:
    sys.argv.remove("--live-runtime")

from configuration import ROOT, SETTINGS, request, run_bridge, sbcl_command
from evaluation import properties
from protocol import lisp_string


FACTS = r'''
(setf (getf facts :wsl) t (getf facts :uid) 1000 (getf facts :real-uid) 1000
      (getf facts :user) "fixture-user" (getf facts :groups) '(1000 1001)
      (getf facts :display) ":17" (getf facts :display-socket) '(:kind :socket :write t)
      (getf facts :tun) '(:kind :character :read t :write t)
      (getf facts :tap-flags) #x1802 (getf facts :link-flags) #x1003
      (getf facts :tap-owner) 1000 (getf facts :tap-group) -1)
(dolist (key '(:root :executable :world :partition :debugger :state-directory :log :log-parent))
  (setf (getf (getf facts key) :uid) 1000))
'''


def as_dict(value):
    return dict(zip(value[::2], value[1::2]))


@unittest.skipUnless(sys.platform.startswith("linux"), "requires SBCL/Linux filesystem inspection")
class PreflightChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vlm fixture ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        header[18] = 62
        for name, data in (("genera", header), ("VLM_debugger", b"debugger"),
                           ("world.vlod", b"saved world"), ("fep0", b"partition")):
            path = self.root / name
            path.write_bytes(data)
            path.chmod(0o700)
        (self.root / "state").mkdir(mode=0o700)
        self.settings = dict(
            LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST="192.0.2.2",
            GENERA_RUNTIME_MODE="local", GENERA_AUTOSTART="true", GENERA_VLM_ROOT=str(self.root),
            GENERA_VLM_WORLD="world.vlod", GENERA_VLM_PARTITION="fep0", GENERA_VLM_USER="fixture-user",
            GENERA_VLM_NETWORK="fixturetap:192.0.2.2;mask=255.255.0.0;gateway=192.0.2.1;preconfiguredTAP=yes",
            GENERA_STATE_DIR=str(self.root / "state"),
        )

    def probe(self, changes="", settings=None, fixture=True):
        env = {key: value for key, value in os.environ.items() if key not in SETTINGS}
        env.update(self.settings if settings is None else settings)
        program = '(progn (lisp-mcp::configure) (in-package :lisp-mcp))'
        body = ('(let* ((runtime *genera-runtime*) (facts (genera-local-facts runtime))) '
                + FACTS + changes + ' (genera-check-prerequisites runtime facts))'
                if fixture else '(genera-preflight)')
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"), "--eval", program,
                             "--eval", '(let ((*print-case* :downcase)) (prin1 ' + body + '))'],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = properties(result.stdout)
        report[":checks"] = {row[":name"]: row for row in map(as_dict, report[":checks"] or [])}
        return report

    def rejected(self, report, name, code):
        self.assertIsNone(report[":ok"], report)
        self.assertIsNone(report[":argv"])
        self.assertEqual(report[":checks"][name][":code"], code)

    def test_ready_argv_canonical_paths_spaces_and_no_side_effects(self):
        before = {str(p.relative_to(self.root)): (p.stat().st_mtime_ns,
                  hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in self.root.rglob("*") if p.is_file()}
        (self.root / "vlm-link").symlink_to(self.root / "genera")
        self.settings["GENERA_VLM_EXECUTABLE"] = "vlm-link"
        report = self.probe()
        self.assertTrue(report[":ok"], report)
        self.assertTrue(report[":launch-permitted"])
        self.assertEqual(report[":argv"], [str(self.root / "genera"), "-vm", "2000", "-debugger",
            str(self.root / "VLM_debugger"), "-world", str(self.root / "world.vlod"), "-network",
            self.settings["GENERA_VLM_NETWORK"], "-partition", str(self.root / "fep0"),
            "-foreground", "#F0EDCC", "-background", "#02343F"])
        self.assertFalse((self.root / "state/vlm.log").exists())
        self.assertEqual(sorted(p.name for p in (self.root / "state").iterdir()), [])
        for name, (modified, digest) in before.items():
            self.assertEqual((self.root / name).stat().st_mtime_ns, modified)
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest)
        report = self.probe(settings=dict(self.settings, GENERA_AUTOSTART="false"))
        self.assertTrue(report[":ok"])
        self.assertIsNone(report[":launch-permitted"])

    def test_missing_runtime_files_and_invalid_executable(self):
        for setting, name in (("GENERA_VLM_EXECUTABLE", "executable"), ("GENERA_VLM_WORLD", "world"),
                              ("GENERA_VLM_PARTITION", "partition"), ("GENERA_VLM_DEBUGGER", "debugger")):
            with self.subTest(setting=setting):
                settings = dict(self.settings, **{setting: "missing"})
                self.rejected(self.probe(settings=settings), ":" + name, ":missing-" + name)
        (self.root / "genera").write_bytes(b"#!/bin/sh\nexit 0\n")
        self.rejected(self.probe(), ":executable", ":invalid-executable")

    @unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0,
                     "root bypasses ordinary file access permissions")
    def test_partition_must_be_writable_world_must_be_readable(self):
        (self.root / "fep0").chmod(0o400)
        self.rejected(self.probe(), ":partition", ":invalid-partition")
        (self.root / "world.vlod").chmod(0)
        self.rejected(self.probe(), ":world", ":invalid-world")

    def test_wrong_user_root_and_non_wsl_are_specific(self):
        for change in ('(setf (getf facts :user) "other")', '(setf (getf facts :uid) 0)',
                       '(setf (getf facts :real-uid) 2000)'):
            self.rejected(self.probe(change), ":user", ":wrong-user")
        self.rejected(self.probe('(setf (getf facts :wsl) nil)'), ":platform", ":not-wsl")

    def test_display_and_tap_failures_and_group_access(self):
        for change, name, code in [
            ('(setf (getf facts :display) nil)', ":display", ":missing-display"),
            ('(setf (getf facts :display-socket) nil)', ":display", ":display-unavailable"),
            ('(setf (getf facts :tap-flags) nil)', ":tap", ":missing-tap"),
            ('(setf (getf facts :link-flags) 0)', ":tap", ":missing-tap"),
            ('(setf (getf facts :tap-owner) 0)', ":tap-access", ":tap-inaccessible"),
            ('(setf (getf facts :tap-owner) 0 (getf facts :tap-group) 1001)', ":tap-access", ":tap-inaccessible"),
            ('(setf (getf facts :tap-group) 2000)', ":tap-access", ":tap-inaccessible"),
            ('(setf (getf facts :tun) nil)', ":tap-access", ":tap-inaccessible"),
        ]:
            with self.subTest(code=code):
                self.rejected(self.probe(change), name, code)
        report = self.probe('(setf (getf facts :tap-owner) -1 (getf facts :tap-group) 1001)')
        self.assertTrue(report[":ok"], report)

    def test_log_collision_private_state_and_missing_log_ancestors(self):
        settings = dict(self.settings, GENERA_VLM_LOG=str(self.root / "world.vlod"))
        self.rejected(self.probe(settings=settings), ":file-identities", ":runtime-file-collision")
        (self.root / "alias.log").hardlink_to(self.root / "world.vlod")
        settings["GENERA_VLM_LOG"] = str(self.root / "alias.log")
        self.rejected(self.probe(settings=settings), ":file-identities", ":runtime-file-collision")
        (self.root / "state").chmod(0o755)
        self.rejected(self.probe(), ":state-directory", ":insecure-state-directory")
        settings = dict(self.settings, GENERA_STATE_DIR=str(self.root / "new/state"))
        self.assertTrue(self.probe(settings=settings)[":ok"])
        self.assertFalse((self.root / "new").exists())
        report = self.probe('(setf (getf (getf facts :state-parent) :write) nil)', settings=settings)
        self.rejected(report, ":state-directory", ":insecure-state-directory")

    def test_attach_only_does_not_inspect_local_files(self):
        report = self.probe(settings=dict(GENERA_HOST="remote.example"), fixture=False)
        self.assertTrue(report[":ok"])
        self.assertEqual(report[":mode"], ":attach")
        self.assertIsNone(report[":launch-permitted"])
        self.assertEqual(report[":checks"], {})

    def test_invalid_launch_configuration_stops_before_dispatch(self):
        for update, variable in [
            ({"GENERA_RUNTIME_MODE": "invalid"}, "GENERA_RUNTIME_MODE"),
            ({"LISP_MCP_PROFILE": "generic", "GENERA_PROTOCOL": "legacy"}, "GENERA_RUNTIME_MODE"),
            ({"GENERA_AUTOSTART": "1"}, "GENERA_AUTOSTART"),
            ({"GENERA_RUNTIME_MODE": "attach"}, "GENERA_AUTOSTART"),
            ({"GENERA_HOST": "remote.example"}, "GENERA_HOST"),
            ({"GENERA_VLM_ROOT": "relative"}, "GENERA_VLM_ROOT"),
            ({"GENERA_VLM_WORLD": "*.vlod"}, "GENERA_VLM_WORLD"),
            ({"GENERA_VLM_PARTITION": "C:\\fep0"}, "GENERA_VLM_PARTITION"),
            ({"GENERA_VLM_PARTITION": "C:/fep0"}, "GENERA_VLM_PARTITION"),
            ({"GENERA_VLM_NETWORK": "tap0:192.0.2.2;preconfiguredTAP=no"}, "GENERA_VLM_NETWORK"),
            ({"GENERA_PORT": "9999"}, "GENERA_PORT"),
        ]:
            with self.subTest(update=update):
                result = run_bridge(dict(self.settings, **update), [request("tools/list")])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("Invalid configuration: " + variable, result.stderr)

    @unittest.skipUnless(LIVE, "requires --live-runtime and explicit local runtime settings")
    def test_actual_wsl_prerequisites(self):
        settings = {name: os.environ[name] for name in SETTINGS if name in os.environ}
        self.assertEqual(settings.get("GENERA_RUNTIME_MODE"), "local")
        report = self.probe(settings=settings, fixture=False)
        self.assertTrue(report[":ok"], report)
        self.assertTrue(all(check[":ok"] for check in report[":checks"].values()))
        print("WSL preflight: runtime files, user, display, TAP, and log checks passed.")


if __name__ == "__main__":
    unittest.main()
