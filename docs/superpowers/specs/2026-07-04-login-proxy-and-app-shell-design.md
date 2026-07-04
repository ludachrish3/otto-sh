# Login proxy & AppShell — design

> Captured 2026-07-04. One spec, **two parts**: Part 1 (login proxy) is the
> priority and is implemented first; Part 2 (AppShell) builds on it. They meet
> in the motivating mysql story — proxy to the login-less `mysql` user, then
> drive the mysql REPL — but each part stands alone. No code has changed yet.

---

## 1. Context & motivation

Some hosts have users that **cannot log in directly** — the classic example is
a `mysql` service account with no login shell. Reaching it requires
authenticating as a different account and then taking host-specific steps
(`su`, `sudo -u`, entering a container, …) to *become* the desired user. otto
has no way to express this today:

- Credentials are a flat `creds: dict[str, str]` (username → password),
  declared identically in four layers
  ([models/host.py](../../../src/otto/models/host.py) `HostSpec.creds` /
  `UnixHostSpec.creds`,
  [unix_host.py](../../../src/otto/host/unix_host.py) `UnixHost.creds`,
  [remote_host.py](../../../src/otto/host/remote_host.py) attribute contract,
  and the generated JSON schemas). There is no room for "how to become this
  user."
- At login, `ConnectionManager.credentials`
  ([connections.py](../../../src/otto/host/connections.py)) collapses the dict
  to a single `(user, password)` tuple — `host.user` or the *first* dict entry
  — consumed by the SSH, telnet, and FTP transports.
- The only user transition after login is the hardcoded single-step `su`
  exchange `_perform_su` in
  [privilege.py](../../../src/otto/host/privilege.py), used by
  `switch_user`/`as_user` (the Spec A per-session elevation work).

Separately, **application shells (REPLs)** — mysql, `python3`, vendor CLIs —
are only drivable via raw `send()`/`expect()` loops (the manual pattern in
`docs/cookbook/sessions.md`). That works but is not ergonomic, and the output
comes back as a string the caller must parse by hand.

Ownership shapes the design: proxy steps are **owned by the host, not by any
project**. otto already has the right delivery channel — `init` modules listed
in `.otto/settings.toml`, imported by `bootstrap()`
([bootstrap.py](../../../src/otto/bootstrap.py)), populating ~18
[`Registry`](../../../src/otto/registry.py) instances. Login proxies become
one more registry; lab data references registered proxies by name, exactly
like term/transfer backends.

## 2. Goals / non-goals

**Goals**

- A cred entry can declare: "to become me, authenticate as user X and run the
  registered proxy steps N" — honored at **both** user-transition moments:
  initial session establishment and `switch_user`/`as_user`.
- Proxies are **named async callables** registered by libraries from `init`
  modules (`register_login_proxy`), performing arbitrary `send`/`expect` steps.
- `otto host <id> login` (the interactive bridge) honors proxies too, with an
  `--as-user` option.
- An `AppShell` base class makes REPLs first-class: declared launch command,
  **explicit required prompt regex**, quit command, context-manager
  entry/exit, and a `cmd()` primitive that parses output into **pydantic
  objects** riding in the existing `Result` family.
- Fail loud: a proxy or REPL that doesn't reach the expected state raises with
  host/user/pattern named — never a silently half-switched session.

**Non-goals**

- **Password blinding/secrecy.** Target hosts live on air-gapped, team-shared
  networks; creds in lab data stay plain. (The existing `LogMode.NEVER`
  hygiene for password *sends* is preserved, but no new secrecy mechanism.)
- Declarative step lists in lab data (a proxy is code; `params` carries data).
- Exposing AppShell commands as `otto host` CLI verbs (future candidate).
- Proxies that land anywhere other than a POSIX shell (the command frame must
  keep working after the steps run). Non-shell landings are what AppShell is
  for — *after* a session exists.
- Backwards compatibility for the `creds` dict shape (deliberate breaking
  change, see §12).

---

## Part 1 — Login proxy

### 3. Creds data model (breaking restructure)

`creds` changes from `dict[login, password]` to an **ordered list of cred
objects**:

```json
"creds": [
  {"login": "admin", "password": "hunter2"},
  {"login": "mysql", "password": "sqlpw", "proxy": "mysql-su", "via": "admin",
   "params": {"service": "mysqld"}}
]
```

**Boundary spec** — new `CredSpec(OttoModel)` in
[models/host.py](../../../src/otto/models/host.py):

```python
class CredSpec(OttoModel):
    login: str                                    # required
    password: str | None = None
    proxy: str | None = None                      # login-proxy registry key
    via: str | None = None                        # another entry's login
    params: dict[str, Any] = Field(default_factory=dict)  # free-form, handed to the proxy
```

- `HostSpec.creds: list[CredSpec]` (default empty); `UnixHostSpec` keeps its
  required override (min length 1).
- Spec-level validation: logins unique; `via`/`params` allowed only with
  `proxy`; `via` must name another entry (not itself); via-chains must
  terminate at a proxy-less entry (cycle detection over the list); `host.user`
  (when set) must name a listed login.
- `proxy` names are validated against the registry **at ingest** in
  `create_host_from_dict` ([storage/factory.py](../../../src/otto/storage/factory.py))
  — same pattern and timing as transfer-backend validation, safely after
  `bootstrap()` has imported `init` modules.
- **First entry = default login user**, making today's implicit "first dict
  entry" semantics explicit and ordered. `host.user` still overrides by
  naming a login. A proxied entry with no `via` defaults to the first
  proxy-less entry.

**Runtime** — frozen dataclass twin `Cred` (same fields). `host.creds`
becomes `list[Cred]` on `UnixHost`, the `RemoteHost` attribute contract, and
everywhere else the dict appears. Helpers: `host.cred(login) -> Cred` (loud
lookup listing known logins) and `host.default_cred` (first entry).
`_user_password` re-routes through the list; `_sudo_password` returns the
password of the session's **current user** (looked up in creds) rather than
the transport-auth cred's — with proxying these can differ.

`password: None` on a direct-login cred is passed as `None` to asyncssh —
permitting key/agent auth as a side benefit (previously impossible with the
str-only dict). The telnet login sends an empty line for `None`.

All four declaration layers move together, plus the spec↔runtime drift guard
(`HOST_SPEC_RUNTIME_PAIRS`) and regenerated JSON schemas (`make schema`).

### 4. Proxy registry & callable contract

New module `src/otto/host/login_proxy.py`, mirroring the existing registries:

```python
LOGIN_PROXIES: Registry[LoginProxy] = Registry(
    "login proxy", register_hint="otto.register_login_proxy()"
)

def register_login_proxy(
    name: str,
    fn: LoginProxyFn,
    *,
    undo: LoginProxyFn | None = None,   # reverses the steps; default: send "exit"
    overwrite: bool = False,
) -> None: ...

class ProxyIO(Protocol):
    """Minimal handle a proxy drives; satisfied by all three carriers."""
    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None: ...
    async def expect(self, pattern: str | re.Pattern[str], timeout: float = 10.0) -> str: ...

@dataclass(frozen=True)
class ProxyContext:
    target: Cred        # the cred being become (login/password/params)
    via: Cred           # the cred currently in control
    host_id: str        # for error messages; deliberately NOT the host object

LoginProxyFn = Callable[[ProxyIO, ProxyContext], Awaitable[None]]
```

- `ProxyIO` is a tiny protocol so one proxy runs over three carriers: the
  raw `ShellSession` (session establishment), the interact bridge (PTY), and
  the elevation path. The existing `send(…, log=LogMode.NEVER)` password
  hygiene from `_perform_su` is part of the contract.
- `ProxyContext` deliberately excludes the host object — calling `run()`
  mid-proxy on the very session being established is a deadlock foot-gun.
  Host-specific data rides in `target.params`.
- **Built-in `"su"` proxy**, pre-registered at import, replaces `_perform_su`:

  ```python
  async def _su_proxy(io: ProxyIO, ctx: ProxyContext) -> None:
      await io.send(f"su {shlex.quote(ctx.target.login)}\n")
      if ctx.target.password is not None:
          await io.expect(r"[Pp]assword:")
          await io.send(ctx.target.password + "\n", log=LogMode.NEVER)
  ```

  A cred with no `proxy` field uses `"su"` for `switch_user`, so exactly
  **one** user-transition mechanism remains in the codebase.
- `undo` reverses the steps for `as_user` restore. The default (None) sends
  `exit\n` — precisely what `as_user` does today, correct for any
  su/sudo-style nested shell; exotic proxies override it.
- Registered from `init` modules; no bootstrap changes needed.

### 5. Runtime flows

**Chain resolution** (pure function in `login_proxy.py`):
`resolve_chain(creds, target_login) -> tuple[Cred, list[Cred]]` — walk `via`
links from the target down to a direct-login cred; return the direct cred
(for transport auth) plus the hop list to apply, outermost first. Cycles are
already excluded at spec validation.

1. **Session establishment** (SSH and telnet identically).
   `ConnectionManager` holds the `list[Cred]`; its `credentials` property now
   returns the **resolved direct cred** `(login, password)` for transport
   auth, and a new `proxy_hops` property exposes the hop list. After the
   marker handshake in `ShellSession._ensure_initialized`
   ([session.py](../../../src/otto/host/session.py)), `SessionManager` applies
   each hop's proxy over a `ShellSession`-backed `ProxyIO`, then seeds
   `current_user` to the requested login. Insertion is in the session-build
   paths (`_ensure_session` / `open_session` / oneshot pool), so it is
   transport-agnostic and covers **every** session — oneshots included
   (slower for proxied users, but correct). Each hop must land in a POSIX
   shell (documented requirement; the command frame keeps working because
   tty-level `stty -echo` and the sentinel mechanism survive nested shells,
   exactly as they do for `su` today).
   **Ownership rule for the other command surfaces:** every *command*
   surface runs as the host's effective user. SSH `oneshot` exec channels
   authenticate at the transport and cannot replay proxy steps (they are not
   interactive shells), so when the effective user is proxied, oneshot-style
   work — including the nc transfer backend's data-plane commands
   (`nc -l … > dst`, `nc … < src`), which ride the same exec path — routes
   through pooled **proxied shell sessions** instead. Net effect: **nc
   transfers respect the proxied user's ownership** (transferred files land
   owned by the target user), at the cost of framed shell-session execution
   instead of a raw exec channel; telnet hosts already run oneshots on
   pooled shell sessions, so only the SSH path changes. The authenticating
   transfer transports (scp/sftp/ftp) necessarily authenticate as the
   resolved *direct* cred — they cannot ride a shell — so their files land
   owned by the via-user; documented, with nc named as the ownership-correct
   alternative.
2. **`switch_user(user, password=None)`** — resolve `host.cred(user)`; if
   absent, synthesize an ad-hoc `Cred(login=user or "root", password=…)` so
   the current "su to any user with an explicit password" behavior is
   preserved (public signatures from Spec A are unchanged). If the cred's
   `via` is set and differs from `current_user`, recursively switch to `via`
   first. Run the cred's proxy (or `"su"`); stamp `current_user`. The
   internal engine returns the applied hop list; **`as_user`** records it and
   restores by running each hop's `undo` in reverse (N nested shells → N
   exits — today's single `exit` generalized), then restamps the prior user.
3. **Interact** — `interact()` ([host.py](../../../src/otto/host/host.py),
   the `login` verb) gains `as_user: str | None = None` (CLI flag
   `--as-user`, default: the host's login user). The bridge
   ([interact.py](../../../src/otto/host/interact.py)) authenticates as the
   resolved direct cred, replays the hops through a bridge-backed `ProxyIO`
   (write to the remote; read until pattern with timeout) **before** starting
   the interactive pumps, then hands the human a ready, proxied terminal.

### 6. Failure semantics

Any proxy step timeout or expect mismatch raises **`LoginProxyError`** naming
the host id, target login, proxy name, and the pattern that didn't match. The
session being established is closed and discarded (never pooled, never handed
out half-switched). During `switch_user`, the session is marked for recovery
(the existing `_needs_recovery` path) since its user state is unknown. No
skips — a dead or misconfigured proxy fails the run loudly
(consistent with the "never skip on host-down" rule).

---

## Part 2 — AppShell

### 7. Class shape & entry points

New module `src/otto/host/app_shell.py`:

```python
class AppShell:
    launch: ClassVar[str]                    # command that starts the app
    prompt: ClassVar[re.Pattern[str]]        # REQUIRED; end-anchor recommended (r"...\Z")
    quit_cmd: ClassVar[str] = "exit"         # sent on context exit
    user: ClassVar[str | None] = None        # cred login to become first (Part-1 proxy machinery)
    cmd_timeout: ClassVar[float] = 30.0      # default prompt wait (launch/quit/cmd)
```

`__init_subclass__` fails at class-definition time if `launch` or `prompt` is
missing — no runtime surprises. There is deliberately **no AppShell
registry**: shells are plain classes users import (a registry adds nothing
until CLI exposure, which is out of scope).

**Two entry points, both async context managers:**

- `host.app_shell(MySql, *, user=None, timeout=None)` — on `BaseHost`.
  Provisions a dedicated named session (auto-named, collision-safe), applies
  `switch_user` for `user or cls.user` (login-proxying if that cred is
  proxied), sends `launch`, expects `prompt`, yields the shell instance. Exit
  sends `quit_cmd`, confirms the POSIX shell is back via the existing
  command-frame recover handshake
  ([command_frame.py](../../../src/otto/host/command_frame.py)), and closes
  the owned session.
- `MySql.attach(session)` — classmethod, layers onto a `HostSession` the
  caller has already positioned (this is the "REPL living inside an active
  shell session" from the TODO). Same launch/quit/recover flow, but the
  session is left open afterward and `run()` is unblocked.

The full mysql story composes both parts:

```python
class SqlRow(Parsed):
    pattern = re.compile(r"\|\s*(?P<id>\d+)\s*\|\s*(?P<name>\S+)\s*\|")
    id: int
    name: str

class MySql(AppShell):
    launch = "mysql --pager=cat"
    prompt = re.compile(r"mysql> \Z")
    quit_cmd = "quit"
    user = "mysql"                            # proxied cred from Part 1

    async def select(self, cols: str, table: str) -> ShellResult:
        return await self.cmd(f"SELECT {cols} FROM {table};", parse=list[SqlRow])

async with host.app_shell(MySql) as sql:      # proxies to mysql, launches the REPL
    rows = (await sql.select("id,name", "users")).value   # list[SqlRow]
```

### 8. Session locking

While an AppShell is attached, the underlying session's `run()` raises
**`AppShellActiveError`** naming the shell class and session — the sentinel
frame must never be typed into the app. Raw `send`/`expect` stay available
(`cmd()` is built on them; power users may need them mid-shell). Attaching to
a session that already has an active AppShell also raises
`AppShellActiveError` (no nesting).

### 9. `cmd()`, `Parsed`, `ShellResult`

```python
async def cmd(self, text: str, *, parse: ParseSpec | None = None,
              timeout: float | None = None) -> ShellResult
```

Sends the line, reads until `prompt` matches the accumulated output (via the
session's `expect`), then strips: the echoed command line if present (echo
behavior varies by app), ANSI sequences (reusing the frame helper), and the
matched prompt. What remains is the command's output.

**`Parsed`** — pydantic base (subclassing `OttoModel`) carrying
`pattern: ClassVar[re.Pattern[str]]`. A class-definition-time check enforces
that the pattern's named groups are a subset of the model's field names
(typo guard) **and** a superset of its required fields — pattern/model drift
is impossible. Optional groups that don't participate in a match arrive as
`None` and validate against optional fields.

**`parse=` accepts three forms** (flow: output → regex named groups →
`dict[str, str]` → pydantic validation/conversion → `Result.value`):

| Form | Mechanics | Result.value |
| --- | --- | --- |
| `Model` (a `Parsed` subclass) | single `pattern.search` → `groupdict` → model | `Model` instance |
| `list[Model]` | `pattern.finditer` → one model per match | `list[Model]` (empty = valid zero-row answer) |
| `Callable[[str], Any]` | escape hatch for formats a row-regex can't handle | whatever it returns |

**Nested parsing (sub-objects).** A `Parsed` field may itself be typed as a
`Parsed` subclass or `list[Sub]`. The outer pattern's same-named group
captures a *region* of the output, and the field's type decides how that
region is interpreted: scalar fields convert the group text directly
(pydantic); a `Parsed`-typed field recursively parses its region with the
sub-model's own pattern (single `search`); a `list[Sub]` field runs
`finditer` over its region. The class-def-time group↔field check applies
recursively at every level, and optional groups that don't participate map
to optional fields as before. This models real REPL output, which is
usually a composite — for mysql's `SELECT`, the printed table *and* the
trailing stats line:

```python
class Row(Parsed):
    pattern = re.compile(r"^\|(?P<cells>.+)\|$", re.MULTILINE)
    cells: str                      # or one named group per column

class Table(Parsed):
    pattern = re.compile(r"(?P<rows>(?:^\|.+\|$\n?)+)", re.MULTILINE)
    rows: list[Row]

class QueryStats(Parsed):
    pattern = re.compile(r"(?P<count>\d+) rows? in set \((?P<seconds>[\d.]+) sec\)")
    count: int
    seconds: float

class Select(Parsed):
    pattern = re.compile(
        r"(?P<table>^\+-[\s\S]+?^\+-[^\n]*$)\s*(?P<stats>\d+ rows? in set[^\n]*)",
        re.MULTILINE,
    )
    table: Table
    stats: QueryStats

result = await sql.cmd("SELECT FirstName, LastName, Department FROM Employees;",
                       parse=Select)
result.value.stats.count      # 5
result.value.table.rows[0]    # Row(cells='| Alice | Smith | Engineering |'-ish)
```

**`ShellResult(Result)`** — new frozen dataclass in
[result.py](../../../src/otto/result.py) with `command: str` and
`output: str` (raw, post-strip). `value` holds the parsed object, or the
output itself when `parse=None` (mirroring the `CommandResult`
"value = output" convention). No `retcode` — REPLs don't have one;
`exit_code` inherits the base `Result` behavior (`status.value` on failure).

### 10. Failure semantics — raise vs fail

*State* problems raise; *data* problems return in-band:

- **Prompt timeout** → `AppShellTimeoutError` (REPL state unknown). The
  context manager marks the shell broken; on unwind it skips `quit_cmd`, goes
  straight to frame recovery, and closes the session it owns.
- **Parse mismatch** (no regex match where one is required, or the callable
  raising) → `ShellResult(status=Status.Failed, msg=<what didn't match>,
  output=<preserved for debugging>, value=None)`. The REPL is fine; the data
  wasn't what the caller expected.

---

## Cross-cutting

### 11. Public exports & module layout

- New modules: `src/otto/host/login_proxy.py`, `src/otto/host/app_shell.py`.
- Lazy top-level exports (house style, import-budget aware —
  `tests/unit/import_budget/` guards apply): `AppShell`, `Parsed`,
  `ShellResult`, `Cred`, `register_login_proxy`.
- `_perform_su` is deleted; `privilege.py` routes through the proxy engine.

### 12. Migration (breaking)

- Every in-repo hosts.json moves to list-creds: `tests/repo*` beds, the
  `otto init` scaffold, `otto.examples` samples, and doc snippets (the
  host-database guide especially).
- JSON schemas regenerated (`make schema`).
- The `HostSpec`/`UnixHost` field change trips the spec↔runtime drift guard —
  the **full `tests/unit`** must run (this class of change broke main in
  #67/#68/#69).
- User-facing breaking-change notes: `creds` list shape, `host.creds` runtime
  type, first-entry default-login semantics (unchanged in spirit, now
  explicit).
- Bed provisioning: the Vagrantfile gains `provision_mysql` for the three
  Unix test VMs (mysql-server + the proxy-target account, §14); the same
  steps were applied live to the running VMs, so no re-provision is needed.

### 13. Docs

- Cookbook `sessions.md`: the manual `send`/`expect` REPL pattern is replaced
  by AppShell.
- Extending guide: `register_login_proxy` section mirroring
  `extending-backends.md`.
- Executable-doctest samples in `otto.examples`: an example proxy fn and a
  `PyRepl` AppShell.
- Host-database guide: creds section rewritten for the list shape.

### 14. Testing

- **Unit**: `CredSpec` validation (uniqueness, via rules, cycles, user∈logins);
  registry registration/lookup/duplicates; `resolve_chain`; proxy engine over
  a fake session (hop application, undo order, failure teardown); AppShell
  state machine (launch/quit/recover, locking, echo/ANSI/prompt stripping);
  all three `parse=` forms including the `Parsed` class-def-time checks.
- **E2E** — the three Unix test VMs (carrot/tomato/pepper) provision
  `mysql-server` plus the login-proxy target account (Vagrantfile
  `provision_mysql`, also applied live to the running VMs on 2026-07-04):
  the packaged `mysql` Unix user gets password `Password1` but keeps its
  `nologin` shell and is `DenyUsers`-blocked in sshd, so direct SSH **must
  fail** (asserted); a socket-auth MySQL account for that Unix user plus an
  `otto_test` database completes the REPL path. The e2e registers a custom
  proxy sending `sudo su -s /bin/bash mysql` (verified live 2026-07-04) —
  deliberately *not* the built-in `su`: the restricted shell defeats plain
  `su`, and util-linux `su` silently **ignores `-s` for non-root callers**
  when the target's shell isn't in `/etc/shells`, so only a root-mediated
  `su -s` works. This exercises the third-party registration path
  end-to-end; the password-expect path is covered by the built-in `su`
  proxy switching to the existing `test` user (bash shell, `Password1`).
  Coverage: proxied session establishment, `switch_user`/`as_user`
  round-trip, nc-put ownership (file owned by `mysql`, not `vagrant`), the
  full `host.app_shell(MySql)` story against real mysqld with nested
  parsing, and interact `--as-user` via the existing pty-driven interact
  test pattern. AppShell also drives a real `python3` REPL (available on
  every bed, including local) for transport-independent coverage.

### 15. Sequencing & future work

Implementation order: Part 1 (creds restructure → registry/engine → session
flows → interact), then Part 2 (AppShell core → parsing → docs/e2e). Single
implementation plan, phased.

Future (explicitly deferred): CLI exposure of AppShell commands as host
verbs; declarative step lists; cred-login completion for `--as-user`;
non-POSIX proxy landings.
