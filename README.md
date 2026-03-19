# lisp-mcp

An MCP (Model Context Protocol) server that lets LLMs evaluate Common Lisp expressions. It always provides a host Common Lisp evaluator and optionally bridges to a Swank server or a Symbolics Genera machine over TCP when the corresponding environment variables are set.

The system has two parts:

1. **lisp-mcp.lisp** -- an MCP stdio server that provides `eval_host_cl` and optionally `eval_swank` and `eval_genera`
2. **eval-server.lisp** -- a TCP evaluation server that runs on the Genera machine (only needed for Genera support)

## Prerequisites

- [SBCL](https://www.sbcl.org/) on the host machine
- [Quicklisp](https://www.quicklisp.org/) installed in SBCL (the MCP server loads `usocket` and `yason` via Quicklisp)
- (Optional) A running Lisp with a [Swank](https://github.com/slime/slime) server for the `eval_swank` tool
- (Optional) A Symbolics Genera virtual machine for the `eval_genera` tool

## MCP configuration

Add the following to your `.mcp.json` (or the MCP configuration of your client):

```json
{
    "mcpServers": {
        "lisp": {
            "command": "/path/to/lisp-mcp.sh"
        }
    }
}
```

This gives you the `eval_host_cl` tool. To also enable the Swank and/or Genera evaluators, add the corresponding environment variables:

```json
{
    "mcpServers": {
        "lisp": {
            "command": "/path/to/lisp-mcp.sh",
            "env": {
                "SWANK_PORT": "4005",
                "GENERA_HOST": "192.168.11.2",
                "GENERA_PORT": "8888"
            }
        }
    }
}
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `SWANK_HOST` | `127.0.0.1` | Hostname or IP of the Swank server |
| `SWANK_PORT` | *(unset)* | TCP port of the Swank server. When set, enables `eval_swank`. |
| `SWANK_PACKAGE` | `COMMON-LISP-USER` | Package in which Swank evaluates expressions |
| `GENERA_HOST` | *(unset)* | Hostname or IP of the Genera machine. When set, enables `eval_genera`. |
| `GENERA_PORT` | `8888` | TCP port of the eval server |
| `GENERA_TIMEOUT` | `300` | Timeout in seconds for each evaluation |

## Exposed tools

- **eval_host_cl** -- Evaluate a Common Lisp expression locally in the host Common Lisp and return the result.
- **eval_swank** *(only when `SWANK_PORT` is set)* -- Evaluate a Common Lisp expression on a running Lisp with a Swank server and return the result.
- **eval_genera** *(only when `GENERA_HOST` is set)* -- Evaluate a Lisp expression on a Symbolics Genera machine and return the result.

## Setting up the Genera side

Copy `eval-server.lisp` to the Genera `SYS:SITE;` directory on the host filesystem:

```sh
cp eval-server.lisp /opt/symbolics/lib/sys.sct/site/eval-server.lisp
```

Then load it from the Genera Lisp Listener:

```lisp
Load File SYS:SITE;eval-server.lisp
```

The file must be loaded once per boot. It defines a TCP server (`:mcp-server`) on port 8888 that accepts connections, reads length-prefixed Lisp expressions, evaluates them, and returns the result, stdout, and stderr as length-prefixed fields.

To verify it is running:

```sh
echo -n '11 (+ 1 2 3 4)' | nc -w 2 192.168.11.2 8888 | cat -v
```
