# Shell history suppression

**Status:** design approved 2026-07-21, awaiting spec review
**Branch:** `worktree-shell-history-suppression`
**Predecessors:** none — this is a self-contained addition to the session handshake
**Successor:** none named

## Goal

The commands otto runs on a unix host land in that host's shell history file, burying a
human's own history under automation traffic. otto should leave no trace in
`~/.bash_history` (or the equivalent) on any unix host it drives — old or new, glibc or
busybox, RHEL or Ubuntu — without breaking on shells that lack the knobs involved.

Suppression is **on by default** and opt-out per host.

## Where history is actually polluted

Only the *persistent, PTY-backed, interactive* shells. That is a smaller surface than it
first appears, and the difference decides the whole design.

| Path | Shell it gets | Pollutes? |
| --- | --- | --- |
| SSH session (`session.py:659`) — `create_process(term_type="dumb")` | interactive non-login bash; reads `~/.bashrc`, writes `~/.bash_history` at exit | **yes** |
| Telnet unix session | `login(1)` → full login shell on a tty | **yes** |
| Docker session (`session.py:936`) — `docker exec -it … sh` | interactive ash/bash with a TTY | yes, but discarded with the container |
| `host.exec()` SSH branch (`session.py:1509`) | plain exec channel, no PTY → bash disables history outright | no |
| `LocalSession` (`session.py:791`) | `bash --norc --noprofile` over pipes, non-interactive | no |

So the fix belongs on the interactive-session path, and `exec` needs nothing. This also
means suppression cannot be done by setting an env var at connect time: the exec channel
that would carry it is not the channel that has the problem, and telnet has no env
channel at all.

## The suppression payload

```sh
{ command export HISTFILE=/dev/null; } 2>/dev/null || :; { command set +o history; } 2>/dev/null || :; case ${ZSH_VERSION:-} in ?*) eval 'export HISTFILE=/dev/null' 2>/dev/null || :;; esac;
```

| Element | Why |
| --- | --- |
| `HISTFILE=/dev/null` | The load-bearing part. Honored by bash, busybox ash, zsh and ksh — though *how* it must be set differs per shell (see the `command` and zsh rows). Deliberately **not** `unset HISTFILE`: ksh falls back to `~/.sh_history` when it is unset, so unsetting is the one portable-looking option that silently fails on the oldest targets. |
| `export …=…` as one statement | Sets and exports in a single guarded command, so it inherits into subshells *and* is protected by the `command` prefix. A bare `HISTFILE=…` assignment cannot be guarded — see the `command` row. |
| `set +o history` | Also stops the *in-memory* list, so an rc file's `PROMPT_COMMAND='history -a'` — common in shared-history setups — has nothing to append. Unsupported on ash/dash. |
| **`command` prefix** | Guards against **two separate POSIX fatality rules**, either of which strands the `echo <READY>` sharing this line and takes a working host OFFLINE. (1) `set` and `export` are both **special builtins**, and an error in one aborts the shell — dash exits on the spot, and `&#124;&#124; :` does not save it because dash leaves before any status is tested. (2) A failed **variable assignment** aborts the rest of the compound line — so on a shell with `readonly HISTFILE`, or under `bash --restricted` (which forbids setting HISTFILE by name), a bare `HISTFILE=…` kills the handshake. `command` strips special-builtin status, making both survivable, and the builtin still takes effect where supported (bash reports `history off`). |
| `&#124;&#124; :` | Pins `$?` to 0. This payload also prefixes the resync probe, whose marker bakes in `$?` — without it dash emits `…RECOVER__2__` and busybox `__1__`. Harmless to the liveness regex (digits are all it needs) but it reads as a failure in a debug log. |
| **zsh `case` clause** | zsh's `command` is a *precommand modifier* restricting lookup to EXTERNAL commands (the familiar "`command cd` doesn't work in zsh"), so both statements above are silent no-ops on a zsh login shell — safe, but suppressing nothing. A bare `export` is not the fix: zsh aborts the line on a readonly failure just like the others. `eval` is both effective and survivable there. It is *not* usable as the general mechanism — dash strands on `eval` even in a clean environment and busybox strands under readonly — hence a zsh-scoped clause. `case` keeps it dependency-free and `${ZSH_VERSION:-}` is `set -u` safe; inert everywhere else. |
| HISTFILE statement first | The load-bearing half lands before the more failure-prone option statement. |
| `2>/dev/null` on a **brace group** | Session stdout and stderr are one merged stream; an unrecognized-option message would corrupt the READY handshake parse. Redirecting the *group* rather than the simple command is required for ksh, which reports a readonly assignment from outside the command's own redirection — `command export … 2>/dev/null` still leaks that line there. |

None of `command`, `&#124;&#124; :`, or the zsh clause was in the original design, and the original design's bare `HISTFILE=…` assignment was actively dangerous. All three defects were found by *running the payload*, never by reading it — see "Discovered during implementation" below.

**`HISTSIZE=0` is rejected, not merely unused.** It is destructive: bash writes its
(now empty) history list over `HISTFILE` at exit, deleting the user's real history. Any
future change here must preserve that reasoning.

The payload emits nothing on stdout and is idempotent — both required, because
`confirm_live` *resends* the handshake on a loop until one lands.

### Why a prefix and not a per-command wrapper

Wrapping each command (`HISTFILE=/dev/null <cmd>`, or leading-space plus
`HISTCONTROL=ignorespace`) was rejected: it lands inside the textual-prefix composition
hazard that `daemon.py:63-80` already documents against `sudo -S -p '…' {cmd}`, it adds
bytes to every command, and a leading space silently does nothing unless `HISTCONTROL`
happens to be set. Spawning a shell we fully control (`HISTFILE=/dev/null sh`) was also
rejected: it discards the user's rc environment, and it is impossible over telnet where
`login(1)` picks the shell.

## Design

### Config surface

One field on `UnixHost` (`unix_host.py:115`):

```python
shell_history: bool = False
"""Whether otto's commands are recorded in the host's shell history.

Default False: otto neutralizes HISTFILE on every interactive shell it
opens, so automation traffic doesn't bury a human's own history. Set True
on a host where otto's commands should be auditable from the shell's own
history file.
"""
```

Mirrored in `UnixHostSpec` (`models/host.py:236`) — mandatory, or the host/spec drift
guard fails.

Cascade: `[os_profiles.<name>]` defaults merge beneath a host's own `lab.json` field
(`merged = {**profile.defaults, **host_data}`, `factory.py:44`). The field is **not**
added to `_HOST_PREFERENCE_CAPABILITIES` (`models/settings.py:272`) — that gate accepts
menu-style capabilities whose values are ordered backend lists, and a bool does not
belong there.

Scoped to `UnixHost` only. `LocalHost` and `DockerContainerHost` are siblings of
`UnixHost`, not subclasses, so neither inherits the field — and because the internal
`shell_history` parameter defaults to `True` (see below), **neither gets suppression at
all**. That is intended: `LocalHost` has nothing to suppress (non-interactive pipes),
and a container's history dies with the container, so adding a knob there would be
surface without a beneficiary.

### Dialect layer

New method on `CommandFrame` (`command_frame.py`):

```python
def quiet_history(self) -> str:
    """Statement prefix that stops this dialect's shell recording commands.

    Empty when the dialect has no such notion. Prepended to the first line
    written into a fresh shell, so it MUST emit no output and MUST be
    idempotent (confirm_live resends the handshake on a loop).
    """
    return ""
```

`BashFrame` overrides it with the payload above, trailing `"; "` included.

**The empty default is the exclusion mechanism.** `ZephyrFrame`, `ZephyrSerialFrame`
and any third-party frame inherit `""` and are untouched — no `isinstance` checks and
no host-family branching anywhere in the change. Zephyr's shell history is a RAM ring
buffer with no persistence, so there is nothing to suppress there anyway.

A module-level helper centralizes the frame-defaulting rule that `session.py:125` and
`login_proxy.py:181` each hardcode today:

```python
def history_prefix(frame: CommandFrame | None, shell_history: bool) -> str:
    """Prefix that suppresses history recording, or "" when history is kept.

    *frame* None ⇒ BashFrame, matching the session-layer default.
    """
    if shell_history:
        return ""
    return (frame or BashFrame()).quiet_history()
```

**This guard clause is the only negation in the design.** `shell_history` carries one
name and one meaning — "are otto's commands recorded in the host's shell history" — at
every layer, and is passed through verbatim rather than inverted at any boundary.

### Session layer

`ShellSession` and `SessionManager` each gain `shell_history: bool = True`, forwarded
exactly like the existing `command_frame` parameter (`SshSession`, `TelnetSession` and
`_DockerSshSession` already thread that one through; the new parameter rides alongside).

`_ensure_initialized` (`session.py:248`) hands `confirm_live` a composed callable rather
than the bare method:

```python
prefix = history_prefix(self._frame, self._shell_history)
handshake = lambda m: prefix + self._frame.handshake(m)
```

Composing into the *existing* handshake line costs zero extra round-trips and keeps the
whole thing invisible to the frame parser.

`UnixHost` passes `shell_history=self.shell_history` at both `SessionManager`
construction sites (`unix_host.py:366`, `unix_host.py:394`) — a plain pass-through, not
an expression.

The default differs by layer, deliberately: `False` on `UnixHost` (the product decision —
otto should not pollute) and `True` on the session internals (preserve existing behavior
for every caller that does not opt in — `LocalHost`, `DockerContainerHost`,
`EmbeddedHost`, and tests constructing sessions directly). Neither is a negation of the
other; they are two independent defaults for the same well-defined flag.

If the docker gap is ever worth closing, flipping the session-internal default to `False`
is a one-line change — `EmbeddedHost` no-ops through the empty `quiet_history()` and
`LocalSession` would merely carry a harmless unused prefix.

### Login-proxy layer

`su` spawns a *new* shell that re-reads rc files, so `HISTFILE` resets to its default.
(`stty -echo` survives the transition — it is a terminal-driver attribute on the same
tty, not a shell variable — which is why history alone needs re-applying.)

`run_proxy`, `run_undo` and `_resync_shell` take `history_prefix: str = ""` — the
resolved **payload**, not a bool, so `login_proxy.py` needs no host or frame lookups and
every existing call site keeps working untouched. The name matches the helper that
produces it; `login_proxy.py` does not import that helper, so nothing is shadowed.
`_resync_shell`'s probe becomes:

```python
lambda m: history_prefix + _RESYNC_FRAME.recover(m)
```

**The payload must ride the probe, not follow it.** Sent afterwards, otto's own
`echo "__OTTO_…_RECOVER__$?__"` liveness probes land in the elevated user's history in
the gap between `su` and the suppression. Riding the probe means the first line the new
shell executes is already suppressed. Sending it *before* the resync instead is also
wrong: `su` flushes pending terminal input (the typeahead-attack defense documented at
`login_proxy.py:169-177`), so a payload written back-to-back with the transition is
silently discarded — riding the resent probe inherits `confirm_live`'s retry for free.

Threaded at `session.py:1004`, `session.py:1022`, `session.py:1032`, `session.py:1273`,
and `privilege.py:102`, `privilege.py:126`, `privilege.py:145`.

`interact.py:415` — the `otto login` interactive bridge — keeps the `""` default. That
is a decision, not an oversight: `otto login` hands a human a real shell, and a human
who loses up-arrow recall would rightly complain. Leaving human shells alone costs zero
code.

## Error handling

Best-effort — but "best-effort" is a property that had to be *engineered*, not one that
follows from redirecting stderr. The payload shares its line with the readiness probe, so
a statement that merely *fails* is fine while a statement that **aborts the line** is
fatal: it strands `echo <READY>`, the handshake never completes, and otto reports "shell
never became ready after open — the device is unresponsive or login failed (e.g. bad
credentials)". A host that works today would go unreachable, blaming credentials.

POSIX gives two ways to abort that line — an error in a special builtin, and a failed
variable assignment — and the payload's two statements are between them capable of both.
Hence `command` on each (see the payload table). With those guards a shell that refuses
everything emits at most two suppressed errors and carries on unsuppressed, which is the
actual degradation contract.

No exit status is inspected anywhere and no success probe is issued: confirming
suppression would cost a round-trip on every session open to learn something that either
worked silently or was never supported.

The one failure mode that *would* hurt is the payload emitting output and corrupting the
READY handshake parse. That is what the portability guard below exists to catch, and it
asserts on empty stderr as well as empty stdout precisely because the two streams are
merged on a PTY.

## Testing

**Unit.**

- `BashFrame.quiet_history()` payload shape; `ZephyrFrame.quiet_history() == ""`.
- `handshake` composition with the flag on and off.
- `history_prefix` frame-defaulting (`None` ⇒ bash payload) and `shell_history=True`
  ⇒ `""`.
- `UnixHost(shell_history=True)` ⇒ no prefix on the handshake; default `UnixHost` ⇒
  prefix present. Asserted on the composed handshake payload rather than on the
  parameter, so the test pins observable behavior instead of the plumbing.

**Portability guard (local, no docker).** Run the payload under every POSIX shell present
on the runner — `sh` and `bash` always, plus `dash`, `busybox sh`, `zsh`, `ksh` when
installed — asserting exit 0 and **empty stdout and stderr**. Parametrized over shells
found, never skipping the two that are guaranteed. This is the cheapest guard on the
thing most likely to regress, and it is what covers busybox.

**Live-bed e2e.** On a real `UnixHost`: sha256 `~/.bash_history` over one connection, run
several `host.run(...)` on a fresh session, close the session (bash writes history at
exit), re-read the file over a *new* connection, assert unchanged. Repeat across
`switch_user("root")` against root's history file — that is the only thing exercising the
login-proxy layer, and without it the resync-probe change is untested.

**Proven-red.** Per the house rule, the e2e must be demonstrated failing against pre-fix
code before it counts as a regression guard.

### Deliberately not tested end-to-end

Busybox gets the portability guard only. The mechanism is identical across shells —
`HISTFILE` is neutralized before any command runs — so end-to-end behavior is already
proven on the live bed's bash; what busybox adds is the risk that the payload is
*mis-parsed*, which the portability guard covers directly. The only real busybox path in
the tree is a container, and adding one would widen docker's footprint against the
standing rule that docker appears in at most one or two old-OS e2e tests.

This call was vindicated immediately: the dev VM has `dash` and `busybox` installed, so
the guard ran against both without any container, and it is what caught the special-builtin
defect below.

## Discovered during implementation

Two defects of the same shape, neither caught by design review, both caught by tests.
The pattern is worth naming: **a statement that shares a line with the readiness probe
must be incapable of aborting that line** — being silent and non-fatal-looking is not
enough, and bash (what anyone tests by hand) exhibits neither failure.

**1. `set +o history` killed dash outright.** The first run of the portability guard failed
12 assertions. `set` is a POSIX *special builtin*, so a shell that rejects its `history`
option treats that as a fatal error and — in dash — **exits the whole shell**. Since
`/bin/sh` is dash on Ubuntu, any host whose login shell is `sh` would have had its otto
session killed at the readiness handshake, presenting as "shell never became ready after
open". The redirect hid the message but not the death, and `|| :` did not help because
dash exits before the status is tested. Fixed with the `command` prefix (drops
special-builtin status) plus `|| :` (pins `$?` for the resync probe).

**2. The bare `HISTFILE=/dev/null` assignment killed rbash and any readonly HISTFILE.**
Found in review, then reproduced: POSIX also aborts the remainder of a compound line when
a *variable assignment* fails. `bash --restricted` forbids setting `HISTFILE` by name
(it is listed in bash's own man page), and an admin may mark it readonly. In either case
the bare assignment stranded `echo <READY>` on bash, dash and busybox alike — the host
would have gone unreachable, by default, reporting a credentials problem. Fixed by
setting it through the already-guarded `command export HISTFILE=…` instead.

The first-run portability guard did not catch this because it only ran
`shell -c '<payload>'` in a *default* environment. The lesson generalizing beyond this
feature: a payload injected into someone else's shell has to be tested against that
shell's hostile configurations (readonly variables, restricted mode, `set -eu`), not just
against a clean one.

**3. The fix for (1) and (2) silently disabled the feature on zsh.** Raised in the second
review round and confirmed on a real interactive PTY: zsh's `command` is a *precommand
modifier* that restricts lookup to EXTERNAL commands, and there is no external `export`,
so both guarded statements became silent no-ops — safe, but suppressing nothing on a zsh
login shell, while the docs still claimed zsh support.

The obvious repair (drop `command` for zsh) was **tested and rejected**: a bare
`export HISTFILE=…` aborts the line on zsh under `readonly` exactly as it does elsewhere,
so it would have reintroduced defect (2) scoped to zsh. Measured alternatives:

| construct | zsh: effective? | zsh + readonly | other shells |
| --- | --- | --- | --- |
| `command export …` | no (no-op) | survives | effective, survives |
| bare `export …` | yes | **strands** | dash/zsh strand even clean |
| `builtin export …` | yes | **strands** | — |
| `eval 'export …'` | yes | survives | **dash strands clean; busybox strands on readonly** |

No single construct works everywhere, hence `command` as the general mechanism plus a
zsh-scoped `eval` clause. Verified on interactive PTYs across bash/dash/busybox/zsh in
clean, `readonly`, `set -eu` and restricted configurations.

**4. ksh leaked a readonly-assignment error past the redirect.** Found by installing ksh
and mksh to check this spec's own (until then untested) ksh claims. ksh reports
`HISTFILE: is read only` from its assignment processing *outside* the simple command's
redirection, so `command export … 2>/dev/null` still put that line on the merged stream.
Non-fatal — ksh carries on — but it is exactly the stray output the READY parse must never
see. Fixed by redirecting a brace group instead. Note this defect is invisible to every
other shell tested.

The meta-lesson across all four: **every one was found by executing the payload, none by
reading it** — and the second and third were found only after adding a *control* proving
the harness could detect failure at all. An earlier PTY probe reported all shells
"stranded" including known-good ones (no controlling terminal), and later reported zsh
stranded when it was not (an 80-column line-editor redraw mangling the echoed marker).
Both were measurement artifacts that a control caught immediately.

## Docs

- `docs/guide/hosts/` — document `shell_history` alongside the other host fields,
  including the "off by default" behavior and why `HISTSIZE=0` is not the mechanism.
- Changelog entry: default behavior change (otto no longer writes to host shell history).

## Risks

| Risk | Mitigation |
| --- | --- |
| Payload emits output on some shell, corrupting the READY parse | Portability guard asserts empty stdout **and** stderr |
| A future edit "simplifies" `HISTFILE=/dev/null` to `unset HISTFILE` | Rationale recorded in the payload table and in the `quiet_history` docstring (ksh falls back to `~/.sh_history`) |
| A future edit reaches for `HISTSIZE=0` | Recorded as destructive, with the mechanism, in the same places |
| A future edit reintroduces a second, inverted flag name | `shell_history` is passed through verbatim at every layer; the sole negation is one guard clause in `history_prefix` |
| Users who *wanted* otto's commands in history | `shell_history = true`, per host or per os-profile |
