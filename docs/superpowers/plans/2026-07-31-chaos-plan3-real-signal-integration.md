# Chaos Plan 3: Real-Signal Integration Implementation Plan

> **Status: DRAFT (executed per the Plans 1/2 precedent: write → self-review → execute).** Judgment calls a reviewer should weigh, settled as drafted unless Chris says otherwise:
>
> 1. **Monitor signal ownership (product change, Task 2):** uvicorn 0.51's `Server.serve()` wraps the whole serve window in `capture_signals()` — raw `signal.signal(SIGINT/SIGTERM, …)` — displacing the loop handlers `run_command` installs, so today a signal to `otto monitor` takes uvicorn's two-stage policy (no otto status line, no teardown deadline, racy exit code once the captured signal is re-raised after the drain). Decision: **otto owns interrupt policy uniformly.** A `uvicorn.Server` subclass neuters the capture; shutdown is driven by cancellation (first signal cancels the command body; `MonitorServer.serve` translates that into uvicorn's graceful drain, still bounded by the lifecycle teardown deadline / force path).
> 2. **The stderr interrupt banner IS the teardown-phase marker.** The spec's "teardown started" log line was a placeholder — no such line exists, and close chains are deliberately silent on success. Rather than adding product log lines, tier-2 uses the existing unbuffered stderr banner (`otto: interrupted — cleaning up remote sessions …`) as the "teardown running" phase marker. Zero product-logging changes.
> 3. **Loopback sshd is the default venue everywhere** — dev VM, GitHub nightly, anyone's laptop. The suite therefore rides `make coverage-python` locally (it lives under `tests/integration/`, which that gate collects); budget ≈1–2 min of sequential wall-clock. `OTTO_CHAOS_BED_HOST=carrot|tomato|pepper` opts a lab run into a bed host instead; signals only ever go to the **local** otto process in either mode.
> 4. **Deterministic force path via `OTTO_TEARDOWN_DEADLINE=0`**, not racing second signals. "Forced" means teardown lost the race (Plan 1); with deadline 0 it always loses, no timing. The double-signal test asserts only prompt exit + exit code — racing real signals for a guaranteed force is inherently flaky and tier-1 already proves the state machine. **Execution discovery (Task 5's gate runs):** under full-suite load the banner-gated second SIGINT can land *after* `_main` has removed its handlers — the process then dies to the OS default disposition, exit `-2`. That is the documented third-signal window reached at signal #2; the product cannot atomically exit-and-keep-handlers, so the honest double-signal contract is "prompt exit, never a wedge, code 130 (otto's handler) or signal-death −2 (post-teardown window) — never anything else", and the test asserts exactly that.
> 5. **`_on_signal`'s status-line guard widens to `except (OSError, ValueError)`** (Task 1): a *closed* stderr file object raises `ValueError`, not `OSError` — Plan 1 review carry-over.
> 6. **Third-signal behavior is deliberately untested.** After `_main` removes its handlers, a third SIGINT during `asyncio.run` finalization surfaces as a bare `KeyboardInterrupt` (SIGTERM: `SIG_DFL` kill). Hitting that window from outside is pure timing — not marker-gatable — so tier 2 documents it (module docstring) instead of flaking on it.
> 7. **`InteractiveOttoSession` gains an additive `extra_env` keyword** (Task 5) so the PTY tests can inject `OTTO_TEARDOWN_DEADLINE`; default `None`, existing callers unaffected.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tier 2 of the chaos harness — real SIGINT/SIGTERM delivered to a real `otto` subprocess at log-derived phase markers, in a new `tests/integration/chaos/` suite backed by a hermetic loopback-sshd host fixture, plus the two small product fixes the tier exposed (status-line guard, monitor signal ownership) and a GitHub nightly job.

**Architecture:** A session-scoped fixture starts a throwaway, non-root `sshd` on `127.0.0.1:<ephemeral>` (own host key, pubkey auth as the current user, config + keys in tmp) and generates a matching otto SUT dir — `settings.toml` + `lab.json` defining one `UnixHost` (`element: "loopback"`, no board ⇒ CLI id `loopback`) whose per-host `ssh_options` carry the port and client key; `known_hosts=None` is already otto's default. Tests spawn `otto -l chaos --log-level DEBUG …` as a subprocess (`start_new_session=True`, stdout/stderr to files), wait for a **phase marker** — a line in the run's `verbose.log` (flushed per record by the file sink; polled, because a QueueListener thread sits between `logger.*()` and the file) or the interrupt banner on stderr — then `os.kill` the child and assert: exit code 130/143, banner presence, empty process group, remote command reaped (graceful paths only; via an independent asyncssh probe connection), and for `login`, termios restored on the PTY master. Product changes are confined to `src/otto/lifecycle.py` (2-line guard) and `src/otto/monitor/server.py` (signal ownership).

**Tech Stack:** Python 3.10 asyncio, pytest (sync tests driving subprocesses; `xdist_group("chaos")` serializes under `--dist loadgroup`), OpenSSH `sshd` (present on the dev VM and `ubuntu-latest`), asyncssh (probe connections), the existing PTY driver `tests/e2e/host/_pty_driver.py`, uvicorn 0.51.

## Global Constraints

- Python floor is 3.10. `X | None` annotations fine; `asyncio.Runner`, `asyncio.timeout`, `except*` are NOT.
- NEVER add `from __future__ import annotations` (Sphinx nitpicky `-W`; repo-wide ban).
- **Real signals go ONLY to spawned subprocesses.** Never install real signal handlers in-process: unit tests use the `_CommandRun(install_handlers=False)` / `_controller` seam (the root-conftest guard enforces this); the tier-2 tests deliver signals with `os.kill`/`send_signal` to their own children exclusively.
- **Never signal, reboot, or load real lab hosts.** Default venue is the loopback sshd this suite owns. `OTTO_CHAOS_BED_HOST` (opt-in, lab only) merely points the otto *subprocess* at a bed host running benign `sleep`s — the signal target is always the local otto process.
- **Waits are condition-polls with deadlines, never fixed sleeps.** Every helper polls (interval ≤ 0.1 s) for an observable condition — a log line, a pgrep result, process exit — under an explicit timeout. The only bare `time.sleep` allowed is the poll interval inside such a loop.
- Sequential by construction: **every test module in `tests/integration/chaos/` carries `pytest.mark.xdist_group("chaos")`** in its `pytestmark`, plus `pytest.mark.timeout(120)`. Tests under `tests/integration/` are auto-stamped `integration` (excluded from CI PR gates; collected by `tests_integration`/`tests_all`/`make coverage-python`).
- `filterwarnings = ["error"]` is live: asyncssh probe connections run inside `asyncio.run` via context managers so nothing leaks; spawned-otto stdout/stderr file handles are closed in fixture/driver teardown.
- pytest-asyncio strict mode: async tests carry `@pytest.mark.asyncio` (most tier-2 tests are deliberately sync).
- Lint suppressions are a failure mode: prefer restructuring; any `# noqa` needs a written justification on the same line.
- Per-task gate: scoped pytest → `uv run nox -s lint` → `make typecheck-python` → `make coverage` (there is no `make test`). Run gates FOREGROUND with `timeout: 600000` spelled into the Bash call.
- Never `git push`. Commit in the worktree with a conventional prefix; end every commit message with the trailer: `Assisted-by: Claude (Fable 5)`
- Worktree quirks: EnterWorktree branches from **origin/main** — `git reset --hard main` immediately after entering; fresh worktrees need `uv sync` and `npm ci` in `web/` before `make coverage`.

## File Structure

| File | Role in this plan |
| --- | --- |
| `src/otto/lifecycle.py` | Task 1: widen the status-line guard to `(OSError, ValueError)` |
| `tests/unit/test_lifecycle.py` | Task 1: closed-stderr regression test |
| `src/otto/monitor/server.py` | Task 2: `_LifecycleOwnedServer` (no-op `capture_signals`) + drain-on-cancel in `MonitorServer.serve` |
| `tests/unit/monitor/test_server_signals.py` (new) | Task 2: differential no-displacement test + cancel-drains test |
| `tests/integration/chaos/__init__.py` (new) | empty package marker |
| `tests/integration/chaos/_sshd.py` (new) | Task 3: keygen, sshd config/writer, `LoopbackSshd` process manager |
| `tests/integration/chaos/_target.py` (new) | Task 3: `ChaosTarget` dataclass, loopback/bed constructors, asyncssh `probe()` |
| `tests/integration/chaos/_driver.py` (new) | Task 3: `spawn_otto`, `OttoProc` (signal/wait/log-tail/stderr-tail/pgroup helpers) |
| `tests/integration/chaos/conftest.py` (new) | Task 3: session-scoped `chaos_target` fixture |
| `tests/integration/chaos/test_harness.py` (new) | Task 3: fixture certification (clean run exits 0; probe round-trips) |
| `tests/integration/chaos/test_signal_run.py` (new) | Task 4: SIGINT/SIGTERM graceful, deadline-0 forced ×2, double-signal |
| `tests/e2e/host/_pty_driver.py` | Task 5: additive `extra_env` keyword on `InteractiveOttoSession` |
| `tests/integration/chaos/test_signal_login.py` (new) | Task 5: SIGTERM during `login` — graceful + forced — termios restored |
| `tests/integration/chaos/test_signal_monitor.py` (new) | Task 6: SIGTERM during `otto monitor --live` serve |
| `.github/workflows/nightly.yml` | Task 6: `chaos-tier2` job (ubuntu-latest, loopback sshd) |

---

### Task 1: Status-line guard survives a closed stderr

`_on_signal`'s best-effort banner print is guarded by `except OSError` only (`src/otto/lifecycle.py:169`). A **closed** file object raises `ValueError` from `write()`/`flush()` — not `OSError` — so a first signal arriving after stderr is closed (e.g. supervisor tore the pipe down and the file object was closed by a logging shutdown hook) would let `ValueError` escape a signal callback. Cancellation and the deadline are already committed before the print (Plan 1 ordered it that way deliberately); the guard just needs to cover both failure shapes.

**Files:**
- Modify: `src/otto/lifecycle.py` (the `try/except OSError` around the banner write in `_on_signal`, ~line 166-170)
- Test: `tests/unit/test_lifecycle.py`

**Interfaces:**
- Consumes: `_CommandRun(teardown_deadline=…, install_handlers=False)` test seam; the existing raising-stderr test `test_on_signal_cancels_and_schedules_deadline_despite_raising_stderr` as the arrangement template.
- Produces: nothing downstream — self-contained hardening.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lifecycle.py`, next to `test_on_signal_cancels_and_schedules_deadline_despite_raising_stderr` (mirror its arrangement — read it first and reuse its helpers/fixtures verbatim where they exist):

```python
class _ClosedStderr:
    """A file object in the CLOSED state: write/flush raise ValueError, not OSError."""

    def write(self, _s: str) -> int:
        raise ValueError("I/O operation on closed file")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file")


@pytest.mark.asyncio
async def test_on_signal_survives_closed_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed stderr raises ValueError from write(), not OSError.

    The banner is best-effort diagnostics printed after cancellation and the
    deadline are committed; a diagnostics failure must never unwind the
    signal callback (chaos plan 3, carried over from plan 1's review).
    """
    monkeypatch.setattr(sys, "stderr", _ClosedStderr())
    run = _CommandRun(teardown_deadline=30.0, install_handlers=False)

    async def body() -> None:
        await asyncio.sleep(3600)

    task = asyncio.get_running_loop().create_task(run._main(body()))
    await asyncio.sleep(0)  # let _main start the body and reach its await
    run._on_signal(signal.SIGINT)  # must not raise
    assert run.interrupted == signal.SIGINT
    with pytest.raises(_InterruptedCommand):
        await task
```

If the existing raising-stderr test drives `_on_signal` differently (e.g. without running `_main`), copy THAT shape instead — the assertion that matters is `_on_signal` returning without raising while `interrupted` is set, with a closed-file `ValueError` stderr.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_lifecycle.py::test_on_signal_survives_closed_stderr -v` (foreground, timeout 600000)
Expected: FAIL — `ValueError: I/O operation on closed file` escaping `_on_signal`.

- [ ] **Step 3: Widen the guard**

In `src/otto/lifecycle.py`, `_on_signal`:

```python
        try:
            sys.stderr.write(_INTERRUPT_STATUS_LINE + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            # Best-effort: a broken pipe raises OSError, a CLOSED file
            # object raises ValueError. Either way the cancellation and
            # deadline above are already committed; diagnostics must not
            # unwind a signal callback.
            pass
```

Keep the existing surrounding comment content if it says more than this — merge, don't delete.

- [ ] **Step 4: Run the module to verify green**

Run: `uv run pytest tests/unit/test_lifecycle.py -v` (foreground, timeout 600000)
Expected: PASS, including all pre-existing tests.

- [ ] **Step 5: Gates + commit**

Run `uv run nox -s lint`, `make typecheck-python`, `make coverage` (all foreground, timeout 600000). Then:

```bash
git add src/otto/lifecycle.py tests/unit/test_lifecycle.py
git commit -m "fix(lifecycle): status-line guard survives a closed stderr

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: Monitor signal ownership — uvicorn must not displace lifecycle handlers

uvicorn 0.51's `Server.serve()` is `with self.capture_signals(): await self._serve(…)`, and `capture_signals()` rebinds SIGINT/SIGTERM via raw `signal.signal(sig, self.handle_exit)` for the whole window, then re-raises captured signals on exit. Under `run_command` this displaces otto's `loop.add_signal_handler` handlers for the entire `otto monitor` run: first Ctrl+C takes uvicorn's policy (no otto banner, no teardown deadline), and the post-drain re-raise races body completion for a nondeterministic exit code. Decision (header, item 1): otto owns interrupt policy; uvicorn's capture becomes a no-op, and cancellation (otto's first-signal body cancel) drives uvicorn's graceful drain.

**Files:**
- Modify: `src/otto/monitor/server.py` (add subclass near `MonitorServer`; edit `serve()`, currently ~lines 757-822)
- Test: `tests/unit/monitor/test_server_signals.py` (new)

**Interfaces:**
- Consumes: `MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)` (existing unit-test construction pattern — see `tests/unit/test_collector_db.py:388`; mirror THAT file's import statements for `MonitorServer`/`MetricCollector` rather than the guesses below); `server.started` property; `server._port` (rebound to the real socket port after startup).
- Produces: `MonitorServer.serve()` that (a) never alters process signal dispositions, (b) on `CancelledError` sets `should_exit`, drains uvicorn, and re-raises. Task 6's tier-2 test relies on (a)+(b) for a deterministic exit 143.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_server_signals.py`:

```python
"""MonitorServer signal ownership: uvicorn must not displace lifecycle handlers.

Chaos plan 3. ``uvicorn.Server.serve()`` wraps the whole run in
``capture_signals()`` — raw ``signal.signal(SIGINT/SIGTERM, ...)`` for the
entire serve window — which would displace the per-loop handlers
``run_command`` installs and bypass otto's two-stage interrupt policy
(status line, teardown deadline, force hooks). otto owns interrupt policy:
the server subclass neuters the capture, and shutdown is driven by
cancellation, translated here into uvicorn's graceful drain.

No real signal handlers are installed by these tests (root-conftest guard);
the displacement check is differential on ``signal.getsignal``.
"""

import asyncio
import signal

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer


async def _started_server() -> "tuple[MonitorServer, asyncio.Task[None]]":
    server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
    task = asyncio.get_running_loop().create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surface the startup failure instead of hanging
        await asyncio.sleep(0.01)
    return server, task


@pytest.mark.asyncio
async def test_serve_does_not_displace_signal_dispositions() -> None:
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    server, task = await _started_server()
    try:
        assert signal.getsignal(signal.SIGINT) is before_int
        assert signal.getsignal(signal.SIGTERM) is before_term
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cancellation_drains_uvicorn_and_reraises() -> None:
    server, task = await _started_server()
    port = server._port  # rebound to the real ephemeral port during startup
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The listener is really gone — a fresh connect must be refused. The
    # drain inside serve() awaited uvicorn's shutdown, so no lingering
    # server task exists to trip filterwarnings=error at loop close.
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
```

Note for the implementer: with stock uvicorn the first test FAILS (`getsignal` returns uvicorn's bound `handle_exit`), and the second may fail with a lingering-task warning or a still-listening socket — both are the point.

- [ ] **Step 2: Run to verify both fail**

Run: `uv run pytest tests/unit/monitor/test_server_signals.py -v` (foreground, timeout 600000)
Expected: FAIL (displacement observed; cancel path unclean).

- [ ] **Step 3: Implement**

In `src/otto/monitor/server.py` (add `import contextlib` and the `Iterator` typing import if not present):

```python
class _LifecycleOwnedServer(uvicorn.Server):
    """uvicorn Server that leaves signal handling to otto's lifecycle.

    ``uvicorn.Server.serve`` wraps its whole run in ``capture_signals()``,
    which rebinds SIGINT/SIGTERM via raw ``signal.signal`` for the entire
    serve window — displacing the per-loop handlers ``run_command``
    installs and bypassing otto's two-stage interrupt policy (status line,
    teardown deadline, force hooks). otto owns interrupt policy; shutdown
    is driven by cancellation from the lifecycle (see ``MonitorServer.serve``),
    not by uvicorn-observed signals.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> "Iterator[None]":
        yield
```

In `MonitorServer.serve()`: build `server = _LifecycleOwnedServer(config)` instead of `uvicorn.Server(config)`, and wrap the region from just after `task = asyncio.create_task(run_uvicorn())` through the final `await task` so cancellation anywhere in it drains uvicorn:

```python
        task = asyncio.create_task(run_uvicorn())
        try:
            # … existing startup-poll loop, port rebind, URL printing …
            await task
        except asyncio.CancelledError:
            # First-signal teardown: ask uvicorn to drain gracefully and
            # wait for it. This await is part of the command body's unwind,
            # so the lifecycle teardown deadline still bounds it and a
            # second signal (or expiry) forces past it — do NOT shield.
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
```

Preserve the existing startup-poll loop, the `task.result()` dead-task check, the port rebind, and the CONSOLE/logger output exactly as they are — only the `try/except` wrapper and the server class change.

- [ ] **Step 4: Run the new tests + the monitor unit tree**

Run: `uv run pytest tests/unit/monitor/ -v` (foreground, timeout 600000)
Expected: PASS — new tests green, no regressions (`test_collector_db.py` and `test_server_tls.py` construct `MonitorServer` too).

- [ ] **Step 5: Gates + commit**

`uv run nox -s lint`, `make typecheck-python`, `make coverage` (foreground, timeout 600000).

```bash
git add src/otto/monitor/server.py tests/unit/monitor/test_server_signals.py
git commit -m "fix(monitor): otto's lifecycle owns SIGINT/SIGTERM during serve

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: Loopback-or-bed target fixture + otto subprocess driver + certification

The tier's foundation: a hermetic SSH target and a subprocess driver. A session-scoped non-root `sshd` on `127.0.0.1:<ephemeral>` (own host key, pubkey auth as the current user), an otto SUT dir generated in tmp (`settings.toml` + `lab.json` — `UnixHostSpec.ssh_options` carries `port` and `client_keys` per host, `src/otto/models/host.py:434`; `known_hosts=None` is otto's default), a `spawn_otto`/`OttoProc` driver with marker-wait helpers, and a certification test proving the whole chain (spawn → discover lab → SSH pubkey auth → session handshake → run → exit 0).

**Files:**
- Create: `tests/integration/chaos/__init__.py` (empty)
- Create: `tests/integration/chaos/_sshd.py`
- Create: `tests/integration/chaos/_target.py`
- Create: `tests/integration/chaos/_driver.py`
- Create: `tests/integration/chaos/conftest.py`
- Test: `tests/integration/chaos/test_harness.py` (new)

**Interfaces:**
- Consumes: `/usr/sbin/sshd` (present on the dev VM and ubuntu-latest; `shutil.which("sshd") or "/usr/sbin/sshd"`), `ssh-keygen`, asyncssh (already a project dep), the env-building convention from `tests/e2e/_otto_subprocess.py` (`OTTO_BIN`, `COVERAGE_PROCESS_START`, `PYTHONPATH` bootstrap prefix).
- Produces (Tasks 4-6 build on these — exact signatures):
  - `ChaosTarget` frozen dataclass: `sut_dir: Path`, `lab: str`, `host_id: str`, `ssh_host: str`, `ssh_port: int`, `ssh_username: str`, `ssh_client_key: Path | None`, `ssh_password: str | None`.
  - `probe(target: ChaosTarget, command: str) -> tuple[int, str]` — fresh asyncssh connection, returns `(exit_status, stdout)`.
  - `spawn_otto(argv: list[str], *, xdir: Path, target: ChaosTarget, extra_env: dict[str, str] | None = None) -> OttoProc`.
  - `OttoProc`: `.pid`, `.signal(sig: int) -> None`, `.wait(timeout: float) -> int`, `.wait_for_log(pattern: str, timeout: float) -> str`, `.wait_for_stderr(pattern: str, timeout: float) -> str`, `.stderr_text() -> str`, `.stdout_text() -> str`, `.assert_no_process_group() -> None`.
  - `BANNER = "cleaning up remote sessions"` (substring of `_INTERRUPT_STATUS_LINE`, exported from `_driver`).
  - conftest fixture `chaos_target` (session scope) yielding a `ChaosTarget`.

- [ ] **Step 1: Write `_sshd.py`**

```python
"""Throwaway non-root sshd on 127.0.0.1 for the tier-2 chaos suite.

Everything (host key, client key, authorized_keys, config, logs) lives in a
tmp directory owned by the test session. The daemon runs foreground
(``sshd -D -e``) as the current user, pubkey-auth only — hermetic on the
dev VM and on ubuntu-latest runners alike, no sudo, no system state.
"""

import shutil
import socket
import subprocess
import time
from pathlib import Path

_SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
_READY_TIMEOUT = 15.0


def free_port() -> int:
    """Reserve an ephemeral loopback port (bind/close; sequential suite makes the race window moot)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def generate_keypairs(keys_dir: Path) -> "tuple[Path, Path]":
    """ssh-keygen an ed25519 host key and client key; build authorized_keys.

    Returns (host_key, client_key) private-key paths.
    """
    keys_dir.mkdir(parents=True, exist_ok=True)
    host_key = keys_dir / "host_key"
    client_key = keys_dir / "client_key"
    for key in (host_key, client_key):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            capture_output=True,
        )
    authorized = keys_dir / "authorized_keys"
    authorized.write_bytes((client_key.with_suffix(".pub")).read_bytes())
    authorized.chmod(0o600)
    return host_key, client_key


def write_sshd_config(cfg_path: Path, *, port: int, host_key: Path, authorized_keys: Path, user: str) -> Path:
    """Write a minimal non-root sshd config (pubkey only, loopback only)."""
    cfg_path.write_text(
        f"""\
ListenAddress 127.0.0.1:{port}
HostKey {host_key}
PidFile none
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile {authorized_keys}
AllowUsers {user}
Subsystem sftp internal-sftp
LogLevel VERBOSE
"""
    )
    return cfg_path


class LoopbackSshd:
    """Foreground sshd child; ``start()`` blocks until the port accepts."""

    def __init__(self, config: Path, log_path: Path) -> None:
        self._config = config
        self._log_path = log_path
        self._proc: "subprocess.Popen[bytes] | None" = None

    def start(self, port: int) -> None:
        log = self._log_path.open("wb")
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned config
                [_SSHD, "-D", "-e", "-f", str(self._config)],
                stdout=log,
                stderr=log,
            )
        finally:
            log.close()  # sshd holds its own fd now
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"loopback sshd died at startup (rc={self._proc.returncode}); "
                    f"log:\n{self._log_path.read_text(errors='replace')}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"loopback sshd not accepting on 127.0.0.1:{port} after {_READY_TIMEOUT}s")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)
        self._proc = None
```

- [ ] **Step 2: Write `_target.py`**

```python
"""Chaos target host: loopback sshd by default, a lab bed host on request.

Tier-2 chaos tests point a real ``otto`` subprocess at one SSH-reachable
host (chaos spec, Tier 2). Default: the hermetic loopback sshd from
``_sshd``. Set ``OTTO_CHAOS_BED_HOST=carrot|tomato|pepper`` on the lab to
aim at a bed host instead — the otto subprocess still runs locally and
signals are only ever delivered to that local process.
"""

import asyncio
import dataclasses
import getpass
import json
import os
from pathlib import Path

_REPO_E2E = Path(__file__).resolve().parents[2] / "repo_e2e"


@dataclasses.dataclass(frozen=True)
class ChaosTarget:
    sut_dir: Path
    lab: str
    host_id: str
    ssh_host: str
    ssh_port: int
    ssh_username: str
    ssh_client_key: "Path | None"
    ssh_password: "str | None"


def make_loopback_target(root: Path, *, port: int, client_key: Path) -> ChaosTarget:
    """Generate the SUT dir + lab data for the loopback host and return the target."""
    user = getpass.getuser()
    tech_dir = root / "labdata" / "chaostech"
    tech_dir.mkdir(parents=True)
    (tech_dir / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "ip": "127.0.0.1",
                        "element": "loopback",
                        "os_type": "unix",
                        "valid_terms": ["ssh"],
                        "valid_transfers": ["sftp", "scp"],
                        "is_virtual": True,
                        "creds": [{"login": user, "password": "unused-pubkey-auth"}],
                        "resources": ["loopback"],
                        "labs": ["chaos"],
                        "ssh_options": {
                            "port": port,
                            "client_keys": [str(client_key)],
                        },
                    }
                ]
            },
            indent=2,
        )
    )
    sut = root / "sut"
    (sut / ".otto").mkdir(parents=True)
    (sut / ".otto" / "settings.toml").write_text(
        f"""\
name = "chaos_harness"
version = "0.1.0"
lab_data_type = "json"
labs = [
    "{tech_dir}",
]

[lab]
backend = "json"
"""
    )
    return ChaosTarget(
        sut_dir=sut,
        lab="chaos",
        host_id="loopback",
        ssh_host="127.0.0.1",
        ssh_port=port,
        ssh_username=user,
        ssh_client_key=client_key,
        ssh_password=None,
    )


def make_bed_target(element: str) -> ChaosTarget:
    """Aim at a veggies bed host via the existing repo_e2e SUT (lab leg only)."""
    lab_json = json.loads(
        (Path(__file__).resolve().parents[2] / "_fixtures" / "lab_data" / "tech1" / "lab.json").read_text()
    )
    host = next(h for h in lab_json["hosts"] if h["element"] == element)
    cred = host["creds"][0]
    return ChaosTarget(
        sut_dir=_REPO_E2E,
        lab="veggies",
        host_id=f"{element}_{host['board']}",
        ssh_host=host["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


async def _probe(target: ChaosTarget, command: str) -> "tuple[int, str]":
    import asyncssh

    kwargs: dict = {
        "username": target.ssh_username,
        "known_hosts": None,
        "port": target.ssh_port,
    }
    if target.ssh_client_key is not None:
        kwargs["client_keys"] = [str(target.ssh_client_key)]
    else:
        kwargs["password"] = target.ssh_password
    async with asyncssh.connect(target.ssh_host, **kwargs) as conn:
        result = await conn.run(command, check=False)
        status = result.exit_status if result.exit_status is not None else -1
        return status, str(result.stdout or "")


def probe(target: ChaosTarget, command: str) -> "tuple[int, str]":
    """Run ``command`` over a fresh, independent SSH connection (remote-hygiene oracle)."""
    return asyncio.run(_probe(target, command))


def bed_host_override() -> "str | None":
    return os.environ.get("OTTO_CHAOS_BED_HOST") or None
```

- [ ] **Step 3: Write `_driver.py`**

```python
"""Spawn a real ``otto`` subprocess and observe it: logs, stderr, signals, exit.

Phase markers, not timing (chaos spec, Tier 2): callers wait for a line in
the run's ``verbose.log`` (the file sink flushes per record, but a
QueueListener thread sits between ``logger.*()`` and the file — hence
polling) or for the interrupt banner on stderr (an unbuffered direct
write from ``_on_signal``), then deliver the signal.

Third-signal behavior is deliberately not exercised anywhere in this
suite: after ``_main`` removes its handlers, a third SIGINT during
``asyncio.run`` finalization surfaces as a bare KeyboardInterrupt (SIGTERM:
SIG_DFL kill) — a timing window no marker can gate on.
"""

import dataclasses
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ._target import ChaosTarget

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OTTO_BIN = Path(sys.executable).parent / "otto"
_COVERAGERC = PROJECT_ROOT / ".coveragerc"
_COV_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

BANNER = "cleaning up remote sessions"
_POLL = 0.05


def _otto_env(xdir: Path, target: ChaosTarget, extra_env: "dict[str, str] | None") -> "dict[str, str]":
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "OTTO_XDIR": str(xdir),
        "OTTO_SUT_DIRS": str(target.sut_dir),
        "COVERAGE_PROCESS_START": str(_COVERAGERC),
        "PYTHONPATH": f"{_COV_BOOTSTRAP}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    return env


@dataclasses.dataclass
class OttoProc:
    proc: "subprocess.Popen[bytes]"
    xdir: Path
    stdout_path: Path
    stderr_path: Path

    @property
    def pid(self) -> int:
        return self.proc.pid

    def signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def wait(self, timeout: float) -> int:
        return self.proc.wait(timeout=timeout)

    def stdout_text(self) -> str:
        return self.stdout_path.read_text(errors="replace")

    def stderr_text(self) -> str:
        return self.stderr_path.read_text(errors="replace")

    def _wait_for(self, read: "Callable[[], str]", pattern: str, timeout: float, what: str) -> str:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = read()
            m = rx.search(text)
            if m:
                return m.group(0)
            if self.proc.poll() is not None:
                # One last read: the process may have flushed on exit.
                text = read()
                m = rx.search(text)
                if m:
                    return m.group(0)
                raise AssertionError(
                    f"otto exited (rc={self.proc.returncode}) before {what} matched {pattern!r}.\n"
                    f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
                )
            time.sleep(_POLL)
        raise AssertionError(
            f"{what} never matched {pattern!r} within {timeout}s.\n"
            f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
        )

    def wait_for_stderr(self, pattern: str, timeout: float) -> str:
        return self._wait_for(self.stderr_text, pattern, timeout, "stderr")

    def _log_text(self) -> str:
        return "\n".join(
            p.read_text(errors="replace") for p in sorted(self.xdir.rglob("verbose.log"))
        )

    def wait_for_log(self, pattern: str, timeout: float) -> str:
        return self._wait_for(self._log_text, pattern, timeout, "verbose.log")

    def assert_no_process_group(self) -> None:
        """After exit, nothing may remain in otto's process group (start_new_session ⇒ pgid == pid)."""
        assert self.proc.poll() is not None, "call wait() before assert_no_process_group()"
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return
        raise AssertionError(f"orphaned local children remain in otto's process group (pgid {self.pid})")


def spawn_otto(
    argv: "list[str]",
    *,
    xdir: Path,
    target: ChaosTarget,
    extra_env: "dict[str, str] | None" = None,
) -> OttoProc:
    """Start ``otto -l <lab> --log-level DEBUG *argv`` with stdout/stderr to files."""
    stdout_path = xdir / "otto-stdout.txt"
    stderr_path = xdir / "otto-stderr.txt"
    cmd = [str(OTTO_BIN), "-l", target.lab, "--log-level", "DEBUG", *argv]
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(  # noqa: S603 — fixed test-owned argv
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            cwd=PROJECT_ROOT,
            env=_otto_env(xdir, target, extra_env),
            start_new_session=True,
        )
    return OttoProc(proc=proc, xdir=xdir, stdout_path=stdout_path, stderr_path=stderr_path)
```

(Note: the `Popen` inherits the file descriptors before the `with` closes the parent's handles — the child keeps its own copies, so the parent holds no open fds for the child's streams and the per-test FD watermark stays flat.)

- [ ] **Step 4: Write `conftest.py`**

```python
"""Session fixtures for the tier-2 chaos suite (chaos spec, Tier 2).

Default target: a throwaway loopback sshd owned by this session — hermetic
on the dev VM and ubuntu-latest alike. ``OTTO_CHAOS_BED_HOST`` (lab leg
only) redirects the otto subprocess at a veggies bed host; signals still
only ever go to the local otto process. Host-down in bed mode fails LOUD
with the host's name — never a skip (dev-VM rule).
"""

import gc
import getpass
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from ._sshd import LoopbackSshd, free_port, generate_keypairs, write_sshd_config
from ._target import ChaosTarget, bed_host_override, make_bed_target, make_loopback_target

_FD_TOLERANCE = 4


@pytest.fixture(autouse=True)
def _fd_watermark() -> "Iterator[None]":
    """Local FD bracket per test (chaos spec, Tier 2 assertions).

    Same shape as tests/e2e/tunnel_stability/conftest.py: the driver and
    probe helpers must not leak descriptors across a test.
    """
    before = len(list(Path("/proc/self/fd").iterdir()))
    yield
    gc.collect()
    after = len(list(Path("/proc/self/fd").iterdir()))
    if after > before + _FD_TOLERANCE:
        gc.collect()
        after = len(list(Path("/proc/self/fd").iterdir()))
    assert after <= before + _FD_TOLERANCE, f"fd leak: {before} -> {after}"


@pytest.fixture(scope="session")
def chaos_target(tmp_path_factory: pytest.TempPathFactory) -> "Iterator[ChaosTarget]":
    bed = bed_host_override()
    if bed is not None:
        target = make_bed_target(bed)
        try:
            with socket.create_connection((target.ssh_host, target.ssh_port), timeout=5):
                pass
        except OSError as e:
            raise RuntimeError(
                f"OTTO_CHAOS_BED_HOST={bed}: {target.ssh_host}:{target.ssh_port} unreachable — bed down?"
            ) from e
        yield target
        return

    root = tmp_path_factory.mktemp("chaos")
    host_key, client_key = generate_keypairs(root / "keys")
    port = free_port()
    cfg = write_sshd_config(
        root / "sshd_config",
        port=port,
        host_key=host_key,
        authorized_keys=root / "keys" / "authorized_keys",
        user=getpass.getuser(),
    )
    sshd = LoopbackSshd(cfg, root / "sshd.log")
    sshd.start(port)
    try:
        yield make_loopback_target(root, port=port, client_key=client_key)
    finally:
        sshd.stop()
```

- [ ] **Step 5: Write the certification tests**

Create `tests/integration/chaos/test_harness.py`:

```python
"""Certification of the tier-2 harness: the full chain works untouched.

If these fail, every scenario test in this package is meaningless — fix
here first. Sequential (xdist_group) like the whole chaos suite.
"""

import pytest

from ._driver import spawn_otto
from ._target import probe

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]


def test_probe_round_trips(chaos_target) -> None:
    status, out = probe(chaos_target, "echo chaos-probe-ok")
    assert status == 0
    assert "chaos-probe-ok" in out


def test_untouched_run_completes_cleanly(chaos_target, tmp_path) -> None:
    p = spawn_otto(["host", chaos_target.host_id, "run", "true"], xdir=tmp_path, target=chaos_target)
    rc = p.wait(timeout=90)
    assert rc == 0, f"stderr:\n{p.stderr_text()}\nstdout:\n{p.stdout_text()}"
    # The file sinks landed and recorded the command-start marker.
    p.wait_for_log(r"\| true", timeout=10)
    p.assert_no_process_group()
```

- [ ] **Step 6: Run to verify green**

Run: `uv run pytest tests/integration/chaos/ -v` (foreground, timeout 600000)
Expected: PASS (2 tests). Debug loop hints: sshd startup failures print `sshd.log`; otto failures print its stderr. If otto rejects the generated `settings.toml`, mirror `tests/repo_e2e/.otto/settings.toml`'s field set (minus `libs`/`tests`/`init`) — that file is known-good.

- [ ] **Step 7: Gates + commit**

`uv run nox -s lint`, `make typecheck-python`, `make coverage` (foreground, timeout 600000).

```bash
git add tests/integration/chaos/
git commit -m "test(chaos): tier-2 harness — loopback sshd target + otto subprocess driver

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: Interrupt scenarios for a mid-flight `run`

The core tier-2 storylines against `otto host loopback run 'sleep …'`: graceful SIGINT and SIGTERM (banner, exit 130/143, remote command reaped, no local orphans), deterministic force via `OTTO_TEARDOWN_DEADLINE=0` (130 and 143 variants; no remote assertion — a forced teardown abandons the sweep by design), and a double-signal smoke (prompt exit; either race winner passes — see header item 4).

**Files:**
- Test: `tests/integration/chaos/test_signal_run.py` (new)

**Interfaces:**
- Consumes: `chaos_target`, `spawn_otto`, `OttoProc`, `BANNER`, `probe` (Task 3 signatures).
- Produces: nothing downstream.

- [ ] **Step 1: Write the tests**

```python
"""Real SIGINT/SIGTERM against a mid-flight ``otto … run`` (chaos spec, Tier 2).

Phase gating: the ``@<host>   | <cmd>`` INFO line in verbose.log marks
"command running" (the one reliable INFO-level marker; connection lines
are DEBUG — the driver passes --log-level DEBUG anyway); the stderr
banner marks "teardown running". Remote hygiene is asserted only on
graceful paths — a forced teardown abandons the sweep by design, and its
leftovers are tier-3 recovery material.

The pgrep pattern brackets its first character (``[s]leep``) so the probe
shell's own command line never matches itself.
"""

import os
import re
import signal
import time

import pytest

from ._driver import BANNER, spawn_otto
from ._target import probe

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]

_MARKER_TIMEOUT = 60.0
_EXIT_TIMEOUT = 30.0


def _sleep_cmd(tag: str) -> str:
    """A unique, greppable long command: per-test tag + parent pid uniquify."""
    return f"sleep 3{tag}.{os.getpid() % 100000}"


def _remote_has(target, cmd: str) -> bool:
    status, _ = probe(target, f"pgrep -f '[{cmd[0]}]{cmd[1:]}'")
    return status == 0


def _wait_remote_reaped(target, cmd: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _remote_has(target, cmd):
            return
        time.sleep(0.2)
    raise AssertionError(f"remote command survived teardown: {cmd!r}")


def _wait_remote_running(target, cmd: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _remote_has(target, cmd):
            return
        time.sleep(0.2)
    raise AssertionError(f"remote command never appeared: {cmd!r}")


def _interrupt_mid_run(chaos_target, tmp_path, *, tag: str, sig: int, expected_rc: int) -> None:
    cmd = _sleep_cmd(tag)
    p = spawn_otto(["host", chaos_target.host_id, "run", cmd], xdir=tmp_path, target=chaos_target)
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)  # phase: command running
    _wait_remote_running(chaos_target, cmd)
    p.signal(sig)
    p.wait_for_stderr(BANNER, timeout=15)  # phase: teardown running
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc == expected_rc, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()
    _wait_remote_reaped(chaos_target, cmd)


def test_sigint_mid_run_cleans_up_and_exits_130(chaos_target, tmp_path) -> None:
    _interrupt_mid_run(chaos_target, tmp_path, tag="01", sig=signal.SIGINT, expected_rc=130)


def test_sigterm_mid_run_cleans_up_and_exits_143(chaos_target, tmp_path) -> None:
    _interrupt_mid_run(chaos_target, tmp_path, tag="02", sig=signal.SIGTERM, expected_rc=143)


def _forced_mid_run(chaos_target, tmp_path, *, tag: str, sig: int, expected_rc: int) -> None:
    """Deadline 0 ⇒ teardown always loses the race ⇒ deterministic force path.

    No remote assertion: the force path abandons the sweep by design.
    """
    cmd = _sleep_cmd(tag)
    p = spawn_otto(
        ["host", chaos_target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "0"},
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)
    p.signal(sig)
    p.wait_for_stderr(BANNER, timeout=15)
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc == expected_rc, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()


def test_forced_sigint_exits_130(chaos_target, tmp_path) -> None:
    _forced_mid_run(chaos_target, tmp_path, tag="03", sig=signal.SIGINT, expected_rc=130)


def test_forced_sigterm_exits_143(chaos_target, tmp_path) -> None:
    _forced_mid_run(chaos_target, tmp_path, tag="04", sig=signal.SIGTERM, expected_rc=143)


def test_second_signal_still_exits_promptly(chaos_target, tmp_path) -> None:
    """Double-signal smoke: banner-gated second SIGINT; prompt exit, no wedge.

    'Forced' means teardown lost the race, not signal count (plan 1). Three
    outcomes are physically possible for the second signal, and the first
    two exit 130: it lands during teardown (forces it), or after teardown
    won but before handler removal (idempotent ``_force.set()``). The third
    is unavoidable: on a loopback target teardown takes milliseconds, so
    under load the second signal can land AFTER ``_main`` removed its
    handlers — the process dies to the OS default disposition, exit ``-2``
    (the documented third-signal window, reached at signal #2). The product
    cannot atomically exit-and-keep-handlers; the contract this test pins
    is "prompt exit, never a wedge, 130 or signal-death — never 143, 1, or
    a hang". Discovered as a 2/2 load repro during Task 5's gate runs.
    """
    cmd = _sleep_cmd("05")
    p = spawn_otto(
        ["host", chaos_target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "600"},
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=_MARKER_TIMEOUT)
    p.signal(signal.SIGINT)
    p.wait_for_stderr(BANNER, timeout=15)
    p.signal(signal.SIGINT)
    rc = p.wait(timeout=_EXIT_TIMEOUT)
    assert rc in (130, -int(signal.SIGINT)), f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()
```

- [ ] **Step 2: Run to verify**

Run: `uv run pytest tests/integration/chaos/test_signal_run.py -v` (foreground, timeout 600000)
Expected: PASS. These are the first tests that can FAIL against real product gaps rather than harness gaps — if the graceful paths leave the remote `sleep` running or exit with the wrong code, that is a REAL tier-2 finding: report it (DONE_WITH_CONCERNS or BLOCKED with evidence), do not weaken the assertion to get green.

- [ ] **Step 3: Gates + commit**

`uv run nox -s lint`, `make typecheck-python`, `make coverage` (foreground, timeout 600000).

```bash
git add tests/integration/chaos/test_signal_run.py
git commit -m "test(chaos): tier-2 interrupt scenarios for a mid-flight run

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: PTY `login` scenarios — termios restored, graceful and forced

SIGTERM during an interactive `login` (in raw mode ^C forwards to the remote as bytes, so SIGTERM is the signal that matters — spec §"Terminal restore on the force path"). Graceful: the login unwind's `finally` restores termios. Forced (`OTTO_TEARDOWN_DEADLINE=0`): the graceful unwind is abandoned, and restoration comes from Plan 1's belt-and-suspenders pair — the bridge task's inner `finally` (re-cancelled during `asyncio.run` finalization) and the **force-exit hook** (`register_force_exit_hook` → `_restore_terminal`, after loop close). **Execution discovery (task review, proven by mutation):** the finalization path alone restores termios even with the hook neutered, so this test pins the end-to-end observable — forced SIGTERM never strands a raw terminal — WITHOUT discriminating which of the two paths did it; hook-mechanism isolation lives in Plan 1's unit tests. Test docstrings must claim exactly that and no more. Verification reads termios off the PTY **master** (on Linux it reflects the slave's settings) after child exit, asserting canonical mode is back (`ICANON|ECHO` — raw mode clears both).

**Files:**
- Modify: `tests/e2e/host/_pty_driver.py` (`InteractiveOttoSession.__init__` gains `extra_env: dict[str, str] | None = None`, merged into the env it builds; default None, existing callers unaffected)
- Test: `tests/integration/chaos/test_signal_login.py` (new)

**Interfaces:**
- Consumes: `InteractiveOttoSession(argv, *, xdir, cols, rows, sut_dirs, extra_env)` (after this task's edit), its `sendline`/`expect`/`wait`/`pid` and private `_master_fd`; `chaos_target`.
- Produces: nothing downstream.

- [ ] **Step 1: Extend the PTY driver**

In `tests/e2e/host/_pty_driver.py`: add `extra_env: "dict[str, str] | None" = None` to `InteractiveOttoSession.__init__`, store it, and in `__enter__` after the env dict is built (via `otto_subprocess_env`), apply `if self._extra_env: env.update(self._extra_env)` before the `Popen`. Read the file first and match its style exactly.

- [ ] **Step 2: Write the tests**

Create `tests/integration/chaos/test_signal_login.py`:

```python
"""SIGTERM during an interactive ``login``: terminal restored, exit 143.

In raw mode the local terminal does not generate SIGINT (^C forwards to
the remote as bytes) — SIGTERM is the interrupt that matters (chaos spec,
"Terminal restore on the force path"). The forced variant (teardown
deadline 0) is the end-to-end proof of the force-exit hook: nothing else
can restore termios once the graceful unwind is abandoned.

Liveness marker: ``echo`` with a split literal, so the matched bytes can
only be command OUTPUT — the raw-mode echo of the typed line contains the
split form, not the joined one.
"""

import os
import re
import signal
import termios

import pytest

from tests.e2e.host._pty_driver import InteractiveOttoSession

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]

_READY = re.compile(rb"CHAOS-READY")


def _login_argv(target) -> "list[str]":
    return ["-l", target.lab, "host", target.host_id, "login"]


def _assert_canonical(master_fd: int) -> None:
    lflag = termios.tcgetattr(master_fd)[3]
    assert lflag & termios.ICANON, "termios not restored: ICANON still cleared (raw mode leaked)"
    assert lflag & termios.ECHO, "termios not restored: ECHO still cleared (raw mode leaked)"


def _sigterm_login(chaos_target, tmp_path, *, extra_env: "dict[str, str] | None") -> None:
    with InteractiveOttoSession(
        _login_argv(chaos_target),
        xdir=tmp_path,
        sut_dirs=chaos_target.sut_dir,
        extra_env=extra_env,
    ) as sess:
        sess.sendline("echo CHAOS-$(echo READY)")
        sess.expect(_READY, timeout=60)  # session live: output round-tripped
        os.kill(sess.pid, signal.SIGTERM)
        rc = sess.wait(timeout=30)
        assert rc == 143
        _assert_canonical(sess._master_fd)  # master reflects the slave's termios


def test_sigterm_during_login_restores_terminal_and_exits_143(chaos_target, tmp_path) -> None:
    _sigterm_login(chaos_target, tmp_path, extra_env=None)


def test_forced_sigterm_during_login_still_restores_terminal(chaos_target, tmp_path) -> None:
    """Deadline 0 abandons the graceful unwind — only the force-exit hook
    (plan 1's ``register_force_exit_hook`` → ``_restore_terminal``) can
    restore termios here."""
    _sigterm_login(chaos_target, tmp_path, extra_env={"OTTO_TEARDOWN_DEADLINE": "0"})
```

(If `_master_fd` is named differently in the driver, use the actual attribute; add a tiny read-only `master_fd` property to the driver instead if lint rejects the private access — additive, callers unaffected.)

- [ ] **Step 3: Run to verify**

Run: `uv run pytest tests/integration/chaos/test_signal_login.py tests/e2e/host -k "interact or login" -v` (foreground, timeout 600000; the second path re-certifies the driver edit against its existing e2e consumers — those need the veggies bed, so on a hostless machine scope to the chaos file only and say so in the report)
Expected: PASS.

- [ ] **Step 4: Gates + commit**

`uv run nox -s lint`, `make typecheck-python`, `make coverage` (foreground, timeout 600000).

```bash
git add tests/e2e/host/_pty_driver.py tests/integration/chaos/test_signal_login.py
git commit -m "test(chaos): SIGTERM-during-login scenarios prove terminal restore end to end

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: Monitor serve scenarios + GitHub nightly job

With Task 2 landed, a signal to `otto monitor --live` must take otto's uniform policy: banner, teardown, exit 143. Marker: the INFO line `Monitor dashboard started on …` (`src/otto/monitor/server.py:818` — reaches `verbose.log`). Two scenarios: graceful (drain runs) and forced (`OTTO_TEARDOWN_DEADLINE=0` — Task 2's shielded serving await hands off to an UNSHIELDED drain await precisely so the force path can abandon it; this test is the end-to-end proof, since Task 2's unit tests never issue a second cancellation). Then wire the whole suite into GitHub nightly (`ubuntu-latest` has sshd; the fixture needs no sudo unless the binary is absent).

**Files:**
- Test: `tests/integration/chaos/test_signal_monitor.py` (new)
- Modify: `.github/workflows/nightly.yml` (new `chaos-tier2` job; add it to `report-failure`'s `needs` list)

**Interfaces:**
- Consumes: Task 2's deterministic monitor shutdown; Task 3's driver/fixture; the monitor CLI shape from `tests/e2e/monitor/test_monitor_e2e.py` (`["monitor", "--live", "--hosts", <id>, "--interval", "1", "--db", <path>]`; no `--port` option exists — `MonitorServer` defaults to an ephemeral port).
- Produces: nightly coverage of the loopback slice (spec §"CI wiring").

- [ ] **Step 1: Write the test**

Create `tests/integration/chaos/test_signal_monitor.py`:

```python
"""SIGTERM during ``otto monitor --live`` serve: otto's policy, not uvicorn's.

Before plan 3's ownership fix, uvicorn's ``capture_signals`` displaced the
lifecycle handlers for the whole serve window — no banner, uvicorn's own
two-stage policy, and a racy exit code from the post-drain signal
re-raise. This pins the uniform contract: banner + 143, same as every
other command.
"""

import signal

import pytest

from ._driver import BANNER, spawn_otto

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]


def _monitor_argv(chaos_target, db_path) -> "list[str]":
    return [
        "monitor",
        "--live",
        "--hosts",
        chaos_target.host_id,
        "--interval",
        "1",
        "--db",
        str(db_path),
    ]


def _sigterm_monitor(chaos_target, tmp_path, *, db_name: str, extra_env: "dict[str, str] | None") -> None:
    p = spawn_otto(
        _monitor_argv(chaos_target, tmp_path / db_name),
        xdir=tmp_path,
        target=chaos_target,
        extra_env=extra_env,
    )
    p.wait_for_log("Monitor dashboard started on", timeout=60)  # phase: serving
    p.signal(signal.SIGTERM)
    p.wait_for_stderr(BANNER, timeout=15)
    rc = p.wait(timeout=30)
    assert rc == 143, f"stderr:\n{p.stderr_text()}"
    p.assert_no_process_group()


def test_sigterm_during_monitor_serve_exits_143(chaos_target, tmp_path) -> None:
    _sigterm_monitor(chaos_target, tmp_path, db_name="chaos-monitor.db", extra_env=None)


def test_forced_sigterm_during_monitor_serve_still_exits_143(chaos_target, tmp_path) -> None:
    """Deadline 0 forces past the uvicorn drain.

    Task 2 shields the serving await but leaves the drain await bare
    precisely so the force path can abandon it — this is the end-to-end
    proof (Task 2's unit tests never issue a second cancellation). A hang
    here means the drain became unabandonable.
    """
    _sigterm_monitor(
        chaos_target,
        tmp_path,
        db_name="chaos-monitor-forced.db",
        extra_env={"OTTO_TEARDOWN_DEADLINE": "0"},
    )
```

- [ ] **Step 2: Run to verify**

Run: `uv run pytest tests/integration/chaos/test_signal_monitor.py -v` (foreground, timeout 600000)
Expected: PASS. If the exit code is 0 or the banner never appears, Task 2's ownership fix regressed — do not adjust the assertions.

- [ ] **Step 3: Add the nightly job**

In `.github/workflows/nightly.yml`, add a job alongside the existing ones — **copy the exact checkout/setup-uv step idioms and action versions from the `unit-matrix` job** (do not invent versions), single Python (no matrix):

```yaml
  chaos-tier2:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      # checkout + setup-uv + `uv sync --all-extras --dev`, copied verbatim
      # from unit-matrix (same action versions), single default Python
      - name: Ensure sshd is available
        run: command -v sshd || { sudo apt-get update && sudo apt-get install -y openssh-server; }
      - name: Tier-2 real-signal chaos suite (loopback sshd)
        run: uv run pytest tests/integration/chaos -p no:cacheprovider --no-cov
```

Add `chaos-tier2` to the `report-failure` job's `needs` list. Update nightly.yml's header docstring/comment ("Tier 1 only: no Vagrant VMs in GHA") to say tier 2 now runs here via the loopback sshd — keep its VM claim accurate: still no VMs.

- [ ] **Step 4: Validate the workflow file**

Run: `uv run python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/nightly.yml').read_text()); print('yaml ok')"` (foreground)
Expected: `yaml ok`. (CI-side proof runs on the next nightly; flag that in the task report.)

- [ ] **Step 5: Gates + commit**

`uv run nox -s lint`, `make typecheck-python`, `make coverage` (foreground, timeout 600000).

```bash
git add tests/integration/chaos/test_signal_monitor.py .github/workflows/nightly.yml
git commit -m "test(chaos): monitor-serve signal scenario + nightly tier-2 job

Assisted-by: Claude (Fable 5)"
```
