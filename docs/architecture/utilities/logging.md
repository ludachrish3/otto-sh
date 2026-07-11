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
hosts to `NEVER` ({doc}`../lifecycles/monitor`) without hiding real
problems.

## otto as a library citizen

A bare `import otto` attaches only a `NullHandler` to the `'otto'` logger —
importing otto never configures logging; the handler topology above is
strictly CLI-side (`otto.logger.management` never imports `otto.context`, and
nothing in the library configures handlers). otto's own modules emit via
`logging.getLogger(__name__)` — module-qualified children of `'otto'` that
propagate up to it — the same stdlib idiom recommended for library consumers;
there is no otto-specific logger accessor. The reverse direction is also
covered: `capture_external_loggers` routes named third-party logger trees
(product code using `logging.getLogger(__name__)`) into otto's sinks, so
suite and instruction logs land in the same transcript as otto's own.
