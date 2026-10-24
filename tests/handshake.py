"""Hello, identity, and service lifecycle checks; live mode restores legacy."""

import os
import socket
import subprocess
import sys
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from configuration import ROOT, sbcl_command
from framing import frame, peer, read_live_field
from protocol import (exchange, legacy, lisp_string, reload_sources,
                      status_request, wire_exchange, wire_string)


HELLO = '(:version 1 :id "hello" :op :hello :expected-world "fixture.vlod")'
REPLY = ('(:version 1 :id "hello" :ok t :boot-id "boot" :service-id "service" '
         ':world "fixture.vlod" :package "USER" :busy nil :features (:status))')


class HandshakeChecks(unittest.TestCase):
    def test_portable_service_state(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "handshake.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Handshake unit checks:", result.stdout)
        print(result.stdout.strip())

    def test_host_validates_hello_metadata(self):
        with peer(frame(REPLY.encode()), fragment=True) as (host, port):
            result = exchange(host, port, HELLO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, REPLY)

    def test_host_rejects_wrong_world_id_and_incomplete_hello(self):
        for reply in [REPLY.replace("fixture.vlod", "wrong.vlod"),
                      REPLY.replace(':id "hello"', ':id "wrong"'),
                      REPLY.replace(' :features (:status)', ''),
                      REPLY.replace(':version 1', ':version 2')]:
            with self.subTest(reply=reply):
                with peer(frame(reply.encode())) as (host, port):
                    result = exchange(host, port, HELLO)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "rejected")

    def test_host_retains_structured_authentication_failure(self):
        failure = ('(:version 1 :id "hello" :ok nil :error '
                   '(:code :authentication-failed :message "Authentication failed"))')
        with peer(frame(failure.encode())) as (host, port):
            result = exchange(host, port, HELLO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, failure)

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_authenticated_handshake_and_lifecycle(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as loader:
            loader.settimeout(30)
            identity = '(list *mcp-boot-id* (getf *mcp-service* :service-id))'
            # The listener may predate this slice on its first invocation.
            self.assertEqual(reload_sources(loader), "codec loaded")
            detected = legacy(loader, '(mcp-detect-world)')
            self.assertNotEqual(detected, "NIL")
            self.assertEqual(detected, legacy(loader, '(getf *mcp-service* :world)'))
            before = legacy(loader, identity)
            self.assertEqual(reload_sources(loader), "codec loaded")
            self.assertEqual(legacy(loader, identity), before)
            legacy(loader, '(setq *mcp-v1-test-sentinel* nil)')

        # One trusted legacy eval manages the native lifecycle while other
        # connections exercise the production version-1 handler. It emits a
        # test-only ready field, then restores legacy in UNWIND-PROTECT.
        program = r'''
(let ((others (remove :mcp-server net:*services-enabled*))
      (worker *mcp-worker*))
  (unwind-protect
      (let* ((id (mcp-start-service :protocol :v1 :token "test-secret" :replace t))
             (boot *mcp-boot-id*))
        (unless (equal id (mcp-start-service)) (error "Start changed identity"))
        (unless (= 1 (count :mcp-server net:*services-enabled*))
          (error "Duplicate service registration"))
        (mcp-write-field *mcp-current-client* "ready")
        (send *mcp-current-client* :force-output)
        (loop repeat 1000 until (getf *mcp-service* :clients) do (sleep .02))
        (unless (getf *mcp-service* :clients) (error "No test client connected"))
        (let ((leader (car (last (getf *mcp-service* :clients)))))
          (loop repeat 1000 while (member leader (getf *mcp-service* :clients))
                do (sleep .02))
          (when (member leader (getf *mcp-service* :clients))
            (error "Test client did not finish")))
        (when *mcp-v1-test-sentinel* (error "Request form was evaluated"))
        (mcp-stop-service)
        (mcp-stop-service)
        (when (member :mcp-server net:*services-enabled*)
          (error "Service was not disabled"))
        (when (equal id (mcp-start-service)) (error "Service identity was reused"))
        (unless (equal boot *mcp-boot-id*) (error "Restart changed boot identity"))
        (mcp-initialize-boot)
        (when (equal boot *mcp-boot-id*) (error "Boot identity was reused"))
        (net:define-server :mcp-server (:medium :byte-stream :stream stream)
          (close stream :abort t))
        (let ((bootstrap (symbol-function 'mcp-server-server-function-internal)))
          (mcp-start-service)
          (when (eq bootstrap (symbol-function 'mcp-server-server-function-internal))
            (error "Bootstrap handler was not replaced")))
        (unless (equal "test-secret" (getf *mcp-service* :token))
          (error "Boot initialization lost authentication"))
        (unless (equal others (remove :mcp-server net:*services-enabled*))
          (error "Unrelated services changed"))
        (unless (eq worker *mcp-worker*) (error "Execution worker was replaced"))
        "lifecycle checked")
    (mcp-stop-service)
    (net:define-server :mcp-server (:medium :byte-stream :stream stream)
      (mcp-handle-client stream))
    (setf *mcp-listener-installed* t)
    (mcp-start-service :protocol :legacy :token nil :replace t)))
'''
        with socket.create_connection((host, port), timeout=5) as control:
            control.settimeout(30)
            control.sendall(frame(program.replace("\n", "\r").encode(), b""))
            self.assertEqual(read_live_field(control), "ready")
            with socket.create_connection((host, port), timeout=5) as keeper:
                keeper.settimeout(5)
                unknown = ('(:version 1 :id "first" :op :status '
                           ':boot-id "unknown" :service-id "unknown")')
                self.assertFailure(wire_exchange(keeper, unknown), ":handshake-required")
                for request in ['(:version 1 :id "bad" :op :hello)',
                                '(:version 1 :id "bad" :op :hello :token "wrong" '
                                ':expected-world "wrong.vlod")']:
                    self.assertFailure(wire_exchange(keeper, request), ":authentication-failed")
                self.assertFailure(wire_exchange(keeper,
                    '(:version 1 :id "bad" :op :hello :token "test-secret" '
                    ':expected-world "wrong.vlod")'), ":world-mismatch")
                hello_request = '(:version 1 :id "hello" :op :hello :token "test-secret")'
                hello = wire_exchange(keeper, hello_request)
                self.assertIn(":ok t", hello)
                self.assertIn(":features (:status :eval)", hello)
                # This suite's long legacy management form owns the worker.
                self.assertIn(":busy t", hello)
                self.assertTrue(wire_string(hello, "world"))
                self.assertTrue(wire_string(hello, "package"))
                self.assertEqual(wire_exchange(keeper, hello_request), hello)
                expected_request = (hello_request[:-1] + ':expected-world '
                                    + lisp_string(wire_string(hello, "world")) + ')')
                for _ in range(2):
                    with socket.create_connection((host, port), timeout=5) as client:
                        client.settimeout(5)
                        self.assertFailure(wire_exchange(client, status_request(hello)),
                                           ":handshake-required")
                        self.assertEqual(wire_exchange(client, expected_request), hello)
                status = wire_exchange(keeper, status_request(hello))
                self.assertIn(":ok t", status)
                self.assertEqual(wire_string(status, "boot-id"), wire_string(hello, "boot-id"))
                for change in [dict(boot="old"), dict(service="old")]:
                    failure = wire_exchange(keeper, status_request(hello, **change))
                    self.assertIn(":stale-generation", failure)
                    self.assertEqual(wire_string(failure, "service-id"),
                                     wire_string(hello, "service-id"))
                self.assertFailure(wire_exchange(keeper,
                    '(:version 1 :id "bad" :op :hello :token "wrong")'),
                    ":authentication-failed")
                self.assertFailure(wire_exchange(keeper, status_request(hello)),
                                   ":handshake-required")
                self.assertEqual(wire_exchange(keeper, hello_request), hello)
                evaluation = (status_request(hello, boot="old").replace(
                              ":op :status", ":op :eval")[:-1]
                              + ' :form "(setq *mcp-v1-test-sentinel* t)")')
                self.assertIn(":stale-generation", wire_exchange(keeper, evaluation))
                with socket.create_connection((host, port), timeout=5) as client:
                    client.settimeout(5)
                    self.assertFailure(wire_exchange(client, evaluation),
                                       ":handshake-required")
                for bad in ['(:version 2 :id "bad" :op :hello :token "test-secret")',
                            '(setq *mcp-v1-test-sentinel* t)']:
                    with socket.create_connection((host, port), timeout=5) as client:
                        client.settimeout(3)
                        client.sendall(frame(bad.encode(), b""))
                        self.assertClosed(client)
                result = exchange(host, port, expected_request)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, hello)
                survivor = socket.create_connection((host, port), timeout=5)
                survivor.settimeout(5)
                try:
                    self.assertEqual(wire_exchange(survivor, hello_request), hello)
                    keeper.close()
                    self.assertClosed(survivor)
                finally:
                    survivor.close()
            result, stdout, stderr = [read_live_field(control) for _ in range(3)]
            self.assertEqual((result, stdout, stderr), ("lifecycle checked", "", ""))
        with socket.create_connection((host, port), timeout=5) as client:
            client.settimeout(5)
            self.assertEqual(legacy(client, '(+ 20 22)'), "42")
            self.assertEqual(legacy(client,
                '(list (getf *mcp-service* :protocol) (getf *mcp-service* :token) '
                '*mcp-v1-test-sentinel* (count :mcp-server net:*services-enabled*))'),
                "(LEGACY NIL NIL 1)")
        print("Live handshake: authentication, generations, lifecycle, and legacy recovery passed.")

    def assertFailure(self, reply, code):
        self.assertIn(":ok nil", reply)
        self.assertIn(code, reply)
        for key in [":boot-id", ":service-id", ":world", ":package", ":features"]:
            self.assertNotIn(key + " ", reply)
        self.assertNotIn("test-secret", reply)

    def assertClosed(self, stream):
        try:
            self.assertEqual(stream.recv(1), b"")
        except ConnectionResetError:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
