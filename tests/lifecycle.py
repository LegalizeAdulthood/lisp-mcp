"""Opt-in paired MCP/v1 checks; leaves Genera running and never saves a world."""

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import socket
import time

from concurrency import send
from configuration import ROOT, SETTINGS, request
from deadlines import connections, identity
from evaluation import receive_request
from framing import frame, read_live_field
from stdio import Bridge, structured


def call(mcp, name, **arguments):
    mcp.call(name, "check", **arguments)
    packet = mcp.receive(timeout=15)
    assert packet["id"] == "check", packet
    return structured(packet)


def successful(mcp, form):
    result = call(mcp, "genera_eval", form=form)
    assert result.get("request_state") == "completed", result
    assert "error" not in result, result
    return result


def wait_status(mcp, predicate, timeout=15, **arguments):
    deadline = time.monotonic() + timeout
    while True:
        status = call(mcp, "genera_session_status", **arguments)
        if predicate(status):
            return status
        assert time.monotonic() < deadline, status
        time.sleep(.03)


def entry_command():
    # Keep the production entry point and $0; tolerate a Windows checkout.
    entry = ROOT / "lisp-mcp.sh"
    return ["/bin/sh", "-c", entry.read_text(encoding="ascii"), str(entry)]


def run(settings, cold=False, previous=None, replace_service=False):
    assert settings.get("LISP_MCP_PROFILE") == "genera"
    assert settings.get("GENERA_PROTOCOL") == "v1"
    target = (settings["GENERA_HOST"], int(settings.get("GENERA_PORT", "8888")))
    if cold:
        assert settings.get("GENERA_RUNTIME_MODE") == "local"
        assert settings.get("GENERA_AUTOSTART") == "true"
        try:
            with socket.create_connection(target, timeout=1):
                raise AssertionError("Cold check requires an absent endpoint")
        except OSError:
            pass
    report = {}
    marker = "*mcp-lifecycle-" + str(time.time_ns()) + "*"
    command = entry_command()
    with ExitStack() as stack:
        def client(options=settings):
            return stack.enter_context(bridge_command(options, command))

        first = client()
        started = time.monotonic()
        initialized = first.initialize()
        report["initialize_seconds"] = time.monotonic() - started
        assert "result" in initialized and report["initialize_seconds"] < 2, initialized
        first.call("genera_ensure_session", "ensure")
        first.call("genera_session_status", "cached")
        first.send(request("ping", "ping"))
        packets = {}
        control_started = time.monotonic()
        while not {"cached", "ping"}.issubset(packets):
            packet = first.receive(timeout=2)
            packets[packet["id"]] = packet
        report["control_seconds"] = time.monotonic() - control_started
        assert report["control_seconds"] < 1, packets
        if "ensure" not in packets:
            packets["ensure"] = first.receive(timeout=int(settings.get("GENERA_BOOT_TIMEOUT", "300")) + 5)
        ready = structured(packets["ensure"])
        assert ready["state"] == "ready" and ready["connected"], ready
        report["ready_seconds"] = time.monotonic() - started
        report["initial"] = ready
        if cold:
            assert ready["startup"]["launch_attempted"], ready
        if previous:
            assert ready["boot_id"] != previous["initial"]["boot_id"], ready
            assert ready["service_id"] != previous["initial"]["service_id"], ready
            assert successful(first, "(boundp '" + previous["marker"] + ")")["values"] == ["NIL"]
            report["new_world"] = True
        assert successful(first, "(setq " + marker + " 0)")["values"] == ["0"]
        for value in (1, 2, 3):
            assert successful(first, "(incf " + marker + ")")["values"] == [str(value)]
        report["marker"] = marker

        second = client()
        second.initialize()
        other = second.ready()
        assert (other["boot_id"], other["service_id"]) == (ready["boot_id"], ready["service_id"]), other
        assert not other.get("startup", {}).get("launch_attempted"), other
        first.call("genera_eval", "slow", form="(progn (incf " + marker + ") (sleep 2) " + marker + ")")
        wait_status(second, lambda s: s.get("busy"), refresh=True)
        rejected = call(second, "genera_eval", form="(incf " + marker + ")")
        assert rejected["error"]["code"] == "busy", rejected
        assert structured(first.receive())["values"] == ["4"]
        assert successful(second, marker)["values"] == ["4"]
        first.close()
        second.close()
        reopened = client()
        reopened.initialize()
        reopened_ready = reopened.ready()
        assert reopened_ready["boot_id"] == ready["boot_id"], reopened_ready
        assert successful(reopened, marker)["values"] == ["4"]
        report["warm_attach_repeated_eval_serialization_reopen"] = True

        forms = []

        def proxy(listener):
            # Drop one actual terminal response, then relay reconciliation.
            for connection in range(2):
                with listener.accept()[0] as front, socket.create_connection(target, timeout=5) as backend:
                    front.settimeout(15)
                    backend.settimeout(15)
                    while True:
                        try:
                            work = receive_request(front)
                        except (EOFError, ConnectionResetError):
                            break
                        if work[":op"] == ":eval":
                            forms.append(work[":form"])
                        send(backend, work)
                        reply = read_live_field(backend, normalize=False)
                        if connection == 0 and work[":op"] == ":eval":
                            break
                        front.sendall(frame(reply.encode("ascii")))

        # A proxy is attach-only: it must never become a local launcher.
        with connections(proxy) as address:
            proxy_settings = {key: settings[key] for key in
                              ("GENERA_TOKEN", "GENERA_EXPECTED_WORLD") if key in settings}
            proxy_settings.update(LISP_MCP_PROFILE="genera", GENERA_PROTOCOL="v1",
                                  GENERA_HOST=address[0], GENERA_PORT=str(address[1]))
            with bridge_command(proxy_settings, command) as lost:
                lost.initialize()
                lost.ready()
                failure = call(lost, "genera_eval", form="(progn (incf " + marker + ") (sleep .3) " + marker + ")")
                assert failure["error"]["code"] == "transport", failure
                assert failure["request_state"] == "unresolved", failure
                assert failure["session"]["state"] == "outcome-unknown", failure
                wait_status(lost, lambda s: s["state"] == "ready" and s["reconnect_attempts"] >= 1)
                retained = call(lost, "genera_request_status", **identity(failure))
                assert retained["result"]["values"] == ["5"], retained
                assert successful(lost, marker)["values"] == ["5"]
                report["lost_reply"] = dict(identity(failure), retained=retained)
        assert len(forms) == 2, forms
        assert successful(reopened, marker)["values"] == ["5"]

        if replace_service:
            # Schedule management after this accepted eval has released its
            # slot. Never print a process or a circular listener/job object.
            form = '''(progn
 (scl:process-run-function "MCP lifecycle replacement"
   (lambda ()
     (scl:process-wait "MCP lifecycle idle"
       (lambda () (not (mcp-execution-busy-p))))
     (mcp-start-service :protocol :v1 :replace t)))
 :scheduled)'''
            management = call(reopened, "genera_eval", form=form)
            if "error" in management:
                assert management["error"]["code"] == "transport", management
                assert management["request_state"] == "unresolved", management
                assert management["session"]["state"] == "outcome-unknown", management
            else:
                assert management["values"] == [":SCHEDULED"], management
            changed = wait_status(reopened, lambda s: s.get("service_id") != ready["service_id"]
                                  and s.get("connected"), refresh=True)
            assert changed["boot_id"] == ready["boot_id"], changed
            change = changed["generation_change"]
            assert change["history_lost"] and not change["world_state_lost"], changed
            if "error" in management:
                assert changed["state"] == "outcome-unknown", changed
                recovered = call(reopened, "genera_recover_session", **identity(management),
                                 expected_boot_id=changed["boot_id"],
                                 expected_service_id=changed["service_id"], acknowledge_unknown=True)
                assert recovered["acknowledged"] and recovered["request_state"] == "unknown", recovered
                report["replacement_unknown_recovery"] = recovered
            assert successful(reopened, marker)["values"] == ["5"]
            report["service_replacement"] = changed
    return report


@contextmanager
def bridge_command(settings, command):
    # Reuse the standard cleanup contract with the production shell entry.
    mcp = Bridge(settings, command=command)
    try:
        yield mcp
    finally:
        try:
            mcp.close()
        except BaseException:
            if mcp.process.poll() is None:
                mcp.process.kill()
            mcp.process.wait()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-v1", action="store_true", required=True)
    parser.add_argument("--cold", action="store_true", help="require one real autostart launch")
    parser.add_argument("--previous-report", type=Path, help="verify changed boot and lost old marker")
    parser.add_argument("--replace-service", action="store_true", help="replace the idle listener; clears history")
    parser.add_argument("--report", type=Path, help="write JSON to a new report file")
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error("Report destination already exists")
    previous = json.loads(args.previous_report.read_text()) if args.previous_report else None
    result = run({key: os.environ[key] for key in SETTINGS if key in os.environ},
                 args.cold, previous, args.replace_service)
    output = json.dumps(result, indent=2)
    if args.report:
        with args.report.open("x", encoding="ascii") as stream:
            stream.write(output + "\n")
    print(output)
