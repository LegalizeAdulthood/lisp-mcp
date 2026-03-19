#!/bin/sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec sbcl --noinform --non-interactive --load "$SCRIPT_DIR/lisp-mcp.lisp" --eval '(lisp-mcp:main)'
