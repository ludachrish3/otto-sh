# Per-session current-user tracking & per-session elevation — design

> Captured 2026-06-27. **Spec A** of a two-spec workstream. This is the
> *foundation*; **Spec B** (kernel-module `load`/`unload`/`lsmod` on `UnixHost`)
> is deferred and will *consume* the per-session current-user state designed
> here for its `sudo`-or-not decision. No code has changed yet.

---

## 1. Context & motivation

A later feature (Spec B) needs to decide whether a privileged command
(`insmod`/`rmmod`) must be prefixed with `sudo`. The correct, probe-free answer
is "only when the shell isn't already running as root." That requires otto to
**know which user a shell session is currently running as** — including after a
`switch_user`/`as_user` elevation.

Today otto does **not** track this:

- The login user is known *per host* (`ConnectionManager.credentials` → one
  `(user, password)` tuple, shared by every session), but otto stores no notion
  of the *current* user once a shell `su`'s to someone else.
- `PosixPrivilege.switch_user` ([src/otto/host/privilege.py](../../../src/otto/host/privilege.py))
  is fire-and-forget: it sends `su` and records nothing.
- `switch_user`/`as_user` only ever reach the **default** session — there is no
  way to elevate a *named* session (`HostSession` has no elevation methods).

What already exists and shapes the design:

- `HostSession` ([src/otto/host/session.py](../../../src/otto/host/session.py),
  the object returned by `await host.open_session(name)`) **already** exposes
  `run`/`send`/`expect`. Named sessions are therefore already independently
  *commandable* — `await mon.run(cmd)` works today.
- `HostSession` is the concurrency primitive: the default session is
  sequential-only, so concurrent work means opening N named `HostSession`s and
  `gather`-ing their `.run(...)` calls.
- File transfers (`put`/`get`) go through a **separate transport**
  (`self._file_transfer`: scp/sftp/ftp/nc) that authenticates straight from
  creds. They do **not** use a shell session, so "current user" / session
  selection has no meaning for them.

The guiding principle that falls out of this: **the session handle carries the
verbs.** The host *is* the handle for the default session; a `HostSession` *is*
the handle for a named session. Each handle exposes the same command verbs.
There are **no `session=` selector arguments** anywhere — they would buy nothing
(`HostSession` already covers named-session commands) while churning the
framework-wide `run()` signature.

## 2. Goals / non-goals

**Goals**

- Every shell session knows the user it is currently running as
  (`current_user`), seeded from the host's login user.
- That value stays correct across `switch_user`/`as_user` elevation **and**
  across a session rebuild (a recovered shell is a fresh login).
- Any session — default *or* named — can be elevated, via the same verbs on its
  handle.
- A read-only public API to query the current user on either handle.
- Existing behaviour and every existing call site are **unchanged** (all new
  surface is additive; no existing signature changes).

**Non-goals (explicit)**

- **No `session=` selector** on `run`/`send`/`expect`/`switch_user`/`as_user`.
  Named-session work uses the `HostSession` handle.
- **No file-transfer changes.** `put`/`get` keep their own transport; current
  user does not apply to them.
- **No kernel-module work** — that is Spec B, written separately, consuming this.
- No attempt to observe a `su`/`exit` a developer types *raw* into a session
  (e.g. `await host.send("su\n")`) — that bypasses the tracked API and is a
  documented limitation, not a goal.
- No change to where passwords come from (`_user_password`/creds stay the
  source).

## 3. The symmetric model (handle carries the verbs)

| Verb | Default session (handle = the host) | Named session (handle = `HostSession`) |
|------|-------------------------------------|----------------------------------------|
| run / send / expect | `host.run(...)` *(exists)* | `mon.run(...)` *(exists)* |
| switch_user / as_user | `host.switch_user(...)` *(exists; gains bookkeeping)* | `mon.switch_user(...)` **(new)** |
| current_user (read) | `host.current_user` **(new)** | `mon.current_user` **(new)** |

`mon = await host.open_session("monitor")`. No selectors; the handle you hold
determines the session you act on.

## 4. Components

### 4.1 `current_user` on `ShellSession`

The shell is the true owner of "who am I running as," so the state lives on the
abstract `ShellSession`. It is a **read-only public attribute** for consumers;
the *only* writers are the elevation methods (§4.4).

- Default value `''` (set in `ShellSession.__init__`), so it always exists.
- **Seeded** to the login user (`ConnectionManager.credentials[0]`) by
  `SessionManager` immediately after a session is built — at the two build
  choke points (the default-session build path and `open_session`). This
  covers the custom-factory sessions (`LocalSession`, `_DockerSshSession`)
  because they are constructed through those same choke points.
- **Reset-on-rebuild is automatic and correct**: a recovered/rebuilt shell is a
  brand-new `ShellSession` object, re-seeded to the login user — matching the
  fact that the old shell's `su` state died with it. (A `SessionManager`-level
  field would have wrongly survived the rebuild; `ShellSession` is the right
  home precisely for this reason.)
- On a loginless/embedded shell, creds are `('', '')`, so `current_user` is `''`
  — harmless, since such hosts never elevate.

### 4.2 `SessionManager` accessors

- `current_user` (read): returns the **default** session's
  `ShellSession.current_user`, falling back to the login user
  (`credentials[0]`) when the default session is not yet built.
- `_set_current_user(user)` (**private**): writes the default session's
  `current_user`. Internal bookkeeping, called *only* by the host's
  `switch_user`/`as_user`. **Not** part of the public API — see §4.5.
- Seeding helper invoked at both build choke points (§4.1).

The **host** exposes a read-only `current_user` property (default-session
handle) that simply delegates to `SessionManager.current_user`.

`SessionManager` also gains an optional `user_password: Callable[[str], str | None] | None`
constructor argument (default `None`), which it **forwards** to every
`HostSession` it builds. Posix hosts pass their `self._user_password`; non-posix
hosts pass nothing. This is what makes named-session elevation possible while
keeping password sourcing creds-based (§4.4).

### 4.3 `HostSession` read API

- `current_user` (read-only property) → delegates to its wrapped
  `ShellSession.current_user`. So `mon.current_user` works.

### 4.4 Elevation: shared mechanics, two handles

The `su` mechanics (build the command, answer the password prompt, return the
resolved target user) are factored into **one shared helper** so the host and
`HostSession` paths cannot drift:

```python
# performs the su exchange against a given session's send/expect; returns the
# resolved target user. Does NOT do current_user bookkeeping — the caller does,
# on the session it just elevated.
async def _perform_su(send, expect, user, password, user_password) -> str:
    target = user or "root"
    cmd = "su" if not user else f"su {shlex.quote(user)}"
    pw = password if password is not None else user_password(target)
    await send(cmd + "\n")
    if pw is not None:
        await expect(r"[Pp]assword:")
        await send(pw + "\n", log=False)
    return target
```

**Host (default session)** — `PosixPrivilege.switch_user` keeps its current
signature and its routing to the default session (`self.send`/`self.expect`),
and adds the bookkeeping:

```python
async def switch_user(self, user="", password=None):
    target = await _perform_su(self.send, self.expect, user, password, self._user_password)
    self._session_mgr._set_current_user(target)   # default-session bookkeeping
```

`as_user` captures the prior user, switches, and on exit sends `exit` **and
restores** the tracked user:

```python
@asynccontextmanager
async def as_user(self, user="root", password=None):
    prev = self.current_user
    await self.switch_user(user, password)
    try:
        yield self
    finally:
        await self.send("exit\n")
        self._session_mgr._set_current_user(prev)
```

**Named session (`HostSession`)** — gains `switch_user`/`as_user` mirroring the
above, but routed to its own session and stamping its own `ShellSession`:

```python
async def switch_user(self, user="", password=None):
    if self._user_password is None:           # non-posix host → not supported
        raise NotImplementedError(
            f"switch_user is not supported on this host's sessions"
        )
    target = await _perform_su(self.send, self.expect, user, password, self._user_password)
    self._session.current_user = target
```

`HostSession.as_user` is the analogous context manager, restoring
`self._session.current_user` on exit.

The `if self._user_password is None` gate makes elevation a posix-only
capability without `HostSession` needing to know the host type: a host that
supplied no password resolver (i.e. a non-posix host) cannot elevate its named
sessions, mirroring how `BaseHost.switch_user` raises "not supported" for
non-posix hosts today.

### 4.5 Why the setter is private

`switch_user`/`as_user` are the **only** legitimate way to change the current
user, because they *perform the real `su`*. A public `set_current_user` would
let a caller desync otto's tracked user from the shell's actual user with no
`su` at all — a footgun. So the public surface is exactly:

- **mutating:** `switch_user()` / `as_user()` (real `su` + bookkeeping),
- **read-only:** `current_user`,

and `_set_current_user` is a private bookkeeping helper invoked only inside the
elevation flow.

## 5. What does NOT change

- `run`/`send`/`expect` signatures and behaviour — untouched, on every host.
- `put`/`get` and the file-transfer transport — untouched.
- `PosixPrivilege.switch_user`/`as_user` *signatures* — unchanged (no `session=`
  param). They gain only the internal bookkeeping line.
- `BaseHost.switch_user`/`as_user` non-posix stubs — unchanged.
- Every existing call site keeps working with no edits.

## 6. Correctness & concurrency notes

- **Manual elevation is untracked.** Calling `switch_user()`/`as_user()` keeps
  the tracked user correct. If a developer instead sends raw `su`/`exit` text
  through `send()`, otto cannot observe it and `current_user` goes stale. The
  `as_user` context manager is always correct for balanced elevation; raw `send`
  is caller-managed. Documented, not defended against.
- **Nesting** works: `as_user` captures the *immediately prior* user before each
  switch and restores it on exit, so nested `as_user` blocks unwind correctly.
- **Concurrency is safe**: each `ShellSession` owns its own `current_user`, and
  named sessions already serialize per-name via their own locks. The default
  session remains sequential-only (unchanged). Concurrent named sessions
  elevating independently each mutate only their own state.

## 7. Testing (unit tier)

Mirroring the existing `tests/unit/host/` patterns with mocked
`send`/`expect`/session plumbing:

- Seeding: a freshly built default session and a freshly opened named session
  both report `current_user == login_user`; a loginless host reports `''`.
- Rebuild resets: after a simulated session rebuild, `current_user` is back to
  the login user.
- Host elevation: `host.switch_user("root")` sends the expected `su`, answers
  the password from creds, and updates `host.current_user`; `as_user` restores
  the prior user on exit (including a nested case).
- Named elevation: `mon.switch_user("root")` updates `mon.current_user` and
  leaves the default session's `current_user` untouched (independence).
- Non-posix gate: a non-posix `HostSession` (no password resolver) raises on
  `switch_user`.
- No regressions: existing `run`/`send`/`expect`/`switch_user` call sites behave
  identically.

## 8. Spec B preview (out of scope here)

For reference only — *not* part of this spec. Spec B will add
`UnixHost.load(file, name=None)` / `unload(name)` / `lsmod()` (plus a
`@cli_exposed` retrofit on the embedded `load`/`unload`). Its `sudo`-or-not
decision will read this spec's state — `need_sudo = self.current_user != "root"`
— with no extra probe. Note the two transports stay independent: the `.ko`'s
`put` lands as the login/transfer user, while the `insmod` runs in the shell
session as `current_user`. Spec B gets its own brainstorm → spec → plan cycle
once this foundation lands.
