# Logging

Everything an otto invocation *emits* flows through one model: three sinks
fed by one queue, with a single per-host/per-command knob
({class}`~otto.logger.mode.LogMode`) deciding what command I/O shows up
where. What a verb *returns* is the other cross-cutting spine — see
{doc}`results`.

## Three sinks

CLI logging writes to three places, wired per invocation by
`otto.logger.management` into the per-command output directory:

| Sink | Level | Purpose |
| --- | --- | --- |
| console (Rich) | `--log-level` | what the operator watches; timestamps only with `--show-time` |
| `console.log` | `--log-level` | a *faithful transcript* of the console — same records, always timestamped, plus the failure that ended the command ([Tracebacks](#tracebacks)) |
| `verbose.log` | INFO floor, DEBUG when `--log-level DEBUG` | the everything-record, including what the console suppressed |

Handlers hang off a `QueueListener`, so slow file I/O (e.g. logs on NFS)
never blocks the event loop, and old run directories are pruned under a
time-boxed budget so rotation cannot stall startup on slow mounts.

## Rich markup in log messages

A log record is rendered as
[Rich markup](https://rich.readthedocs.io/en/stable/markup.html) only when it
asks for it with `extra={"markup": True}`, Rich's own per-record convention:
`rich.logging.RichHandler` reads `record.markup`. The rule holds for every sink
and every logger: otto's, a suite's or instruction's, a third-party library's.
The console handler is a `RichHandler` built with `markup=False`, so it
renders only opted-in records, and both files go through
{class}`~otto.logger.formatters.RichFormatter`, which renders an opted-in
record through a capture console and leaves any other record's message
untouched. A record that does not opt in is printed as written: brackets,
backslashes and `:emoji:` codes reach the console and both files unchanged
(the one exception: a file that is not `--rich-log-file` still strips ANSI
escape sequences from every record).
How a suite or instruction author opts in, and escapes what an opted-in
message interpolates, is on {ref}`log-markup`.

Where otto opts in, it is for its own styling, such as the dry-run banner and a
test's start banner:

```python
logger.info(
    "[magenta][DRY RUN] Commands and file transfers will be skipped.",
    extra={"markup": True},
)
```

Rich reads a bracket as a tag only when its text starts with a lowercase
letter, `#`, `@` or `/`, so `[DRY RUN]` is printed as written; `[bench, floor]`
is read as a tag, and a tag that names no style disappears. An opted-in message
is parsed in full, f-string or `%`-argument alike, so whatever it interpolates
must be escaped with `rich.markup.escape`.

A host's echoed command and its output lines combine the two: otto's styled
prefix (`@host   | ` in bold, `@host > | ` in yellow) and text otto did not
write. Passing text through means two things: escape its markup and turn the
`:emoji:` pass off. Each of those lines is logged by
{func}`~otto.logger.relay.log_relayed`, which takes otto's markup prefix and the
relayed text separately, escapes only the text, and sends the record with
`markup=True, emoji=False` itself, so a caller cannot relay text without both.
`image:[sha256:abc]` in a device's output reaches the console and both files
with its brackets kept, `root:x:0:0 :warning:` stays literal, and a stray
`[/x]` cannot raise. The escape lives in the record's message, so
`caplog.text`, pytest's "Captured log" sections and any handler that does not
render markup see `\[` in any opted-in line that escapes foreign text: relay lines, a test's
name banner, an expect-failure line. Device or tool output
quoted in a plain otto warning (a failed `docker compose down`, a failed
coverage archive, a monitor command's error) is not styled, does not opt in,
and is shown as written. The text an `otto host <id>` verb returns
(`read-file`) is printed with markup and emoji codes off.

An opted-in record renders `:emoji:` codes on every sink; `extra={"emoji": False}`
turns that pass off for the record (meaningful only with markup on). An opted-in
record that escapes text otto did not write passes `"emoji": False` too; a guard
test enforces it.

By default the files are plain text: an opted-in record's markup is applied
and its ANSI codes are stripped. `--rich-log-file` (or `OTTO_LOG_RICH`) keeps those codes in both
files, for a pager that renders them (`less -R`). The colour comes from the
terminal otto started in, so a run whose output is redirected writes plain
message lines even with the flag set.

## Tracebacks

An exception reaches the sinks by one of three paths. The console shows a Rich
traceback without local variables; the files show a Rich traceback with each
frame's local variables:

| Path | Console | `console.log` and `verbose.log` |
| --- | --- | --- |
| logged: `logger.exception(...)`, or any call with `exc_info=` | the message and a Rich traceback (up to 20 frames, no locals), if `--log-level` admits the record | the message and the traceback, in each file whose level admits the record |
| an `OttoError` ends the command | the one-line `error: ...`, plus a plain traceback on stderr at `--log-level DEBUG` only | `error: <message>` and the traceback, in both files at any `--log-level` |
| any other exception ends the command (a crash, including a leaked `asyncio.CancelledError`) | the interpreter's traceback on stderr, once (Typer's, for an `Exception`) | `uncaught <Type> ended the command` and the traceback, in both files at any `--log-level` |

The console leaves locals out because a logged exception is often routine: a
teardown step, an asyncio task nobody awaited, a dashboard request that failed.
A locals panel per frame would bury the terminal; the files are where they are
read.

The two command-ending rows are written to both files whatever their level,
since a run that ends on an error has to say why in its transcript, and they
are never written to the console, which has already shown the failure its own
way. The files take an `OttoError`'s full message: a coverage refusal keeps its
plain verdict listing there, where the console prints the verdict table and a
headline. `SystemExit`, `KeyboardInterrupt` and `GeneratorExit` are not
failures and write nothing; a usage error, `typer.Exit`, `--help` and Ctrl-C
all leave the app as `SystemExit`.

Two kinds of failure leave no traceback in the files:

- a failure before the command has created its output directory (bootstrap,
  lab load), which has no files to land in; the terminal is its only record;
- a command that catches its own error, prints it and exits (a `typer.Exit` or
  `SystemExit` raised in `otto.cli`), which is a clean exit as far as the
  logs are concerned. Only an exception that leaves the command is recorded.

In the files a traceback is written below its message, each line with the
usual timestamp and level prefix, at a fixed 160 columns so a frame is not
folded to the width of the terminal the run started in. It shows up to 100
frames; frames inside Typer and click are shown by location only, as Typer's
own traceback does. It is plain text; with `--rich-log-file` it keeps Rich's
colours, whether or not otto was started in a terminal. It never passes
through the markup rendering above, so a source line or a local whose value is
`"[bold]x[/bold]"` appears exactly as written.

Each file writes a given exception object's traceback once. The same exception
logged twice, or logged and then re-raised until it ends the command, gets
`(traceback written above)` in place of a second copy, and only once the
first copy was actually written. A different exception object is written in
full, chain and all: an exception raised `from` one that was already logged,
or an exception group, prints the earlier exception again as part of its
chain.

```{warning}
Local variables are written to both log files as Rich shows them, in every
frame outside Typer and click. Each value is bounded: a string past 120
characters is truncated, a container past 10 items is cut short, and nesting
stops three levels down. The bounds hold in every frame of the traceback,
including the frames of an exception group's members (a `TaskGroup`, anyio or
`except*` failure). Containers, dataclasses (an `OttoContext`, a host)
and pydantic models are expanded field by field down to that depth. The
bounds shorten a secret but do not hide it. Nothing is redacted, and
`LogMode.NEVER` does not apply (it gates command I/O, not tracebacks). A
password, a token or a credential object in scope when the exception is
raised ends up in `console.log` and `verbose.log`. The one exception is the
monitor dashboard: `uvicorn.error` records are written without locals, because
every request passes through the frame that holds the dashboard's access key.
```

### How it works

The `QueueHandler` on root is a subclass whose `prepare` turns a record's
exception into data on the thread that logged it:
{func}`~otto.logger.formatters.capture_exception` takes a Rich trace (with the
locals, inside the bounds above), the stdlib's plain traceback as a fallback,
and an id that names the exception object, and clears `exc_info`. The stdlib's
own `prepare` flattens the traceback into the message text instead, which
loses the Rich rendering and, for a record that opted in, feeds the traceback
to the markup pass.

Capturing on the logging thread is what keeps the listener safe. The frame that
caught the exception is still running when the listener gets to the record, so
reading its locals there races the code that owns them, and an exception
escaping a handler kills the listener thread and every record after it. A live
traceback in the queue would also keep every frame and local alive until the
listener let go of the record, so their finalizers would run late and on the
listener thread. A logger filter that sets
{data}`~otto.logger.formatters.NO_LOCALS_ATTR` runs before the capture, which
is how the monitor keeps its key out.

Each sink renders the data its own way: the console handler builds a Rich
traceback without the locals, and the files go through
{func}`~otto.logger.formatters.render_traceback`. If Rich cannot take or render
the trace, both fall back to the plain traceback captured with it.

The command-ending paths call
{func}`~otto.logger.management.record_command_failure` from the frame in
`otto.cli.main.entry` that wraps the app. It captures the exception the same
way, puts a marked record straight onto the listener's queue behind everything
logged before it, and the listener hands that record to the two files alone.
The record is written as soon as the listener reaches it; the `atexit` stop of
the listener is the backstop that drains whatever is still queued when the
process exits. Nothing survives an exit that skips `atexit` (`os._exit`,
SIGKILL), and a sink stuck on a slow write holds back everything queued behind
it. Because locals are read where the exception is logged, a local whose
`repr` blocks holds up that logging call, not the listener.

## LogMode: one knob for command I/O

Whether a host's command echo and output *show up* is a per-host and
per-command disposition, {class}`~otto.logger.mode.LogMode`:

- `NORMAL` — logged at the call's native level, visible everywhere.
- `QUIET` — suppressed from the console and `console.log`, kept in
  `verbose.log`. For routine chatter: file-op read bodies, `lsmod` scrapes,
  config probes.
- `NEVER` — redacted from every sink. For secrets (an `su` password) and
  bulk noise (a hex firmware payload streamed over a console).

The effective mode composes **most-restrictive-wins**
({func}`~otto.logger.mode.effective_mode`): a `QUIET` host running a `NEVER`
command yields `NEVER`. If either party considers the I/O sensitive, the
stricter disposition holds.

Scope is the important invariant: **LogMode gates command I/O only** —
records tagged with the host that emitted them. Framework diagnostics,
warnings, and errors are never suppressed by LogMode; a `NEVER` host still
logs its connection failures. This is why the monitor can set its polling
hosts to `NEVER` ({doc}`../subsystems/monitoring`) without hiding real
problems.

## Root capture: three postures

otto configures the **ROOT** logger, not `'otto'`. The `'otto'` logger is an
ordinary library logger — `propagate = True`, no handlers beyond the
import-time `NullHandler` — and otto's own modules emit via
`logging.getLogger(__name__)` exactly like any consumer's code. Handlers go
on root instead, which is what makes capture zero-registration: a record
from ANY logger in the process — otto's own, a project's product code, a suite
module, a third-party library — reaches the three sinks above by ordinary
propagation. There is no allowlist to keep in sync and nothing to register.

Three postures cover every way the process gets configured:

- **`otto` CLI** — otto owns the process, so it configures root itself: the
  console handler goes up in the root Typer callback, before any project
  gate or lab probe that might want to `logger.warning`; the file sinks
  attach once the output directory exists.
- **Inner pytest sessions (`otto test`)** — same process, already
  configured by the CLI posture above. pytest's own logging plugin (caplog,
  `log_cli`) coexists rather than competing — see "Handler ownership"
  below.
- **Library mode** — `import otto` configures nothing beyond the
  `NullHandler`; an embedding process opts in with one call, and undoes
  it with {func}`otto.logger.reset <otto.logger.management.reset>`. The call
  is {func}`otto.session.install_logging`, which applies the projects'
  {ref}`[logging.levels] <logging-levels>` noise-floor table as the CLI does,
  or the raw primitive underneath it,
  {func}`otto.logger.install <otto.logger.management.install>`, which knows
  nothing of projects. See {doc}`the library page <../../extend/python-library>`
  for the embedder API.

### Handler ownership

Every handler otto attaches to root carries a marker attribute
(`otto.logger.management.OTTO_HANDLER_ATTR`); otto detaches only its own
marked handlers, on re-install or
{func}`reset() <otto.logger.management.reset>`. A foreign root handler —
pytest's caplog/`log_cli` capture, an embedder's own — is never touched and
keeps receiving records by propagation alongside otto's sinks. This is what
lets postures 2 and 3 coexist with otto's own handlers rather than fighting
them for root.

## Retrieved logs are a different tree

The three sinks above are what *otto* emits. Logs pulled off the hosts — a
product's own `get_logs` output, a product's `debug_log_globs`, a host's
`debug_log_globs` — are not records at all and never enter the queue; they
are files, and they land under the run directory at paths keyed by host and
then product. {ref}`The run tree <run-tree>` is the contract, and
`otto.layout` is its single implementation: the coverage pipeline builds
its own paths from the same module, which is what keeps the two trees the
same shape below `<kind>/<host_id>/`.

## Where the code lives

- `otto.layout` — the pure run-tree path builders and the product-name
  rule, shared by the host layer and the coverage pipeline
- {mod}`otto.logger.management` — `install_console`/`install_sinks` (root
  handler wiring, marked-handler ownership), the `QueueListener`, the
  exception-capturing `QueueHandler`, the console handler,
  `record_command_failure`, and time-boxed log rotation
- {mod}`otto.logger.formatters` — `RichFormatter` (markup rendering of an
  opted-in record for the files), `capture_exception`, `render_traceback` (the files' one traceback
  renderer) and the traceback bounds
- {mod}`otto.logger.relay` — `log_relayed`, otto's markup prefix plus relayed
  text whose brackets every sink keeps
- {mod}`otto.logger.mode` — `LogMode` and `effective_mode`, the
  most-restrictive-wins composition
