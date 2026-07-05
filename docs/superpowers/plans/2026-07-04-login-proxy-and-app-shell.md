# Login Proxy & AppShell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the two-part design in
`docs/superpowers/specs/2026-07-04-login-proxy-and-app-shell-design.md`:
(1) login proxies — creds become a list of cred objects that can declare
"to become me, authenticate as X and run registered proxy N", honored at
session establishment, `switch_user`/`as_user`, oneshot/nc, and interact;
(2) `AppShell` — a REPL abstraction with a required prompt regex and
regex→pydantic output parsing riding the `Result` family.

**Architecture:** New `src/otto/host/login_proxy.py` (Cred, registry, proxy
engine, chain resolution) threaded through the existing seams
(`models/host.py` boundary spec, `ConnectionManager`, `SessionManager`,
`PosixPrivilege`, `interact.py`). New `src/otto/host/app_shell.py`
(`AppShell`, `Parsed`, parse engine) plus `ShellResult` in `result.py`.
Part 1 first; Part 2 builds on it.

**Tech Stack:** Python 3.10+, pydantic v2, asyncio, asyncssh/telnetlib3
(untouched), pytest (+xdist), ruff, ty, Sphinx.

## Global Constraints

- **NEVER** `from __future__ import annotations` (breaks the Sphinx nitpicky gate). Real 3.10+ annotations, module-top imports.
- `ruff check .` AND `ruff format --check .` must pass (nox lint runs both). After `ruff format`, re-run `ruff check` — format is not lint-neutral.
- `ty` runs ONLY at `nox -s typecheck` — budget a typecheck round after src edits. `@override` (from `typing_extensions`) required on every overridden method (`ty` enforces).
- Rules stay ENFORCED — fix code idiomatically, never blanket-ignore; narrow per-site `# noqa: RULE — reason` is last resort (existing style: see `session.py` SLF001 comments).
- Host runtime init-field changes MUST be mirrored in `models/host.py` specs (drift guard) and require the FULL `tests/unit` run.
- Per-task gate: scoped pytest is NOT sufficient evidence — run the named commands in each task, and `make coverage-hostless` before declaring a phase done (fresh worktrees lack the `web/` dist `make coverage` needs).
- Fresh worktree: run `uv sync` first (no `.venv` otherwise; phantom ty import errors).
- Commits: allowed in the worktree (NOT on main). Every commit message: Conventional Commits prefix + blank line + body + trailer line `Assisted-by: Claude Fable 5`. Use `git commit -m "subject" -m "body" -m "Assisted-by: Claude Fable 5"`. If the repo's `prepare-commit-msg` hook aborts on `/dev/tty` (non-interactive), commit that one time with `git -c core.hooksPath=/dev/null commit ...` — the trailer is already embedded, which is all the hook adds.
- Never `git add -u` / `git add .` — stage explicit paths.
- Never run destructive probes inside the repo — tests use `tmp_path`.
- Test tiers: directory = level (`tests/unit`, `tests/integration`, `tests/e2e`); markers = resources. Mirror neighboring tests' markers when adding e2e.
- New public exports go in `src/otto/__init__.py` `__all__` (line ~38) + `_LAZY_EXPORTS` (line ~45); the import-budget guard (`tests/unit/import_budget/`) must stay green — `import otto` must not eagerly import the new modules.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/otto/host/login_proxy.py` (new) | `Cred`, `ProxyIO`, `ProxyContext`, `LoginProxy(Fn)`, `LoginProxyError`, `LOGIN_PROXIES` registry + `register_login_proxy`, built-in `"su"` proxy, `resolve_chain`, `run_proxy`/`run_undo`, `perform_switch` engine |
| `src/otto/models/host.py` | `CredSpec` + list-creds validation; `creds` conversion in `_common_host_kwargs` |
| `src/otto/host/unix_host.py`, `remote_host.py` | `creds: list[Cred]` field + `cred()`/`default_cred` helpers; `_user_password`/`_sudo_password` re-route |
| `src/otto/host/connections.py` | `ConnectionManager` holds `list[Cred]`; `credentials` resolves the direct-auth cred; new `login_target`/`proxy_hops` |
| `src/otto/host/session.py` | hop application after handshake (all session builds); proxied-oneshot routing; `ShellSession._app_shell` lock; `HostSession.switch_user/as_user` via engine |
| `src/otto/host/privilege.py` | `switch_user`/`as_user` via `perform_switch`; `_perform_su` deleted |
| `src/otto/host/host.py` | `interact(as_user=...)`; `BaseHost.app_shell()` |
| `src/otto/host/interact.py` | bridge `ProxyIO` adapter + hop replay before pumps |
| `src/otto/result.py` | `ShellResult` |
| `src/otto/host/app_shell.py` (new) | `AppShell`, `Parsed`, parse engine, `AppShellActiveError`, `AppShellTimeoutError` |
| `src/otto/__init__.py` | lazy exports: `Cred`, `register_login_proxy`, `AppShell`, `Parsed`, `ShellResult` |
| `tests/unit/host/test_login_proxy.py` (new), `tests/unit/host/test_app_shell.py` (new), `tests/unit/models/` (extend) | unit coverage |
| `tests/e2e/host/test_login_proxy_e2e.py`, `tests/e2e/host/test_app_shell_e2e.py` (new) | live-bed / local e2e |
| `tests/_fixtures/lab_data/**`, `tests/repo*/**`, `src/otto/examples/**`, `otto init` scaffold, `schemas/*.json`, docs | list-creds migration + docs |

---

## Phase A — Part 1: Login proxy

### Task 0: Worktree + baseline

**Files:** none (environment)

- [ ] **Step 1:** Create the worktree via the superpowers:using-git-worktrees skill (branch `worktree-login-proxy-app-shell`, based on current `main`). Chris's staged spec/Vagrantfile on main are NOT needed inside the worktree — but if `git status` in the main checkout shows them still staged, leave them untouched.
- [ ] **Step 2:** `uv sync` in the worktree. Run `make coverage-hostless` once to record the green baseline (expect ~3000 pass).
- [ ] **Step 3:** Verify the commit recipe works: make an empty commit `git commit --allow-empty -m "chore: worktree baseline" -m "Assisted-by: Claude Fable 5"`, confirm it lands (fall back to `git -c core.hooksPath=/dev/null commit ...` if the hook aborts), then `git reset --hard HEAD~1` to drop it.

### Task 1: `login_proxy.py` foundations

**Files:**
- Create: `src/otto/host/login_proxy.py`
- Test: `tests/unit/host/test_login_proxy.py`

**Interfaces:**
- Produces: `Cred(login, password=None, proxy=None, via=None, params={})` frozen dataclass; `ProxyIO` Protocol (`send(text, *, log=LogMode.NORMAL)`, `expect(pattern, timeout=10.0) -> str`); `ProxyContext(target: Cred, via: Cred, host_id: str)`; `LoginProxyFn = Callable[[ProxyIO, ProxyContext], Awaitable[None]]`; `LoginProxyError(ConnectionError)`; `register_login_proxy(name, fn, *, undo=None, overwrite=False)`; `LOGIN_PROXIES: Registry[LoginProxy]`; `resolve_chain(creds, target_login) -> tuple[Cred, list[Cred]]`; `run_proxy(io, hop, via, host_id)`; `run_undo(io, hop, via, host_id)`; built-in `"su"` proxy pre-registered.

- [ ] **Step 1: Write failing tests** in `tests/unit/host/test_login_proxy.py` (mirror the async test style of `tests/unit/host/` neighbors; use a small recorder fake):

```python
import re

import pytest

from otto.host.login_proxy import (
    LOGIN_PROXIES,
    Cred,
    LoginProxyError,
    ProxyContext,
    register_login_proxy,
    resolve_chain,
    run_proxy,
    run_undo,
)
from otto.logger.mode import LogMode


class RecorderIO:
    """ProxyIO fake: records sends, replays canned expect output."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.sent: list[tuple[str, LogMode]] = []
        self._replies = list(replies or [])

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        return self._replies.pop(0) if self._replies else ""


ADMIN = Cred(login="admin", password="hunter2")
MYSQL = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")


def test_resolve_chain_direct():
    direct, hops = resolve_chain([ADMIN, MYSQL], "admin")
    assert direct == ADMIN
    assert hops == []


def test_resolve_chain_one_hop():
    direct, hops = resolve_chain([ADMIN, MYSQL], "mysql")
    assert direct == ADMIN
    assert hops == [MYSQL]


def test_resolve_chain_default_via_is_first_directly_loginable():
    orphan = Cred(login="svc", proxy="su")  # no via
    direct, hops = resolve_chain([ADMIN, orphan], "svc")
    assert direct == ADMIN
    assert hops == [orphan]


def test_resolve_chain_unknown_login_is_loud():
    with pytest.raises(LoginProxyError, match="admin"):
        resolve_chain([ADMIN], "nobody")


async def test_su_proxy_sends_su_and_password():
    io = RecorderIO(replies=["Password:"])
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")
    assert io.sent[0] == ("su mysql\n", LogMode.NORMAL)
    assert io.sent[1] == ("sqlpw\n", LogMode.NEVER)


async def test_su_proxy_passwordless_skips_expect():
    io = RecorderIO()
    await run_proxy(io, Cred(login="svc"), via=ADMIN, host_id="h1")
    assert io.sent == [("su svc\n", LogMode.NORMAL)]


async def test_su_proxy_root_default():
    io = RecorderIO()
    await run_proxy(io, Cred(login=""), via=ADMIN, host_id="h1")
    assert io.sent == [("su\n", LogMode.NORMAL)]


async def test_run_proxy_wraps_failure_with_context():
    async def boom(io, ctx):
        raise TimeoutError("no prompt")

    register_login_proxy("boom", boom)
    try:
        with pytest.raises(LoginProxyError, match=r"h1.*mysql.*boom"):
            await run_proxy(io=RecorderIO(), hop=Cred(login="mysql", proxy="boom"),
                            via=ADMIN, host_id="h1")
    finally:
        LOGIN_PROXIES.unregister("boom")


async def test_default_undo_sends_exit():
    io = RecorderIO()
    await run_undo(io, MYSQL, via=ADMIN, host_id="h1")
    assert io.sent == [("exit\n", LogMode.NORMAL)]


async def test_custom_undo_used_when_registered():
    steps: list[str] = []

    async def enter(io, ctx):
        steps.append("enter")

    async def leave(io, ctx):
        steps.append("leave")

    register_login_proxy("custom", enter, undo=leave)
    try:
        hop = Cred(login="x", proxy="custom")
        await run_proxy(RecorderIO(), hop, via=ADMIN, host_id="h1")
        await run_undo(RecorderIO(), hop, via=ADMIN, host_id="h1")
    finally:
        LOGIN_PROXIES.unregister("custom")
    assert steps == ["enter", "leave"]


def test_duplicate_registration_is_loud():
    async def p(io, ctx): ...

    register_login_proxy("dup-test", p)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_login_proxy("dup-test", p)
    finally:
        LOGIN_PROXIES.unregister("dup-test")
```

- [ ] **Step 2:** `uv run --no-sync pytest tests/unit/host/test_login_proxy.py -v` — expect FAIL (module not found).
- [ ] **Step 3: Implement** `src/otto/host/login_proxy.py`:

```python
"""Login proxies: registered multi-step user-switch sequences.

A cred entry may declare that its login cannot be reached by direct
authentication: to *become* it, otto authenticates (or starts) as another
account (``via``) and replays the named proxy's send/expect steps. Proxies
are async callables registered by libraries from ``init`` modules via
:func:`register_login_proxy`, mirroring the term/transfer registries. The
built-in ``"su"`` proxy is the default user-switch mechanism (it replaces
the old hardcoded ``_perform_su``).
"""

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..logger.mode import LogMode
from ..registry import Registry, caller_module


@dataclass(frozen=True)
class Cred:
    """One credential entry: a login plus how to become it."""

    login: str
    """The account name."""

    password: str | None = None
    """Password, or None (key/agent auth on SSH; empty line on telnet; no
    password exchange in the built-in su proxy)."""

    proxy: str | None = None
    """Login-proxy registry key; None means directly loginable (switch via
    the built-in ``"su"``)."""

    via: str | None = None
    """Login of the account the proxy starts from. None defaults to the
    first proxy-less cred entry."""

    params: dict[str, Any] = field(default_factory=dict)
    """Free-form data handed to the proxy callable (host-specific knobs)."""


@runtime_checkable
class ProxyIO(Protocol):
    """Minimal I/O handle a proxy drives.

    Satisfied by hosts, ``HostSession``s, the raw-session adapter used at
    session establishment, and the interact bridge adapter.
    """

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None: ...

    async def expect(self, pattern: str | re.Pattern[str], timeout: float = 10.0) -> str: ...


@dataclass(frozen=True)
class ProxyContext:
    """Everything a proxy step may need. Deliberately NOT the host object —
    running commands mid-proxy on the session being established deadlocks."""

    target: Cred
    via: Cred
    host_id: str


LoginProxyFn = Callable[[ProxyIO, ProxyContext], Awaitable[None]]


@dataclass(frozen=True)
class LoginProxy:
    """A registered proxy: the steps plus an optional reversal."""

    fn: LoginProxyFn
    undo: LoginProxyFn | None = None


class LoginProxyError(ConnectionError):
    """A proxy step failed or a chain could not be resolved."""


LOGIN_PROXIES: Registry[LoginProxy] = Registry(
    "login proxy", register_hint="otto.register_login_proxy()"
)


def register_login_proxy(
    name: str,
    fn: LoginProxyFn,
    *,
    undo: LoginProxyFn | None = None,
    overwrite: bool = False,
) -> None:
    """Register a login proxy under *name* (see :data:`LoginProxyFn`).

    *undo* reverses the steps for ``as_user`` restore; None means the
    default reversal (send ``exit``), correct for any su/sudo-style nested
    shell.
    """
    LOGIN_PROXIES.register(
        name, LoginProxy(fn, undo), overwrite=overwrite, origin=caller_module()
    )


async def _su_proxy(io: ProxyIO, ctx: ProxyContext) -> None:
    """Built-in single-step ``su`` exchange (the pre-proxy default)."""
    login = ctx.target.login
    cmd = "su" if not login else f"su {shlex.quote(login)}"
    await io.send(cmd + "\n")
    if ctx.target.password is not None:
        await io.expect(r"[Pp]assword:")
        await io.send(ctx.target.password + "\n", log=LogMode.NEVER)


register_login_proxy("su", _su_proxy)


def _default_direct(creds: list[Cred]) -> Cred | None:
    return next((c for c in creds if c.proxy is None), None)


def cred_for(creds: list[Cred], login: str) -> Cred | None:
    """Look up a cred entry by login (None when absent)."""
    return next((c for c in creds if c.login == login), None)


def resolve_chain(creds: list[Cred], target_login: str) -> tuple[Cred, list[Cred]]:
    """Resolve the direct-auth cred and the hop list for *target_login*.

    Returns ``(direct, hops)`` where *direct* is the cred to authenticate
    the transport as and *hops* are the proxied creds to apply afterwards,
    outermost (first to run) first. Spec validation guarantees termination;
    the ``seen`` set is a runtime backstop against hand-built cred lists.
    """
    cred = cred_for(creds, target_login)
    if cred is None:
        known = ", ".join(c.login for c in creds) or "<none>"
        raise LoginProxyError(
            f"unknown login {target_login!r}; creds define: {known}"
        )
    hops: list[Cred] = []
    seen = {cred.login}
    while cred.proxy is not None:
        hops.append(cred)
        nxt = cred_for(creds, cred.via) if cred.via is not None else _default_direct(creds)
        if nxt is None or nxt.login in seen:
            raise LoginProxyError(
                f"cred {cred.login!r}: cannot resolve a directly-loginable "
                f"via-chain (missing or cyclic 'via')"
            )
        seen.add(nxt.login)
        cred = nxt
    return cred, list(reversed(hops))


def _get_proxy(hop: Cred) -> LoginProxy:
    return LOGIN_PROXIES.get(hop.proxy or "su")


async def run_proxy(io: ProxyIO, hop: Cred, via: Cred, host_id: str) -> None:
    """Run *hop*'s proxy steps over *io*, wrapping failures with context."""
    proxy = _get_proxy(hop)
    name = hop.proxy or "su"
    try:
        await proxy.fn(io, ProxyContext(target=hop, via=via, host_id=host_id))
    except LoginProxyError:
        raise
    except Exception as e:
        raise LoginProxyError(
            f"{host_id}: login proxy failed becoming {hop.login!r} "
            f"via proxy {name!r}: {e}"
        ) from e


async def run_undo(io: ProxyIO, hop: Cred, via: Cred, host_id: str) -> None:
    """Reverse *hop*: the registered undo, or the default ``exit``."""
    proxy = _get_proxy(hop)
    if proxy.undo is None:
        await io.send("exit\n")
        return
    await proxy.undo(io, ProxyContext(target=hop, via=via, host_id=host_id))
```

- [ ] **Step 4:** `uv run --no-sync pytest tests/unit/host/test_login_proxy.py -v` — expect PASS.
- [ ] **Step 5:** Commit: `git add src/otto/host/login_proxy.py tests/unit/host/test_login_proxy.py && git commit -m "feat(host): login-proxy registry, Cred, chain resolution, built-in su proxy" -m "Assisted-by: Claude Fable 5"`

### Task 2: `perform_switch` engine (recursive via-switching)

**Files:**
- Modify: `src/otto/host/login_proxy.py`
- Test: `tests/unit/host/test_login_proxy.py`

**Interfaces:**
- Produces: `async perform_switch(io, creds, user, password, current_user, host_id) -> list[Cred]` — becomes *user* from *current_user*, recursively switching to `via` first when needed; returns the hops applied in order (for `as_user` undo). Preserves Spec-A semantics: `user=""` → root via bare `su`; explicit `password` overrides; unknown user → ad-hoc `Cred` (su + optional password).

- [ ] **Step 1: Failing tests** (append):

```python
from otto.host.login_proxy import perform_switch


async def test_perform_switch_plain_su_known_cred():
    io = RecorderIO(replies=["Password:"])
    applied = await perform_switch(io, [ADMIN], user="admin", password=None,
                                   current_user="root", host_id="h1")
    assert [c.login for c in applied] == ["admin"]
    assert io.sent[0] == ("su admin\n", LogMode.NORMAL)
    assert io.sent[1] == ("hunter2\n", LogMode.NEVER)


async def test_perform_switch_unknown_user_ad_hoc():
    io = RecorderIO()
    applied = await perform_switch(io, [ADMIN], user="ghost", password=None,
                                   current_user="admin", host_id="h1")
    assert [c.login for c in applied] == ["ghost"]
    assert io.sent == [("su ghost\n", LogMode.NORMAL)]  # no password known


async def test_perform_switch_explicit_password_overrides():
    io = RecorderIO(replies=["Password:"])
    await perform_switch(io, [ADMIN], user="admin", password="other",
                         current_user="root", host_id="h1")
    assert io.sent[1] == ("other\n", LogMode.NEVER)


async def test_perform_switch_recurses_through_via():
    io = RecorderIO(replies=["Password:", "Password:"])
    applied = await perform_switch(io, [ADMIN, MYSQL], user="mysql", password=None,
                                   current_user="root", host_id="h1")
    assert [c.login for c in applied] == ["admin", "mysql"]
    assert io.sent[0][0] == "su admin\n"       # via first
    assert io.sent[2][0] == "su mysql\n"       # then the proxy


async def test_perform_switch_skips_via_when_already_there():
    io = RecorderIO(replies=["Password:"])
    applied = await perform_switch(io, [ADMIN, MYSQL], user="mysql", password=None,
                                   current_user="admin", host_id="h1")
    assert [c.login for c in applied] == ["mysql"]
```

- [ ] **Step 2:** Run — expect FAIL (`perform_switch` missing).
- [ ] **Step 3: Implement** (append to `login_proxy.py`):

```python
async def perform_switch(
    io: ProxyIO,
    creds: list[Cred],
    user: str,
    password: str | None,
    current_user: str,
    host_id: str,
) -> list[Cred]:
    """Become *user* from *current_user*; return the hops applied, in order.

    Semantics preserved from the pre-proxy ``switch_user``: ``user=""``
    targets root via bare ``su``; an explicit *password* overrides the
    creds entry; a user with no creds entry is an ad-hoc ``su`` target.
    A cred whose ``via`` differs from *current_user* first switches to the
    via account (recursively), so ``as_user`` can undo hop-by-hop.
    """
    from dataclasses import replace

    cred = cred_for(creds, user) if user else None
    if cred is None:
        cred = Cred(login=user)
    if password is not None:
        cred = replace(cred, password=password)

    applied: list[Cred] = []
    if cred.via is not None and cred.via != current_user:
        applied += await perform_switch(
            io, creds, cred.via, None, current_user, host_id
        )
        current_user = applied[-1].login
    via = cred_for(creds, current_user) or Cred(login=current_user)
    await run_proxy(io, cred, via=via, host_id=host_id)
    applied.append(cred)
    return applied
```

Move `from dataclasses import replace` to the module-top import line (`from dataclasses import dataclass, field, replace`).

- [ ] **Step 4:** `uv run --no-sync pytest tests/unit/host/test_login_proxy.py -v` — PASS.
- [ ] **Step 5:** Commit (`feat(host): perform_switch engine with recursive via-switching`, same trailer recipe).

### Task 3: `CredSpec` + list-creds boundary validation

**Files:**
- Modify: `src/otto/models/host.py` (creds field at lines 133 and 262; `_COMMON_PLAIN_FIELDS` line 56; `_common_host_kwargs` line 210)
- Test: extend the existing host-spec unit tests (grep `rg -l "UnixHostSpec" tests/unit` and add to the main spec test module)

**Interfaces:**
- Consumes: `Cred`, `resolve_chain`, `LOGIN_PROXIES` from Task 1.
- Produces: `CredSpec(OttoModel)` with `login/password/proxy/via/params` and `.to_cred() -> Cred`; `HostSpec.creds: list[CredSpec]` (default empty), `UnixHostSpec.creds: list[CredSpec]` required with `min_length=1`; list-level validators; legacy-dict error message.

- [ ] **Step 1: Failing tests** (in the existing host-spec test module; adjust fixture dicts as needed):

```python
import pytest
from pydantic import ValidationError

from otto.models.host import UnixHostSpec

BASE = {"ip": "10.0.0.1", "element": "e1"}


def _spec(creds, **extra):
    return UnixHostSpec.model_validate({**BASE, "creds": creds, **extra})


def test_creds_list_minimal():
    spec = _spec([{"login": "admin", "password": "pw"}])
    host = spec.to_host()
    assert host.creds[0].login == "admin"
    assert host.creds[0].password == "pw"


def test_creds_proxied_entry_roundtrip():
    spec = _spec([
        {"login": "admin", "password": "pw"},
        {"login": "mysql", "proxy": "su", "via": "admin", "params": {"svc": "db"}},
    ])
    mysql = spec.to_host().creds[1]
    assert (mysql.proxy, mysql.via, mysql.params) == ("su", "admin", {"svc": "db"})


def test_creds_legacy_dict_rejected_with_migration_hint():
    with pytest.raises(ValidationError, match="list of cred objects"):
        _spec({"admin": "pw"})


def test_creds_duplicate_login_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        _spec([{"login": "a", "password": "x"}, {"login": "a", "password": "y"}])


def test_creds_via_requires_proxy():
    with pytest.raises(ValidationError, match="require 'proxy'"):
        _spec([{"login": "a", "password": "x"}, {"login": "b", "via": "a"}])


def test_creds_via_must_name_listed_login():
    with pytest.raises(ValidationError, match="unknown 'via'"):
        _spec([{"login": "a", "password": "x"},
               {"login": "b", "proxy": "su", "via": "ghost"}])


def test_creds_via_cycle_rejected():
    with pytest.raises(ValidationError, match="chain"):
        _spec([{"login": "a", "proxy": "su", "via": "b"},
               {"login": "b", "proxy": "su", "via": "a"}])


def test_creds_proxy_name_checked_against_registry():
    with pytest.raises(ValidationError, match="not a registered login proxy"):
        _spec([{"login": "a", "password": "x"},
               {"login": "b", "proxy": "nope", "via": "a"}])


def test_user_must_be_listed_login():
    with pytest.raises(ValidationError, match="user"):
        _spec([{"login": "a", "password": "x"}], user="ghost")


def test_creds_required_on_unix_host():
    with pytest.raises(ValidationError):
        UnixHostSpec.model_validate(BASE)
```

- [ ] **Step 2:** Run the module — expect FAIL.
- [ ] **Step 3: Implement** in `models/host.py`:

```python
from ..host.login_proxy import LOGIN_PROXIES, Cred, LoginProxyError, resolve_chain


class CredSpec(OttoModel):
    """One ``creds`` entry: a login plus (optionally) how to become it."""

    login: str
    password: str | None = None
    proxy: str | None = None
    via: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _proxy_field_rules(self) -> "CredSpec":
        if self.proxy is None and (self.via is not None or self.params):
            raise ValueError(f"cred {self.login!r}: 'via' and 'params' require 'proxy'")
        if self.via is not None and self.via == self.login:
            raise ValueError(f"cred {self.login!r}: 'via' cannot reference itself")
        return self

    def to_cred(self) -> Cred:
        return Cred(
            login=self.login,
            password=self.password,
            proxy=self.proxy,
            via=self.via,
            params=dict(self.params),
        )
```

On `HostSpec`: replace the `creds` field (line 133) with

```python
    creds: list[CredSpec] = Field(default_factory=list)
```

add a before-validator (near `_strip_comment_keys`) for the legacy shape:

```python
    @field_validator("creds", mode="before")
    @classmethod
    def _reject_legacy_creds_dict(cls, v: object) -> object:
        if isinstance(v, dict):
            raise ValueError(
                "creds is now a list of cred objects: "
                '[{"login": "user", "password": "pw"}, ...] '
                "(was: {user: password}). See the host-database guide."
            )
        return v
```

and an after-validator on the model for cross-entry rules:

```python
    @model_validator(mode="after")
    def _validate_cred_entries(self) -> "HostSpec":
        logins = [c.login for c in self.creds]
        dupes = {n for n in logins if logins.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate cred logins: {sorted(dupes)}")
        by = set(logins)
        for c in self.creds:
            if c.via is not None and c.via not in by:
                raise ValueError(f"cred {c.login!r}: unknown 'via' {c.via!r}")
            if c.proxy is not None and c.proxy not in LOGIN_PROXIES:
                known = ", ".join(sorted(LOGIN_PROXIES.names()))
                raise ValueError(
                    f"cred {c.login!r}: {c.proxy!r} is not a registered "
                    f"login proxy. Known: {known}"
                )
        runtime = [c.to_cred() for c in self.creds]
        for c in runtime:
            if c.proxy is not None:
                try:
                    resolve_chain(runtime, c.login)
                except LoginProxyError as e:
                    raise ValueError(f"cred {c.login!r}: unresolvable via-chain: {e}") from None
        if self.user is not None and self.creds and self.user not in by:
            raise ValueError(f"user {self.user!r} is not a cred login: {sorted(by)}")
        return self
```

`UnixHostSpec` override (line 262) becomes:

```python
    creds: list[CredSpec] = Field(min_length=1)  # required for a Unix host
```

In `_common_host_kwargs` (line 210): remove `"creds"` from `_COMMON_PLAIN_FIELDS` (line 59) and add the conversion:

```python
        if "creds" in s:
            kw["creds"] = [c.to_cred() for c in self.creds]
```

- [ ] **Step 4:** Run the spec test module — new tests PASS; pre-existing tests in that module will fail on old-shape fixtures — that migration is Task 5. Only assert the NEW tests pass here.
- [ ] **Step 5:** Commit (`feat(models): CredSpec list-creds boundary validation`).

### Task 4: Runtime plumbing (host fields, connections, session seeding)

**Files:**
- Modify: `src/otto/host/unix_host.py` (`creds` field ~line 119; `_user_password` ~294; `_sudo_password` ~288), `src/otto/host/remote_host.py` (contract ~line 116; hop creds ~388), `src/otto/host/embedded_host.py` (`creds` field ~line 125 — same `list[Cred]` migration; embedded creds ride the identical ConnectionManager path), `src/otto/host/connections.py` (`TermContext` line 44; ctor ~200; `credentials` 254-266), `src/otto/host/session.py` (`_login_user` 1133-1143)
- Test: `tests/unit/host/test_login_proxy.py` (chain-aware `credentials`), existing connection/session unit tests updated to list-creds fakes

**Interfaces:**
- Produces: `UnixHost.creds: list[Cred]`; `UnixHost.cred(login) -> Cred` (raises `LoginProxyError` listing logins when absent) and `UnixHost.default_cred -> Cred | None`; `ConnectionManager.credentials -> tuple[str, str | None]` (resolved **direct** cred), `ConnectionManager.login_target -> str` (requested login: `_user` or first entry or `""`), `ConnectionManager.proxy_hops -> list[Cred]`.

- [ ] **Step 1: Failing tests** for the ConnectionManager surface (place alongside existing connection tests):

```python
from otto.host.connections import ConnectionManager
from otto.host.login_proxy import Cred

ADMIN = Cred(login="admin", password="hunter2")
MYSQL = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")


def _mgr(creds, user=None):
    return ConnectionManager(ip="10.0.0.1", creds=creds, user=user, term="ssh", name="h1")


def test_credentials_resolves_direct_cred_for_proxied_target():
    mgr = _mgr([MYSQL, ADMIN], user="mysql")
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "mysql"
    assert [c.login for c in mgr.proxy_hops] == ["mysql"]


def test_credentials_plain_first_entry_default():
    mgr = _mgr([ADMIN, MYSQL])
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "admin"
    assert mgr.proxy_hops == []


def test_credentials_empty_creds_loginless():
    mgr = _mgr([])
    assert mgr.credentials == ("", "")
    assert mgr.login_target == ""
```

- [ ] **Step 2:** Run — FAIL (ConnectionManager still takes a dict).
- [ ] **Step 3: Implement.** In `connections.py`: `TermContext.creds` and the ctor param become `list[Cred]` (`_creds` attribute replaces `_creds_dict`); rewrite the property block:

```python
    @property
    def login_target(self) -> str:
        """The login the session should end up as (host.user or first entry)."""
        if self._user is not None:
            return self._user
        return self._creds[0].login if self._creds else ""

    @property
    def credentials(self) -> tuple[str, str | None]:
        """(username, password) for TRANSPORT auth — the resolved direct cred.

        For a proxied ``login_target`` this is the via-chain's directly
        loginable end; the hops are applied post-handshake (see
        ``proxy_hops``). ``('', '')`` when no creds are configured.
        """
        if not self._creds:
            return ("", "")
        direct, _ = resolve_chain(self._creds, self.login_target)
        return direct.login, direct.password

    @property
    def proxy_hops(self) -> list[Cred]:
        """Proxied creds to apply after the marker handshake, outermost first."""
        if not self._creds:
            return []
        _, hops = resolve_chain(self._creds, self.login_target)
        return hops
```

Consumers of `credentials` (`ssh()` line ~319, `ftp()` ~359, `telnet()` ~402, and `session.py:1449`) keep working; where a `str` password is required (telnet/ftp), pass `password or ""`. asyncssh gets `password=password` (None ⇒ key/agent auth). Hop-transport creds in `remote_host.py:388-392` use the same first-entry/`user` rule via `login_target`+`credentials` of the hop's manager (adapt in place). In `unix_host.py`: field becomes `creds: list[Cred]`, add:

```python
    def cred(self, login: str) -> Cred:
        """The cred entry for *login*; loud lookup listing known logins."""
        found = cred_for(self.creds, login)
        if found is None:
            known = ", ".join(c.login for c in self.creds) or "<none>"
            raise LoginProxyError(f"{self.name}: no cred for login {login!r}. Known: {known}")
        return found

    @property
    def default_cred(self) -> Cred | None:
        """First cred entry — the default login user."""
        return self.creds[0] if self.creds else None
```

`_user_password(user)` → `c = cred_for(self.creds, user); return c.password if c else None`. `_sudo_password()` → password of the **current user**: `c = cred_for(self.creds, self.current_user); return c.password if c else None` (spec §3). `SessionManager._login_user` (session.py:1133) → prefer `getattr(self._connections, "login_target", None)`, falling back to `credentials[0]`, then `""` (keeps fake-tolerance). Check `LocalHost`/`DockerContainerHost` (`local_host.py:155`, `docker_host.py:163`) — wherever they pass `user_password=`, leave the seam intact for now (Task 6 replaces it).

- [ ] **Step 4:** Run the new tests — PASS. Run `uv run --no-sync pytest tests/unit/host -x -q` and fix ONLY failures caused by fakes constructing `ConnectionManager(creds={...})` — update those fakes to `list[Cred]`. Lab-data-driven failures wait for Task 5.
- [ ] **Step 5:** Commit (`feat(host)!: creds become list[Cred]; ConnectionManager resolves the direct-auth chain`).

### Task 5: In-repo lab-data migration + schema regen

**Files:**
- Modify: every `hosts.json` under `tests/` (`rg -l '"creds"' tests/`), `src/otto/examples/` sample lab data, the `otto init` scaffold (`rg -rn '"creds"' src/otto` for the template), documentation snippets (`rg -l '"creds"' docs/`), `schemas/*.json` (regenerated)
- Test: full unit suite

- [ ] **Step 1:** Mechanically convert every creds dict `{"user": "pw", ...}` to `[{"login": "user", "password": "pw"}, ...]`, preserving entry order (first entry stays first — it is the default login). A `python - <<'EOF'` script over the found files is fine; review the diff by eye.
- [ ] **Step 2:** `make schema` — regenerates `schemas/*.json` from the specs; stage the changes.
- [ ] **Step 3:** `uv run --no-sync pytest tests/unit -x -q` — fix remaining old-shape references (test fixtures constructing hosts directly with dict creds → `[Cred(...)]`). Expect drift-guard tests to pass now (`CredSpec` mirrors the runtime field).
- [ ] **Step 4:** `make coverage-hostless` — green.
- [ ] **Step 5:** Commit (`feat(host)!: migrate in-repo lab data + schemas to list-creds`).

### Task 6: `switch_user`/`as_user` through the proxy engine

**Files:**
- Modify: `src/otto/host/privilege.py` (delete `_perform_su`; rewrite `switch_user`/`as_user`), `src/otto/host/session.py` (`HostSession.switch_user` 955-966, `as_user` 968-979, ctor `user_password` seam 923-939, `SessionManager` ctor 1067+)
- Test: existing Spec-A elevation tests (update), plus new multi-hop tests

**Interfaces:**
- Consumes: `perform_switch`, `run_undo` (Tasks 1-2).
- Produces: unchanged public signatures — `switch_user(user="", password=None)`, `as_user(user="root", password=None)` on hosts and `HostSession`. The `user_password` seam on `SessionManager`/`HostSession` is REPLACED by `creds: list[Cred] | None` + `host_id: str` (None ⇒ elevation unsupported; `[]` ⇒ supported, passwordless). `PosixPrivilege` gains `_switch_creds() -> list[Cred]` (default `getattr(self, "creds", [])`).

- [ ] **Step 1:** Locate the Spec-A elevation tests (`rg -l "switch_user" tests/unit`). Write new failing tests: multi-hop `as_user` undoes in reverse (2 exits for admin→mysql from root); `switch_user` to a proxied cred from its via user runs only the final hop; `HostSession.switch_user` on a proxied cred works and stamps `current_user`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** `privilege.py`: delete `_perform_su` (lines 37-58) and rewrite:

```python
    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        applied = await perform_switch(
            _HostProxyIO(self), self._switch_creds(), user, password,
            self._session_mgr.current_user, getattr(self, "name", ""),
        )
        self._session_mgr._set_current_user(applied[-1].login or "root")  # noqa: SLF001 — intra-package elevation bookkeeping

    @asynccontextmanager
    async def as_user(self, user: str = "root", password: str | None = None):
        prev = self._session_mgr.current_user
        applied = await perform_switch(
            _HostProxyIO(self), self._switch_creds(), user, password,
            prev, getattr(self, "name", ""),
        )
        self._session_mgr._set_current_user(applied[-1].login or "root")  # noqa: SLF001
        try:
            yield self
        finally:
            for i, hop in enumerate(reversed(applied)):
                via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
                await run_undo(_HostProxyIO(self), hop, Cred(login=via_login),
                               getattr(self, "name", ""))
            self._session_mgr._set_current_user(prev)  # noqa: SLF001
```

`_HostProxyIO` is a 6-line adapter delegating to `self.send`/`self.expect` (host `send` already takes `log=`; keep the ty annotations happy rather than passing the mixin itself). `_switch_creds()` default returns `getattr(self, "creds", [])`. Keep `_sudo_password`/`_elevate` (sudo path unchanged). Mirror the same rewrite on `HostSession` (its ctor now takes `creds`/`host_id` instead of `user_password`; `SessionManager` forwards them; `unix_host.py:327` / `local_host.py:155` / `docker_host.py:163` construction sites pass `creds=self.creds` (LocalHost/Docker: pass their creds if they have one, else `[]`) and `host_id=self.name`). `NotImplementedError` behavior for non-posix hosts keyed on `creds is None`, matching today's `user_password is None`.

- [ ] **Step 4:** Run elevation tests + `uv run --no-sync pytest tests/unit/host -q` — PASS. Confirm `rg -n "_perform_su" src tests` returns nothing.
- [ ] **Step 5:** Commit (`feat(host): switch_user/as_user route through the login-proxy engine; _perform_su deleted`).

### Task 7: Hop application at session establishment

**Files:**
- Modify: `src/otto/host/session.py` (`_ensure_session` ~1216-1252, `open_session` ~1402-1506)
- Test: new tests near the existing SessionManager tests (fake `ShellSession` + fake connections exposing `proxy_hops`)

**Interfaces:**
- Produces: private `SessionManager._apply_login_proxy(session: ShellSession) -> None` — reads `proxy_hops`/`login_target` off `self._connections` (tolerating fakes without them), runs each hop over a `_SessionProxyIO` adapter (send/expect on the raw `ShellSession`, honoring `log=LogMode.NEVER` redaction via the existing `_log_command` sinks), then stamps `session.current_user = login_target`. Called immediately after every successful `_ensure_initialized()` (both the default-session path in `_ensure_session` and the named-session build inside `open_session`). On `LoginProxyError`: close + discard the session, re-raise.

- [ ] **Step 1: Failing test sketch** (adapt to the local fake style):

```python
async def test_default_session_applies_proxy_hops(fake_session_manager_with_hops):
    mgr = fake_session_manager_with_hops        # connections: target mysql via admin
    await mgr._ensure_session()
    sess = mgr._session
    assert sess.current_user == "mysql"
    assert "su mysql" in "".join(sess.writes)   # hop replayed on the raw session


async def test_failed_hop_tears_session_down(fake_session_manager_with_bad_hop):
    with pytest.raises(LoginProxyError):
        await fake_session_manager_with_bad_hop._ensure_session()
    assert fake_session_manager_with_bad_hop._session is None
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement** `_apply_login_proxy` + `_SessionProxyIO`; insert the call in `_ensure_session` right after the `await new_session._ensure_initialized()` success (before `self._session = new_session`), and in `open_session` after its session init, before `_seed_user`/registration. `_seed_user` stays (it stamps `login_target` now via Task 4's `_login_user`). The failure path mirrors the existing `ConnectionError` cleanup (`close()` + suppress) but does NOT consume the one-retry (a proxy failure is not a handshake race — let it propagate on first failure).
- [ ] **Step 4:** New tests PASS; `uv run --no-sync pytest tests/unit/host -q` green.
- [ ] **Step 5:** Commit (`feat(host): proxied logins at session establishment (default, named, pooled)`).

### Task 8: Proxied oneshot routing (SSH) — nc ownership

**Files:**
- Modify: `src/otto/host/session.py` (`SessionManager.oneshot` ~1321+)
- Test: SessionManager oneshot tests (existing module)

**Interfaces:**
- Produces: `oneshot()` uses the pooled named-session path (already proxied via Task 7) whenever `self._connections` reports non-empty `proxy_hops`, even when an `_oneshot_factory` (SSH exec channel) is configured. Raw exec channels cannot replay proxy steps; this keeps every command surface on the effective user — and makes nc transfers (whose `exec_cmd` is `UnixHost.oneshot`, see `unix_host.py:398`) land files owned by the target user.

- [ ] **Step 1: Failing test:** with a fake `_oneshot_factory` that records calls and fake connections with one hop, `await mgr.oneshot("id")` must NOT call the factory (assert pool path used); with no hops the factory IS called.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement** — at the top of `oneshot()`:

```python
        hops = getattr(self._connections, "proxy_hops", [])
        if self._oneshot_factory is not None and not hops:
            ...existing factory path...
        # else: fall through to the pooled-session path (proxied via
        # _apply_login_proxy on the pool sessions)
```

Match the actual structure of the method when editing (read it first — it dispatches factory vs pool).

- [ ] **Step 4:** Tests PASS; `uv run --no-sync pytest tests/unit/host -q` green.
- [ ] **Step 5:** Commit (`feat(host): oneshot/nc route through proxied pool sessions when the user is proxied`).

### Task 9: Interact `--as-user` with hop replay

**Files:**
- Modify: `src/otto/host/host.py` (`interact` verb ~537-550), `src/otto/host/unix_host.py` (`_interact` ~446-495), `src/otto/host/interact.py` (`run_ssh_login` ~400, `run_telnet_login` ~485)
- Test: unit tests for the bridge adapter (fake reader/writer); the pty-driven interact e2e is extended in Task 10

**Interfaces:**
- Produces: `interact(as_user: str | None = None)` (CLI flag `--as-user` via the `@cli_exposed` synthesizer — verify with `otto host <id> login --help` in Step 4); `_BridgeProxyIO` in `interact.py` (send → write bytes to the bridge writer with the telnet/ssh-appropriate line ending; expect → buffered read-until-regex with timeout); `run_ssh_login`/`run_telnet_login` gain `proxy_hops: list[Cred]` + `host_id: str` params (default empty) and replay hops through `_BridgeProxyIO` after auth, BEFORE starting the stdin/stdout pumps.

- [ ] **Step 1:** Failing unit test: `_BridgeProxyIO` over an in-memory reader/writer pair replays the built-in su hop (send `su mysql`, expect `Password:`, send password) and times out with `LoginProxyError` context when the pattern never arrives.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** `UnixHost._interact(as_user)`: resolve `target = as_user or self._connections.login_target`; `direct, hops = resolve_chain(self.creds, target)`; build the ssh/telnet bridge authenticating as `direct` (telnet: dedicated `TelnetClient(user=direct.login, password=direct.password or "")`; ssh: the cached conn is already the direct cred — they're the same resolution) and pass `proxy_hops=hops, host_id=self.name` down. In `interact.py`, after the connection/login succeeds and before `_run_bridge` starts pumps, `for hop in hops: await run_proxy(bridge_io, hop, via=..., host_id=host_id)`. Keep it visible: proxy exchange bytes may echo to the user's terminal — acceptable (it shows what happened) and simplest.
- [ ] **Step 4:** Unit tests PASS. Manually smoke against a live VM: `uv run --no-sync otto -l <unix lab> host <test-vm-id> login --as-user <proxied login>` is deferred to Task 10's bed wiring — for now assert `--as-user` renders in `--help` output via the CLI e2e pattern (`rg "as-user" tests/e2e` for the help-assert style).
- [ ] **Step 5:** Commit (`feat(host): interact --as-user replays login-proxy hops over the bridge`).

### Task 10: Part 1 e2e on the live bed + exports

**Files:**
- Modify: `tests/_fixtures/lab_data/tech1/hosts.json` (or the unix-lab file the e2e suite leases — `rg -l "10.10.200.1" tests/_fixtures`), the leased repo's init module (where the fixture repo registers custom backends — `rg -n "register_" tests/_fixtures | head`), `src/otto/__init__.py` (`__all__` ~38, `_LAZY_EXPORTS` ~45)
- Create: `tests/e2e/host/test_login_proxy_e2e.py`
- Test: the new e2e module (resource-marked like its neighbors)

**Interfaces:**
- Consumes: the live test VMs (carrot/tomato/pepper) provisioned 2026-07-04 with: Unix user `mysql` (password `Password1`, shell `/bin/false`, sshd `DenyUsers mysql`), MySQL socket-auth account + `otto_test` DB. `vagrant` is passwordless-sudo.
- Produces: lab-data cred entry + registered `sudo-su-shell` proxy; public exports `Cred`, `register_login_proxy`.

- [ ] **Step 1:** Add to the leased unix hosts' creds (each test VM entry):

```json
"creds": [
  {"login": "vagrant", "password": "vagrant"},
  {"login": "test", "password": "Password1"},
  {"login": "mysql", "password": "Password1", "proxy": "sudo-su-shell", "via": "vagrant"}
]
```

and register in the fixture repo's init module:

```python
from otto import register_login_proxy


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")


register_login_proxy("sudo-su-shell", _sudo_su_shell)
```

- [ ] **Step 2:** Write the e2e tests (mark with the same resource markers as neighboring unix-bed e2e tests; NEVER skip on host-down — fail loudly):

```python
async def test_direct_ssh_as_mysql_is_denied(...):
    # host configured with user='mysql' but creds mutated to strip the proxy →
    # expect ConnectionError; simplest: assert sshd denies by attempting a
    # direct asyncssh connect as mysql/Password1 (DenyUsers).

async def test_proxied_default_session(...):
    # host.user = 'mysql': (await host.run("whoami")).only.value.strip() == "mysql"

async def test_switch_user_roundtrip(...):
    # from vagrant: async with host.as_user("mysql"): whoami == mysql; after: vagrant

async def test_nc_put_owned_by_proxied_user(...):
    # transfer pin 'nc', host.user='mysql': put tmp file, then
    # stat -c %U == "mysql"

async def test_oneshot_runs_as_proxied_user(...):
    # (await host.oneshot("whoami")).value.strip() == "mysql"
```

Structure per existing e2e host tests (they build hosts from the fixture lab via the leased bed; copy a neighbor's fixture usage).

- [ ] **Step 3:** Add exports: `__all__ += ["Cred", "register_login_proxy"]`; `_LAZY_EXPORTS |= {"Cred": ("otto.host.login_proxy", "Cred"), "register_login_proxy": ("otto.host.login_proxy", "register_login_proxy")}` (match the literal dict style in place). Run `uv run --no-sync pytest tests/unit/import_budget -q` — must stay green.
- [ ] **Step 4:** Run the e2e module against the live bed: `uv run --no-sync pytest tests/e2e/host/test_login_proxy_e2e.py -v` (single pass, no xdist storm — dev-VM load rule). All pass.
- [ ] **Step 5:** `make coverage-hostless` green, then commit (`feat(host): login-proxy e2e on the mysql bed; public exports`).

---

## Phase B — Part 2: AppShell

### Task 11: `ShellResult`

**Files:**
- Modify: `src/otto/result.py`
- Test: extend the result unit tests (`rg -l "CommandResult" tests/unit | head`)

**Interfaces:**
- Produces: `ShellResult(Result)` frozen dataclass with `command: str = ""`, `output: str = ""`; `value` = parsed object (or the output when unparsed); inherits `Result.exit_code`.

- [ ] **Step 1: Failing tests:**

```python
from otto.result import ShellResult
from otto.utils import Status


def test_shell_result_success_truthy_with_parsed_value():
    r = ShellResult(Status.Success, value={"n": 1}, command="SELECT 1;", output="| 1 |")
    assert r and r.exit_code == 0 and r.output == "| 1 |"


def test_shell_result_parse_failure_is_falsy_and_keeps_output():
    r = ShellResult(Status.Failed, value=None, msg="pattern did not match",
                    command="SELECT 1;", output="garbage")
    assert not r and r.exit_code == Status.Failed.value and r.output == "garbage"
```

- [ ] **Step 2:** Run — FAIL. **Step 3: Implement** (mirror `CommandResult`'s docstring style; no `retcode` — REPLs have none):

```python
@dataclass(frozen=True)
class ShellResult(Result):
    """Result of one :class:`~otto.host.app_shell.AppShell` command.

    :attr:`~otto.result.Result.value` holds the parsed object (or the raw
    output when no parser was given); :attr:`output` always keeps the raw,
    prompt-stripped text for debugging.
    """

    command: str = ""
    """The line sent to the application shell."""

    output: str = ""
    """Raw output between the echoed command and the next prompt."""
```

- [ ] **Step 4:** Tests PASS. **Step 5:** Commit (`feat(result): ShellResult for AppShell commands`).

### Task 12: `Parsed` + parse engine (single/list/callable/nested)

**Files:**
- Create: `src/otto/host/app_shell.py` (parsing half)
- Test: `tests/unit/host/test_app_shell.py`

**Interfaces:**
- Produces: `Parsed(OttoModel)` with `pattern: ClassVar[re.Pattern[str]]` and class-def-time checks (pattern present; named groups ⊆ field names; ⊇ required fields — recursion covered per-class since every `Parsed` subclass self-checks); `ParseMismatch(ValueError)`; `parse_one(model, text) -> Parsed`; `parse_all(model, text) -> list[Parsed]`; `apply_parse(spec, text) -> Any` where spec is `type[Parsed] | list[Model] | Callable[[str], Any]`. Nested rule: a field annotated as a `Parsed` subclass (or `list[Sub]` / `Sub | None`) is recursively parsed from its named group's captured region.

- [ ] **Step 1: Failing tests** (include the spec §9 mysql example verbatim — `Row`/`Table`/`QueryStats`/`Select` against the sample SELECT output — plus):

```python
def test_parsed_requires_pattern():
    with pytest.raises(TypeError, match="pattern"):
        class NoPattern(Parsed):
            x: int


def test_parsed_group_field_drift_is_class_def_error():
    with pytest.raises(TypeError, match="named groups"):
        class Drift(Parsed):
            pattern = re.compile(r"(?P<typo>\d+)")
            x: int


def test_parse_one_converts_types():
    class Kv(Parsed):
        pattern = re.compile(r"(?P<key>\w+)=(?P<n>\d+)")
        key: str
        n: int
    assert parse_one(Kv, "a=5").n == 5


def test_parse_one_mismatch_raises():
    ...  # ParseMismatch with the pattern in the message


def test_parse_all_empty_is_valid():
    ...  # finditer over text with no matches -> []


def test_optional_group_maps_to_none():
    ...  # (?P<opt>...)?  + field `opt: str | None`


def test_nested_select_example():
    ...  # spec §9 Select/Table/Row/QueryStats against the sample output:
    # value.stats.count == 5, value.stats.seconds == 0.0,
    # len(value.table.rows) == 5


def test_apply_parse_list_form():
    ...  # apply_parse(list[Kv], "a=1 b=2") -> [Kv, Kv]


def test_apply_parse_callable_form():
    ...  # apply_parse(str.upper, "x") == "X"
```

- [ ] **Step 2:** Run — FAIL. **Step 3: Implement** in `app_shell.py`:

```python
class ParseMismatch(ValueError):
    """Output did not match the model's pattern."""


class Parsed(OttoModel):
    """pydantic model + the regex that produces it.

    Named groups feed same-named fields; a field typed as another
    ``Parsed`` subclass (or ``list[Sub]``) is recursively parsed from the
    region its group captured.
    """

    pattern: ClassVar[re.Pattern[str]]

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        pattern = getattr(cls, "pattern", None)
        if not isinstance(pattern, re.Pattern):
            raise TypeError(f"{cls.__name__} must define a compiled ClassVar 'pattern'")
        groups = set(pattern.groupindex)
        fields = set(cls.model_fields)
        required = {n for n, f in cls.model_fields.items() if f.is_required()}
        if groups - fields:
            raise TypeError(
                f"{cls.__name__}: pattern named groups {sorted(groups - fields)} "
                f"have no matching field"
            )
        if required - groups:
            raise TypeError(
                f"{cls.__name__}: required fields {sorted(required - groups)} "
                f"have no pattern named group"
            )
```

Field-type introspection (`typing.get_origin/get_args`, unwrap `X | None`) picks per-group handling in `_from_match`; `parse_one` = `pattern.search` else `ParseMismatch`; `parse_all` = `finditer` list; `apply_parse` dispatches on `isinstance(spec, type) and issubclass(spec, Parsed)` / `get_origin(spec) is list` / callable. Wrap callable exceptions in `ParseMismatch`.

- [ ] **Step 4:** Tests PASS. **Step 5:** Commit (`feat(host): Parsed models with nested regex-region parsing`).

### Task 13: `AppShell` core + session locking

**Files:**
- Modify: `src/otto/host/app_shell.py`, `src/otto/host/session.py` (`ShellSession.__init__` add `self._app_shell: object | None = None`; `run_cmd` ~351 add the guard)
- Test: `tests/unit/host/test_app_shell.py` (fake `HostSession` driving a scripted `ShellSession`)

**Interfaces:**
- Produces: `AppShell` (ClassVars `launch: str`, `prompt: re.Pattern[str] | str` normalized to Pattern, `quit_cmd = "exit"`, `user: str | None = None`, `cmd_timeout = 30.0`); `AppShell.attach(session: HostSession)` async CM classmethod; `await shell.cmd(text, *, parse=None, timeout=None) -> ShellResult`; `AppShellActiveError(RuntimeError)`; `AppShellTimeoutError(TimeoutError)`. `ShellSession.run_cmd` raises `AppShellActiveError` while `_app_shell` is set (local import of the error to avoid a cycle); `send`/`expect` stay usable.

- [ ] **Step 1: Failing tests** — cover: `__init_subclass__` rejects a subclass missing `launch`/`prompt`; `attach` sends launch + waits for prompt, and on exit sends `quit_cmd` then triggers frame recovery and clears the lock; `run()` on the locked session raises `AppShellActiveError` naming the shell class; nested `attach` raises; `cmd()` returns `ShellResult` with echo/prompt-stripped output; `cmd(parse=Model)` mismatch → failed `ShellResult` (not an exception) with output preserved; prompt timeout → `AppShellTimeoutError`, shell marked broken, exit path skips `quit_cmd` but still recovers/unlocks.
- [ ] **Step 2:** Run — FAIL. **Step 3: Implement.** Key mechanics:
  - `_enter`: guard `session._session._app_shell is None` else raise; set it; `await session.send(self.launch + "\n")`; `await session.expect(self.prompt, timeout=self.cmd_timeout)` (timeout → unlock + `AppShellTimeoutError`).
  - `cmd`: `send(text + "\n")`; `out = await session.expect(self.prompt, timeout or self.cmd_timeout)`; strip trailing prompt match, a leading echoed `text` line if present, and ANSI (reuse the ANSI-strip helper the command frame uses — `rg -n "ansi" src/otto/host/command_frame.py`); no parse → `ShellResult(Status.Success, value=body, command=text, output=body)`; `ParseMismatch` → `ShellResult(Status.Failed, value=None, msg=str(e), ...)`.
  - `_exit`: `if not self._broken: send(quit_cmd + "\n")`; then `await session._session._recover_session()` (noqa SLF001, matching repo style) to confirm the POSIX shell; `finally:` clear `_app_shell`.
  - `session.py` guard at the top of `run_cmd`:

```python
        if self._app_shell is not None:
            from .app_shell import AppShellActiveError

            raise AppShellActiveError(
                f"{type(self._app_shell).__name__} is attached to this session; "
                f"run() is unavailable until the app shell exits"
            )
```

- [ ] **Step 4:** Tests PASS; `uv run --no-sync pytest tests/unit/host -q` green. **Step 5:** Commit (`feat(host): AppShell REPL abstraction with prompt-regex cmd() and session locking`).

### Task 14: `BaseHost.app_shell()` + exports

**Files:**
- Modify: `src/otto/host/host.py` (new method near `open_session`), `src/otto/__init__.py`
- Test: `tests/unit/host/test_app_shell.py`

**Interfaces:**
- Produces: `host.app_shell(shell_cls, *, user=None)` async CM — opens a uniquely-named session, `switch_user(user or shell_cls.user)` when set, delegates to `shell_cls.attach`, always closes the owned session. Exports: `AppShell`, `Parsed`, `ShellResult`.

- [ ] **Step 1: Failing tests:** `host.app_shell(Fake)` yields an attached shell on a fresh named session and closes it after; `user="mysql"` (or `Fake.user`) triggers `switch_user` before launch (assert on a recording fake session).
- [ ] **Step 2:** Run — FAIL. **Step 3: Implement:**

```python
    @asynccontextmanager
    async def app_shell(
        self, shell_cls: "type[AppShellT]", *, user: str | None = None
    ) -> "AsyncIterator[AppShellT]":
        """Run *shell_cls* on a dedicated session; see the sessions cookbook."""
        name = f"__appshell_{shell_cls.__name__.lower()}_{uuid.uuid4().hex[:6]}__"
        session = await self.open_session(name)
        try:
            target = user if user is not None else shell_cls.user
            if target is not None:
                await session.switch_user(target)
            async with shell_cls.attach(session) as shell:
                yield shell
        finally:
            await session.close()
```

(`AppShellT = TypeVar("AppShellT", bound=AppShell)` via TYPE_CHECKING import to avoid an import cycle.) Add the three `_LAZY_EXPORTS` entries + `__all__`; import-budget tests must stay green.

- [ ] **Step 4:** Tests + `tests/unit/import_budget` PASS. **Step 5:** Commit (`feat(host): host.app_shell() context manager + public exports`).

### Task 15: AppShell + proxy e2e (python3 REPL, mysql)

**Files:**
- Create: `tests/e2e/host/test_app_shell_e2e.py`
- Test: same file

- [ ] **Step 1:** Write e2e (markers per neighbors; python3 case can run on the local/docker host — check `tests/e2e/host/` for the hostless-capable pattern):

```python
class PyRepl(AppShell):
    launch = "python3 -u -i"
    prompt = re.compile(r">>> \Z")
    quit_cmd = "exit()"


class Version(Parsed):
    pattern = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
    major: int
    minor: int


async def test_python_repl_roundtrip(host):
    async with host.app_shell(PyRepl) as py:
        r = await py.cmd("import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
                         parse=Version)
        assert r and r.value.major == 3
    # session is back to a working POSIX shell:
    assert (await host.run("echo back")).only.value.strip() == "back"


async def test_run_blocked_while_attached(host): ...
async def test_mysql_appshell_full_story(unix_bed_host): ...
    # user='mysql' proxied cred (Task 10 lab data) + a MySql AppShell:
    # CREATE TABLE + INSERT via cmd(), then SELECT with the spec §9 nested
    # Select model; assert stats.count and row contents. mysql needs
    # `mysql --pager=cat otto_test` as launch and prompt r"mysql> \Z".
```

- [ ] **Step 2:** Run the python3 cases locally (`-v`, single pass); run the mysql case against the leased bed. All PASS — investigate any prompt-strip surprises (readline echo differs between apps; fix the strip helper, not the test).
- [ ] **Step 3:** `make coverage-hostless` green. **Step 4:** Commit (`test(e2e): AppShell python3 + mysql full-story e2e`).

### Task 16: Docs + examples

**Files:**
- Modify: `docs/cookbook/sessions.md` (manual REPL pattern § → AppShell), the extending guide (`docs/guide/extending-backends.md` — add a "Login proxies" section mirroring the transfer-backend section), the host-database guide (`rg -l "creds" docs/guide` — rewrite the creds section for list-creds incl. proxy fields and the breaking-change note), `src/otto/examples/` (new `login_proxy.py` + `app_shell.py` samples with executable doctests, following `examples/options.py` style)
- Test: `make docs`

- [ ] **Step 1:** Write the docs/examples. Landmines: close every fence on its own line; no `| tail` on gate commands (masks exit codes); if doctests import new helpers, update `doctest_global_setup` in `docs/conf.py`; Sphinx runs `-W` nitpicky — every xref must resolve (`:class:`otto.AppShell`` etc. need the lazy exports documented — mirror how `options` is documented).
- [ ] **Step 2:** `make docs` — 0 warnings. **Step 3:** Commit (`docs: AppShell cookbook, login-proxy extending guide, list-creds host-database guide`).

### Task 17: Full gates + review

- [ ] **Step 1:** `make coverage-hostless` (full suite green; coverage ≥ baseline from Task 0).
- [ ] **Step 2:** `uv run --no-sync nox -s lint` then `nox -s typecheck` — fix idiomatically (re-run `ruff check .` after any `ruff format`).
- [ ] **Step 3:** `make docs` — 0 warnings.
- [ ] **Step 4:** Re-run BOTH e2e modules against the live bed once more (single pass).
- [ ] **Step 5:** Commit fixups (`chore: gate fixes`), then request code review per superpowers:requesting-code-review. Do NOT merge to main — Chris manages main-branch commits.

## Self-review notes (spec → plan coverage)

- Spec §3 creds/CredSpec/validators → Tasks 3-5; §4 registry/callable/su/undo → Task 1; §5.1 establishment + ownership rule → Tasks 4, 7, 8; §5.2 switch_user/as_user → Tasks 2, 6; §5.3 interact → Task 9; §6 failure semantics → Tasks 1, 7 (teardown tests); §7-§8 AppShell entry/locking → Tasks 13-14; §9 cmd/Parsed/ShellResult + nested → Tasks 11-13; §10 raise-vs-fail → Task 13; §11 exports/modules → Tasks 10, 14; §12 migration → Task 5 (Vagrantfile/live-bed already done on main, 2026-07-04); §13 docs → Task 16; §14 testing → Tasks 1-15 unit/e2e, mysql bed in Tasks 10, 15.
- Deliberately deferred per spec §15: CLI exposure of AppShell verbs, declarative steps, `--as-user` completion.
