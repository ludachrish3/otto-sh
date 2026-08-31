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
| `console.log` | `--log-level` | a *faithful transcript* of the console — same records, always timestamped |
| `verbose.log` | INFO floor, DEBUG when `--log-level DEBUG` | the everything-record, including what the console suppressed |

Handlers hang off a `QueueListener`, so slow file I/O (e.g. logs on NFS)
never blocks the event loop, and old run directories are pruned under a
time-boxed budget so rotation cannot stall startup on slow mounts.

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
from ANY logger in the process — otto's own, a repo's product code, a suite
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
  `NullHandler`; an embedding process opts in with one call,
  {func}`otto.logger.install <otto.logger.management.install>`, and undoes
  it with {func}`otto.logger.reset <otto.logger.management.reset>`. See
  {doc}`the library page <../../library/index>` for the embedder API and
  {ref}`[logging.levels] <logging-levels>` for the noise-floor table both
  the CLI and `install()` apply.

### Handler ownership

Every handler otto attaches to root carries a marker attribute
(`otto.logger.management.OTTO_HANDLER_ATTR`); otto detaches only its own
marked handlers, on re-install or
{func}`reset() <otto.logger.management.reset>`. A foreign root handler —
pytest's caplog/`log_cli` capture, an embedder's own — is never touched and
keeps receiving records by propagation alongside otto's sinks. This is what
lets postures 2 and 3 coexist with otto's own handlers rather than fighting
them for root.

## Where the code lives

- {mod}`otto.logger.management` — `install_console`/`install_sinks` (root
  handler wiring, marked-handler ownership), the `QueueListener`, and
  time-boxed log rotation
- {mod}`otto.logger.mode` — `LogMode` and `effective_mode`, the
  most-restrictive-wins composition
