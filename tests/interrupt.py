"""Targeted interruption; --live-v1 requires the updated running listener."""
import os
import socket
import subprocess
import sys
import time
import unittest
import uuid

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from configuration import ROOT, SETTINGS, request, sbcl_command
from deadlines import identity
from evaluation import receive_request
from session import reply, response
from stdio import bridge, genera_settings, peer, structured


def advertise(hello):
    hello[":features"].append(":interrupt")


def interrupted(work):
    result = response(work)
    result.update({":ok": None, ":request-state": ":interrupted", ":values": None,
                   ":value-count": 0, ":error": {":code": ":interrupted",
                   ":message": "Interrupted", ":condition-type": "SYS:ABORT"}})
    return result


def call(mcp, name, **arguments):
    mcp.call(name, "check", **arguments)
    packet = mcp.receive(timeout=10)
    assert packet["id"] == "check", packet
    return structured(packet)


def tools(mcp):
    mcp.send(request("tools/list", "list"))
    return {item["name"] for item in mcp.receive()["result"]["tools"]}


def ready(mcp):
    mcp.initialize()
    status = mcp.ready()
    end = time.monotonic() + 3
    while "genera_interrupt" not in tools(mcp):
        assert time.monotonic() < end, status
        time.sleep(.01)
    return status


class InterruptChecks(unittest.TestCase):
    def test_portable_targeting_and_terminal_contract(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/interrupt.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Interrupt unit checks: 24 passed.", result.stdout)

    def test_capability_notification_validation_and_stale_target_send_nothing(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.send(request("initialize", "init", protocolVersion="2025-06-18"))
            self.assertTrue(mcp.receive()["result"]["capabilities"]["tools"]["listChanged"])
            self.assertNotIn("genera_interrupt", tools(mcp))
            mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            mcp.ready()
            self.assertIn("genera_interrupt", tools(mcp))
            target = dict(request_id="work", boot_id="wrong", service_id="service")
            result = call(mcp, "genera_interrupt", **target)
            self.assertEqual(result["error"]["code"], "generation-changed")
            self.assertEqual(result["request_id"], "work")
            for value in (0, 5001, "bad", None):
                mcp.call("genera_interrupt", "invalid", **target, timeout_ms=value)
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
        notices = [p for p in mcp.received if p.get("method") == "notifications/tools/list_changed"]
        self.assertEqual(len(notices), 1)

    def test_same_socket_controls_interrupt_eval_and_release_execution(self):
        signals = []

        def serve(stream, hello):
            work = receive_request(stream)
            signal = receive_request(stream)
            signals.append(signal)
            self.assertEqual(signal[":op"], ":interrupt")
            self.assertEqual(signal[":target-id"], work[":id"])
            pending = response(signal)
            pending.update({":ok": None, ":error": {":code": ":interrupt-pending", ":message": "Pending"}})
            reply(stream, pending)
            query = receive_request(stream)
            self.assertEqual(query[":op"], ":request-status")
            result = interrupted(work)
            reply(stream, dict(response(query), **{":request-state": ":interrupted", ":result": result}))
            reply(stream, result)
            next_work = receive_request(stream)
            self.assertEqual(next_work[":op"], ":eval")
            reply(stream, response(next_work, "7"))

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            status = ready(mcp)
            mcp.call("genera_eval", "work", form="long")
            current = call(mcp, "genera_session_status")
            target = dict(request_id=current["active_request_id"], boot_id=status["boot_id"],
                          service_id=status["service_id"])
            mcp.call("genera_interrupt", "stop", **target)
            packets = [mcp.receive(), mcp.receive()]
            results = {p["id"]: structured(p) for p in packets}
            self.assertEqual(results["work"]["request_state"], "interrupted")
            self.assertEqual(results["stop"]["request_state"], "interrupted")
            self.assertTrue(results["stop"]["ok"])
            self.assertEqual(call(mcp, "genera_eval", form="7")["values"], ["7"])
        self.assertEqual(len(signals), 1)

    def test_pending_timeout_keeps_execution_and_cached_status_live(self):
        signals = []

        def serve(stream, hello):
            work = receive_request(stream)
            stream.settimeout(.05)
            while time.monotonic() - began < .6:
                try:
                    query = receive_request(stream)
                except socket.timeout:
                    continue
                if query[":op"] == ":interrupt":
                    signals.append(query)
                    pending = response(query)
                    pending.update({":ok": None, ":error": {":code": ":interrupt-pending", ":message": "Pending"}})
                    reply(stream, pending)
                else:
                    reply(stream, response(query))
            reply(stream, response(work))

        began = time.monotonic()
        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            status = ready(mcp)
            began = time.monotonic()
            mcp.call("genera_eval", "work", form="long")
            current = call(mcp, "genera_session_status")
            target = dict(request_id=current["active_request_id"], boot_id=status["boot_id"], service_id=status["service_id"])
            mcp.call("genera_interrupt", "stop", **target, timeout_ms=400)
            mcp.call("genera_session_status", "fresh", refresh=True)
            mcp.call("genera_session_status", "cached")
            mcp.send(request("ping", "ping"))
            packets = [mcp.receive(), mcp.receive(), mcp.receive()]
            self.assertEqual({p["id"] for p in packets}, {"fresh", "cached", "ping"})
            self.assertLess(time.monotonic() - began, .3)
            results = {p["id"]: structured(p) for p in [mcp.receive(), mcp.receive()]}
            self.assertIn(results["stop"]["error"]["code"], ["interrupt-pending", "timeout"])
            self.assertEqual(results["work"]["request_state"], "completed")
        self.assertEqual(len(signals), 1)

    def test_peer_cannot_claim_interrupt_success_for_completed_work(self):
        def serve(stream, hello):
            signal = receive_request(stream)
            work = {":op": ":eval", ":id": signal[":target-id"]}
            invalid = dict(response(signal), **{":target-id": signal[":target-id"],
                            ":request-state": ":completed", ":result": response(work)})
            reply(stream, invalid)

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            status = ready(mcp)
            result = call(mcp, "genera_interrupt", request_id="work", boot_id=status["boot_id"],
                          service_id=status["service_id"])
            self.assertEqual(result["error"]["code"], "protocol", result)

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_interrupt_cleanup_worker_survival_and_unrelated_process(self):
        settings = {key: os.environ[key] for key in SETTINGS if key in os.environ}
        settings.update(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1")
        self.assertIn("GENERA_HOST", settings)
        marker = "*mcp-interrupt-" + uuid.uuid4().hex + "*"
        cleanup, other, witness = marker + "-cleanup", marker + "-other", marker + "-worker"
        ticket = marker + "-job"
        with bridge(settings) as first, bridge(settings) as control:
            status = ready(first)
            ready(control)
            form = (f"(progn (setq {marker} 0 {cleanup} nil {other} 0 {witness} *mcp-worker* {ticket} *mcp-worker-busy*) "
                    f'(scl:process-run-function "MCP unrelated probe" (lambda () (sleep .5) (incf {other}))) '
                    f'(unwind-protect (progn (incf {marker}) (write-string "before") (sleep 8) (incf {marker})) '
                    f'(setq {cleanup} t)))')
            first.call("genera_eval", "long", form=form)
            current = call(first, "genera_session_status")
            target = dict(request_id=current["active_request_id"], boot_id=status["boot_id"], service_id=status["service_id"])
            time.sleep(.2)
            for changes, code in [({"request_id": "absent"}, "request-not-active"), ({"boot_id": "old"}, "generation-changed")]:
                self.assertEqual(call(control, "genera_interrupt", **dict(target, **changes))["error"]["code"], code)
            self.assertTrue(call(control, "genera_session_status", refresh=True)["busy"])
            stopped = call(control, "genera_interrupt", **target)
            self.assertEqual(stopped["request_state"], "interrupted", stopped)
            original = structured(first.receive())
            self.assertEqual(original["request_state"], "interrupted", original)
            self.assertEqual(original["stdout"], "before")
            self.assertEqual(stopped["result"], {key: value for key, value in original.items()
                                                if key != "timeout_enforced_by"})
            result = call(control, "genera_eval", form=f"(progn (sleep .5) (list {marker} {cleanup} {other} (eq {witness} *mcp-worker*)))")
            self.assertEqual(result["values"], ["(1 T 1 T)"], result)
            # Execute a delayed old callback in the successor job's environment.
            result = call(control, "genera_eval", form=f"(progn (mcp-deliver-interrupt {ticket}) (+ 20 22))")
            self.assertEqual(result["values"], ["42"], result)
            self.assertEqual(call(control, "genera_interrupt", **target)["request_state"], "interrupted")
            self.assertEqual(call(control, "genera_interrupt", **dict(target, request_id="absent"))["error"]["code"], "request-not-active")

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_protected_region_refuses_without_deferred_abort(self):
        self.native_refusal('(sys:without-aborts ("MCP protected fixture") (sleep 1) 42)', False)

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_cleanup_does_not_claim_early_success(self):
        self.native_refusal('(unwind-protect (sleep 8) (sleep 1))', True)

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_explicit_debugger_entry_recovers_by_targeted_interrupt(self):
        settings = {key: os.environ[key] for key in SETTINGS if key in os.environ}
        settings.update(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1")
        with bridge(settings) as mcp, bridge(settings) as control:
            status = ready(mcp)
            ready(control)
            mcp.call("genera_eval", "debugger", form='(break "MCP debugger fixture")')
            current = call(mcp, "genera_session_status")
            target = dict(request_id=current["active_request_id"], boot_id=status["boot_id"], service_id=status["service_id"])
            time.sleep(.2)
            self.assertTrue(call(control, "genera_session_status", refresh=True)["busy"])
            stopped = call(control, "genera_interrupt", **target)
            self.assertEqual(stopped["request_state"], "interrupted", stopped)
            self.assertEqual(structured(mcp.receive())["request_state"], "interrupted")
            self.assertEqual(call(control, "genera_eval", form="42")["values"], ["42"])

    def native_refusal(self, form, cleanup):
        settings = {key: os.environ[key] for key in SETTINGS if key in os.environ}
        settings.update(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1")
        with bridge(settings) as first, bridge(settings) as control:
            status = ready(first)
            ready(control)
            first.call("genera_eval", "long", form=form)
            current = call(first, "genera_session_status")
            target = dict(request_id=current["active_request_id"], boot_id=status["boot_id"], service_id=status["service_id"])
            time.sleep(.2)
            if cleanup:
                start = time.monotonic()
                control.call("genera_interrupt", "stop", **target, timeout_ms=800)
                control.call("genera_session_status", "fresh", refresh=True)
                fresh = control.receive()
                self.assertEqual(fresh["id"], "fresh", fresh)
                self.assertLess(time.monotonic() - start, .5)
                stopped = structured(control.receive())
            else:
                stopped = call(control, "genera_interrupt", **target, timeout_ms=2000)
            self.assertIn(stopped["error"]["code"], ["interrupt-pending", "timeout"] if cleanup else ["interrupt-refused"])
            original = structured(first.receive())
            self.assertEqual(original["request_state"], "interrupted" if cleanup else "completed")
            retained = call(control, "genera_request_status", **target)
            self.assertEqual(retained["request_state"], original["request_state"], retained)
            self.assertEqual(call(control, "genera_eval", form="(+ 20 22)")["values"], ["42"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
