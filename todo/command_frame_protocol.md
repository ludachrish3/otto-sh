# CommandFrame protocol — pluggable shell command framing

## Motivation

otto drives a remote shell by wrapping each command in sentinels — a BEGIN
marker, the command, a way to recover the exit code, and an END marker — then
parsing the echoed stream back into `(output, retcode)`. That "dialect" differs
per shell: bash bakes `$?` into the END marker; the Zephyr RTOS shell has no
`$?` and appends a stock `retval` command; a Zephyr **2.7** target has neither
(`retval` only exists from 3.x — see `command_frame` notes below).

Today that dialect lives as a set of overridable methods on `ShellSession`
(`_frame_command`, `_handshake_command`, `_recover_command`, `_marks_begin`,
`_parse_output`, `_extract_retcode`, `_end_pattern`), with bash as the base
implementation and `ZephyrSession(TelnetSession)` overriding them. The seam
works, but it's bound to the **transport** inheritance chain:

- `SessionManager` picks the dialect by swapping the *transport class*
  (`telnet_session_cls=ZephyrSession`).
- The SSH path hardcodes bash (`SshSession`), so Zephyr-framing-over-SSH is
  impossible even though framing and transport are independent.
- A new dialect (e.g. Zephyr 2.7's inline retcode) means a new transport
  subclass, and projects/libraries can't register one without subclassing
  otto internals.

**Goal:** promote the dialect into a first-class, composable, registrable
strategy — `CommandFrame` — that a session *holds* rather than *is*. This
mirrors the existing `EmbeddedFileSystem` + `register_filesystem` pattern.

## The protocol

A `CommandFrame` is a **stateless value object** (like `EmbeddedFileSystem`).
Per-session state (the unique markers, derived from the session id) stays on
the session and is passed in as a small `SessionMarkers` value object, so the
frame itself is pure and table-testable without a live session.

```python
@dataclass(frozen=True)
class SessionMarkers:
    begin: str       # __OTTO_<id>_BEGIN__
    end_prefix: str  # __OTTO_<id>_END__
    ready: str       # __OTTO_<id>_READY__
    recover: str     # __OTTO_<id>_RECOVER__

class CommandFrame(ABC):
    type_name: ClassVar[str]                 # registry key, e.g. "bash"
    # render half — command -> bytes to write
    def handshake(self, m: SessionMarkers) -> str: ...
    def frame(self, cmd: str, m: SessionMarkers) -> str: ...
    def recover(self, m: SessionMarkers) -> str: ...
    # parse half — bytes read -> structured result
    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]: ...
    def marks_begin(self, data: str, m: SessionMarkers) -> bool: ...
    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str: ...
    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int: ...
```

**Why render and parse are one protocol, not two.** They co-vary through
*where the retcode lives*: bash puts it in the END marker (`end_pattern`
captures `(\d+)`), Zephyr reads it from a `retval` line, the 2.7 form reads a
`retCode = %d` line. `end_pattern` + `extract_retcode` must be authored
together; splitting "framer" and "parser" into separate registrable protocols
would let mismatched halves combine. Keep them cohesive until a real reuse case
demands the split.

**Division of labor.** The session keeps transport I/O, the read/stream loop,
expect handling, recovery orchestration, marker generation, and the generic
READY-detection regex (already dialect-agnostic — Zephyr overrides only the
handshake *command*, not the readiness check). The frame owns the render
payloads, the parse trio, and the end pattern.

## Stock frames

- `BashFrame` (`type_name="bash"`) — today's `ShellSession` defaults. Used by
  `SshSession` / `TelnetSession` (unix) / `LocalSession`.
- `ZephyrFrame` (`type_name="zephyr"`) — today's `ZephyrSession` overrides.

A project/library registers more via `register_command_frame(name, frame)` from
its `.otto` init module — the same hook as `register_filesystem` /
`register_host_parsers`.

## Selection

- hosts.json gains an optional `"command_frame": "<name>"` field.
- Default it from `osType` (`unix`→`bash`, `embedded`→`zephyr`) so **no existing
  lab entry needs the field** — pure back-compat.
- `create_host_from_dict` resolves the string to a frame via
  `build_command_frame` and hands it to the host, which passes it into its
  `SessionManager`.

## Sequencing

This refactors a subtle, load-bearing component (the seam carries the
timeout-recovery and cancellation handling), so it's verified against the
existing unix + Zephyr 3.7/4.4 suites as the oracle. Per the "prioritise the
long-term API, not the diff size — otto has no users yet" decision, step 1 goes
straight to the clean end-state (no transitional `ZephyrSession` shim) rather
than a minimal-churn intermediate.

### Step 1 — extract the dialect, wire selection first-class (DONE)

1. New `src/otto/host/command_frame.py`: `SessionMarkers`, `CommandFrame`,
   `BashFrame`, `ZephyrFrame`, the registry (`register_command_frame`,
   `build_command_frame`).
2. `ShellSession` *composes* a frame: `__init__(command_frame=None,
   init_timeout=None)` → `self._frame = command_frame or BashFrame()`; builds
   `self._markers`; sets `self._end_pattern = self._frame.end_pattern(...)`. The
   old per-dialect seam methods are **removed** — the read loop / handshake /
   recovery call `self._frame.<hook>(self._markers)` directly.
3. `SshSession` / `TelnetSession` forward `command_frame` + `init_timeout`.
4. `ZephyrSession` is **deleted** (`src/otto/host/zephyr.py` removed). The
   Zephyr dialect is `ZephyrFrame`; the transport is a plain `TelnetSession`.
5. `SessionManager` takes `command_frame` + `init_timeout` (replacing
   `telnet_session_cls`) and hands them to every session it builds — SSH and
   telnet alike, so framing is fully decoupled from transport.
6. `EmbeddedHost` gains a `command_frame` field (default `ZephyrFrame`,
   string-coerced like `filesystem`) and passes it + the 15 s embedded
   readiness ceiling to its `SessionManager`. `create_host_from_dict` resolves
   the hosts.json `command_frame` string via `build_command_frame`.
7. Tests: new `test_command_frame.py` (frame value objects + registry, incl.
   first standalone `BashFrame` coverage); `test_zephyr.py` re-based on
   `TelnetSession + ZephyrFrame` with `TestFraming` re-pointed at `ZephyrFrame`
   directly; `test_unixHost.py` call-arg assertions updated for the new kwargs.

**Verified:** 1370 unit tests green; full unix integration matrix (ssh/telnet/
local) green; Zephyr **3.7 and 4.4** embedded contract + integration green.
(One non-portable test, `kernel threads` — renamed to `kernel thread` in 4.4 —
was switched to the version-stable `help`; this was a test-vocabulary bug
surfaced by the 4.4 host, not a framing regression: the frame parsed 4.4's
output cleanly with the correct retcode.) 2.7 deliberately excluded.

### Step 2 — the 2.7 inline-retcode frame (the differentiation experiment)

1. Firmware (2.7-only, deliberate deviation): patch `state_collect()` to stop
   discarding `execute()`'s return value and print `retCode = %d` instead.
   Delivered as `tests/firmware/zephyr/patches/v2_7-shell-retcode.patch`,
   applied in the Vagrantfile per-version workspace loop.
2. otto: `ZephyrInlineRetcodeFrame` (subclass of `ZephyrFrame`) — frame
   `BEGIN / cmd / END` (no `retval` line), parse `retCode = (-?\d+)`. Define it
   in the **test lab repo** and register it via that repo's init module, so the
   2.7 work doubles as proof of the project-defined extension path.
3. hosts.json: 2.7 entries set `"command_frame": "zephyr-inline"`.
4. Verify the full 9-backend matrix (2.7 included) green.

## Notes / open questions

- `_init_timeout` (Zephyr's 15 s readiness ceiling) is session-level timing, not
  framing — it stays on the session/transport, not the frame.
- Capability flags (`has_retval: false`) were considered and rejected as the
  primary mechanism: they collapse to a pile of conditionals and can't express a
  genuinely novel shell. A flag is a degenerate frame; the strategy object is the
  honest abstraction. (A frame *may* read a flag internally if useful.)
- Related TODO.md item: "strip out the return value and let it be a more silent
  aspect" — the frame is the natural home for that, since it already owns
  retcode extraction.
