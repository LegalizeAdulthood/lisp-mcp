"""Structured eval acceptance; --live-genera leaves the legacy listener running."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import re
import socket
import subprocess
import sys
import unittest

LIVE = "--live-genera" in sys.argv
if LIVE:
    sys.argv.remove("--live-genera")

from configuration import ROOT, receive_exact, sbcl_command
from framing import frame, read_live_field
from protocol import legacy, lisp_string, reload_sources, run_lisp


def parse(text):
    """Read test replies without using an executable Lisp reader."""
    tokens = iter(re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text))

    def value(token):
        if token == "(":
            result = []
            for item in tokens:
                if item == ")":
                    return result
                result.append(value(item))
            raise AssertionError("Unterminated test reply")
        if token.startswith('"'):
            return re.sub(r'\\(["\\])', r'\1', token[1:-1])
        if token == "nil":
            return None
        if token == "t":
            return True
        return int(token) if re.fullmatch(r"-?\d+", token) else token

    result = value(next(tokens))
    if next(tokens, None) is not None:
        raise AssertionError("Trailing test reply")
    return result


def properties(text):
    data = parse(text)
    return dict(zip(data[::2], data[1::2]))


def wire(value):
    if value is None:
        return "nil"
    if value is True:
        return "t"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        def field(key, item):
            if key in [":op", ":request-state", ":interrupt-state", ":code", ":test-status"]:
                return item if isinstance(item, str) and item.startswith(":") else wire(item)
            if key == ":features":
                return "(" + " ".join(item) + ")"
            return wire(item)
        return "(" + " ".join(k + " " + field(k, v) for k, v in value.items()) + ")"
    if isinstance(value, list):
        return "(" + " ".join(map(wire, value)) + ")"
    return lisp_string(value)


def receive_request(stream):
    header = b""
    while not header.endswith(b" "):
        header += receive_exact(stream, 1)
    return properties(receive_exact(stream, int(header)).decode("ascii"))


@contextmanager
def eval_peer(change=None, hello_error=False, features=None):
    with socket.socket() as listener, ThreadPoolExecutor(1) as executor:
        listener.bind(("127.0.0.2", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            with listener.accept()[0] as stream:
                stream.settimeout(5)
                hello = receive_request(stream)
                if hello_error:
                    greeting = {":version": 1, ":id": hello[":id"], ":ok": None,
                                ":error": {":code": ":authentication-failed",
                                           ":message": "Authentication failed"}}
                else:
                    greeting = {":version": 1, ":id": hello[":id"], ":ok": True,
                                ":boot-id": "boot", ":service-id": "service",
                                ":world": "fixture.vlod", ":package": "USER",
                                ":busy": None, ":features": features if features is not None
                                else [":status", ":eval"]}
                stream.sendall(frame(wire(greeting).encode()))
                if hello_error or ":eval" not in greeting[":features"]:
                    if stream.recv(1) != b"":
                        raise AssertionError("Eval sent after rejected hello")
                    return hello, None
                request = receive_request(stream)
                reply = {":version": 1, ":id": request[":id"], ":ok": True,
                         ":boot-id": "boot", ":service-id": "service",
                         ":request-state": ":completed", ":values": ["3", ":DONE"],
                         ":value-count": 2, ":stdout": "a\nb\r\nc\t", ":stderr": "",
                         ":warnings": ["a warning"], ":package": "USER",
                         ":elapsed-ms": 3, ":truncated": None, ":replaced": None}
                if change:
                    change(reply)
                stream.sendall(frame(wire(reply).encode()))
                if stream.recv(1) != b"":
                    raise AssertionError("Unexpected retry after eval reply")
                return hello, request

        received = executor.submit(serve)
        yield *listener.getsockname(), received
        received.result(timeout=10)


def host_eval(host, port, form, **kwargs):
    options = " ".join(":" + key.replace("_", "-") + " " + wire(value)
                       for key, value in kwargs.items())
    return run_lisp(
        '(handler-case (write-string (lisp-mcp::mcp-v1-encode '
        f'(lisp-mcp::genera-eval-envelope {lisp_string(form)} '
        f':host {lisp_string(host)} :port {port} {options}) :response)) '
        '(error () (write-string "rejected")))', binary=True)


class EvaluationChecks(unittest.TestCase):
    def test_portable_bounds_and_result_contract(self):
        result = subprocess.run(
            sbcl_command() + ["--load", str(ROOT / "tests" / "evaluation.lisp")],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Evaluation unit checks:", result.stdout)
        print(result.stdout.strip())

    def test_host_hello_and_eval_share_socket_and_preserve_source(self):
        form = '(values #\\; "a; b\nline" #| outer #| nested |# |#) ; tail\r\n'
        with eval_peer() as (host, port, received):
            result = host_eval(host, port, form, package="USER", print_level=3,
                               print_length=5, token="secret", expected_world="fixture.vlod")
        self.assertEqual(result.returncode, 0, result.stderr)
        hello, request = received.result()
        self.assertEqual(hello[":token"], "secret")
        self.assertEqual(request[":form"], form)
        self.assertEqual(request[":package"], "USER")
        self.assertEqual(request[":print-level"], 3)
        self.assertEqual(request[":print-length"], 5)
        self.assertEqual(request[":boot-id"], "boot")
        self.assertNotEqual(request[":id"], hello[":id"])
        reply = properties(result.stdout.decode())
        self.assertEqual(reply[":values"], ["3", ":DONE"])
        self.assertEqual(reply[":stdout"], "a\nb\r\nc\t")
        self.assertEqual(reply[":warnings"], ["a warning"])

    def test_host_rejects_wrong_identity_or_incomplete_result(self):
        changes = [lambda r: r.pop(":stdout"), lambda r: r.update({":id": "other"}),
                   lambda r: r.update({":boot-id": "other"}),
                   lambda r: r.update({":service-id": "other"}),
                   lambda r: r.update({":request-state": ":running"})]
        for change in changes:
            with self.subTest(change=change):
                with eval_peer(change=change) as (host, port, _):
                    result = host_eval(host, port, "(+ 1 2)")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"rejected")

    def test_host_does_not_eval_after_failed_hello_or_missing_capability(self):
        for settings in [dict(hello_error=True), dict(features=[":status"])]:
            with self.subTest(settings=settings):
                with eval_peer(**settings) as (host, port, received):
                    result = host_eval(host, port, "(error \"never\")")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(received.result()[1])
                if settings.get("hello_error"):
                    self.assertIn(b":authentication-failed", result.stdout)
                else:
                    self.assertEqual(result.stdout, b"rejected")

    def test_host_rejects_unsupported_form_before_connecting(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.2", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            result = host_eval(*listener.getsockname(), '(print "\u00e9")')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"rejected")
            with self.assertRaises(socket.timeout):
                listener.accept()

    @unittest.skipUnless(LIVE, "requires --live-genera and a running legacy listener")
    def test_live_structured_evaluation_and_recovery(self):
        host = os.environ.get("GENERA_HOST")
        self.assertTrue(host, "Set GENERA_HOST for --live-genera")
        port = int(os.environ.get("GENERA_PORT", "8888"))
        with socket.create_connection((host, port), timeout=5) as control:
            control.settimeout(30)
            self.assertEqual(reload_sources(control), "codec loaded")
            legacy(control, '(setf (getf *mcp-service* :protocol) :v1 *mcp-protocol* :v1)')
            try:
                with socket.create_connection((host, port), timeout=5) as stream:
                    stream.settimeout(20)
                    stream.sendall(frame(b'(:version 1 :id "hello" :op :hello)', b""))
                    hello = properties(read_live_field(stream, normalize=False))
                    self.assertIn(":eval", hello[":features"])
                    counter = 0

                    def evaluate(form, **options):
                        nonlocal counter
                        counter += 1
                        request = {":version": 1, ":id": f"live-eval-{counter}", ":op": ":eval",
                                   ":boot-id": hello[":boot-id"], ":service-id": hello[":service-id"],
                                   ":form": form}
                        request.update({":" + k.replace("_", "-"): v for k, v in options.items()})
                        stream.sendall(frame(wire(request).encode("ascii"), b""))
                        raw = read_live_field(stream, normalize=False)
                        self.assertLess(len(raw), 150000)
                        reply = properties(raw)
                        for key in [":id", ":boot-id", ":service-id"]:
                            self.assertEqual(reply[key], request[key])
                        if ":request-state" in reply:
                            self.assertGreaterEqual(reply[":elapsed-ms"], 0)
                            self.assertEqual(reply[":value-count"], len(reply[":values"] or []))
                        return reply

                    def success(form, expected, **options):
                        reply = evaluate(form, **options)
                        self.assertTrue(reply[":ok"], reply)
                        self.assertEqual(reply[":request-state"], ":completed")
                        self.assertEqual(reply[":values"] or [], expected)
                        return reply

                    success("(+ 20 22)", ["42"])
                    success("(values 3 :done nil)", ["3", ":DONE", "NIL"])
                    success("(values)", [])
                    success("(setq *mcp-structured-test-value* 41)", ["41"])
                    success("(incf *mcp-structured-test-value*)", ["42"])
                    for newline in ["\n", "\r", "\r\n"]:
                        with self.subTest(newline=repr(newline)):
                            success(f"(+ 1 ; ignored{newline}2)", ["3"])
                    success("(+ 1 #| outer #| nested |# comment |# 2)", ["3"])
                    success('(list #\\; #\\" #\\\\ #\\Newline)',
                            ['(#\\; #\\" #\\\\ #\\Return)'])
                    success('(length "a; b\nnext")', ["9"])
                    reply = success('(progn (write-string "a\nb\r\nc\t") 7)', ["7"])
                    self.assertEqual(reply[":stdout"], "a\nb\r\nc\t")
                    self.assertIsNone(reply[":replaced"])
                    success("(lisp:package-name lisp:*package*)", ['"KEYWORD"'], package="KEYWORD")
                    success("'(1 2 3 4)", ["(1 2 ...)"], print_length=2)
                    success("'((1 (2)))", ["((1 #))"], print_level=2)
                    warning = success('(progn (warn "native warning") '
                                      '(ansi-common-lisp:warn "ansi warning") 8)', ["8"])
                    self.assertEqual(warning[":warnings"], ["native warning", "ansi warning"])
                    reply = success('(progn (write-string "out") '
                                    '(write-string "err" *error-output*) '
                                    '(write-string "trace" *trace-output*) '
                                    '(write-string "query" *query-io*) '
                                    '(write-string "debug" *debug-io*) '
                                    '(write-string "terminal" *terminal-io*) '
                                    '(read-line *query-io* nil :eof))', [":EOF"])
                    self.assertEqual(reply[":stdout"], "out")
                    self.assertEqual(reply[":stderr"], "errtracequerydebugterminal")
                    failures = [('(+ 1 2)', "package-not-found", dict(package="MCP-NO-SUCH-PACKAGE")),
                                ('(', "reader-error", {}),
                                ('(setq *mcp-structured-test-value* 999) 2', "reader-error", {}),
                                ('#.(progn (setq *mcp-structured-test-value* 999) 1)',
                                 "reader-error", {}),
                                ('#,(progn (setq *mcp-structured-test-value* 999) 1)',
                                 "reader-error", {}),
                                ('(mcp-no-such-function)', "evaluation-error", {}),
                                ('(ansi-common-lisp:error "ansi failure")', "evaluation-error", {}),
                                ('(progn (write-string "before") (error "native failure"))',
                                 "evaluation-error", {})]
                    for form, code, options in failures:
                        with self.subTest(form=form):
                            reply = evaluate(form, **options)
                            self.assertIsNone(reply[":ok"])
                            self.assertEqual(reply[":request-state"], ":failed")
                            error = dict(zip(reply[":error"][::2], reply[":error"][1::2]))
                            self.assertEqual(error[":code"], ":" + code)
                            self.assertTrue(error[":condition-type"])
                            self.assertTrue(error[":message"])
                            success("(+ 1 2)", ["3"])
                    self.assertEqual(reply[":stdout"], "before")
                    success("*mcp-structured-test-value*", ["42"])
                    reply = evaluate('(error (make-string 100000 :initial-element #\\x))')
                    self.assertIsNone(reply[":ok"])
                    self.assertTrue(reply[":truncated"])
                    error = dict(zip(reply[":error"][::2], reply[":error"][1::2]))
                    self.assertLessEqual(len(error[":message"]), 4096)
                    success("(+ 1 2)", ["3"])
                    timeout = evaluate('(setq *mcp-structured-test-value* 999)', timeout_ms=1)
                    self.assertIsNone(timeout[":ok"])
                    self.assertNotIn(":request-state", timeout)
                    success("*mcp-structured-test-value*", ["42"])
                    reply = success('(progn (dotimes (i 100000) (write-char #\\x)) 9)', [""])
                    self.assertEqual(len(reply[":stdout"]), 65536)
                    self.assertTrue(reply[":truncated"])
                    reply = evaluate('(make-string 100000 :initial-element #\\x)')
                    self.assertTrue(reply[":ok"])
                    self.assertEqual(len(reply[":values"][0]), 65536)
                    self.assertTrue(reply[":truncated"])
                    reply = success('(progn (write-char (code-char 300)) 1)', ["1"])
                    self.assertEqual(reply[":stdout"], "?")
                    self.assertTrue(reply[":replaced"])
                    warnings = success('(progn (dotimes (i 40) (warn "repeat")) 1)', ["1"])
                    self.assertEqual(len(warnings[":warnings"]), 32)
                    self.assertTrue(warnings[":truncated"])
                    success('(progn (defflavor mcp-broken-printer () ()) '
                            '(defmethod (:print-self mcp-broken-printer) (s p d) '
                            ' (declare (ignore s p d)) (error "broken printer")) '
                            '(make-instance \'mcp-broken-printer))', ['#<printing failed>'])
                    success("(+ 1 2)", ["3"])
                # New host sockets observe the same state; host helper also
                # validates the real result, including all terminal fields.
                result = host_eval(host, port, "*mcp-structured-test-value*")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(properties(result.stdout.decode())[":values"], ["42"])
                print(f"Live structured eval: {counter} requests, then host reconnect passed.")
            finally:
                legacy(control, '(setf (getf *mcp-service* :protocol) :legacy '
                                '*mcp-protocol* :legacy)')
                self.assertEqual(legacy(control, '(+ 20 22)'), "42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
