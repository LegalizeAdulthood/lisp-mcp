"""Smoke test for the Genera TCP eval server."""

import asyncio
import os
import sys

from server import genera_send

HOST = os.environ.get("GENERA_HOST", "genera")
PORT = int(os.environ.get("GENERA_PORT", "8888"))

TESTS = [
    # (description, expression, check_result)
    ("arithmetic", "(+ 2 3)", lambda r, o, e: r == "5"),
    ("string result", '(format nil "hello ~A" "world")', lambda r, o, e: r == "hello world"),
    ("stdout capture", '(progn (format t "printed") 42)', lambda r, o, e: r == "42" and o == "printed"),
    ("stderr capture", '(progn (format *error-output* "warning") 1)', lambda r, o, e: r == "1" and e == "warning"),
    ("error handling", "(/ 1 0)", lambda r, o, e: e != ""),
    # Characters that need char-to-ascii translation:
    # #\Circle (char code 150) and #\Triangle (char code 151) have no ASCII equivalent
    ("non-ascii chars in output",
     '(format nil "~C and ~C" #\\Circle #\\Triangle)',
     lambda r, o, e: "?" in r),
    # #\Alpha (char code 2) -> no ASCII mapping
    ("alpha char translation",
     '(princ-to-string #\\Alpha)',
     lambda r, o, e: r == "?"),
    # Expression containing newlines (multiline let form)
    ("multiline expression",
     '(let ((x 10)\n      (y 20))\n  (+ x y))',
     lambda r, o, e: r == "30"),
    # Result containing newlines
    ("newlines in result",
     '(format nil "line1~%line2~%line3")',
     lambda r, o, e: r == "line1\nline2\nline3"),
    # Stdout containing newlines
    ("newlines in stdout",
     '(progn (format t "a~%b~%c") 0)',
     lambda r, o, e: r == "0" and o == "a\nb\nc"),
]


async def run_tests():
    passed = failed = 0
    for desc, expr, check in TESTS:
        try:
            result, stdout, stderr = await genera_send(expr, HOST, PORT)
            ok = check(result, stdout, stderr)
            status = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
                print(f"  {status}  {desc}")
                print(f"        result={result!r} stdout={stdout!r} stderr={stderr!r}")
            else:
                passed += 1
                print(f"  {status}  {desc}")
        except Exception as ex:
            failed += 1
            print(f"  FAIL  {desc}")
            print(f"        {ex}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)
