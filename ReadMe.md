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

### Local runtime preflight

`genera-launch.lisp` provides read-only host diagnostics for an explicitly
configured local WSL runtime. Remote endpoints use attach-only mode.
Configuration is validated before MCP dispatch; filesystem inspection is
performed only when `genera-preflight` is called.

| Variable | Default | Meaning |
|---|---|---|
| `GENERA_RUNTIME_MODE` | `attach` | `attach` or `local` |
| `GENERA_AUTOSTART` | `false` | `true` permits local launch; `false` denies it |
| `GENERA_VLM_ROOT` | unset | Absolute WSL working directory; required locally |
| `GENERA_VLM_EXECUTABLE` | `genera` | VLM executable pathname |
| `GENERA_VLM_WORLD` | unset | Existing boot-enabled world; required locally |
| `GENERA_VLM_PARTITION` | unset | Existing FEP partition; required locally |
| `GENERA_VLM_DEBUGGER` | `VLM_debugger` | Debugger executable pathname |
| `GENERA_VLM_USER` | unset | Expected non-root WSL account; required locally |
| `GENERA_VLM_NETWORK` | unset | Preconfigured TAP argument; required locally |
| `GENERA_STATE_DIR` | XDG state root plus `lisp-mcp` | Private Linux state directory |
| `GENERA_VLM_LOG` | `vlm.log` in the state directory | VLM log destination |

The default state root is `XDG_STATE_HOME`, or `~/.local/state` when unset.
Root, state, and log paths must be absolute Unix paths. Executable, world,
partition, and debugger paths may be absolute or relative to the VLM root.
Wildcards and Windows path syntax are rejected. Paths containing spaces
remain single arguments; existing symlinks resolve to canonical paths.

Local mode requires the Genera profile, protocol v1, and port 8888.
`GENERA_HOST` must exactly match the guest IPv4 in the network argument:

```text
TAP:IPv4;mask=IPv4;gateway=IPv4;preconfiguredTAP=yes
```

The argument is preserved verbatim, including its guest netmask.
Attach-only mode rejects autostart permission and does not inspect local
runtime files. Configure and inspect from the host Lisp:

```lisp
(lisp-mcp::configure)
(lisp-mcp::genera-preflight)
```

The returned property list includes `:ok`, `:launch-permitted`, canonical
`:paths`, working `:root`, structured `:argv`, environment facts, TAP
ownership, log metadata, and named `:checks`. Failed checks carry stable
codes such as `:missing-executable`, `:missing-world`, `:wrong-user`,
`:missing-display`, and `:tap-inaccessible`. `:argv` is NIL on failure.
The fixed arguments retain VM size 2000, the debugger/world/network/FEP
options, and foreground/background colors `#F0EDCC` and `#02343F`.

Preflight checks regular files and permissions, the VLM's Linux ELF64
x86-64 header, the actual WSL user, local X11 socket access, persistent TAP
flags and owner/group access, `/dev/net/tun`, and log/state locations.
All configured TAP owner and group restrictions must match, as required
by the [Linux TAP driver](https://github.com/torvalds/linux/blob/v6.18/drivers/net/tun.c#L481).
The check does not assume privileged network capabilities.
It rejects log aliases of runtime files, including hard links. A missing
state/log directory may be reported ready when its existing ancestor is
writable; inspection does not create it. Existing state directories must
be private and owned by the current user.

`:launch-permitted` is a prerequisite verdict, not process ownership or
an instruction to launch. Preflight starts no process, contacts no Genera
endpoint, attaches no TAP device, runs no sudo, changes no display setting,
and never saves or overwrites a world. Socket/TAP metadata checks do not
prove display authentication, guest routing, or listener readiness.

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

`genera_session_status` returns local state, phase, connection, pending
request count, known generation, error data, and bounded local history.
Its default snapshot does no network work. Optional `refresh: true`
performs a bounded status request through the control worker; failure
returns a tool error with stale cached session data.

`remote_status_stale` is true after connection loss, during local work,
after a completed eval, or when the observation ages past the health
interval. `remote_observed_at` is the host observation time in Unix
seconds; `remote_observation_age_ms` reports its age. Cached idle status
cannot prove an unresolved request's outcome.

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
executes the request. It can join or retry a failed control connection,
repeating authentication, expected-world, and generation checks.
A new MCP process can retrieve a retained result
using the original identifiers. `running` and `unknown` outcomes remain
blocked; missing or evicted history does not prove non-delivery.
Terminal responses appear under `result`. `source` is `ledger` or `host`;
a retained host result survives remote cache eviction.

The Genera profile reconnects automatically after transport or framing
failure. Delays grow from 0.25 seconds to a cap of 5 seconds; each TCP
connect and hello has a separate 5-second bound. A validated status or
eval response resets the failure count. Authentication rejection or an
expected-world mismatch stops automatic retries. Socket failure never
launches or restarts a VLM, and execution is never resent.

After reconnecting to the same generation, the bridge queries unresolved
work and observes a running ledger entry at bounded intervals. Terminal
results release the block; unknown history retains it for explicit
recovery. `reconnect_attempts` and `reconnect_failures` report progress.
`generation_change` retains previous/current boot and service IDs.
`world_state_lost` identifies a boot change; `history_lost` identifies
boot or service replacement. Old unresolved work remains blocked.

Idle health requests run every 30 seconds, with a 5-second host deadline.
A silent or partial reply causes connection recovery. Periodic health
requests pause during local work and while the last fresh observation
says the worker is busy. Explicit status refresh and request lookup
remain available. MCP EOF stops maintenance, backoff, and socket work.

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

Install `genera-protocol.lisp`, `eval-server.lisp`, and optionally
`genera-boot.lisp` at verified Genera pathnames. `SYS:SITE;` may reside
inside a FEP partition; copying files beside the VLM executable does not
make them visible there. Use an available file service or a trusted
installation channel, then verify the files in Genera:

```lisp
(probe-file "SYS:SITE;genera-protocol.lisp")
(probe-file "SYS:SITE;eval-server.lisp")
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

#### Starting the listener at boot

`genera-boot.lisp` registers `"MCP listener"` with Genera's warm
initialization list. Genera runs this list after namespace and network
initialization on cold and warm boots. Save the loaded definitions and
hook in a separate world; startup then needs no console source loading.
Keep the original world and partition as recovery copies.

After installing all three source files at verified Genera paths, load
them in `COMMON-LISP-USER`. To load definitions without starting a
listener, bind `*mcp-autostart*` to NIL:

```lisp
(let ((*package* (find-package "COMMON-LISP-USER"))
      (*mcp-autostart* nil))
  (declare (special *mcp-autostart*))
  (load "SYS:SITE;genera-protocol.lisp")
  (load "SYS:SITE;eval-server.lisp")
  (load "SYS:SITE;genera-boot.lisp"))
```

Register the desired protocol and optional token. For versioned startup:

```lisp
(mcp-install-boot-hook :protocol :v1 :enable-network t)
(mcp-boot-status)
```

`:enable-network t` explicitly enables the configured Genera networks
before starting the listener. Omit it when the site's startup already
enables networking. Add `:token "shared-secret"` for authenticated
versioned access; configuration, including that token, is saved in the
world. Legacy startup remains available with `:protocol :legacy`.
Supply `:world "world.vlod"` only when VLM world detection is unavailable.

Use Save World or `si:disk-save` to create a complete world at a **new**
destination. For a VLM, use its verified embedded-host pathname; ordinary
file-service access to that host is a separate facility. Never overwrite
the recovery world. Boot the new world with its matching partition and
verify hello/status before entering console commands.

The hook discards saved bridge streams, locks, request history, and
worker references. It creates fresh runtime resources and new boot and
service IDs. A repeated `mcp-boot-start` in the same boot preserves these
resources and identities; reconnecting also preserves them. Installation
replaces the named hook entry rather than adding another entry. It does
not save a world or start a VLM.

`mcp-boot-status` reports boot/listener source versions (currently 1),
state, and bounded failure diagnostics without tokens. Startup failures
are contained so Genera can finish booting. For manual recovery, inspect
that status from the console, correct the installed source/configuration,
reload the matching files with autostart disabled, and call
`mcp-boot-start`. Configuration changes to a running service still require
explicit service replacement. Save another new world after updating
definitions or installation settings; an existing saved world retains
its previous definitions.

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
the expected current boot/service IDs and an acknowledgement. Execution
is never replayed. `:host-timeout` on submit/call
overrides the host wait in seconds; eval defaults to `GENERA_TIMEOUT`,
controls to the session I/O timeout. It is never encoded on the wire.

`genera-close-session` stops its owner without waiting for remote eval.
The optional `:connect-timeout` defaults to 5 seconds for each TCP connect
and hello. `:io-timeout` defaults to 5 seconds for frame writes and
control waits. These bounds do not limit remote execution.
Cached status is the last validated snapshot, not live evidence after a
session fails. Direct API callers own and close their sessions. The
Genera MCP profile owns its session from initialized notification to EOF.

Direct session callers opt into maintenance with `:reconnect t` on
`genera-begin-session` or `genera-open-session`. Without it, only explicit
control calls retry a failed connection. The asynchronous begin function
allows initial connection failures to retry; open reports its initial
failure to the caller. Optional `:retry-min`, `:retry-max`,
`:health-interval`, and `:observe-interval` take positive seconds, with
defaults 0.25, 5, 30, and 0.5. The retry minimum must not exceed its cap.

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
python3 tests/reconnect.py
python3 tests/boot.py
python3 tests/preflight.py
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

After independently cold-booting a world with the installed versioned
hook, check its already running listener:

```sh
GENERA_HOST=genera.example GENERA_EXPECTED_WORLD=bridge.vlod \
  python3 tests/boot.py --live-genera
```

Set `GENERA_TOKEN` when configured. Optionally set
`GENERA_PREVIOUS_BOOT_ID` to the identity observed before restoring the
world; the check then requires a different boot ID. This runner does not
launch, reboot, load sources, or save a world. It verifies repeated hook
calls retain the worker, locks, service, and single hook registration;
contained failures permit retry; a new TCP connection retains identity.

The preflight runner uses temporary filesystem fixtures and explicit OS
snapshots to check missing files, permissions, user/display/TAP failures,
local versus attach-only policy, canonical argv, private state, and log
aliases. It verifies that inspection leaves saved-file contents and
metadata unchanged and creates no state/log files. To inspect a real WSL
runtime after setting its local runtime variables:

```sh
python3 tests/preflight.py --live-runtime \
  PreflightChecks.test_actual_wsl_prerequisites
```

This check inspects the configured host runtime without launching or
rebooting Genera and needs no computer control.

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

To check reconnect behavior against a running legacy listener:

```sh
GENERA_HOST=genera.example GENERA_PORT=8888 \
  python3 tests/reconnect.py --live-genera
```

Scripted peers check idle loss, repeated hello checks, capped backoff,
separate handshake deadlines, generation replacement, unknown history,
silent health replies, busy suppression, and EOF during backoff. The
native check uses a local proxy to drop a real terminal reply, reconnects
to the same world, and verifies retained lookup and a single side effect.
It restores legacy mode and leaves Genera running.
