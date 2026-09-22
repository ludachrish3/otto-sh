# `exec` is the host CLI verb, and it reaches every user

**Status:** draft for review, 2026-09-21.
**Breaking:** yes — `otto host <id> run` is removed. Ships as a `feat(host)!` in
0.16.0 (0.15.0 is already tagged).

## 1. Goal

`otto host <id> exec "whoami" --user root` runs one command on the host as
`root` and exits, whether root has a direct login or is reachable only through
proxy hops, over ssh or telnet. The host CLI verb is the `Host` method that
takes `user=`, and that method is `exec`.

The rationale is the owner's, stated during design: *the host command is a
convenience one-shot. Because it is a CLI command with a concrete end, it
cannot really support an ongoing session. That paradigm matches `exec`'s verb
more closely.* `run`'s defining property — shell state (`cd`, exported
variables) persisting *between calls* — cannot survive the process exit that
ends every CLI invocation, so the CLI verb has only ever behaved like `exec`
while paying to open a stateful session it then discards. `docker exec` is the
precedent for the name.

### 1.1 How `run` and `exec` relate

One rule, stated once: **`exec(cmd)` runs *cmd* on a session you do not keep.**
`run` is the same execution on the session you do keep. A family may satisfy
`exec` with a raw channel (ssh `create_process`, a local subprocess, `docker
exec`) *when the call needs nothing a shell provides* — that is an
optimization, not a second meaning, and it is the reason `exec` is
concurrency-safe and byte-exact where `run` is neither. Anything a raw channel
cannot do (answer a prompt, elevate, switch user, replay proxy hops, run a
`session_setup` hook), `exec` does the way `run` would, on a pooled session.

The code already says this for the pooled route — its body is
`(await exec_session.run(cmd, ...)).only` — and `_exec_route` already states
the guards in its own words. This spec makes the rule the design rather than
an observation, and closes the places where the two verbs had drifted (§3,
D9–D12).

## 2. Today

- `BaseHost.run` carries `@cli_exposed`, so the CLI verb is `run`
  (`src/otto/host/host.py`). `exec` is not exposed.
- `run(user=)` refuses on every family but docker. The refusal
  (`src/otto/host/remote_host.py`, `run`'s `user is not None` arm) says
  *"on unix, exec/put/get accept user= directly"* without asking whether the
  named user can be served that way.
- `exec(user=X)` on an ssh-term unix host opens a connection authenticated as
  X (`ConnectionManager.ssh_as`), and `_direct_cred_for` refuses any X
  reachable only through proxy hops: *"stateless per-user auth cannot replay
  proxy hops — use login(user=...) or as_user() instead"*. On a telnet-term
  unix host `_refuse_exec_user` refuses `exec(user=)` outright.
- So a user whose root is a proxy login meets two refusals in a row, and the
  second one names `as_user()`, which the CLI cannot reach.
- `exec` already reaches the *login target* statelessly when that target is
  proxied: `SessionManager._exec_route` sends it to `POOLED_SHELL`, a pooled
  named shell session that replays the hops and runs any `session_setup` hook.
  The gap is only a `user=` that differs from the login target.
- The capability grid (`src/otto/host/capability_grid.py`) declares unix
  `exec_user=authenticate`, which already overstates the telnet case.

## 3. Decisions

| # | Decision |
| --- | --- |
| D1 | `exec` becomes the `@cli_exposed` host verb. `run` loses `@cli_exposed` and stays a Python-only stateful method. **Hard cutover** — no alias, no shim; `otto host <id> run` becomes an unknown command. |
| D2 | `exec` gains `sudo: bool = False`. |
| D3 | `exec(user=X)` for a proxy-only X runs on a pooled shell session switched to X for the one call, then switched back. |
| D4 | A pooled session whose switch-back fails is **discarded**, never returned to the pool. |
| D5 | `exec(sudo=True)` always takes `POOLED_SHELL`; it never tries a raw channel first. |
| D6 | `exec` keeps its `user=` for Python callers (it was never in question); `run(user=)` keeps its current behaviour (docker `chown`, refused elsewhere). |
| D7 | The CLI takes **one command as one quoted string**, mirroring `exec(cmd: str)`. |
| D8 | New `UserSupport.switch`; unix `exec_user` becomes `switch`. |
| D9 | **One shared core, and the session-based route is the fallback.** `BaseHost.exec` owns normalisation, elevation, refusal ordering and the dry-run arm for every family, and hands `_exec_one` a resolved command plus the call's *needs_shell*. For every session-bearing family that lands in `SessionManager.exec`, whose pooled arm is literally `run` on a throwaway session (acquire, `run`, release — or discard, D4); the raw channels are per-family fast paths `_exec_route` selects only when the call needs nothing a shell provides. Families with no shell session behind `exec` (local, docker) keep their own primitive and refuse what it cannot do. |
| D10 | `exec` accepts `expects=` and a single `ShellCommand`, the same single-command shape `run` accepts. `expects` forces the pooled route. Sequences and the cumulative timeout stay `run`-only — they are what a kept session is *for*. |
| D11 | **Ambient identity applies to `exec`.** Inside `async with host.as_user(X)`, `exec(cmd)` with `user=None` runs as X, exactly as `run` does. Today it silently runs as the login user. |
| D12 | **`run(user=X)` on unix becomes a scoped switch for that call** — `async with as_user(X): run(cmds)` — instead of a refusal, and unix `run_user` becomes `switch`. Additive and separable; it is the mirror of D3 so that both verbs answer `user=` the same way on every family (docker `chown`, unix `switch`, embedded/local refused). |

D12 is not the rejected "approach A" below returning: A was rejected as the
*CLI* fix, because it kept a session-named verb the CLI cannot honour. D12 is
Python API parity, and the CLI verb is still `exec`.

Rejected alternatives, for the record:

- **Make `run(user=)` work via `as_user` and keep `run` as the verb.** It
  solves the reported failure, but keeps a CLI verb named for a persistent
  session the CLI cannot have (§1).
- **A pool keyed per user** instead of D3's switch per call. It saves the
  switch on reuse, which a one-shot CLI never gets, and adds a second pool
  dimension to get wrong.
- **Dispatch `--user` on cred shape between `exec` and `as_user`.** It makes
  stateful-versus-stateless depend on how the lab file spells a cred. D3 keeps
  both paths stateless; only the authentication mechanism differs, and §7
  publishes that difference.
- **Unquoted docker-style argv (`exec ls -la /tmp`).** It needs option
  pass-through on the verb, lets the local shell expand `$VARS` before otto
  sees them, and turns the old multi-command habit (`"cd /tmp" "ls"`) into a
  silently garbled single command instead of a usage error.

## 4. The `exec` contract

```python
async def exec(
    self,
    cmd: str | ShellCommand,
    expects: Expect | list[Expect] | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    log: LogMode = LogMode.NORMAL,
    sudo: bool = False,
    user: str | None = None,
) -> CommandResult
```

Parameter for parameter this is `run`'s signature minus the sequence: the
single-command shape is identical, and the parameter order matches `run` so a
call site can move between them by changing the verb. The CLI exposes
`COMMAND`, `--timeout`, `--user` and `--sudo`; `expects` and `log` are
`Exclude`d, as they are on `run`.

Validation stays in `BaseHost.exec`, above the dry-run arm, so a call a family
could never honour refuses under `--dry-run` too. `_refuse_exec_user` keeps that
role for `user`; `sudo` gets the same treatment (§4.3).

### 4.1 Routing

`SessionManager._exec_route` remains the single place that decides, so
`exec_line_budget` keeps reading the same answer `exec` acts on. It gains the
call's `user` and `sudo` as inputs.

| Call | Route | Why |
| --- | --- | --- |
| plain, direct login | raw fast path (`FACTORY` / `SSH_CHANNEL`) — **unchanged** | byte-identical for today's callers |
| plain, proxied login, `session_setup` host, or telnet | `POOLED_SHELL` — **unchanged** | already how it works |
| `sudo=True` | `POOLED_SHELL` | elevation may answer a password prompt (`_elevate` returns an `Expect`); raw channels have no pty |
| `expects` given (D10) | `POOLED_SHELL` | a prompt needs a pty |
| `user=X`, X has a direct cred, ssh term | `ssh_as(X)` channel — **unchanged** | X's real credentials |
| `user=X`, any other case | `POOLED_SHELL` + switch to X (§4.2) | replays X's `via` chain, and serves telnet |
| `user=None` inside `as_user(X)` (D11) | as `user=X` | ambient identity; `current_user != login` is the test |

### 4.2 The switched pooled session

1. Take a session from the `POOLED_SHELL` pool.
2. `perform_switch` to X — the engine `as_user` / `switch_user` use, which
   replays each hop of X's `via` chain.
3. Run the command (elevated if `sudo=True`: switch first, then elevate as X —
   the same meaning `async with as_user(X): run(..., sudo=True)` has today).
4. Undo the switch, innermost hop first, shielded as `as_user`'s undo is.
5. Return the session to the pool — **unless step 4 failed**, in which case the
   session is closed and dropped (D4). Returning it would let the next pooled
   `exec` run silently as X.

A switch that fails at step 2 raises the existing `LoginProxyError` naming the
user and the hop; the session is dropped, since a partial switch leaves it in an
unknown identity. A failed undo logs a warning naming the host and the user and
still returns the command's result — the command ran.

Pooled sessions are already independent of the default session, so nothing
switched here is visible to a later `run`.

### 4.3 Families

| Family | `exec(user=)` | `exec(sudo=True)` |
| --- | --- | --- |
| unix, ssh term | direct cred → `ssh_as`; otherwise switch | pooled shell |
| unix, telnet term | switch (**new** — was refused) | pooled shell |
| docker | `docker exec -u` (`chown`) — unchanged | open — see below |
| embedded | refused — unchanged | refused, as `run(sudo=True)` is (no `_elevate`) |
| local | refused — unchanged | **refused**, naming `run(sudo=True)`, which keeps working |

**Local.** `LocalHost.run(sudo=True)` elevates today (`PosixPrivilege._elevate`
with no userland wraps the command in `sudo -S`). `exec` on local is a bare
subprocess with no pty to answer a prompt on, so `exec(sudo=True)` refuses and
its message points at `run(sudo=True)`.

**Docker.** `exec` is `docker exec … sh -c <cmd>` sent through the PARENT
host's `exec` — a raw channel with no pty — so the `sudo -S` wrap `run` uses
cannot answer a prompt there. `exec(sudo=True)` on a container is therefore
**`docker exec -u root`**: no prompt, no `_elevate`, and the result runs as
root in the container, which is what `sudo` means there. `exec(user=X,
sudo=True)` on a container is a refusal: the two both choose the `-u`.

## 5. The CLI

```bash
otto host test1 exec "uname -a"
otto host test1 exec "cd /tmp && ls -la" --user root
otto host test1 exec "systemctl restart foo" --sudo
```

`otto host <HOST_ID> exec [OPTIONS] COMMAND`, synthesized from `@cli_exposed`
on `BaseHost.exec`:

| Option | Default | Meaning |
| --- | --- | --- |
| `COMMAND` | — | One shell command, quoted as one argument. |
| `--timeout S` | `DEFAULT_COMMAND_TIMEOUT` | Seconds before the command is abandoned; `inf` for unbounded. |
| `--user U` | — | Run as this user — direct or proxy login (§4.2). |
| `--sudo` | off | Elevate through the host's resolved mechanism. |

The verb keeps `run`'s per-verb output-dir behaviour. Two positional commands
are a usage error — click's own, exit 2, *"Got unexpected extra argument
(…)"*, raised before the verb runs (the synthesized verbs have no hook that
runs earlier, and a hand-raised `UsageError` escapes Typer's vendored click).
The guidance lives where the user looks next: the argument's `--help` text
says *one shell command, quoted as one argument; join steps with `&&` on a
POSIX shell*.

## 6. Errors

| Where | Change |
| --- | --- |
| `run(user=)` refusal (`remote_host.py`) | Gone on unix (D12). Where it remains (embedded, local) it names a remedy true for that family — and never one the CLI cannot reach. |
| `UnixHost._refuse_exec_user` | The telnet refusal is removed; telnet reaches §4.2. |
| `_direct_cred_for` | Unchanged — `put`/`get` still refuse proxy-only users. `exec` no longer reaches it for them. |
| Switch failure (§4.2 step 2) | Existing `LoginProxyError`. |
| Undo failure (§4.2 step 4) | Warning plus a discarded session; the result is returned. |

## 7. The capability grid

New member:

> **`switch`** — *The verb runs in a shell session switched to that user,
> reached the way an interactive login reaches them — directly, or by
> replaying the proxy hops their cred declares. On an SSH host, a user with a
> direct cred is instead authenticated on a connection of their own.*

Unix `exec_user`: `authenticate` → `switch`, and with D12 unix `run_user`:
`refused` → `switch` too, so the two verbs read the same on every row. Unix
`put`/`get` stay `authenticate`; docker stays `chown`. The unix row's `note`
drops "`exec(user=)` additionally requires `term="ssh"`".

The conformance probe (`src/otto/testing/conformance_host.py`) already treats
every non-`refused` member as "a dry-run call with `user=` completes
cleanly", so `switch` needs no new probe arm — but it constrains D12: under a
dry run, `run(user=X)` on unix must decline the way a plain dry-run `run`
does, **not** enter `as_user` (whose dry-run arm raises
`CommandNotRunError`). The switch happens only on a live run.
`docs/cli/host/families.md` is generated from the enum by
`scripts/render_support_matrix.py`, so the new member's meaning reaches the
page without a hand edit. The support-matrix page renders member meanings from the enum, so it
follows without a hand edit; the conformance probe must learn the new member.

## 8. Migration

Complete inventory as of `97c88056`, found by grepping for same-line
`"host", …, "run"` argv (29 sites) and then for multi-line argv builds.

**Tests — 16 files with same-line sites:**

- `tests/unit/cli/test_host.py`, `tests/unit/cli/test_dry_run_seam.py`,
  `tests/unit/scripts/test_refresh_docs_captures.py`
- `tests/e2e/cli/test_exit_codes_e2e.py`, `tests/e2e/cli/test_dry_run_preamble_e2e.py`
- `tests/e2e/host/test_local_host_e2e.py`, `test_host_transfer_e2e.py`,
  `test_host_priv_modules_e2e.py`, `test_remote_completion_e2e.py`
- `tests/e2e/cov/test_docker_image_coverage_e2e.py`
- `tests/e2e/chaos/test_connection_drop.py`, `test_console_chaos.py`,
  `test_harness.py`, `test_session_chaos.py`
- `tests/integration/chaos/test_harness.py`, `test_signal_run.py`

**Plus multi-line argv builds to check by reading:**
`tests/e2e/docker/test_docker_e2e_cli.py` and the shared helper
`tests/e2e/_otto_subprocess.py`.

Two of those need care:

- The six **chaos** files sit in a lane no default gate runs, so they are run
  explicitly.
- Anything under `tests/integration/` SSHes to the lab and reaps it.

Sites that exercise the persistent session *across multiple commands in one
invocation* (the old `run "cd /tmp" "ls"` shape) are rewritten with `&&`, not
dropped — they are the behaviour D7's usage error replaces. **One site cannot
be:** `tests/e2e/chaos/test_console_chaos.py:212` queues **forty** copies of
`kernel version` in one `run` on a Zephyr console so that a SIGKILL lands
with work still queued, and a Zephyr shell has no `&&`. That test is
redesigned rather than joined: the keep-busy becomes one `exec` of a command
that stays busy on the console for the window the test needs, and the test's
own assertions (kill lands mid-command; the console recovers) are what it
keeps. The usage error's "join with `&&`" wording is right for the POSIX
shells every other site targets, and the docs page says so rather than
implying a Zephyr shell can join.

**Docs:** `docs/cli/host/run.md` → `exec.md`; `docs/cli/host/index.md`,
`docs/cli/dry-run.md` and `docs/cli/host/capabilities/privilege.md`; nav and
cross-links. The page documents one command per call, `--user` (proxy logins
included) and `--sudo`, and **links** to the Python `run`/`exec` distinction
rather than restating it.

**Captures:** the five `docs/examples/getting-started/captures.toml` entries
that use `host … run` (`zephyr37-fat`, `zephyr27-fat`, `test1`, `test1-py`,
`zephyr37-lfs`) move to `exec` and are **re-recorded on the bed** with
`scripts/refresh_docs_captures.py`. A capture is a transcript, so it is never
hand-edited.

**Not migrated:** historical specs and plans under `docs/superpowers/`. They
are records of what was decided at the time.

## 9. Testing

**Unit.**

- Routing: one test per §4.1 row asserting the selected route. The plain rows
  pin today's fast path.
- Discard-on-failed-undo, **by injection**: force the undo to raise, assert
  the session is not returned, and assert the next `exec` opens a fresh one.
- Switch failure at step 2 drops the session and raises `LoginProxyError`.
- Dry-run ordering: refusing families refuse `user` and `sudo` above the
  dry-run arm.
- CLI synthesis: `exec` present, `run` absent, the three options, and the
  two-command usage error.
- Grid: unix `exec_user == switch` (and `run_user`, D12), and the conformance
  probe accepts it.
- Parity (D9–D12): `exec(ShellCommand)` and `exec(expects=)` answer a prompt;
  `exec` inside `as_user(X)` reports X from `whoami` — asserted against a
  fake whose raw channel would have answered the login user, so the test
  fails if D11 regresses to the fast path; `run(user=X)` on unix switches and
  restores, and leaves `current_user` as it found it.
- Differential: for a single plain command, `exec(cmd)` and
  `run(cmd).only` return equal `CommandResult` fields (status, retcode, value)
  on every family with a session — the one-sentence rule of §1.1, as a test.

**Repo-wide invariants — named in every gate, since targeted runs miss them:**

- the public API snapshot (`exec` gains `sudo`, `run` loses its verb);
- the error taxonomy;
- the import budget.

**On the bed — the real hopped topology, never a loopback proxy:**

- `otto host <id> exec whoami --user <proxy-only user>` prints that user. This
  is the reported failure, end to end.
- `exec --user` on a telnet-term unix host.
- `exec --sudo` on a host whose userland needs a password.

**Acceptance — must hold before this merges:**

- **pyrepl (`test1-py`).** `exec "1 + 1"` lands in the Python REPL the
  `session_setup` hook opens, and returns `2`. `POOLED_SHELL` runs the hook,
  but whether it frames a result correctly in a non-shell interpreter has not
  been proven.
- **Embedded (`zephyr37-*`, `zephyr27-fat`).** `exec` on the shared console
  returns what `run` did.

If either fails, implementation stops and the finding goes back to review; it
is not worked around.

**Gates:**

- the targeted tests plus `make coverage`;
- `make check-python`;
- `nox -s tests_hostless-3.14`;
- the six chaos files, explicitly;
- `make docs`, plus `make docs-captures-check` against the bed.

## 10. Out of scope

- `put`/`get` reaching proxy-only users. They keep `authenticate`, and the
  same switch path could serve them later.
- Unquoted argv for `exec` (§3).
- Merging `run` and `exec` into one method with a `session=` parameter. The
  transports genuinely differ (§1.1), it would be a second breaking change
  across ~68 product call sites, and after D1 the user-facing surface is
  already one verb.
- `app_shell` and `exec`: the pooled route runs the `session_setup` hook, a
  raw channel cannot, so an `exec` inside `app_shell` is served correctly only
  on the pooled route today. The §9 pyrepl acceptance check covers the
  reported shape; making the wrapper force the pooled route the way D11 does
  for identity is the same class of fix, deferred until the check says it is
  needed.
