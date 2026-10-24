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
                "LISP_MCP_PROFILE": "generic",
                "GENERA_PROTOCOL": "legacy",
                "SWANK_PORT": "4005",
                "GENERA_HOST": "genera.example",
                "GENERA_PORT": "8888"
            }
        }
    }
}
```

Only `command` is required. The environment variables enable the optional tools.

### Startup validation

The bridge validates configuration before reading MCP requests or opening
connections. Invalid settings produce a diagnostic naming the variable
on stderr, leave stdout empty, and exit with status 2.

- Ports must contain decimal digits only and be in `1..65535`.
- `GENERA_TIMEOUT` must contain a positive whole number of seconds. It
  retains its existing use for Genera, host, and Swank evaluations.
- Hostnames and IP addresses must be nonempty and contain no whitespace
  or control characters. `SWANK_PACKAGE` must be nonempty text without
  surrounding whitespace or control characters.
- Unset `GENERA_HOST` to disable Genera; unset `SWANK_PORT` to disable
  Swank. Empty values are errors. Supplied settings are validated even
  when their optional tool is disabled.

| Variable | Default | Supported values |
|---|---|---|
| `LISP_MCP_PROFILE` | `generic` | `generic`, with the existing host and optional Swank/Genera tools |
| `GENERA_PROTOCOL` | `legacy` | `legacy`, using expression requests and three response fields |

Unsupported profile or protocol selections fail at startup; the bridge
never falls back to legacy after an unsupported selection. Initialization
and discovery do not contact Genera. Run the target Lisp before
evaluating expressions; the bridge does not start a VLM.

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

If `SYS:SITE;` is shared with the host filesystem, copy
`genera-protocol.lisp` and `eval-server.lisp` into its backing directory.
For example:

```sh
cp genera-protocol.lisp eval-server.lisp /opt/symbolics/lib/sys.sct/site/
```

Load both files in `COMMON-LISP-USER` from the Genera Lisp Listener:

```lisp
(let ((*package* (find-package "COMMON-LISP-USER")))
  (load "SYS:SITE;genera-protocol.lisp")
  (load "SYS:SITE;eval-server.lisp"))
```

Load the files once per boot. The listener defines a TCP server
(`:mcp-server`) on port 8888 that accepts length-prefixed Lisp expressions
and returns result, stdout, and stderr as three length-prefixed fields.

To verify it is running:

```sh
echo -n '11 (+ 1 2 3 4)' | nc -w 2 genera.example 8888 | cat -v
```

### Framing limits

Genera requests and each result/stdout/stderr field are limited to
1,048,576 bytes. Length headers contain at most 10 decimal digits and
exactly one trailing space. A reader accepts at most one leading LF,
CR, or CRLF; the host also accepts the native Genera Return byte (141)
emitted by the existing length printer. Empty fields are valid.

Both endpoints reject malformed or oversized headers before allocating a
payload. Payload reads require exactly the declared length. A truncated
frame closes the connection; the bridge reports a protocol error. Invalid
headers fail without scanning for another digit or waiting for peer EOF.
The Genera listener closes a malformed client connection before eval.

Input supports ASCII tab, LF, CR, and printable bytes 32 through 126.
The legacy host checks the original expression before comment removal or TCP
connection. Genera checks incoming bytes before character translation.
Existing diagnostic replacement and newline conversion are preserved.

Swank uses a six-digit hexadecimal header. Header reads require all six
digits; payload reads require exactly the declared length. Malformed
headers and payloads exceeding 1,048,576 characters fail before allocation.

### Data codec

`genera-protocol.lisp` contains the portable version-1 envelope codec.
It accepts known keywords, ASCII strings, signed 64-bit integers,
booleans, and proper lists. It limits nesting to 16 list levels, data
nodes to 4,096, and each encoded envelope to the frame limit above.

Request schemas require `:version`, `:id`, and `:op`; response schemas
require `:version`, `:id`, and `:ok`. Unknown keys or symbols, duplicate
properties, reader syntax, and trailing objects are rejected. The codec
does not invoke the Lisp reader or evaluator. Literal tab, LF, and CR
bytes in strings survive encoding and decoding unchanged.

### Versioned service control

The Genera listener provides these local lifecycle entry points:

```lisp
(mcp-start-service)
(mcp-stop-service)
(mcp-start-service :protocol :v1 :token "shared-secret" :replace t)
```

Starting with the same configuration preserves the service identity.
Configuration changes require `:replace t`. Stopping disables only the
MCP service and closes its clients. A restart creates a new service ID
while preserving the boot ID. Source reloads preserve both IDs.

The listener detects the VLM world basename. Supply `:world "world.vlod"`
when that information is unavailable. `mcp-initialize-boot` stops the
service and regenerates boot identity; it does not reboot Genera. Start
the service after initializing a new boot identity.

Versioned connections begin with `:hello`. For example:

```lisp
(:version 1 :id "hello-1" :op :hello
 :token "shared-secret" :expected-world "world.vlod")
```

A successful hello returns boot/service IDs, world, package, busy state,
and implemented capabilities. `:status` requests carry both returned
generation IDs. Stale IDs are rejected before dispatch. Authentication
and expected-world failures expose no service metadata; a failed repeat
hello revokes that connection's authorization.

Tokens require version-1 mode; legacy mode cannot be combined with a
token. Tokens contain 1 through 4,096 printable ASCII characters. Keep
versioned envelope clients and legacy eval clients on their explicitly
selected listener mode.

### Versioned evaluation

On the authenticated connection, send one form with the generation IDs
returned by hello:

```lisp
(:version 1 :id "eval-1" :op :eval
 :boot-id "returned-boot-id" :service-id "returned-service-id"
 :package "COMMON-LISP-USER" :form "(values (+ 1 2) :done)"
 :print-level 6 :print-length 100)
```

The result includes the matching request and generation IDs, terminal
`:request-state`, printed `:values`, `:value-count`, `:stdout`, `:stderr`,
`:warnings`, `:package`, and `:elapsed-ms`. Zero values produce an empty
value list. Execution failures have `:ok nil`, state `:failed`, and an
`:error` containing `:code`, `:condition-type`, and `:message`; captured
output survives the failure. Interactive streams use empty input and
captured error output. A failed request leaves the connection usable.

Package selection defaults to the bridge's evaluation package. Printing
defaults to level 6 and length 100, with circular printing enabled. An explicit NIL
print limit removes that printer limit. Output, warnings, and printed
values share a 65,536-character collection budget. At most 32 warnings
are retained; condition and package diagnostics share another 4,096
characters. `:truncated` reports collection limits; `:replaced` reports
unsupported diagnostic characters replaced with `?`. A failed value
printer returns `#<printing failed>` for that value.

Form text is sent unchanged. The reader accepts LF, CR, and CRLF line
comments while preserving literal newlines in strings. It requires one
form and disables read-time evaluation. `:timeout-ms` is rejected before
execution with `:unsupported-operation`; it does not impose a remote
execution deadline.

The host Lisp helper `lisp-mcp::genera-eval-envelope` performs hello and
one eval on the same socket, returning the result property list. It
accepts `:host`, `:port`, `:token`, `:expected-world`, `:package`,
`:print-level`, and `:print-length`. It validates response identity and
terminal fields and never retries execution or falls back to legacy.
The MCP environment configuration above selects the legacy tool path.

## Host acceptance checks

Use Python 3 and the existing SBCL/Quicklisp prerequisites. Load the
dependencies once before starting MCP so first-use compilation output
cannot enter its stdout:

```sh
cd /path/to/lisp-mcp
sbcl --noinform --non-interactive --load "$HOME/quicklisp/setup.lisp" \
  --eval "(ql:quickload '(:usocket :yason))"
python3 tests/configuration.py
python3 tests/framing.py
python3 tests/protocol.py
python3 tests/handshake.py
python3 tests/evaluation.py
```

If Quicklisp is installed outside the usual home directory, set
`LISP_MCP_TEST_QUICKLISP` to its `setup.lisp` path for the test runners.
This optional test setting does not change production startup.

The checks launch the same SBCL load/main invocation as `lisp-mcp.sh`.
They cover startup rejection before dispatch, port boundaries, timeout
enforcement, explicit profile/protocol selection, optional tool discovery,
host evaluation, and Genera/Swank requests to loopback TCP fixtures. No
Genera world, Swank installation, or VLM launch is needed.

The framing runner checks the actual host functions and the listener's
portable framing definitions, using explicit stream/character fixtures
for Genera APIs. It includes forced short reads, fragmented TCP fields,
invalid delimiters and prefixes, early EOF, header/frame limits, and
unsupported input rejection before connection. Malformed fake peers stay
open to verify prompt failure independently of the evaluation timeout.

The protocol runner checks shared fixtures in separate host and listener
packages, schema and resource limits, reader-syntax rejection, literal
newlines, and explicit envelope transport without legacy fallback.

After loading the updated listener in Genera, run:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/framing.py --live-genera
```

The live check sends fragmented ASCII requests, verifies stdout and
literal newlines, reuses a connection, checks malformed-client closure,
and finishes with an evaluation through the host bridge. Host fixtures
do not establish compatibility with Genera streams or condition handling.

To check the paired codec sources against a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/protocol.py --live-genera
```

This live check reloads both Lisp files, compares the shared fixtures in
Genera, and temporarily selects envelope mode to check one framed reply
per request and rejection without eval. It retains a legacy control
connection and restores legacy mode when finished.

To check authenticated hello/status and native service lifecycle:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/handshake.py --live-genera
```

This check requires a running legacy listener on a VLM. It reloads both
sources, temporarily configures a versioned service with a test token,
and checks identity, client closure, and bootstrap-loader replacement.
It simulates boot-identity initialization without rebooting the world,
then restores legacy mode and removes the test token.

To check structured evaluation and recovery on a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/evaluation.py --live-genera
```

The check reloads both sources, temporarily selects versioned mode, and
exercises values, packages, printing, warnings, stream redirection,
reader/condition failures, output bounds, and persistent state through
the host helper. It retains a legacy control connection and restores
legacy mode when finished.
