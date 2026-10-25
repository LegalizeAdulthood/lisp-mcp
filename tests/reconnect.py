"""Reconnect, generation changes, bounded health, and shutdown checks."""

import os
import queue
import socket
import sys
import threading
import time
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from concurrency import receive, send
from deadlines import connections, identity, lookup_reply
from evaluation import receive_request
from framing import frame, read_live_field
from protocol import legacy, lisp_string, run_lisp
from session import greeting, lisp_program, reply, response
from stdio import bridge, genera_settings, structured


def accept(listener, change=None):
    stream = listener.accept()[0]
    stream.settimeout(5)
    hello = receive_request(stream)
    if hello[":op"] != ":hello":
        raise AssertionError(hello)
    packet = greeting(hello)
    if change:
        change(packet)
    reply(stream, packet)
    return stream, hello


def program(address, body, options="", begin=False):
    host, port = address
    source = f'''(let ((s ({"genera-begin-session" if begin else "genera-open-session"}
        :host {lisp_string(host)} :port {port} :reconnect t {options}
        :io-timeout .2 :connect-timeout .2 :retry-min .07 :retry-max .14
        :health-interval 30 :observe-interval .06)))
      (unwind-protect
          (flet ((wait-for (predicate)
                   (let ((end (+ (genera-now) 4)))
                     (loop until (funcall predicate)
                           do (assert (< (genera-now) end)) (sleep .005)))))
            {body})
        (genera-close-session s :interrupt-io t)))'''
    return run_lisp(lisp_program(source))


def until_status(mcp, predicate):
    deadline = time.monotonic() + 4
    while True:
        mcp.call("genera_session_status", "poll")
        status = structured(mcp.receive())
        if predicate(status):
            return status
        if time.monotonic() > deadline:
            raise AssertionError(status)
        time.sleep(.01)


class ReconnectChecks(unittest.TestCase):
    def check(self, result, marker="checked"):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, marker)

    def test_idle_loss_reauthenticates_and_retains_generation(self):
        hellos = []

        def serve(listener):
            stream, hello = accept(listener)
            with stream:
                hellos.append(hello)
                reply(stream, response(receive_request(stream)))
            stream, hello = accept(listener)
            with stream:
                hellos.append(hello)
                work = receive_request(stream)
                self.assertEqual(work[":form"], "after-reconnect")
                reply(stream, response(work))
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (assert (getf (genera-call s :status) :ok))
 (wait-for (lambda () (and (= 1 (genera-session-reconnects s))
                           (eq :ready (genera-session-state s)))))
 (assert (null (genera-session-generation-change s)))
 (assert (equal '("42") (getf (genera-call s :eval :form "after-reconnect") :values)))
 (write-string "checked")''', ':token "secret" :expected-world "fixture.vlod"')
        self.check(result)
        self.assertEqual([h[":token"] for h in hellos], ["secret", "secret"])
        self.assertEqual([h[":expected-world"] for h in hellos], ["fixture.vlod"] * 2)

    def test_disconnect_reconciles_running_then_terminal_without_replay(self):
        forms = []

        def serve(listener):
            stream, _ = accept(listener)
            with stream:
                work = receive_request(stream)
                forms.append(work[":form"])
            stream, _ = accept(listener)
            with stream:
                lookup_reply(stream, ":running")
                lookup_reply(stream, ":completed", response(work))
                next_work = receive_request(stream)
                forms.append(next_work[":form"])
                reply(stream, response(next_work))
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (let ((work (genera-submit-request s :eval :form "side-effect")))
   (assert (eq :transport (handler-case (genera-await-request s work)
                          (genera-session-error (e) (genera-session-error-kind e)))))
   (wait-for (lambda () (null (genera-session-execution s))))
   (assert (equal '("42") (getf (genera-request-response work) :values))))
 (assert (getf (genera-call s :eval :form "different-form") :ok))
 (write-string "checked")''')
        self.check(result)
        self.assertEqual(forms, ["side-effect", "different-form"])

    def test_unknown_reconciliation_remains_blocked_without_polling_forever(self):
        observed = threading.Event()

        def serve(listener):
            stream, _ = accept(listener)
            with stream:
                receive_request(stream)
            stream, _ = accept(listener)
            with stream:
                lookup_reply(stream, ":unknown")
                observed.set()
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (let ((work (genera-submit-request s :eval :form "once")))
   (handler-case (genera-await-request s work) (genera-session-error () nil))
   (wait-for (lambda () (eq :unknown (genera-request-remote-state work))))
   (sleep .25)
   (assert (eq work (genera-session-execution s)))
   (assert (eq :busy (handler-case (genera-submit-request s :eval :form "blocked")
                       (genera-session-error (e) (genera-session-error-kind e))))))
 (write-string "checked")''')
        self.check(result)
        self.assertTrue(observed.is_set())

    def test_boot_and_service_replacement_preserve_unknown_until_recovery(self):
        for key, world_lost in [(":boot-id", True), (":service-id", False)]:
            with self.subTest(key=key):
                def serve(listener):
                    stream, _ = accept(listener)
                    with stream:
                        receive_request(stream)
                    stream, _ = accept(listener, lambda p: p.update({key: "replacement"}))
                    with stream:
                        status = receive_request(stream)
                        self.assertEqual(status[":op"], ":status")
                        idle = response(status)
                        idle[key] = "replacement"
                        reply(stream, idle)
                        self.assertEqual(stream.recv(1), b"")

                new_boot = '"replacement"' if world_lost else '"boot"'
                new_service = '"service"' if world_lost else '"replacement"'
                with connections(serve) as address:
                    result = program(address, f'''
 (let ((work (genera-submit-request s :eval :form "side-effect")))
   (handler-case (genera-await-request s work) (genera-session-error () nil))
   (wait-for (lambda () (genera-session-generation-change s)))
   (assert (eq {'t' if world_lost else 'nil'}
               (getf (genera-session-generation-change s) :world-state-lost)))
   (assert (getf (genera-session-generation-change s) :history-lost))
   (assert (eq work (genera-session-execution s)))
   (assert (eq :generation-changed
     (handler-case (genera-lookup-request s (getf (genera-request-envelope work) :id) "boot" "service")
       (genera-session-error (e) (genera-session-error-kind e)))))
   (genera-recover-request s (getf (genera-request-envelope work) :id)
                           "boot" "service" {new_boot} {new_service} t)
   (assert (eq :unknown (genera-request-state work)))
   (assert (genera-request-acknowledged work))
   (assert (null (genera-session-execution s))))
 (write-string "checked")''')
                self.check(result)

    def test_idle_health_is_bounded_and_pauses_during_execution(self):
        def serve(listener):
            stream, _ = accept(listener)
            with stream:
                status = receive_request(stream)
                self.assertEqual(status[":op"], ":status")
                reply(stream, response(status))
                work = receive_request(stream)
                self.assertEqual(work[":op"], ":eval")
                stream.settimeout(.25)
                with self.assertRaises(socket.timeout):
                    stream.recv(1)
                reply(stream, response(work))
                stream.settimeout(5)
                status = receive_request(stream)
                self.assertEqual(status[":op"], ":status")
                reply(stream, response(status))
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (wait-for (lambda () (and (genera-session-cached-status s)
                          (getf (genera-session-cached-status s) :boot-id)
                          (not (equal (getf (genera-session-cached-status s) :id)
                                      (getf (genera-session-hello s) :id))))))
 (assert (getf (genera-call s :eval :form "long-work") :ok))
 (wait-for (lambda () (not (genera-session-status-stale s))))
 (write-string "checked")''', ':health-interval .08')
        self.check(result)

    def test_silent_partial_health_reply_triggers_reconnect(self):
        def serve(listener):
            stream, _ = accept(listener)
            with stream:
                self.assertEqual(receive_request(stream)[":op"], ":status")
                stream.sendall(b"100 (")
                self.assertEqual(stream.recv(1), b"")
            stream, _ = accept(listener)
            with stream:
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (wait-for (lambda () (and (plusp (genera-session-reconnects s))
                           (eq :ready (genera-session-state s)))))
 (write-string "checked")''', ':health-interval .08')
        self.check(result)

    def test_remote_busy_suppresses_health_and_cached_status_ages(self):
        def serve(listener):
            stream, _ = accept(listener, lambda p: p.update({":busy": True}))
            with stream:
                # No periodic traffic while the last observation says busy.
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (sleep .25)
 (let* ((*rpc-runtime* (make-rpc-runtime :session s)) (status (rpc-session-status)))
   (assert (eq 'yason:true (gethash "remote_status_stale" status)))
   (assert (eq 'yason:true (gethash "busy" status)))
   (assert (>= (gethash "remote_observation_age_ms" status) 200)))
 (write-string "checked")''', ':health-interval .05')
        self.check(result)

    def test_reconnect_backoff_is_capped_and_handshake_has_its_own_deadline(self):
        times = []

        def serve(listener):
            for _ in range(4):
                with listener.accept()[0] as stream:
                    stream.settimeout(5)
                    receive_request(stream)
                    times.append(time.monotonic())
                    stream.sendall(b"bad ")
            stream, _ = accept(listener)
            with stream:
                times.append(time.monotonic())
                self.assertEqual(stream.recv(1), b"")

        with connections(serve) as address:
            result = program(address, '''
 (wait-for (lambda () (and (>= (genera-session-reconnects s) 4)
                           (eq :ready (genera-session-state s)))))
 (write-string "checked")''', begin=True)
        self.check(result)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreaterEqual(gaps[0], .06)
        self.assertTrue(all(.12 <= gap < .7 for gap in gaps[1:]), gaps)

        def silent(listener):
            with listener.accept()[0] as stream:
                stream.settimeout(5)
                receive_request(stream)
                self.assertEqual(stream.recv(1), b"")
            stream, _ = accept(listener)
            with stream:
                self.assertEqual(stream.recv(1), b"")

        with connections(silent) as address:
            result = program(address, '''
 (wait-for (lambda () (and (plusp (genera-session-reconnects s))
                           (eq :ready (genera-session-state s)))))
 (write-string "checked")''', ':connect-timeout .08 :io-timeout 2', begin=True)
        self.check(result)

    def test_reconnect_rejects_authentication_and_expected_world_changes(self):
        for fault in ["authentication", "world"]:
            with self.subTest(fault=fault):
                def serve(listener):
                    stream, _ = accept(listener)
                    with stream:
                        reply(stream, response(receive_request(stream)))
                    with listener.accept()[0] as stream:
                        stream.settimeout(5)
                        hello = receive_request(stream)
                        self.assertEqual(hello[":token"], "secret")
                        self.assertEqual(hello[":expected-world"], "fixture.vlod")
                        if fault == "authentication":
                            packet = {":version": 1, ":id": hello[":id"], ":ok": None,
                                      ":error": {":code": ":authentication-failed", ":message": "Denied"}}
                        else:
                            packet = greeting(hello)
                            packet[":world"] = "other.vlod"
                        reply(stream, packet)
                        self.assertEqual(stream.recv(1), b"")

                with connections(serve) as address:
                    result = program(address, '''
 (assert (getf (genera-call s :status) :ok))
 (wait-for (lambda () (let ((failure (genera-session-failure s)))
                       (and failure (eq :handshake (genera-session-error-kind failure))))))
 (sleep .3)
 (write-string "checked")''', ':token "secret" :expected-world "fixture.vlod"')
                self.check(result)

    def test_mcp_eof_during_backoff_and_live_status_refresh(self):
        def serve(listener):
            stream, _ = accept(listener)
            with stream:
                status = receive_request(stream)
                self.assertEqual(status[":op"], ":status")
                reply(stream, response(status))

        with connections(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            ready = mcp.ready()
            self.assertFalse(ready["remote_status_stale"])
            self.assertIsInstance(ready["remote_observed_at"], int)
            mcp.call("genera_session_status", "refresh", refresh=True)
            self.assertFalse(structured(mcp.receive())["remote_status_stale"])
            stale = until_status(mcp, lambda p: p["phase"] == "backoff")
            self.assertTrue(stale["remote_status_stale"])
            self.assertFalse(stale["connected"])
            started = time.monotonic()
            mcp.close()
            self.assertLess(time.monotonic() - started, 1)

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_lost_reply_reconnects_to_same_genera_without_replaying(self):
        target = (os.environ["GENERA_HOST"], int(os.environ.get("GENERA_PORT", "8888")))
        forms = []

        def serve(listener):
            with listener.accept()[0] as front, socket.create_connection(target, timeout=5) as backend:
                front.settimeout(5)
                send(backend, receive_request(front))
                front.sendall(frame(read_live_field(backend, normalize=False).encode("ascii")))
                work = receive_request(front)
                forms.append(work[":form"])
                send(backend, work)
                self.assertEqual(receive(backend)[":request-state"], ":completed")
                # Drop a real terminal reply after Genera commits its ledger.
            with listener.accept()[0] as front, socket.create_connection(target, timeout=5) as backend:
                front.settimeout(5)
                send(backend, receive_request(front))
                front.sendall(frame(read_live_field(backend, normalize=False).encode("ascii")))
                while front.recv(1, socket.MSG_PEEK):
                    work = receive_request(front)
                    if work[":op"] == ":eval":
                        forms.append(work[":form"])
                    send(backend, work)
                    front.sendall(frame(read_live_field(backend, normalize=False).encode("ascii")))

        with socket.create_connection(target, timeout=5) as control:
            control.settimeout(10)
            legacy(control, '(setq *mcp-reconnect-check* 0)')
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with connections(serve) as address, bridge(genera_settings(address)) as mcp:
                    mcp.initialize()
                    initial = mcp.ready()
                    mcp.call("genera_eval", "once", form='(progn (incf *mcp-reconnect-check*) '
                             '(sleep .3) *mcp-reconnect-check*)')
                    failure = structured(mcp.receive())
                    self.assertEqual(failure["error"]["code"], "transport")
                    recovered = until_status(mcp, lambda p: p["state"] == "ready" and p["reconnect_attempts"] == 1)
                    self.assertEqual(recovered["boot_id"], initial["boot_id"])
                    self.assertEqual(recovered["service_id"], initial["service_id"])
                    mcp.call("genera_request_status", "retained", **identity(failure))
                    self.assertEqual(structured(mcp.receive())["result"]["values"], ["1"])
                    mcp.call("eval_genera", "count", expression="*mcp-reconnect-check*")
                    self.assertEqual(structured(mcp.receive())["values"], ["1"])
                    with self.assertRaises(queue.Empty):
                        mcp.receive(timeout=.1)
                    self.assertEqual(sum(p.get("id") == "once" for p in mcp.received), 1)
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy *mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, '*mcp-reconnect-check*'), "1")
        self.assertEqual(len(forms), 2)
        print("Live reconnect: same generation, retained result, one side effect, Genera preserved.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
