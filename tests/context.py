"""Listener context reset; --live-v1 also checks the running Genera world."""
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid

LIVE = "--live-v1" in sys.argv
if LIVE:
    sys.argv.remove("--live-v1")

from configuration import ROOT, SETTINGS, sbcl_command
from deadlines import identity
from evaluation import receive_request
from interrupt import call
from session import reply, response
from stdio import bridge, genera_settings, peer, structured

TOOL = "genera_reset_listener_context"


def advertise(hello):
    hello[":features"].append(":reset-listener-context")


def generation(status):
    return {key: status[key] for key in ("boot_id", "service_id")}


class ContextChecks(unittest.TestCase):
    def test_portable_admission_identity_and_history(self):
        result = subprocess.run(sbcl_command() + ["--load", str(ROOT / "tests/context.lisp")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Context unit checks: 17 passed.", result.stdout)

    def test_reset_schema_and_generation_reject_before_wire(self):
        def serve(stream, hello):
            work = receive_request(stream)
            self.assertEqual(work[":op"], ":reset-listener-context")
            self.assertNotIn(":form", work)
            reply(stream, response(work, '\"USER\"'))
            self.assertEqual(stream.recv(1), b"")

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            target = generation(mcp.ready())
            for changes in ({"boot_id": ""}, {"service_id": None}, {"form": "bad"},
                            {"timeout_ms": 0}, {"timeout_ms": "bad"}):
                mcp.call(TOOL, "invalid", **dict(target, **changes))
                self.assertEqual(mcp.receive()["error"]["code"], -32602)
            self.assertEqual(call(mcp, TOOL, **dict(target, service_id="old"))["error"]["code"],
                             "generation-changed")
            result = call(mcp, TOOL, **target)
            self.assertEqual(result["request_state"], "completed")
            self.assertEqual(result["values"], ['"USER"'])
            self.assertEqual(result["timeout_enforced_by"], "host")

    def test_old_listener_refuses_without_sending_reset(self):
        def serve(stream, hello):
            self.assertEqual(stream.recv(1), b"")

        with peer(serve) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            target = generation(mcp.ready())
            self.assertEqual(call(mcp, TOOL, **target)["error"]["code"], "unsupported")

    def test_active_and_uncertain_execution_cannot_be_cleared(self):
        release = threading.Event()

        def serve(stream, hello):
            work = receive_request(stream)
            self.assertEqual(work[":op"], ":eval")
            self.assertTrue(release.wait(5))
            reply(stream, response(work))
            reset = receive_request(stream)
            self.assertEqual(reset[":op"], ":reset-listener-context")
            reply(stream, response(reset))

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            target = generation(mcp.ready())
            mcp.call("genera_eval", "eval", form="slow", timeout_ms=200)
            mcp.call(TOOL, "busy", **target)
            packets = {p["id"]: structured(p) for p in (mcp.receive(), mcp.receive())}
            self.assertEqual(packets["busy"]["error"]["code"], "busy")
            self.assertEqual(packets["eval"]["request_state"], "unresolved")
            self.assertEqual(call(mcp, TOOL, **target)["error"]["code"], "busy")
            current = call(mcp, "genera_session_status")
            self.assertEqual(current["active_request_id"], packets["eval"]["request_id"])
            release.set()
            end = time.monotonic() + 3
            while call(mcp, "genera_session_status").get("active_request_id"):
                self.assertLess(time.monotonic(), end)
                time.sleep(.01)
            self.assertTrue(call(mcp, TOOL, **target)["ok"])

    def test_lost_reset_result_is_retained_without_replay(self):
        release = threading.Event()

        def serve(stream, hello):
            reset = receive_request(stream)
            self.assertEqual(reset[":op"], ":reset-listener-context")
            query = receive_request(stream)
            self.assertEqual(query[":target-id"], reset[":id"])
            terminal = response(reset, '\"USER\"')
            reply(stream, dict(response(query), **{":request-state": ":completed", ":result": terminal}))
            self.assertTrue(release.wait(5))
            reply(stream, terminal)
            later = receive_request(stream)
            self.assertEqual(later[":op"], ":eval")
            reply(stream, response(later, "7"))

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            target = generation(mcp.ready())
            result = call(mcp, TOOL, **target, timeout_ms=80)
            self.assertEqual(result["error"]["code"], "timeout")
            self.assertEqual(result["request_state"], "unresolved")
            self.assertEqual(call(mcp, TOOL, **target)["error"]["code"], "busy")
            found = call(mcp, "genera_request_status", **identity(result))
            self.assertEqual(found["result"]["values"], ['"USER"'])
            release.set()
            self.assertEqual(call(mcp, "genera_eval", form="7")["values"], ["7"])

    def test_incomplete_reset_success_is_protocol_failure(self):
        def serve(stream, hello):
            work = receive_request(stream)
            invalid = response(work)
            del invalid[":values"]
            del invalid[":value-count"]
            reply(stream, invalid)

        with peer(serve, advertise) as address, bridge(genera_settings(address)) as mcp:
            mcp.initialize()
            target = generation(mcp.ready())
            result = call(mcp, TOOL, **target)
            self.assertEqual(result["error"]["code"], "protocol")
            self.assertEqual(result["request_state"], "unresolved")

    @unittest.skipUnless(LIVE, "requires --live-v1 and the updated listener")
    def test_live_package_world_state_and_history_survive(self):
        settings = {key: os.environ[key] for key in SETTINGS if key in os.environ}
        settings.update(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1")
        marker = "mcp-context-" + uuid.uuid4().hex
        with bridge(settings) as mcp, bridge(settings) as other:
            mcp.initialize()
            target = generation(mcp.ready())
            other.initialize()
            other.ready()
            def evaluate(form):
                return call(mcp, "genera_eval", form=form, package="COMMON-LISP-USER")
            setup = evaluate(f'(progn (setq *{marker}* 37 *{marker}-default* *mcp-default-package*) '
                             f'(setq *{marker}-current* *mcp-eval-package*) '
                             f'(defun {marker} () *{marker}*) '
                             '(package-name *mcp-default-package*))')
            expected = setup["values"][0].strip('"')
            try:
                evaluate('(setq *mcp-eval-package* (find-package "KEYWORD"))')
                self.assertEqual(evaluate('(package-name *mcp-eval-package*)')["values"], ['"KEYWORD"'])
                # A second client reaches the listener's global busy guard.
                other.call("genera_eval", "busy", form="(progn (sleep 1) 42)", package="COMMON-LISP-USER")
                end = time.monotonic() + 2
                while not call(mcp, "genera_session_status", refresh=True)["busy"]:
                    self.assertLess(time.monotonic(), end)
                blocked = call(mcp, TOOL, **target)
                self.assertEqual(blocked["error"]["code"], "busy", blocked)
                self.assertEqual(structured(other.receive())["values"], ["42"])
                before = call(mcp, "genera_session_status", refresh=True)
                result = call(mcp, TOOL, **target)
                self.assertEqual(result["package"], expected)
                self.assertEqual(result["values"], ['"' + expected + '"'])
                self.assertEqual(result["stdout"], "")
                self.assertEqual(result["warnings"], [])
                self.assertEqual(evaluate(f"(list *{marker}* ({marker}))")["values"], ["(37 37)"])
                retained = call(mcp, "genera_request_status", **identity(setup))
                self.assertEqual(retained["result"]["values"], setup["values"])
                after = call(mcp, "genera_session_status", refresh=True)
                self.assertEqual(generation(after), target)
                self.assertTrue({item["request_id"] for item in before["history"]}.issubset(
                                {item["request_id"] for item in after["history"]}))
                # A host deadline cannot turn still-running native work into reset permission.
                mcp.call("genera_eval", "uncertain", form="(progn (sleep .6) 42)",
                         package="COMMON-LISP-USER", timeout_ms=80)
                uncertain = structured(mcp.receive())
                self.assertEqual(uncertain["request_state"], "unresolved")
                self.assertEqual(call(mcp, TOOL, **target)["error"]["code"], "busy")
                end = time.monotonic() + 5
                while True:
                    outcome = call(mcp, "genera_request_status", **identity(uncertain))
                    if outcome["request_state"] == "completed":
                        break
                    self.assertLess(time.monotonic(), end, outcome)
                    time.sleep(.05)
                # A configured default other than USER is honored too.
                evaluate('(setq *mcp-default-package* (find-package "KEYWORD"))')
                custom = call(mcp, TOOL, **target)
                self.assertEqual(custom["package"], "KEYWORD")
                self.assertEqual(evaluate(f"({marker})")["values"], ["37"])
            finally:
                restored = evaluate(f'(progn (setq *mcp-default-package* *{marker}-default* '
                                    f'*mcp-eval-package* *{marker}-current*) (package-name *mcp-eval-package*))')
                self.assertTrue(restored["ok"], restored)


if __name__ == "__main__":
    unittest.main()
