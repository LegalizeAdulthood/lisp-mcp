"""Native worker, connection, and response serialization acceptance checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import time
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from configuration import ROOT, sbcl_command
from evaluation import properties, wire
from framing import frame, read_live_field
from protocol import legacy, reload_sources


def send(stream, request):
    stream.sendall(frame(wire(request).encode(), b""))


def receive(stream):
    return properties(read_live_field(stream, normalize=False))


def request(hello, operation, request_id, **fields):
    result = {":version": 1, ":id": request_id, ":op": operation,
              ":boot-id": hello[":boot-id"], ":service-id": hello[":service-id"]}
    result.update({":" + k.replace("_", "-"): v for k, v in fields.items()})
    return result


@contextmanager
def client(host, port):
    with socket.create_connection((host, port), timeout=5) as stream:
        stream.settimeout(15)
        send(stream, {":version": 1, ":id": "hello", ":op": ":hello"})
        hello = receive(stream)
        if not hello[":ok"]:
            raise AssertionError(hello)
        yield stream, hello


class ConcurrencyChecks(unittest.TestCase):
    def test_portable_admission_and_queue_bounds(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "concurrency.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Concurrency unit checks:", result.stdout)
        print(result.stdout.strip())

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_worker_and_response_serialization(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as control:
            control.settimeout(30)
            self.assertEqual(reload_sources(control), "codec loaded")
            identity = legacy(control, "(princ-to-string *mcp-worker*)")
            self.assertEqual(legacy(control,
                '(let ((*mcp-service* (list :running t)) (*mcp-worker* nil)) '
                '(condition-case () (mcp-ensure-worker) (error :refused)))'), "REFUSED")
            # Exercise the actual native loop with isolated bindings and an
            # invalid internal job. It must exit without a console debugger,
            # retain the unresolved slot, and refuse another submission.
            fault = '''(progn
 (defun mcp-test-worker-failure ()
   (let ((cell (list nil)))
     (scl:process-run-function "MCP isolated failure check"
       (lambda ()
         (let ((*mcp-state-lock* (process:make-lock "MCP isolated state" :recursive t))
               (*mcp-worker-busy* (list :state :pending :request (list :id "fault") :client 42))
               (*mcp-worker-failed* nil) (*mcp-bootstrap-service* nil))
           (mcp-worker-loop)
           (setf (car cell)
                 (list *mcp-worker-failed* (getf *mcp-worker-busy* :state)
                       (getf (getf (mcp-submit-eval (list :id "next") *mcp-service* nil)
                                   :error) :code))))))
     (scl:process-wait-with-timeout "MCP isolated failure" 3 (lambda () (car cell)))
     (car cell)))
 (compile 'mcp-test-worker-failure)
 (mcp-test-worker-failure))'''
            self.assertEqual(legacy(control, fault.replace("\n", "\r")),
                             "(T RUNNING WORKER-UNAVAILABLE)")
            legacy(control, '(setq *mcp-concurrency-counter* 0)')
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with client(host, port) as (a, hello), client(host, port) as (b, other):
                    send(a, request(hello, ":eval", "slow", form=
                        '(progn (incf *mcp-concurrency-counter*) (sleep 2) '
                        '*mcp-concurrency-counter*)'))
                    started = time.monotonic()
                    send(a, request(hello, ":status", "own-status"))
                    status = receive(a)
                    self.assertEqual(status[":id"], "own-status")
                    self.assertTrue(status[":busy"])
                    self.assertEqual(status[":active-request-id"], "slow")
                    self.assertLess(time.monotonic() - started, 1.5)
                    send(b, request(other, ":eval", "competing", form=
                        '(incf *mcp-concurrency-counter*)'))
                    busy = receive(b)
                    self.assertIsNone(busy[":ok"])
                    self.assertEqual(dict(zip(busy[":error"][::2], busy[":error"][1::2]))[":code"],
                                     ":busy")
                    self.assertEqual(busy[":active-request-id"], "slow")
                    self.assertNotIn(":request-state", busy)
                    with client(host, port) as (_, fresh):
                        self.assertTrue(fresh[":busy"])
                        self.assertEqual(fresh[":active-request-id"], "slow")
                    done = receive(a)
                    self.assertEqual(done[":id"], "slow")
                    self.assertEqual(done[":values"], ["1"])
                    send(b, request(other, ":eval", "after", form="*mcp-concurrency-counter*"))
                    self.assertEqual(receive(b)[":values"], ["1"])
                    # Simultaneous submissions race for one slot, rather than
                    # waiting in an execution queue and both running later.
                    with ThreadPoolExecutor(2) as pool:
                        futures = [pool.submit(send, stream, request(greeting, ":eval", name,
                                   form='(progn (incf *mcp-concurrency-counter*) '
                                        '(sleep .5) *mcp-concurrency-counter*)'))
                                   for stream, greeting, name in [(a, hello, "race-a"),
                                                                  (b, other, "race-b")]]
                        for future in futures:
                            future.result()
                    replies = [receive(a), receive(b)]
                    self.assertEqual(sum(bool(r[":ok"]) for r in replies), 1)
                    self.assertEqual(next(r for r in replies if r[":ok"])[":values"], ["2"])
                    send(a, request(hello, ":eval", "replace-active", form=
                        '(condition-case () (mcp-start-service :replace t) (error :refused))'))
                    self.assertEqual(receive(a)[":values"], [":REFUSED"])
                    # Client EOF releases the socket, not the execution slot.
                    with client(host, port) as (gone, greeting):
                        send(gone, request(greeting, ":eval", "disconnected", form=
                            '(progn (incf *mcp-concurrency-counter*) (sleep 1) :done)'))
                        send(gone, request(greeting, ":status", "accepted"))
                        self.assertTrue(receive(gone)[":busy"])
                    send(b, request(other, ":eval", "during-disconnect", form=
                        '(incf *mcp-concurrency-counter*)'))
                    self.assertIsNone(receive(b)[":ok"])
                    deadline = time.monotonic() + 5
                    while True:
                        send(b, request(other, ":status", "wait"))
                        if not receive(b)[":busy"]:
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    send(b, request(other, ":eval", "disconnect-result",
                                    form="*mcp-concurrency-counter*"))
                    self.assertEqual(receive(b)[":values"], ["3"])
                # A legacy connection also receives immediate busy, while a
                # versioned evaluation holds the shared execution worker.
                with client(host, port) as (a, hello):
                    send(a, request(hello, ":eval", "legacy-gate", form="(sleep 1)"))
                    send(a, request(hello, ":status", "gate-status"))
                    self.assertTrue(receive(a)[":busy"])
                    control.sendall(frame(b'(incf *mcp-concurrency-counter*)', b""))
                    fields = [read_live_field(control) for _ in range(3)]
                    self.assertIn("busy", fields[2])
                    self.assertEqual(receive(a)[":id"], "legacy-gate")
                # Block one writer before its first control response, pipeline
                # more controls than fit, then inspect the bounded backlog.
                hook = '''(progn
 (defvar *mcp-test-writer-release* nil)
 (defvar *mcp-original-write-envelope* nil)
 (setq *mcp-original-write-envelope* (symbol-function 'mcp-write-envelope)
       *mcp-test-writer-release* nil)
 (defun mcp-write-envelope (stream response)
   (when (equal (getf response :id) "hold")
     (scl:process-wait "MCP test writer" (lambda () *mcp-test-writer-release*)))
   (let ((text (mcp-v1-encode response :response)))
     (mcp-write-length stream (length text))
     (loop for start from 0 below (length text) by 73
           do (write-string text stream :start start :end (min (+ start 73) (length text)))
              (send stream :force-output) (sleep .001))))
 (compile 'mcp-write-envelope) :hooked)'''
                legacy(control, hook.replace("\n", "\r"))
                try:
                    with client(host, port) as (a, hello):
                        send(a, request(hello, ":status", "hold"))
                        for i in range(40):
                            send(a, request(hello, ":status", f"queued-{i}"))
                        deadline = time.monotonic() + 5
                        while True:
                            maximum = legacy(control,
                                '(reduce #\'max *mcp-connections* :key '
                                '(lambda (c) (getf c :controls)))')
                            if maximum == "16":
                                break
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.02)
                        legacy(control, '(setq *mcp-test-writer-release* t)')
                        self.assertEqual(receive(a)[":id"], "hold")
                        self.assertEqual([receive(a)[":id"] for _ in range(40)],
                                         [f"queued-{i}" for i in range(40)])
                        # A large terminal reply shares a socket with controls
                        # while writes are deliberately fragmented and delayed.
                        send(a, request(hello, ":eval", "large", form=
                            '(progn (sleep .1) (make-string 30000 :initial-element #\\x))'))
                        for i in range(20):
                            send(a, request(hello, ":status", f"fragment-{i}"))
                        replies = [receive(a) for _ in range(21)]
                        self.assertEqual({r[":id"] for r in replies},
                                         {"large"} | {f"fragment-{i}" for i in range(20)})
                        self.assertEqual(len(next(r for r in replies if r[":id"] == "large")
                                             [":values"][0]), 30002)
                finally:
                    legacy(control, '(progn (setq *mcp-test-writer-release* t) '
                                    '(setf (symbol-function \'mcp-write-envelope) '
                                    '*mcp-original-write-envelope*) :restored)')
                # One retained legacy control plus fifteen versioned clients
                # fills the native admission limit; the next socket is closed.
                held = []
                try:
                    for i in range(15):
                        stream = socket.create_connection((host, port), timeout=5)
                        stream.settimeout(5)
                        held.append(stream)
                        send(stream, {":version": 1, ":id": f"limit-{i}", ":op": ":hello"})
                        self.assertTrue(receive(stream)[":ok"])
                    with socket.create_connection((host, port), timeout=5) as extra:
                        extra.settimeout(5)
                        try:
                            self.assertEqual(extra.recv(1), b"")
                        except ConnectionResetError:
                            pass
                    self.assertEqual(legacy(control, '(length *mcp-connections*)'), "16")
                finally:
                    for stream in held:
                        stream.close()
                self.assertEqual(legacy(control, '(princ-to-string *mcp-worker*)'), identity)
                self.assertEqual(reload_sources(control), "codec loaded")
                self.assertEqual(legacy(control, '(princ-to-string *mcp-worker*)'), identity)
                self.assertEqual(legacy(control, '*mcp-concurrency-counter*'), "3")
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy '
                                '*mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, '(+ 20 22)'), "42")
        with socket.create_connection((host, port), timeout=5) as ordered:
            ordered.settimeout(5)
            ordered.sendall(frame(b'(progn (sleep .05) 11)', b"") + frame(b'22', b""))
            self.assertEqual([read_live_field(ordered) for _ in range(6)],
                             ["11", "", "", "22", "", ""])
        print("Live concurrency: one worker, responsive controls, intact frames, bounded clients/queues.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
