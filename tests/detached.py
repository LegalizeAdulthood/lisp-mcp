"""Detached launch, inherited descriptors, logging, and early-exit checks."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import launch
from configuration import ROOT, SETTINGS
from evaluation import properties
from protocol import lisp_string


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"),
                     "requires Linux and cc for the disposable executable")
class DetachedChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="vlm detached build ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "vlm-fixture"
        result = subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-O2",
                                 str(ROOT / "tests/detached.c"), "-o", str(cls.binary)],
                                capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)

    def setUp(self):
        self.f = launch.LaunchChecks()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        shutil.copyfile(self.binary, self.f.executable)
        self.report = self.f.root / "child.json"
        self.log = self.f.root / "output.log"
        self.addCleanup(self.stop_child)

    def stop_child(self):
        if self.report.exists():
            try:
                pid = json.loads(self.report.read_text())["pid"]
                if Path(f"/proc/{pid}/exe").resolve() == self.f.executable:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, FileNotFoundError):
                pass

    def run_launch(self, body=None, environment=None, argv=None, log=None):
        q = lambda p: lisp_string(str(p))
        argv = argv or [str(self.f.executable), "-vm", "2000", "-debugger", "debugger with spaces",
                        "-world", str(self.f.world), "-network", "tap:192.0.2.2;mask=255.255.0.0",
                        "-partition", str(self.f.partition), "-foreground", "#F0EDCC", "-background", "#02343F"]
        setup = f'''(setf (getf preflight :root) {q(self.f.root)}
                          (getf preflight :log) {q(log or self.log)}
                          (getf preflight :argv) (list {' '.join(q(a) for a in argv)})
                          (getf (getf preflight :paths) :debugger) {q(self.f.executable)}
                          (getf (getf preflight :paths) :detacher) "/usr/bin/setsid")'''
        body = body or '''(genera-launch-vlm runtime :preflight preflight
                            :endpoint-observer (constantly :absent))'''
        # This inherited, non-CLOEXEC file must also be closed in the child.
        expression = f'''(progn {setup}
           (let ((fd (sb-posix:open {q(self.f.world)} sb-posix:o-rdonly)))
             (unwind-protect {body} (sb-posix:close fd))))'''
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
        env.update(VLM_FIXTURE_REPORT=str(self.report), DISPLAY=":fixture-42",
                   WAYLAND_DISPLAY="wayland-fixture", XDG_RUNTIME_DIR="/fixture/runtime")
        env.update(environment or {})
        result = subprocess.run(self.f.command(expression), cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = properties(result.stdout)
        self.assertNotIn("fixture stdout", result.stdout)
        self.assertNotIn("fixture stderr", result.stderr)
        return report, argv

    def test_argv_cwd_environment_descriptors_and_parent_exit(self):
        # Metacharacters remain one literal path; no shell interprets them.
        world = self.f.root / "world ; $literal.vlod"
        self.f.world.rename(world)
        self.f.world = world
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (world, self.f.partition)]
        report, argv = self.run_launch(environment={"VLM_FIXTURE_DELAY": "1500"})
        self.assertEqual(report[":action"], ":launched", report)
        child = json.loads(self.report.read_text())
        self.assertEqual(child["argv"], argv)
        self.assertEqual(child["cwd"], str(self.f.root))
        self.assertEqual(child["pid"], child["sid"])
        self.assertEqual(child["pid"], child["pgid"])
        self.assertTrue(child["stdin_eof"])
        self.assertEqual(child["environment"], [":fixture-42", "wayland-fixture", "/fixture/runtime"])
        self.assertEqual(child["fds"], {"0": "/dev/null", "1": str(self.log), "2": str(self.log)})
        self.assertTrue(Path(f'/proc/{child["pid"]}').exists(), "child died with SBCL parent")
        self.assertEqual(json.loads(self.f.metadata_path().read_text())[2], child["pid"])
        self.assertEqual(self.f.probe()[":action"], ":observe", "child inherited launch locks")
        deadline = time.monotonic() + 5
        while "after parent exit" not in self.log.read_text() and time.monotonic() < deadline:
            time.sleep(.03)
        text = self.log.read_text()
        for stream in ("stdout", "stderr"):
            self.assertIn(f"fixture {stream} before parent exit", text)
            self.assertIn(f"fixture {stream} after parent exit", text)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (world, self.f.partition)])

    def test_early_exit_is_reported_once_with_log_and_exit_code(self):
        report, _ = self.run_launch(environment={"VLM_FIXTURE_DELAY": "20", "VLM_FIXTURE_EXIT": "17"})
        self.assertEqual(report[":action"], ":failed", report)
        self.assertEqual(report[":code"], ":early-exit")
        self.assertEqual(report[":exit-code"], 17)
        self.assertTrue(report[":attempted"])
        self.assertEqual(report[":log"], str(self.log))
        self.assertEqual(self.log.read_text().count("fixture stdout before"), 1)

    def test_later_exit_observation_never_respawns(self):
        body = '''(multiple-value-bind (report attempt)
                    (genera-launch-vlm runtime :preflight preflight :endpoint-observer (constantly :absent))
                    (declare (ignore report))
                    (sleep .5)
                    (genera-launch-observation attempt))'''
        report, _ = self.run_launch(body, environment={"VLM_FIXTURE_DELAY": "350", "VLM_FIXTURE_EXIT": "23"})
        self.assertEqual(report[":state"], ":exited", report)
        self.assertEqual(report[":exit-code"], 23)
        self.assertEqual(self.log.read_text().count("fixture stdout before"), 1)

    def test_observer_death_does_not_report_a_live_vlm_as_exited(self):
        body = '''(multiple-value-bind (report attempt)
                    (genera-launch-vlm runtime :preflight preflight :endpoint-observer (constantly :absent))
                    (when (eql (getf report :pid) (getf report :launcher-pid))
                      (error "This fixture requires the separate setsid observer"))
                    (sb-posix:kill (getf report :launcher-pid) sb-posix:sigterm)
                    (sleep .05)
                    (let ((live (genera-launch-observation attempt)))
                      (sleep .6)
                      (let ((done (genera-launch-observation attempt)))
                        (list :live-state (getf live :state) :observer-lost (getf live :observer-lost)
                              :final-state (getf done :state) :exit-code (getf done :exit-code)))))'''
        report, _ = self.run_launch(body, environment={"VLM_FIXTURE_DELAY": "650"})
        self.assertEqual(report[":live-state"], ":running", report)
        self.assertTrue(report[":observer-lost"])
        self.assertEqual(report[":final-state"], ":exited")
        self.assertIsNone(report[":exit-code"], "observer death cannot establish the VLM exit code")
        self.assertEqual(self.log.read_text().count("fixture stdout before"), 1)

    def test_private_log_appends_and_rejects_runtime_aliases(self):
        self.log.write_text("retained log\n")
        self.log.chmod(0o600)
        report, _ = self.run_launch(environment={"VLM_FIXTURE_DELAY": "20"})
        self.assertTrue(self.log.read_text().startswith("retained log\n"), report)
        self.report.unlink()
        self.log.unlink()
        self.log.symlink_to(self.f.world)
        report, _ = self.run_launch()
        self.assertEqual(report[":action"], ":failed", report)
        self.assertFalse(report[":attempted"])
        self.assertFalse(self.report.exists())
        self.assertEqual(self.f.world.read_text(), "fixture world")

    def test_existing_process_is_observed_without_a_second_launch(self):
        report, _ = self.run_launch()
        self.assertEqual(report[":action"], ":launched", report)
        report, _ = self.run_launch()
        self.assertEqual(report[":action"], ":observe", report)
        self.assertEqual(self.log.read_text().count("fixture stdout before"), 1)

    def test_missing_executable_exits_once_without_a_fixture_process(self):
        report, _ = self.run_launch(argv=[str(self.f.root / "missing"), "-world",
                                         str(self.f.world), "-partition", str(self.f.partition)])
        self.assertEqual(report[":action"], ":failed", report)
        self.assertEqual(report[":code"], ":early-exit")
        self.assertEqual(report[":exit-code"], 127)
        self.assertTrue(report[":attempted"])
        self.assertFalse(self.report.exists())
        self.assertIn("failed to execute", self.log.read_text())

    def test_missing_log_parents_are_private_and_lock_log_is_rejected(self):
        log = self.f.root / "new/private/vlm.log"
        report, _ = self.run_launch(log=log, environment={"VLM_FIXTURE_DELAY": "20"})
        self.assertEqual(report[":code"], ":early-exit", report)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        self.report.unlink()
        report, _ = self.run_launch(log=self.f.state / "launch.lock")
        self.assertEqual(report[":action"], ":failed", report)
        self.assertFalse(report[":attempted"])
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
