# Per-session current-user tracking & elevation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every shell session know which user it is currently running as, and let any session — default or named — be elevated, so a later feature can decide `sudo`-or-not without probing the target.

**Architecture:** `current_user` state lives on the `ShellSession` (the shell is the real owner; a rebuilt shell naturally resets to the login user). `SessionManager` seeds it from the host's login creds and exposes a read accessor plus a private bookkeeping setter. The `su` mechanics are factored into one shared `_perform_su` helper used by both `PosixPrivilege.switch_user`/`as_user` (default session) and new `HostSession.switch_user`/`as_user` (named session). The "handle carries the verbs": the host is the handle for the default session, a `HostSession` is the handle for a named one — there are **no `session=` selector arguments**.

**Tech Stack:** Python 3.10+, asyncio, dataclasses (`slots=True` hosts), pytest / pytest-asyncio, `unittest.mock`.

**Spec:** [docs/superpowers/specs/2026-06-27-per-session-user-and-elevation-design.md](../specs/2026-06-27-per-session-user-and-elevation-design.md)

## Global Constraints

- **Additive only.** No existing public signature changes. Every new parameter defaults to `None`/`''` so all existing call sites behave identically.
- **Stage only — do NOT `git commit`.** otto's `prepare-commit-msg` hook needs a TTY and mis-attributes agent commits. Each task ends with `git add <paths>`; Chris commits the staged work. A paste-able message is given per task.
- **Coverage floor: 92%.** `make coverage` must stay green (the floor was raised 90→92 in `6ddcddd`). Add a test for every new branch.
- **Type checking:** otto uses `ty`. For cross-MRO attribute access on a mixin/base that does not declare the attribute (`self.send`, `self.expect`, `self._session_mgr`), follow the **existing** `# ty: ignore[unresolved-attribute]` pattern already used throughout `privilege.py`.
- **No new `from __future__ import annotations`.** Do not add it to any file. The three edited source files (`session.py`, `host.py`, `privilege.py`) already have it — leave it as-is. Create no new source modules (all changes are edits to existing files).
- **Tests are unit-tier**, mirroring existing fixtures: `MockSession(ShellSession)` in `tests/unit/host/test_session.py`, and the real-host + `host._session_mgr = AsyncMock()` pattern in `tests/unit/host/test_privilege.py`.
- **Do not modify `embedded_host.py`.** Embedded must NOT forward a `user_password` resolver (so its named sessions correctly cannot elevate).

## File Structure

- `src/otto/host/session.py` — `ShellSession` gains the `current_user` attribute; `SessionManager` gains the `user_password` ctor param, `_login_user`/`_seed_user`/`current_user`/`_set_current_user`, and seeds at both build choke points; `HostSession` gains `current_user`/`switch_user`/`as_user` and a `user_password` field.
- `src/otto/host/privilege.py` — new module-level `_perform_su` helper; `switch_user`/`as_user` refactored to use it and record `current_user`.
- `src/otto/host/host.py` — `BaseHost` gains a read-only `current_user` property.
- `src/otto/host/unix_host.py`, `local_host.py`, `docker_host.py` — forward `user_password=self._user_password` into their `SessionManager(...)` construction(s).
- `tests/unit/host/test_session.py`, `tests/unit/host/test_privilege.py`, `tests/unit/host/test_unix_host.py` — new and updated tests.

---

### Task 1: `current_user` state on `ShellSession`

**Files:**
- Modify: `src/otto/host/session.py` (`ShellSession.__init__`, ends at line 129)
- Test: `tests/unit/host/test_session.py`

**Interfaces:**
- Produces: `ShellSession.current_user: str` — public read attribute, defaults to `''`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/host/test_session.py` (the `MockSession` class and imports already exist at the top):

```python
def test_shell_session_current_user_defaults_empty():
    """A freshly constructed shell session has no tracked user yet."""
    s = MockSession()
    assert s.current_user == ''
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_session.py::test_shell_session_current_user_defaults_empty -v`
Expected: FAIL — `AttributeError: 'MockSession' object has no attribute 'current_user'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/session.py`, in `ShellSession.__init__`, immediately after the existing last line `self._write_progress = None` (line 129):

```python
        # The OS user this shell is currently running as. Seeded by
        # SessionManager from the host's login user; mutated only by the
        # elevation flow (switch_user/as_user). '' on loginless shells.
        self.current_user: str = ''
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/host/test_session.py::test_shell_session_current_user_defaults_empty -v`
Expected: PASS

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/session.py tests/unit/host/test_session.py
```
Paste-able message: `feat(session): track current_user on ShellSession`

---

### Task 2: `SessionManager` user accessors + seeding

**Files:**
- Modify: `src/otto/host/session.py` (`SessionManager.__init__` line 955; add methods after `has_live_sessions` ~line 1016; seed in `_ensure_session` ~line 1065 and `open_session` ~line 1312)
- Test: `tests/unit/host/test_session.py`

**Interfaces:**
- Consumes: `ShellSession.current_user` (Task 1).
- Produces:
  - `SessionManager.__init__(..., user_password: 'Callable[[str], str | None] | None' = None)` → stored as `self._user_password`.
  - `SessionManager.current_user -> str` (property; default session's user, else login user).
  - `SessionManager._set_current_user(user: str) -> None` (private bookkeeping).
  - `SessionManager._login_user() -> str` and `SessionManager._seed_user(session) -> None` (internal helpers).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_session.py`. Update the top mock import to include `MagicMock`:
```python
from unittest.mock import AsyncMock, MagicMock, patch
```
Then add:
```python
def test_session_manager_current_user_falls_back_to_login():
    from otto.host.session import SessionManager
    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    assert mgr.current_user == "alice"  # no default session built yet


def test_session_manager_current_user_empty_without_connections():
    from otto.host.session import SessionManager
    mgr = SessionManager(name="local")  # connections=None (e.g. LocalHost)
    assert mgr.current_user == ""


def test_session_manager_seed_user_stamps_login_user():
    from otto.host.session import SessionManager
    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._seed_user(s)
    assert s.current_user == "alice"


def test_session_manager_set_current_user_updates_default_session():
    from otto.host.session import SessionManager
    conn = MagicMock()
    conn.credentials = ("alice", "pw")
    mgr = SessionManager(connections=conn, name="h")
    s = MockSession()
    mgr._session = s
    mgr._set_current_user("root")
    assert s.current_user == "root"
    assert mgr.current_user == "root"


def test_session_manager_accepts_user_password_arg():
    from otto.host.session import SessionManager
    mgr = SessionManager(name="h", user_password=lambda u: "pw")
    assert mgr._user_password("anyone") == "pw"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_session.py -k "session_manager_current_user or seed_user or set_current_user or user_password_arg" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'user_password'` / `AttributeError: ... 'current_user'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/session.py`, add the `user_password` parameter to `SessionManager.__init__`. Change the signature's tail (after `retry_backoff: float | None = None,`) to include:
```python
        retry_backoff: float | None = None,
        user_password: 'Callable[[str], str | None] | None' = None,
    ) -> None:
```
and store it alongside the other assignments (e.g. right after `self._oneshot_factory = oneshot_factory`):
```python
        # Resolver for su-target passwords, forwarded to HostSessions so named
        # sessions can elevate. None on non-posix hosts → named-session
        # elevation is unsupported there.
        self._user_password = user_password
```

Add these methods to `SessionManager` (e.g. immediately after the `has_live_sessions` property, ~line 1016):
```python
    def _login_user(self) -> str:
        """The host's login username, or '' when loginless / no creds."""
        if self._connections is None:
            return ''
        return self._connections.credentials[0]

    def _seed_user(self, session: 'ShellSession') -> None:
        """Stamp a freshly built session with the login user."""
        session.current_user = self._login_user()

    @property
    def current_user(self) -> str:
        """User the default session is currently running as.

        Seeded from the login user; changed only via switch_user/as_user.
        Falls back to the login user before the default session is built.
        """
        if self._session is not None:
            return self._session.current_user
        return self._login_user()

    def _set_current_user(self, user: str) -> None:
        """Private bookkeeping for the default session. Called only by the
        elevation flow (PosixPrivilege.switch_user/as_user) after a real
        ``su`` has run — never a public API (that would let callers desync
        the tracked user from the shell's actual user)."""
        if self._session is not None:
            self._session.current_user = user
```

Wire seeding into `_ensure_session`: after the line `new_session = await self._build_session()` (line 1065), add:
```python
                self._seed_user(new_session)
```
Wire seeding into `open_session`: after the line `shell_session._on_output = self._log_output` (line 1312), add:
```python
            self._seed_user(shell_session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_session.py -k "session_manager_current_user or seed_user or set_current_user or user_password_arg" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/session.py tests/unit/host/test_session.py
```
Paste-able message: `feat(session): SessionManager current_user accessors + seeding`

---

### Task 3: `_perform_su` helper + bookkeeping in `PosixPrivilege`

**Files:**
- Modify: `src/otto/host/privilege.py` (imports; add `_perform_su`; refactor `switch_user` lines 53-66 and `as_user` lines 68-81)
- Test: `tests/unit/host/test_privilege.py`

**Interfaces:**
- Consumes: `SessionManager._set_current_user`, `SessionManager.current_user` (Task 2).
- Produces: `otto.host.privilege._perform_su(send, expect, user, password, user_password) -> str` — shared `su` exchange used here and by `HostSession` (Task 4).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/host/test_privilege.py`, add a shared helper near the top (after the imports) and new tests. The file already imports `AsyncMock, MagicMock, patch`.

```python
def _mock_session_mgr():
    """AsyncMock session-mgr whose send/expect are awaitable but whose
    current_user bookkeeping is synchronous (no un-awaited coroutines)."""
    mgr = AsyncMock()
    mgr._set_current_user = MagicMock()
    mgr.current_user = ""
    return mgr


@pytest.mark.asyncio
async def test_perform_su_builds_command_and_returns_target():
    from otto.host.privilege import _perform_su
    sent = []

    async def send(text, log=True):
        sent.append((text, log))

    async def expect(pat, timeout=10.0):
        return "Password:"

    target = await _perform_su(send, expect, "root", None, lambda u: "rootpw")
    assert target == "root"
    assert ("su root\n", True) in sent
    assert ("rootpw\n", False) in sent


@pytest.mark.asyncio
async def test_perform_su_no_user_means_root_no_quote():
    from otto.host.privilege import _perform_su
    sent = []

    async def send(text, log=True):
        sent.append(text)

    async def expect(pat, timeout=10.0):
        return "Password:"

    target = await _perform_su(send, expect, "", None, lambda u: None)
    assert target == "root"
    assert "su\n" in sent  # bare `su`, no username, no password sent


@pytest.mark.asyncio
async def test_switch_user_records_current_user():
    from otto.host.unix_host import UnixHost
    host = UnixHost(ip="10.0.0.1", element="box",
                    creds={"admin": "secret", "root": "rootpw"}, user="admin", log=False)
    host._session_mgr = _mock_session_mgr()
    await host.switch_user("root")
    host._session_mgr._set_current_user.assert_called_once_with("root")


@pytest.mark.asyncio
async def test_as_user_restores_previous_user():
    from otto.host.unix_host import UnixHost
    host = UnixHost(ip="10.0.0.1", element="box",
                    creds={"admin": "secret", "root": "rootpw"}, user="admin", log=False)
    mgr = _mock_session_mgr()
    mgr.current_user = "admin"
    host._session_mgr = mgr
    async with host.as_user("root"):
        pass
    calls = [c.args[0] for c in mgr._set_current_user.call_args_list]
    assert calls == ["root", "admin"]  # entered as root, restored to admin
```

Then update the **four existing** tests that assign `host._session_mgr = AsyncMock()` to use the helper instead, so the new sync bookkeeping does not raise un-awaited-coroutine warnings. In each of `test_switch_user_sends_su_and_password`, `test_switch_user_default_is_root_no_user_arg`, `test_as_user_switches_then_exits`, and `test_switch_user_quotes_special_char_username`, replace:
```python
    host._session_mgr = AsyncMock()
```
with:
```python
    host._session_mgr = _mock_session_mgr()
```
(In `test_switch_user_default_is_root_no_user_arg`, keep the existing `host._session_mgr.expect.return_value = "Password:"` line right after.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_privilege.py -k "perform_su or records_current_user or restores_previous" -v`
Expected: FAIL — `ImportError: cannot import name '_perform_su'` / `AssertionError`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/privilege.py`, extend the imports. After `from contextlib import asynccontextmanager` add:
```python
from collections.abc import Awaitable, Callable
```

Add the module-level helper (after the `_SUDO_PROMPT` constant, before `class PosixPrivilege`):
```python
async def _perform_su(
    send: 'Callable[..., Awaitable[None]]',
    expect: 'Callable[..., Awaitable[str]]',
    user: str,
    password: str | None,
    user_password: 'Callable[[str], str | None]',
) -> str:
    """Run the ``su`` exchange against a session's ``send``/``expect`` and
    return the resolved target user. Does **no** ``current_user`` bookkeeping —
    the caller stamps the session it elevated. Shared by
    :meth:`PosixPrivilege.switch_user` (default session) and
    :meth:`~otto.host.session.HostSession.switch_user` (named session)."""
    target = user or "root"
    cmd = "su" if not user else f"su {shlex.quote(user)}"
    pw = password if password is not None else user_password(target)
    await send(cmd + "\n")
    if pw is not None:
        await expect(r"[Pp]assword:")
        await send(pw + "\n", log=False)
    return target
```

Replace the body of `switch_user` (lines 53-66) with:
```python
    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """``su`` the persistent (default) session to *user* (default root).

        Performs the real ``su`` and then records the new user so
        :attr:`current_user` reflects it. Mutates session state — affects
        subsequent ``run`` calls until the user exits back.
        """
        target = await _perform_su(
            self.send, self.expect, user, password, self._user_password  # ty: ignore[unresolved-attribute]
        )
        self._session_mgr._set_current_user(target)  # ty: ignore[unresolved-attribute]
```

Replace the body of `as_user` (lines 68-81) with:
```python
    @asynccontextmanager
    async def as_user(
        self, user: str = "root", password: str | None = None
    ) -> AsyncIterator["PosixPrivilege"]:
        """Run a block as *user*, returning to the original user on exit.

            async with host.as_user("root"):
                await host.run("systemctl restart foo")

        Tracks :attr:`current_user` across the switch and restores the prior
        user when the block exits.
        """
        prev = self._session_mgr.current_user  # ty: ignore[unresolved-attribute]
        await self.switch_user(user, password)
        try:
            yield self
        finally:
            await self.send("exit\n")  # ty: ignore[unresolved-attribute]
            self._session_mgr._set_current_user(prev)  # ty: ignore[unresolved-attribute]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_privilege.py -v`
Expected: PASS (all tests in the file, including the 4 updated ones and the new ones)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/privilege.py tests/unit/host/test_privilege.py
```
Paste-able message: `refactor(privilege): factor _perform_su, record current_user on switch/as_user`

---

### Task 4: `HostSession` elevation + `current_user`

**Files:**
- Modify: `src/otto/host/session.py` (imports; `HostSession.__init__` line 854; add methods to `HostSession`; forward `user_password` in `open_session` HostSession construction line 1327)
- Test: `tests/unit/host/test_session.py`

**Interfaces:**
- Consumes: `_perform_su` (Task 3), `ShellSession.current_user` (Task 1), `SessionManager._user_password` (Task 2).
- Produces:
  - `HostSession.__init__(..., user_password: 'Callable[[str], str | None] | None' = None)`.
  - `HostSession.current_user -> str` (property).
  - `HostSession.switch_user(user='', password=None) -> None`.
  - `HostSession.as_user(user='root', password=None)` (async context manager).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_session.py`. `ShellSession` is already imported; ensure `AsyncMock` is imported (it is).

```python
@pytest.mark.asyncio
async def test_host_session_current_user_delegates_to_shell():
    from otto.host.session import HostSession
    shell = MockSession()
    shell.current_user = "alice"
    hs = HostSession("n", shell, lambda _: None, lambda _: None, lambda _: None)
    assert hs.current_user == "alice"


@pytest.mark.asyncio
async def test_host_session_switch_user_without_resolver_raises():
    from otto.host.session import HostSession
    shell = MockSession()
    hs = HostSession("n", shell, lambda _: None, lambda _: None, lambda _: None)
    with pytest.raises(NotImplementedError):
        await hs.switch_user("root")


@pytest.mark.asyncio
async def test_host_session_switch_user_elevates_and_stamps():
    from otto.host.session import HostSession
    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession("n", shell, lambda _: None, lambda _: None, lambda _: None,
                     user_password=lambda u: "rootpw")
    await hs.switch_user("root")
    assert shell.current_user == "root"
    sent = [c.args[0] for c in shell.send.await_args_list]
    assert "su root\n" in sent and "rootpw\n" in sent


@pytest.mark.asyncio
async def test_host_session_as_user_restores_previous():
    from otto.host.session import HostSession
    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"
    hs = HostSession("n", shell, lambda _: None, lambda _: None, lambda _: None,
                     user_password=lambda u: "rootpw")
    async with hs.as_user("root"):
        assert shell.current_user == "root"
    assert shell.current_user == "alice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_session.py -k "host_session_current_user or host_session_switch or host_session_as_user" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'user_password'` / `AttributeError: ... 'current_user'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/session.py`, extend imports. Add to the `collections.abc` import on line 21 so it reads:
```python
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
```
and add (near the other top-level imports):
```python
from contextlib import asynccontextmanager
```

Add the `user_password` parameter to `HostSession.__init__` (line 854). Change the signature tail (after `deregister: Callable[[str], None],`) to:
```python
        deregister: Callable[[str], None],
        user_password: 'Callable[[str], str | None] | None' = None,
    ) -> None:
```
and store it after `self._deregister = deregister`:
```python
        # Resolver for su-target passwords (creds-based). None on non-posix
        # hosts → this session cannot elevate. Set by SessionManager.
        self._user_password = user_password
```

Add these members to `HostSession` (e.g. after the `alive` property, before `run`):
```python
    @property
    def current_user(self) -> str:
        """User this named session is currently running as.

        Seeded from the host's login user; changed only via
        :meth:`switch_user` / :meth:`as_user`."""
        return self._session.current_user

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """``su`` *this* session to *user* (default root), tracking
        :attr:`current_user`. Posix-only — raises ``NotImplementedError`` on
        hosts whose sessions do not support elevation (no password resolver)."""
        if self._user_password is None:
            raise NotImplementedError(
                "switch_user is not supported on this host's sessions"
            )
        from .privilege import _perform_su
        target = await _perform_su(
            self.send, self.expect, user, password, self._user_password
        )
        self._session.current_user = target

    @asynccontextmanager
    async def as_user(
        self, user: str = "root", password: str | None = None
    ) -> 'AsyncIterator[HostSession]':
        """Run a block as *user* on this session, restoring the prior user on
        exit."""
        prev = self.current_user
        await self.switch_user(user, password)
        try:
            yield self
        finally:
            await self.send("exit\n")
            self._session.current_user = prev
```

Forward the resolver when `open_session` builds a `HostSession` (line 1327) — add `user_password=self._user_password,` to the constructor call:
```python
            host_session = HostSession(
                name=name,
                session=shell_session,
                log_command=self._log_command,
                log_output=self._log_output,
                deregister=lambda n: (self._named_sessions.pop(n, None), None)[1],
                user_password=self._user_password,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_session.py -k "host_session_current_user or host_session_switch or host_session_as_user" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/session.py tests/unit/host/test_session.py
```
Paste-able message: `feat(session): HostSession current_user + per-session elevation`

---

### Task 5: Host-level `current_user` + wire resolver from posix hosts

**Files:**
- Modify: `src/otto/host/host.py` (`BaseHost`, add property near switch_user/as_user stubs ~line 340)
- Modify: `src/otto/host/unix_host.py` (`__post_init__` SessionManager ~line 318; `rebuild_connections` SessionManager ~line 344)
- Modify: `src/otto/host/local_host.py` (`__post_init__` SessionManager ~line 105)
- Modify: `src/otto/host/docker_host.py` (`_build_session_mgr` SessionManager ~line 154)
- Test: `tests/unit/host/test_unix_host.py`, `tests/unit/host/test_privilege.py`

**Interfaces:**
- Consumes: `SessionManager.current_user` (Task 2), `SessionManager._user_password` param (Task 2), `PosixPrivilege._user_password` (existing).
- Produces: `BaseHost.current_user -> str` (read-only property, inherited by all hosts).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_unix_host.py` (check the file's existing imports; add `AsyncMock, MagicMock` from `unittest.mock` and `from otto.host.session import ShellSession` if not already present):
```python
@pytest.mark.asyncio
async def test_host_current_user_reads_default_session():
    from unittest.mock import MagicMock
    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost
    host = UnixHost(ip="10.0.0.1", element="box", creds={"admin": "secret"},
                    user="admin", log=False)
    transport = MagicMock(spec=ShellSession)
    transport.current_user = "admin"
    host._session_mgr._session = transport
    assert host.current_user == "admin"


@pytest.mark.asyncio
async def test_unix_switch_user_updates_host_current_user():
    from unittest.mock import AsyncMock, MagicMock
    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost
    host = UnixHost(ip="10.0.0.1", element="box",
                    creds={"admin": "secret", "root": "rootpw"}, user="admin", log=False)
    transport = MagicMock(spec=ShellSession)
    transport.alive = True
    transport.send = AsyncMock()
    transport.expect = AsyncMock(return_value="Password:")
    transport.current_user = "admin"
    host._session_mgr._session = transport
    await host.switch_user("root")
    assert host.current_user == "root"
```

Add to `tests/unit/host/test_privilege.py`:
```python
@pytest.mark.asyncio
async def test_embedded_current_user_is_empty_loginless():
    from otto.host.embedded_host import ZephyrHost
    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=False)
    assert host.current_user == ""  # loginless embedded shell
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_unix_host.py -k "host_current_user or switch_user_updates_host" tests/unit/host/test_privilege.py::test_embedded_current_user_is_empty_loginless -v`
Expected: FAIL — `AttributeError: 'UnixHost' object has no attribute 'current_user'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/host.py`, add to `BaseHost` (next to the `switch_user`/`as_user` stubs, ~line 340):
```python
    @property
    def current_user(self) -> str:
        """User this host's default shell session is currently running as.

        Seeded from the login user; changes only through :meth:`switch_user` /
        :meth:`as_user`. See :attr:`~otto.host.session.HostSession.current_user`
        for named sessions."""
        return self._session_mgr.current_user  # ty: ignore[unresolved-attribute]
```

In `src/otto/host/unix_host.py`, add `user_password=self._user_password,` to **both** `SessionManager(...)` calls — in `__post_init__` (~line 318) and in `rebuild_connections` (~line 344). Each call gains one line, e.g.:
```python
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            user_password=self._user_password,
        )
```

In `src/otto/host/local_host.py`, add `user_password=self._user_password,` to the `SessionManager(...)` call in `__post_init__` (~line 105):
```python
        self._session_mgr = SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=LocalSession,
            oneshot_factory=self._exec_subprocess,
            user_password=self._user_password,
        )
```

In `src/otto/host/docker_host.py`, add `user_password=self._user_password,` to the `SessionManager(...)` returned by `_build_session_mgr` (~line 154):
```python
        return SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=_make_session,
            oneshot_factory=self._oneshot_via_parent,
            user_password=self._user_password,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_unix_host.py -k "host_current_user or switch_user_updates_host" tests/unit/host/test_privilege.py::test_embedded_current_user_is_empty_loginless -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/host.py src/otto/host/unix_host.py src/otto/host/local_host.py src/otto/host/docker_host.py tests/unit/host/test_unix_host.py tests/unit/host/test_privilege.py
```
Paste-able message: `feat(host): current_user property + forward su-password resolver to sessions`

---

### Task 6: Full-gate verification

**Files:** none (verification only).

- [ ] **Step 1: Run the host unit suite**

Run: `uv run pytest tests/unit/host/test_session.py tests/unit/host/test_privilege.py tests/unit/host/test_unix_host.py tests/unit/host/test_local_host.py tests/unit/host/test_docker_host.py tests/unit/host/test_embedded_host.py -v`
Expected: PASS — no regressions across the touched hosts.

- [ ] **Step 2: Type check**

Run: `make typecheck`
Expected: clean (the only new cross-MRO accesses carry `# ty: ignore[unresolved-attribute]`, matching the existing pattern).

- [ ] **Step 3: Coverage gate**

Run: `make coverage`
Expected: PASS with total coverage ≥ 92%. If any new branch is uncovered, add a targeted test (e.g. the `_set_current_user` no-session guard, or `_login_user` with `connections=None`) and re-run.

- [ ] **Step 4: Docs gate**

Run: `make docs`
Expected: 0 warnings (the new docstrings reference real symbols; `_perform_su`/`_set_current_user` are private and not in the API docs).

- [ ] **Step 5: Stage any coverage top-up tests**

```bash
git add tests/unit/host/
```
Paste-able message: `test(host): coverage top-up for per-session current_user`

> `make nox` (5 Pythons, live beds) is the heavy full gate — leave it to Chris on the host per the dev-VM load policy; do not run it as part of this plan.

---

## Self-Review

**1. Spec coverage** — every spec §4 component maps to a task:
- §4.1 `current_user` on `ShellSession` + seeding → Task 1 (attribute) + Task 2 (seeding at both choke points, reset-on-rebuild is automatic via fresh object).
- §4.2 `SessionManager` accessors + `user_password` forward param → Task 2 (`current_user`, `_set_current_user`, `_login_user`, `_seed_user`, ctor param) + Task 4 (forward into `open_session`) + Task 5 (hosts supply it).
- §4.3 `HostSession.current_user` → Task 4.
- §4.4 elevation: shared `_perform_su`, host + `HostSession` paths, non-posix gate → Task 3 (helper + host path) + Task 4 (`HostSession` path + gate).
- §4.5 private setter → Task 2 (`_set_current_user` private; public surface = switch_user/as_user + current_user).
- §5 "what does not change" → enforced by Global Constraints (additive-only) and verified in Task 6.
- §6 correctness/concurrency (nesting, rebuild reset, per-session independence) → Task 3 (`as_user` restore + nesting via `prev`), Task 4 (named independence test), Task 2 (rebuild reset is structural). The "manual raw send is untracked" limitation is documented in the spec, intentionally not defended.
- §7 testing → Tasks 1-5 each ship their tests; Task 6 runs the gate.

**2. Placeholder scan** — no TBD/TODO; every code step shows full code; every test shows full assertions; exact run commands with expected output.

**3. Type consistency** — `current_user` (str) used identically on `ShellSession`, `SessionManager`, `HostSession`, `BaseHost`. `_set_current_user(user: str)` private, single signature. `_perform_su(send, expect, user, password, user_password) -> str` — same call shape in Task 3 (host) and Task 4 (`HostSession`). `user_password: 'Callable[[str], str | None] | None'` consistent across `SessionManager.__init__` and `HostSession.__init__`. No name drift.
