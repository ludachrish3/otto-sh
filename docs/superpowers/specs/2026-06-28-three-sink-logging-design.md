# Three-sink logging with per-command `LogMode`

**Date:** 2026-06-28
**Status:** Design — awaiting implementation plan
**Area:** `otto.logger`, `otto.host` (command logging), `otto.cli.main`

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
- Minimal call-site churn: the common path (a plain `run()`) is unchanged.

## Non-goals

- Capturing decorative/ephemeral console chrome (banner, `--show-lab` dump,
  `--list-*`, `--version`, the `Output directory:` footer) into the log files.
  These deliberately stay direct `CONSOLE.print`/`rprint` writes (see §5).
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
acts as `QUIET`, see §6). The session executes against this single effective
mode.

The **level axis** (INFO vs DEBUG) is *not* a parameter — it is chosen natively
at the log call (`logger.info(...)` vs `logger.debug(...)`). Normal command I/O
logs at INFO; framework/session diagnostics log at DEBUG.

### Reclassifying today's `log=False` / suppression sites

| Site | Today | New disposition |
|---|---|---|
| `send(pw, log=False)` — su password ([privilege.py:53](../../../src/otto/host/privilege.py)) | hard drop | **`NEVER`** (secret) |
| embedded console hex load ([embedded_host.py:621,624](../../../src/otto/host/embedded_host.py)) | hard drop | **`NEVER`** (bulky, useless even when debugging) |
| `file_ops` read body ([file_ops.py:114](../../../src/otto/host/file_ops.py)) | hard drop | **`QUIET`** (recorded in verbose, off console) |
| `cat /proc/modules` lsmod ([unix_host.py:717](../../../src/otto/host/unix_host.py)) | hard drop | **`QUIET`** |
| `host.log = False` — monitor daemon ([monitor/factory.py:42](../../../src/otto/monitor/factory.py)) | filter-drop everywhere | **`NEVER`** (per-host; keeps long-running daemon out of verbose.log) |
| `SuppressCommandOutput` blocks, `LocalHost(log=False)` config probe ([repo.py:620,628](../../../src/otto/configmodule/repo.py)) | filter-drop everywhere | **`QUIET`** |

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
notice from `rprint` to `logger.info` ([main.py:371](../../../src/otto/cli/main.py))
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
handler at [main.py:288](../../../src/otto/cli/main.py); we exclude `verbose.log`).

## `NEVER` — redaction at the source

`NEVER` is enforced where content originates, not by a handler filter, so no
secret-bearing `LogRecord` is ever created (a filter could be bypassed by a
future handler added without it):

- `_log_command` / `_log_output` are skipped for the command.
- The session output sink is `_drop_output`.
- The session's **content-bearing DEBUG diagnostics** emit a redacted
  placeholder instead of the raw bytes:
  - framed-write line ([session.py:422](../../../src/otto/host/session.py)) →
    `framed write cmd=<redacted> payload=<redacted 1048576 bytes>`
  - begin-marker chunk dump ([session.py:474](../../../src/otto/host/session.py))
  - buffer preview ([session.py:497](../../../src/otto/host/session.py))

For this, the **effective `LogMode` must thread `run` → `run_cmd` → session** so
the session knows to redact. The byte count in the placeholder is safe to show
(length is not the secret).

## CLI flag changes

- **`--rich-log-file`** (existing) now governs **both** files (strip ANSI from
  each unless set). No per-file variant.
- **`--verbose` / `-v` → `--show-time`.** Its sole remaining job is toggling the
  live console's timestamps. (`console.log` / `verbose.log` are always
  timestamped regardless.)
- **New `--lab-depth INT`** (default `3`, `0` = unlimited) takes over the
  `--show-lab` display-depth control that `--verbose` incidentally owned
  ([main.py:384](../../../src/otto/cli/main.py)).

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
- **`console.log` faithfulness test:** assert it equals the captured console
  content (modulo always-on timestamps and ANSI stripping) and that the
  `acting as` notice is present while the `--show-lab` dump is absent.
- **`--rich-log-file` test:** both files ANSI-stripped when unset, retained when
  set.
- **CLI flag tests:** `--show-time` toggles console timestamps only; `--lab-depth`
  controls `--show-lab` depth; `--verbose` is gone.

## Migration / churn summary

1. New `LogMode` enum (likely `otto.logger.levels` or `otto.host.host`).
2. Per-command signature + `ShellCommand.log` type → `LogMode`; per-host `log`
   field → `LogMode`; effective-mode composition helper.
3. `if log:` truthiness checks → `LogMode` comparisons; session sink/redaction
   branch keyed on effective mode; session diagnostics redaction for `NEVER`.
4. `management.py`: add the `verbose.log` handler with its INFO-floor level rule;
   set the logger level to the most-verbose floor; rename `otto.log` →
   `console.log`; restrict the console-suppress filter to console + console.log.
5. `cli/main.py`: `--verbose` → `--show-time`; new `--lab-depth`; promote the
   `acting as` notice to `logger.info`.
6. Reclassify the ~6 `log=False` / `host.log=False` call sites per the table.
7. Update tests/fixtures for the file rename and `LogMode`.

## Open / deferred

- Capturing console chrome (banner, `Output directory:` footer) into the logs by
  routing through the logger — explicitly out of scope; can be a later
  enhancement if a complete console transcript is ever wanted.
- Whether `--show-time` keeps `-v` as an alias — minor; decide during
  implementation.
