"""Portable boot policy. Native cold-restore checks require a separate world."""

import os
import socket
import subprocess
import sys
import unittest
import uuid

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from configuration import ROOT, sbcl_command
from protocol import lisp_string, status_request, wire_exchange, wire_string


class BootChecks(unittest.TestCase):
    def test_boot_policy(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "boot.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Boot unit checks:", result.stdout)
        print(result.stdout.strip())

    @unittest.skipUnless(LIVE, "requires an already boot-loaded versioned listener")
    def test_boot_loaded_listener_is_idempotent(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        hello_request = '(:version 1 :id "boot-hello" :op :hello'
        for key, variable in (("token", "GENERA_TOKEN"),
                              ("expected-world", "GENERA_EXPECTED_WORLD")):
            if os.environ.get(variable):
                hello_request += f' :{key} {lisp_string(os.environ[variable])}'
        hello_request += ')'
        with socket.create_connection((host, port), timeout=5) as stream:
            stream.settimeout(15)
            hello = wire_exchange(stream, hello_request)
            self.assertIn(":ok t", hello)
            previous = os.environ.get("GENERA_PREVIOUS_BOOT_ID")
            if previous:
                self.assertNotEqual(wire_string(hello, "boot-id"), previous)
            identity = tuple(wire_string(hello, key)
                             for key in ("boot-id", "service-id"))
            program = r'''
(let ((worker *mcp-worker*) (service *mcp-service*)
      (state-lock *mcp-state-lock*) (lifecycle-lock *mcp-lifecycle-lock*))
  (dotimes (i 3)
    (unless (equal (getf service :service-id) (mcp-boot-start))
      (error "Boot initialization changed service")))
  (unless (and (eq worker *mcp-worker*) (eq service *mcp-service*)
               (eq state-lock *mcp-state-lock*)
               (eq lifecycle-lock *mcp-lifecycle-lock*)
               (mcp-worker-live-p)
               (= 1 (count :mcp-server net:*services-enabled*))
               (= 1 (count "MCP listener" si:warm-initialization-list
                            :key #'first :test #'equal)))
    (error "Boot initialization duplicated runtime resources"))
  (let ((*mcp-boot-configuration* '(:protocol :invalid)))
    (unless (and (null (mcp-boot-start))
                 (eq (getf (mcp-boot-status) :state) :failed))
      (error "Boot failure escaped containment")))
  (unless (and (equal (mcp-boot-start) (getf service :service-id))
               (eq (getf (mcp-boot-status) :state) :ready)
               (= 1 (getf (mcp-boot-status) :boot-source))
               (= 1 (getf (mcp-boot-status) :listener-source)))
    (error "Boot retry failed"))
  "boot hook stable")
'''
            response = wire_exchange(
                stream, f'(:version 1 :id "boot-check-{uuid.uuid4().hex}" :op :eval '
                f':boot-id {lisp_string(identity[0])} '
                f':service-id {lisp_string(identity[1])} '
                f':form {lisp_string(program)})')
            self.assertIn(":ok t", response)
            self.assertIn("boot hook stable", response)
            self.assertIn(":ok t", wire_exchange(stream, status_request(hello)))
        # This second TCP client attaches without changing either identity.
        with socket.create_connection((host, port), timeout=5) as stream:
            stream.settimeout(15)
            again = wire_exchange(stream, hello_request)
            self.assertEqual(identity, tuple(wire_string(again, key)
                                             for key in ("boot-id", "service-id")))


if __name__ == "__main__":
    unittest.main()
