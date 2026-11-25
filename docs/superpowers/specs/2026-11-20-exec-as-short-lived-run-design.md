# `exec()` is a short-lived `run()`

**Status:** approved by Chris, 2026-11-20. Revised 2026-11-21 after the Stage 0
measurement ("What Stage 0 found"), with Chris's three rulings of that day.

## Intent

A host has two ways to run a command. `run()` sends it to a persistent shell
session, so `cd` and exported variables carry over to the next call. `exec()`
runs one command that leaves nothing behind and may run beside other `exec()`
calls.

Today these are two implementations. `run()` always goes through one
mechanism: a framed command typed into a shell session. `exec()` picks one of
five routes per call (`ssh-channel`, `pooled-shell`, `default-session`,
`local-subprocess`, `container/<parent route>`), each with its own way to
start, feed, time out, stop and read back a command. About 2,000 lines exist
only for those routes: the planner and dispatch in `session.py` and
`unix_host.py`, `process_stop.py`, `exec_route.py`, and the console's
emulation of a clean login shell. The feature-pruning review of 2026-11-20
found this area to be the largest source of fixes that add code: the same
input behaves differently per route, and each difference gets its own patch.
The #935 conformance series spends most of its cells proving that the routes
agree.

Chris's ruling (2026-11-20): `exec()` is needed on every host family, telnet,
console and Zephyr included, and it is a degenerate case of `run()`: a
short-lived `run()` that comes almost for free. This design makes that
literally true. One mechanism runs every command; `exec()` is that mechanism
on a session it borrows for one command.

## Goals

- **One mechanism.** Every command, from `run()` or `exec()`, on every host
  family, is a framed command on a shell session. The same input gives the
  same output, exit status, timeout behaviour and errors from both entries.
- **`exec()` stays on every family** that has `run()`: ssh, telnet, console,
  local, docker containers, Zephyr. Console and Zephyr already run it on the
  default session; nothing is added there, they simply stop being special.
- **One terminal rule.** `run()` and `exec()` have the same default (terminal
  on) and the same switch to turn it off.
- **Delete the routes.** The bare-channel and subprocess paths, the planner,
  the process-route helpers of `process_stop.py`, `exec_route.py`,
  `CommandResult.route` and the console's clean-login emulation are gone at
  the end.
- **The conformance matrix shrinks.** The `exec-shell` entry becomes `exec`;
  cells that exist only to compare routes are deleted.

## Non-goals

- No change to `run()`'s behaviour, apart from gaining the terminal switch.
- No change to file transfer, registries (`register_*` seams), login, or
  `interact`.
- No terminal for the local host. A local session is a pipe today, on both
  entries, and stays one (see "The terminal switch").
- The #1200 container stop fix is not designed here. This design removes the
  mechanism that defect lives in; #1200 is re-read after Stage 3.

## Anti-goals

- **A slower sunny day.** A serial `exec()` loop on an ssh host must not cost
  more round trips per call than today once the pool is warm. Stage 0
  measured this as met (one exchange either way). A cold burst of concurrent
  calls costs more on sessions; that cost is accepted.
- **A flag soup.** `exec()` must not grow per-family options to recover what
  a route used to do. Where a route did something the session cannot, the
  design names the loss below, and it is a breaking change, not a knob.
- **A second mechanism kept "for speed" or "for long lines".** If the
  measurement says the session is too slow or too narrow somewhere, that
  comes back to Chris before a second path is kept.

## Constraints

- Backwards compatibility matters now (early users). Every break is listed
  under "Breaking changes" and carried in a `BREAKING CHANGE:` footer.
- Sunny day is deterministic and no slower; timers only on rainy-day paths.
- One decision, one place (AGENTS.md): after Stage 2 there is one function
  that runs a framed command on a session, and both entries call it.
- No threads with asyncio. Python floor stays 3.10.
- otto's own code awaits `.exec(` about 85 times and `.run(` about 21 times.
  Every internal caller must keep working; some depend on today's
  no-terminal `exec` (long command lines, byte-exact output).

## How it works today (what the builder starts from)

- Framing lives in `host/command_frame.py`. `frame` wraps a `run()` command
  as `echo BEGIN; { cmd }; echo END$?`. `frame_exec` wraps a shell-routed
  `exec()` command in a subshell, `( cmd ) </dev/null`, and restores errexit
  and terminal settings afterwards (`ExecRestore`).
- `SessionManager` (`host/session.py`) already keeps a per-user pool of idle
  sessions for `exec()` where a host has no bare-channel primitive (telnet, a
  proxied login, a hook, a call that needs a shell). The pool opens a new
  session when all are busy, so concurrent `exec()` calls run independently.
- Session kinds and their terminal today:

  | Session | Terminal today |
  |---|---|
  | ssh (`SshSession`) | always a pty (`term_type="dumb"`) |
  | telnet, console, Zephyr (`TelnetSession`) | always a terminal; the far end owns it |
  | local (`LocalSession`) | never; a pipe |
  | container `run()` session | `docker exec -it` (terminal) |
  | container `exec()` | no session: each call sends a fresh `docker exec -i … sh -c` through the parent's `exec()` |

- A typed line has a length limit under a terminal (`PTY_TYPED_LINE_MAX` =
  4095, `ASH_ECHOING_LINE_MAX` = 1022 in `userland.py`). Today's ssh `exec`
  has no such limit because it opens a bare channel. The bare channel and the
  local subprocess already merge stderr into stdout, as sessions do.
- A bare ssh channel and a local subprocess run the command in a non-login,
  non-interactive shell. A session is a login shell that otto has shaken
  hands with (profile read, `session_setup` applied).
- The conformance entries are `run`, `exec`, `exec-shell`
  (`tests/conformance/_entry.py`). `exec-shell` already drives `exec()`
  through a pooled shell session on families that would otherwise take a
  bare channel.

## Approaches considered

1. **Measure, then move in stages (picked).** A throwaway measurement on the
   bed first, then three landings: the terminal switch, `exec` onto sessions,
   deletion. Each landing is gated and useful alone, and the measurement can
   stop the work before any product code changes.
2. **One landing.** Rewrite `exec()` and delete the routes in one pull
   request. Fewest intermediate states, but the diff touches every family at
   once, and a latency or line-length surprise is found after the rewrite.
3. **Keep the routes, hide them.** Make the planner private and stop
   reporting `route`. Cheap, but removes no mechanism: the routes still
   disagree and still collect fixes. This works against the reason for the
   change.

Chris picked 1 (2026-11-20).

## The design

### Which session an `exec()` borrows — decided by Chris

- **Where the host can open a second session** (ssh, telnet, local,
  containers): `exec()` takes an idle session from the per-user pool, or
  opens one if none is idle, runs the command in a subshell with stdin from
  `/dev/null`, and hands the session back. Concurrent calls each get their
  own session. This is today's pool, used on every call instead of some.
- **Where the host has one line only** (a serial console, a Zephyr shell):
  `exec()` runs on the `run()` session, in a subshell where the far end has
  one. It sees what `run()` left (working directory, variables) and leaves
  nothing of its own behind. Calls queue behind each other and behind
  `run()`. On a Zephyr shell, which has no subshell, `exec()` and `run()` are
  the same thing.

Builder's calls on pool lifetime (Stage 2, 2026-11-24):

- **ssh keeps one idle session per user and terminal mode.** Every idle
  pooled session holds an ssh channel and a default server allows ten;
  keeping a whole burst idle would leave no channel for a stop, SCP or SFTP.
  A warm serial loop still costs one exchange per call. Telnet pools keep
  every session, as before.
- **The local host keeps none.** Each local `exec()` opens a session, runs
  and closes it. A kept local shell would hold the working directory and
  environment of the moment it opened and drift from the Python process.

### The terminal switch — decided by Chris

- `run()` and `exec()` share one default in the end state: **terminal on**.
  During the transition `exec()` may keep a different default (ruled
  2026-11-21): it stays **off** from Stage 2 until Stage 4 flips it.
- The switch is a `terminal=` argument on `run()`, `exec()` and
  `open_session()`, with the same meaning on all three, for one call or one
  session. There is no host option (ruled 2026-11-22): every session has a
  terminal unless the call says `terminal=False`. A host-wide default can
  be added later if users ask for one. The command line carries the same
  switch as `--terminal/--no-terminal`.
- Shell state belongs to a session. `run(..., terminal=False)` therefore
  uses a separate persistent session from `run(...)`; a `cd` in one is not
  seen by the other. The same holds for named sessions: a session is opened
  with or without a terminal and keeps that for its life.
- The `exec()` pool is keyed by user and terminal mode.

- **No terminal and switching users do not combine** (ruled 2026-11-22).
  `terminal=False` with `user=`, `sudo=True` or inside `as_user()` is
  refused by name, and so is `exec(terminal=False)` with `expects=`:
  switching users and answering a prompt need a terminal to answer on.
- **Stopping a command without a terminal** (measured and ruled
  2026-11-22). A pipe has no Ctrl-C, and closing the channel sends no
  SIGHUP, so the command would keep running.
  - The no-terminal ssh session starts the user's login shell with a token
    in its environment (`OTTO_SESSION`); everything the shell starts
    inherits it. A timeout or cancel opens a second channel on the same
    connection that freezes and then kills every process carrying the
    token. Nothing depends on how the server delivers a signal, so OpenSSH
    and dropbear behave the same (both measured: every command stopped,
    nothing after it ran, under a second).
  - **The session ends with the stop** (ruled). A shell that reads a pipe
    is not interactive: one that outlived the stop would run the rest of the
    line otto gave up on (`sleep 600; rm -rf build`). The next command opens
    a fresh session, so `cd` and variables from before the stop are gone.
  - The stop reaches everything the session started and is still running,
    a background job from an earlier command included. A terminal session
    is unchanged: Ctrl-C there leaves such jobs alone. The docs say so.
  - **The same rule holds for a reused pooled `exec()` session** (ruled
    2026-11-24, Stage 2). A timed-out or cancelled `exec()` without a
    terminal stops everything started on its session, a background process
    left by an earlier `exec()` included. A caller that leaves one starts
    it with `OTTO_SESSION` unset (`setsid` alone is not enough: the stop
    finds a process by that mark, not by its group) and redirects its
    output; otto's own detached launches do so through one function
    (`otto.host.daemon.detached_command`). (Changed during implementation
    2026-11-25 from "detaches it (`setsid`)", pending the maintainer's
    confirmation.) Rejected: a mark per command
    (it misses the shell that runs the command line, so the rest of the
    line would run) and a fresh session per call (two more exchanges on
    every call).
  - The channel's `KILL` request follows as the fallback, for a host with
    no `/proc` or where no second channel opens. There, dropbear ends only
    the shell and a running command is left running.
  - Rejected after measurement: a `trap` that keeps the shell (it runs the
    rest of the line); `INT` before `KILL` (a handler runs a statement in
    the gap); tracking the shell's pid and signalling its children (misses
    a wrapped command on both servers).
- **Defaults always succeed.** An unset `terminal` is the transport's own
  mode and never raises; only an explicit value the transport cannot honour
  is refused.

  | Family | Unset | The other mode |
  |---|---|---|
  | ssh | terminal on | `terminal=False`: the no-pty session |
  | telnet, console, Zephyr | terminal on (the only mode) | `terminal=False` refused by name |
  | local host | no terminal (the only mode) | `terminal=True` refused by name |
  | container | terminal on for `run()` | passed to the parent host |

  `exec()` on ssh keeps today's bare channel when unset until Stage 4.

My calls on the switch, for Chris to check:

- **A mode a family cannot offer is refused by name.** "No terminal" on
  telnet, console and Zephyr raises an error that names the host and says
  the transport is a terminal. Nothing is silently downgraded.
- **The local host is the one exception to the default.** It has only the
  no-terminal mode today, on both entries, so its default is off and
  `terminal=True` is refused by name. Giving local sessions a pty would
  change every local command's output (echo, CRLF, the line limit) for no
  request anyone has made. It is a possible follow-up issue, not this work.
- **Name.** `terminal` (bool) for the argument. The builder may pick a
  different name if an existing option already uses the word; the meaning
  is fixed.
- **The switch is a plain bool; there is no "off where offered" value.**
  otto's own callers that already work on telnet and console work under a
  terminal, so they take the default on every family. A caller that
  works only without a terminal (long lines, byte-exact output) passes
  `terminal=False` and is refused on a terminal-only family, which is where
  it already cannot run. Stage 0 produces both lists. If it finds a caller
  that needs a third behaviour, that comes back to Chris before Stage 1.

### Timeout and cancellation — my calls

- A timed-out or cancelled `exec()` on a pooled session **closes that
  session** and returns the timeout result. No recovery dance: the session
  is disposable, and closing it is the one stop mechanism that works the
  same on every family. The pool opens a fresh session on the next call.
- `run()` keeps today's recovery, because its session holds state the user
  wants back.
- On a one-line host (console, Zephyr) `exec()` cannot close the line, so it
  uses `run()`'s recovery. This is the same code, not a third path.
- What happens to the remote process when the session closes is whatever the
  transport does on hangup. The docs say so per family in one table; the
  design does not promise a kill.

### Containers — my call

A container's `exec()` stays what it is: the `docker exec -i … sh -c` line
sent through its parent's `exec()`, with `-t` added and `terminal=` passed on
per the switch. It keeps working over any parent that has `exec()` (ssh,
telnet, local, a structural stand-in). It is no longer a route of its own,
because the parent's `exec()` is now the one mechanism; the
`container/<parent route>` label goes with the routes. A container is a leaf
(PR #1217), so there is exactly one hop. The container's `run()` session is
unchanged, including its need for an ssh unix parent.

### What the result carries — my call

`CommandResult.route` is removed. There is one route. Tests that asserted a
route assert behaviour instead.

## What Stage 0 found (2026-11-20, bed)

Full report: `spike/exec-as-run/report.md` on the local branch
`spike/exec-as-run`. What changes this design:

- **Cost.** A warm serial `exec()` is one exchange on a bare channel and one
  on a pooled session. A new pooled session costs about two more exchanges
  than a new channel, so 8 concurrent calls on a fresh host cost about 25
  exchanges against 9. Suite wall time rose 42% forced on a busy VM; the
  repeat on a quiet VM measured +3.2% forced against a baseline spread of
  8%, so the 42% was the VM. One test, `test_run_instruction_on_container`,
  looked slower forced (6-10 s against 24-36 s). It is not: run alone it
  takes 5.4-6.4 s on either route, and beside the docker tests its fixture
  waits 40-47 s for the docker host's file lock on either route. The suite
  passes compared "alone" with "queued".
- **Typed input is not transparent.** A shell session under a terminal types
  the command through the shell's line editor. A literal TAB was lost on
  bash (#1224). A line of about 3,980 characters or more was cut on the
  bed's BusyBox guests and still returned status 0: they were pinned as ash
  only through `userland_options.shell_dialect`, which no session read, so
  the ash frame's long-line refusal never applied (#1222). Bash took 9,000
  characters intact. Both are fixed on `main` (PR #1236, 2026-11-21).
- **Tools change their output under a terminal.** `docker images` printed
  colour codes; `docker compose` and `docker build` printed progress output.
  otto's docker callers parse such output.
- **Output bytes** were identical on both routes for every probe.
- A session is a login shell with the profile read, with or without a
  terminal; a bare channel is neither.

## Stages

**Stage 0: measurement.** Done, see above, the quiet-VM timing repeat
(ruled 2026-11-21) included.

**Stage 1: the terminal switch (additive).** `terminal=` on `run()`, `exec()` and `open_session()`. New work: an ssh
session without a pty. Refusals by name where a family has one mode.
`exec()` keeps its routes and its present default: with `terminal` unset it
behaves exactly as today; `terminal=True` sends it to the pool;
`terminal=False` takes the bare channel where there is one.

Done in PR #1266 (the ssh session without a pty) and PR #1290 (the public
switch), both 2026-11-23. Three things differ from the paragraph above, or
were left open:

- The public change carried a `!` after all. Callers are unaffected, but the
  `Host` protocol's three methods gained the keyword, so a class that
  implements `Host` itself must accept it.
- A container's `exec(terminal=True)` is refused (Chris agreed, 2026-11-23): `docker exec -t`
  output carries CR. Its `run()` has a terminal and its `exec()` has none, as
  before.
- The switch has bed integration tests and no conformance arms. Chris ruled
  2026-11-23 that the conformance matrix gains it with Stage 4, not before,
  and that this work stream does not close without it (see Stage 4).

**Stage 1b: input transparency (ruled 2026-11-21: before Stage 2).** A
command typed into a session under a terminal reaches the shell as written,
or is refused by name; never changed silently. Done in PR #1236
(2026-11-21): a TAB is typed after Ctrl-V so it arrives as a TAB, and any
other control character raises `UntypableCommandError` before the command
is sent (#1224; ruled: refuse, add no encoding). A host's shell is declared
once, as `command_frame`, so a host pinned as ash refuses an over-long
typed line (#1222; `userland_options.shell_dialect` is removed). Not
covered: a BusyBox shell whose terminal echo could not be turned off still
completes on TAB (#953). Remaining before Stage 2: a conformance contract
that pins byte-exact input on every term.

**Stage 2: `exec()` moves onto sessions everywhere (`!`).** One function
runs a framed command on a session; `run()` and `exec()` both call it.
`exec()`'s default stays **terminal off** in this stage: on ssh, local and
containers it borrows a session with no pty, which keeps today's output
shape for tools that detect a terminal and has no line editor in the way.
On telnet, console and Zephyr, where the transport is a terminal, `exec()`
runs under it, as it does today. In the same pull request the `exec-shell`
conformance cells become the `exec` cells and the old `exec` cells are
deleted.

**Stage 3: deletion.** The bare-channel and subprocess paths, the planner
(`_plan_exec`, `_run_plan`, `ExecPlan`), `exec_route.py`, the process-route
helpers of `process_stop.py` (the module's marked-process sweep stays: the
no-terminal ssh stop, the local session's close and the coverage scan
clean-up use it), `CommandResult.route`, the console clean-login emulation
(with its break below), and the docs and tests that describe them. #1200 is
re-read and closed or re-scoped.

**Stage 4: `exec()`'s default becomes terminal on (`!`).** The end state
Chris ruled: both entries share one default. It is its own release-noted
break, after Stage 3. Before it, the release before it says the default
will change, and otto's own callers stop depending on the default.

Stage 4 also puts the terminal switch into the conformance matrix (ruled
2026-11-23). Per entry, for `run` and `exec`: `terminal=True` and
`terminal=False` each give a command that sees the mode it asked for or are
refused by name, and unset never refuses.

A census of `main` on 2026-11-21 counted about 97 `exec` call sites in
`src/otto`. About 5 forward a command the user wrote (the CLI and
instruction wrapper, the container host's transport, the power commands,
the observe verbs). The other 92 or so are otto's machinery: about 50 use
only the exit status, about 24 parse output that is the same bytes either
way, about 14 parse a tool that changes under a terminal (`docker images`,
`docker ps`, `docker compose`, `docker load`, `ls`), and about 4, plus the
transfer path, carry byte-exact data. They share no single helper.

Ruled 2026-11-22: only the sites that need it today pass `terminal=False`
(the 14 or so that parse a terminal-aware tool and the byte-exact ones).
There is no internal entry point and no lint rule; the breaking-change
notes tell users how to migrate their own calls. The container host's
transport is the one site that must pass on the container call's own
choice, not the parent's.

Each stage is its own pull request with its own gate. Stages 1 and 2 may be
split further by the plan; Stage 3 lands only after Stage 2 has been on
`main` through one nightly.

## Breaking changes (all user-facing, all `!`)

- Stage 4: `exec()` runs under a terminal by default. A tool that detects
  a terminal prints what it prints for a person (colour, progress, a pager),
  and the command is typed through the shell's line editor.
  `terminal=False` restores the Stage 2 shape where the family offers it.
- Stage 2: `exec()` on ssh runs in a login shell session, as `run()` does:
  the profile has been read. Before, it ran in a non-login, non-interactive
  shell. On the local host it runs in the local session's shell (bash, no
  profile) where it ran in `/bin/sh`.
- Stage 2: a timed-out or cancelled `exec()` closes its session; it no
  longer signals the remote process by pid. Without a terminal the stop
  reaches everything started on that session (see "Stopping a command
  without a terminal").
- Stage 3: `exec()` on a console no longer runs in an emulated clean login
  shell. It sees the `run()` session's directory and variables.
- `CommandResult.route`, `otto.host.exec_route` and the `ExecRoute` values
  are removed.

## Left to the builder

- The name and signature of the one shared function, and where it lives.
- How an ssh session without a pty is prompted and framed (no echo, no
  prompt to wait for), reusing what `LocalSession` already does for a pipe.
- Pool limits and idle-session lifetime; today's behaviour is the default.
- The per-family wording of refusals and of the hangup table in the docs.
- How the plan splits Stages 1 and 2 into commits and parallel tracks.
- Which existing tests are rewritten, moved or deleted, provided no
  behaviour contract is dropped without being named in the pull request.
