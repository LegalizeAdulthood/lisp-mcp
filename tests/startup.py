"""Bounded MCP start-or-attach with real locks and disposable VLM processes."""

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

if sys.platform.startswith("linux"):
    import pwd

import launch
from configuration import ROOT, request, run_bridge, sbcl_command
from evaluation import receive_request
from session import greeting, reply, response
from stdio import Bridge, bridge, genera_settings, structured


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc")
                     and getattr(os, "geteuid", lambda: 0)() != 0, "requires non-root Linux and cc")
class StartupChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="vlm startup build ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "vlm-fixture"
        result = subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-O2", "-pthread",
                                 str(ROOT / "tests/startup.c"), "-o", str(cls.binary)],
                                capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)

    def setUp(self):
        self.f = launch.LaunchChecks()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        shutil.copyfile(self.binary, self.f.executable)
        self.report = self.f.root / "child.json"
        self.debugger = self.f.root / "debugger"
        shutil.copyfile("/bin/true", self.debugger)
        self.debugger.chmod(0o700)
        self.settings = dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST="127.0.0.2",
            GENERA_RUNTIME_MODE="local", GENERA_AUTOSTART="true", GENERA_VLM_ROOT=str(self.f.root),
            GENERA_VLM_EXECUTABLE=str(self.f.executable), GENERA_VLM_WORLD="world.vlod",
            GENERA_VLM_PARTITION="fep0", GENERA_VLM_DEBUGGER=str(self.debugger),
            GENERA_VLM_USER=pwd.getpwuid(os.geteuid()).pw_name, GENERA_STATE_DIR=str(self.f.state),
            GENERA_VLM_NETWORK="fixturetap:127.0.0.2;mask=255.0.0.0;gateway=127.0.0.1;preconfiguredTAP=yes",
            GENERA_BOOT_TIMEOUT="6", GENERA_CONNECT_TIMEOUT="1",
            VLM_FIXTURE_REPORT=str(self.report), VLM_FIXTURE_DELAY="650")
        self.clients = []
        self.manual = []
        self.addCleanup(self.close_clients)

    def close_clients(self):
        for mcp in self.clients:
            if mcp.process.poll() is None:
                mcp.close()
        if self.report.exists():
            pid = json.loads(self.report.read_text())["pid"]
            if Path(f"/proc/{pid}/exe").resolve() == self.f.executable:
                os.kill(pid, signal.SIGTERM)
        for process in self.manual:
            process.wait(timeout=5)
        # Release the fixed fixture port before the following test.
        time.sleep(.04)

    def client(self, **settings):
        mcp = Bridge(dict(self.settings, **settings), command=sbcl_command() +
                     ["--load", str(ROOT / "tests/startup-driver.lisp"), "--eval", "(lisp-mcp:main)"])
        self.clients.append(mcp)
        return mcp

    def status(self, mcp):
        mcp.call("genera_session_status", "status")
        return structured(mcp.receive())

    def wait_failed(self, mcp):
        deadline = time.monotonic() + 5
        while True:
            status = self.status(mcp)
            if status.get("state") == "failed":
                return status
            self.assertLess(time.monotonic(), deadline, status)
            time.sleep(.03)

    def test_cold_start_joins_one_cycle_and_keeps_status_responsive(self):
        mcp = self.client()
        mcp.send(request("initialize", "init", protocolVersion="2025-06-18"))
        self.assertEqual(mcp.receive(timeout=15)["id"], "init")
        mcp.send(request("tools/list", "tools"))
        self.assertIn("genera_ensure_session", {t["name"] for t in mcp.receive()["result"]["tools"]})
        self.assertEqual(self.status(mcp)["phase"], "not-started")
        self.assertFalse(self.report.exists())
        self.assertFalse(self.f.state.exists())
        mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        mcp.call("genera_ensure_session", "ensure-a")
        mcp.call("genera_ensure_session", "ensure-b")
        start = time.monotonic()
        mcp.call("genera_session_status", "cached")
        mcp.send(request("ping", "ping"))
        packets = [mcp.receive(), mcp.receive()]
        self.assertEqual({p["id"] for p in packets}, {"cached", "ping"})
        self.assertLess(time.monotonic() - start, 1)
        ensured = [structured(mcp.receive(timeout=8)), structured(mcp.receive(timeout=8))]
        self.assertTrue(all("joined_cycle" in r for r in ensured), ensured)
        self.assertEqual([r["joined_cycle"] for r in ensured], [1, 1])
        self.assertTrue(all(r["connected"] for r in ensured))
        self.assertTrue(ensured[0]["startup"]["launch_attempted"])
        pid = json.loads(self.report.read_text())["pid"]
        self.assertEqual(ensured[0]["startup"]["process"]["pid"], pid)
        mcp.call("genera_eval", "eval", form="42")
        self.assertEqual(structured(mcp.receive())["values"], ["42"])
        mcp.close()
        again = self.client()
        again.initialize()
        status = again.ready()
        self.assertFalse(status["startup"]["launch_attempted"])
        self.assertEqual(json.loads(self.report.read_text())["pid"], pid)
        self.assertEqual((self.f.state / "vlm.log").read_text().count("fixture stdout before"), 1)

    def test_concurrent_mcp_clients_converge_on_one_vlm(self):
        clients = [self.client(VLM_FIXTURE_DELAY="1100") for _ in range(2)]
        for mcp in clients:
            mcp.initialize()
            mcp.call("genera_ensure_session", "ensure")
        statuses = [structured(mcp.receive(timeout=8)) for mcp in clients]
        self.assertTrue(all("connected" in s for s in statuses), statuses)
        self.assertTrue(all(s["connected"] for s in statuses), statuses)
        self.assertEqual(len({s["boot_id"] for s in statuses}), 1)
        self.assertEqual(sum(s["startup"]["launch_attempted"] for s in statuses), 1)
        self.assertEqual((self.f.state / "vlm.log").read_text().count("fixture stdout before"), 1)

    def test_world_alias_uses_canonical_handshake_identity(self):
        alias = self.f.root / "world-alias"
        alias.symlink_to(self.f.world)
        mcp = self.client(GENERA_VLM_WORLD="world-alias")
        mcp.initialize()
        mcp.call("genera_ensure_session", "ensure")
        ensured = structured(mcp.receive(timeout=8))
        self.assertTrue(ensured["connected"], ensured)
        self.assertEqual(ensured["world"], "world.vlod")

    def test_existing_vlm_without_listener_times_out_and_is_preserved(self):
        process = subprocess.Popen([str(self.f.executable), "-world", str(self.f.world),
                                     "-partition", str(self.f.partition)], cwd=self.f.root,
            env=dict(os.environ, **self.settings, VLM_FIXTURE_NO_LISTENER="1"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.manual.append(process)
        mcp = self.client(GENERA_BOOT_TIMEOUT="1")
        mcp.initialize()
        failed = self.wait_failed(mcp)
        self.assertEqual(failed["last_error"]["code"], "boot-timeout", failed)
        self.assertFalse(failed["startup"]["launch_attempted"])
        self.assertIsNone(process.poll())
        time.sleep(.2)
        self.assertEqual(self.status(mcp)["startup"]["cycle"], 1)
        mcp.call("genera_ensure_session", "retry")
        result = structured(mcp.receive())
        self.assertEqual(result["error"]["code"], "boot-timeout", result)
        self.assertEqual(result["session"]["startup"]["cycle"], 2)
        self.assertIsNone(process.poll())
        self.assertFalse((self.f.state / "vlm.log").exists())

    def test_failed_boot_has_one_launch_until_explicit_retry(self):
        mcp = self.client(VLM_FIXTURE_DELAY="20", VLM_FIXTURE_EXIT="17")
        mcp.initialize()
        failed = self.wait_failed(mcp)
        self.assertEqual(failed["last_error"]["code"], "early-exit", failed)
        log = self.f.state / "vlm.log"
        time.sleep(.3)
        self.assertEqual(log.read_text().count("fixture stdout before"), 1)
        mcp.call("genera_ensure_session", "retry")
        result = structured(mcp.receive())
        self.assertEqual(result["error"]["code"], "early-exit", result)
        self.assertEqual(log.read_text().count("fixture stdout before"), 2)

    def test_eof_during_boot_closes_host_but_preserves_launched_vlm(self):
        mcp = self.client(VLM_FIXTURE_DELAY="10000")
        mcp.initialize()
        deadline = time.monotonic() + 4
        while not self.report.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.report.exists())
        start = time.monotonic()
        mcp.close()
        self.assertLess(time.monotonic() - start, 2.5)
        pid = json.loads(self.report.read_text())["pid"]
        self.assertEqual(Path(f"/proc/{pid}/exe").resolve(), self.f.executable)

    def test_wrong_hello_authentication_and_world_never_launch(self):
        for mode in ("id", "authentication", "world"):
            with self.subTest(mode=mode), socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.2", 8888))
                listener.listen()
                errors = []

                def serve():
                    try:
                        with listener.accept()[0] as stream:
                            stream.settimeout(5)
                            response = greeting(receive_request(stream))
                            response[":world"] = "world.vlod"
                            if mode == "id":
                                response[":id"] = "wrong"
                            elif mode == "world":
                                response[":world"] = "other.vlod"
                            else:
                                response.update({":ok": None, ":error": {
                                    ":code": ":authentication-failed", ":message": "fixture rejected token"}})
                            reply(stream, response)
                            self.assertEqual(stream.recv(1), b"")
                    except BaseException as error:
                        errors.append(error)

                thread = threading.Thread(target=serve)
                thread.start()
                mcp = self.client()
                mcp.initialize()
                failed = self.wait_failed(mcp)
                self.assertIn(failed["last_error"]["code"], ("protocol", "handshake", "authentication-failed"))
                self.assertFalse(failed["startup"]["launch_attempted"])
                self.assertFalse(self.report.exists())
                mcp.close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors, errors)

    def test_attach_only_ensure_is_bounded_and_never_inspects_runtime(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.2", 0))
            settings = dict(genera_settings(unused.getsockname()), GENERA_BOOT_TIMEOUT="1", GENERA_CONNECT_TIMEOUT="1")
        with bridge(settings) as mcp:
            mcp.initialize()
            mcp.call("genera_ensure_session", "ensure")
            failed = structured(mcp.receive())
            self.assertEqual(failed["error"]["code"], "boot-timeout", failed)
            self.assertFalse(failed["session"]["startup"]["launch_attempted"])
        self.assertFalse(self.f.state.exists())
        self.assertFalse(self.report.exists())

    def test_stalled_hello_stops_without_launch(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.2", 8888))
            listener.listen()
            finished = threading.Event()

            def serve():
                with listener.accept()[0] as stream:
                    stream.settimeout(3)
                    receive_request(stream)
                    finished.wait(3)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                mcp = self.client()
                mcp.initialize()
                failed = self.wait_failed(mcp)
                self.assertEqual(failed["last_error"]["code"], "handshake", failed)
                self.assertFalse(failed["startup"]["launch_attempted"])
                self.assertFalse(self.report.exists())
                mcp.close()
            finally:
                finished.set()
                thread.join(timeout=5)

    def test_preflight_failure_reports_specific_check_without_launch(self):
        mcp = self.client(GENERA_VLM_WORLD="missing-world.vlod")
        mcp.initialize()
        failed = self.wait_failed(mcp)
        self.assertEqual(failed["last_error"]["code"], "missing-world", failed)
        self.assertFalse(failed["startup"]["launch_attempted"])
        self.assertFalse(self.report.exists())

    def test_ensure_preserves_an_unresolved_execution(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen()
            listener.settimeout(10)
            address = listener.getsockname()
            requests, errors = [], []

            def serve():
                try:
                    with listener.accept()[0] as stream:
                        stream.settimeout(5)
                        reply(stream, greeting(receive_request(stream)))
                        requests.append(receive_request(stream))
                    with listener.accept()[0] as stream:
                        stream.settimeout(5)
                        reply(stream, greeting(receive_request(stream)))
                        while True:
                            try:
                                item = receive_request(stream)
                            except EOFError:
                                break
                            requests.append(item)
                            result = response(item)
                            if item[":op"] == ":request-status":
                                result[":request-state"] = ":unknown"
                            reply(stream, result)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            with bridge(dict(genera_settings(address), GENERA_BOOT_TIMEOUT="2")) as mcp:
                mcp.initialize()
                mcp.ready()
                mcp.call("genera_eval", "lost", form="side effect")
                lost = structured(mcp.receive())
                mcp.call("genera_ensure_session", "ensure")
                ensured = structured(mcp.receive())
                self.assertEqual(ensured["state"], "outcome-unknown", ensured)
                self.assertEqual(ensured["unresolved_request"]["request_id"], lost["request_id"])
                self.assertTrue(ensured["connected"])
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertEqual(sum(item[":op"] == ":eval" for item in requests), 1)

    def test_boot_timeout_and_connect_timeout_validate_before_dispatch(self):
        for name in ("GENERA_BOOT_TIMEOUT", "GENERA_CONNECT_TIMEOUT"):
            for value in ("0", "-1", " 1", "no"):
                result = run_bridge(dict(genera_settings(("127.0.0.2", 1)), **{name: value}), [request("tools/list")])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(name, result.stderr)


if __name__ == "__main__":
    unittest.main()
