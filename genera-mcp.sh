#!/bin/sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec sbcl --noinform --non-interactive --load "$SCRIPT_DIR/genera-mcp.lisp" --eval '(genera-mcp:main)'
