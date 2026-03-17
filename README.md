# symbolics-mcp

An MCP (Model Context Protocol) server that lets LLMs evaluate Lisp expressions on a Symbolics Genera machine over TCP.

The system has two parts:

1. **eval-server.lisp** -- a TCP evaluation server that runs on the Genera machine
2. **genera-mcp.lisp** -- an MCP stdio server that runs on the host (SBCL) and bridges JSON-RPC requests to Genera over TCP

## Prerequisites

- A Symbolics Genera virtual machine (e.g. via Open Genera on a Linux/Alpha host or the VLM)
- [SBCL](https://www.sbcl.org/) on the host machine running the virtual Genera
- [Quicklisp](https://www.quicklisp.org/) installed in SBCL (the MCP server loads `usocket` and `yason` via Quicklisp)

## Setting up the Genera side

First, copy `eval-server.lisp` to the Genera `SYS:SITE;` directory on the host filesystem:

```sh
cp eval-server.lisp /opt/symbolics/lib/sys.sct/site/eval-server.lisp
```

Then load it from the Genera Lisp Listener:

```lisp
Load File SYS:SITE;eval-server.lisp
```

The file must be loaded once per boot. It does two things:

- Defines a TCP server (`:mcp-server`) that accepts connections, reads length-prefixed Lisp expressions, evaluates them, and returns the result, stdout, and stderr as length-prefixed fields.
- Registers the server on TCP port 8888 via `tcp:add-tcp-port-for-protocol`.

After loading, the server starts automatically -- there is no separate start command. It listens on port 8888 and handles each connection in its own process.

To verify it is running, you can send a test expression from the host:

```sh
echo -n '11 (+ 1 2 3 4)' | nc -w 2 192.168.11.2 8888 | cat -v
```

The number before the space is the byte length of the expression (`(+ 1 2 3 4)` = 11 bytes). If the server is working, you should see a response containing `10` (the result of the expression).

## MCP configuration

Add the following to your `.mcp.json` (or the MCP configuration of your client), adjusting the path and IP address for your setup:

```json
{
    "mcpServers": {
        "symbolics-genera": {
            "command": "/path/to/genera-mcp.sh",
            "env": {
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
| `GENERA_HOST` | `genera` | Hostname or IP of the Genera machine |
| `GENERA_PORT` | `8888` | TCP port of the eval server |
| `GENERA_TIMEOUT` | `30` | Timeout in seconds for each evaluation |

## Exposed tool

The MCP server exposes a single tool:

- **eval_lisp** -- Evaluate a Common Lisp / Zetalisp expression on the Genera machine and return the result. The response includes the return value, stdout, and stderr (if any).
