# Results, exit codes, and logging

Two cross-cutting spines run through every host verb: what it *returns*
(the {mod}`otto.result` family) and what it *emits* (the three-sink logging
model). Both are deliberately small and uniform.

## The Result family

Every host verb returns a member of one family in {mod}`otto.result`:

- {class}`~otto.result.Result` — status + optional payload (`value`) + human
  diagnostic (`msg`). Truthiness follows {attr}`~otto.result.Result.is_ok`
  (Success or Skipped), never the payload — `if result:` always asks "did it
  work?".
- {class}`~otto.result.CommandResult` — one shell command: adds the `command`
  string and the shell `retcode` (`-1` means the command never ran).
- {class}`~otto.result.Results` — the aggregate `run()` returns: a `Result`
  that is also a `Sequence[CommandResult]`. Its status is the first non-ok
  entry's status; `only` asserts exactly one command ran and returns it;
  `first_failure` finds the culprit in a batch. Transfer verbs aggregate
  per-file results the same way.

The shared vocabulary is {class}`~otto.utils.Status`: `Success`, `Failed`,
`Error`, `Unstable`, `Skipped`.

### Exit codes

CLI exit codes are *derived from* results — there is no separate exit-code
logic to drift. `Result.exit_code` is `0` when ok, else the status value.
`CommandResult.exit_code` follows the ssh convention users already know:

| Situation | Exit code |
| --- | --- |
| Command succeeded | `0` |
| Command ran and failed | the shell's own `retcode` |
| Command never ran (connection/timeout) | `255` |
| Failed without a retcode | the `Status` value |

A `@cli_exposed` host verb returning any `Result` gets these semantics on the
CLI for free; returning a plain value exits `0`.

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
hosts to `NEVER` ({doc}`monitoring-and-coverage`) without hiding real
problems.

## otto as a library citizen

`otto.logger` attaches only a `NullHandler` to the `'otto'` logger —
importing otto never configures logging; the handler topology above is
strictly CLI-side (`otto.logger.management` never imports `otto.context`, and
nothing in the library configures handlers). The reverse direction is also
covered: `capture_external_loggers` routes named third-party logger trees
(product code using `logging.getLogger(__name__)`) into otto's sinks, so
suite and instruction logs land in the same transcript as otto's own.
