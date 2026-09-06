# Session setup hook — arbitrary code between the landing shell and the target shell

**Date:** 2026-09-05 (revised the same day after an independent Fable review,
then re-scoped to a local target)
**Status:** Approved design, pre-implementation
**Scope:** A host-level, registered hook that runs once on every shell session
otto opens, after the landing handshake and after every login-proxy hop, with
a real `HostSession` in hand; and a second dialect, the *landing frame*, so
the shell otto lands in need not be the shell its `command_frame` describes.
The target shell is an application running **on the host itself** — a vendor
CLI, a REPL, an application shell — entered from the login shell by sending
and receiving on the same console. Covers the persistent default session,
named sessions, the exec pool on Unix hosts, and the interactive `login`
bridge, for lab-declared `UnixHost` and `EmbeddedHost` entries.

## 1. Motivation and decisions

`docs/getting-started/customizations.md` says, under *After connecting*, that
otto runs no shell command of its own after a session opens and offers no
hook to add one, and that the usual reason to want one (a custom prompt) does
not apply because every command frame brackets commands with its own markers.
Chris pushed back on 2026-09-05: the reason to want a post-connect step is
**environment setup**, which ranges from exporting a variable to entering an
application shell, provisioning a database, and handing a confirmed-good
shell to the caller or the human. The sharper form of the need, stated later
the same day: the shell the `command_frame` describes may be an application
that runs on the host and is reached from the login shell, so otto has to
land, manoeuvre into that application with arbitrary code, and only then
invoke the frame.

What otto can do today, and why it is not the feature:

- The **login proxy** (`otto.host.login_proxy`) is arbitrary async code that
  runs send/expect over the raw channel right after the handshake, on every
  path that matters, and the documented `docker-shell` proxy already "enters
  something and hands the shell over". But a proxy is keyed to an
  **identity**: every hop ends with an `id -un` assertion against the cred's
  login, so setup that changes no user has to be modelled as a phantom cred;
  it fires only when its cred is the login target; its `ProxyIO` is
  send/expect only, with no framed `run()` and no `AppShell.attach`; and it
  is a no-op on the families built from a session factory (local, docker,
  embedded).
- The **CommandFrame** is the wrong layer for the code. It is a stateless
  dialect value object; its only "first thing written" seam, `handshake`, is
  resent in a loop until the shell answers, so anything folded into it must
  be idempotent, and it has no access to host, creds or params. Decisively,
  the interactive bridge (`otto.host.interact`) never runs a frame handshake,
  so frame-based setup would reach `run()` sessions but not `otto host X
  login`.
- One frame serves both the landing shell and the target shell. The
  handshake is rendered by the host's `command_frame` in whatever shell the
  transport lands in, so a host whose frame describes a local application
  fails its handshake in the login shell before any code could run.
- The **AppShell** drives a REPL *temporarily* inside a bash session and
  hands bash back on exit. It is the right tool when the application is a
  detour; it is not a way to make the application the session's shell.
- `SshOptions.post_connect` is connection-level (port forwards, X11) and
  never touches a shell.

Decisions taken during brainstorming (Chris, 2026-09-05):

1. **A host-level hook, not a CommandFrame extension and not a proxy.**
2. **The hook receives a real `HostSession`**, so it can `run()` with exit
   codes, `send`/`expect`, and attach `AppShell` subclasses. The login bridge
   therefore has to build a framed session on its PTY before handing over.
3. **Lab data declares it as `session_setup`, a name or a table**, mirroring
   `power_control`: `"session_setup": "provision-db"` or
   `"session_setup": {"type": "provision-db", "db": "otto_test"}`; every key
   but `type` becomes the hook's params.
4. **Confirmation is dialect-owned and OS-agnostic.** No unix `id -un`
   probe; embedded hosts must get the hook, and they may have no unix-like
   signal to depend on.
5. **Identity is untouched.** `current_user` stays what the proxy chain
   established. Becoming someone is a proxy's job with its own identity
   proof; a hook that changes identity is unsupported and documented as such.
6. **Two dialects, both explicit.** The *handshake* confirms connectivity to
   the first landing shell and is rendered by a new `landing_frame`, which
   defaults to `command_frame`. *Frame entry* is a second readiness probe,
   rendered by `command_frame`, run after the hook in whatever shell the hook
   navigated to. Every existing host, and every hook that stays in one
   dialect, keeps today's single-dialect behaviour byte for byte.
7. **Order.** Transport hops, then land, then the handshake, then every
   login-proxy hop, then the hook in the landing dialect, then frame entry.

Decisions taken from the independent review (2026-09-05), each with the
finding that forced it:

**8.** **Frame entry never reopens the transport, and the post-hook confirmation
is unconditional.** The first draft re-ran the code that also opens the
channel, which on SSH would have confirmed a fresh shell and orphaned the
hook's; and its idempotence rule skipped the confirmation on every
single-dialect host. Frame entry is the handshake half only, always run
after the hook, with fresh markers.

**9.** **Version 1 is lab-declared Unix and embedded hosts.** `LocalHost` and
`DockerContainerHost` have no lab entry to declare a hook on, and their
`exec` never consults the route that would run it; the manager-level
mechanism is family-agnostic, but declaring it there is separate work.

**10.** **A two-dialect host speaks the target dialect on every session after
the hook, `exec` included.** Each pooled session runs the hook and enters
the application on its own, as it replays proxy hops today. The Unix
verbs that assume a POSIX shell at the far end of `exec` — which is how
the userland is resolved — are unavailable on such a host, and the docs
list them.

**11.** **Login-proxy hops require a bash-family landing dialect**, because the
hop identity probe is bash. A non-bash landing with a cred chain that
yields hops is refused at load.

**12.** **The documented example is the tested example.** The bed e2e loads the
getting-started lab and the hooks `gs_example` registers; the page's bed
captures are cross-checked by that e2e because no push gate runs bed
captures.

Decisions taken on re-scoping (Chris, 2026-09-05):

**13.** **The target is local.** The application otto ends up in runs on the
host it landed on and is reached through the same console. A target that
is a separate machine reached by a nested console program (telnet,
picocom) is not this design's case: the mechanism may serve it, with
caveats a project owns (single-client consoles, escape-key collisions),
but nothing here claims or tests it.

**14.** **The bed's stand-in for "a local application with its own dialect" is
the `python3` REPL**, with a `PyReplFrame` registered by the example
project the way it already registers a custom Zephyr frame. One page then
shows `register_command_frame`, `landing_frame` and `session_setup`
working together on real bytes.

**15.** **A built-in `raw` landing dialect for landings that answer no frame.**
A boot menu that reads a digit, an autoboot countdown, a serial-recovery
prompt, a boot log with no prompt yet: nothing there echoes a token back, so
no frame can handshake. `landing_frame: "raw"` means no landing handshake;
the hook gets `send`/`expect` and nothing framed until `enter_frame()`;
frame entry is unconditional, so the session is still confirmed by the
target frame before anyone gets it. Landing-only: `command_frame: "raw"` is
refused at load, and hops are refused with it (it is not bash-family).

**16.** **Telnet to a serial console comes for free.** The embedded family
already reaches a console by telnet through any number of SSH transport hops
(every Zephyr guest on the bed is `term: telnet` + `hop: test4`). With
`landing_frame: "raw"` and a hook, that same transport lands in whatever
state the console is in, the hook manoeuvres to the Zephyr shell, and frame
entry confirms it. No transport work is involved; decision 13 excludes only a
console reached by a *program run from the landing shell*, not a console
that *is* the transport.

## 2. Architecture

```text
transport opens (through every `hop` tunnel; no shell exists yet)
  └─ ShellSession._open()                                   transport only
       └─ ShellSession._handshake()        LANDING frame: READY marker
            └─ SessionManager._apply_login_proxy   proxy hops → login_target, identity-proved
                 └─ SessionManager._apply_session_setup                       ← NEW
                      ├─ hook(HostSession framed in the LANDING dialect, SetupContext)
                      └─ ShellSession.enter_frame(command_frame)   fresh markers,
                           TARGET frame's handshake in the shell the hook left; always runs
                                └─ session is handed to the caller / the pumps
```

Two kinds of hop, both before the hook: **transport hops** (`hop` in the
host entry) are SSH tunnels the `ConnectionManager` opens before any shell
exists; **login-proxy hops** are user switches replayed in the landed shell.
A host two tunnels away and one `su` deep runs its hook after all three.

`ShellSession._ensure_initialized` is split into `_open()` (transport) and
`_handshake()` (dialect readiness); the existing callers see no change. Frame
entry reuses `_handshake()` with a different frame and fresh markers; nothing
about transports changes.

### 2.1 Registry, declaration, and the second dialect

- `otto.host.session_setup` (new module) holds:
  - `SetupContext` (frozen dataclass): `host_id: str`, `host_name: str`,
    `user: str` (the identity the session is in when the hook starts, `""`
    when the family has none), `params: dict[str, Any]`, and
    `kind: Literal["default", "named", "exec_pool", "bridge"]` — which
    session is being set up. A hook that provisions something once (a
    database) does it on `default` and only *enters* on the others; the
    context is how it tells.
  - `SessionSetupFn = Callable[[HostSession, SetupContext], Awaitable[None]]`.
  - `SESSION_SETUPS: Registry[SessionSetupFn]`, kind `"session setup"`,
    `register_hint="otto.register_session_setup()"`.
  - `register_session_setup(name, fn, *, overwrite=False)`, origin from
    `caller_module()`, exactly like `register_login_proxy`.
  - `SessionSetupError(OttoError, ConnectionError)`.
  - `SessionSetup` (frozen dataclass): `name: str`, `params: dict[str, Any]`
    — the runtime value a host carries.
  - `session_setup_from_spec(value)` accepting `None`, an existing
    `SessionSetup`, a `str`, or a `dict` with a `type` key, mirroring
    `power_control_from_spec`. Unknown names raise through the registry's
    did-you-mean error.
- `otto.__init__` exports `register_session_setup` and `SetupContext` through
  the lazy-export table. No test enumerates the top-level package's exports
  (the hand-written lists in `tests/unit/config/test_lazy_exports.py` cover
  `otto.config`), so the doctest in `otto.examples.session_setup`, which
  imports both names from `otto`, is what proves the export.
- `HostSpec` (common to both families) gains two fields:
  - `session_setup: dict[str, Any] | str | None = None`, validated at load: a
    string must be registered; a dict must carry a string `type` that is
    registered.
  - `landing_frame: str | None = None`, validated against `FRAME_CLASSES`
    exactly as `command_frame` is, and **only valid alongside
    `session_setup`**: a landing dialect with nothing to transition out of it
    is a contradiction, refused at load with a message naming both fields.
    `EmbeddedHostSpec` accepts it too — a boot-loader console landing on an
    embedded target is coherent.
  - A `landing_frame` that is not a `BashFrame` subclass together with **any**
    cred carrying a `proxy` is refused at load: hops run in the landing shell
    and their identity probe is bash. The test is the presence of a proxied
    cred, not whether today's `login_target` resolves through one — a later
    `--user`, or a hook's own `as_user`, would hop in the landing shell whatever
    the default login does, so the safe rule is the one that cannot be made
    wrong by a caller's argument.
  - `command_frame: "raw"` is refused at load: raw is a landing, never a
    destination.
  Both pass through `_common_host_kwargs` when set, and the drift guard's
  field lists (`tests/unit/models/test_host_specs.py`) are updated with them.
- `RawFrame` (`type_name = "raw"`) joins the built-in frames in
  `otto.host.command_frame`: `handshake()` renders `""`, and `_handshake()`
  treats an empty payload as "nothing to confirm" — the session is marked
  initialized and alive without writing or reading a byte. `frame()` and
  `recover()` raise `RawLandingError` (in `otto.host.errors`, an `OttoError`)
  with the message that a raw landing has no command dialect and the caller
  should use `send`/`expect` or `enter_frame()`. `quiet_history()` is `""`,
  `restore_interactive()` is `None`, `streams_output_live` is `False`.
  Consequences the docs state: in a raw landing `run()` refuses, and a
  cancelled `expect()` marks the session for recovery, which the raw frame
  cannot perform, so the session is dead and the open fails; the hook's
  `expect` timeouts should therefore be generous.
- `UnixHost` and `EmbeddedHost` gain `session_setup: SessionSetup | None =
  None` and `landing_frame: CommandFrame | None = None` beside
  `command_frame`, coerced in `__post_init__` (`session_setup_from_spec`,
  `build_command_frame`), and forward both to their `SessionManager`.
- `SessionManager` builds every session with `landing_frame or
  command_frame`, keeps `command_frame` as the target for frame entry, and
  renders `_history_prefix()` from the **landing** frame, because that
  payload rides the proxy hops' resync probes and `as_user` switches, which
  run in the landing shell. The frame-entry payload renders its own prefix
  from the target frame inside `_handshake()`. With `landing_frame` unset the
  two frames are the same object and today's behaviour is unchanged.

### 2.2 The hook contract

```python
async def enter_acme(session: HostSession, ctx: SetupContext) -> None:
    # landing dialect (bash): framed run() works here
    up = (await session.run("systemctl is-active acme")).only
    if up.retcode != 0:
        raise RuntimeError(f"{ctx.host_name}: acme is not running")
    if ctx.kind == "default":
        await session.run(f"acme-admin create-db {ctx.params['db']}")
    # navigate into the local application with raw send/expect
    await session.send("acme-cli\n")
    await session.expect(r"acme> \Z")
    # optional: enter the target dialect now to verify it before returning
    await session.enter_frame()
    await session.run("show version")
```

- The session is a real `HostSession`: `run()` returns `Results` with exit
  codes through the session's *current* frame; `send`/`expect` are raw;
  `AppShell.attach` takes the session's app-shell lock as it does anywhere
  else. All traffic goes through the manager's log sinks like any other
  session traffic.
- Until frame entry the current frame is the landing frame, so `run()` is
  valid in the landing shell. After `enter_frame()` it is the target frame.
  A hook that drives a transitional program between the two uses send/expect
  or an `AppShell` for that stretch. In a `raw` landing there is no landing
  dialect: `run()` raises `RawLandingError` until `enter_frame()`, and the
  hook works with `send`/`expect` alone — for a boot menu, that is sending
  the digit and expecting the shell's first prompt:

  ```python
  async def land_on_zephyr(session: HostSession, ctx: SetupContext) -> None:
      await session.send("\n")
      await session.expect(r"uart:~\$ \Z", timeout=ctx.params.get("boot_timeout", 30.0))
  ```
- `await session.enter_frame(timeout=None)` is optional and may be called
  more than once; every call runs the target handshake (§2.4). A hook that
  never calls it has frame entry performed for it after it returns, and one
  that does still gets the unconditional post-hook confirmation. `timeout`
  overrides the session's handshake ceiling for that call (a Unix host's is
  3 s; an application that starts slowly can need more), and is the hook's
  knob, not a lab field.
- `enter_frame()` refuses with `AppShellActiveError` while an `AppShell` is
  attached (the handshake would be typed into the REPL) and with
  `SessionSetupError` on a session already marked dead.
- `session.close()` inside the hook refuses (`SessionSetupError`): the
  session is mid-establishment and the manager owns its teardown.
- Calling `run()` after navigating away from the landing shell without
  entering the frame is the hook's own bug, and it is expensive: the framed
  command waits out its timeout (30 s by default), recovery sends the
  landing frame's probe into the application, never confirms, and marks the
  session dead; frame entry then refuses and the open fails as
  `SessionSetupError`. The docs say so.
- The hook does **not** receive the host object, for the reason
  `ProxyContext` does not: `host.run()` re-enters the session lock that is
  held while the hook runs and would deadlock. Host-specific data rides in
  `ctx.params`.

### 2.3 Where it runs

`SessionManager._apply_session_setup(session, kind)` runs in exactly the
places `_apply_login_proxy` runs, immediately after it:

1. `_ensure_session` (the default session), inside the establishment lock and
   inside the same `try` that tears a fresh session down on failure.
2. `open_session` (named sessions), which is also the exec-pool path on
   Unix hosts.
3. The interactive bridge (§2.5).

Consequences, all of which the docs state:

- A **Unix host with a hook takes the pooled-session route for `exec`**,
  exactly as a proxied host does today (`_ExecRoute`): the raw exec channel
  cannot run setup, and silently running `exec` without the hook's
  environment would be the same class of bug as running it as the via-user.
  The route decision gains one more condition beside `proxy_hops`.
  `exec(user=...)` goes to a per-user connection and bypasses the hook, as
  it bypasses hops today; documented.
- On a **two-dialect host** (`landing_frame` set) every session, pooled
  ones included, ends in the application and speaks the target dialect. So
  `run`, `exec`, `send`/`expect`, named sessions, `app_shell` and `login` all
  address the application. The Unix verbs that assume a POSIX shell at the
  far end of `exec` are unavailable there and the host-options page lists
  them: `run(sudo=True)`, `read_file`/`write_file`, `lsmod`, `shutdown`,
  `reboot(wait=True)`, the `shell` and `nc` transfer backends, and anything
  else routed through `Userland`. `put`/`get` over `sftp`/`scp` work: they
  are transport-level and land on the host the application runs on; whether
  the application sees the files is the project's business.
- The hook runs **once per session open**. It never re-runs on `as_user` or
  `switch_user`, which are identity transitions inside an already-set-up
  session, and it never runs on a session revived by the post-timeout
  recovery, which reopens no transport.
- Under a **dry run** no session opens, so the hook does not run; the
  `DeclinedSession` path is untouched.
- `EmbeddedHost` builds the same manager, so its default and named sessions
  run the hook. Its `login` is not implemented, so there is no bridge to
  extend. **The serial-console flow is enabled as it stands** (decision 16):
  any number of SSH transport hops, then the embedded family's telnet to the
  console through the last hop's port forward (today's `term: telnet` +
  `hop`, `login=False`), landing in whatever state the console is in;
  `landing_frame: "raw"` and a hook manoeuvre to the Zephyr shell; frame
  entry in `zephyr`, `zephyr-serial` or a project frame confirms it. The
  single-dialect embedded entry keeps today's behaviour: its handshake's
  resend loop still absorbs a boot banner ahead of the prompt.
- `LocalHost` and `DockerContainerHost` are out of version 1 (decision 9).

### 2.4 Frame entry

`ShellSession.enter_frame(frame, *, timeout=None)`:

1. refuse with `AppShellActiveError` if an `AppShell` is attached; refuse
   with `SessionSetupError` if `_alive` is False;
2. mint a fresh `SessionMarkers` (the landing set may still have an
   unread READY reply sitting in the stream from the resend loop; a probe
   that could match it would confirm a shell that never answered);
3. set `_frame = frame`, recompute `_end_pattern`, clear `_needs_recovery`
   (the handshake supersedes any pending recovery);
4. run `_handshake()` — the target frame's own `handshake` payload, the
   target frame's history payload prefixed, resent by `confirm_live` until
   the READY marker comes back or `timeout` (default the session's
   `_init_timeout`) passes. **Never `_open()`**: the transport, and the
   application the hook navigated into, are kept.

That is the whole of "invoking the CommandFrame", and it is the existing
handshake code with a different frame and new markers. Bash sends
`stty -echo; echo READY`; Zephyr sends the bare token and matches its error
handler printing it back; the example project's `PyReplFrame` silences the
REPL's prompts and prints the token; a project frame supplies its own. No
unix signal, no `id -un`. A zsh landing followed by a hook that runs `exec
ash` needs no `landing_frame` at all: the bash and ash frames render the
same bytes and the repeat handshake is idempotent.

The `SessionManager` calls `enter_frame(command_frame)` **after every hook,
unconditionally**, whether or not the hook called it and whether or not the
two frames are the same object. That call is the post-hook confirmation, and
it is the same probe on every family. A hook that returns parked somewhere
that cannot answer the target frame, or with the transport gone, fails here
as `SessionSetupError`.

The identity is not re-read and `current_user` is not changed (decision 5).

### 2.5 The interactive bridge

Today `run_ssh_login`/`run_telnet_login` replay proxy hops over raw bytes
with echo on, then start the stdin/stdout pumps. A hook needs a framed
session, so for a host **with** a hook the bridge builds one:

- `_BridgeShellSession(ShellSession)` in `otto.host.interact`, over the
  bridge's existing `write_remote`/`read_remote` closures: `_open` and
  `close` are no-ops because the bridge owns the PTY; `_write` on the SSH
  bridge passes text through, and on the telnet bridge mirrors
  `TelnetSession._write` (every `\r?\n` becomes `\r`, not only a trailing
  one, so a multi-line payload is right); `_read_until_pattern` accumulates
  chunks the way `_BridgeProxyIO.expect` does. Built with
  `shell_history=True` so neither handshake carries a `HISTFILE` payload:
  `otto login` keeps its promise to leave a human's shell alone.
- Order on the bridge for a hook host, both transports:
  1. landing handshake on the bridge session (bash: echo off);
  2. proxy hops over a `_SessionProxyIO`-shaped adapter on that session — the
     resync probe is echo-proof in either mode, so this is the same exchange
     as today's raw replay, one layer up. That adapter logs each hop line
     into the session log where today's raw replay logs nothing; stated in
     the docs, and a password hop still sends with `log=NEVER`;
  3. the **landing** frame's `restore_interactive()` payload, if any, sent
     as a framed command: the hook may leave the landing shell for the
     application, and a `stty -echo` left behind on the pty would meet the
     human when they quit the application back to bash. The hook therefore
     runs with echo on. `BashFrame` tolerates that — its parser finds the
     last BEGIN marker and so skips an echoed frame — at the cost of the
     echoed frame text appearing in the bridge's session log, which is a
     transcript anyway;
  4. the hook over a `HostSession` wrapping the bridge session (`kind =
     "bridge"`; log sinks: the session log file's line writer; `creds` from
     the host so `as_user` inside a hook works where it works today;
     `name = "login"`, `deregister` a no-op);
  5. frame entry (§2.4);
  6. the **target** frame's `restore_interactive()` payload, if any;
  7. the bridge session's residual read buffer (a prompt printed after the
     last framed command, a banner the hook's `expect` did not consume) is
     written to stdout and the session log, so the human sees the prompt
     they are at;
  8. pumps and banner, as today.
- With a `raw` landing on the bridge, steps 1 and 3 send nothing (an empty
  handshake and no landing restore); the hook runs over the pty as the
  human would find it, and steps 5 to 8 are unchanged.
- A host without a hook takes today's bridge path byte for byte. The bridge
  session exists only to serve the hook.
- `CommandFrame.restore_interactive() -> str | None` is a new optional
  render-half method: the line that undoes what the frame's handshake did to
  the terminal for a human. Default `None` (nothing to undo). `BashFrame`
  returns `stty echo`; `ZephyrSerialFrame` returns `shell echo on`;
  `ZephyrFrame` inherits `None`; the example `PyReplFrame` restores the
  REPL's prompts. Dialect-owned for the same reason the handshake is.
- `run_ssh_login` and `run_telnet_login` gain the parameters this needs (the
  hook, landing and target frames, creds, host id); `UnixHost._login` passes
  them. Docker's `_login` call site is unchanged in version 1.
- Failure aborts the login before the human sees a prompt, with the same
  `SessionSetupError` and a one-line stderr notice, and the bridge's
  `finally` still closes the process and the session log.

### 2.6 Errors

- `_apply_session_setup` wraps **everything** inside itself — the hook's
  exception, and the `ConnectionError` a failed frame entry raises after
  `_fail_init` closes the session — as
  `SessionSetupError("{host_id}: session setup {name!r} failed: {cause}")`
  or `SessionSetupError("{host_id}: session setup {name!r} left no shell
  that answers frame {type_name!r}")`, raised from the cause. Wrapping
  inside the step matters: `_ensure_session`'s retry arm catches bare
  `ConnectionError` as a transport race and would otherwise retry a hook
  failure.
- `_ensure_session` treats `SessionSetupError` like `LoginProxyError`: tear
  the session down, propagate on the first attempt, never consume the
  handshake retry.
- `RawLandingError` from `run()` inside a hook propagates as itself: it is
  the hook author's bug, named for what it is, not a setup failure to wrap.
- Lab-load errors (unknown hook name, table without `type`, unknown
  `landing_frame`, `landing_frame` without `session_setup`, non-bash-family
  `landing_frame` with hops, `command_frame: "raw"`) are `ValueError`s from
  the spec validator,
  listing the registered names where a name was wrong, in the same place
  `proxy` and `command_frame` are validated.

## 3. Documentation

Each artifact below names the gate that proves it; §4.3 gives the order.

- `docs/guide/configuration/lab-config.md`: a `session_setup` row and a
  `landing_frame` row in the per-host field table beside `command_frame`,
  each linking to the host-options prose. **Gate:** the field-coverage guard
  (`tests/unit/docs/test_lab_config_field_coverage.py`) goes red the moment
  the fields land on `HostSpec` without these rows; it proves presence on
  this page and nothing else.
- `docs/guide/configuration/host-options.md`: a *Session setup* section with
  the field reference (`session_setup` as name or table, `type`, params;
  `landing_frame` and its coupling to `session_setup` and to hops), the
  establishment order from §2 with transport hops included, and the
  two-dialect verb table from §2.3 (works / unavailable). **Gate:** Sphinx
  `-W`, which proves links resolve and nothing more.
- `src/otto/examples/session_setup.py`: a doctested registration example
  mirroring `otto.examples.login_proxy`, and — because a registration
  doctest with `overwrite=True` can only fail for import-path reasons — it
  also exercises the two things a copier gets wrong:
  `session_setup_from_spec({"type": "...", "db": "x"}).params == {"db":
  "x"}` and the unknown-name did-you-mean error as a `Traceback ...
  ValueError` doctest. **Gate:** `make doctest-src` (`pytest
  --doctest-modules src/otto`), which the docs lane and CI run; `testpaths`
  excludes `src`, so this is the only lane that runs it.
- The getting-started example project gains two hooks and one frame, each
  in its own module under `libs/gs_example/` and imported from
  `__init__.py` inside its own `# doc: begin/end` block (without the import
  the name is unregistered at load):
  - `setup.py`: `provision-app`, the single-dialect hook on `test1` in lab
    `unix`. It exports `APP_ENV`, and on the `default` session provisions
    through the existing `PyRepl` `AppShell` (creating a marker file the
    e2e reads back), then returns to bash.
  - `pyrepl_frame.py`: `PyReplFrame`, `type_name = "pyrepl"`, mirroring
    `zephyr_inline.py`'s shape: the handshake silences `sys.ps1`/`sys.ps2`
    and prints the READY token; each command is rendered as one REPL line
    that prints BEGIN, runs the line, and prints END with a code that is 0
    unless the line raised (a one-line `try`/`except` around `exec`);
    `restore_interactive()` puts the prompts back. Single-line commands
    only, stated on the page.
  - `enter_python.py`: `enter-python`, the two-dialect hook, on a second
    host entry for the same test1 VM in its own lab `pyrepl`, declared with
    `landing_frame: "bash"`, `command_frame: "pyrepl"` and
    `session_setup: "enter-python"`: it checks `python3` is present with a
    framed `run()`, sends `python3 -u -i`, expects the first prompt, and
    returns; frame entry does the rest.
  - `land_on_zephyr.py`: `land-on-zephyr`, the raw-landing hook, declared on
    `zephyr37_lfs` in the existing lab `embedded` (an entry no other capture
    uses) with `landing_frame: "raw"` and `session_setup: "land-on-zephyr"`:
    it sends a newline and expects the Zephyr prompt, then returns; frame
    entry in the entry's existing `command_frame` does the rest. The
    transport is untouched: telnet to the console through the `test4` hop,
    as every Zephyr entry on the page already is.
  **Gates:** `test_every_lab_loads` runs the real `HostSpec` validator and
  `__post_init__` over all three entries after importing `gs_example`, and
  the unit lane pins each entry's `session_setup.name` and the `pyrepl` and
  `zephyr37_lfs` entries' frame types. The inventory twin of the `unix` lab declares
  no init module on purpose and cannot carry the hook; the twin comparison
  (`_ATTRS`) leaves `session_setup` out, and the page says the inventory
  example does not show it.
- `docs/getting-started/customizations.md`, *After connecting*: rewritten as
  two worked subsections including the fragments above by marker. The first
  shows `provision-app` with a capture of `otto --lab unix host test1 run
  'echo $APP_ENV'` and the hook's traffic in the log; the second shows the
  two-dialect shape with a capture of `otto --lab pyrepl host test1 run
  '1 + 1'` answering `2`, and one paragraph on when to reach for an
  `AppShell` instead (the application is a detour, not the destination). A
  third, shortest subsection shows the raw landing on `zephyr37_lfs` with a
  capture of `otto --lab embedded host zephyr37_lfs run "kernel version"`,
  and says what raw is for: a console whose landing state answers no frame,
  reached over the transport the entry already has.
  Per-session ids are redacted the way the Zephyr debug capture already
  does. The current paragraph's claim that otto runs no command of its own
  after a session opens is corrected: the handshake and history suppression
  are commands. **Gates:** `test_the_real_pages_resolve` (every included
  file and marker exists exactly once) and Sphinx `-W`. The captures are
  **tier 3** (`labless = false`): `make docs`, `make gate` and CI never
  re-run them, and `make docs-captures-check` is monitoring, never a push
  gate. There is no labless form — a dry run does not run the hook — so
  each capture's gated twin is the bed e2e in §4.2, which runs the same
  command against the same lab entry and asserts the same printed value.
- `docs/library/extending-backends.md`: a new *Session setup hooks* section
  beside *Login proxies*: the contract, `SetupContext.kind`, the landing and
  target dialects and `enter_frame()`, why there is no host object,
  identity untouched, the exec-route consequence, `restore_interactive()`
  for frame authors, and a copyable registration.
- `docs/library/connection-options.md`: one sentence distinguishing
  `SshOptions.post_connect` from `session_setup`.
- `docs/architecture/subsystems/hosts.md`: the establishment sequence gains
  the two new steps and the `_open`/`_handshake` split.

## 4. Testing

### 4.1 Unit (`tests/unit/host/`, `tests/unit/models/`, `tests/unit/docs/`)

One new fake is required and named here so the plan does not pretend
otherwise: the existing `ShellModel` answers password prompts and the bash
recover probe only, and `MockSession` is a passive stream fed by hand. A
`DialectShell` beside `ShellModel` recognises the bash handshake and frame,
a scripted transition line, and then a second dialect's handshake and frame
(the Zephyr token and frame, since that dialect ships with otto), and is
driven into `MockSession` by a feeder keyed on what was written.

Tests, each with what makes it able to fail:

- registry: register, duplicate refused, `overwrite`, did-you-mean on lookup,
  `session_setup_from_spec` for all four input shapes; spec validation
  errors for every §2.6 lab-load case, with the registered-names listing;
  `EmbeddedHostSpec` accepts `landing_frame`.
- transport is opened once: `_open` is called exactly once across landing
  handshake and frame entry (`MockSession._open` replaces its reader, so a
  test that feeds the target reply before `enter_frame` fails if frame
  entry reopens).
- fresh markers: the frame-entry handshake carries a different marker id
  from the landing one, and a READY reply for the *landing* markers left in
  the stream does not confirm frame entry.
- ordering with hops: the hook's first send lands after the last hop's
  resync; `current_user` is the proxied login inside the hook; the hop
  resync probes carry the **landing** frame's history prefix on a
  two-dialect host with `shell_history=False`.
- two dialects on `DialectShell`: the hook's pre-transition `run()` is
  bash-framed; `enter_frame()` sends the second dialect's handshake; a
  `run()` after it is framed in the second dialect; a hook that navigates
  away and returns without entering the frame still ends in the second
  dialect; `enter_frame()` twice sends two handshakes and both are answered.
- confirmation on a **same-frame** host: a hook that leaves the shell inside
  a REPL fails as `SessionSetupError` from frame entry (this is the test
  that catches a skipped confirmation); the frame-entry handshake carries
  the target frame's history prefix on a bash-family target with
  `shell_history=False` (on a Zephyr target the prefix is empty and the
  assertion could not fail).
- refusals: `enter_frame()` inside an attached `AppShell` raises
  `AppShellActiveError`; `enter_frame()` on a dead session raises
  `SessionSetupError`; `session.close()` inside the hook raises.
- raw landing on `DialectShell` with a non-echoing "menu" state that moves
  only on a scripted line: the landing writes no bytes before the hook (the
  fake records zero writes ahead of the hook's first send); `run()` in the
  landing raises `RawLandingError` and the session stays alive; a hook that
  sends the scripted line then returns ends in the second dialect after
  exactly one handshake; a hook that never sends it fails at frame entry as
  `SessionSetupError`; `command_frame: "raw"` and `landing_frame: "raw"`
  with hops are both refused at load.
- a hook that raises mid-`AppShell`: the app-shell lock is released, the
  session is closed, the error is `SessionSetupError` from the cause.
- frame entry after an `AppShell` exit: the exit's recovery confirmation is
  followed by exactly one target handshake.
- named sessions and the exec route: `open_session` runs the hook with
  `kind="named"`; a Unix host with a hook and no hops takes the pooled route
  for `exec` and the hook sees `kind="exec_pool"`; a hook that raises on
  `exec_pool` surfaces from `exec` as `SessionSetupError`.
- once-only: a second `run()` after the session is up sends nothing from the
  hook; `as_user` does not re-run it.
- failure teardown and wrapping: the session is closed, no retry, the error
  names host and hook (and the frame for frame entry); a failed frame entry
  does not trip `_ensure_session`'s retry (the fake counts `_open` calls).
- dry run: with a hook declared, `host.run` under a dry run opens nothing and
  the hook is never called.
- bridge: with the recorded `write_remote`/`read_remote` closures used by
  `TestReplayProxyHops`, a hook host produces landing handshake, hops, the
  landing `stty echo`, the hook's framed command, the target handshake, the
  target `restore_interactive()`, in that order, and the pumps start only
  afterwards; the telnet bridge variant sends `\r` for every newline of a
  multi-line payload; a host without a hook produces **the literal byte
  list** today's path produces, pinned the way `TestReplayProxyHops` pins
  hop bytes rather than compared against the same code path.
- frames: `restore_interactive()` for the three built-ins, and the default
  `None` for a minimal project frame; `PyReplFrame` render and parse on
  canned REPL output (a raising line yields a non-zero code; a printing line
  yields its output and 0), the way `test_gs_example_parsers.py` tests the
  example parsers.
- docs guards: `test_lab_config_field_coverage`, `test_every_lab_loads`,
  `test_the_real_pages_resolve`, and the pins on both example entries.

### 4.2 End-to-end (bed-backed, existing markers)

- `tests/e2e/host/test_session_setup_e2e.py` loads the **getting-started
  labs** (`load_example_lab(...)` as the unit lane does) and uses the hooks
  and frame `gs_example` registers — not hosts or hooks of its own. It
  leases `test1` explicitly (the pool leases any of test1–3 by element name;
  the hooks are declared on test1 only).
  - Single-dialect, lab `unix`, in-process leg: assert `APP_ENV` over the
    persistent session, over a named session, and over `exec`; assert the
    marker file the `PyRepl` provisioning wrote exists; assert the printed
    value equals what the page's capture shows. Bridge leg: `otto --lab unix
    host test1 login` with `sut_dirs` pointing at the example project,
    driven the way `test_login_user_over_bridge` is; assert the human lands
    at a bash prompt where `echo $APP_ENV` prints the value, and that echo is
    on by asserting the typed line is echoed before its output.
  - Two dialects, lab `pyrepl`, in-process leg: `run("1 + 1")` answers `2`
    with code 0 and `run("1 / 0")` answers non-zero, over the persistent
    session, a named session, and `exec` (each pooled session enters its own
    REPL); assert the printed value equals the page's capture. Bridge leg:
    `login` lands the human at a `>>>` prompt with echo on, and typing
    `exit()` returns them to a bash prompt that echoes. This is the gate for
    decision 6 on real bytes.
  - Raw landing over the real serial transport, lab `embedded`, entry
    `zephyr37_lfs`, leased under the embedded marker the way the existing
    Zephyr e2e lease their guests: `run("kernel version")` answers over the
    default session and a named session; the session log shows no bytes
    written before the hook's newline. This proves the raw engine on the
    SSH-hop-then-telnet transport on real bytes. What it does not prove, and
    the spec says so: a landing state that echoes nothing — the guest's
    console is already at the Zephyr prompt — which has no target on the bed
    and stays gated on the `DialectShell` menu test alone.
- Single-dialect ash: a hook that runs `exec ash` from the landing shell on
  a BusyBox host declared `command_frame: "ash"` and no `landing_frame`,
  confirming the same-bytes claim in §2.4.
- The two getting-started captures are refreshed with `make docs-captures
  --only <id>` after the e2e is green, and committed in the same change.

### 4.3 The order, and the gate that proves each step

1. Registry, `HostSpec` fields, runtime fields, manager step, `_open` /
   `_handshake` split, `enter_frame`, bridge session, `restore_interactive`,
   with the §4.1 tests → `make coverage-unit`; the spec/runtime drift guard
   forces `HostSpec` and both runtime classes to change together.
2. `lab-config.md` rows and `host-options.md` prose → the field-coverage
   guard and `make docs`.
3. `src/otto/examples/session_setup.py` and the lazy exports → `make
   doctest-src` (the doctest imports both names from `otto`).
4. `gs_example/setup.py`, `pyrepl_frame.py`, `enter_python.py`,
   `land_on_zephyr.py`, their import blocks, the `unix` entry's
   `session_setup`, the `pyrepl` lab entry and the `zephyr37_lfs` entry's
   two fields → `test_every_lab_loads`, the pins, and the `PyReplFrame` unit
   tests.
5. `customizations.md` rewrite → `test_the_real_pages_resolve` and Sphinx
   `-W`.
6. The bed e2e loading the getting-started labs, both legs of both
   subsections → the integration lane. This is the only gate that proves
   the documented hooks work.
7. The tier-3 captures, refreshed and cross-checked by step 6.
8. The BusyBox `exec ash` leg.
9. `extending-backends.md`, `hosts.md`, `connection-options.md` prose →
   the docs lane.

## 5. Non-goals

- More than two dialects per session, or a hook switching to a frame other
  than the host's declared `command_frame`.
- Re-reading or asserting identity after the hook.
- Running the hook on the raw stateless exec channel, on `exec(user=...)`,
  or on `as_user`/`switch_user` transitions.
- The POSIX-assuming Unix verbs on a two-dialect host (decision 10).
- A target that is a separate machine reached through a nested console
  program run from the landing shell (decision 13). A console that is the
  entry's own transport is in scope (decision 16).
- A bed proof of a landing state that echoes nothing (decision 15); the raw
  engine is gated on the scripted fake and on the real serial transport
  landing at a prompt.
- `LocalHost` and `DockerContainerHost` declaring a hook (decision 9).
- An embedded `login` bridge, or letting the embedded family land on a Unix
  box (telnet with login, `term: ssh`); both unchanged here.
- More than one hook per host; composition is Python's job.
- Multi-line commands through `PyReplFrame`; it is an example frame, not a
  Python driver.
