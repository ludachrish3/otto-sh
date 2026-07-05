# Shell-liveness probe unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace three hand-rolled shell-liveness confirmations (session handshake, post-timeout recovery, post-login-proxy resync) with one shared, echo-proof, resend-until-deadline primitive.

**Architecture:** A new `otto/host/shell_liveness.py` owns the timing loop (`confirm_live`); each `CommandFrame` dialect owns the probe render + confirm pattern (bash gets an echo-proof `$?`-digit probe, Zephyr keeps its rejected-token probe). `_recover_session`, `_ensure_initialized`, and `_resync_shell` all call `confirm_live`. Fixes the 3.13 resync flake (flush-eaten first probe + fixed retry budget) and the foreseen I-3 recover false-positive (a REPL can't fake the digit form).

**Tech Stack:** Python 3.10+, asyncio, pytest / pytest-asyncio, ruff, ty.

## Global Constraints

- No `from __future__ import annotations` (trips Sphinx nitpicky docs gate). Use real 3.10+ annotations with module-top imports.
- `@override` (from `typing_extensions`) on every method that overrides a base — ty enforces it at the typecheck gate.
- Lint = `ruff check .` AND `ruff format --check .` (format is not lint-neutral; run format after edits, then re-check).
- Per-task gate: `uv run --no-sync python -m pytest <files> --no-cov -o addopts=""`. Full gate before Task 6 handoff: `make coverage` + `nox` typecheck + docs.
- Execution runs in a git worktree (self-commit OK). Commit messages: conventional prefix + trailer `Assisted-by: Claude Opus 4.8`.
- `confirm_live` deadline for resync = **10 s** (`probe_timeout ≈ 0.5 s`, `settle ≈ 0.3 s`); recover deadline = existing `_RECOVERY_TIMEOUT` (5 s), probe 0.5 s; handshake keeps `_init_timeout` / `_init_probe_interval`.
- The interim `_RESYNC_SETTLE`/`_RESYNC_TIMEOUT`/`_RESYNC_ATTEMPTS` knobs + the `(?<!echo )` lookbehind in `login_proxy.py` are retired by Task 5 (they are the thing being replaced).

---

### Task 1: `confirm_live` helper module

**Files:**
- Create: `src/otto/host/shell_liveness.py`
- Test: `tests/unit/host/test_shell_liveness.py`

**Interfaces:**
- Consumes: `otto.host.command_frame.SessionMarkers`.
- Produces:
  ```python
  async def confirm_live(
      send: Callable[[str], Awaitable[None]],
      expect: Callable[[re.Pattern[str], float], Awaitable[str]],
      render: Callable[[SessionMarkers], str],
      pattern: Callable[[SessionMarkers], re.Pattern[str]],
      new_markers: Callable[[], SessionMarkers],
      *, settle: float, probe_timeout: float, deadline: float,
  ) -> bool
  ```
  Returns `True` on first confirming match; `False` if the deadline elapses. Swallows per-probe `TimeoutError`/`asyncio.TimeoutError` and resends; lets other exceptions (e.g. `IncompleteReadError`) propagate.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_shell_liveness.py`:

```python
"""Unit tests for the shared shell-liveness confirmation loop."""

import asyncio
import re

import pytest

from otto.host import shell_liveness
from otto.host.command_frame import SessionMarkers
from otto.host.shell_liveness import confirm_live

_FIXED = SessionMarkers.for_session("cafef00d")


def _render(m: SessionMarkers) -> str:
    return f'probe {m.end_prefix}\n'


def _pattern(m: SessionMarkers) -> re.Pattern[str]:
    return re.compile(re.escape(m.end_prefix) + r"(\d+)__")


class _FakeIO:
    """send/expect fake: expect times out `fail_times` times, then matches."""

    def __init__(self, fail_times: int, sleep_on_fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self.calls = 0
        self._sleep_on_fail = sleep_on_fail

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def expect(self, pattern: re.Pattern[str], timeout: float) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            if self._sleep_on_fail:
                await asyncio.sleep(timeout)
            raise asyncio.TimeoutError
        return "matched"


def _fixed_markers() -> SessionMarkers:
    return _FIXED


@pytest.mark.asyncio
async def test_confirms_on_first_probe():
    io = _FakeIO(fail_times=0)
    ok = await confirm_live(
        io.send, io.expect, _render, _pattern, _fixed_markers,
        settle=0.0, probe_timeout=0.5, deadline=5.0,
    )
    assert ok is True
    assert len(io.sent) == 1


@pytest.mark.asyncio
async def test_resends_past_timeouts_then_confirms():
    io = _FakeIO(fail_times=2)
    ok = await confirm_live(
        io.send, io.expect, _render, _pattern, _fixed_markers,
        settle=0.0, probe_timeout=0.5, deadline=5.0,
    )
    assert ok is True
    assert len(io.sent) == 3  # two resends + the one that landed


@pytest.mark.asyncio
async def test_returns_false_when_deadline_elapses():
    io = _FakeIO(fail_times=999, sleep_on_fail=True)
    ok = await confirm_live(
        io.send, io.expect, _render, _pattern, _fixed_markers,
        settle=0.0, probe_timeout=0.02, deadline=0.08,
    )
    assert ok is False
    assert io.calls >= 1


@pytest.mark.asyncio
async def test_settles_before_first_probe(monkeypatch):
    io = _FakeIO(fail_times=0)
    events: list[str] = []

    async def _record_sleep(duration: float) -> None:
        events.append(f"sleep:{duration}")

    monkeypatch.setattr(shell_liveness.asyncio, "sleep", _record_sleep)
    orig_send = io.send

    async def _tracked_send(text: str) -> None:
        events.append("send")
        await orig_send(text)

    await confirm_live(
        _tracked_send, io.expect, _render, _pattern, _fixed_markers,
        settle=0.3, probe_timeout=0.5, deadline=5.0,
    )
    assert events[0] == "sleep:0.3"  # settle happens BEFORE the first send
    assert "send" in events


@pytest.mark.asyncio
async def test_fresh_markers_used_per_probe():
    io = _FakeIO(fail_times=1)  # one timeout, then match -> two probes
    seen: list[str] = []

    def _counting_markers() -> SessionMarkers:
        m = SessionMarkers.for_session(f"id{len(seen)}")
        return m

    def _render_track(m: SessionMarkers) -> str:
        seen.append(m.end_prefix)
        return f'probe {m.end_prefix}\n'

    await confirm_live(
        io.send, io.expect, _render_track, _pattern, _counting_markers,
        settle=0.0, probe_timeout=0.5, deadline=5.0,
    )
    assert seen == ["__OTTO_id0_END__", "__OTTO_id1_END__"]  # distinct per probe
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_shell_liveness.py --no-cov -o addopts="" -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.host.shell_liveness'`.

- [ ] **Step 3: Write the implementation**

Create `src/otto/host/shell_liveness.py`:

```python
"""Shared shell-liveness confirmation: prove a real shell is at its prompt.

One resend-until-deadline loop reused everywhere otto must confirm a shell is
responsive — the session readiness handshake, post-timeout recovery, and the
post-login-proxy-transition resync. Each caller supplies how to *render* a probe
and how to *recognize* its reply (delegated to the ``CommandFrame`` dialect),
plus a per-probe marker source; this module owns only the timing: settle, then
resend a probe on a short interval until confirmed or an overall deadline passes.

It lives below both ``session.py`` and ``login_proxy.py`` (``session.py`` imports
``login_proxy``, so this cannot live in ``session.py`` without a cycle) and
depends only on ``command_frame`` + asyncio.
"""

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable

from .command_frame import SessionMarkers


async def confirm_live(
    send: Callable[[str], Awaitable[None]],
    expect: Callable[[re.Pattern[str], float], Awaitable[str]],
    render: Callable[[SessionMarkers], str],
    pattern: Callable[[SessionMarkers], re.Pattern[str]],
    new_markers: Callable[[], SessionMarkers],
    *,
    settle: float,
    probe_timeout: float,
    deadline: float,
) -> bool:
    """Prove a real shell is at its prompt by probing until confirmed or timed out.

    Sleeps ``settle`` (absorbing any transition tty-flush), then repeatedly mints
    markers via ``new_markers``, sends ``render(markers)``, and waits up to
    ``min(probe_timeout, remaining)`` for ``pattern(markers)`` to match. Returns
    ``True`` on the first match, ``False`` if ``deadline`` elapses first. A
    per-probe timeout is swallowed and retried; other read errors propagate.
    """
    await asyncio.sleep(settle)
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while loop.time() < stop:
        markers = new_markers()
        await send(render(markers))
        remaining = stop - loop.time()
        if remaining <= 0:
            break
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await expect(pattern(markers), min(probe_timeout, remaining))
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_shell_liveness.py --no-cov -o addopts="" -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint**

Run: `uv run --no-sync ruff check src/otto/host/shell_liveness.py tests/unit/host/test_shell_liveness.py && uv run --no-sync ruff format src/otto/host/shell_liveness.py tests/unit/host/test_shell_liveness.py`
Expected: clean (format may reformat; re-run `ruff check` after).

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/shell_liveness.py tests/unit/host/test_shell_liveness.py
git commit -m "feat(host): shared confirm_live shell-liveness loop

Assisted-by: Claude Opus 4.8"
```

---

### Task 2: Frame API — `recover_pattern` + echo-proof bash recover probe

**Files:**
- Modify: `src/otto/host/command_frame.py` (add `recover_pattern` default to `CommandFrame`; change `BashFrame.recover` + add `BashFrame.recover_pattern`)
- Test: `tests/unit/host/test_command_frame.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `CommandFrame.recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]` — concrete default matching the bare `m.recover` token (so Zephyr and any third-party frame inherit today's behavior).
  - `BashFrame.recover(m)` now renders `echo "{m.end_prefix}$?__"\n` (exit-code probe).
  - `BashFrame.recover_pattern(m)` returns `end_pattern(m)` (`{end_prefix}(\d+)__`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_command_frame.py` (module already defines `M = SessionMarkers.for_session("cafef00d")`):

```python
class TestRecoverProbe:
    bash = BashFrame()
    zephyr = ZephyrFrame()

    def test_bash_recover_is_exit_code_probe(self):
        assert self.bash.recover(M) == f'echo "{M.end_prefix}$?__"\n'

    def test_bash_recover_pattern_matches_digit_form(self):
        pat = self.bash.recover_pattern(M)
        assert pat.search(f"{M.end_prefix}0__")
        assert pat.search(f"prompt$ {M.end_prefix}130__")

    def test_bash_recover_pattern_rejects_echoed_literal_probe(self):
        # An echo/REPL reflects the probe text verbatim: literal "$?", no digits.
        pat = self.bash.recover_pattern(M)
        assert pat.search(f'echo "{M.end_prefix}$?__"') is None

    def test_zephyr_recover_pattern_matches_bare_token(self):
        pat = self.zephyr.recover_pattern(M)
        assert pat.search(f"{M.recover}")
        assert pat.search(f'{M.recover}: command not found')

    def test_zephyr_serial_inherits_recover_pattern(self):
        assert ZephyrSerialFrame().recover_pattern(M).pattern == re.escape(M.recover)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_command_frame.py::TestRecoverProbe --no-cov -o addopts="" -q`
Expected: FAIL — `test_bash_recover_is_exit_code_probe` (recover still `echo {m.recover}`) and `AttributeError: 'BashFrame' object has no attribute 'recover_pattern'`.

- [ ] **Step 3: Add the default `recover_pattern` to `CommandFrame`**

In `src/otto/host/command_frame.py`, after `CommandFrame.extract_retcode` (the last abstract method, ~line 144), add a concrete method:

```python
    # --- liveness confirmation (concrete default; dialects may strengthen) ---

    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        r"""Pattern that PROVES the shell executed the recover probe.

        Default: the bare ``RECOVER`` token, matching the convention where
        ``recover`` echoes it back (Zephyr rejects it as an unknown command and
        its error handler prints it; a third-party frame inherits this). Bash
        strengthens it to the exit-code form an echo/REPL cannot fake — see
        :meth:`BashFrame.recover_pattern`.
        """
        return re.compile(re.escape(m.recover))
```

- [ ] **Step 4: Change `BashFrame.recover` and override `recover_pattern`**

Replace `BashFrame.recover` (currently `return f"echo {m.recover}\n"`, ~line 169) with:

```python
    @override
    def recover(self, m: SessionMarkers) -> str:
        # Echo-proof liveness probe: the END sentinel bakes in $?, so a real
        # shell emits `..._END__<digits>__` while an echo/REPL can only reproduce
        # the literal `$?`. recover_pattern matches only the digit form.
        return f'echo "{m.end_prefix}$?__"\n'

    @override
    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        return self.end_pattern(m)
```

(`ZephyrFrame`/`ZephyrSerialFrame` need no change — they inherit the default `recover_pattern`, and their `recover` stays the bare token.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_command_frame.py --no-cov -o addopts="" -q`
Expected: PASS (all, including the new `TestRecoverProbe`).

- [ ] **Step 6: Verify no CommandFrame subclass breaks**

Run: `grep -rn "CommandFrame)" src/ tests/ | grep class`
Expected: subclasses are `BashFrame`, `ZephyrFrame`, `ZephyrSerialFrame`, and test/repo frames extending `ZephyrFrame` (e.g. `ZephyrInlineRetcodeFrame`) — all inherit or override `recover_pattern`. If any extends `CommandFrame` directly, confirm it inherits the new concrete default (it does — the default is non-abstract).

- [ ] **Step 7: Lint + commit**

```bash
uv run --no-sync ruff check src/otto/host/command_frame.py tests/unit/host/test_command_frame.py && uv run --no-sync ruff format src/otto/host/command_frame.py tests/unit/host/test_command_frame.py
git add src/otto/host/command_frame.py tests/unit/host/test_command_frame.py
git commit -m "feat(host): echo-proof exit-code recover probe on BashFrame; recover_pattern

Assisted-by: Claude Opus 4.8"
```

---

### Task 3: Route `_recover_session` through `confirm_live`

**Files:**
- Modify: `src/otto/host/session.py` (`_recover_session`, ~line 579; add `_RECOVERY_PROBE_TIMEOUT` constant near `_RECOVERY_TIMEOUT` ~line 54; add `from .shell_liveness import confirm_live` to imports)
- Test: `tests/unit/host/test_session.py`

**Interfaces:**
- Consumes: `confirm_live` (Task 1), `BashFrame.recover`/`recover_pattern` (Task 2).
- Produces: `_recover_session` unchanged signature (`-> str`), same semantics (returns partial output, marks dead on failure) but now resend-based and echo-proof.

- [ ] **Step 1: Write the failing test (echo-proof I-3 fix)**

Add to `tests/unit/host/test_session.py` in the timeout/recovery test class (near `test_session_dies_if_recovery_fails`, ~line 349):

```python
    @pytest.mark.asyncio
    async def test_recovery_fails_when_parked_in_repl(self, session: MockSession):
        """A session parked in a REPL echoes the probe's literal `$?`, never the
        digit form — recovery must report failure (I-3), not a false positive."""
        async def simulate():
            await asyncio.sleep(0.01)
            session.feed(f"{session._begin_marker}\n")  # command hangs, no END

        feed_task = asyncio.create_task(simulate())

        async def echo_probe_literally():
            # Mimic a REPL parroting the probe command back verbatim (literal $?).
            await asyncio.sleep(0.15)
            session.feed(f'echo "{session._end_marker_prefix}$?__"\n')

        repl_task = asyncio.create_task(echo_probe_literally())

        import otto.host.session as session_mod
        original = session_mod._RECOVERY_TIMEOUT
        session_mod._RECOVERY_TIMEOUT = 0.3
        try:
            result = await session.run_cmd("mysql", timeout=0.1)
        finally:
            session_mod._RECOVERY_TIMEOUT = original
        await feed_task
        await repl_task

        assert result.status == Status.Error
        assert not session.alive  # literal-$? echo never matched -> dead, no false "recovered"
```

Also update the two existing recovery tests to feed the **exit-code digit form** instead of the bare recover marker:
- `test_timeout_returns_error_status` (~line 292): change `session.feed(f"{session._recover_marker}\n")` → `session.feed(f"{session._end_marker_prefix}0__\n")`.
- `test_session_stays_alive_after_recovered_timeout` (~line 316): same change.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest "tests/unit/host/test_session.py::TestRunCmd" --no-cov -o addopts="" -q`
Expected: FAIL — the two updated tests hang/fail (old `_recover_session` waits for `_recover_marker`, which is no longer fed), and `test_recovery_fails_when_parked_in_repl` fails (old recover matches the bare marker substring — the false positive we are removing).

- [ ] **Step 3: Add the recovery-probe timeout constant**

In `src/otto/host/session.py`, next to `_RECOVERY_TIMEOUT = 5.0` (~line 54), add:

```python
_RECOVERY_PROBE_TIMEOUT = 0.5  # per-probe wait inside the recovery resend loop
```

And add to the imports near the other `.` imports (e.g. after the `command_frame` import):

```python
from .shell_liveness import confirm_live
```

- [ ] **Step 4: Rewrite `_recover_session`**

Replace the body of `_recover_session` (the base `ShellSession` one, ~lines 579-614) with:

```python
    async def _recover_session(self) -> str:
        """Interrupt the hung command, then confirm the shell is back (echo-proof).

        Sends Ctrl+C, then drives :func:`~otto.host.shell_liveness.confirm_live`
        with the dialect's recover probe. On bash the probe is exit-code framed,
        so a session parked inside a REPL (which can only echo the literal ``$?``)
        correctly fails to confirm and is marked dead rather than falsely
        "recovered". Returns any partial output captured before the probe reply.
        """
        logger.debug(f"{self._log_tag}: recover_session entry marker={self._recover_marker!r}")
        await self._write("\x03")
        await asyncio.sleep(0.1)

        captured = ""

        async def _expect(pat: re.Pattern[str], t: float) -> str:
            nonlocal captured
            captured = await asyncio.wait_for(self._read_until_pattern(pat), t)
            return captured

        try:
            confirmed = await confirm_live(
                self._write,
                _expect,
                self._frame.recover,
                self._frame.recover_pattern,
                lambda: self._markers,
                settle=0.0,  # the post-Ctrl+C sleep above already settled
                probe_timeout=_RECOVERY_PROBE_TIMEOUT,
                deadline=_RECOVERY_TIMEOUT,
            )
        except asyncio.IncompleteReadError:
            confirmed = False

        if not confirmed:
            logger.debug(f"{self._log_tag}: recover_session failed; session marked dead")
            self._alive = False
            return ""
        # Output that arrived before the probe's END marker (echo is off during a
        # session, so the probe command itself is not in the stream to confuse this).
        return captured.split(self._markers.end_prefix)[0].strip()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --no-sync python -m pytest "tests/unit/host/test_session.py" --no-cov -o addopts="" -q`
Expected: PASS (all, including `test_recovery_fails_when_parked_in_repl`).

- [ ] **Step 6: Lint + commit**

```bash
uv run --no-sync ruff check src/otto/host/session.py tests/unit/host/test_session.py && uv run --no-sync ruff format src/otto/host/session.py tests/unit/host/test_session.py
git add src/otto/host/session.py tests/unit/host/test_session.py
git commit -m "feat(host): recover_session via confirm_live (echo-proof, fixes REPL false-positive)

Assisted-by: Claude Opus 4.8"
```

---

### Task 4: Route `_ensure_initialized` (handshake) through `confirm_live`

**Files:**
- Modify: `src/otto/host/session.py` (`_ensure_initialized`, ~lines 232-302)
- Test: `tests/unit/host/test_session.py` (existing handshake tests should stay green)

**Interfaces:**
- Consumes: `confirm_live` (Task 1). Uses `self._frame.handshake` (render) and a line-anchored READY pattern.
- Produces: `_ensure_initialized` unchanged behavior — sets `_initialized`/`_alive` on success, calls `_fail_init` (→ `ConnectionError`) on failure or EOF.

- [ ] **Step 1: Run the existing handshake tests first (baseline green)**

Run: `uv run --no-sync python -m pytest "tests/unit/host/test_session.py" -k "handshake or init or ready or eof" --no-cov -o addopts="" -q`
Expected: PASS (these are the behaviors to preserve: `test_init_sends_stty_and_ready_marker`, `test_eof_during_handshake_raises_clear_error`, and the `session` fixture's handshake).

- [ ] **Step 2: Rewrite `_ensure_initialized`**

Replace the loop body of `_ensure_initialized` (from just after `await self._open()` through the end of the `while True:` loop and `self._initialized = True; self._alive = True`, ~lines 234-302) with:

```python
        def _ready_pattern(m: "SessionMarkers") -> re.Pattern[str]:
            # Line-anchored (or buffer-start) + ANSI-absorbing, so the marker
            # can't match inside the echoed probe command (fatal on a failed
            # telnet login that loops back to "login:" echoing our probe).
            return re.compile(r"(?:^|\r|\n)(?:\x1b\[[0-9;]*m)*" + re.escape(m.ready))

        logger.debug(
            f"{self._log_tag}: handshake start marker={self._ready_marker!r} "
            f"timeout={self._init_timeout}s"
        )
        confirmed = False
        with suppress(asyncio.IncompleteReadError):
            confirmed = await confirm_live(
                self._write,
                lambda pat, t: asyncio.wait_for(self._read_until_pattern(pat), t),
                self._frame.handshake,
                _ready_pattern,
                lambda: self._markers,
                settle=0.0,
                probe_timeout=self._init_probe_interval,
                deadline=self._init_timeout,
            )
        if not confirmed:
            await self._fail_init()

        self._initialized = True
        self._alive = True
```

Notes for the implementer:
- `suppress` is already imported (`from contextlib import suppress` — verify at top of `session.py`; if not present, add it). `SessionMarkers` is already imported.
- `_fail_init` already raises `ConnectionError`; keep calling it with no args (its `attempt` param defaults to 0). Leave `_fail_init` itself unchanged.
- Delete the now-unused local `marker`, `deadline`, `handshake_cmd`, `attempt` loop scaffolding that this replaces.

- [ ] **Step 3: Run the handshake tests to verify they still pass**

Run: `uv run --no-sync python -m pytest "tests/unit/host/test_session.py" --no-cov -o addopts="" -q`
Expected: PASS (full file — handshake + recovery + run_cmd).

- [ ] **Step 4: Lint + commit**

```bash
uv run --no-sync ruff check src/otto/host/session.py && uv run --no-sync ruff format src/otto/host/session.py
git add src/otto/host/session.py
git commit -m "refactor(host): fold session handshake onto confirm_live

Assisted-by: Claude Opus 4.8"
```

---

### Task 5: Route `_resync_shell` through `confirm_live` + retire interim knobs

**Files:**
- Modify: `src/otto/host/login_proxy.py` (`_resync_shell` + the `_RESYNC_*` constants ~lines 169-232; imports)
- Test: `tests/unit/host/test_login_proxy.py`

**Interfaces:**
- Consumes: `confirm_live` (Task 1), `BashFrame` + `SessionMarkers` (Task 2 / command_frame).
- Produces: `_resync_shell(io, host_id, hop_login)` unchanged signature — raises `LoginProxyError` on failure. Retires `_RESYNC_ATTEMPTS`, `_RESYNC_TIMEOUT`, and the lookbehind; keeps `_RESYNC_SETTLE`.

- [ ] **Step 1: Update the test fakes and existing resync tests**

In `tests/unit/host/test_login_proxy.py`:

Change the resync-probe prefix constant (the new probe is `echo "__OTTO_<id>_END__$?__"`):
```python
_RESYNC_ECHO_PREFIX = 'echo "__OTTO_'
```

The `RecorderIO.expect` resync branch only needs to *not raise* (confirm_live confirms on any non-raising expect); simplify it:
```python
    async def expect(self, pattern, timeout: float = 10.0) -> str:
        if self.sent and self.sent[-1][0].startswith(_RESYNC_ECHO_PREFIX):
            return "resync-ok"  # confirm_live treats a non-raising expect as confirmed
        return self._replies.pop(0) if self._replies else ""
```

Replace the exhaustion test (fixed-attempt count is gone; the loop is deadline-bounded) and delete the interim settle test:
```python
@pytest.mark.asyncio
async def test_resync_shell_raises_login_proxy_error_when_deadline_elapses(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(login_proxy, "_RESYNC_DEADLINE", 0.05)  # short deadline for the test
    io = _FlakyResyncIO(fail_times=999)  # never lands
    with pytest.raises(LoginProxyError, match=r"h1.*resync.*mysql"):
        await _resync_shell(io, host_id="h1", hop_login="mysql")
    assert io._calls >= 1  # it probed; the deadline (not a fixed count) ended it
```
Delete `test_resync_shell_settles_before_first_probe` (the settle is now `confirm_live`'s job, covered by `test_settles_before_first_probe` in `test_shell_liveness.py`). Keep `test_resync_shell_retries_past_timeouts_then_succeeds` unchanged (it asserts `io._calls == 3` / `len(io.sent) == 3`, which still holds: two timeouts then a landing probe). Keep the `_fast_resync_settle` autouse fixture (it zeroes `login_proxy._RESYNC_SETTLE`, which `_resync_shell` still reads and passes to `confirm_live`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_login_proxy.py --no-cov -o addopts="" -q`
Expected: FAIL — `AttributeError: module 'otto.host.login_proxy' has no attribute '_RESYNC_DEADLINE'`, and `perform_switch` tests fail because the old probe prefix no longer filters (resync noise leaks into `_without_resync`).

- [ ] **Step 3: Rewrite the constants + `_resync_shell`**

In `src/otto/host/login_proxy.py`, replace the `_RESYNC_*` constant block (~lines 169-180, the comment + `_RESYNC_ATTEMPTS`/`_RESYNC_TIMEOUT`/`_RESYNC_SETTLE`) with:

```python
# Post-transition resync. A su/sudo/exit hop is a foreground-process handoff on
# the pty: su/login/sudo flush pending terminal input across the privilege
# boundary (a typeahead-attack defense), so the first probe written back-to-back
# with the transition is silently dropped (verified 40/40 on the live bed).
# _RESYNC_SETTLE absorbs that flush; confirm_live then resends an echo-proof
# exit-code probe (BashFrame.recover) on a short interval until the shell answers
# with the digit form or _RESYNC_DEADLINE passes — decoupling per-probe wait from
# the overall budget so a slow round-trip under load no longer exhausts a fixed
# attempt count (the 3.13 flake). See otto.host.shell_liveness.confirm_live.
_RESYNC_SETTLE = 0.3
_RESYNC_PROBE_TIMEOUT = 0.5
_RESYNC_DEADLINE = 10.0
_RESYNC_FRAME = BashFrame()
```

Add imports near the top (with the other `.` imports, after `from ..registry import ...`):
```python
from .command_frame import BashFrame, SessionMarkers
from .shell_liveness import confirm_live
```

Replace the whole `_resync_shell` function body (the settle + the `for _ in range(_RESYNC_ATTEMPTS)` loop + `raise`) with:

```python
    confirmed = await confirm_live(
        io.send,
        io.expect,
        _RESYNC_FRAME.recover,
        _RESYNC_FRAME.recover_pattern,
        lambda: SessionMarkers.for_session(uuid.uuid4().hex[:12]),
        settle=_RESYNC_SETTLE,
        probe_timeout=_RESYNC_PROBE_TIMEOUT,
        deadline=_RESYNC_DEADLINE,
    )
    if not confirmed:
        raise LoginProxyError(
            f"{host_id}: shell did not resync after a login-proxy transition "
            f"({hop_login!r}) — su/sudo/exit flushed the next command"
        )
```

Update `_resync_shell`'s docstring: it now drives `confirm_live` with an echo-proof exit-code probe (drop the negative-lookbehind explanation, which is gone). Remove now-unused imports if any (`re` may still be used elsewhere in the module — check with `ruff check`; `uuid` and `contextlib` are still used).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_login_proxy.py --no-cov -o addopts="" -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + commit**

```bash
uv run --no-sync ruff check src/otto/host/login_proxy.py tests/unit/host/test_login_proxy.py && uv run --no-sync ruff format src/otto/host/login_proxy.py tests/unit/host/test_login_proxy.py
git add src/otto/host/login_proxy.py tests/unit/host/test_login_proxy.py
git commit -m "feat(host): login-proxy resync via confirm_live; retire interim knobs + lookbehind

Assisted-by: Claude Opus 4.8"
```

---

### Task 6: Full gate + live-bed validation

**Files:** none (verification only). Requires the live bed free (no `make release`/`nox` running) and the Unix VMs (carrot/tomato/pepper) up.

- [ ] **Step 1: Typecheck + lint gate**

Run: `uv run --no-sync ty check src/otto/host/shell_liveness.py src/otto/host/command_frame.py src/otto/host/session.py src/otto/host/login_proxy.py` then `uv run --no-sync ruff check . && uv run --no-sync ruff format --check .`
Expected: no errors (in particular ty's `missing-override-decorator` is clean — `BashFrame.recover_pattern` has `@override`; the base `CommandFrame.recover_pattern` default does not).

- [ ] **Step 2: Hostless unit suite for the touched modules**

Run: `uv run --no-sync python -m pytest tests/unit/host/test_shell_liveness.py tests/unit/host/test_command_frame.py tests/unit/host/test_session.py tests/unit/host/test_login_proxy.py -p no:cacheprovider --no-cov -o addopts="" -q`
Expected: PASS (all).

- [ ] **Step 3: Live-bed matrix — bash paths**

Confirm the bed is free first: `ps -ef | grep -iE "nox|make release" | grep -v grep` (empty).
Run (each a few times sequentially — no xdist storm on the dev VM):
```bash
for i in 1 2 3 4 5; do uv run --no-sync python -m pytest tests/e2e/host/test_login_proxy_e2e.py -p no:cacheprovider --no-cov -o addopts="" -q; done
uv run --no-sync python -m pytest tests/e2e/host/test_app_shell_e2e.py -p no:cacheprovider --no-cov -o addopts="" -q
```
Expected: PASS — `test_switch_user_roundtrip` (resync), the `interact --as-user` bridge test (echo-ON), builtin-su, oneshot, nc; app-shell e2e (recover from inside a REPL).

- [ ] **Step 4: Live-bed matrix — recover-from-REPL and wedged bash**

The app-shell e2e already drives `_recover_session` from inside mysql/python3 (I-3 path). If a standalone check is wanted, use the scratchpad harness pattern from the design session (drive `host.run("mysql", timeout=…)` on a leased host and assert the session is reported dead, not falsely recovered). Do NOT power/reboot any VM.

- [ ] **Step 5: Live-bed matrix — Zephyr recovery (no regression)**

If the Zephyr bed is up (ssh hop 10.10.200.14 → telnet Zephyr shell), drive a command timeout on a Zephyr host and confirm `_recover_session` still recovers via the `retval`/bare-token path (Zephyr inherits the default `recover_pattern`; its `recover` probe is unchanged). If the Zephyr bed is not available this session, note it and rely on the `test_zephyr*` unit coverage (Zephyr `recover`/`recover_pattern` unchanged) — flag for a follow-up live check.

- [ ] **Step 6: Full gate**

Run: `make coverage` then `nox` (typecheck + docs). Address any drift (e.g. coverage floor). Expected: green.

- [ ] **Step 7: Retire the interim (already staged separately)**

The interim `_RESYNC_SETTLE`/`_RESYNC_TIMEOUT`/`_RESYNC_ATTEMPTS` hardening was superseded by Task 5 (the constants block and lookbehind are gone; `_RESYNC_SETTLE` survives as the confirm_live settle). Confirm `grep -n "_RESYNC_ATTEMPTS\|_RESYNC_TIMEOUT\|(?<!echo " src/otto/host/login_proxy.py` returns nothing.

- [ ] **Step 8: Final commit (if any gate fixups were needed)**

```bash
git add -A
git commit -m "test(host): live-bed validation + gate fixups for liveness unification

Assisted-by: Claude Opus 4.8"
```

---

## Self-Review

**Spec coverage:**
- §3.1 shared loop → Task 1. §3.2 frame API (recover_pattern, bash exit-code probe, Zephyr inherits) → Task 2. §3.3 call sites: `_recover_session` → Task 3, `_ensure_initialized` folded in → Task 4, `_resync_shell` → Task 5. §3.4 retired knobs/lookbehind/bare-substring → Tasks 5 & 3. §4 test + live-bed matrix (incl. Zephyr) → Task 6. §5 defaults (deadline 10 s, probe 0.5 s, settle 0.3 s) → Global Constraints + Task 5. All covered.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; run commands have expected output. Task 6 steps 4–5 reference the design-session harness pattern rather than re-pasting a full script — acceptable (they are optional live checks whose primary coverage is the app-shell e2e + unit tests).

**Type consistency:** `confirm_live(send, expect, render, pattern, new_markers, *, settle, probe_timeout, deadline) -> bool` is defined identically in Task 1 and called with matching positional/keyword args in Tasks 3, 4, 5. `recover_pattern(self, m) -> re.Pattern[str]` defined in Task 2, used in Tasks 3 & 5. `SessionMarkers.for_session` / `.end_prefix` / `.recover` / `.ready` names match `command_frame.py`. Constants `_RECOVERY_PROBE_TIMEOUT`, `_RESYNC_PROBE_TIMEOUT`, `_RESYNC_DEADLINE`, `_RESYNC_SETTLE`, `_RESYNC_FRAME` introduced where used.
