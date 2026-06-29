# Three-sink logging with per-command `LogMode`

**Date:** 2026-06-28
**Status:** Design — awaiting implementation plan
**Area:** `otto.logger`, `otto.host` (command logging), `otto.cli.main`,
`otto.configmodule` (product-prefix capture)

## Problem

otto currently emits a single per-run log file (`otto.log`) that is fanned
together with the live console through one `QueueListener`. There is no clean
separation between "what the user saw on the console" and "the full record of
what happened." The only knob for hiding command output — the per-command
`log=False` boolean — is overloaded: it is a hard *drop-at-source* used for
three unrelated reasons (a secret password, a megabyte hex payload, and merely
noisy-but-harmless output), so content that is genuinely useful for a post-hoc
investigation is discarded with no way to recover it.

We want three distinct log sinks and a disposition model expressive enough to
route each line to the right ones.

## Goals

- **Three sinks:** the live console, `console.log` (a faithful transcript of the
  console), and `verbose.log` (an "everything" record).
- A per-command/per-host **disposition** that cleanly expresses "show
  everywhere," "console-quiet but recorded," and "never record anywhere."
- Keep genuine secrets (su password) and bulky-useless content (embedded console
  hex) out of *every* file, at *every* level — including framework DEBUG
  diagnostics.
- **Generic library logging:** product/lab code uses a plain
  `logging.getLogger(__name__)` (no otto import) and is captured into otto's log
  files, without dragging in third-party noise.
- Minimal call-site churn: the common path (a plain `run()`) is unchanged.

## Non-goals

- Capturing decorative/ephemeral console chrome (banner, `--show-lab` dump,
  `--list-*`, `--version`, the `Output directory:` footer) into the log files.
  These deliberately stay direct `CONSOLE.print`/`rprint` writes (see
  *`console.log` — faithful console transcript* below).
- Changing the per-run output-dir layout, retention/rotation, or the
  `QueueListener` non-blocking design.
- Reworking how `CommandFrame` strips its BEGIN/END sentinels / echoed command /
  retcode scaffolding — that scaffolding remains genuinely always stripped
  (it never reaches a logger today and will not under this design).

## The disposition model: `LogMode`

A small enum captures the disposition of a command's logged I/O. Ordering is
**least → most restrictive**: `NORMAL < QUIET < NEVER`.

```python
class LogMode(Enum):
    NORMAL = "normal"   # logged at the call's native level, shown everywhere
    QUIET  = "quiet"    # console-suppressed (console + console.log); kept in verbose.log
    NEVER  = "never"    # redacted from every sink at every level, incl. session diagnostics
```

Two places carry a mode:

- **Per-command** — `run` / `oneshot` / `send` / `run_cmd` and the
  `ShellCommand.log` field. Parameter type becomes `LogMode`, default
  `LogMode.NORMAL`. Because the default is `NORMAL`, every call that does not
  pass `log=` is untouched.
- **Per-host** — `UnixHost.log`, `LocalHost.log`, `EmbeddedHost.log`,
  `RemoteHost.log`. The standing per-host flag is promoted from `bool` to
  `LogMode` so a host can be persistently quiet (`QUIET`) or fully silent
  (`NEVER`, used by the monitor daemon).

**Effective mode** for a given command is the *most restrictive* of the
per-host mode and the per-command mode (plus the global suppression flag, which
acts as `QUIET`, see *Filters and suppression machinery*). The session executes against this single effective
mode.

The **level axis** (INFO vs DEBUG) is *not* a parameter — it is chosen natively
at the log call (`logger.info(...)` vs `logger.debug(...)`). Normal command I/O
logs at INFO; framework/session diagnostics log at DEBUG.

### Scope: `LogMode` governs command I/O only

`LogMode` (per-command **and** per-host) gates **only the host's command I/O** —
the echo/output records emitted by `_log_command`/`_log_output`, which are the
only records that carry the `extra={"host": self}` tag
([host.py:955,967](../../../src/otto/host/host.py)). It is **not** a global mute
for a host.

Everything else — `logger.warning`/`logger.error` (whether framework-emitted such
as connection failures and timeouts, or the monitor's own failure warnings at
[collector.py:390+](../../../src/otto/monitor/collector.py)), and any non-command
INFO/DEBUG record — carries no host tag and is therefore **never** suppressed by a
host's or command's `LogMode`. Such records always reach every sink, subject only
to `--log-level`.

Consequence for the monitor: `host.log = NEVER` strips the host's routine polling
*chatter* everywhere, but a monitored host that hits a problem mid-run still
produces captured WARNING/ERROR records. (No special `Status.Error` handling is
added; the monitor already emits a warning when it detects a failure.)

### Reclassifying today's `log=False` / suppression sites

| Site | Today | New disposition |
|---|---|---|
| `send(pw, log=False)` — su password ([privilege.py:56](../../../src/otto/host/privilege.py)) | hard drop | **`NEVER`** (secret) |
| embedded console hex load ([embedded_host.py:657,661](../../../src/otto/host/embedded_host.py)) | hard drop | **`NEVER`** (bulky, useless even when debugging) |
| `file_ops` read body ([file_ops.py](../../../src/otto/host/file_ops.py)) | hard drop | **`QUIET`** (recorded in verbose, off console) |
| `cat /proc/modules` lsmod ([unix_host.py:721](../../../src/otto/host/unix_host.py)) | hard drop | **`QUIET`** |
| `host.log = False` — monitor daemon ([monitor/factory.py:42](../../../src/otto/monitor/factory.py)) | filter-drop everywhere | **`NEVER`** (per-host; strips routine chatter from verbose.log — warnings/errors still captured) |
| `SuppressCommandOutput` blocks, `LocalHost(log=False)` config probe ([repo.py:663,680](../../../src/otto/configmodule/repo.py)) | filter-drop everywhere | **`QUIET`** |

## Sink topology

All three sinks continue to fan through the existing `QueueListener`
(non-blocking I/O). The `'otto'` logger's own level is set to the *most-verbose
floor in effect* — `DEBUG` when `--log-level DEBUG`, else `INFO` — so that INFO
records still reach the queue even when the console is set quieter (e.g.
`--log-level WARNING`). Each handler then filters by its own level
(`respect_handler_level=True`, already set).

| Sink | Handler | Level | Console-suppress filter | Timestamp | Rich |
|---|---|---|---|---|---|
| **console** | `RichHandler` → `CONSOLE` | `--log-level` | **yes** | only when `--show-time` | live markup |
| **console.log** | `FileHandler` (was `otto.log`) | `--log-level` (mirrors console) | **yes** (faithful transcript) | **always** | stripped unless `--rich-log-file` |
| **verbose.log** | `FileHandler` (new) | `DEBUG` if `--log-level DEBUG` else `INFO` | **no** (keeps `QUIET` + suppressed) | **always** | stripped unless `--rich-log-file` |

Consequences of the level rule:

- Default (`--log-level INFO`): console / console.log / verbose.log all floor at
  INFO. Framework DEBUG diagnostics appear in none of them.
- `--log-level WARNING`: console and console.log show WARNING+; verbose.log still
  floors at INFO (its "everything" value comes from the INFO floor + ignoring
  console-suppression, not from a lower level).
- `--log-level DEBUG`: all three floor at DEBUG; the framework/session DEBUG
  diagnostics appear, and `NEVER` content is *still* absent (redacted at source).

## `console.log` — faithful console transcript

`console.log` mirrors the console exactly: same `--log-level`, same
console-suppress filter. It differs from the console only in that it is **always
timestamped** and is **ANSI-stripped unless `--rich-log-file`**.

Crucially, `console.log` records **only what passes through the logger.**
Decorative/ephemeral console writes that bypass the logger
(`rprint`/`CONSOLE.print`) are *not* captured — by design (§Non-goals). The
distinction is:

- **Logged (captured):** command I/O (per `LogMode`); operational notices that
  affect what runs — the dry-run banner (already `logger.info`), the log-rotation
  notice, warnings/errors, and the `[reservations] acting as …` identity notice.
- **Direct console print (not captured):** the ASCII banner, the `--show-lab`
  pretty-dump, `--list-labs` / `--list-hosts`, `--version`, and the
  `Output directory:` exit footer.

**Required change:** promote the `[reservations] acting as <user> (--as-user)`
notice from `rprint` to `logger.info` ([main.py:400](../../../src/otto/cli/main.py))
so it lands in `console.log` and `verbose.log`. (`--show-lab` stays a direct
print and is explicitly *not* logged.)

## `verbose.log` — the "everything" record

`verbose.log` is defined precisely as:

> every record at the effective level floor (INFO, or DEBUG at
> `--log-level DEBUG`), **ignoring console-suppression** (`QUIET`, per-host
> `QUIET`, and global suppression), **minus the `NEVER` bucket.**

It therefore contains, by default (INFO floor):

- everything `console.log` contains, **plus**
- command I/O that was `QUIET` (console-decluttered) — e.g. file-body reads,
  `/proc/modules`, `SuppressCommandOutput` blocks — which the console hid.

It does **not** contain: `NEVER` content (password, embedded hex, frame
scaffolding), nor (by default) the framework DEBUG diagnostics — those require
`--log-level DEBUG`.

Implementation: `verbose.log`'s `FileHandler` is the one handler that does **not**
receive the console-suppress filter (today that filter is attached to *every*
handler at [main.py:318](../../../src/otto/cli/main.py); we exclude `verbose.log`).

## `NEVER` — redaction at the source

`NEVER` is enforced where content originates, not by a handler filter, so no
secret-bearing `LogRecord` is ever created (a filter could be bypassed by a
future handler added without it):

- `_log_command` / `_log_output` are skipped for the command.
- The session output sink is `_drop_output`.
- The session's **content-bearing DEBUG diagnostics** emit a redacted
  placeholder instead of the raw bytes:
  - framed-write line ([session.py:435](../../../src/otto/host/session.py)) →
    `framed write cmd=<redacted> payload=<redacted 1048576 bytes>`
  - begin-marker chunk dump ([session.py:487](../../../src/otto/host/session.py))
  - buffer preview ([session.py:505](../../../src/otto/host/session.py))

For this, the **effective `LogMode` must thread `run` → `run_cmd` → session** so
the session knows to redact. The byte count in the placeholder is safe to show
(length is not the secret).

`NEVER` is scoped to command I/O like the other modes (see *Scope: `LogMode`
governs command I/O only*): a `NEVER` host or command suppresses its own
echo/output and the matching session diagnostics, but never a `logger.warning`/
`logger.error` record.

## CLI flag changes

- **`--rich-log-file`** (existing) now governs **both** files (strip ANSI from
  each unless set). No per-file variant.
- **`--verbose` / `-v` → `--show-time`.** Its sole remaining job is toggling the
  live console's timestamps. (`console.log` / `verbose.log` are always
  timestamped regardless.)
- **New `--lab-depth INT`** (default `3`, `0` = unlimited) takes over the
  `--show-lab` display-depth control that `--verbose` incidentally owned
  ([main.py:415](../../../src/otto/cli/main.py)).

## Filters and suppression machinery

- **`HostFilter`** (renamed conceptually to a *console-suppress* filter) is
  attached to **console + console.log only**. It drops a host-tagged command
  record when the effective mode is `QUIET` *or* global
  `log_command_output` is `False`. `NEVER` never produces a record to filter.
- **`SuppressCommandOutput`**: the per-host form sets `host.log = LogMode.QUIET`
  (and restores); the global form continues to flip
  `OttoContext.log_command_output`. Both mean *console-quiet, kept in verbose* —
  consistent with `QUIET`.
- **Global `log_command_output`** stays a `bool` on `OttoContext` (it is a
  console-side switch; verbose.log ignores it).

## Library / external logger capture

### Problem

Product/lab code must currently import an otto accessor
(`from otto.logger import get_otto_logger`) to land in otto's files. If a product
author instead writes the idiomatic `logging.getLogger(__name__)`, the record
lives under a non-`otto.*` name (e.g. `repo1_instructions.*`), propagates to the
**root** logger — which the CLI does not configure — and is silently lost. The
ergonomic, generic pattern is exactly the one that doesn't work.

### Design — scoped capture (not root)

When running as the CLI **application**, otto attaches its single shared
`QueueHandler` (the one feeding the `QueueListener` → three sinks) to a *scoped
set* of logger roots, never globally to root:

1. `otto.*` — otto's own modules (as today).
2. **Auto-derived product prefixes** — the top-level package of each repo's
   `init` modules ([repo.py:551-559](../../../src/otto/configmodule/repo.py)) and
   the immediate sub-packages of each repo's `libs` directory
   ([repo.py:546-549](../../../src/otto/configmodule/repo.py)).
3. **Explicit `[logging] capture = [...]`** — a new repo-settings list for
   package roots that live outside `libs`.

For each captured prefix, otto:

- adds the shared `QueueHandler` to `getLogger(prefix)`, and
- sets `getLogger(prefix).setLevel(<most-verbose floor>)` so product `INFO`
  records are not dropped by root's default `WARNING` level.

A product record reaches exactly one `QueueHandler` (at its nearest captured
ancestor), so there is no double-emission. Third-party libraries (not in the
captured set) are **not** captured — keeping `verbose.log` free of
asyncssh/paramiko/docker wire noise even at `--log-level DEBUG`. Generic records
carry no `host` attribute, so the console-suppress filter passes them
(`host is None → True`) to all sinks at their level.

The **library-import path is unchanged**: only the CLI attaches. `import otto`
keeps the `NullHandler` on `'otto'` and touches no other logger, preserving the
good-citizen behavior established by the logger-standardization work.

### Ergonomics

- The blessed generic pattern for **all** code (otto, product, lab) is
  `log = logging.getLogger(__name__)`.
- `get_otto_logger()` is retained as optional sugar and re-exported as
  `otto.get_logger` for code wanting an explicit `otto.<name>` child; it is no
  longer required for capture.
- Optional, deferrable cleanup: otto's own 30 `get_otto_logger()` call sites may
  migrate to `getLogger(__name__)` (idiomatic, already captured under `otto.*`).
  Not required by this work.

### Timing note

Product modules create their logger at import time (`logger = getLogger(__name__)`
at module top). Because handlers are resolved at *emit* time, attaching the
`QueueHandler` to the prefix during the CLI callback — before product code runs —
is sufficient; the child logger finds the handler when it first emits.

## File rename and test churn

- `otto.log` → `console.log`; add `verbose.log` in the same per-run dir.
- Tests and helpers that reference `otto.log` or assert on the single-file
  topology need updating (`tests/unit/logger/*`, `tests/integration/logger/*`,
  `tests/unit/host/test_session_logging.py`, and any fixture reading the run dir).
- Per-command/per-host `log=True/False` literals in tests migrate to `LogMode`.

## Testing strategy

- **`LogMode` unit tests:** effective-mode composition (most-restrictive), and
  that `NORMAL`/`QUIET`/`NEVER` route to the expected sinks.
- **Sink topology tests:** at `--log-level` ∈ {WARNING, INFO, DEBUG}, assert the
  contents of console (captured), `console.log`, and `verbose.log` for a mix of
  NORMAL/QUIET commands and an INFO-vs-DEBUG framework line.
- **`NEVER` redaction tests:** drive a `NEVER` command at `--log-level DEBUG` and
  assert neither the payload bytes nor the buffer appear in any sink — only the
  `<redacted N bytes>` placeholder. Cover the su-password `send` path and the
  embedded-hex `run_cmd` path.
- **Scope guarantee test:** a host set to `NEVER` (monitor case) suppresses its
  command echo/output, but a `logger.warning`/`logger.error` emitted during that
  host's run still appears in console, console.log, and verbose.log.
- **`console.log` faithfulness test:** assert it equals the captured console
  content (modulo always-on timestamps and ANSI stripping) and that the
  `acting as` notice is present while the `--show-lab` dump is absent.
- **`--rich-log-file` test:** both files ANSI-stripped when unset, retained when
  set.
- **CLI flag tests:** `--show-time` toggles console timestamps only; `--lab-depth`
  controls `--show-lab` depth; `--verbose` is gone.
- **Library-capture tests:** a synthetic product package under a temp `libs` dir
  logs via `getLogger(__name__)`; assert its `INFO` record reaches `console.log`
  and `verbose.log`. A third-party-style logger (`getLogger("asyncssh")`) at
  `INFO` is **not** captured. `[logging] capture = ["extra_pkg"]` adds an
  otherwise-undiscovered package. Importing otto as a library attaches nothing
  beyond the `'otto'` `NullHandler`.

## Migration / churn summary

1. New `LogMode` enum (likely `otto.logger.levels` or `otto.host.host`).
2. Per-command signature + `ShellCommand.log` type → `LogMode`; per-host `log`
   field → `LogMode`; effective-mode composition helper.
3. `if log:` truthiness checks → `LogMode` comparisons; session sink/redaction
   branch keyed on effective mode; session diagnostics redaction for `NEVER`.
4. `management.py`: add the `verbose.log` handler with its INFO-floor level rule;
   set the logger level to the most-verbose floor; rename `otto.log` →
   `console.log`; restrict the console-suppress filter to console + console.log;
   add the scoped-capture attach (shared `QueueHandler` onto `otto.*` + product
   prefixes, with per-prefix level).
5. `cli/main.py`: `--verbose` → `--show-time`; new `--lab-depth`; promote the
   `acting as` notice to `logger.info`; derive + pass product prefixes (from the
   repos' `libs`/`init` + `[logging] capture`) into the capture attach.
6. `configmodule`: parse the new `[logging] capture` setting onto `Repo`; expose
   the derived product-prefix set.
7. `otto.logger`: re-export `get_otto_logger` as `otto.get_logger`.
8. Reclassify the ~6 `log=False` / `host.log=False` call sites per the table.
9. Update tests/fixtures for the file rename, `LogMode`, and library capture.

## Open / deferred

- Capturing console chrome (banner, `Output directory:` footer) into the logs by
  routing through the logger — explicitly out of scope; can be a later
  enhancement if a complete console transcript is ever wanted.
- Whether `--show-time` keeps `-v` as an alias — minor; decide during
  implementation.
