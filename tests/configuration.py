"""Host acceptance checks. Run with python3 tests/configuration.py."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = (
    "LISP_MCP_PROFILE", "GENERA_PROTOCOL", "GENERA_HOST", "GENERA_PORT",
    "GENERA_TIMEOUT", "SWANK_HOST", "SWANK_PORT", "SWANK_PACKAGE",
    "GENERA_CONNECT_TIMEOUT", "GENERA_BOOT_TIMEOUT",
    "GENERA_TOKEN", "GENERA_EXPECTED_WORLD",
    "GENERA_RUNTIME_MODE", "GENERA_AUTOSTART", "GENERA_VLM_ROOT",
    "GENERA_VLM_EXECUTABLE", "GENERA_VLM_WORLD", "GENERA_VLM_PARTITION",
    "GENERA_VLM_DEBUGGER", "GENERA_VLM_USER", "GENERA_VLM_NETWORK",
    "GENERA_STATE_DIR", "GENERA_VLM_LOG",
)


def request(method, request_id=1, **params):
    return dict(jsonrpc="2.0", id=request_id, method=method, params=params)


def call(name, expression, request_id=1):
    return request("tools/call", request_id, name=name,
                   arguments=dict(expression=expression))


def sbcl_command():
    command = ["sbcl", "--noinform", "--non-interactive"]
    setup = os.environ.get("LISP_MCP_TEST_QUICKLISP")
    if setup:
        command += ["--load", setup]
    return command


def run_bridge(settings, messages):
    env = {key: value for key, value in os.environ.items()
           if key not in SETTINGS}
    env.update(settings)
    return subprocess.run(
        sbcl_command() + ["--load",
         str(ROOT / "lisp-mcp.lisp"), "--eval", "(lisp-mcp:main)"],
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True, capture_output=True, env=env, cwd=ROOT, timeout=15,
    )


def receive_exact(stream, length):
    data = b""
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            raise EOFError("Bridge closed before sending its request")
        data += chunk
    return data


@contextmanager
def fake_peer(protocol):
    # A non-default loopback address also checks that GENERA_HOST is used.
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
                        if len(header) > 20:
                            raise ValueError("Unexpected request header")
                    payload = receive_exact(stream, int(header))
                    stream.sendall(b"\n1 6\n0 \n0 ")
                else:
                    length = int(receive_exact(stream, 6), 16)
                    payload = receive_exact(stream, length)
                    reply = b'(:return (:ok ("fixture output" "42")) 1)'
                    stream.sendall(f"{len(reply):06x}".encode() + reply)
                return payload.decode("ascii")

        received = executor.submit(serve)
        yield listener.getsockname(), received
        received.result(timeout=10)


class ConfigurationChecks(unittest.TestCase):
    def responses(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def tools(self, result):
        return {tool["name"] for tool in
                self.responses(result)[0]["result"]["tools"]}

    def test_host_only_defaults(self):
        replies = self.responses(run_bridge({}, [
            request("initialize"), request("tools/list", 2),
            call("eval_host_cl", "(+ 2 3)", 3),
        ]))
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "lisp-mcp")
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]],
                         ["eval_host_cl"])
        self.assertEqual(replies[2]["result"]["content"][0]["text"], "5")

    def test_optional_tool_discovery(self):
        for settings, expected in [
            ({"SWANK_PORT": "4005"}, {"eval_host_cl", "eval_swank"}),
            ({"GENERA_HOST": "192.0.2.2"}, {"eval_host_cl", "eval_genera"}),
            ({"SWANK_PORT": "4005", "GENERA_HOST": "192.0.2.2"},
             {"eval_host_cl", "eval_swank", "eval_genera"}),
        ]:
            with self.subTest(settings=settings):
                self.assertEqual(self.tools(run_bridge(
                    settings, [request("tools/list")])), expected)

    def test_explicit_current_modes(self):
        self.assertEqual(self.tools(run_bridge(
            {"LISP_MCP_PROFILE": "generic", "GENERA_PROTOCOL": "legacy"},
            [request("tools/list")])), {"eval_host_cl"})

    def test_invalid_configuration_stops_before_dispatch(self):
        invalid = {
            "GENERA_PORT": ["", "0", "65536", "-1", "+8888", "8.5", "8x", " 8", "8 "],
            "SWANK_PORT": ["", "0", "65536", "4005x"],
            "GENERA_TIMEOUT": ["", "0", "-1", "1.5", "1s", " 1", "1\n"],
            "GENERA_HOST": ["", " ", "host name", "host\n"],
            "SWANK_HOST": ["", " ", "host name"],
            "SWANK_PACKAGE": ["", " ", "USER\n"],
            "LISP_MCP_PROFILE": ["", "genera", "unknown"],
            "GENERA_PROTOCOL": ["", "1", "v1", "auto"],
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    result = run_bridge({name: value}, [call(
                        "eval_host_cl", '(write-line "must not execute")')])
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Invalid configuration: " + name, result.stderr)
                    self.assertNotIn("server starting", result.stderr)
                    self.assertNotIn("Unhandled", result.stderr)

    def test_port_boundaries_and_positive_timeout(self):
        for port in ["1", "65535"]:
            with self.subTest(port=port):
                self.assertEqual(self.tools(run_bridge(
                    {"GENERA_PORT": port, "SWANK_PORT": port,
                     "GENERA_TIMEOUT": "1"}, [request("tools/list")])),
                    {"eval_host_cl", "eval_swank"})

    def test_discovery_does_not_connect(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            self.assertEqual(self.tools(run_bridge(
                {"GENERA_HOST": "127.0.0.2",
                 "GENERA_PORT": str(listener.getsockname()[1])},
                [request("tools/list")])), {"eval_host_cl", "eval_genera"})
            with self.assertRaises(socket.timeout):
                listener.accept()

    def test_configured_genera_endpoint(self):
        with fake_peer("genera") as ((host, port), received):
            replies = self.responses(run_bridge(
                {"GENERA_HOST": host, "GENERA_PORT": str(port)},
                [call("eval_genera", "(+ 1 2 3)")]))
            self.assertEqual(replies[0]["result"]["content"][0]["text"], "6")
            self.assertEqual(received.result(timeout=5), "(+ 1 2 3)")

    def test_configured_swank_endpoint_and_package(self):
        with fake_peer("swank") as ((host, port), received):
            replies = self.responses(run_bridge(
                {"SWANK_HOST": host, "SWANK_PORT": str(port),
                 "SWANK_PACKAGE": "CL-USER"}, [call("eval_swank", "(* 6 7)")]))
            self.assertEqual(replies[0]["result"]["content"][0]["text"],
                             "42\n\n[stdout]\nfixture output")
            self.assertEqual(received.result(timeout=5),
                             '(:emacs-rex (swank:eval-and-grab-output "(* 6 7)") '
                             '"CL-USER" t 1)')

    def test_host_timeout_is_applied(self):
        replies = self.responses(run_bridge(
            {"GENERA_TIMEOUT": "1"}, [call("eval_host_cl", "(sleep 5)")]))
        self.assertEqual(replies[0]["result"]["content"][0]["text"],
                         "Timeout: evaluation did not complete within 1s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
