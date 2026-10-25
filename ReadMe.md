# lisp-mcp

An MCP (Model Context Protocol) server for Common Lisp evaluation.
The default `generic` profile provides up to three tools:

- **eval_host_cl** -- available in the generic profile; evaluates expressions in the MCP server's own SBCL process
- **eval_swank** -- connects to a running Lisp via the Swank protocol (the backend used by SLIME and Sly)
- **eval_genera** -- bridges to a Symbolics Genera machine over TCP

The explicit `genera` profile provides `genera_session_status`,
`genera_request_status`, `genera_recover_session`, `genera_eval`, and the
`eval_genera(expression)` compatibility alias.

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
  applies to evaluations in the generic profile and supplies the default
  host wait deadline for versioned Genera evaluation.
- Hostnames and IP addresses must be nonempty and contain no whitespace
  or control characters. `SWANK_PACKAGE` must be nonempty text without
  surrounding whitespace or control characters.
- Unset `GENERA_HOST` to disable Genera; unset `SWANK_PORT` to disable
  Swank. Empty values are errors. Supplied settings are validated even
  when their optional tool is disabled.

| Variable | Default | Supported values |
|---|---|---|
| `LISP_MCP_PROFILE` | `generic` | `generic` or `genera` |
| `GENERA_PROTOCOL` | `legacy` | `legacy` with `generic`; explicit `v1` with `genera` |
| `GENERA_TOKEN` | *(unset)* | Optional shared token for the Genera profile |
| `GENERA_EXPECTED_WORLD` | *(unset)* | Optional expected world basename for the Genera profile |

Unsupported profile or protocol selections fail at startup; the bridge
never falls back to legacy. The Genera profile requires `GENERA_HOST`
and explicit `GENERA_PROTOCOL=v1`. Token and expected-world settings
require that profile and contain at most 4,096 printable ASCII characters
without surrounding whitespace. Initialization, discovery, and local
status do not contact Genera. The Genera profile starts one asynchronous
attach after `notifications/initialized`. Run the target Lisp first; the
bridge does not start a VLM.

### Genera profile

For a listener explicitly configured for version 1, use:

```json
{
    "mcpServers": {
        "lisp": {
            "command": "/path/to/lisp-mcp.sh",
            "env": {
                "LISP_MCP_PROFILE": "genera",
                "GENERA_PROTOCOL": "v1",
                "GENERA_HOST": "genera.example",
                "GENERA_PORT": "8888",
                "GENERA_EXPECTED_WORLD": "world.vlod"
            }
        }
    }
}
```

Set `GENERA_TOKEN` when the listener requires a shared token. Repeated
evaluations and the alias use the same authenticated session. The profile
does not expose host or Swank tools. Tool discovery advertises the five
implemented tools even before the connection is ready; an eval then
returns a structured not-ready tool failure instead of waiting in a queue.

`genera_session_status` takes no arguments and returns local state, phase,
connection, pending request count, known generation, error data, and
bounded local history. Its `remote_status_stale` field is true: cached
remote metadata is not a live worker-idle check. Status never opens,
refreshes, or restarts a connection.

`genera_eval` requires `form` and accepts optional `package`, `timeout_ms`,
`print_level`, and `print_length`. Print limits are nonnegative integers
or null, with defaults 6 and 100. `eval_genera` takes `expression` and uses the same
session and execution tracking. One eval is admitted at a time; another
receives a busy tool failure. `timeout_ms` is a positive integer specifying
the host wait deadline, including queue time. The default is
`GENERA_TIMEOUT` seconds (300); the alias uses this default. Results and
failures report `timeout_enforced_by: "host"`. The deadline never stops
Genera work and is not sent as a remote execution timeout.

Timeout and transport failures retain `request_id`, `boot_id`, and
`service_id`. Work whose send never began is `not-submitted`; a partial
or completed send without a terminal result remains `unresolved` and
blocks further execution. A matching late response or retained ledger
result releases that block without a second MCP completion. Execution
is never automatically replayed.

`genera_request_status` requires those three identifiers and never
executes the request. It can explicitly retry a failed control connection,
repeating authentication, expected-world, and generation checks. There is
no background reconnect. A new MCP process can retrieve a retained result
using the original identifiers. `running` and `unknown` outcomes remain
blocked; missing or evicted history does not prove non-delivery.
Terminal responses appear under `result`. `source` is `ledger` or `host`;
a retained host result survives remote cache eviction.

`genera_recover_session` requires the same unknown identity, the expected
current `expected_boot_id` and `expected_service_id`, and
`acknowledge_unknown: true`. It checks the ledger and obtains fresh worker
status before releasing the block. Running work, a busy worker, mismatched
identity, or unexpected current generation prevents recovery. After a
generation change, explicit recovery records `history_lost: true`.
Successful recovery records `acknowledged: true` while preserving the
outcome as `unknown`. It never retries or cancels work. Another client's
later admission can still cause a subsequent eval to receive busy.

Local history retains up to 32 records, with its limit and eviction count
in session status; unresolved records are retained ahead of terminal or
acknowledged records. Host memory and the remote ledger are bounded and
not durable journals. Preserve the original identifiers outside the MCP
process when recovery across client restarts is needed.

The MCP reader remains responsive while tool work runs. One response
writer serializes JSON-RPC output; responses may arrive out of request
order and retain their MCP IDs independently of bridge request IDs.
Malformed calls and invalid arguments receive JSON-RPC errors. Readiness,
connection, and evaluation failures use tool results with `isError: true`.
The generic profile retains its text results and ordered host/Swank work.

Supported MCP revisions are `2024-11-05` and `2025-06-18`. Versioned tools
always include serialized JSON in text content. Revision `2025-06-18`
also receives the same object as `structuredContent` and tool annotations;
older clients receive JSON text. See the
[MCP tools contract](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).

EOF in the Genera profile closes host resources promptly, including a
pending handshake or eval, while accepted Genera work continues. Generic
stdin batches drain accepted tool calls in their existing order. Logs
use stderr; stdout contains only JSON-RPC responses.

## Tools

### eval_host_cl

Evaluate a Common Lisp expression in the MCP server's own SBCL process.
Available in the generic profile, with no additional configuration.
Stdout, stderr, and return values are captured and returned.

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
| `GENERA_TIMEOUT` | `300` | Generic evaluation timeout; default versioned host wait deadline, in seconds |

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

The host Lisp helper `lisp-mcp::genera-eval-envelope` returns the result
property list. Without `:session`, it owns a temporary session for hello
and one eval. Supply `:session` to reuse an open session. It
accepts `:host`, `:port`, `:token`, `:expected-world`, `:package`,
`:print-level`, and `:print-length`. It validates response identity and
terminal fields and never retries execution or falls back to legacy.
When borrowing a session, connection/authentication options belong to
that session; combining `:session` with `:host`, `:port`, `:token`, or
`:expected-world` is rejected before submission.
The generic MCP profile selects the legacy tool path.

### Persistent host sessions

The SBCL session API authenticates once per connection. One I/O thread
owns the socket, all frame reads/writes, and response matching:

```lisp
(let ((session (lisp-mcp::genera-open-session
                 :host "genera.example" :port 8888
                 :token "shared-secret" :expected-world "world.vlod")))
  (unwind-protect
      (progn
        (lisp-mcp::genera-eval-envelope "(+ 20 22)" :session session)
        (lisp-mcp::genera-eval-envelope "(values :ready)" :session session)
        (lisp-mcp::genera-call session :status))
    (lisp-mcp::genera-close-session session)))
```

`genera-submit-request` accepts a session, operation, and request fields,
returning a handle immediately. `genera-await-request` waits for that
handle; `genera-call` submits and waits. Operations are `:eval`, `:status`,
and `:request-status`, subject to the listener's advertised features.
For example, submit `:eval :form "(sleep 2)"`, then call `:status` while
another thread awaits the eval handle. Responses are matched by ID and
generation before any waiter completes. Partial frames do not block
outgoing controls. The session admits one execution and up to 16 pending
controls; excess requests fail locally with a `:busy` session error.

Malformed, unmatched, repeated, or mismatched responses close the socket
and signal `genera-session-error` with kind `:protocol`. Transport loss
also closes the socket. Failed pending handles retain their original
envelopes, IDs, and generation: queued work becomes `:not-submitted`;
work whose send began becomes `:unresolved`. The condition's
`genera-session-error-session` accessor retains the failed session even
for a temporary helper call. A failed session accepts no new requests.
`genera-lookup-request` takes the session, request ID, boot ID, and service
ID; it explicitly retries the control connection when needed and
reconciles a retained result. `genera-recover-request` additionally takes
the expected current boot/service IDs and an acknowledgement. There is no
automatic reconnect or execution replay. `:host-timeout` on submit/call
overrides the host wait in seconds; eval defaults to `GENERA_TIMEOUT`,
controls to the session I/O timeout. It is never encoded on the wire.

`genera-close-session` stops its owner without waiting for remote eval.
The optional `:io-timeout` on open defaults to 5 seconds and bounds
connect, hello, and frame writes; it does not limit remote execution.
Cached status is the last validated snapshot, not live evidence after a
session fails. Direct API callers own and close their sessions. The
Genera MCP profile owns its session from initialized notification to EOF.

### Request history

Versioned eval records its request ID and payload before execution. The
service retains the active request plus at most 32 completed outcomes,
bounded by 4,194,304 serialized ASCII bytes for requests and results.
Hello/status reports `:history-limit`, `:history-byte-limit`,
`:history-count`, `:history-bytes`, and `:history-evictions`. Completed
entries are evicted in completion order; lookup does not refresh them.
An entry larger than the byte budget is evicted after completion.

After hello, query a request using the original generation IDs:

```lisp
(:version 1 :id "lookup-1" :op :request-status
 :boot-id "returned-boot-id" :service-id "returned-service-id"
 :target-id "eval-1")
```

The response uses the lookup ID, echoes `:target-id`, and returns
`:request-state` as `:running`, `:completed`, `:failed`, or `:unknown`.
A terminal lookup includes the original eval response under `:result`,
including its original ID, success/failure, and captured output. Lookup
itself succeeds even when that retained execution failed.

Resending a retained eval ID with the same payload returns running status
or the original terminal response without executing again. Reusing that
ID with different form, package, deadline, or print fields fails with
`:request-id-conflict`. Property order is ignored; omitted fields and
explicit defaults count as different payloads. A terminal result is
retained before writing its reply, including after client disconnection.

Source reload and repeated start preserve history. Service replacement
creates a new service ID and empty history; stale generation lookups fail.
Unknown or evicted history does not establish whether work was delivered.
This bounded cache is not a durable execution guarantee: do not replay an
uncertain request because its lookup returned unknown.

### Concurrent requests

Legacy and versioned evaluation share one persistent execution process.
A competing eval receives `:busy` before acceptance, with an
`:active-request-id` when available. Legacy callers receive a busy error
in the existing stderr field. An accepted eval continues if its client
disconnects. A connection retains its execution slot until its result
has been written; additional new evals on that connection are rejected.

Versioned hello/status/request-status remain responsive during evaluation,
including on the evaluating socket. Responses may arrive in a different
order from requests; match their IDs. One writer owns each connection,
so frames cannot interleave. Legacy replies retain request order.

The listener admits at most 16 clients, each with at most 16 outstanding
control replies and one reserved execution result. A full control
backlog pauses request reading. Additional clients are closed. An
unavailable execution worker produces `:worker-unavailable`; the bridge
does not silently start a second worker.

Source reload and listener replacement preserve the execution process.
External stop/replacement is refused during active work. A legacy
management form may replace its own listener while retaining the worker
and its occupied execution slot until the form finishes.

When upgrading a listener without an execution worker, close other
clients before loading the sources. The loading connection drains and
closes after its reply; reconnect afterward. Generation IDs survive
this transition; new evaluations receive busy until that handler finishes.
Worker entry points and lock continuations are compiled during loading.

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
python3 tests/concurrency.py
python3 tests/ledger.py
python3 tests/session.py
python3 tests/stdio.py
python3 tests/deadlines.py
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

To check concurrent native execution and response handling:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/concurrency.py --live-genera
```

The check exercises simultaneous submissions, status during eval,
disconnects, listener reload, client limits, and blocked/fragmented
response writers. It restores legacy mode and leaves Genera running.

To check retained outcomes and request lookup:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/ledger.py --live-genera
```

The check disconnects after acceptance, retrieves the result from a new
connection, and verifies that retained duplicates do not repeat side
effects. It checks failure retention, payload conflicts, source reload,
count/byte eviction, and generation changes. Fixture limits are reduced
temporarily to exercise eviction. It restores default limits and legacy
mode, preserving the execution worker and leaving Genera running.

To check persistent host sessions against a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/session.py --live-genera
```

The fake peers check one connection/hello, out-of-order response matching,
control requests during a pending eval, partial frames, admission limits,
protocol faults, disconnect identity retention, and prompt close. The
native check reuses one session for eval/status/request lookup while an
eval sleeps. It restores legacy mode and leaves Genera running.

To check MCP stdio against a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/stdio.py --live-genera
```

The scripted checks cover responsive initialization/discovery/status,
MCP and bridge ID matching, structured/text results, tool failures,
argument validation, stdout isolation, and EOF during eval or hello.
The native check confirms the alias, remote conditions, and completion
of accepted work after MCP EOF. It restores legacy mode and leaves
Genera running.

To check deadline and recovery behavior against a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/deadlines.py --live-genera
```

Scripted peers cover partial writes, queued deadlines, late and lost
replies, running/unknown/evicted outcomes, generation mismatch, and
fresh-idle recovery. The native check retrieves a result from a new MCP
client after a timeout and lost reply, verifies the side effect ran once,
and rejects recovery while another client occupies the worker. It
restores legacy mode and leaves Genera running.
