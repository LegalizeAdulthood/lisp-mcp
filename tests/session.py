"""Persistent host session checks with scripted TCP peers and optional Genera."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
import sys
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from evaluation import receive_request, wire
from framing import frame
from protocol import legacy, lisp_string, run_lisp


def greeting(request):
    return {":version": 1, ":id": request[":id"], ":ok": True,
            ":boot-id": "boot", ":service-id": "service", ":world": "fixture.vlod",
            ":package": "USER", ":busy": None,
            ":features": [":status", ":request-status", ":eval"]}


def response(request, value="42"):
    result = {":version": 1, ":id": request[":id"], ":ok": True,
              ":boot-id": "boot", ":service-id": "service"}
    if request[":op"] in (":eval", ":reset-listener-context", ":load", ":compile-file", ":run-tests", ":describe", ":apropos", ":find-definition"):
        result.update({":request-state": ":completed", ":values": [value], ":value-count": 1,
                       ":stdout": "a\nb\r\nc\t", ":stderr": "", ":warnings": None,
                       ":package": "USER", ":elapsed-ms": 1, ":truncated": None,
                       ":replaced": None})
    elif request[":op"] == ":request-status":
        result.update({":target-id": request[":target-id"], ":request-state": ":running"})
    else:
        result.update({key: value for key, value in greeting(request).items()
                       if key not in result})
    return result


def reply(stream, result):
    stream.sendall(frame(wire(result).encode("ascii")))


@contextmanager
def peer(behavior, hello_change=None, accept_timeout=5):
    with socket.socket() as listener, ThreadPoolExecutor(1) as pool:
        listener.bind(("127.0.0.2", 0))
        listener.listen(2)
        listener.settimeout(accept_timeout)

        def serve():
            with listener.accept()[0] as stream:
                stream.settimeout(5)
                hello = receive_request(stream)
                if hello[":op"] != ":hello":
                    raise AssertionError("Session omitted hello")
                result = greeting(hello)
                if hello_change:
                    hello_change(result)
                reply(stream, result)
                behavior(stream, hello)
                try:
                    if stream.recv(1) != b"":
                        raise AssertionError("Session sent an unexpected request")
                except ConnectionResetError:
                    pass
            listener.settimeout(.1)
            try:
                extra, _ = listener.accept()
            except socket.timeout:
                return
            extra.close()
            raise AssertionError("Session opened another connection")

        finished = pool.submit(serve)
        yield listener.getsockname()
        finished.result(timeout=10)


def lisp_program(source):
    return ('(let ((*package* (find-package "LISP-MCP"))) '
            f'(eval (read-from-string {lisp_string(source)})))')


def run_session(host, port, body, **settings):
    options = " ".join(":" + key.replace("_", "-") + " " + wire(value)
                       for key, value in settings.items())
    return run_lisp(lisp_program(
        f'(let ((s (genera-open-session :host {lisp_string(host)} :port {port} {options}))) '
        f'(unwind-protect (progn {body}) (genera-close-session s)))'))


class SessionChecks(unittest.TestCase):
    def assertProgram(self, result, marker):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, marker)

    def test_repeated_helper_evals_use_one_authenticated_socket(self):
        received = []

        def serve(stream, hello):
            self.assertEqual(hello[":token"], "secret")
            self.assertEqual(hello[":expected-world"], "fixture.vlod")
            for _ in range(3):
                request = receive_request(stream)
                received.append(request)
                reply(stream, response(request))
            self.assertEqual(len({r[":id"] for r in received} | {hello[":id"]}), 4)

        body = '''
 (assert (equal '("42") (getf (genera-eval-envelope "(+ 20 22)" :session s) :values)))
 (assert (equal '("42") (getf (genera-eval-envelope "line\n;tail" :session s
                                  :package "USER" :print-level 3) :values)))
 (assert (getf (genera-call s :status) :ok))
 (dolist (option '((:host "other") (:port 1) (:token "wrong") (:expected-world "other.vlod")))
   (assert (handler-case
     (progn (apply #'genera-eval-envelope "99" :session s option) nil)
     (error () t))))
 (assert (zerop (hash-table-count (genera-session-pending s))))
 (write-string "reused")'''
        with peer(serve) as address:
            result = run_session(*address, body, token="secret", expected_world="fixture.vlod")
        self.assertProgram(result, "reused")
        self.assertEqual([r[":op"] for r in received], [":eval", ":eval", ":status"])
        self.assertEqual(received[1][":form"], "line\n;tail")
        self.assertEqual(received[1][":print-level"], 3)

    def test_controls_complete_while_eval_is_pending(self):
        def serve(stream, _):
            evaluation = receive_request(stream)
            status = receive_request(stream)
            reply(stream, response(status))
            lookup = receive_request(stream)
            self.assertEqual(lookup[":target-id"], evaluation[":id"])
            reply(stream, response(lookup))
            reply(stream, response(evaluation))

        body = '''
 (let* ((work (genera-submit-request s :eval :form "(sleep 1)"))
        (waiter (sb-thread:make-thread (lambda () (genera-await-request s work))))
        (status (genera-call s :status)))
   (assert (getf status :ok))
   (assert (null (genera-request-response work)))
   (assert (eq :running (getf (genera-call s :request-status
                      :target-id (getf (genera-request-envelope work) :id)) :request-state)))
   (assert (equal '("42") (getf (sb-thread:join-thread waiter) :values))))
 (write-string "multiplexed")'''
        with peer(serve) as address:
            result = run_session(*address, body)
        self.assertProgram(result, "multiplexed")

    def test_partial_reply_does_not_block_outgoing_controls(self):
        def serve(stream, _):
            first = receive_request(stream)
            data = frame(wire(response(first)).encode())
            stream.sendall(data[:5])
            second = receive_request(stream)
            stream.sendall(data[5:])
            for byte in frame(wire(response(second)).encode(), b"\r\n"):
                stream.sendall(bytes([byte]))

        body = '''
 (let ((first (genera-submit-request s :status)))
   (sleep .1)
   (let ((second (genera-submit-request s :status)))
     (assert (getf (genera-await-request s second) :ok))
     (assert (getf (genera-await-request s first) :ok))))
 (write-string "partial")'''
        with peer(serve) as address:
            result = run_session(*address, body)
        self.assertProgram(result, "partial")

    def test_execution_and_control_admission_are_bounded(self):
        def serve(stream, _):
            requests = [receive_request(stream) for _ in range(17)]
            self.assertEqual(sum(r[":op"] == ":eval" for r in requests), 1)
            for request in reversed(requests):
                reply(stream, response(request))

        body = '''
 (let ((work (genera-submit-request s :eval :form "42"))
       (controls (loop repeat 16 collect (genera-submit-request s :status))))
   (dolist (operation '(:eval :status))
     (assert (eq :busy (handler-case
       (progn (if (eq operation :eval) (genera-submit-request s :eval :form "99")
                   (genera-submit-request s :status)) :accepted)
       (genera-session-error (e) (genera-session-error-kind e))))))
   (dolist (control controls) (assert (getf (genera-await-request s control) :ok)))
   (assert (getf (genera-await-request s work) :ok)))
 (write-string "bounded")'''
        with peer(serve) as address:
            result = run_session(*address, body)
        self.assertProgram(result, "bounded")

    def test_protocol_faults_preserve_pending_identity_and_prior_completion(self):
        def bad_response(change):
            def fault(evaluation, _):
                result = response(evaluation)
                change(result)
                return frame(wire(result).encode())
            return fault

        faults = [lambda e, s: b"junk",
                  lambda e, s: b"1048577 ",
                  lambda e, s: b"5 abc",
                  lambda e, s: frame(b'(:version 1 :id "bad" :ok t :stdout "\xff")'),
                  lambda e, s: frame(wire(response(s)).encode()),
                  bad_response(lambda r: r.update({":id": "unmatched"})),
                  bad_response(lambda r: r.update({":service-id": "stale"})),
                  bad_response(lambda r: r.update({":boot-id": "stale"})),
                  bad_response(lambda r: r.pop(":stdout")),
                  bad_response(lambda r: r.update({":request-state": ":running"}))]
        for index, fault in enumerate(faults):
            with self.subTest(fault=index):
                def serve(stream, _):
                    evaluation, status = receive_request(stream), receive_request(stream)
                    reply(stream, response(status))
                    stream.sendall(fault(evaluation, status))
                    stream.shutdown(socket.SHUT_WR)

                body = '''
 (let* ((work (genera-submit-request s :eval :form "side-effect"))
        (envelope (genera-request-envelope work))
        (status (genera-call s :status)))
   (assert (getf status :ok))
   (assert (eq :protocol (handler-case (progn (genera-await-request s work) :accepted)
                 (genera-session-error (e) (genera-session-error-kind e)))))
   (assert (eq :unresolved (genera-request-state work)))
   (assert (eq work (gethash (getf envelope :id) (genera-session-pending s))))
   (assert (equal "boot" (getf envelope :boot-id)))
   (assert (equal "service" (getf envelope :service-id)))
   (assert (equal "side-effect" (getf envelope :form)))
   (assert (null (genera-request-response work)))
   (assert (null (genera-session-socket s)))
   (assert (eq :failed (genera-session-state s))))
 (write-string "protocol")'''
                with peer(serve) as address:
                    result = run_session(*address, body)
                self.assertProgram(result, "protocol")

    def test_eof_preserves_unresolved_execution_without_reconnect(self):
        def serve(stream, _):
            receive_request(stream)
            stream.shutdown(socket.SHUT_WR)

        body = '''
 (let ((work (genera-submit-request s :eval :form "once")))
   (assert (eq :transport (handler-case (progn (genera-await-request s work) :accepted)
                  (genera-session-error (e)
                    (assert (eq s (genera-session-error-session e)))
                    (genera-session-error-kind e)))))
   (assert (eq :unresolved (genera-request-state work)))
   (assert (eq work (genera-session-execution s)))
   (assert (= 1 (hash-table-count (genera-session-pending s)))))
 (write-string "retained")'''
        with peer(serve) as address:
            result = run_session(*address, body)
        self.assertProgram(result, "retained")

    def test_close_does_not_wait_for_remote_eval(self):
        def serve(stream, _):
            receive_request(stream)

        body = '''
 (let ((work (genera-submit-request s :eval :form "(sleep 100)")))
   (loop while (eq :queued (genera-request-state work)) do (sleep .005))
   (sleep .05)
   (let ((start (get-internal-real-time)))
     (genera-close-session s)
     (assert (< (/ (- (get-internal-real-time) start) internal-time-units-per-second) .5)))
   (assert (not (sb-thread:thread-alive-p (genera-session-owner s))))
   (assert (eq :unresolved (genera-request-state work)))
   (assert (eq :closed (genera-session-state s))))
 (write-string "closed")'''
        with peer(serve) as address:
            result = run_session(*address, body)
        self.assertProgram(result, "closed")

    def test_hello_validation_failure_closes_without_sending_eval(self):
        for change in [lambda r: r.update({":id": "wrong"}),
                       lambda r: r.pop(":world"),
                       lambda r: r.update({":package": "bad\x00"})]:
            with self.subTest(change=change):
                with peer(lambda stream, hello: None, hello_change=change) as address:
                    host, port = address
                    result = run_lisp(lisp_program(
                        '(handler-case (progn (genera-open-session '
                        f':host {lisp_string(host)} :port {port}) (write-string "accepted")) '
                        '(genera-session-error (e) '
                        '(write-string (string-downcase (symbol-name (genera-session-error-kind e))))))'))
                self.assertProgram(result, "protocol")

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_persistent_session_and_control_during_eval(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as control:
            control.settimeout(10)
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                body = '''
 (assert (equal '("2") (getf (genera-eval-envelope "(length *mcp-connections*)"
                                                              :session s) :values)))
 (assert (equal '("42") (getf (genera-eval-envelope "(+ 20 22)" :session s) :values)))
 (let* ((work (genera-submit-request s :eval :form
              "(progn (setq *mcp-session-check* 1) (sleep 1) *mcp-session-check*)"))
        (start (get-internal-real-time))
        (status (genera-call s :status)))
   (assert (getf status :busy))
   (assert (equal (getf (genera-request-envelope work) :id) (getf status :active-request-id)))
   (assert (< (/ (- (get-internal-real-time) start) internal-time-units-per-second) .75))
   (assert (equal '("1") (getf (genera-await-request s work) :values)))
   (assert (eq :completed (getf (genera-call s :request-status
                      :target-id (getf (genera-request-envelope work) :id)) :request-state))))
 (assert (equal '("2") (getf (genera-eval-envelope "(length *mcp-connections*)"
                                                              :session s) :values)))
 (write-string "native")'''
                result = run_session(host, port, body)
                self.assertProgram(result, "native")
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy *mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, '*mcp-session-check*'), "1")
            self.assertEqual(legacy(control, '(+ 20 22)'), "42")
        print("Live session: reused socket, pending eval controls, retained lookup, clean close.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
