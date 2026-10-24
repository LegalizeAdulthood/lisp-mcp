"""Retained outcomes; --live-genera restores the running legacy listener."""

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
from concurrency import client, receive, request, send
from protocol import legacy, reload_sources


def plist(items):
    return dict(zip(items[::2], items[1::2]))


class LedgerChecks(unittest.TestCase):
    def test_portable_ledger_and_result_schema(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "ledger.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ledger unit checks:", result.stdout)
        print(result.stdout.strip())

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_retained_outcomes_and_eviction(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        prefix = f"ledger-{time.time_ns()}-"
        control = socket.create_connection((host, port), timeout=5)
        control.settimeout(30)
        replaced = False
        try:
            self.assertEqual(reload_sources(control), "codec loaded")
            worker = legacy(control, '(princ-to-string *mcp-worker*)')
            legacy(control, '(setq *mcp-ledger-counter* 0)')
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with client(host, port) as (observer, hello):
                    self.assertIn(":request-status", hello[":features"])
                    self.assertEqual(hello[":history-limit"], 32)
                    self.assertEqual(hello[":history-byte-limit"], 4194304)

                    def exchange(operation, name, **fields):
                        send(observer, request(hello, operation, prefix + name, **fields))
                        return receive(observer)

                    def lookup(name):
                        return exchange(":request-status", "lookup-" + name,
                                        target_id=prefix + name)

                    self.assertEqual(lookup("absent")[":request-state"], ":unknown")
                    with client(host, port) as (lost, greeting):
                        accepted = request(greeting, ":eval", prefix + "lost", form=
                            '(progn (incf *mcp-ledger-counter*) (write-string "before") '
                            '(sleep 1) *mcp-ledger-counter*)')
                        send(lost, accepted)
                        send(lost, request(greeting, ":status", prefix + "accepted"))
                        status = receive(lost)
                        self.assertTrue(status[":busy"])
                        self.assertEqual(status[":active-request-id"], accepted[":id"])
                        self.assertEqual(lookup("lost")[":request-state"], ":running")
                        send(observer, accepted)
                        self.assertEqual(receive(observer)[":request-state"], ":running")
                        conflict = dict(accepted, **{":form": '(incf *mcp-ledger-counter*)'})
                        send(observer, conflict)
                        self.assertEqual(plist(receive(observer)[":error"])[":code"],
                                         ":request-id-conflict")
                    # The accepted connection is now gone. A new connection can
                    # recover the same outcome without resending execution.
                    deadline = time.monotonic() + 5
                    while True:
                        recovered = lookup("lost")
                        if recovered[":request-state"] != ":running":
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    self.assertEqual(recovered[":request-state"], ":completed")
                    terminal = plist(recovered[":result"])
                    self.assertEqual(terminal[":id"], accepted[":id"])
                    self.assertEqual(terminal[":values"], ["1"])
                    self.assertEqual(terminal[":stdout"], "before")
                    with client(host, port) as (fresh, _):
                        send(fresh, request(hello, ":request-status", prefix + "fresh",
                                            target_id=accepted[":id"]))
                        self.assertEqual(receive(fresh)[":result"], recovered[":result"])
                        send(fresh, accepted)
                        self.assertEqual(receive(fresh), terminal)
                        send(fresh, conflict)
                        self.assertEqual(plist(receive(fresh)[":error"])[":code"],
                                         ":request-id-conflict")
                    failed_request = request(hello, ":eval", prefix + "failed", form=
                        '(progn (incf *mcp-ledger-counter*) (write-string "prior") '
                        '(error "ledger failure"))')
                    send(observer, failed_request)
                    failed = receive(observer)
                    self.assertEqual(failed[":request-state"], ":failed")
                    self.assertIsNone(failed[":ok"])
                    self.assertEqual(failed[":stdout"], "prior")
                    self.assertEqual(plist(lookup("failed")[":result"]), failed)
                    send(observer, failed_request)
                    self.assertEqual(receive(observer), failed)
                    count = exchange(":eval", "counter", form='*mcp-ledger-counter*')
                    self.assertEqual(count[":values"], ["2"])
                    before_reload = lookup("lost")
                    self.assertEqual(reload_sources(control), "codec loaded")
                    self.assertEqual(lookup("lost"), before_reload)
                    self.assertEqual(legacy(control, '(princ-to-string *mcp-worker*)'), worker)
                    # Reduce only this fixture service's limits to exercise both
                    # eviction paths quickly; production defaults stay fixed.
                    legacy(control, '(setf (getf *mcp-service* :history-limit) 3)')
                    for i in range(4):
                        exchange(":eval", f"count-{i}", form=str(i))
                    status = exchange(":status", "count-status")
                    self.assertEqual(status[":history-limit"], 3)
                    self.assertEqual(status[":history-count"], 3)
                    self.assertGreater(status[":history-evictions"], 0)
                    self.assertEqual(lookup("count-0")[":request-state"], ":unknown")
                    self.assertNotIn(":result", lookup("count-0"))
                    self.assertEqual(lookup("count-1")[":request-state"], ":completed")
                    previous_evictions = status[":history-evictions"]
                    legacy(control, '(setf (getf *mcp-service* :history-byte-limit) 1500)')
                    exchange(":eval", "oversized", form=" " * 3000 + "42")
                    status = exchange(":status", "byte-status")
                    self.assertEqual(status[":history-byte-limit"], 1500)
                    self.assertEqual(status[":history-count"], 0)
                    self.assertEqual(status[":history-bytes"], 0)
                    self.assertGreater(status[":history-evictions"], previous_evictions)
                    self.assertEqual(lookup("oversized")[":request-state"], ":unknown")
                    retained = exchange(":eval", "replacement", form="42")
                    self.assertEqual(plist(lookup("replacement")[":result"]), retained)
                # A trusted legacy management job can replace its own service.
                # Its connection drains and closes, while the worker survives.
                legacy(control, '(mcp-start-service :protocol :v1 :replace t)')
                replaced = True
                with client(host, port) as (fresh, current):
                    self.assertEqual(current[":boot-id"], hello[":boot-id"])
                    self.assertNotEqual(current[":service-id"], hello[":service-id"])
                    self.assertEqual(current[":history-count"], 0)
                    self.assertEqual(current[":history-evictions"], 0)
                    self.assertEqual(current[":history-limit"], 32)
                    self.assertEqual(current[":history-byte-limit"], 4194304)
                    send(fresh, request(hello, ":request-status", prefix + "stale",
                                        target_id=prefix + "replacement"))
                    self.assertEqual(plist(receive(fresh)[":error"])[":code"],
                                     ":stale-generation")
                    send(fresh, request(current, ":request-status", prefix + "new",
                                        target_id=prefix + "replacement"))
                    self.assertEqual(receive(fresh)[":request-state"], ":unknown")
            finally:
                if replaced:
                    with client(host, port) as (fresh, current):
                        send(fresh, request(current, ":eval", prefix + "restore", form=
                            '(setf (getf *mcp-service* :protocol) :legacy '
                            '*mcp-protocol* :legacy)'))
                        self.assertTrue(receive(fresh)[":ok"])
                else:
                    legacy(control, '(setf (getf *mcp-service* :protocol) :legacy '
                                    '*mcp-protocol* :legacy '
                                    '(getf *mcp-service* :history-limit) 32 '
                                    '(getf *mcp-service* :history-byte-limit) 4194304)')
        finally:
            control.close()
        with socket.create_connection((host, port), timeout=5) as restored:
            restored.settimeout(5)
            self.assertEqual(legacy(restored, '(+ 20 22)'), "42")
            self.assertEqual(legacy(restored, '(princ-to-string *mcp-worker*)'), worker)
            self.assertEqual(legacy(restored, '(count :mcp-server net:*services-enabled*)'), "1")
            self.assertEqual(legacy(restored, '*mcp-ledger-counter*'), "2")
        print("Live ledger: retained success/failure, no duplicate execution, "
              "bounded history, eviction, and generation replacement passed.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
