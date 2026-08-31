# Logging: root capture — design

**Date:** 2026-08-30
**Status:** approved by Chris (sections 1–3 in conversation)
**Supersedes:** the prefix-capture half of the three-sink logging design
(`2026-06-28-three-sink-logging-design.md` — the three sinks themselves are unchanged);
the per-item collection capture added by `2026-08-30-suite-pytest-native-design.md` §5.5.

## 1. Goal

Logs from ANY code running during an otto invocation — otto itself, a repo's product
code, test suites, and arbitrary third-party libraries — appear through otto's logging,
while every emitter stays otto-agnostic: `logging.getLogger(__name__)` and nothing else.
This is the standard Python logging model (libraries emit, the application configures the
root logger once); otto currently inverts it with a capture allowlist, and that allowlist
is the source of an entire class of bugs (swallowed `lab_free` warnings, invisible suite
modules, the `tests`-package over-capture). The public logging interface should land in
its final shape now and stop churning.

## 2. Decisions (locked, do not re-litigate)

- **Root funnel.** The CLI configures handlers on the ROOT logger. The `otto` logger
  returns to being an ordinary library logger: `propagate = True`, no handlers beyond the
  import-time `NullHandler`.
- **Postures.** (1) `otto` CLI: otto owns the process → configures root. (2) Inner pytest
  sessions under `otto test`: same process, already configured; pytest's own logging
  plugin coexists. (3) Library mode: `import otto` configures nothing; embedders opt in
  with one public call, `otto.logger.install()`.
- **Noise floor = option A.** Hardcoded per-library default levels in otto, overridable
  per logger in `settings.toml` `[logging.levels]`.
- **Hard cutover.** `[logging] capture` leaves the settings model in the same commit — an
  old config fails validation loudly, naming `[logging.levels]` and the root-capture
  change. No deprecation shim (no users are that deep yet; a deprecation stance comes
  later as policy).
- **No location-based capture.** Pathname/per-record filtering stays rejected
  (per-statement cost, complexity). Root propagation needs none of it.
- **One knob per purpose.** `--log-level` governs otto's own verbosity and the sink
  levels; `[logging.levels]` governs which third-party records enter the funnel. They do
  not interact.

## 3. The funnel

### 3.1 Installer, split in two

`otto.logger.management` keeps its role (the application side); its entry point splits:

- **`install_console(log_level, show_time=...)`** — runs in the CLI main callback as soon
  as root options are parsed, BEFORE any project gate or lab probe that might want to
  warn. Effects: a `RichHandler` (today's console handler, unchanged construction) is
  attached to the ROOT logger; root's level is set to the verbose floor
  (`verbose_floor(log_level)`); the noise-floor table (§4) is applied. From this moment,
  `logger.warning(...)` from anywhere in the process is visible on the console.
- **`install_sinks(output_dir)`** — runs from `create_output_dir` (unchanged timing).
  Replaces the direct console handler with today's queue fan-out: one `QueueHandler` on
  root, a `QueueListener` fanning to the console handler + `console.log` (at
  `--log-level`, with the console's suppress filters copied) + `verbose.log` (at the
  verbose floor, keeping QUIET records). Identical sink semantics to today; only the
  logger they hang on changes (root instead of `otto`).

`init_cli_logging` remains as the CLI-facing wrapper that stores state and calls
`install_console`; `create_output_dir` calls `install_sinks`. Both are idempotent.

### 3.2 Handler ownership

Otto removes ONLY handlers it installed. Every handler otto puts on root carries a marker
attribute (`handler._otto_installed = True`, set by the installers); `install_sinks` and
`reset()` remove exactly the marked ones. Foreign root handlers — pytest's caplog /
`log_cli` capture, an embedder's own — are never touched and keep receiving records
synchronously via propagation. (This is today's discipline at
`management._add_log_handlers`, moved to root and made explicit by the marker instead of
identity checks.)

### 3.3 Levels

- Root logger level = verbose floor (INFO; DEBUG at `--log-level DEBUG`) — the process-wide
  entry gate.
- Sink levels unchanged: console and `console.log` at `--log-level`; `verbose.log` at the
  verbose floor.
- The `otto` logger's own level: NOTSET (inherit root). No forced levels anywhere except
  the noise-floor table (§4).

### 3.4 Library citizenship

- Import time: unchanged — `otto/__init__.py` attaches the `NullHandler` to the `otto`
  logger; nothing touches root, no levels change.
- `otto.logger.install(log_level="INFO", output_dir=None, show_time=False, overrides=None)`
  — the public embedder entry: `install_console` (+ `install_sinks` when `output_dir` is
  given), applying the default noise floor merged with `overrides` (a `dict[str, str]`
  mirroring the TOML table). Documented on the library page; the CLI path is this same
  machinery.
- `reset()` restores root exactly as found (marked handlers off, root level back to its
  prior value, noise-floor levels back to NOTSET — the state records what it set, as
  `captured_prefixes` does today).

### 3.5 Deleted with this design

`set_capture_prefixes`, `capture_external_loggers`, `_LogConfig.capture_prefixes` /
`captured_prefixes`, `Repo.product_log_prefixes()`, `Repo.logging_capture`, the
`[logging] capture` settings field, and the logger-capture half of
`OttoOptionsPlugin.pytest_collection_modifyitems` (the ensure-validation half stays; the
tach edge `otto.suite → otto.logger` is removed again if nothing else in `otto.suite`
imports `otto.logger` — verify, hand-edit, never `tach sync`).

## 4. The noise floor: `[logging.levels]`

### 4.1 Defaults

```python
# otto/logger/management.py
DEFAULT_LIBRARY_LEVELS: dict[str, str] = {...}  # seeded empirically, see below
```

Seeding is an implementation step, not guesswork: run the bed-free suites and a scaffold
`otto test` at `--log-level DEBUG`, record which third-party loggers actually emit
through otto's dependency tree (asyncssh is the known offender), and list exactly those
at WARNING. A library never observed logging gets no entry.

### 4.2 Override surface

```toml
[logging.levels]
asyncssh = "DEBUG"        # unmute for an SSH debugging session
noisy_vendor_sdk = "ERROR"
```

Semantics:

- Applied at `install_console` time as plain `logging.getLogger(name).setLevel(level)`.
  Nothing else — no handlers, no filters.
- User entries override otto's defaults per name. Unlisted loggers inherit root: captured
  by default; the table only quiets or un-quiets.
- Sink levels still apply downstream: the table decides what ENTERS the funnel, the sinks
  decide what SHOWS. `asyncssh = "DEBUG"` surfaces on the console only under
  `--log-level DEBUG`.
- Values validated against the standard level names plus otto's WARN/CRIT aliases
  (`otto.logger.levels`). Unknown LOGGER names are accepted (pre-quieting an
  about-to-be-installed library is legitimate); unknown LEVEL names are a validation
  error.
- Names equal to or under `otto` are rejected at validation ("otto's own verbosity is
  --log-level's job").
- Multi-repo merge: union of tables; two repos setting the SAME logger to DIFFERENT
  levels is a validation error naming both repos and the logger. Same level twice is
  fine.
- `install(overrides=...)` merges the same way over the defaults for embedders.

### 4.3 Hard cutover for `[logging] capture`

The field is removed from the settings model. Validation of a config still carrying it
fails with a message stating: capture is now automatic (root logging), per-library levels
live in `[logging.levels]`, and the doc anchor to read. The error must name the file that
carried the key.

## 5. Surfaces

### 5.1 CLI commands

`ensure_cli_session` calls `install_console` early enough that every project gate,
inventory preflight and lab probe can `logger.warning` visibly — including `lab_free`
groups, whose NullHandler-swallowed-warning failure mode this closes. The deliberate
`typer.echo` workarounds that exist only because logging was not yet configured
(`src/otto/cli/invoke.py` project-gate note, `src/otto/inventory/cache.py` notes) are
revisited one by one: each becomes a plain `logger.warning` IFF the early installer
provably precedes it on every call path (verified per site during implementation, with
the call-order evidence in the task report); a genuinely pre-logging print stays a print
with its comment updated to say why. `attach_console_suppress_filter` (`HostFilter`,
`LogMode`) is unchanged — handler-side, logger-agnostic.

### 5.2 Suites (`otto test`)

The inner pytest session shares the process; root is already configured. A suite module's
`logging.getLogger(__name__)` — and every library a suite imports — propagates with no
plugin support. pytest's caplog in the inner session keeps working (foreign root handlers
untouched). The writing-suites Logging paragraph shrinks to: put
`logger = logging.getLogger(__name__)` at the top of the file; everything that logs
reaches otto's console and log files.

### 5.3 Instructions and product code

`init` roots, `libs` packages, vendor SDKs: captured by propagation. No registration
surface remains anywhere in repo config.

### 5.4 Monitor / lifecycle / embedded

`shutdown_listener` (bounded flush, force-path semantics) unchanged. The Zephyr journald
window and monitor capture are outside the logging module and unaffected.

### 5.5 Otto's own test suite

The CliRunner capture guard currently pre-sets `otto.propagate = False`
(`tests/unit/cli/conftest.py`); it is re-expressed as "no otto-marked handlers on root"
(and `reset()` in teardown), keeping CliRunner streams clean without bending the
production posture. The known `log_cli`-closes-CliRunner-stream hazard (CI flake #110) is
unchanged but re-documented beside the guard.

## 6. Public interface after this change (the whole surface)

- Emitters, everywhere: `logging.getLogger(__name__)`. Nothing else. No otto import.
- `--log-level` (+ `--show-time`, `--rich-log-file`, `--log-days`): unchanged CLI knobs.
- `settings.toml` `[logging.levels]`: the one new key; `[logging] capture`: removed.
- `otto.logger.install()` / `otto.logger.management.reset()`: the embedder pair.
- The three sinks and their level rules: unchanged and documented where they are today.

## 7. Testing

Each load-bearing behaviour ships with a test AND a named mutation that must red it
(mutate, observe the exact failure, restore, re-green — recorded in the task report):

1. **Zero-registration capture:** a logger otto has never heard of (`fake_vendor.sub`)
   emits INFO during a run → the record reaches the sinks (assert on `verbose.log`
   content or a captured console). Mutation: skip the root handler install → red.
2. **Noise floor:** with defaults, a `DEFAULT_LIBRARY_LEVELS` logger's INFO does not
   enter the funnel even at `--log-level DEBUG`; `[logging.levels]` unmutes it; an
   `otto.x` name and a bad level each fail validation; conflicting repos error names
   both repos and the logger. Mutation: drop the table application → red.
3. **Hard cutover:** a config with `[logging] capture` fails validation; the message
   names `[logging.levels]` and the carrying file. Mutation: re-accept the key → red.
4. **Early console:** a `logger.warning` emitted after `install_console` but before
   `create_output_dir` reaches the console handler (the lab_free regression, now
   testable). Mutation: move the noise/console install back to sink time → red.
5. **Ownership/idempotence:** double `install_console` adds no duplicate handler;
   `reset()` restores root to its prior state exactly (level and handler set); a foreign
   root handler (stand-in for caplog) receives records and survives install/reset.
   Mutation: remove the marker check → red.
6. **Surfaces end-to-end:** the inner-session suite-module capture test is REWRITTEN to
   assert propagation with the collection hook deleted; an embedder round-trip
   (`install()` → emit → `reset()`) in a subprocess-free unit test; `test_library_usage`
   extended: import touches neither root handlers nor any logger level.

## 8. Documentation

One home per topic, links elsewhere:

- `docs/guide/configuration/settings.md` (or the settings reference page): the
  `[logging.levels]` table — semantics bullets from §4.2, cutover note.
- The library page: `otto.logger.install()` / `reset()` and the three-posture rule.
- `docs/library/writing-suites.md`: the Logging paragraph shrinks to the two-sentence
  version (§5.2); the package-layout caveat sentence added by the pytest-native work is
  deleted (no longer true — there is no prefix logic to be surprised by).
- Sweep: every mention of `[logging] capture` and `product_log_prefixes` in docs and
  docstrings (grep exit criterion:
  `grep -rn 'logging\] capture\|logging_capture\|product_log_prefixes\|capture_external_loggers\|set_capture_prefixes' src docs tests README.md`
  → zero hits outside this spec and the superseded spec, which are historical records).

## 9. Out of scope / follow-ups

- The `suite_options`-on-a-plain-class error message (tracked separately; not logging).
- Any change to `LogMode`/`HostFilter` semantics, the sink set, rotation, or
  `shutdown_listener`.
- A general deprecation policy (Chris: wanted soon, not yet).
