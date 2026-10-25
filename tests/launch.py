"""Linux VLM discovery and advisory-lock decisions; never starts Genera."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from configuration import ROOT, SETTINGS, sbcl_command
from evaluation import properties
from protocol import lisp_string


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc and flock")
class LaunchChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vlm identity ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "vlm-fixture"
        shutil.copyfile(sys.executable, self.executable)
        self.executable.chmod(0o700)
        self.world = self.root / "world.vlod"
        self.partition = self.root / "fep0"
        self.world.write_text("fixture world")
        self.partition.write_text("fixture partition")
        self.state = self.root / "state"
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    def start(self, world=None, partition=None, executable=None, extra=()):
        process = subprocess.Popen(
            [str(executable or self.executable), "-c", "import time; time.sleep(90)",
             "-world", str(world or self.world), "-partition", str(partition or self.partition), *extra],
            cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        self.assertIsNone(process.poll())
        return process

    def command(self, body, world=None, partition=None, state=None):
        q = lambda p: lisp_string(str(p))
        prefix = f'''(let* ((runtime (%make-genera-runtime :mode :local :autostart t
                          :root {q(self.root)} :guest "127.0.0.2" :state-directory {q(state or self.state)}))
                          (preflight (list :launch-permitted t :paths
                            (list :executable {q(self.executable)} :world {q(world or self.world)}
                                  :partition {q(partition or self.partition)})))
                          (target (genera-launch-target preflight)))
                     (declare (ignorable target)) {body})'''
        expression = ('(let ((*print-case* :downcase)) (prin1 (handler-case ' + prefix +
                      ' (error (e) (list :action :error :message (princ-to-string e))))))')
        return sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"),
            "--eval", "(lisp-mcp::genera-load-launch-module)",
            "--eval", "(in-package :lisp-mcp)", "--eval", expression]

    def probe(self, body=None, **options):
        body = body or '''(genera-call-with-launch-decision runtime
                          (lambda (decision context) (declare (ignore context)) decision)
                          :preflight preflight :endpoint-observer (lambda (runtime)
                            (declare (ignore runtime)) :absent))'''
        result = subprocess.run(self.command(body, **options), cwd=ROOT,
            env={k: v for k, v in os.environ.items() if k not in SETTINGS},
            capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        return properties(result.stdout)

    def metadata_path(self):
        return next(self.state.glob("world-*/identity.json"))

    def write_metadata(self, data):
        path = self.metadata_path()
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def test_absent_decision_creates_private_state_without_touching_world(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.world, self.partition)]
        report = self.probe()
        self.assertEqual(report[":action"], ":launch", report)
        self.assertEqual(report[":metadata"], ":missing")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.world, self.partition)])
        for path in [self.state, *self.state.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertFalse(list(self.state.glob("world-*/identity.json")))

    def test_missing_state_parents_and_world_alias_share_the_lock(self):
        alias = self.root / "world-alias"
        alias.hardlink_to(self.world)
        nested = self.root / "new/state"
        self.assertEqual(self.probe(state=nested)[":action"], ":launch")
        self.assertEqual(self.probe(state=nested, world=alias)[":action"], ":launch")
        self.assertEqual(len(list(nested.glob("world-*"))), 1)

    def test_disabled_launch_and_unknown_endpoint_never_select_owner(self):
        body = '''(progn (setf (getf preflight :launch-permitted) nil)
                    (genera-call-with-launch-decision runtime
                      (lambda (decision context) (declare (ignore decision context))
                        (error "consumer must not run")) :preflight preflight))'''
        self.assertEqual(self.probe(body)[":code"], ":launch-disabled")
        self.assertFalse(self.state.exists())
        body = '''(genera-call-with-launch-decision runtime
                    (lambda (decision context) (declare (ignore context)) decision)
                    :preflight preflight :endpoint-observer (constantly :unknown))'''
        self.assertEqual(self.probe(body)[":code"], ":endpoint-unknown")

    def test_proc_stat_accepts_parentheses_in_process_name(self):
        text = "123 (name with ) parentheses)) S " + " ".join(["0"] * 18 + ["987654"])
        report = self.probe(f'''(genera-proc-stat {lisp_string(text)})''')
        self.assertEqual(report[":start-time"], 987654)

    def test_manual_process_relative_paths_and_symlinks_are_observed(self):
        link = self.root / "world-link"
        link.symlink_to(self.world)
        process = self.start(world="world-link", partition="fep0")
        report = self.probe()
        self.assertEqual(report[":code"], ":vlm-present", report)
        data = json.loads(self.metadata_path().read_text())
        self.assertEqual(data[2], process.pid)
        self.assertEqual(data[5], str(self.executable))
        self.assertEqual(data[8], str(self.world))
        self.assertEqual(self.probe()[":metadata"], ":current")

    def test_stale_pid_start_time_owner_and_boot_metadata_are_rejected(self):
        self.start()
        self.assertEqual(self.probe()[":action"], ":observe")
        original = json.loads(self.metadata_path().read_text())
        for index, value in [(2, os.getpid()), (3, original[3] + 1),
                             (1, original[1] + 1), (4, "previous-linux-boot")]:
            with self.subTest(field=index):
                changed = original.copy()
                changed[index] = value
                self.write_metadata(changed)
                report = self.probe()
                self.assertEqual(report[":metadata"], ":stale", report)
                self.assertEqual(report[":action"], ":observe")
                self.assertEqual(json.loads(self.metadata_path().read_text()), original)
        self.stop_processes()
        self.write_metadata(original)
        report = self.probe()
        self.assertEqual(report[":metadata"], ":stale")
        self.assertEqual(report[":action"], ":launch")
        self.assertFalse(list(self.state.glob("world-*/identity.json")))

    def test_conflicting_partition_world_executable_and_duplicate_flags_block(self):
        other = self.root / "other"
        other.write_text("other resource")
        foreign = self.root / "other-vlm"
        shutil.copyfile(self.executable, foreign)
        foreign.chmod(0o700)
        for options in [dict(partition=other), dict(world=other), dict(executable=foreign),
                        dict(extra=("-world", str(self.world)))]:
            with self.subTest(options=options):
                self.start(**options)
                report = self.probe()
                self.assertEqual(report[":code"], ":process-conflict", report)
                self.stop_processes()

    def test_multiple_matches_and_hardlinked_partition_block(self):
        self.start()
        self.start()
        report = self.probe()
        self.assertEqual(report[":code"], ":multiple-vlms", report)
        self.stop_processes()
        alias = self.root / "partition-alias"
        alias.hardlink_to(self.partition)
        other_world = self.root / "other-world"
        other_world.write_text("other world")
        self.start(world=other_world, partition=alias)
        self.assertEqual(self.probe()[":code"], ":process-conflict")

    def test_unreadable_and_foreign_owner_snapshots_fail_closed(self):
        process = self.start()
        body = f'''(let ((identity (genera-process-identity {process.pid} target)))
                      (setf (getf identity :uid) (1+ (getf target :uid)))
                      (list :foreign (genera-process-matches-p identity target)
                            :unreadable (progn (setf (getf identity :uid) (getf target :uid)
                                                    (getf identity :unreadable) t)
                                               (genera-process-matches-p identity target))
                            :action (getf (genera-launch-verdict
                              (list :conflicts (list identity)) :absent :missing) :action)))'''
        report = self.probe(body)
        self.assertIsNone(report[":foreign"])
        self.assertIsNone(report[":unreadable"])
        self.assertEqual(report[":action"], ":blocked")

    def test_endpoint_is_rechecked_inside_lock_and_never_claims_a_handshake(self):
        body = '''(genera-call-with-launch-decision runtime
                    (lambda (decision context) (declare (ignore context)) decision)
                    :preflight preflight :endpoint-observer (lambda (runtime)
                      (declare (ignore runtime))
                      (unless *genera-launch-context* (error "probe without locks")) :present))'''
        # The dynamic context must already exist during the endpoint observation.
        report = self.probe(body)
        self.assertEqual(report[":code"], ":endpoint-present", report)
        self.assertEqual(report[":action"], ":observe")
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen()
            port = listener.getsockname()[1]
            body = f'''(multiple-value-bind (state detail) (genera-probe-launch-endpoint runtime {port})
                         (list :action state :detail detail))'''
            report = self.probe(body)
            self.assertEqual(report[":action"], ":present", report)
        self.assertEqual(self.probe(body)[":action"], ":absent")

    def test_state_symlinks_permissions_and_malformed_metadata_fail_closed(self):
        self.state.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.probe()[":action"], ":error")
        self.state.unlink()
        self.state.mkdir(mode=0o755)
        self.assertEqual(self.probe()[":action"], ":error")
        self.state.chmod(0o700)
        self.start()
        self.assertEqual(self.probe()[":action"], ":observe")
        path = self.metadata_path()
        path.write_text('#.(error "must never run a reader")')
        self.assertEqual(self.probe()[":action"], ":error")
        path.unlink()
        path.symlink_to(self.world)
        self.assertEqual(self.probe()[":action"], ":error")
        self.assertEqual(self.world.read_text(), "fixture world")
        path.unlink()
        lock = self.state / "launch.lock"
        lock.unlink()
        lock.hardlink_to(self.world)
        self.assertEqual(self.probe()[":action"], ":error")
        self.assertEqual(self.world.read_text(), "fixture world")

    def test_lock_released_after_callback_error_and_process_exit(self):
        body = '''(genera-call-with-launch-decision runtime
                    (lambda (decision context) (declare (ignore decision context))
                      (error "fixture callback failure"))
                    :preflight preflight :endpoint-observer (constantly :absent))'''
        self.assertEqual(self.probe(body)[":action"], ":error")
        self.assertEqual(self.probe()[":action"], ":launch")
        entered = self.root / "entered"
        body = f'''(genera-call-with-launch-decision runtime
                     (lambda (decision context) (declare (ignore context))
                       (with-open-file (out {lisp_string(str(entered))} :direction :output)
                         (write-line "locked" out))
                       (sleep 60) decision)
                     :preflight preflight :endpoint-observer (constantly :absent))'''
        child = subprocess.Popen(self.command(body), cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(entered.exists())
            self.assertEqual(self.probe()[":code"], ":lock-busy")
            other_world = self.root / "other-world"
            other_world.write_text("another world sharing the partition")
            self.assertEqual(self.probe(world=other_world)[":code"], ":lock-busy")
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertEqual(self.probe()[":action"], ":launch")

    def test_concurrent_decisions_select_one_owner_and_then_observe(self):
        gate = self.root / "go"
        markers = [self.root / f"ready-{i}" for i in range(3)]
        children = []
        for marker in markers:
            body = f'''(progn
              (with-open-file (out {lisp_string(str(marker))} :direction :output) (write-line "ready" out))
              (loop until (probe-file {lisp_string(str(gate))}) do (sleep .01))
              (genera-call-with-launch-decision runtime
                (lambda (decision context)
                  (when (eq (getf decision :action) :launch)
                    (let* ((child (sb-ext:run-program {lisp_string(str(self.executable))}
                                     (list "-c" "import time; time.sleep(90)" "-world"
                                           {lisp_string(str(self.world))} "-partition"
                                           {lisp_string(str(self.partition))})
                                     :wait nil :input nil :output nil :error nil))
                           (pid (sb-ext:process-pid child)))
                      (sleep .15)
                      (genera-record-launch-identity context (genera-process-identity pid target))
                      (setf (getf decision :fixture-pid) pid)
                      (sleep .6))) decision)
                :preflight preflight :endpoint-observer (constantly :absent)))'''
            children.append(subprocess.Popen(self.command(body), cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        spawned = None
        try:
            deadline = time.monotonic() + 20
            while not all(p.exists() for p in markers) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(all(p.exists() for p in markers))
            gate.touch()
            reports = []
            for child in children:
                stdout, stderr = child.communicate(timeout=25)
                self.assertEqual(child.returncode, 0, stderr)
                reports.append(properties(stdout))
            owners = [r for r in reports if r[":action"] == ":launch"]
            self.assertEqual(len(owners), 1, reports)
            spawned = owners[0][":fixture-pid"]
            self.assertTrue(all(r[":code"] in (":vlm-absent", ":lock-busy", ":vlm-present") for r in reports))
            self.assertEqual(self.probe()[":code"], ":vlm-present")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            if spawned:
                os.kill(spawned, 15)


if __name__ == "__main__":
    unittest.main()
