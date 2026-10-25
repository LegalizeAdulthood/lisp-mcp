"""Tracked test forms and configured adapters; --live-v1 uses running Genera."""
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
from stdio import bridge, genera_settings, peer, structured


def advertise(hello):
    hello[":features"].append(":run-tests")
    hello[":test-runners"] = ["form", "suite"]


def tested(work, status=":passed", counts=True):
    packet = response(work, "report")
    packet.update({":runner": work[":runner"], ":test-status": status})
    if counts:
        packet.update({":passed-count": 3, ":failed-count": 1 if status == ":failed" else 0})
    if status == ":failed":
        packet.update({":ok": None, ":request-state": ":failed", ":stdout": "failed output",
                       ":error": {":code": ":test-failure", ":condition-type": "TEST-FAILURE", ":message": "tests failed"}})
    return packet


class RunnerChecks(unittest.TestCase):
    def test_portable_runner_reports_admission_and_retention(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/runners.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Runner unit checks: 25 passed.", result.stdout)

    def test_schema_and_unsupported_capability_send_no_tests(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            for args in ({}, {"runner": "bad name"}, {"runner": "suite", "form": "42"},
                         {"form": None}, {"form": "42", "timeout_ms": 0}, {"form": "42", "extra": True}):
                mcp.call("genera_run_tests", "bad", **args)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            result = call(mcp, "genera_run_tests", form="42")
            self.assertEqual(result["error"]["code"], "unsupported")

    def test_unknown_runner_is_rejected_before_submission(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            result = call(mcp, "genera_run_tests", runner="absent")
            self.assertEqual(result["error"]["code"], "unsupported-runner")
            self.assertNotIn("request_id", result)
            status = call(mcp, "genera_session_status")
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["test_runners"], ["form", "suite"])

    def test_explicit_and_named_results_have_only_reported_counts(self):
        def serve(stream, hello):
            for status in (":passed", ":failed", ":unknown"):
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":run-tests")
                self.assertEqual(work[":runner"], "suite" if status == ":failed" else "form")
                self.assertEqual(":form" in work, status != ":failed")
                reply(stream, tested(work, status, counts=status != ":unknown"))
            reply(stream, response(receive_request(stream), "42"))
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            passed = call(mcp, "genera_run_tests", form="(tests)")
            self.assertEqual((passed["test_status"], passed["passed_count"], passed["failed_count"]), ("passed", 3, 0))
            failed = call(mcp, "genera_run_tests", runner="suite")
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["test_status"], "failed")
            self.assertEqual(failed["failed_count"], 1)
            self.assertEqual(failed["stdout"], "failed output")
            unknown = call(mcp, "genera_run_tests", form="t")
            self.assertEqual(unknown["test_status"], "unknown")
            self.assertNotIn("passed_count", unknown)
            self.assertNotIn("failed_count", unknown)
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])

    def test_malformed_catalogue_or_result_is_never_trusted(self):
        def invalid(hello):
            advertise(hello)
            del hello[":test-runners"]
        def idle(stream, hello):
            self.assertEqual(stream.recv(1), b"")
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
        for change in (lambda p: p.update({":runner": "other"}), lambda p: p.pop(":test-status"),
                       lambda p: p.pop(":failed-count"), lambda p: p.update({":failed-count": 2})):
            def serve(stream, hello):
                work = receive_request(stream)
                packet = tested(work)
                change(packet)
                reply(stream, packet)
            with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
                mcp.initialize()
                mcp.ready()
                result = call(mcp, "genera_run_tests", form="(tests)")
                self.assertEqual(result["error"]["code"], "protocol")
                self.assertEqual(result["request_state"], "unresolved")

    def test_lost_reply_lookup_keeps_one_tracked_run(self):
        seen = []
        def serve(stream, hello):
            work = receive_request(stream)
            seen.append(work)
            lookup = receive_request(stream)
            self.assertEqual(lookup[":op"], ":request-status")
            terminal = tested(work)
            reply(stream, dict(response(lookup), **{":request-state": ":completed", ":result": terminal}))
            reply(stream, terminal)
            reply(stream, response(receive_request(stream), "42"))
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            result = call(mcp, "genera_run_tests", form="(tests)", timeout_ms=80)
            self.assertEqual(result["request_state"], "unresolved")
            self.assertEqual(call(mcp, "genera_run_tests", form="99")["error"]["code"], "busy")
            found = call(mcp, "genera_request_status", **identity(result))
            self.assertEqual(found["result"]["passed_count"], 3)
            self.assertEqual(call(mcp, "genera_eval", form="42")["values"], ["42"])
        self.assertEqual(len(seen), 1)

    def native_settings(self):
        return dict(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1", GENERA_HOST=os.environ["GENERA_HOST"],
                    GENERA_PORT=os.environ.get("GENERA_PORT", "8888"))

    def evaluate(self, form):
        with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
            send(stream, native_request(hello, ":eval", "runner-control-" + uuid.uuid4().hex, form=form))
            result = receive(stream)
            self.assertTrue(result[":ok"], result)
            return result

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_reports_errors_unknown_values_and_bounded_output(self):
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            passed = call(mcp, "genera_run_tests", form=
                          '(progn (write-string "test stdout") (write-string "test stderr" *error-output*) '
                          '(ansi-common-lisp:warn "runner warning") '
                          '(quote (:test-status :passed :passed-count 4 :failed-count 0)))')
            self.assertTrue(passed["ok"], passed)
            self.assertEqual(passed["test_status"], "passed")
            self.assertEqual(passed["passed_count"], 4)
            self.assertEqual(passed["stdout"], "test stdout")
            self.assertEqual(passed["stderr"], "test stderr")
            self.assertTrue(passed["warnings"])
            failed = call(mcp, "genera_run_tests", form="'(:test-status :failed :passed-count 2 :failed-count 1)")
            self.assertFalse(failed["ok"], failed)
            self.assertEqual(failed["error"]["code"], "test-failure")
            self.assertEqual((failed["passed_count"], failed["failed_count"]), (2, 1))
            self.assertTrue(failed["values"])
            for status in ("passed", "failed"):
                report = call(mcp, "genera_run_tests", form=f"'(:test-status :{status})")
                self.assertEqual(report["test_status"], status)
                self.assertNotIn("passed_count", report)
                self.assertNotIn("failed_count", report)
            for form in ("t", "(values 17 0)", "'(:passed 17 :failed 0)"):
                unknown = call(mcp, "genera_run_tests", form=form)
                self.assertTrue(unknown["ok"], unknown)
                self.assertEqual(unknown["test_status"], "unknown")
                self.assertNotIn("passed_count", unknown)
            error = call(mcp, "genera_run_tests", form='(progn (write-string "before") (error "runner failure"))')
            self.assertEqual(error["error"]["code"], "test-runner-error", error)
            self.assertEqual(error["stdout"], "before")
            self.assertEqual(error["test_status"], "unknown")
            malformed = call(mcp, "genera_run_tests", form="'(:test-status :passed :passed-count 5 :failed-count 2)")
            self.assertEqual(malformed["error"]["code"], "test-report-error", malformed)
            self.assertNotIn("passed_count", malformed)
            mcp.call("genera_run_tests", "bounded", form=
                           '(progn (write-string (make-string 70000 :initial-element #\\a)) '
                           '(quote (:test-status :passed :passed-count 1 :failed-count 0)))')
            bounded = structured(mcp.receive(timeout=60))
            self.assertTrue(bounded["ok"], bounded)
            self.assertTrue(bounded["truncated"])
            self.assertLessEqual(len(bounded["stdout"]), 65536)
            self.assertEqual(bounded["passed_count"], 1)

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_named_adapters_and_rejected_unknown_runner(self):
        tag = uuid.uuid4().hex
        name = "fixture-" + tag
        invalid = "symbol-" + tag
        function = "runner-fixture-" + tag
        saved = "*runner-fixture-saved-" + tag + "*"
        self.evaluate(f'(progn (setq {saved} *mcp-test-runners*) '
                      f'(defun {function} () (write-string "named runner") '
                      '(quote (:test-status :passed :passed-count 2 :failed-count 0))) '
                      f'(setq *mcp-test-runners* (cons (cons {lisp_string(name)} '
                      f"#'{function}) (cons (cons {lisp_string(invalid)} 'list) *mcp-test-runners*))) t)")
        try:
            with bridge(self.native_settings()) as mcp:
                mcp.initialize()
                mcp.ready()
                result = call(mcp, "genera_run_tests", runner=name)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["runner"], name)
                self.assertEqual(result["stdout"], "named runner")
                self.assertEqual(result["passed_count"], 2)
                names = call(mcp, "genera_session_status")["test_runners"]
                self.assertIn(name, names)
                self.assertNotIn(invalid, names)
                found = call(mcp, "genera_request_status", **identity(result))
                self.assertEqual(found["result"]["runner"], name)
                self.assertEqual(call(mcp, "genera_run_tests", runner="not-configured")["error"]["code"], "unsupported-runner")
                with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                    work = native_request(hello, ":run-tests", "unknown-runner-" + tag, runner="not-configured")
                    send(stream, work)
                    reply_packet = receive(stream)
                    self.assertIn(":unsupported-runner", reply_packet[":error"])
                    send(stream, native_request(hello, ":request-status", "lookup-" + tag, target_id=work[":id"]))
                    self.assertEqual(receive(stream)[":request-state"], ":unknown")
        finally:
            self.evaluate(f"(progn (setq *mcp-test-runners* {saved}) (makunbound '{saved}) t)")

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_timeout_disconnect_and_duplicate_id_never_repeat_tests(self):
        count = "*runner-count-" + uuid.uuid4().hex + "*"
        self.evaluate(f"(setq {count} 0)")
        form = f"(progn (incf {count}) (sleep .6) '(:test-status :passed :passed-count 1 :failed-count 0))"
        with bridge(self.native_settings()) as mcp:
            mcp.initialize()
            mcp.ready()
            result = call(mcp, "genera_run_tests", form=form, timeout_ms=80)
            self.assertEqual(result["request_state"], "unresolved", result)
            self.assertEqual(call(mcp, "genera_run_tests", form="99")["error"]["code"], "busy")
            deadline = time.monotonic() + 5
            while True:
                found = call(mcp, "genera_request_status", **identity(result))
                if found.get("result"):
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.04)
            self.assertEqual(found["result"]["passed_count"], 1)
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["1"])
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                work = native_request(hello, ":run-tests", "detached-test-" + uuid.uuid4().hex, runner="form", form=form)
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
            with client(os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888"))) as (stream, hello):
                send(stream, work)
                terminal = receive(stream)
                self.assertTrue(terminal[":ok"], terminal)
                self.assertEqual(terminal[":passed-count"], 1)
            self.assertEqual(call(mcp, "genera_eval", form=count)["values"], ["2"])


if __name__ == "__main__":
    unittest.main()
