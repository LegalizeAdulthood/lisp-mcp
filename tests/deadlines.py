"""Host deadline, lost reply, ledger reconciliation, and recovery checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import queue
import socket
import struct
import sys
import threading
import time
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from concurrency import client, receive, request as native_request, send
from configuration import request
from evaluation import receive_request
from protocol import legacy, run_lisp
from session import greeting, lisp_program, reply, response
from stdio import bridge, genera_settings, peer, structured


def identity(result):
    return {key: result[key] for key in ("request_id", "boot_id", "service_id")}


def lookup_reply(stream, state, result=None):
    query = receive_request(stream)
    if query[":op"] != ":request-status":
        raise AssertionError(query)
    packet = response(query)
    packet[":request-state"] = state
    if result is not None:
        packet[":result"] = result
    reply(stream, packet)
    return query


def recover(mcp, request_id, target, **changes):
    arguments = dict(target, expected_boot_id="boot", expected_service_id="service",
                     acknowledge_unknown=True)
    arguments.update(changes)
    mcp.call("genera_recover_session", request_id, **arguments)


@contextmanager
def connections(behavior):
    """Script explicit replacement connections; fail on unexpected replay."""
    with socket.socket() as listener, ThreadPoolExecutor(1) as pool:
        listener.bind(("127.0.0.2", 0))
        listener.listen(3)
        listener.settimeout(15)
        finished = pool.submit(behavior, listener)
        yield listener.getsockname()
        finished.result(timeout=10)
        listener.settimeout(.1)
        with unittest.TestCase().assertRaises(socket.timeout):
            extra, _ = listener.accept()
            extra.close()


def accept(listener):
    stream = listener.accept()[0]
    stream.settimeout(5)
    hello = receive_request(stream)
    if hello[":op"] != ":hello":
        raise AssertionError(hello)
    reply(stream, greeting(hello))
    return stream


class DeadlineChecks(unittest.TestCase):
    def timeout(self, mcp, **arguments):
        mcp.call("genera_eval", "once", form="side-effect", timeout_ms=80, **arguments)
        packet = mcp.receive()
        self.assertEqual(packet["id"], "once")
        self.assertTrue(packet["result"]["isError"])
        data = structured(packet)
        self.assertEqual(data["error"]["code"], "timeout")
        self.assertEqual(data["request_state"], "unresolved")
        self.assertEqual(data["timeout_enforced_by"], "host")
        return identity(data)

    def test_late_reply_completes_once_and_controls_remain_responsive(self):
        release = threading.Event()
        evaluations = []

        def serve(stream, _):
            work = receive_request(stream)
            evaluations.append(work)
            self.assertNotIn(":timeout-ms", work)
            lookup_reply(stream, ":running")
            self.assertTrue(release.wait(4))
            reply(stream, response(work))
            next_work = receive_request(stream)
            evaluations.append(next_work)
            reply(stream, response(next_work, "43"))

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            target = self.timeout(mcp)
            mcp.call("genera_eval", "blocked", form="99")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            mcp.call("genera_request_status", "running", **target)
            self.assertEqual(structured(mcp.receive())["request_state"], "running")
            release.set()
            mcp.ready()
            with self.assertRaises(queue.Empty):
                mcp.receive(timeout=.1)
            mcp.call("genera_eval", "next", form="43")
            self.assertEqual(structured(mcp.receive())["values"], ["43"])
            self.assertEqual(sum(p["id"] == "once" for p in mcp.received), 1)
        self.assertEqual([r[":form"] for r in evaluations], ["side-effect", "43"])

    def test_ledger_result_releases_block_before_original_reply(self):
        def serve(stream, _):
            work = receive_request(stream)
            lookup_reply(stream, ":completed", response(work))
            next_work = receive_request(stream)
            reply(stream, response(work))
            reply(stream, response(next_work, "7"))

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            target = self.timeout(mcp)
            mcp.call("genera_request_status", "result", **target)
            found = structured(mcp.receive())
            self.assertEqual(found["result"]["request_id"], target["request_id"])
            self.assertEqual(found["result"]["values"], ["42"])
            mcp.call("genera_eval", "next", form="7")
            self.assertEqual(structured(mcp.receive())["values"], ["7"])
            with self.assertRaises(queue.Empty):
                mcp.receive(timeout=.1)

    def test_unknown_recovery_requires_acknowledgement_identity_and_fresh_idle(self):
        release = threading.Event()

        def serve(stream, _):
            work = receive_request(stream)
            lookup_reply(stream, ":unknown")
            lookup_reply(stream, ":unknown")
            status = receive_request(stream)
            busy = response(status)
            busy[":busy"] = True
            reply(stream, busy)
            lookup_reply(stream, ":unknown")
            status = receive_request(stream)
            reply(stream, response(status))
            next_work = receive_request(stream)
            # An old late result must not release a newer execution slot.
            reply(stream, response(work))
            self.assertTrue(release.wait(4))
            reply(stream, response(next_work, "8"))

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            target = self.timeout(mcp)
            mcp.call("genera_request_status", "unknown", **target)
            self.assertEqual(structured(mcp.receive())["request_state"], "unknown")
            recover(mcp, "no-ack", target, acknowledge_unknown=False)
            self.assertEqual(mcp.receive()["error"]["code"], -32602)
            recover(mcp, "wrong-generation", target, expected_service_id="other")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "generation-changed")
            recover(mcp, "busy", target)
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            recover(mcp, "recovered", target)
            recovered = structured(mcp.receive())
            self.assertTrue(recovered["acknowledged"])
            self.assertEqual(recovered["request_state"], "unknown")
            mcp.call("genera_session_status", "history")
            history = structured(mcp.receive())["history"]
            self.assertEqual(history[0]["request_state"], "unknown")
            self.assertTrue(history[0]["acknowledged"])
            mcp.call("genera_eval", "new", form="8")
            mcp.call("genera_session_status", "new-active")
            active = structured(mcp.receive())["active_request_id"]
            time.sleep(.05)
            mcp.call("genera_session_status", "still-active")
            self.assertEqual(structured(mcp.receive())["active_request_id"], active)
            mcp.call("genera_eval", "blocked", form="9")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            release.set()
            self.assertEqual(structured(mcp.receive())["values"], ["8"])

    def test_lost_reply_uses_explicit_control_retry_without_execution_replay(self):
        evaluations = []

        def serve(listener):
            with accept(listener) as stream:
                work = receive_request(stream)
                evaluations.append(work)
            with accept(listener) as stream:
                lookup_reply(stream, ":running")
                lookup_reply(stream, ":completed", response(work))
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "once", form="side-effect")
            failure = structured(mcp.receive())
            self.assertEqual(failure["error"]["code"], "transport")
            self.assertEqual(failure["request_state"], "unresolved")
            target = identity(failure)
            for call_id, state in [("running", "running"), ("done", "completed")]:
                mcp.call("genera_request_status", call_id, **target)
                self.assertEqual(structured(mcp.receive())["request_state"], state)
            mcp.ready()
        self.assertEqual(len(evaluations), 1)

    def test_running_lookup_and_recovery_do_not_wait_for_eval_completion(self):
        release = threading.Event()

        def serve(stream, _):
            work = receive_request(stream)
            lookup_reply(stream, ":running")
            lookup_reply(stream, ":running")
            self.assertTrue(release.wait(4))
            reply(stream, response(work))

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            current = mcp.ready()
            mcp.call("genera_eval", "pending", form="long-running")
            mcp.call("genera_session_status", "active")
            active = structured(mcp.receive())
            target = dict(request_id=active["active_request_id"], boot_id=current["boot_id"],
                          service_id=current["service_id"])
            mcp.call("genera_request_status", "running", **target)
            self.assertEqual(structured(mcp.receive())["request_state"], "running")
            recover(mcp, "cannot-release", target)
            self.assertEqual(structured(mcp.receive())["error"]["code"], "not-unknown")
            release.set()
            self.assertEqual(mcp.receive()["id"], "pending")

    def test_remote_eviction_cannot_erase_a_retained_terminal_result(self):
        def serve(stream, _):
            work = receive_request(stream)
            lookup_reply(stream, ":completed", response(work))
            lookup_reply(stream, ":unknown")

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            target = self.timeout(mcp)
            mcp.call("genera_request_status", "ledger", **target)
            self.assertEqual(structured(mcp.receive())["source"], "ledger")
            mcp.call("genera_request_status", "evicted", **target)
            retained = structured(mcp.receive())
            self.assertEqual(retained["source"], "host")
            self.assertEqual(retained["request_state"], "completed")
            self.assertEqual(retained["result"]["values"], ["42"])

    def test_alias_uses_configured_default_host_deadline(self):
        with peer(lambda stream, _: receive_request(stream)) as address:
            settings = dict(genera_settings(address), GENERA_TIMEOUT="1")
            with bridge(settings) as mcp:
                mcp.initialize()
                mcp.ready()
                started = time.monotonic()
                mcp.call("eval_genera", "alias", expression="side-effect")
                failure = structured(mcp.receive())
                self.assertEqual(failure["error"]["code"], "timeout")
                self.assertEqual(failure["request_state"], "unresolved")
                self.assertLess(time.monotonic() - started, 2)

    def test_generation_change_preserves_block_until_explicit_idle_recovery(self):
        def serve(listener):
            with accept(listener) as stream:
                receive_request(stream)
            with listener.accept()[0] as stream:
                stream.settimeout(5)
                hello = receive_request(stream)
                changed = greeting(hello)
                changed[":service-id"] = "replacement"
                reply(stream, changed)
                status = receive_request(stream)
                self.assertEqual(status[":op"], ":status")
                idle = response(status)
                idle[":service-id"] = "replacement"
                reply(stream, idle)
                work = receive_request(stream)
                self.assertEqual(work[":form"], "new-generation")
                done = response(work)
                done[":service-id"] = "replacement"
                reply(stream, done)
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "lost", form="old-side-effect")
            target = identity(structured(mcp.receive()))
            mcp.call("genera_request_status", "old-generation", **target)
            self.assertEqual(structured(mcp.receive())["error"]["code"], "generation-changed")
            mcp.call("genera_eval", "blocked", form="cannot-yet-run")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
            recover(mcp, "recover", target, expected_service_id="replacement")
            recovered = structured(mcp.receive())
            self.assertTrue(recovered["history_lost"])
            self.assertEqual(recovered["request_state"], "unknown")
            mcp.call("genera_eval", "new", form="new-generation")
            self.assertEqual(structured(mcp.receive())["values"], ["42"])

    def test_evicted_history_remains_unknown_and_blocks_execution(self):
        def serve(stream, _):
            for _ in range(35):
                work = receive_request(stream)
                reply(stream, response(work))
            lookup_reply(stream, ":unknown")

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            oldest = None
            for number in range(35):
                mcp.call("genera_eval", number, form="42")
                result = structured(mcp.receive())
                if oldest is None:
                    oldest = identity(result)
            mcp.call("genera_session_status", "history")
            history = structured(mcp.receive())
            self.assertEqual(len(history["history"]), 32)
            self.assertEqual(history["history_evicted"], 3)
            mcp.call("genera_request_status", "evicted", **oldest)
            self.assertEqual(structured(mcp.receive())["request_state"], "unknown")
            mcp.call("genera_eval", "blocked", form="do-not-replay")
            self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")

    def test_partial_write_is_uncertain(self):
        def serve(stream, _):
            self.assertTrue(stream.recv(64))
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            stream.close()

        # This peer deliberately resets mid-frame rather than reading an eval.
        def reset_peer(listener):
            with accept(listener) as stream:
                serve(stream, None)

        with connections(reset_peer) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "partial", form="x" * 900000)
            failure = structured(mcp.receive())
            self.assertEqual(failure["request_state"], "unresolved")
            mcp.call("genera_session_status", "status")
            self.assertEqual(structured(mcp.receive())["state"], "outcome-unknown")

    def test_eof_closes_a_pending_lookup_promptly(self):
        received = threading.Event()

        def serve(stream, _):
            receive_request(stream)
            self.assertEqual(receive_request(stream)[":op"], ":request-status")
            received.set()

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            target = self.timeout(mcp)
            mcp.call("genera_request_status", "pending-lookup", **target)
            self.assertTrue(received.wait(3))
            started = time.monotonic()
            mcp.close()
            self.assertLess(time.monotonic() - started, 1)

    def test_rejection_before_admission_is_not_a_terminal_ledger_result(self):
        def serve(stream, _):
            work = receive_request(stream)
            reply(stream, {":version": 1, ":id": work[":id"], ":boot-id": "boot",
                           ":service-id": "service", ":ok": None,
                           ":error": {":code": ":busy", ":message": "Worker busy"}})
            lookup_reply(stream, ":unknown")
            reply(stream, response(receive_request(stream)))

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            mcp.ready()
            mcp.call("genera_eval", "rejected", form="not-admitted")
            rejected = structured(mcp.receive())
            self.assertEqual(rejected["error"]["code"], "busy")
            mcp.call("genera_request_status", "lookup", **identity(rejected))
            self.assertEqual(structured(mcp.receive())["request_state"], "unknown")
            mcp.call("genera_eval", "next", form="42")
            self.assertEqual(structured(mcp.receive())["values"], ["42"])

    def test_queued_deadline_and_disconnect_are_provably_not_submitted(self):
        result = run_lisp(lisp_program('''(progn
 (let ((s (%make-genera-session :state :ready
            :hello '(:boot-id "boot" :service-id "service" :features (:eval)))))
   (let ((work (genera-submit-request s :eval :form "never-sent" :host-timeout .001)))
     (sleep .01)
     (assert (eq :timeout (handler-case (genera-await-request s work)
                             (genera-session-error (e) (genera-session-error-kind e)))))
     (assert (eq :not-submitted (genera-request-state work)))
     (assert (null (genera-session-queue s)))
     (assert (null (genera-session-execution s))))
   (let ((work (genera-submit-request s :eval :form "also-never-sent")))
     (genera-session-fail s (make-condition 'genera-session-error :kind :transport))
     (assert (eq :not-submitted (genera-request-state work)))
     (assert (null (genera-session-execution s)))))
 (write-string "not-submitted"))'''))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "not-submitted")

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_lost_reply_new_client_and_busy_recovery(self):
        address = (os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888")))
        with socket.create_connection(address, timeout=5) as control:
            control.settimeout(10)
            legacy(control, '(setq *mcp-deadline-check* 0)')
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with bridge(genera_settings(address)) as mcp:
                    mcp.initialize()
                    mcp.ready()
                    mcp.call("genera_eval", "once", timeout_ms=100,
                             form='(progn (incf *mcp-deadline-check*) (sleep 1) *mcp-deadline-check*)')
                    failure = structured(mcp.receive())
                    self.assertEqual(failure["error"]["code"], "timeout")
                    target = identity(failure)
                    mcp.close()
                with bridge(genera_settings(address)) as mcp:
                    mcp.initialize()
                    current = mcp.ready()
                    deadline = time.monotonic() + 4
                    while True:
                        mcp.call("genera_request_status", "retained", **target)
                        found = structured(mcp.receive())
                        if found["request_state"] == "completed":
                            break
                        self.assertEqual(found["request_state"], "running")
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.05)
                    self.assertEqual(found["result"]["values"], ["1"])
                    missing = dict(target, request_id="deadline-unknown-" + target["request_id"])
                    mcp.call("genera_request_status", "missing", **missing)
                    self.assertEqual(structured(mcp.receive())["request_state"], "unknown")
                    with client(*address) as (observer, hello):
                        send(observer, native_request(hello, ":eval", "deadline-other-" + target["request_id"],
                                                      form="(sleep 1)"))
                        # Same-stream control confirms admission before recovery.
                        send(observer, native_request(hello, ":status", "deadline-busy"))
                        self.assertTrue(receive(observer)[":busy"])
                        recover(mcp, "busy", missing, expected_boot_id=current["boot_id"],
                                expected_service_id=current["service_id"])
                        self.assertEqual(structured(mcp.receive())["error"]["code"], "busy")
                        self.assertEqual(receive(observer)[":request-state"], ":completed")
                    recover(mcp, "idle", missing, expected_boot_id=current["boot_id"],
                            expected_service_id=current["service_id"])
                    self.assertTrue(structured(mcp.receive())["acknowledged"])
                    mcp.call("eval_genera", "count", expression="*mcp-deadline-check*")
                    self.assertEqual(structured(mcp.receive())["values"], ["1"])
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy *mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, '*mcp-deadline-check*'), "1")
        print("Live deadline: lost reply, new-client ledger, one side effect, busy/idle recovery.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
