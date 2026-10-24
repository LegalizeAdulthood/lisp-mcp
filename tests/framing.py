"""Framing acceptance checks; add --live-genera for the real listener."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import time
import unittest

from configuration import ROOT, call, receive_exact, run_bridge, sbcl_command


LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")


def frame(payload, prefix=b"\n"):
    return prefix + str(len(payload)).encode("ascii") + b" " + payload


@contextmanager
def peer(response, protocol="genera", fragment=False, truncate=False):
    with socket.socket() as listener, ThreadPoolExecutor(1) as executor:
        listener.bind(("127.0.0.2", 0))
        listener.listen(1)
        listener.settimeout(10)

        def serve():
            with listener.accept()[0] as stream:
                stream.settimeout(5)
                if protocol == "genera":
                    header = b""
                    while not header.endswith(b" "):
                        header += receive_exact(stream, 1)
                    receive_exact(stream, int(header))
                else:
                    receive_exact(stream, int(receive_exact(stream, 6), 16))
                chunks = (bytes([byte]) for byte in response) if fragment else [response]
                for chunk in chunks:
                    stream.sendall(chunk)
                    if fragment:
                        time.sleep(0.002)
                if truncate:
                    stream.shutdown(socket.SHUT_WR)
                # Keep the peer open: malformed frames must fail without EOF
                # or waiting for the bridge's longer evaluation timeout.
                stream.settimeout(3)
                if stream.recv(1) != b"":
                    raise AssertionError("Bridge did not close its failed connection")

        completed = executor.submit(serve)
        yield listener.getsockname()
        completed.result(timeout=10)


def text_result(result):
    import json
    if result.returncode:
        raise AssertionError(result.stderr)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    return replies[0]["result"]["content"][0]["text"]


def bridge_peer(response, protocol="genera", **kwargs):
    with peer(response, protocol, **kwargs) as (host, port):
        settings = {"GENERA_TIMEOUT": "8"}
        if protocol == "genera":
            settings.update(GENERA_HOST=host, GENERA_PORT=str(port))
            tool = "eval_genera"
        else:
            settings.update(SWANK_HOST=host, SWANK_PORT=str(port))
            tool = "eval_swank"
        return text_result(run_bridge(settings, [call(tool, "(+ 1 2)")]))


def read_live_field(stream):
    byte = receive_exact(stream, 1)
    if byte == b"\r":
        byte = receive_exact(stream, 1)
        if byte == b"\n":
            byte = receive_exact(stream, 1)
    elif byte in [b"\n", b"\x8d"]:
        byte = receive_exact(stream, 1)
    header = b""
    while byte != b" ":
        if not b"0" <= byte <= b"9" or len(header) == 10:
            raise AssertionError("Malformed live response header")
        header += byte
        byte = receive_exact(stream, 1)
    length = int(header)
    if length > 1048576:
        raise AssertionError("Oversized live response")
    return receive_exact(stream, length).decode("ascii").replace("\r\n", "\n").replace("\r", "\n")


class FramingChecks(unittest.TestCase):
    def test_portable_framing_definitions(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "framing.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Framing unit checks:", result.stdout)

    def test_fragmented_genera_fields_and_newlines(self):
        response = (frame(b"fragment\nend", b"\r\n")
                    + frame(b"a\r\nb\rc") + frame(b"", b"\r"))
        self.assertEqual(bridge_peer(response, fragment=True),
                         "fragment\nend\n\n[stdout]\na\nb\nc")

    def test_malformed_genera_headers_fail_before_peer_eof(self):
        for response in [b"junk1 a", b" 1 a", b"+1 a", b"-1 a", b"1:a",
                         b"1\na", b"\n\n1 a", b"\t1 a", b"00000000000 ",
                         b"1048577 ", b"9999999999 "]:
            with self.subTest(response=response):
                self.assertTrue(bridge_peer(response).startswith("Protocol error:"))

    def test_truncated_genera_frames(self):
        for response in [b"", b"\n", b"1", b"\r\n3 ab", b"1 a\n0 "]:
            with self.subTest(response=response):
                self.assertIn("Protocol error: Truncated",
                              bridge_peer(response, truncate=True))

    def test_unsupported_requests_do_not_connect(self):
        for expression in ["(+ 1 2)\x00", ";hidden \u00e9", "a" * 1048577]:
            with self.subTest(length=len(expression)):
                with socket.socket() as listener:
                    listener.bind(("127.0.0.2", 0))
                    listener.listen(1)
                    listener.settimeout(0.1)
                    result = text_result(run_bridge(
                        {"GENERA_HOST": "127.0.0.2",
                         "GENERA_PORT": str(listener.getsockname()[1])},
                        [call("eval_genera", expression)],
                    ))
                    self.assertTrue(result.startswith("Protocol error:"), result)
                    with self.assertRaises(socket.timeout):
                        listener.accept()

    def test_fragmented_swank_fields(self):
        payload = b'(:return (:ok ("" "3")) 1)'
        response = f"{len(payload):06x}".encode() + payload
        self.assertEqual(bridge_peer(response, "swank", fragment=True), "3")

    def test_malformed_and_truncated_swank_fields(self):
        for response, truncated in [(b"00000g", False), (b"100001", False),
                                    (b"000", True), (b"000003ab", True)]:
            with self.subTest(response=response):
                self.assertTrue(bridge_peer(response, "swank", truncate=truncated)
                                .startswith("Protocol error:"))

    @unittest.skipUnless(LIVE, "requires --live-genera and the updated listener")
    def test_live_genera_ascii_and_newlines(self):
        host = os.environ["GENERA_HOST"]
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as stream:
            stream.settimeout(5)
            for expression, expected in [
                ('(progn (format t "alpha~%beta~%") (+ 20 22))',
                 ["42", "alpha\nbeta\n", ""]),
                ('(length "alpha\nbeta")', ["10", "", ""]),
                ('(+ 1 2)', ["3", "", ""]),
            ]:
                wire = frame(expression.encode("ascii"), b"")
                for byte in wire:
                    stream.sendall(bytes([byte]))
                    time.sleep(0.002)
                self.assertEqual([read_live_field(stream) for _ in range(3)], expected)
        for bad_frame in [b"junk1 a", b"1048577 ", b"1 \x00", b"3 ab"]:
            with socket.create_connection((host, port), timeout=5) as stream:
                stream.settimeout(3)
                stream.sendall(bad_frame)
                stream.shutdown(socket.SHUT_WR)
                try:
                    self.assertEqual(stream.recv(1), b"")
                except ConnectionResetError:
                    pass
        self.assertEqual(text_result(run_bridge(
            {"GENERA_HOST": host, "GENERA_PORT": str(port)},
            [call("eval_genera", "(+ 20 22)")],
        )), "42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
