# Default Command Timeout for `Host` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `Host` command execution a 30-second default timeout so a hung command fails the test instead of hanging it; make the built-in `timeout` parameter load-bearing on every code path; and leave **every command surface advertising the same default in the same way**.

**Architecture:** A single `DEFAULT_COMMAND_TIMEOUT = 30.0` becomes the signature default on the four public command surfaces (`BaseHost.run`, `BaseHost.exec`, `BaseHost.expect`, `HostSession.run`), whose annotations narrow from `float | None` to plain `float`. Each validates its argument once and delegates to a per-family hook (`_run_one` / `_exec_one` / `_expect_one`), so a host family cannot drift from the advertised default. The internal seams below them then drop their `None` branches entirely, so no code path can await a command without a bound. Unbounded execution stays available only as an explicit `float("inf")`.

**Tech Stack:** Python 3.10 (floor), asyncio, asyncssh, telnetlib3, Typer/click, pytest + pytest-asyncio, ruff, ty.

**Spec:** `docs/superpowers/specs/2026-07-29-host-default-command-timeout-design.md`

## Global Constraints

- **Only Chris pushes.** Never `git push`. This plan runs on a worktree branch, so self-commit is fine — use a conventional-commit prefix and an `Assisted-by:` trailer.
- `DEFAULT_COMMAND_TIMEOUT = 30.0` — exact value, defined once in `src/otto/host/host.py`.
- **Never** add `from __future__ import annotations` — it trips Sphinx nitpicky `-W`.
- Typer hard-asserts on multi-member `Union` types. `float | None` is fine (one member after stripping `NoneType`); `int | str | None` is not.
- Prefer lists over tuples in APIs; callables return dataclasses.
- **Do not silence lint/type rules to make a number go down.** If a rule genuinely should be off, turn it off honestly and say so — a suppression that hides a real finding is a failure.
- Per-task gate: `make check-python` (ruff lint + format + `ty check`) plus the task's own targeted `pytest`, then `make coverage-unit`. **`ty` runs only via `check-python`/`nox`** — budget a typecheck round after every `src/` edit. `make coverage` (full suite, needs lab VMs) runs once in Task 12 only, to respect the no-heavy-load-on-the-dev-VM rule.
- Docstring edits need a **clean** docs rebuild (`make docs`) — incremental Sphinx `-W` misses broken `:doc:` refs inside docstrings.

## Command Surface Consistency

Consistency across command surfaces is a goal in its own right, so the target state is stated explicitly. Auditing every `timeout` in the host package found **one real defect** (`expect`, Task 9) and one surface that was already right.

**Every command surface ends at `DEFAULT_COMMAND_TIMEOUT`, validated at the entry point, with `float("inf")` as the only opt-out:**

| Surface | Before | After | Task |
| --- | --- | --- | --- |
| `Host.run` / `BaseHost.run` | `None` (unbounded) | `DEFAULT_COMMAND_TIMEOUT` | 5 |
| `Host.exec` / `BaseHost.exec` | `None` (unbounded) | `DEFAULT_COMMAND_TIMEOUT` | 6 |
| `HostSession.run` | `10.0` | `DEFAULT_COMMAND_TIMEOUT` | 7 |
| `Host.expect` / `BaseHost.expect` | `30.0` **advertised** | `DEFAULT_COMMAND_TIMEOUT` | 9 |
| `{Local,Unix,Embedded,Docker}Host.expect` | `10.0` **actual** | via `_expect_one` | 9 |
| `HostSession.expect` | `10.0` | `DEFAULT_COMMAND_TIMEOUT` | 9 |
| `ShellSession.expect` | `30.0` | `DEFAULT_COMMAND_TIMEOUT` | 9 |
| `AppShell.cmd_timeout` | `30.0` (2nd literal) | `DEFAULT_COMMAND_TIMEOUT` | 9 |
| `_run_one` (×5) | `10.0` (cosmetic) | **no default** — hooks require it | 8 |
| `_exec_one`, `_expect_one` | n/a (new) | **no default** — hooks require it | 6, 9 |
| `ShellSession.run_cmd`, `SessionManager.{run_cmd,exec,expect}` | `None` / `10.0` | `DEFAULT_COMMAND_TIMEOUT` | 8, 9 |

**`AppShell` was already semantically right, and now shares the constant.** `AppShell.cmd_timeout` (`app_shell.py:275`) already held `30.0` — independent corroboration of the value — but as a *second literal* it could drift, so Task 9 makes it reference `DEFAULT_COMMAND_TIMEOUT`. Its `timeout: float | None = None` parameters stay: they mean *"inherit the enclosing level"*, never unbounded.

**The invariant this work converges on:**

> `float | None` appears **iff** there is an enclosing default to inherit from; plain `float` appears wherever the value is final. `None` never means unbounded, anywhere — `float("inf")` is the only way to opt out of a bound.

Three sites satisfy it and keep `float | None`: `AppShell`'s three-level cascade (class default → session override → per-call override), `ShellCommand.timeout`, and `_resolve_command`'s `default_timeout`. A cascade needs a sentinel distinct from every valid value, and since `DEFAULT_COMMAND_TIMEOUT` is itself valid, it cannot serve as its own "not specified" marker.

**Validation sits at the boundary where a value becomes final,** not at every layer: `BaseHost.{run,exec,expect}` and `HostSession.{run,expect}`. That is what covers `AppShell` without `AppShell` containing any validation code — both of its waits route through `HostSession.expect`.

**Deliberately NOT unified** — these are a different semantic class, and forcing one number on them would be cargo-culting:

- **Transport / connect timeouts:** `options.py` (`connect_timeout`, `socket_timeout`, `path_timeout`, `listener_timeout`, `echo_negotiation_timeout`), `transfer/nc.py:134,481` (connect), `session.py` `_INIT_TIMEOUT` / `_RECOVERY_TIMEOUT`, and the new shared `_EXEC_REAP_TIMEOUT` (in `host.py`). These bound *establishing or tearing down* a channel, not running a command.
- **Reachability and power:** `is_reachable(10.0)`, `wait_until_up/down`, `reboot(600.0)`. A reboot legitimately takes ten minutes.
- **`_ProxyIO.expect` (`interact.py:363`, stays `10.0`):** raw transport IO during login/proxy negotiation — a login-prompt wait, not a host command.

## File Structure

**Modified:**
- `src/otto/result.py` — `CommandResult` gains `timed_out: bool`
- `src/otto/host/host.py` — the constant, `_validate_timeout`, `BaseHost.run`, `BaseHost.exec` template + `_exec_one` abstract, `Host` protocol, `_run_cmds_with_budget`
- `src/otto/host/session.py` — SSH `exec` timeout fix, `HostSession.run`/`.expect` entry points, `ShellSession.run_cmd` narrowing, `timed_out` on the timeout path
- `src/otto/host/{login_proxy,privilege}.py` — mixin protocol `expect` declarations adopt the constant
- `src/otto/host/app_shell.py` — `cmd_timeout` references `DEFAULT_COMMAND_TIMEOUT` instead of a second `30.0`
- `src/otto/host/{local,unix,embedded,docker}_host.py` — `exec` → `_exec_one`, `expect` → `_expect_one`, `_run_one` signature cleanup
- `src/otto/utils.py` — `Opt` gains `min`
- `src/otto/cli/param_synth.py` — forwards `Opt.min` to `typer.Option`
- `src/otto/{context.py,config/fleet.py}` — forwarding wrappers adopt the default
- `src/otto/docker/{build,compose}.py`, `src/otto/host/transfer/nc.py` — `timeout=None` → `float("inf")`
- `src/otto/tunnel/{manage,discovery}.py`, `src/otto/link/manage.py` — retire external `asyncio.wait_for`
- `docs/guide/cli-reference.md` — document the default and `inf`

**Tests modified:** `tests/unit/host/{test_run_timeout,test_shell_command,test_unix_host,test_embedded_host}.py`, `tests/integration/host/test_session_stability_integration.py`
**Tests created:** none — every new test lands in an existing file alongside its siblings.

---

### Task 1: The constant and the validator

Pure addition — nothing calls these yet, so no behavior changes.

**Files:**
- Modify: `src/otto/host/host.py` (add `import math`; add constant + `_validate_timeout` after the `Expect` alias at line 53)
- Test: `tests/unit/host/test_run_timeout.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DEFAULT_COMMAND_TIMEOUT: float = 30.0` and `_validate_timeout(timeout: float) -> float`, both in `otto.host.host`. Tasks 5, 6, 7 and 9 import them; Task 7 imports them into `session.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_run_timeout.py`:

```python
class TestValidateTimeout:
    """The entry-point validator rejects values asyncio.wait_for misreads."""

    def test_default_is_thirty_seconds(self):
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        assert DEFAULT_COMMAND_TIMEOUT == 30.0

    @pytest.mark.parametrize("good", [0, 0.0, 0.5, 30.0, 3600, float("inf")])
    def test_accepts_non_negative_numbers_and_inf(self, good):
        from otto.host.host import _validate_timeout

        assert _validate_timeout(good) == float(good)

    # `None`/str/bool are deliberately invalid per the annotation; tests/ is
    # excluded from ty (pyproject.toml [tool.ty.src] exclude), so passing them
    # here needs no suppression.
    @pytest.mark.parametrize("bad", [None, "30", True, False, [1]])
    def test_rejects_non_numbers(self, bad):
        from otto.host.host import _validate_timeout

        with pytest.raises(TypeError, match="timeout must be a number"):
            _validate_timeout(bad)

    def test_rejects_nan(self):
        from otto.host.host import _validate_timeout

        with pytest.raises(ValueError, match="must not be NaN"):
            _validate_timeout(float("nan"))

    @pytest.mark.parametrize("bad", [-1, -0.001, float("-inf")])
    def test_rejects_negatives_including_neg_inf(self, bad):
        from otto.host.host import _validate_timeout

        with pytest.raises(ValueError, match="must be >= 0"):
            _validate_timeout(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestValidateTimeout -v -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_COMMAND_TIMEOUT'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/host.py`, add `import math` to the stdlib import block (after `import asyncio`, keeping alphabetical order: `asyncio`, `math`, `re`, `uuid`). Then after the `Expect` alias (line 53) and before `logger = getLogger(__name__)`:

```python
DEFAULT_COMMAND_TIMEOUT = 30.0
"""Seconds a single command may run before otto gives up on it.

Applies to :meth:`Host.run`, :meth:`Host.exec` and
:meth:`~otto.host.session.HostSession.run` when no explicit timeout is
given. Pass ``float("inf")`` for a deliberately unbounded command; there is
no other way to disable the bound.
"""


def _validate_timeout(timeout: float) -> float:
    """Reject timeout values ``asyncio.wait_for`` would silently misinterpret.

    Annotations are not enforced at runtime, so this guards the public entry
    points against callers a type checker never sees. ``float("inf")`` is
    allowed — it is the supported spelling for an unbounded command.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(f"timeout must be a number, got {type(timeout).__name__}: {timeout!r}")
    if math.isnan(timeout):
        raise ValueError("timeout must not be NaN")
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0, got {timeout!r}")
    return float(timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestValidateTimeout -v -p no:cacheprovider`
Expected: PASS (16 tests)

- [ ] **Step 5: Run the static gate**

Run: `make check-python`
Expected: clean. If `ty` flags the `isinstance` checks as redundant, do **not** suppress — report back; the spec's premise (verified against `all = "error"`) is that it does not.

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/host.py tests/unit/host/test_run_timeout.py
git commit -m "feat(host): add DEFAULT_COMMAND_TIMEOUT and the entry-point timeout validator

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `CommandResult.timed_out`

Makes a timeout structurally detectable. `retcode == -1` cannot serve — it already means both "never ran" and "budget exhausted".

**Files:**
- Modify: `src/otto/result.py:62-69` (add field), `src/otto/host/session.py` (~411-420), `src/otto/host/local_host.py` (~262-269), `src/otto/host/host.py` (~157-165)
- Test: `tests/unit/host/test_run_timeout.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CommandResult.timed_out: bool = False`. Task 3 sets it on the new SSH guard; Task 10 reads it at every retired-workaround site.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_run_timeout.py`:

```python
class TestTimedOutFlag:
    """Every timeout path marks the result, so callers need no string matching."""

    def test_defaults_to_false(self):
        r = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        assert r.timed_out is False

    @pytest.mark.asyncio
    async def test_local_exec_timeout_sets_flag(self):
        host = LocalHost(element="local", log=LogMode.QUIET)
        try:
            result = await host.exec("sleep 10", timeout=0.1)
        finally:
            await host.close()
        assert result.status == Status.Error
        assert result.timed_out is True
        assert "timed out" in result.value

    @pytest.mark.asyncio
    async def test_budget_exhausted_skip_sets_flag(self, host: UnixHost):
        async def slow_cmd(cmd, **kwargs):
            await asyncio.sleep(0.08)
            return CommandResult(status=Status.Success, value="ok", command=cmd, retcode=0)

        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=slow_cmd):
            results = await host.run(["slow1", "slow2", "skipped"], timeout=0.1)

        skipped = [r for r in results if "budget exhausted" in r.value]
        assert skipped, "expected at least one skipped command"
        assert all(r.timed_out for r in skipped)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestTimedOutFlag -v -p no:cacheprovider`
Expected: FAIL — `TypeError: CommandResult.__init__() got an unexpected keyword argument` is *not* what you should see; instead `AttributeError`/`assert False is True` on `timed_out`.

- [ ] **Step 3: Add the field**

In `src/otto/result.py`, inside `CommandResult` after the `retcode` field (line 68-69):

```python
    timed_out: bool = False
    """True when the command was killed by its timeout rather than exiting.

    Distinguishes a timeout from an ordinary failure without string-matching
    :attr:`~otto.result.Result.value`; ``retcode`` cannot, since ``-1`` also
    means "never ran" and "skipped: cumulative budget exhausted".
    """
```

- [ ] **Step 4: Set it on the three existing timeout paths**

`src/otto/host/session.py` — in `ShellSession.run_cmd`'s `except asyncio.TimeoutError:` branch, add `timed_out=True` to the returned `CommandResult` (alongside `retcode=-1`).

`src/otto/host/local_host.py` — in `_exec_subprocess`'s `except asyncio.TimeoutError:` branch, add `timed_out=True` to the returned `CommandResult`.

`src/otto/host/host.py` — in `_run_cmds_with_budget`, the budget-exhausted skip:

```python
                entries.append(
                    CommandResult(
                        status=Status.Error,
                        value="Skipped: cumulative timeout budget exhausted",
                        command=sc.cmd,
                        retcode=-1,
                        timed_out=True,
                    )
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_run_timeout.py tests/unit/host/test_session.py -v -p no:cacheprovider`
Expected: PASS — including the pre-existing `test_timeout_returns_error_status` and `test_session_stays_alive_after_recovered_timeout`, which must be unaffected.

- [ ] **Step 6: Run the static gate**

Run: `make check-python`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/otto/result.py src/otto/host/session.py src/otto/host/local_host.py src/otto/host/host.py tests/unit/host/test_run_timeout.py
git commit -m "feat(result): mark timed-out CommandResults structurally

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Fix the SSH `exec` timeout gap

At `session.py:1579-1587` the output loop is never wrapped in `asyncio.wait_for`, so the `except asyncio.TimeoutError` is dead code and `await process.wait()` is unbounded. **The guard must be proven red against the unfixed code** — a regression test that was never seen failing certifies nothing.

**Files:**
- Modify: `src/otto/host/session.py:1568-1594` (the `case "ssh":` branch of `SessionManager.exec`)
- Test: `tests/unit/host/test_unix_host.py`

**Interfaces:**
- Consumes: `CommandResult.timed_out` (Task 2).
- Produces: no signature change — `SessionManager.exec` keeps its shape; only the SSH branch's behavior changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_unix_host.py`. The fake `create_process` yields one line then stalls forever, exactly reproducing a wedged remote command:

```python
class TestSshExecTimeout:
    """Regression: UnixHost.exec over SSH must honour its timeout.

    Before the fix the read loop was never wrapped in asyncio.wait_for, so the
    `except asyncio.TimeoutError` below it was dead code and this test hung.
    """

    @pytest.mark.asyncio
    async def test_ssh_exec_stalling_command_times_out(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="stalled",
            creds=[Cred(login="u", password="p")],
            term="ssh",
            log=LogMode.QUIET,
        )

        class _StalledStdout:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(3600)  # never yields, never returns
                raise StopAsyncIteration

        class _StalledProcess:
            stdout = _StalledStdout()
            terminated = False

            def terminate(self):
                type(self).terminated = True

            async def wait(self):
                return SimpleNamespace(exit_status=-1)

        class _Conn:
            async def create_process(self, cmd, **kw):
                return _StalledProcess()

        h._session_mgr._connections = MagicMock()
        h._session_mgr._connections.term = "ssh"
        h._session_mgr._connections.proxy_hops = []
        h._session_mgr._connections.ssh = AsyncMock(return_value=_Conn())
        h._session_mgr._exec_factory = None

        result = await asyncio.wait_for(h.exec("sleep 3600", timeout=0.1), timeout=10.0)

        assert result.status == Status.Error
        assert result.timed_out is True
        assert "timed out" in result.value
        assert _StalledProcess.terminated is True
```

Add `from types import SimpleNamespace` to the file's imports if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_unix_host.py::TestSshExecTimeout -v -p no:cacheprovider`
Expected: **FAIL** — the outer `asyncio.wait_for(..., 10.0)` fires with `TimeoutError` because the inner `timeout=0.1` is ignored. That outer bound is what stops this step hanging the suite. Record that you saw it fail; if it passes here, the fix is already present and the test is not proving anything.

- [ ] **Step 3: Wrap the read loop and the wait**

Replace the `case "ssh":` body's `lines`/`try` block in `src/otto/host/session.py`:

```python
                lines: list[str] = []

                async def _drain() -> None:
                    async for raw_line in process.stdout:
                        line = raw_line.rstrip("\n")
                        lines.append(line)
                        if mode is not LogMode.NEVER:
                            self._log_output(line, mode)

                try:
                    await asyncio.wait_for(_drain(), timeout=timeout)
                except asyncio.TimeoutError:
                    process.terminate()
                    # Bound the reap too: a wedged remote command can leave the
                    # channel unreadable, and an unbounded wait() here would
                    # reintroduce the very hang this timeout exists to prevent.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=_EXEC_REAP_TIMEOUT)
                    return CommandResult(
                        status=Status.Error,
                        value=f"Command timed out after {timeout}s\n" + "\n".join(lines),
                        command=cmd,
                        retcode=-1,
                        timed_out=True,
                    )
                result = await process.wait()
```

**`_EXEC_REAP_TIMEOUT` already exists — import it, do not redefine it.** Task 2 introduced it in `src/otto/host/host.py` beside `DEFAULT_COMMAND_TIMEOUT`, because the local-subprocess timeout path needed exactly the same bounded reap for exactly the same reason (a process can ignore SIGTERM, so an unbounded reap inside a timeout handler defeats the timeout). Import it from `.host`; `session.py` already imports `_validate_timeout` and friends from there, so the direction is established, and one constant for one concept is the whole point of this work item.

Ensure `import contextlib` is present at the top of `session.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/host/test_unix_host.py::TestSshExecTimeout -v -p no:cacheprovider`
Expected: PASS, in well under a second.

- [ ] **Step 5: Run the surrounding suites**

Run: `uv run pytest tests/unit/host -x -q -p no:cacheprovider`
Expected: PASS. The nc-transfer tests exercise this same SSH exec path heavily; a regression shows up there first.

- [ ] **Step 6: Static gate and commit**

```bash
make check-python
git add src/otto/host/session.py tests/unit/host/test_unix_host.py
git commit -m "fix(host): enforce the timeout on the SSH exec fast path

The output loop was never wrapped in asyncio.wait_for, so the except
asyncio.TimeoutError below it was unreachable and process.wait() was
unbounded — UnixHost.exec(timeout=N) over SSH could hang forever.

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Replace every `timeout=None` caller with `float("inf")`

Do this **before** narrowing the annotations, so the tree is `None`-free when the types tighten. **`ty` catches only the two docker sites** — the nc calls go through an injected `exec_cmd` typed `Callable[..., Coroutine[Any, Any, CommandResult]]` (`transfer/nc.py:174`), whose `...` makes kwargs unchecked. Fix all five by hand.

**Files:**
- Modify: `src/otto/docker/build.py:98`, `src/otto/docker/compose.py:125`, `src/otto/host/transfer/nc.py:646,713,846`
- Modify: `tests/unit/host/test_unix_host.py:547`, `tests/integration/host/test_session_stability_integration.py:200`

**Interfaces:**
- Consumes: nothing (`float("inf")` is already a valid `float | None` argument today).
- Produces: no `timeout=None` remains anywhere under `src/`.

- [ ] **Step 1: Change the two docker sites**

`src/otto/docker/build.py:98` and `src/otto/docker/compose.py:125` — replace `timeout=None` with `timeout=float("inf")`, and add a comment above each:

```python
    # Unbounded on purpose: an image build/pull has no defensible bound, and a
    # made-up constant would be wrong on a slower builder. `inf` states that.
    result = await parent.exec(cmd, timeout=float("inf"))
```

- [ ] **Step 2: Change the three nc sites**

`src/otto/host/transfer/nc.py:646`, `:713`, `:846` — replace `timeout=None` with `timeout=float("inf")`. For the two `-w`-bearing listener calls (`:713`, `:846`) add:

```python
                        # netcat self-bounds via `-w <listener_timeout>`; an otto
                        # timeout here would be redundant and could fire first,
                        # failing a transfer that was still healthy.
                        timeout=float("inf"),
```

For `:646` (the sender) use:

```python
                        # Unbounded on purpose: the command's duration *is* the
                        # transfer, which scales with file size.
                        timeout=float("inf"),
```

- [ ] **Step 3: Fix the two tests that pass `None` at runtime**

`tests/unit/host/test_unix_host.py:547` — `timeout=None` → `timeout=float("inf")`. Also update the docstring at line 495 which reads `exec(timeout=None)` → `exec(timeout=float("inf"))`.

`tests/integration/host/test_session_stability_integration.py:200` — `host1.exec("sleep 5", timeout=None)` → `timeout=float("inf")`.

- [ ] **Step 4: Verify no `timeout=None` survives in src**

Run: `grep -rn "timeout=None" src/`
Expected: **no output.**

Run: `grep -rn "timeout=None" tests/ | grep -vE "def |async def "`
Expected: no output — remaining hits must all be fake/stub *signatures* (e.g. `async def exec_cmd(self, cmd, timeout=None)`), which are unaffected.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/unit/host/test_unix_host.py tests/unit/host/test_transfer_nc_get.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Static gate and commit**

```bash
make check-python
git add src/otto/docker/build.py src/otto/docker/compose.py src/otto/host/transfer/nc.py tests/unit/host/test_unix_host.py tests/integration/host/test_session_stability_integration.py
git commit -m "refactor(host): spell deliberate unbounded commands as float('inf')

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `BaseHost.run` and the `Host` protocol adopt the default

**Files:**
- Modify: `src/otto/host/host.py` — `Host.run` protocol (215-242), `Host.exec` protocol (244-264), `BaseHost.run` (614-685)
- Modify: `tests/unit/host/test_run_timeout.py:37-42`, `tests/unit/host/test_shell_command.py:77,86,136,162,170`

**Interfaces:**
- Consumes: `DEFAULT_COMMAND_TIMEOUT`, `_validate_timeout` (Task 1).
- Produces: `BaseHost.run(cmds, expects=None, timeout: float = DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL, sudo=False) -> Results`. Task 6 mirrors this on `HostSession.run`; Task 7 relies on `_run_cmds_with_budget` receiving a real `float`.

**Critical distinction for the tests:** the single-command form passes `single.timeout` straight through, so `_run_one` receives **exactly** `30.0`. The list form uses the value as a *cumulative budget*, so `_run_one` receives a budget-derived value **slightly below** `30.0`. Assert exact equality only for the single form.

- [ ] **Step 1: Update the tests that encode the old behavior**

`tests/unit/host/test_run_timeout.py` — replace `test_no_timeout_passes_none_to_run_one` (lines 37-42) with:

```python
    @pytest.mark.asyncio
    async def test_no_timeout_uses_the_default_budget(self, host: UnixHost):
        """Without an explicit timeout, the list form budgets DEFAULT_COMMAND_TIMEOUT."""
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        ok = CommandResult(status=Status.Success, value="hi", command="echo hi", retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(["echo hi"])
        actual = mock.call_args.kwargs["timeout"]
        # List form = cumulative budget, so this is just under the full default.
        assert 0 < actual <= DEFAULT_COMMAND_TIMEOUT
        assert actual > DEFAULT_COMMAND_TIMEOUT - 1.0
```

`tests/unit/host/test_shell_command.py` — the single-command assertions at lines 77, 86, 162 and 170 change `timeout=None` to `timeout=DEFAULT_COMMAND_TIMEOUT`. Add `from otto.host.host import DEFAULT_COMMAND_TIMEOUT` to the imports. For example line 77 becomes:

```python
        mock.assert_called_once_with(
            "ls", expects=None, timeout=DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL
        )
```

Line 136's `test_none_timeout_everywhere` is a **list** form (`host.run([ShellCommand(cmd="x")])`), so it cannot assert exact equality. Rename and rewrite it:

```python
    @pytest.mark.asyncio
    async def test_default_timeout_everywhere(self, host: UnixHost, ok: CommandResult):
        """No timeout anywhere → the default becomes the cumulative budget."""
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run([ShellCommand(cmd="x")])
        actual = mock.call_args.kwargs["timeout"]
        assert 0 < actual <= DEFAULT_COMMAND_TIMEOUT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_shell_command.py tests/unit/host/test_run_timeout.py -q -p no:cacheprovider`
Expected: FAIL — assertions report `timeout=None` where `30.0` was expected.

- [ ] **Step 3: Change the protocol signatures**

In `src/otto/host/host.py`, `Host.run` (line 219) and `Host.exec` (line 247): `timeout: float | None = None` → `timeout: float = DEFAULT_COMMAND_TIMEOUT`.

In `Host.run`'s docstring replace the timeout paragraph (lines 230-232) with:

```text
            timeout: Per-command timeout for a single command, or a cumulative
                budget shared across all commands in a sequence. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`. Execution is always bounded;
                pass ``float("inf")`` for a deliberately unbounded command.
```

- [ ] **Step 4: Change `BaseHost.run`**

Signature (lines 622-624):

```python
        timeout: Annotated[
            float, Opt(help="Per-command/cumulative timeout (seconds).")
        ] = DEFAULT_COMMAND_TIMEOUT,
```

Body — validate as the first statement (line 657, before `default_expects`):

```python
        timeout = _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)
```

Docstring (lines 641-645) — append to the `timeout:` entry:

```text
                Defaults to :data:`DEFAULT_COMMAND_TIMEOUT`; pass
                ``float("inf")`` to opt out of the bound.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_shell_command.py tests/unit/host/test_run_timeout.py tests/unit/host/test_privilege.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Run the full unit host suite**

Run: `uv run pytest tests/unit/host -q -p no:cacheprovider`
Expected: PASS. Any other file asserting a forwarded `timeout` surfaces here — fix it the same way, respecting the single-vs-list distinction.

- [ ] **Step 7: Static gate and commit**

```bash
make check-python
git add src/otto/host/host.py tests/unit/host/test_shell_command.py tests/unit/host/test_run_timeout.py
git commit -m "feat(host): default Host.run to a 30s bound and validate the value

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `exec` becomes a validating template over `_exec_one`

`BaseHost.exec` currently only raises `NotImplementedError`. Making it concrete gives one validation point instead of four, mirrors the existing `run`/`_run_one` pattern, and means a future host subclass cannot forget it.

**Files:**
- Modify: `src/otto/host/host.py:701-708`, `src/otto/host/local_host.py:212-226`, `src/otto/host/unix_host.py:640-695`, `src/otto/host/embedded_host.py:449-465`, `src/otto/host/docker_host.py:298-312`
- Modify: `tests/unit/host/test_unix_host.py:607`, `tests/unit/host/test_embedded_host.py:385`

**Interfaces:**
- Consumes: `DEFAULT_COMMAND_TIMEOUT`, `_validate_timeout` (Task 1).
- Produces: `BaseHost.exec(cmd: str, timeout: float = DEFAULT_COMMAND_TIMEOUT, log: LogMode = LogMode.NORMAL) -> CommandResult` (concrete, final — subclasses must **not** override) and the abstract hook `BaseHost._exec_one(cmd: str, timeout: float, log: LogMode) -> CommandResult`. `DockerContainerHost._exec_via_parent` keeps calling the *parent's* public `exec`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_run_timeout.py`:

```python
class TestExecTemplate:
    """exec() validates once in BaseHost and delegates to _exec_one."""

    @pytest.mark.asyncio
    async def test_exec_forwards_the_default(self, host: UnixHost):
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        ok = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        with patch.object(host, "_exec_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.exec("x")
        assert mock.await_args.kwargs["timeout"] == DEFAULT_COMMAND_TIMEOUT

    @pytest.mark.asyncio
    async def test_exec_rejects_bad_timeout_before_dispatch(self, host: UnixHost):
        with patch.object(host, "_exec_one", new_callable=AsyncMock) as mock:
            with pytest.raises(ValueError, match="must be >= 0"):
                await host.exec("x", timeout=-1)
        mock.assert_not_awaited()

    def test_no_subclass_overrides_exec(self):
        """exec is final; family behavior belongs in _exec_one."""
        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.host import BaseHost
        from otto.host.local_host import LocalHost

        for cls in (LocalHost, UnixHost, EmbeddedHost, DockerContainerHost):
            assert "exec" not in vars(cls), f"{cls.__name__} must override _exec_one, not exec"
            assert "_exec_one" in vars(cls), f"{cls.__name__} must implement _exec_one"
            assert BaseHost.exec is cls.exec
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestExecTemplate -v -p no:cacheprovider`
Expected: FAIL — `exec` is still defined on every subclass.

- [ ] **Step 3: Make `BaseHost.exec` a template**

Replace `BaseHost.exec` (`src/otto/host/host.py:701-708`) with:

```python
    async def exec(
        self,
        cmd: str,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run a single command outside the persistent shell session.

        Validates *timeout* and delegates to :meth:`_exec_one`, which each host
        family implements. Do not override this method — override
        :meth:`_exec_one`, so the validation cannot be bypassed.

        Args:
            cmd: Shell command to run.
            timeout: Seconds before the command is abandoned. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`; pass ``float("inf")`` for a
                deliberately unbounded command.
            log: Logging disposition for this call.
        """
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._exec_one(cmd, timeout=timeout, log=log)

    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Family-specific stateless command runner. Subclasses override."""
        raise NotImplementedError from None
```

Note the dry-run check moves *up* into the template, replacing four copies. Behavior is unchanged: it still short-circuits before any family-specific work (including `DockerContainerHost`'s `_ensure_running`).

- [ ] **Step 4: Rename the four subclass implementations**

In each file, rename `async def exec` → `async def _exec_one`, keep `@override`, change the signature's `timeout: float | None = None` → `timeout: float`, and **delete the now-duplicated `if is_dry_run(): return self._dry_run_result(cmd)`** from each body. Keep every docstring — they document real per-family differences.

- `src/otto/host/local_host.py:212` → body becomes `return await self._exec_subprocess(cmd, timeout, log=self._effective_log(log))`
- `src/otto/host/unix_host.py:640` → body becomes `return await self._session_mgr.exec(cmd, timeout=timeout, log=self._effective_log(log))`
- `src/otto/host/embedded_host.py:449` → body becomes `return await self._session_mgr.run_cmd(cmd, timeout=timeout, log=self._effective_log(log))`
- `src/otto/host/docker_host.py:298` → body becomes `return await self._exec_via_parent(cmd, timeout, log=log)`

In each docstring, replace any sentence describing `None` as disabling the timeout (notably `unix_host.py`'s "``None`` (the default) disables the timeout — appropriate for long-running commands such as a netcat listener") with:

```text
                Defaults to :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`;
                pass ``float("inf")`` for a deliberately unbounded command such
                as a netcat listener awaiting a connection.
```

- [ ] **Step 5: Update the two forwarding assertions**

`tests/unit/host/test_unix_host.py:605-609` — `h._session_mgr.exec.assert_awaited_once_with(...)` now expects `timeout=DEFAULT_COMMAND_TIMEOUT`. Import the constant.

`tests/unit/host/test_embedded_host.py:383-387` — `host._session_mgr.run_cmd.assert_awaited_once_with(...)` likewise.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Static gate**

Run: `make check-python`
Expected: clean. `ty` will flag any remaining internal caller passing `None` into `exec` — fix at the call site, never by widening the parameter back.

- [ ] **Step 8: Commit**

```bash
git add src/otto/host/host.py src/otto/host/local_host.py src/otto/host/unix_host.py src/otto/host/embedded_host.py src/otto/host/docker_host.py tests/unit/host/
git commit -m "refactor(host): make exec a validating template over _exec_one

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `HostSession.run` — the third public entry point

`HostSession` is exported from `otto.host` (`src/otto/host/__init__.py:55`) and documented in `docs/guide/docker.md`; callers reach it via `await host.open_session(name)`. Its `timeout` currently defaults to `10.0`, so this is a **loosening** to 30s — intentional, for one advertised default everywhere.

**Files:**
- Modify: `src/otto/host/session.py:40` (import), `:1078-1084` (signature), `:1094` (local import), body
- Test: `tests/unit/host/test_session.py`

**Interfaces:**
- Consumes: `DEFAULT_COMMAND_TIMEOUT`, `_validate_timeout` (Task 1).
- Produces: `HostSession.run(cmds, expects=None, timeout: float = DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL) -> Results`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_session.py`:

```python
class TestHostSessionRunTimeout:
    """HostSession.run is a public entry point and validates like BaseHost.run."""

    @pytest.mark.asyncio
    async def test_default_is_the_shared_constant(self):
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT
        from otto.host.session import HostSession

        sig = inspect.signature(HostSession.run)
        assert sig.parameters["timeout"].default == DEFAULT_COMMAND_TIMEOUT
        assert sig.parameters["timeout"].annotation in (float, "float")

    @pytest.mark.asyncio
    async def test_rejects_a_negative_timeout(self):
        from otto.host.session import HostSession

        session = HostSession.__new__(HostSession)
        with pytest.raises(ValueError, match="must be >= 0"):
            await session.run("echo hi", timeout=-5)
```

Add `import inspect` to the file's imports if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_session.py::TestHostSessionRunTimeout -v -p no:cacheprovider`
Expected: FAIL — default is `10.0`, and no validation happens.

- [ ] **Step 3: Implement**

`src/otto/host/session.py:40` — `from .host import DEFAULT_COMMAND_TIMEOUT, ShellCommand`

Signature (line 1081) — `timeout: float | None = 10.0` → `timeout: float = DEFAULT_COMMAND_TIMEOUT`.

Line 1094's local import gains the validator:

```python
        from .host import _normalize_expects, _resolve_command, _run_cmds_with_budget, _validate_timeout

        timeout = _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)
```

Docstring — add this paragraph:

```text
        *timeout* defaults to :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`
        and behaves exactly as on :meth:`~otto.host.host.Host.run`: a
        per-command bound for a single command, a cumulative budget for a
        sequence. Pass ``float("inf")`` for a deliberately unbounded command.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_session.py -q -p no:cacheprovider`
Expected: PASS. Any sibling test relying on the old 10s default will surface here — update it to pass `timeout=10.0` explicitly rather than reinstating the old default.

- [ ] **Step 5: Static gate and commit**

```bash
make check-python
git add src/otto/host/session.py tests/unit/host/test_session.py
git commit -m "feat(host): give HostSession.run the shared default and validation

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Narrow the internal seams and delete the dead `None` branches

The three entry points now guarantee a real number, so the `None` arms below them are unreachable. Deleting them is the substantive payoff: unbounded stops being a *branch* and becomes merely a *value*.

**Files:**
- Modify: `src/otto/host/host.py` — `_run_cmds_with_budget` (134-177), `BaseHost._run_one` (691-699)
- Modify: `src/otto/host/session.py` — `ShellSession.run_cmd` (365-372, 406-411), `SessionManager.run_cmd` (1495-1500), `SessionManager.exec` (1523-1528)
- Modify: `src/otto/host/{local,unix,embedded,docker}_host.py` — `_run_one` signatures

**Interfaces:**
- Consumes: validated `float` from Tasks 5-7.
- Produces: `_run_cmds_with_budget(run_one, cmds, timeout: float) -> Results`; `_run_one(cmd, expects=None, timeout: float = DEFAULT_COMMAND_TIMEOUT, log=LogMode.NORMAL)`. `ShellCommand.timeout` and `_resolve_command`'s `default_timeout` **keep** `float | None` — there `None` means "inherit", a still-needed meaning.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_run_timeout.py`:

```python
class TestNoUnboundedBranch:
    """Every command goes through wait_for; inf needs no bypass."""

    def test_run_cmds_with_budget_takes_a_plain_float(self):
        import inspect

        from otto.host.host import _run_cmds_with_budget

        ann = inspect.signature(_run_cmds_with_budget).parameters["timeout"].annotation
        assert ann in (float, "float"), f"expected plain float, got {ann!r}"

    @pytest.mark.asyncio
    async def test_infinite_timeout_still_completes(self):
        """inf flows through the same wait_for path as any other value."""
        host = LocalHost(element="local", log=LogMode.QUIET)
        try:
            result = await host.exec("echo hi", timeout=float("inf"))
        finally:
            await host.close()
        assert result.status == Status.Success
        assert "hi" in result.value
        assert result.timed_out is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestNoUnboundedBranch -v -p no:cacheprovider`
Expected: FAIL on the annotation assertion (`float | None`).

- [ ] **Step 3: Narrow `_run_cmds_with_budget`**

In `src/otto/host/host.py`, replace the body's deadline/remaining logic:

```python
async def _run_cmds_with_budget(
    run_one: Callable[[ShellCommand, float], Awaitable[CommandResult]],
    cmds: list[ShellCommand],
    timeout: float,
) -> Results:
    """Run a list of commands sequentially under a shared timeout budget.

    Each command receives the minimum of its own ``ShellCommand.timeout`` and
    the remaining budget; when the budget is exhausted, remaining commands are
    skipped with ``Status.Error``. Used by both ``BaseHost.run`` and
    ``HostSession.run`` so the budgeting logic lives in one place.

    *timeout* is always a real number — the public entry points validate it —
    so there is no unbounded branch here. ``float("inf")`` yields an infinite
    deadline, which every comparison below handles naturally.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    entries: list[CommandResult] = []

    for sc in cmds:
        remaining = deadline - loop.time()
        if remaining <= 0:
            entries.append(
                CommandResult(
                    status=Status.Error,
                    value="Skipped: cumulative timeout budget exhausted",
                    command=sc.cmd,
                    retcode=-1,
                    timed_out=True,
                )
            )
            continue

        effective = remaining if sc.timeout is None else min(sc.timeout, remaining)
        entries.append(await run_one(sc, effective))

    return Results.collect(entries)
```

- [ ] **Step 4: Narrow the `_run_one` signatures**

`BaseHost._run_one` (`host.py:695`) and all four overrides (`local_host.py:196`, `unix_host.py:601`, `embedded_host.py:432`, `docker_host.py:337`): `timeout: float | None = 10.0` → **`timeout: float`, with no default at all.**

Dropping the default rather than replacing it with `DEFAULT_COMMAND_TIMEOUT` is deliberate and is the rule for all three per-family hooks (`_run_one`, `_exec_one` in Task 6, `_expect_one` in Task 9): each is *always* called explicitly by its template, so any default is dead code that can only drift from the real one. The old `10.0` was exactly that kind of cosmetic default — it silently disagreed with the advertised value for as long as it existed. One rule: **the default lives on the public surface; the hooks require the value.**

In `unix_host.py:_run_one`'s docstring, replace "``None`` disables the timeout (use for long-running commands)" with "pass ``float(\"inf\")`` for a deliberately unbounded command".

- [ ] **Step 5: Make `ShellSession.run_cmd` bound unconditionally**

`src/otto/host/session.py` — signature (line 369) `timeout: float | None = None` → `timeout: float = DEFAULT_COMMAND_TIMEOUT`, and collapse the conditional (lines 406-411):

```python
        try:
            # Always bounded: `inf` never fires, so an intentionally unbounded
            # command needs no separate branch.
            return await asyncio.wait_for(
                self._run_cmd_inner(cmd, expects, sink, redact, write_progress),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
```

Also narrow `SessionManager.run_cmd` and `SessionManager.exec` to `timeout: float = DEFAULT_COMMAND_TIMEOUT`, and narrow the `exec_factory` constructor parameter from `Callable[[str, float | None], Awaitable[CommandResult]] | None` to `Callable[[str, float], Awaitable[CommandResult]] | None`. These are internal — only the entry points reach them — so no validation is added.

**Then DELETE BOTH temporary `None`-coercion shims.** Tasks 6 and 7 narrowed their callees while `SessionManager`'s signatures were still `float | None`, and each bridged the resulting contravariance with a coercion. Both exist **only** because of that task-ordering artifact, and both become dead weight the moment the signatures above are narrowed:

1. **`src/otto/host/docker_host.py`** (~line 177) — a local `_exec_factory` closure wrapping `_exec_via_parent`. Once `exec_factory` is narrowed, pass `exec_factory=self._exec_via_parent` directly again and delete the closure and its docstring.
2. **`src/otto/host/session.py`** (~line 1574, in `SessionManager.exec`) — a `session_timeout = timeout if timeout is not None else DEFAULT_COMMAND_TIMEOUT` local, added because `HostSession.run` now rejects an explicit `None`. Once `SessionManager.exec` takes plain `float`, use `timeout` directly and delete the local and its comment.

**Gate — this must return nothing when the task is done:**

```bash
grep -rn "is not None else DEFAULT_COMMAND_TIMEOUT" src/
```

This matters beyond tidiness: each shim is a `None`-coercion sitting in the middle of a codebase whose entire point is that `None` no longer reaches command execution. Leaving either would preserve, in code, exactly the ambiguity this work item exists to remove.

**Plan-sequencing lesson, recorded for the future:** these shims were avoidable. This plan narrowed the public entry points (Tasks 5-7) *before* the internal seams they call (Task 8), so every entry-point narrowing transiently broke the still-wide internals beneath it. Ordering Task 8 first — narrow inward-out, callees before callers — would have produced no shims at all. When a refactor tightens a type across layers, tighten the innermost layer first.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host -q -p no:cacheprovider`
Expected: PASS.

Run: `uv run pytest tests/unit -q -p no:cacheprovider`
Expected: PASS — this is the first task that touches shared session plumbing, so widen the net.

- [ ] **Step 7: Static gate and commit**

```bash
make check-python
git add src/otto/host/ tests/unit/host/test_run_timeout.py
git commit -m "refactor(host): delete the unbounded branches below the entry points

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Unify the `expect` command surface

**This task fixes a live defect found while auditing command-surface consistency.** `Host.expect`'s protocol (`host.py:294`) and `BaseHost.expect` (`host.py:754`) both promise `timeout: float = 30.0`, but **every concrete implementation silently uses `10.0`** — so a caller reading the documented interface gets a third of the advertised wait. `HostSession.expect` is also `10.0` while `ShellSession.expect` beneath it is `30.0`.

`expect` is not `cli_exposed`, so there is no CLI surface change. The four implementations are near-identical — the only real differences are the dry-run message wording and `DockerContainerHost`'s extra `_ensure_running()` — so the same template treatment as Task 6 removes the duplication and the disagreement at once.

**Files:**
- Modify: `src/otto/host/host.py:291-300` (protocol), `:751-757` (→ template + `_expect_one`)
- Modify: `src/otto/host/local_host.py:303-315`, `unix_host.py:739-751`, `embedded_host.py:490-502`, `docker_host.py:373-385`
- Modify: `src/otto/host/session.py:1169` (`HostSession.expect`), `:342` (`ShellSession.expect`), `:1760-1770` (`SessionManager.expect`)
- Modify: `src/otto/host/login_proxy.py:61`, `src/otto/host/privilege.py:64` (mixin protocol declarations)
- Modify: `src/otto/host/app_shell.py:275` (`cmd_timeout` references the constant)
- Test: `tests/unit/host/test_unix_host.py:1501`, `tests/unit/host/test_run_timeout.py`, `tests/unit/host/test_app_shell.py`

**Interfaces:**
- Consumes: `DEFAULT_COMMAND_TIMEOUT`, `_validate_timeout` (Task 1). Depends on nothing else — orderable anywhere after Task 1.
- Produces: `BaseHost.expect(pattern, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> str` (concrete, final) and the abstract hook `BaseHost._expect_one(pattern: str | re.Pattern[str], timeout: float) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_run_timeout.py`:

```python
class TestExpectSurfaceConsistency:
    """expect advertises one default everywhere, and the impls honour it."""

    def test_protocol_and_impls_agree_on_the_default(self):
        import inspect

        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT, BaseHost, Host
        from otto.host.local_host import LocalHost
        from otto.host.session import HostSession

        surfaces = [
            Host.expect,
            BaseHost.expect,
            HostSession.expect,
            LocalHost._expect_one,
            UnixHost._expect_one,
            EmbeddedHost._expect_one,
            DockerContainerHost._expect_one,
        ]
        for fn in surfaces:
            default = inspect.signature(fn).parameters["timeout"].default
            assert default == DEFAULT_COMMAND_TIMEOUT, f"{fn.__qualname__} disagrees: {default}"

    def test_no_subclass_overrides_expect(self):
        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.host import BaseHost
        from otto.host.local_host import LocalHost

        for cls in (LocalHost, UnixHost, EmbeddedHost, DockerContainerHost):
            assert "expect" not in vars(cls), f"{cls.__name__} must override _expect_one"
            assert BaseHost.expect is cls.expect

    @pytest.mark.asyncio
    async def test_expect_rejects_bad_timeout_before_dispatch(self, host: UnixHost):
        with patch.object(host, "_expect_one", new_callable=AsyncMock) as mock:
            with pytest.raises(ValueError, match="must be >= 0"):
                await host.expect("prompt", timeout=-1)
        mock.assert_not_awaited()
```

Note `LocalHost._expect_one` is referenced unbound, so no instance or event loop is needed for the signature checks.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_run_timeout.py::TestExpectSurfaceConsistency -v -p no:cacheprovider`
Expected: FAIL — `AttributeError: ... has no attribute '_expect_one'`. This is the defect made visible: before the fix, swapping `_expect_one` for `expect` in the first test would report `10.0 != 30.0` for all four impls.

- [ ] **Step 3: Make `BaseHost.expect` a template**

Replace `BaseHost.expect` (`src/otto/host/host.py:751-757`):

```python
    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for *pattern* in the session output.

        Validates *timeout* and delegates to :meth:`_expect_one`. Do not
        override this method — override :meth:`_expect_one`, so the validation
        and the advertised default cannot drift per host family.

        Args:
            pattern: A literal string or compiled regex to match against output.
            timeout: Maximum seconds to wait. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`; pass ``float("inf")`` to wait
                indefinitely.
        """
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            self._log_command(
                "[DRY RUN] expect() skipped — pattern would never match without a live session"
            )
            return ""
        return await self._expect_one(pattern, timeout)

    async def _expect_one(
        self,
        pattern: str | re.Pattern[str],
        timeout: float,
    ) -> str:
        """Family-specific pattern wait. Subclasses override."""
        raise NotImplementedError from None
```

The four dry-run messages differed only in wording ("live session" vs "live connection"); one shared message replaces them. No test or doc asserts that text — verified by grep.

Also change the `Host` protocol's `expect` (`host.py:294`) to `timeout: float = DEFAULT_COMMAND_TIMEOUT` so the protocol names the constant rather than repeating a literal that can drift.

- [ ] **Step 4: Convert the four implementations**

In each, rename `expect` → `_expect_one`, keep `@override`, change the default to no default (it is always passed by the template — declare `timeout: float`), and delete the dry-run block now handled above:

```python
    @override
    async def _expect_one(self, pattern: str | re.Pattern[str], timeout: float) -> str:
        """Wait for a pattern in the host's session output stream."""
        return await self._session_mgr.expect(pattern, timeout)
```

`docker_host.py` keeps its extra call and its own docstring:

```python
    @override
    async def _expect_one(self, pattern: "str | re.Pattern[str]", timeout: float) -> str:
        """Wait for a pattern in the container's session output stream."""
        await self._ensure_running()
        return await self._session_mgr.expect(pattern, timeout)
```

- [ ] **Step 5: Align the session layer and the mixin protocols**

`src/otto/host/session.py`:

- `HostSession.expect` (line 1169) — `timeout: float = 10.0` → `DEFAULT_COMMAND_TIMEOUT`, **and it validates**, because `HostSession` is public (exported from `otto.host`, documented) and its sibling `HostSession.run` validates:

```python
    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for a pattern in this session's output. See :meth:`~otto.host.unix_host.UnixHost.expect`."""  # noqa: E501 — Sphinx xref
        from .host import _validate_timeout

        timeout = _validate_timeout(timeout)
        result = await self._session.expect(pattern, timeout)
        self._log_output(result, LogMode.NORMAL)
        return result
```

  This is the check that covers `AppShell` (Step 5a) — both of its waits route through here, so one validation at the boundary where a cascaded value becomes final covers the class default, the session override and the per-call override.

- `ShellSession.expect` (line 342) — `timeout: float = 30.0` → `DEFAULT_COMMAND_TIMEOUT` (same value; naming it stops the literals drifting apart again)
- `SessionManager.expect` (line 1763) — `timeout: float = 10.0` → `DEFAULT_COMMAND_TIMEOUT`

`src/otto/host/login_proxy.py:61` and `src/otto/host/privilege.py:64` declare `async def expect(self, pattern, timeout: float = 10.0) -> str` as the structural requirement these mixins place on a host. Change both to `DEFAULT_COMMAND_TIMEOUT`, importing it, so the declared requirement matches the real surface.

**Leave `src/otto/host/interact.py:363` at `10.0`.** `_ProxyIO.expect` is raw transport IO during login/proxy negotiation, not the host command surface — its 10s is a login-prompt wait, a different semantic class.

- [ ] **Step 5a: Point `AppShell` at the shared constant**

`src/otto/host/app_shell.py:275` currently hardcodes a second copy of the value:

```python
    cmd_timeout: ClassVar[float] = 30.0
```

Two equal literals are a drift risk, so import the constant and reference it:

```python
from .host import DEFAULT_COMMAND_TIMEOUT
```

```python
    cmd_timeout: ClassVar[float] = DEFAULT_COMMAND_TIMEOUT
```

This is free — `import otto.host.app_shell` already loads `otto.host.host` transitively via the package `__init__` (measured: no new modules), and it creates no cycle because `host.py` imports `app_shell` only under `TYPE_CHECKING`. Confirm with the import-budget gate in Step 9.

**Do not remove `AppShell`'s `timeout: float | None = None` parameters** (`app_shell.py:283`, `:317`, `:399`). `AppShell` has a three-level cascade — class default → per-session override via `attach()`/`BaseHost.app_shell()` → per-call override via `cmd()` — and each level's `None` means "not specified, inherit the level above". The sentinel is load-bearing: `DEFAULT_COMMAND_TIMEOUT` is itself a valid explicit value, so it cannot double as the "not specified" signal. If `cmd(timeout=...)` defaulted to the constant, then `app_shell(SomeShell, timeout=120)` followed by a plain `sh.cmd("…")` would silently use 30s instead of 120s, breaking per-session overrides.

`AppShell` needs no validation code of its own — Step 5's `HostSession.expect` covers all three levels, since both of `AppShell`'s waits (`app_shell.py:372`, `:420`) route through it.

Add to the existing `AppShell` tests (`tests/unit/host/test_app_shell.py`):

```python
def test_cmd_timeout_tracks_the_shared_constant():
    """AppShell must not carry a second copy of the default that can drift."""
    from otto.host.app_shell import AppShell
    from otto.host.host import DEFAULT_COMMAND_TIMEOUT

    assert AppShell.cmd_timeout == DEFAULT_COMMAND_TIMEOUT


@pytest.mark.asyncio
async def test_per_session_override_is_not_clobbered_by_the_default():
    """The None sentinel must keep inheriting: a session override survives cmd()."""
    from otto.host.app_shell import AppShell

    class _Shell(AppShell):
        launch = "python3"
        prompt = r">>> "

    session = MagicMock()
    session.send = AsyncMock()
    session.expect = AsyncMock(return_value=">>> ")
    sh = _Shell(session, timeout=120.0)
    await sh.cmd("1+1")
    # cmd() passed no timeout, so it must use the session's 120.0 — not the class default.
    assert session.expect.await_args.args[1] == 120.0
```

- [ ] **Step 6: Update the one test that pins the old default**

`tests/unit/host/test_unix_host.py:1501` — `shell.expect.assert_called_once_with(r"\$", 10.0)` becomes `assert_called_once_with(r"\$", DEFAULT_COMMAND_TIMEOUT)`. This is `HostSession.expect` forwarding to the inner `ShellSession`. Import the constant. Leave `test_expect_forwards_timeout` at `:1508` alone — it passes `5.0` explicitly.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 8: Verify one default across every command surface**

Run: `grep -rn "timeout: float = 10.0\|timeout: float = 30.0" src/otto/host/`
Expected: exactly one hit — `interact.py:363`, the deliberate carve-out from Step 5. Every other command surface now names `DEFAULT_COMMAND_TIMEOUT`.

- [ ] **Step 9: Static gate, import budget, and commit**

```bash
make check-python
make profile   # import budget: proves the app_shell -> host import added no modules
git add src/otto/host/ tests/unit/host/
git commit -m "fix(host): give expect one advertised default across every surface

The Host protocol and BaseHost promised 30s while all four concrete hosts
used 10s, so callers got a third of the documented wait. expect now
follows the exec/_exec_one template: validate once, delegate to
_expect_one.

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: CLI bound and advertised default

`otto host <id> run` is synthesized from `BaseHost.run` by introspection, so `[default: 30.0]` already appears after Task 5. This task adds the range bound and locks both into a test.

**Files:**
- Modify: `src/otto/utils.py:135-147` (`Opt` gains `min`), `src/otto/cli/param_synth.py:223`
- Modify: `src/otto/host/host.py` (`BaseHost.run`'s `Opt(...)` gains `min=0.0`)
- Test: `tests/unit/cli/test_dynamic_host_commands.py`

**Interfaces:**
- Consumes: `BaseHost.run`'s signature from Task 5.
- Produces: `Opt(elem_type=None, name=None, help=None, min=None)` — a fourth optional field; every existing `Opt(...)` call site is unaffected.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/cli/test_dynamic_host_commands.py`, following the `_make_app` pattern already used by `test_login_as_user_flag_renders_in_help`:

```python
def test_run_timeout_advertises_default_and_range(monkeypatch):
    """The synthesized --timeout carries BaseHost.run's default and a >=0 bound."""
    app = _make_app(monkeypatch, {"u1": UnixHost})
    r = CliRunner().invoke(app, ["u1", "run", "--help"])
    assert r.exit_code == 0, r.output
    assert "30.0" in r.output, "the default must be advertised in help"
    assert "x>=0" in r.output, "the range bound must be advertised in help"


def test_run_rejects_a_negative_timeout(monkeypatch):
    """A negative --timeout is a clean click usage error, not a traceback."""
    app = _make_app(monkeypatch, {"u1": UnixHost})
    r = CliRunner().invoke(app, ["u1", "run", "--timeout", "-5", "echo hi"])
    assert r.exit_code == 2, r.output
    assert "not in the range" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/cli/test_dynamic_host_commands.py -k "timeout" -v -p no:cacheprovider`
Expected: FAIL — no `x>=0` in help; the negative value is accepted.

- [ ] **Step 3: Add `min` to `Opt`**

`src/otto/utils.py`, in the `Opt` dataclass after `help`:

```python
    min: float | None = None
    """Inclusive lower bound forwarded to click's numeric range, or None.

    Typer exposes only an inclusive ``min`` (no ``min_open``), so design the
    accepted range so an inclusive bound expresses it exactly.
    """
```

- [ ] **Step 4: Forward it in `param_synth`**

`src/otto/cli/param_synth.py:223`:

```python
            ann = Annotated[norm, typer.Option(..., *opt_decls, help=opt.help, min=opt.min)]
```

`typer.Option`'s own default for `min` is `None`, so unbounded options are unaffected.

- [ ] **Step 5: Set the bound on `run`**

`src/otto/host/host.py`, `BaseHost.run`:

```python
        timeout: Annotated[
            float,
            Opt(help="Per-command/cumulative timeout (seconds); use inf for unbounded.", min=0.0),
        ] = DEFAULT_COMMAND_TIMEOUT,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cli -q -p no:cacheprovider`
Expected: PASS — includes `test_param_synth.py`, which covers the edited synthesizer.

- [ ] **Step 7: Static gate and commit**

```bash
make check-python
git add src/otto/utils.py src/otto/cli/param_synth.py src/otto/host/host.py tests/unit/cli/test_dynamic_host_commands.py
git commit -m "feat(cli): bound --timeout at >=0 and advertise its default

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Retire the external `asyncio.wait_for` workarounds

Nine sites drop their wrapper and pass the module's existing constant as the built-in `timeout=`. **Their error messages are contractual and must survive verbatim** — `link/manage.py:5-6` cites spec §9 ("a down host is a loud, host-named `RuntimeError` — never a skip"). Because a timeout now *returns* rather than raises, each site switches to checking `result.timed_out`.

**Two sites are deliberately left alone:** `tunnel/discovery.py:70` and `tunnel/manage.py:107` wrap `host.is_running()`, a liveness probe with no `timeout` parameter.

**Files:**
- Modify: `src/otto/link/manage.py:128-154`, `src/otto/tunnel/manage.py:79-86,298-306,321-328,341-348,459-467,511-523`, `src/otto/tunnel/discovery.py:60-82`
- Test: `tests/unit/link/`, `tests/unit/tunnel/`

**Interfaces:**
- Consumes: `CommandResult.timed_out` (Task 2); the now-load-bearing `timeout=` (Tasks 5-9).
- Produces: no signature changes; `_IMPAIR_HOST_TIMEOUT` and `_TUNNEL_HOST_TIMEOUT` are retained and passed explicitly.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/link/test_manage.py` (create the class alongside existing tests; match the file's existing fake-host fixture style):

```python
class TestTimeoutStillNamesTheHost:
    """A timed-out host command is a loud, host-named RuntimeError (spec §9)."""

    @pytest.mark.asyncio
    async def test_exec_timeout_raises_host_named_runtime_error(self):
        from otto.link.manage import _exec

        class _Host:
            id = "carrot"

            async def exec(self, cmd, timeout=None, log=None):
                return CommandResult(
                    status=Status.Error,
                    value=f"Command timed out after {timeout}s",
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )

        with pytest.raises(RuntimeError, match="carrot.*unreachable"):
            await _exec(_Host(), "tc qdisc show")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_manage.py -k TimeoutStillNames -v -p no:cacheprovider`
Expected: FAIL — the current `_exec` sees a non-ok result and raises the *generic* `"{cmd!r} failed on {host}"` message, not the unreachable one.

- [ ] **Step 3: Convert `link/manage.py`**

```python
async def _exec(host: Any, cmd: str) -> Any:
    """Run a read-only *cmd* on *host*; timeout/transport errors are host-named."""
    try:
        result = await host.exec(cmd, timeout=_IMPAIR_HOST_TIMEOUT, log=LogMode.QUIET)
    except (OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    if result.timed_out:
        raise RuntimeError(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {_IMPAIR_HOST_TIMEOUT}s"
        )
    if not result.is_ok:
        raise RuntimeError(f"{cmd!r} failed on {host.id!r}: {result.msg or result.value}")
    return result


async def _root_run(host: Any, cmd: str) -> Any:
    """Run a mutating *cmd* on *host*, sudo'd unless already root.

    A non-ok result is deliberately NOT raised here — a command that reaches the
    host but reports failure is caught by the caller's own re-read
    (:func:`impair_link`'s post-apply verify, :func:`repair_link`'s post-clear
    re-read), never silently swallowed here.
    """
    need_sudo = host.current_user != "root"
    try:
        results = await host.run(
            cmd, sudo=need_sudo, timeout=_IMPAIR_HOST_TIMEOUT, log=LogMode.QUIET
        )
    except (OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    if results[0].timed_out:
        raise RuntimeError(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {_IMPAIR_HOST_TIMEOUT}s"
        )
    return results[0]
```

Update the module docstring (lines 4-6) to say every host call passes `timeout=_IMPAIR_HOST_TIMEOUT` rather than being wrapped in `asyncio.wait_for`. Keep the "loud, host-named `RuntimeError` — never a skip (spec §9, dev-VM rule)" sentence exactly.

- [ ] **Step 4: Convert the six `tunnel/` sites**

Each follows the same shape — pass the constant, then branch on `timed_out` and raise/log the *existing* message unchanged. Four raise:

- `manage.py:79-86` → `f"host {container.parent.id!r} timed out inspecting container {container.id!r}"`
- `manage.py:298-306` → `f"host {host.id!r} timed out checking for {carrier.tools_description}"`
- `manage.py:321-328` → `f"host {r.hop.host!r} timed out probing for free ports"`
- `manage.py:459-467` → `f"host {_proc_host_name(resolved, proc)!r} timed out spawning the tunnel"`

For example `:298-306` becomes:

```python
    result = await host.exec(
        carrier.requirements_command, timeout=_TUNNEL_HOST_TIMEOUT, log=LogMode.QUIET
    )
    if result.timed_out:
        raise RuntimeError(f"host {host.id!r} timed out checking for {carrier.tools_description}")
```

Two do **not** raise:

- `manage.py:511-523` (reap) — on `result.timed_out`, `logger.warning(f"otto tunnel: timed out reaping host {host_id!r}")`, `unreachable.add(host_id)`, `continue`. Keep the broad `except Exception` for non-timeout transport failures.
- `manage.py:341-348` (rollback reap) — best-effort; keep swallowing. Pass the timeout and leave the `except Exception` warning path intact.
- `discovery.py:72-78` — on `result.timed_out`, `logger.warning(f"otto tunnel: timed out scanning host {host.id!r}")` and `return [], host.id`. **Keep** the `asyncio.wait_for` on the `probe()` call at line 70 and add a comment: `is_running()` has no timeout parameter, so it stays externally bounded.

At each converted site, `asyncio.TimeoutError` can no longer arrive from the host call. Remove only the `except asyncio.TimeoutError` clauses that become unreachable; leave the broader `except Exception` handlers, which still catch transport errors.

- [ ] **Step 5: Verify the conversion is complete and the carve-outs remain**

Run: `grep -rn "asyncio.wait_for" src/otto/tunnel/ src/otto/link/`
Expected: exactly two hits — `discovery.py` (the `probe()` call) and `manage.py:107` (`host.is_running()`).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/link tests/unit/tunnel -q -p no:cacheprovider`
Expected: PASS. Tests that patched a host `exec` to hang and relied on the external wrapper must now return a `timed_out=True` result instead — update those fakes.

- [ ] **Step 7: Static gate and commit**

```bash
make check-python
git add src/otto/link/manage.py src/otto/tunnel/manage.py src/otto/tunnel/discovery.py tests/unit/link tests/unit/tunnel
git commit -m "refactor(tunnel,link): use the built-in host timeout, drop the wrappers

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Forwarding wrappers, documentation, and the full gate

**Files:**
- Modify: `src/otto/context.py:236-260`, `src/otto/config/fleet.py:265-315`
- Modify: `src/otto/host/host.py` (`Host.run` protocol docstring), `docs/guide/cli-reference.md:256-260`
- Test: `tests/unit/test_context.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_on_all_hosts(..., timeout: float = DEFAULT_COMMAND_TIMEOUT, ...)` in both modules.

- [ ] **Step 1: Adopt the default in both wrappers**

Both declare `timeout: float | None = None` and forward it into `host.run(...)`. Change each to `timeout: float = DEFAULT_COMMAND_TIMEOUT` (`context.py:238`, `fleet.py:269`, and `do_for_all_hosts` at `fleet.py:190` if it declares one). Update `fleet.py:291`'s docstring line to name the default.

**`ty` will NOT find these for you.** `BaseHost.run` carries `@cli_exposed`, which is typed `Callable[..., Any] -> Callable[..., Any]` and therefore erases the method's signature from `ty`'s view — measured under `all = "error"`, a decorated method silently accepts `timeout=None`, `timeout="banana"`, and even a nonexistent `bogus_kwarg`. Earlier drafts of this plan claimed the typecheck gate would flag these wrappers; it will not. Find them by grep:

```bash
grep -rn "timeout" src/otto/context.py src/otto/config/fleet.py
grep -rn "float | None" src/otto/ | grep -i timeout
```

- [ ] **Step 2: Confirm `ty` is clean, and grep for what `ty` cannot see**

Run: `make check-python`
Expected: clean. Any remaining `invalid-argument-type` is a forwarder into an *undecorated* surface (`exec` / `expect`) — fix it at the source, never by widening a signature back to `float | None`.

Then run the two greps above, because forwarders into `run` are invisible to the gate. Expected: no `timeout: float | None` remains in `src/otto/` except `ShellCommand.timeout` and `_resolve_command`'s `default_timeout`, which keep it deliberately (there `None` means "inherit", not "unbounded").

- [ ] **Step 3: Fix the test double**

`tests/unit/test_context.py:191` defines `async def run(self, cmds, timeout=None)`. As a fake it still works, but update it to `timeout=DEFAULT_COMMAND_TIMEOUT` so the double mirrors the real signature.

- [ ] **Step 3a: Update the prose docs that docstrings do NOT cover**

Docstrings carry the API surface, but three hand-written docs make claims that this work falsifies. Sphinx `-W` cannot catch any of them — two are fenced code samples and one is prose, none are cross-references.

**`docs/guide/hosts/capabilities.md`** (~line 353) uses `run`'s `timeout` as *the canonical example* of the `Opt(...)` pattern, quoting the signature verbatim:

```python
timeout: Annotated[float | None, Opt(help="Timeout in seconds.")] = None
```

Every part of that is now wrong — the annotation, the default, and (after Task 10) the `Opt` arguments. Replace it with the real current signature, including `min=0.0`:

```python
timeout: Annotated[float, Opt(help="Per-command/cumulative timeout (seconds); use inf for unbounded.", min=0.0)] = DEFAULT_COMMAND_TIMEOUT
```

This one matters more than a typical stale sample: readers copy it as the pattern to imitate when adding new CLI-exposed parameters.

**`docs/architecture/utilities/results.md`** (~lines 18-19) enumerates what `CommandResult` adds over `Result` — "the `command` string and the shell `retcode` (`-1` means the command never ran)". It must now also document the new public `timed_out` field, and the `-1` sentence needs correcting: `-1` no longer implies "never ran", because it is also used for "timed out" and "skipped: cumulative budget exhausted". Say that `timed_out` is how a caller distinguishes a timeout from an ordinary failure, since `retcode` cannot.

**`docs/library/sessions.md`** needs **no change** — verified: its `send`/`expect` examples pass `timeout=5.0` explicitly and it makes no claim about defaults. Recorded so a later reader does not re-audit it.

- [ ] **Step 4: Update the CLI reference**

`docs/guide/cli-reference.md:256-260` — give the `run` options table a `Default` column, matching the `reboot` table at `:296`:

```markdown
| Option | Default | Description |
| ------ | ------- | ----------- |
| `COMMANDS...` | — | One or more shell commands (space-separated, each quoted as needed) |
| `--sudo / --no-sudo` | `--no-sudo` | Run every command through `sudo` |
| `--timeout SECS` | `30.0` | Cumulative timeout in seconds across all commands. Must be `>= 0`; pass `inf` for a deliberately unbounded command |
```

- [ ] **Step 5: Grep for surviving stale claims**

Run: `grep -rn "no limit\|disables the timeout\|None means" src/otto/host/ docs/guide/`
Expected: no output. Any hit is a docstring still promising unbounded-by-default — fix it.

- [ ] **Step 6: Clean docs build**

Run: `make docs`
Expected: PASS. Incremental Sphinx `-W` misses broken `:doc:`/`:data:` refs inside docstrings, so this must be a clean build — the new `:data:`DEFAULT_COMMAND_TIMEOUT`` cross-references are exactly the kind of thing it catches.

- [ ] **Step 7: Full gate**

Run: `make coverage`
Expected: PASS with the 95% floor held. This is the only task that runs the full suite (it needs lab VMs); everything before it gated on `coverage-unit`. If the lab is unavailable, run `make coverage-hostless` and say so explicitly in the handoff rather than reporting a pass you did not observe.

- [ ] **Step 8: Commit**

```bash
git add src/otto/context.py src/otto/config/fleet.py src/otto/host/host.py docs/guide/cli-reference.md tests/unit/test_context.py
git commit -m "docs(host): document the default command timeout and the inf escape hatch

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Release note (for the eventual CHANGELOG entry)

> **Breaking:** `Host.run()`, `Host.exec()` and `HostSession.run()` now default to a 30-second timeout instead of running unbounded, and their `timeout` parameter no longer accepts `None`. Replace `timeout=None` with `timeout=float("inf")` for a deliberately unbounded command. A sequence passed to `run([...])` shares the 30s as a cumulative budget, so long multi-command calls need an explicit timeout. `HostSession.run()`'s default moves from 10s to 30s.
