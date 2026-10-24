"""Envelope acceptance checks; --live-genera reloads the paired Lisp sources."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import unittest

from configuration import ROOT, sbcl_command


LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from framing import frame, peer, read_live_field

FIXTURES = json.loads((Path(__file__).with_name("protocol-fixtures.json"))
                     .read_text(encoding="ascii"))
CODEC_REQUEST = next(f["wire"] for f in FIXTURES
                     if f["valid"] and f["direction"] == "request"
                     and ":op :eval" in f["wire"])


def lisp_string(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def run_lisp(expression, binary=False):
    return subprocess.run(
        sbcl_command() + ["--load", str(ROOT / "lisp-mcp.lisp"),
                          "--eval", expression],
        cwd=ROOT, capture_output=True, text=not binary, timeout=15,
    )


def exchange(host, port, request, binary=False):
    return run_lisp(
        '(handler-case (write-string (lisp-mcp::mcp-v1-encode '
        '(lisp-mcp::genera-send-envelope '
        f'(lisp-mcp::mcp-v1-decode {lisp_string(request)} :request) '
        f':host {lisp_string(host)} :port {port}) :response)) '
        '(error () (write-string "rejected")))', binary=binary,
    )


def legacy(stream, expression):
    stream.sendall(frame(expression.encode("ascii"), b""))
    result, stdout, stderr = [read_live_field(stream) for _ in range(3)]
    if stderr:
        raise AssertionError(f"Genera error: {stderr}; result: {result}; stdout: {stdout}")
    return result


def reload_sources(stream):
    source = "\n".join(
        (ROOT / name).read_text(encoding="ascii")
        for name in ("genera-protocol.lisp", "eval-server.lisp")
    )
    # Genera's source reader ends line comments at native Return (ASCII CR).
    # Read every trusted source form before evaluating any of them. The old
    # legacy evaluator otherwise ignores trailing forms after the first one.
    program = ('(progn (mapcar #\'eval (with-input-from-string (source '
               + lisp_string(source.replace("\n", "\r"))
               + ') (loop for form = (read source nil :eof) '
               'until (eq form :eof) collect form))) "codec loaded")')
    return legacy(stream, program)


def wire_string(reply, key):
    match = re.search(r':' + re.escape(key) + r' "((?:\\["\\]|[^"\\])*)"', reply)
    if not match:
        raise AssertionError(f"Missing string {key}: {reply}")
    return re.sub(r'\\(["\\])', r'\1', match.group(1))


def wire_exchange(stream, request):
    stream.sendall(frame(request.encode("ascii"), b""))
    return read_live_field(stream)


def status_request(hello, request_id="status", boot=None, service=None):
    return (f'(:version 1 :id {lisp_string(request_id)} :op :status '
            f':boot-id {lisp_string(boot or wire_string(hello, "boot-id"))} '
            f':service-id {lisp_string(service or wire_string(hello, "service-id"))})')


class ProtocolChecks(unittest.TestCase):
    def test_portable_codec_definitions(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "protocol.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Protocol unit checks:", result.stdout)

    def test_fragmented_response_preserves_literal_newlines(self):
        response = next(f["wire"] for f in FIXTURES
                        if f["valid"] and f["direction"] == "response"
                        and ':stdout "a' in f["wire"])
        with peer(frame(response.encode("ascii")), fragment=True) as (host, port):
            result = exchange(host, port, CODEC_REQUEST, binary=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, response.encode("ascii"))

    def test_invalid_responses_never_fall_back_to_legacy(self):
        for payload in [b"3", b'(:version 2 :id "x" :ok t)',
                        b'(:version 1 :id "x" :ok t :ok t)',
                        b'(:version 1 :id "x" :ok t) nil']:
            with self.subTest(payload=payload):
                with peer(frame(payload)) as (host, port):
                    result = exchange(host, port, CODEC_REQUEST)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "rejected")

    def test_envelope_response_rejects_unsupported_bytes(self):
        for byte in [0, 127, 141, 255]:
            payload = b'(:version 1 :id "x" :ok t :stdout "' + bytes([byte]) + b'")'
            with self.subTest(byte=byte):
                with peer(frame(payload)) as (host, port):
                    result = exchange(host, port, CODEC_REQUEST)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "rejected")

    def test_invalid_request_is_rejected_before_connection(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            result = run_lisp(
                '(handler-case (lisp-mcp::genera-send-envelope '
                "'(:version 2 :id \"x\" :op :hello) "
                f':host "127.0.0.2" :port {listener.getsockname()[1]}) '
                '(error () (write-string "rejected")))'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "rejected")
            with self.assertRaises(socket.timeout):
                listener.accept()

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_genera_codec_and_single_response(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as control:
            control.settimeout(30)
            self.assertEqual(reload_sources(control), "codec loaded")
            legacy(control, "(setq *mcp-v1-test-sentinel* nil)")
            # Keep this legacy connection open to restore the selected mode.
            # New connections explicitly select :v1; there is no wire autodetection.
            try:
                for fixture in FIXTURES:
                    codes = " ".join(str(ord(character)) for character in fixture["wire"])
                    direction = ":" + fixture["direction"]
                    expression = (
                        '(condition-case (e) '
                        '(format nil "~{~D~^,~}" (map \'list #\'char-code '
                        '(mcp-v1-encode (mcp-v1-decode '
                        f"(map 'string #'code-char '({codes})) {direction}) "
                        f'{direction}))) (error "rejected"))'
                    )
                    with self.subTest(fixture=fixture):
                        expected = (",".join(str(ord(c)) for c in fixture["wire"])
                                    if fixture["valid"] else "rejected")
                        self.assertEqual(legacy(control, expression), expected)
                # Keep this test's trusted legacy control connection in the same
                # service; the lifecycle suite tests replacement through the API.
                legacy(control, '(setf (getf *mcp-service* :protocol) :v1 '
                                '*mcp-protocol* :v1)')
                # Eval semantics have a dedicated suite. Keep the reader-macro
                # form fixture confined to codec parity, where it is only data.
                requests = [f["wire"] for f in FIXTURES
                            if f["valid"] and f["direction"] == "request"
                            and ":op :eval" not in f["wire"]]
                for request in requests:
                    with socket.create_connection((host, port), timeout=5) as stream:
                        stream.settimeout(3)
                        hello = wire_exchange(stream, '(:version 1 :id "probe" :op :hello)')
                        request = request.replace(':boot-id "boot"',
                            ':boot-id ' + lisp_string(wire_string(hello, "boot-id")))
                        request = request.replace(':service-id "service"',
                            ':service-id ' + lisp_string(wire_string(hello, "service-id")))
                        # Reuse the connection. Each request produces exactly one field.
                        reply = wire_exchange(stream, request)
                        self.assertIn(":ok t", reply)
                        if ":op :request-status" in request:
                            self.assertIn(":request-state :unknown", reply)
                        self.assertEqual(wire_exchange(stream, request), reply)
                        stream.settimeout(0.1)
                        with self.assertRaises(socket.timeout):
                            stream.recv(1)
                result = exchange(host, port, FIXTURES[0]["wire"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(":ok t", result.stdout)
                self.assertIn(":features (:status :request-status :eval)", result.stdout)
                bad_requests = [f["wire"] for f in FIXTURES
                                if not f["valid"] and f["direction"] == "request"]
                bad_requests.append("(setq *mcp-v1-test-sentinel* t)")
                for request in bad_requests:
                    with socket.create_connection((host, port), timeout=5) as stream:
                        stream.settimeout(3)
                        stream.sendall(frame(request.encode("latin-1"), b""))
                        try:
                            self.assertEqual(stream.recv(1), b"")
                        except ConnectionResetError:
                            pass
                self.assertEqual(legacy(control, "*mcp-v1-test-sentinel*"), "NIL")
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy '
                                '*mcp-protocol* :legacy)')
            self.assertEqual(legacy(control, "(+ 20 22)"), "42")
        print(f"Live codec fixtures: {len(FIXTURES)} matched; legacy mode restored.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
