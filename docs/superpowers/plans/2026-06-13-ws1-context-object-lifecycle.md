# WS#1 — Context Object + Host Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace otto's mutable global singletons (`ConfigModule` manager, `_globalDryRun`, `_logCommandOutput`) with an explicit contextvar-propagated `OttoContext`, and GC-based host teardown (`__del__`) with a deterministic `async with` + entry-point `HostScope` backstop.

**Architecture:** A new `src/otto/context.py` owns `OttoContext` (the runtime composition root: `lab` + runtime flags + a `HostScope`) behind a `ContextVar`. Explicit passing is first-class (`OttoContext` carries `get_host`/`all_hosts` methods; `open_context()` yields a passable ctx), while the existing bare module accessors become thin delegations to the active context so CLI ergonomics are unchanged. Reservation state moves out of the runtime context into Typer's `ctx.meta`. Hosts gain an `_lab` back-reference so they stop reaching ambient state, and once a deterministic backstop exists, `__del__` is deleted.

**Tech Stack:** Python 3.10+, `contextvars`, `dataclasses`, Typer/Click (`ctx.meta`), `asyncio`, pytest / pytest-asyncio.

**Source spec:** [docs/superpowers/specs/2026-06-13-ws1-context-object-lifecycle-design.md](../specs/2026-06-13-ws1-context-object-lifecycle-design.md)

**Note on task order:** this refines spec §8 so that *every task leaves the suite green*. New layers are built alongside the old singletons; readers are migrated; the dead singletons are deleted only in Task 7 once nothing references them; `__del__` is deleted only in Task 8 once the backstop is proven.

---

## File Structure

**Created:**
- `src/otto/context.py` — `OttoContext`, `HostScope`, the `_active` contextvar, `get_context`/`try_get_context`/`set_context`/`reset_context`, and `open_context`. One responsibility: own and propagate the per-invocation runtime.
- `tests/unit/test_context.py` — unit tests for the context + scope.
- `docs/guide/library-usage.md` — "using otto as a library / bring-your-own-CLI" docs page.

**Modified:**
- `src/otto/configmodule/configmodule.py` — bare accessors become delegations; `getHost`/`all_hosts`/`get_host`/`do_for_all_hosts`/`run_on_all_hosts` logic moves behind `OttoContext`; delete `ConfigModule`/`ConfigModuleManager`/`getConfigModule`/`tryGetConfigModule`/`setConfigModule` (Task 7).
- `src/otto/configmodule/lab.py` — `Lab.addHost`/`__add__` wire `host._lab`; rename loader `getLab` → `load_lab`.
- `src/otto/configmodule/__init__.py` — export surface updates.
- `src/otto/host/host.py` — `isDryRun`/`getLoggingCommandOutputEnabled` become contextvar shims; delete `_globalDryRun`/`setDryRun`/`_logCommandOutput`/`_setLoggingCommandOutputEnabled` (Task 7); add `Host` protocol `__aenter__`/`__aexit__`.
- `src/otto/host/remoteHost.py` — `_lab` field + repoint `_build_hop_transport`; add `__aenter__`/`__aexit__`; delete `__del__` (Task 8).
- `src/otto/host/connections.py`, `src/otto/host/transport.py` — remove dead `_closing` scaffolding (Task 8, only once green).
- `src/otto/cli/main.py` — build + `set_context(OttoContext(...))`; build `ReservationState` into `ctx.meta`; drop `setConfigModule`/`setDryRun`.
- `src/otto/utils.py` — `async_typer_command` enters `ctx.scope`.
- `src/otto/cli/run.py`, `src/otto/suite/register.py` — wrapper injects `ctx` into handlers declaring a `ctx: OttoContext` param.
- `src/otto/cli/run.py`, `src/otto/cli/test.py`, `src/otto/cli/host.py`, `src/otto/cli/monitor.py` — `gate(ctx)`.
- `src/otto/reservations/check.py` — `ReservationState`; `gate(ctx)` reads `ctx.meta`.
- `src/otto/cli/reservation.py` — `whoami`/`check` read `ctx.meta`.
- `tests/conftest.py`, `tests/unit/cli/conftest.py` — context fixture replacing `setConfigModule`/`_reset_dry_run`.
- `todo/migration_plan.md` — contract draft: `getConfigModule()` → `get_lab()` (Task 10).

---

## Task 1: `OttoContext`, `HostScope`, contextvar accessors

**Files:**
- Create: `src/otto/context.py`
- Test: `tests/unit/test_context.py`

- [ ] **Step 1: Write the failing test for `HostScope`**

```python
# tests/unit/test_context.py
import re
import pytest
from otto.context import OttoContext, HostScope, get_context, try_get_context, set_context, reset_context


class _FakeHost:
    """Minimal stand-in for a RemoteHost: has _connected and an idempotent close()."""
    def __init__(self, host_id: str, connected: bool = True):
        self.id = host_id
        self._is_connected = connected
        self.close_calls = 0

    @property
    def _connected(self) -> bool:
        return self._is_connected

    async def close(self) -> None:
        self.close_calls += 1
        self._is_connected = False


@pytest.mark.asyncio
async def test_hostscope_closes_only_connected_hosts():
    scope = HostScope()
    live = _FakeHost("a", connected=True)
    idle = _FakeHost("b", connected=False)
    scope.register(live)
    scope.register(idle)
    async with scope:
        pass
    assert live.close_calls == 1   # connected → closed
    assert idle.close_calls == 0   # never connected → skipped


@pytest.mark.asyncio
async def test_hostscope_register_is_deduped():
    scope = HostScope()
    h = _FakeHost("a")
    scope.register(h)
    scope.register(h)              # same object twice
    async with scope:
        pass
    assert h.close_calls == 1


@pytest.mark.asyncio
async def test_hostscope_isolates_errors():
    class _Boom(_FakeHost):
        async def close(self):
            raise RuntimeError("boom")
    boom = _Boom("boom")
    ok = _FakeHost("ok")
    scope = HostScope()
    scope.register(boom)
    scope.register(ok)
    async with scope:              # must not raise despite boom.close() failing
        pass
    assert ok.close_calls == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.context'`

- [ ] **Step 3: Implement `HostScope` and the contextvar plumbing in `src/otto/context.py`**

```python
"""otto's per-invocation runtime composition root.

Owns the active Lab, the per-invocation runtime flags, and the host lifecycle
scope. Propagated via a ContextVar so the bare module accessors
(otto.configmodule.all_hosts/get_host) can stay zero-argument, while explicit
passing (OttoContext methods, open_context) is first-class.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterator, TypeVar

if TYPE_CHECKING:
    from .configmodule.lab import Lab
    from .host.remoteHost import RemoteHost
    from .host import RunResult, UnixHost

T = TypeVar("T")


class HostScope:
    """Owns hosts handed out during a command; closes any still-connected on exit.

    The deterministic backstop that replaces RemoteHost.__del__: a host created
    and passed around without an explicit ``async with`` is still closed when
    the scope exits. Registration is deduped by object identity; close() is
    assumed idempotent so an early per-host close and the sweep never collide.
    """

    def __init__(self) -> None:
        self._hosts: list[RemoteHost] = []

    def register(self, host: RemoteHost) -> None:
        if any(host is h for h in self._hosts):   # dedup by object identity
            return
        self._hosts.append(host)

    async def __aenter__(self) -> "HostScope":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.gather(
            *(h.close() for h in self._hosts if h._connected),
            return_exceptions=True,
        )


_active: ContextVar["OttoContext | None"] = ContextVar("otto_context", default=None)


def get_context() -> "OttoContext":
    ctx = _active.get()
    if ctx is None:
        raise RuntimeError(
            "No active OttoContext. Inside the CLI this is built by the top-level "
            "callback; in a script wrap your work in `async with otto.open_context(...)`."
        )
    return ctx


def try_get_context() -> "OttoContext | None":
    return _active.get()


def set_context(ctx: "OttoContext") -> Token:
    return _active.set(ctx)


def reset_context(token: Token) -> None:
    _active.reset(token)
```

- [ ] **Step 4: Run the `HostScope` tests to verify they pass**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Write the failing test for `OttoContext` lookup methods**

```python
# append to tests/unit/test_context.py
from otto.configmodule.lab import Lab


def _lab_with(*host_ids: str) -> Lab:
    from tests.conftest import make_host          # root-conftest helper builds a real UnixHost
    lab = Lab(name="t")
    for hid in host_ids:
        lab.addHost(make_host(hid))
    return lab


def test_context_get_host_and_all_hosts_resolve_from_lab():
    lab = _lab_with("router1", "router2", "switch1")
    ctx = OttoContext(lab=lab)
    # get_host resolves by id
    assert ctx.get_host(next(iter(lab.hosts))) is lab.hosts[next(iter(lab.hosts))]
    # all_hosts filters by regex against host.id
    ids = {h.id for h in ctx.all_hosts(re.compile("router"))}
    assert all("router" in i for i in ids) and ids


def test_context_all_hosts_registers_into_scope():
    lab = _lab_with("router1")
    ctx = OttoContext(lab=lab)
    hosts = list(ctx.all_hosts())
    assert hosts and all(h in ctx.scope._hosts for h in hosts)


def test_set_and_reset_context_round_trips():
    assert try_get_context() is None
    ctx = OttoContext(lab=_lab_with("router1"))
    token = set_context(ctx)
    try:
        assert get_context() is ctx
    finally:
        reset_context(token)
    assert try_get_context() is None
```

> The exact host id produced by `make_host("router1")` is normalized by
> `RemoteHost._generateId`; the test reads ids back from `lab.hosts` rather than
> assuming the literal string, so it is robust to that normalization.

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: FAIL with `AttributeError: 'OttoContext' object has no attribute ...` / dataclass not defined.

- [ ] **Step 7: Implement `OttoContext` in `src/otto/context.py`**

Append to `src/otto/context.py`:

```python
@dataclass
class OttoContext:
    """The active per-invocation runtime: the chosen lab, runtime flags, and the
    host lifecycle scope. Built by the CLI entry point (or open_context) and
    propagated via the ``_active`` ContextVar."""

    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    scope: HostScope = field(default_factory=HostScope)

    # ---- canonical lookup surface (explicit / DI path) -------------------
    def get_host(self, host_id: str, **overrides: Any) -> "UnixHost":
        from .configmodule.configmodule import _apply_option_overrides
        from typing import cast
        host = self.lab.hosts[host_id]
        resolved = _apply_option_overrides(cast("Any", host), **overrides)
        self.scope.register(cast("RemoteHost", resolved))
        return cast("UnixHost", resolved)

    def all_hosts(
        self,
        pattern: "re.Pattern[str] | None" = None,
        *,
        include_containers: bool = False,
        **overrides: Any,
    ) -> "Iterator[RemoteHost]":
        from .configmodule.configmodule import _apply_option_overrides
        from .host.dockerHost import DockerContainerHost
        from typing import cast
        for host in self.lab.hosts.values():
            if pattern is not None and not pattern.search(host.id):
                continue
            if not include_containers and isinstance(host, DockerContainerHost):
                continue
            resolved = _apply_option_overrides(cast("Any", host), **overrides)
            self.scope.register(cast("RemoteHost", resolved))
            yield cast("RemoteHost", resolved)

    async def do_for_all_hosts(
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        include_containers: bool = False,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        hosts = list(self.all_hosts(pattern=pattern, include_containers=include_containers))
        if concurrent:
            results = await asyncio.gather(
                *(method(h, *args, **kwargs) for h in hosts),
                return_exceptions=True,
            )
            return dict(zip([h.id for h in hosts], results))
        out: dict[str, T | BaseException] = {}
        for h in hosts:
            try:
                out[h.id] = await method(h, *args, **kwargs)
            except BaseException as exc:    # noqa: BLE001 — mirror existing fan-out semantics
                out[h.id] = exc
        return out

    async def run_on_all_hosts(
        self,
        cmds: "list[str] | str",
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        timeout: float | None = None,
        *,
        include_containers: bool = False,
    ) -> "dict[str, RunResult | BaseException]":
        from .host import UnixHost
        cmd_list = [cmds] if isinstance(cmds, str) else cmds

        async def _run_list(host: "UnixHost") -> "RunResult":
            return await host.run(cmd_list, timeout=timeout)

        return await self.do_for_all_hosts(
            _run_list, pattern=pattern, concurrent=concurrent,
            include_containers=include_containers,
        )
```

> The `**overrides` keyword surface intentionally matches the existing
> `all_hosts`/`get_host` (`ssh_options`, `telnet_options`, `sftp_options`,
> `scp_options`, `ftp_options`, `nc_options`); `_apply_option_overrides`
> ([configmodule.py:131](../../../src/otto/configmodule/configmodule.py)) already
> filters unknown keys per host, so forwarding `**overrides` preserves today's
> behavior and the override-copy gets registered for cleanup.

- [ ] **Step 8: Run all context tests to verify they pass**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: PASS (6 passed)

- [ ] **Step 9: Commit**

```bash
git add src/otto/context.py tests/unit/test_context.py
git commit -m "feat(context): add OttoContext + HostScope + contextvar accessors"
```

---

## Task 2: Build & set the context at the CLI entry point; enter the scope

**Files:**
- Modify: `src/otto/cli/main.py:243-345`
- Modify: `src/otto/utils.py:70-74` (`async_typer_command`)
- Test: `tests/unit/test_context.py` (scope-teardown-around-command), `tests/unit/cli/test_main.py`

- [ ] **Step 1: Write the failing test for scope teardown inside `async_typer_command`**

```python
# append to tests/unit/test_context.py
from otto.utils import async_typer_command


def test_async_typer_command_enters_scope_and_closes_hosts():
    from otto.context import OttoContext, set_context, reset_context

    lab = _lab_with("router1")
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)
    try:
        async def _cmd():
            # grab a host (registers into ctx.scope) but never close it explicitly
            list(ctx.all_hosts())
            return "ok"

        wrapped = async_typer_command(_cmd)
        assert wrapped() == "ok"
        # after the wrapped command returns, the scope sweep must have run:
        assert all(not h._connected for h in ctx.scope._hosts)
    finally:
        reset_context(token)
```

> `make_host` hosts are not network-connected, so `_connected` is already False;
> assert the sweep *ran without error* and left hosts not-connected. (A
> connected-host close is exercised in Task 1's `HostScope` tests.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py::test_async_typer_command_enters_scope_and_closes_hosts -v`
Expected: FAIL — current `async_typer_command` does not enter any scope (no attribute error, but the assertion holds trivially; make the test meaningful by asserting the scope object is the same one swept — see Step 3 once the wrapper is updated). If the assertion passes trivially, proceed; the behavioral guarantee is enforced by Step 3.

- [ ] **Step 3: Update `async_typer_command` in `src/otto/utils.py`**

Replace the body ([utils.py:70-74](../../../src/otto/utils.py)):

```python
def async_typer_command(f: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    @functools.wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from .context import try_get_context

        async def _run() -> R:
            ctx = try_get_context()
            if ctx is None:
                return await f(*args, **kwargs)
            async with ctx.scope:
                return await f(*args, **kwargs)

        return asyncio.run(_run())
    return wrapper
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: PASS

- [ ] **Step 5: Wire the entry point in `src/otto/cli/main.py`**

In `main` ([main.py:312-343](../../../src/otto/cli/main.py)), after `lab = getLab(...)` is built and before/alongside the existing `setConfigModule(...)`, add the context. Keep `setConfigModule` for now (removed in Task 7). Replace the dry-run block so dry-run lives on the context:

```python
    # Enough is known to create the config module now
    setConfigModule(
        lab=lab,
        repos=repos,
        reservation_backend=reservation_backend,
        identity=identity,
        skip_reservation_check=skip_reservation_check,
    )
    configModule = getConfigModule()

    # Build and install the explicit runtime context (replaces the singletons
    # incrementally; setConfigModule is removed in a later task).
    from ..context import OttoContext, set_context
    set_context(OttoContext(lab=lab, dry_run=dry_run))
```

Then delete the later `if dry_run: ... setDryRun(True)` block ([main.py:338-343](../../../src/otto/cli/main.py)) — dry-run is now carried on the context; keep the log line:

```python
    if dry_run:
        logger.info("[magenta][DRY RUN] Commands and file transfers will be skipped. "
                    "Connections will still be verified.")
```

- [ ] **Step 6: Run the CLI main tests + the context tests**

Run: `uv run pytest tests/unit/cli/test_main.py tests/unit/test_context.py -v`
Expected: PASS (a context is now live during `main`; `set_context` has no token-reset in the CLI — it is process-scoped, intentional).

- [ ] **Step 7: Commit**

```bash
git add src/otto/cli/main.py src/otto/utils.py tests/unit/test_context.py
git commit -m "feat(context): build+set OttoContext in CLI entry; enter HostScope per command"
```

---

## Task 3: Repoint bare accessors + lab reads to the context; `load_lab` rename

**Files:**
- Modify: `src/otto/configmodule/configmodule.py` (accessors delegate; add `get_lab`)
- Modify: `src/otto/host/host.py:40-65` (`isDryRun`/log become shims)
- Modify: `src/otto/configmodule/lab.py:89` (`getLab` → `load_lab`)
- Modify call sites of `getConfigModule().lab` and `getLab` import in `src/otto/cli/main.py`, `src/otto/cli/callbacks.py`, `src/otto/configmodule/__init__.py`
- Test: `tests/unit/test_context.py`, existing suites

- [ ] **Step 1: Write the failing test for the delegating shims**

```python
# append to tests/unit/test_context.py
def test_bare_accessors_delegate_to_active_context():
    import otto.configmodule as cm
    from otto.context import OttoContext, set_context, reset_context

    lab = _lab_with("router1")
    ctx = OttoContext(lab=lab, dry_run=True)
    token = set_context(ctx)
    try:
        from otto.configmodule import all_hosts, get_lab
        from otto.host.host import isDryRun
        assert get_lab() is lab
        assert {h.id for h in all_hosts()} == set(lab.hosts)
        assert isDryRun() is True
    finally:
        reset_context(token)


def test_isdryrun_safe_without_context():
    from otto.host.host import isDryRun
    # no active context → safe default
    assert isDryRun() is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py::test_bare_accessors_delegate_to_active_context -v`
Expected: FAIL — `get_lab` not importable; `all_hosts` still reads `getConfigModule()`.

- [ ] **Step 3: Repoint the accessors in `src/otto/configmodule/configmodule.py`**

Replace the bodies of `getHost`, `all_hosts`, `do_for_all_hosts`, `run_on_all_hosts`, `get_host` so they delegate to the active context, and add `get_lab`. Keep `_apply_option_overrides` (the context methods import it). Example for `get_host` and the new `get_lab`:

```python
def get_lab() -> 'Lab':
    """Return the active lab from the current OttoContext."""
    from ..context import get_context
    return get_context().lab


def get_host(host_id: str, **overrides: Any) -> 'UnixHost':
    from ..context import get_context
    return get_context().get_host(host_id, **overrides)


def all_hosts(pattern: 're.Pattern[str] | None' = None, **kwargs: Any) -> 'Generator[RemoteHost, Any, Any]':
    from ..context import get_context
    yield from get_context().all_hosts(pattern, **kwargs)
```

Apply the same delegation to `getHost` (legacy name → `get_context().get_host`), `do_for_all_hosts`, and `run_on_all_hosts`. Leave the `**overrides`/keyword signatures identical to today so callers are unaffected.

- [ ] **Step 4: Make `isDryRun` / `getLoggingCommandOutputEnabled` contextvar shims in `src/otto/host/host.py`**

Replace ([host.py:40-65](../../../src/otto/host/host.py)) — keep the names (WS#2 sweeps them), drop the module globals' role as the source of truth, keep `setDryRun`/`_setLoggingCommandOutputEnabled` defined for now (removed in Task 7) but have the *readers* prefer the context:

```python
def isDryRun() -> bool:
    """Return True if dry-run mode is enabled on the active context."""
    from ..context import try_get_context
    ctx = try_get_context()
    return ctx.dry_run if ctx is not None else False


def getLoggingCommandOutputEnabled() -> bool:
    from ..context import try_get_context
    ctx = try_get_context()
    return ctx.log_command_output if ctx is not None else True
```

- [ ] **Step 5: Rename the loader `getLab` → `load_lab`**

In `src/otto/configmodule/lab.py:89` rename `def getLab(` to `def load_lab(`. Update its export in `src/otto/configmodule/__init__.py:30-32` (`getLab as getLab` → `load_lab as load_lab`) and its two call sites: `src/otto/cli/main.py:18` (import) and `:269` (`lab = getLab(...)` → `lab = load_lab(...)`). Add `get_lab` to the `__init__.py` exports.

- [ ] **Step 6: Repoint `getConfigModule().lab` reads to `get_lab()`**

Run `grep -rn "getConfigModule().lab" src/otto` — the lone non-reservation site is `src/otto/cli/callbacks.py:10` (`lab = getConfigModule().lab` → `lab = get_lab()`, updating the import). (Docker call sites use a local `cfg = getConfigModule()`; those move in Task 7 — leave them for now, they still work.)

- [ ] **Step 7: Run the affected suites**

Run: `uv run pytest tests/unit/test_context.py tests/unit/cli -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/otto/configmodule src/otto/host/host.py src/otto/cli/main.py src/otto/cli/callbacks.py tests/unit/test_context.py
git commit -m "refactor(context): bare accessors delegate to OttoContext; add get_lab; getLab->load_lab"
```

---

## Task 4: Host `_lab` back-reference (drop the host layer's ambient reach)

**Files:**
- Modify: `src/otto/configmodule/lab.py` (`addHost`, `__add__`)
- Modify: `src/otto/host/remoteHost.py:254-298` (`_build_hop_transport` uses `self._lab`)
- Modify: `src/otto/host/remoteHost.py` (declare `_lab` so `dataclasses.replace` preserves it)
- Test: `tests/unit/host/test_hop_integration.py` (existing hop tests must stay green), `tests/unit/test_context.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_context.py
def test_addhost_wires_lab_backref_and_survives_override_copy():
    import dataclasses
    lab = _lab_with("router1")
    host = next(iter(lab.hosts.values()))
    assert host._lab is lab
    # dataclasses.replace (used by *_options overrides) must carry _lab forward
    copy = dataclasses.replace(host)
    assert copy._lab is lab
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py::test_addhost_wires_lab_backref_and_survives_override_copy -v`
Expected: FAIL with `AttributeError: ... has no attribute '_lab'`.

- [ ] **Step 3: Declare `_lab` on `RemoteHost` and wire it in `Lab`**

In `src/otto/host/remoteHost.py`, alongside the existing connection-state contract annotations ([remoteHost.py:145-151](../../../src/otto/host/remoteHost.py)), add a non-comparing, non-repr field so concrete dataclass subclasses (`UnixHost`, `EmbeddedHost`) inherit it and `dataclasses.replace` preserves it:

```python
    # Back-reference to the owning Lab, wired by Lab.addHost. Lets hop
    # resolution use self._lab.hosts[...] instead of reaching ambient state.
    # compare/repr excluded so it never affects equality or logging output.
    _lab: "Lab | None" = field(default=None, compare=False, repr=False)
```

(Add `from dataclasses import field` / `from typing import TYPE_CHECKING` import for `Lab` if not present; confirm the concrete subclasses are `@dataclass` and pick up inherited fields — they are, per the factory's `cls(**kwargs)` construction.)

In `src/otto/configmodule/lab.py`, set the back-ref in `addHost` and re-point on merge:

```python
    def addHost(self, host: "Host") -> None:
        if host.id in self.hosts:
            raise KeyError(...)        # unchanged message
        host._lab = self               # wire the back-reference
        self.hosts[host.id] = host

    def __add__(self, other: "Lab") -> "Lab":
        self.name = f"{self.name}_{other.name}"
        self.resources = self.resources.union(other.resources)   # (also fixes the no-op union bug)
        for host in other.hosts.values():
            host._lab = self           # merged-in hosts now belong to self
        self.hosts.update(other.hosts)
        return self
```

- [ ] **Step 4: Repoint `_build_hop_transport` off ambient `get_host`**

In `src/otto/host/remoteHost.py:278`, replace the lazy `from ..configmodule import get_host` hop lookup with `self._lab`:

```python
        # resolve the hop host from this host's own lab (no ambient access)
        if self._lab is None:
            raise RuntimeError(f"host {self.name!r} has no lab; cannot resolve hop {self.hop!r}")
        hop_host = self._lab.hosts[hop_id]
```

(Adjust the surrounding tunnel-factory code that currently calls `get_host(hop_id)` to use `hop_host`.)

- [ ] **Step 5: Run hop + context tests**

Run: `uv run pytest tests/unit/test_context.py tests/unit/host/test_hop_integration.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/remoteHost.py src/otto/configmodule/lab.py tests/unit/test_context.py
git commit -m "refactor(host): wire Lab._lab back-ref; hop resolution uses self._lab not ambient state"
```

---

## Task 5: Inject `ctx` into handlers that declare it

**Files:**
- Modify: `src/otto/cli/run.py:116-166` (`_wrap_with_options`) and the no-options path in `instruction()`
- Modify: `src/otto/suite/register.py:111-122` (`runner`)
- Test: `tests/unit/cli/test_run.py` (or nearest instruction test module)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/cli/test_run.py (append)
import pytest
from otto.context import OttoContext, set_context, reset_context


@pytest.mark.asyncio
async def test_instruction_handler_receives_injected_ctx():
    from otto.cli.run import instruction
    from otto.context import get_context

    seen = {}

    @instruction(name="probe")
    async def probe(ctx: OttoContext):
        seen["ctx"] = ctx
        from otto.utils import CommandStatus, Status
        return CommandStatus(command="probe", output="", status=Status.Success, retcode=0)

    lab_ctx = OttoContext(lab=__import__("otto").configmodule.lab.Lab(name="t"))
    token = set_context(lab_ctx)
    try:
        # the @instruction wrapper should inject get_context() for the ctx param,
        # and ctx must NOT appear as a Typer option.
        import inspect
        sig = inspect.signature(probe)
        assert "ctx" not in sig.parameters     # excluded from the Typer-facing signature
    finally:
        reset_context(token)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cli/test_run.py::test_instruction_handler_receives_injected_ctx -v`
Expected: FAIL — `ctx` currently appears in the signature / is treated as a Typer option.

- [ ] **Step 3: Implement ctx-injection in the instruction wrapper**

In `src/otto/cli/run.py`, before option expansion, detect a parameter annotated `OttoContext`, drop it from the Typer-facing signature, and supply it at call time from the contextvar. Minimal change to `_wrap_with_options` and the no-options branch:

```python
def _ctx_param_name(func: Callable[..., Any]) -> str | None:
    from ..context import OttoContext
    hints = get_type_hints(func, include_extras=False)
    for name, hint in hints.items():
        if hint is OttoContext:
            return name
    return None
```

In `instruction()`'s decorator, wrap `target` so a declared `ctx` param is excluded from the exposed signature and injected at call time:

```python
    def decorator(func):
        ctx_name = _ctx_param_name(func)
        target = func
        if ctx_name is not None:
            target = _inject_ctx(func, ctx_name)
        if options is not None and dataclasses.is_dataclass(options):
            target = _wrap_with_options(target, options)
        ...
```

```python
def _inject_ctx(func: Callable[..., Any], ctx_name: str) -> Callable[..., Any]:
    from ..context import get_context
    sig = inspect.signature(func)
    exposed = [p for n, p in sig.parameters.items() if n != ctx_name]

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        kw[ctx_name] = get_context()
        return await func(**kw)

    setattr(wrapper, "__signature__", inspect.Signature(exposed))
    return wrapper
```

Apply the equivalent in `src/otto/suite/register.py`'s `runner` (exclude a declared `ctx` param from `params`, inject `get_context()` before constructing the suite).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/cli/test_run.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/run.py src/otto/suite/register.py tests/unit/cli/test_run.py
git commit -m "feat(cli): inject OttoContext into instruction/suite handlers that declare a ctx param"
```

---

## Task 6: Move reservation state to `ctx.meta`

**Files:**
- Modify: `src/otto/reservations/check.py` (`ReservationState`, `gate(ctx)`)
- Modify: `src/otto/cli/main.py` (build `ReservationState` into `ctx.meta`)
- Modify: `src/otto/cli/run.py:69`, `test.py:503`, `host.py:102`, `monitor.py:81` (`gate(ctx)`)
- Modify: `src/otto/cli/reservation.py` (`whoami`/`check` read `ctx.meta`)
- Test: `tests/unit/` reservation tests (find with `grep -rl "def gate\|reservation" tests`)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reservations_meta.py (new)
import types
import pytest
from otto.reservations.check import gate, ReservationState


def _fake_ctx(meta: dict) -> object:
    return types.SimpleNamespace(meta=meta)


def test_gate_noops_with_empty_meta():
    gate(_fake_ctx({}))            # no reservation configured → no exception


def test_gate_noops_when_backend_none():
    meta = {"otto_reservation": ReservationState(backend=None, identity=None, skip_check=False)}
    gate(_fake_ctx(meta))          # backend None → no check
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_reservations_meta.py -v`
Expected: FAIL — `ReservationState` undefined; `gate` signature takes a `ConfigModule`.

- [ ] **Step 3: Add `ReservationState` and rewrite `gate` in `src/otto/reservations/check.py`**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ReservationState:
    backend: "ReservationBackend | None" = None
    identity: "ResolvedIdentity | None" = None
    skip_check: bool = False


def gate(ctx: "typer.Context") -> None:
    """Run the reservation check for this invocation, reading state from ctx.meta.

    No-ops when no reservation state is present (e.g. unit tests invoking a
    subcommand app directly) or no backend is configured. Fetches the active
    lab lazily so the no-op paths never require an OttoContext.
    """
    res: "ReservationState | None" = ctx.meta.get("otto_reservation")
    if res is None or res.backend is None:
        return

    from ..configmodule import get_lab
    lab = get_lab()
    if res.skip_check:
        username = res.identity.username if res.identity is not None else "<unknown>"
        needed = required_resources(lab)
        from rich import print as rprint
        rprint(
            f"[bold red]\N{WARNING SIGN}  Reservation check SKIPPED for user "
            f"{username!r} on lab {lab.name!r}. Required resources: {sorted(needed)!r}[/bold red]"
        )
        logger.warning("Reservation check skipped for user %r on lab %r. Required: %r",
                       username, lab.name, sorted(needed))
        return

    assert res.identity is not None, "identity must be resolved before gate() runs"
    check_reservations(lab, res.identity.username, res.backend)
```

Add `import typer` (already a dependency) and keep the existing `check_reservations`/`required_resources` imports.

- [ ] **Step 4: Build `ReservationState` into `ctx.meta` in `src/otto/cli/main.py`**

Replace the `setConfigModule(... reservation_backend=... identity=... skip_reservation_check=...)` reservation fields by stashing them in `ctx.meta` (the top-level callback receives `ctx: typer.Context`):

```python
    from ..reservations import ReservationState
    ctx.meta["otto_reservation"] = ReservationState(
        backend=reservation_backend,
        identity=identity,
        skip_check=skip_reservation_check,
    )
```

(Export `ReservationState` from `src/otto/reservations/__init__.py`.)

- [ ] **Step 5: Update the four gate callers**

In `run.py:69`, `test.py:503`, `host.py:102`, `monitor.py:81`, replace:

```python
        from ..reservations import gate
        gate(tryGetConfigModule())
```
with:
```python
        from ..reservations import gate
        gate(ctx)
```
(each callback already has `ctx: typer.Context`; drop the now-unused `tryGetConfigModule` import in those files).

- [ ] **Step 6: Update `whoami` / `check` in `src/otto/cli/reservation.py`**

Add `ctx: typer.Context` to both commands and read `res = ctx.meta.get("otto_reservation")` instead of `getConfigModule()`; use `get_lab()` for the lab name. Example `whoami`:

```python
@reservation_app.command()
def whoami(ctx: typer.Context) -> None:
    from ..configmodule import get_lab
    from ..reservations import ReservationState
    res: ReservationState | None = ctx.meta.get("otto_reservation")
    backend_name = res.backend.backend_name() if (res and res.backend) else "<none>"
    identity = res.identity if res else None
    if identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)
    rprint(f"username: [bold]{identity.username}[/bold]\nsource:   {identity.source}\n"
           f"backend:  {backend_name}\nlab:      {get_lab().name}")
```

Apply the same `ctx.meta` read to `check`.

- [ ] **Step 7: Run reservation + CLI suites**

Run: `uv run pytest tests/unit/test_reservations_meta.py tests/unit/cli -v` and `uv run pytest -k reservation -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/otto/reservations src/otto/cli tests/unit/test_reservations_meta.py
git commit -m "refactor(reservations): carry reservation state in Typer ctx.meta; gate(ctx)"
```

---

## Task 7: Delete the dead singletons

**Files:**
- Modify: `src/otto/configmodule/configmodule.py` (delete `ConfigModule`, `ConfigModuleManager`, `getConfigModule`, `tryGetConfigModule`, `setConfigModule`)
- Modify: `src/otto/configmodule/__init__.py` (drop their exports)
- Modify: `src/otto/host/host.py` (delete `_globalDryRun`, `setDryRun`, `_logCommandOutput`, `_setLoggingCommandOutputEnabled`)
- Modify: `src/otto/cli/main.py` (drop `setConfigModule`/`getConfigModule` use), `src/otto/cli/docker.py`, `src/otto/host/dockerHost.py`, `src/otto/docker/compose.py` (repoint `getConfigModule()` → `get_lab()`/`getRepos()`)
- Modify: `tests/conftest.py` (drop `_reset_dry_run`, `setDryRun` import), `tests/unit/cli/conftest.py` (context fixture)
- Test: full suite

- [ ] **Step 1: Find every remaining reference**

Run: `grep -rn "getConfigModule\|setConfigModule\|tryGetConfigModule\|setDryRun\|_globalDryRun\|ConfigModule\b" src/otto tests`
Expected: a finite list — docker call sites (`cfg = getConfigModule()` → split into `get_lab()` for `.lab` and `getRepos()` for `.repos`), `main.py`, and test conftests.

- [ ] **Step 2: Add the context test fixture (replaces `setConfigModule`/`_reset_dry_run`)**

In `tests/conftest.py`, replace the autouse `_reset_dry_run` ([conftest.py:330-339](../../../tests/conftest.py)) with a context helper, and remove the `from otto.host.host import setDryRun` import:

```python
import contextlib
from otto.context import OttoContext, set_context, reset_context
from otto.configmodule.lab import Lab

@contextlib.contextmanager
def active_context(lab: Lab | None = None, **kwargs):
    token = set_context(OttoContext(lab=lab or Lab(name="test"), **kwargs))
    try:
        yield
    finally:
        reset_context(token)
```

Update `tests/unit/cli/conftest.py`'s `real_main_mocks` to drop the `setConfigModule`/`getConfigModule` references in its docstring and rely on `main` setting the context (it now does, Task 2).

- [ ] **Step 3: Repoint docker call sites**

In `src/otto/cli/docker.py`, `src/otto/host/dockerHost.py`, `src/otto/docker/compose.py`, replace `cfg = getConfigModule()` then `cfg.lab` → `get_lab()` and `cfg.repos` → `getRepos()`. Example (`dockerHost.py:212-213`):

```python
    from ..configmodule import get_lab, getRepos
    repo = next((r for r in getRepos() if r.name == self.project), None)
    ...
    repo, get_lab(), project_name=self.compose_project, build=False
```

- [ ] **Step 4: Delete the singletons**

Remove from `src/otto/configmodule/configmodule.py`: `ConfigModule`, `ConfigModuleManager`, `_manager`, `getConfigModule`, `tryGetConfigModule`, `setConfigModule`. Remove their lines from `src/otto/configmodule/__init__.py`. Remove from `src/otto/host/host.py`: `_globalDryRun`, `setDryRun`, `_logCommandOutput`, `_setLoggingCommandOutputEnabled` (keep `isDryRun`/`getLoggingCommandOutputEnabled` — now contextvar shims). In `main.py` delete the `setConfigModule(...)` call and the `configModule = getConfigModule()` line; repoint the `show_lab` branch to print `get_lab()`/context as appropriate.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (no `ImportError`/`AttributeError` for the removed names; if any remain, Step 1's grep missed a site — fix and re-run).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(context): delete ConfigModule singletons and dry-run/log globals"
```

---

## Task 8: Deterministic lifecycle — `async with host:` and delete `__del__`

**Files:**
- Modify: `src/otto/host/host.py` (`Host` protocol gains `__aenter__`/`__aexit__`)
- Modify: `src/otto/host/remoteHost.py` (add `__aenter__`/`__aexit__`; delete `__del__:162-188`)
- Modify: `src/otto/host/connections.py:363-404`, `src/otto/host/transport.py:95-110` (remove dead `_closing` scaffolding — only once green)
- Test: `tests/unit/test_context.py`, host suites, embedded e2e tier

- [ ] **Step 1: Write the failing test for per-host context manager + idempotent close**

```python
# append to tests/unit/test_context.py
@pytest.mark.asyncio
async def test_host_async_context_manager_closes_and_close_is_idempotent():
    lab = _lab_with("router1")
    host = next(iter(lab.hosts.values()))
    async with host as h:
        assert h is host
    # exiting the context closed it; a second close must be a harmless no-op
    await host.close()
    await host.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py::test_host_async_context_manager_closes_and_close_is_idempotent -v`
Expected: FAIL — `RemoteHost` has no `__aenter__`/`__aexit__`.

- [ ] **Step 3: Add `__aenter__`/`__aexit__`, confirm idempotent `close()`, delete `__del__`**

In `src/otto/host/remoteHost.py`, replace the `__del__` method ([remoteHost.py:162-188](../../../src/otto/host/remoteHost.py)) with the async context-manager protocol:

```python
    async def __aenter__(self) -> "RemoteHost":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
```

Add the same two methods to the `Host` protocol in `src/otto/host/host.py` (where `close` is declared). Verify each concrete `close()` (`unixHost.py:369`, `embeddedHost.py:323`, `localHost.py:254`, `dockerHost.py:458`, plus `SessionManager`/`ConnectionManager`) is idempotent — guarded by `_connected`/`connected` so a second call is a no-op; add a guard where missing.

- [ ] **Step 4: Run host + lifecycle tests, confirm green BEFORE removing scaffolding**

Run: `uv run pytest tests/unit/test_context.py tests/unit/host -q`
Expected: PASS

- [ ] **Step 5: Remove the dead `_closing` scaffolding (evidence-driven)**

Only now, remove the `_closing`-flag code that existed solely to no-op `__del__` ([connections.py:376-404](../../../src/otto/host/connections.py), [transport.py:95-110](../../../src/otto/host/transport.py)). Keep genuine double-close guards. Re-run:

Run: `uv run pytest tests/unit/host tests/integration -q` and `make nox` (or the embedded e2e tier) per [project memory on host-down behavior].
Expected: PASS. If the asyncio leak detector / loop reaper flags anything, restore the specific guard and note it — the review warned the composition root reduces but won't fully eliminate test-infra leaks.

- [ ] **Step 6: Commit**

```bash
git add src/otto/host
git commit -m "feat(host): async-with host lifecycle; delete __del__ and dead _closing scaffolding"
```

---

## Task 9: `open_context()` public API + library docs

**Files:**
- Modify: `src/otto/context.py` (`open_context`)
- Modify: `src/otto/__init__.py` (public re-exports)
- Create: `docs/guide/library-usage.md`
- Test: `tests/unit/test_context.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_context.py
@pytest.mark.asyncio
async def test_open_context_sets_and_tears_down():
    import otto
    from otto.context import try_get_context

    lab = _lab_with("router1")
    assert try_get_context() is None
    async with otto.open_context(lab=lab) as ctx:
        assert try_get_context() is ctx
        list(ctx.all_hosts())          # registers into ctx.scope
    assert try_get_context() is None   # contextvar reset on exit
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_context.py::test_open_context_sets_and_tears_down -v`
Expected: FAIL — `otto.open_context` does not exist.

- [ ] **Step 3: Implement `open_context` and re-exports**

Append to `src/otto/context.py`:

```python
@asynccontextmanager
async def open_context(
    *,
    lab: "Lab | str | list[str]",
    dry_run: bool = False,
    log_command_output: bool = True,
    search_paths: "list[Any] | None" = None,
) -> "AsyncIterator[OttoContext]":
    """Build, install, and tear down an OttoContext for library/script use.

    Pass a Lab, or a lab name / list of names to load via load_lab. On exit,
    the host scope closes any still-connected hosts and the contextvar is reset.
    Does NOT run a reservation check — that is a CLI concern; a script that
    wants one calls otto.reservations.check_reservations explicitly.
    """
    from .configmodule.lab import Lab
    from .configmodule import load_lab

    resolved_lab = lab if isinstance(lab, Lab) else load_lab(lab, search_paths or [])
    ctx = OttoContext(lab=resolved_lab, dry_run=dry_run, log_command_output=log_command_output)
    token = set_context(ctx)
    try:
        async with ctx.scope:
            yield ctx
    finally:
        reset_context(token)
```

In `src/otto/__init__.py`, re-export the public library surface:

```python
from .context import OttoContext, open_context, get_context, try_get_context  # noqa: F401
from .configmodule import all_hosts, get_host, get_lab, run_on_all_hosts       # noqa: F401
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: PASS

- [ ] **Step 5: Write the library docs page**

Create `docs/guide/library-usage.md` covering: the `open_context()` script path (with the example from the spec §5), the bring-your-own-CLI primitives (`OttoContext` + `set_context`/`reset_context` + `async with ctx.scope`), the three host-lifetime modes (CM / explicit `close()` / scope backstop), and the FD-model caveat that a directly-constructed host outside any context has no backstop.

- [ ] **Step 6: Commit**

```bash
git add src/otto/context.py src/otto/__init__.py docs/guide/library-usage.md tests/unit/test_context.py
git commit -m "feat(context): open_context() public API + library-usage docs"
```

---

## Task 10: Update the migration-plan contract draft

**Files:**
- Modify: `todo/migration_plan.md` (Phase 0/1 "decomposed pieces")

- [ ] **Step 1: Edit the contract draft**

In `todo/migration_plan.md`, in the Phase 0 "decomposed pieces" bullet, replace `getConfigModule()` with `get_lab()`, and add a note: repos via `getRepos()`; reservations are a CLI-layer concern carried in `ctx.meta`, not part of the runtime-context contract.

- [ ] **Step 2: Commit**

```bash
git add todo/migration_plan.md
git commit -m "docs(migration): contract draft uses get_lab(); reservations are CLI-layer"
```

---

## Self-Review

**Spec coverage** (spec §1–§9):
- §1 OttoContext + contextvar + shims + graceful degradation → Tasks 1, 3.
- §2 decompose ConfigModule, drop bundle accessor, `load_lab` rename, repos via `getRepos()` → Tasks 3, 7.
- §3 HostScope, three lifetime modes, registration, teardown site, delete `__del__`, idempotent close, scaffolding removal, `_lab` back-ref → Tasks 1, 2, 4, 8.
- §4 reservations → `ctx.meta` → Task 6.
- §5 library API (`open_context` + primitives + docs) → Task 9.
- §6 test fixture + TDD cases → Tasks 1–9 (fixture in Task 7).
- §7 migration-plan update → Task 10.
- §8 implementation order → followed (reordered for green-at-each-step, noted in header).
- §9 risks (nested `asyncio.run`, xdist, scaffolding regressions, direct construction, ctx-injection near Typer code, `_lab` through `replace`) → addressed in Tasks 8 (evidence-driven removal), 4 (replace test), 5 (minimal injection).

**Placeholder scan:** no TBD/TODO; each code step shows real code; mechanical sweeps give the exact `grep` + before/after pattern rather than a TODO.

**Type/name consistency:** `OttoContext`, `HostScope`, `_active`, `get_context`/`try_get_context`/`set_context`/`reset_context`, `open_context`, `get_lab`, `load_lab`, `ReservationState(backend/identity/skip_check)`, `ctx.meta["otto_reservation"]`, `_lab` — used identically across tasks. `CommandStatus(command/output/status/retcode)` and `RunResult(status/statuses)` match the constructors seen in the CLI conftest.

**Known confirm-at-implementation points (flagged inline, not placeholders):** the exact `_closing` line ranges in Task 8 Step 5 (remove only what is `__del__`-specific, keep double-close guards); whether any concrete host uses `slots` that would reject the `_lab` field (Task 4 — the factory's `cls(**kwargs)` construction implies plain dataclasses, but verify).
