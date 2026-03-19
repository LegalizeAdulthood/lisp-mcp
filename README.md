# lisp-mcp

An MCP (Model Context Protocol) server that gives LLMs the ability to evaluate Common Lisp expressions. It provides up to three tools depending on configuration:

- **eval_host_cl** -- always available; evaluates expressions in the MCP server's own SBCL process
- **eval_swank** -- connects to a running Lisp via the Swank protocol (the backend used by SLIME and Sly)
- **eval_genera** -- bridges to a Symbolics Genera machine over TCP

## Prerequisites

- [SBCL](https://www.sbcl.org/) on the host machine
- [Quicklisp](https://www.quicklisp.org/) installed in SBCL (the MCP server loads `usocket` and `yason` via Quicklisp)

## MCP configuration

Add the following to your `.mcp.json` (or the MCP configuration of your client):

```json
{
    "mcpServers": {
        "lisp": {
            "command": "/path/to/lisp-mcp.sh",
            "env": {
                "SWANK_PORT": "4005",
                "GENERA_HOST": "192.168.11.2"
            }
        }
    }
}
```

Only `command` is required. The environment variables enable the optional tools.

## Tools

### eval_host_cl

Evaluate a Common Lisp expression in the MCP server's own SBCL process. Always available, no configuration needed. Stdout, stderr, and the return value are captured and returned.

### eval_swank

Evaluate a Common Lisp expression on a running Lisp that has a Swank server. This is the most useful tool for development -- it lets the LLM interact with your live Lisp image, load systems, inspect state, and run tests.

The Swank client implements the Swank wire protocol, handles debugger entry by automatically invoking the `*ABORT` restart and returning the error description, and responds to interactive prompts (e.g. `y-or-n-p` during compilation).

Start a Swank server in your target Lisp:

```lisp
(swank:create-server :port 4005 :dont-close t)
```

The `:dont-close t` argument keeps the server listening after the first connection disconnects, so the MCP server can reconnect between requests.

To have Swank automatically start an additional server on a fixed port whenever you launch SLIME from Emacs with `M-x slime`, add the following to your Emacs configuration:

```elisp
(add-hook 'slime-connected-hook
          (lambda ()
            (slime-eval-async '(swank:create-server :port 4005 :dont-close t))))
```

Then set `SWANK_PORT` in the MCP configuration to enable the tool.

| Variable | Default | Description |
|---|---|---|
| `SWANK_PORT` | *(unset)* | TCP port of the Swank server. When set, enables `eval_swank`. |
| `SWANK_HOST` | `127.0.0.1` | Hostname or IP of the Swank server |
| `SWANK_PACKAGE` | `COMMON-LISP-USER` | Package in which expressions are evaluated |

### eval_genera

Evaluate a Lisp expression on a Symbolics Genera machine. Requires `eval-server.lisp` running on the Genera side.

Set `GENERA_HOST` in the MCP configuration to enable the tool.

| Variable | Default | Description |
|---|---|---|
| `GENERA_HOST` | *(unset)* | Hostname or IP of the Genera machine. When set, enables `eval_genera`. |
| `GENERA_PORT` | `8888` | TCP port of the eval server |
| `GENERA_TIMEOUT` | `300` | Timeout in seconds for each evaluation |

#### Setting up the Genera side

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
