# A console shell that outlives otto's session keeps its own settings (#1358)

**Status:** draft for review, revised after two plan checks. From the design
study, its design check, both plan checks and the controller's rulings
(workspace `.superpowers/sdd/2026-11-26-console-shell-settings-1358/`).

## The defect

A console shell outlives otto's session when the console client owes no logout
at close: `options.logout and self._logged_in` is false
(`src/otto/host/console.py:653`). That is a host with `console_options.login:
false` (otto drives a shell that was already there; `_logged_in` is set only by
`login_sequence`, `console.py:401`) or with `logout: false`. Whoever uses that
line next meets the shell otto used, including a person who reaches it through
`otto host <id> login`, whose bridge dials the same line with no new login
(`src/otto/host/unix_host.py:953-999`).

What stays behind in that shell:

- **since before v0.20.0**, on a host with `shell_history: false` (the
  `UnixHost` default, `unix_host.py:356`; `v0.20.0:src/otto/host/unix_host.py:340`):
  history recording off, from `quiet_history()` (`src/otto/host/command_frame.py:1051`).
  In v0.20.0 the terminal's echo also stayed off (`v0.20.0:src/otto/host/command_frame.py:413`);
  the terminal save and restore added since (`session.py:1102-1153`) fixed that part.
- **since the fixes for #1346 and #1356**, on bash, zsh and mksh: `!` history
  expansion off, zsh's `#` read as a comment, and TAB no longer completing, from
  `script_parsing()` (`command_frame.py:1136`). TAB is the one people notice.

Both ride the first line of every fresh shell (`shell_prefix`,
`command_frame.py:1697`, typed by `ShellSession._handshake_payload`,
`src/otto/host/session.py:1311`). otto restores the terminal, not the shell.

## The decision

On a console whose shell outlives the session, otto's session runs in a child
shell it starts there, the **work shell**, and leaves it at close. The shell
that was already there, the **person's shell**, never runs a statement that
changes a setting.

The argument is isolation, maintenance and symmetry, not line length.
**Isolation:** nothing otto sets reaches the person's shell, so nothing is
saved, restored or got wrong, and the history leak that predates v0.20.0 closes
too. **Maintenance:** a statement added to the prefix later needs nothing more.
**Symmetry:** on ssh, telnet and a console that logs in and out, each otto
session starts from a fresh shell and nothing typed persists to the next; an
outliving console becomes the same.

**Why A (save the settings, restore them on close) loses.** Not on bytes: a
prefix-free first line followed by a line of saves would fit the 1022-byte
echoing-ash limit (`src/otto/host/userland.py:2578`). It loses on its gaps and
its upkeep. Its round trip restored 198 of 208 person states exactly (the 10
others lost an exported `HISTFILE`); zsh writes otto's commands into the
person's history file at exit, ksh93 stops recording, restricted bash refuses
the history restore, and TAB needs a capture per shell (`bind -p`, `bindkey`,
mksh `bind`). Every gap is per shell and every new prefix statement adds one.

**Why B (`exec` a fresh shell on close) loses.** A failed `exec` ends the shell
on every measured shell but bash, and a fresh shell drops everything the person
typed by hand while keeping otto's exported `HISTFILE=/dev/null`.

## Which sessions

One predicate, on the console client, read by the close and by this decision:
`ConsoleClient.owes_logout`, `options.logout and _logged_in`. `_release` reads
it instead of computing it (`console.py:653`).

A session runs in a work shell when all four hold:

1. it holds a console client (`ShellSession.console_client`, `session.py:953`);
2. the client does not owe a logout (`ShellSession.shell_outlives_session`,
   beside `terminal_outlives_session`, `session.py:1103`);
3. its frame offers a work shell: one public member of the frame contract,
   `CommandFrame.work_shell(*, prefix) -> WorkShellLines | None`, `None` by
   default. `BashFrame` and every frame derived from it (`AshFrame`, a
   project's bash subclass) return the lines. `ZephyrFrame`, `ZephyrSerialFrame`,
   `RawFrame` and a frame derived from `CommandFrame` return `None` and never
   get the start line;
4. the session types script lines (`_script_lines`, `session.py:931`). The
   login bridge sets it false (`src/otto/host/interact.py:511`): its shell is
   the person's own.

It is decided once, at the **first applicable handshake**: the first handshake
whose frame offers a work shell. A raw landing's handshake sends nothing and
decides nothing (`session.py:1430`), so a raw-landing hook that calls
`console_login()` (`session.py:2974`) has logged in before the decision.

| Session | Frame | `logout` | logged in | Work shell |
|---|---|---|---|---|
| ssh, telnet, local | any | n/a | n/a | no |
| console | Zephyr, raw, `CommandFrame`-derived | any | any | no |
| console, login bridge | bash family | any | any | no |
| console | bash family | true | yes | no: the logout ends the shell |
| console | bash family | true | no (`login: false`, a hook that did not log in) | yes |
| console | bash family | false | yes | yes |
| console | bash family | false | no | yes |

## The frame's lines

`WorkShellLines` is one frozen record, rendered by the frame, so a frame owns
the whole protocol and cannot implement half of it:

- `lead`: what goes before a line typed into the person's shell (`" "`, the
  ruled courtesy for `ignorespace`);
- `start`: the start line: start a shell of the same kind unless this one is a
  work shell, passing `__otto_child=1` to the child only;
- `proof`: statements for the entry proof: where `__otto_child` is set, define
  the work mark and unset the start mark; then, only where the mark is defined,
  the history suppression, which the session passes as *prefix*
  (`history_prefix`, `command_frame.py:1679`; empty where history is kept);
- `readback`: a word that expands to `1` where the mark is defined and to
  nothing elsewhere;
- `exit`: the close statement: `exit` only where the mark is defined, on zsh
  first `setopt CHECKJOBS NOCHECKRUNNINGJOBS NOHUP`.

**The work mark is the identity mark's mechanism**, an alias
(`IDENTITY_MARK_PREFIX`, `command_frame.py:768-780`), named `__otto_as_w`: no
shell exports an alias, even under `set -a`, and `alias NAME` is a builtin
query. A variable was rejected there for this reason, so a child or a switched
shell never inherits ownership. Its name is fixed, unlike an identity mark's
random one, because a leftover work shell must be recognised by the next
session. The record checks its own fields (no quote, one newline ending the
start line, a whitespace lead), and `_check_frame_class` (`command_frame.py:1732`)
builds the frame and refuses a `work_shell` that returns anything else.

## The sequence

### Open

All of it runs inside `ShellSession._handshake` (`session.py:1379`), so the
first open and a raw landing's frame entry (`enter_frame`, `session.py:1559`)
take the same path.

1. **Line 1, into the person's shell.** The handshake with the terminal save
   and no shell prefix, history untouched (`handshake_prefix(...,
   shell_history=True, script_lines=False)`, `command_frame.py:1712`; 387
   bytes measured), after the lead. It measures and saves the terminal.
2. **The start line, into the person's shell**, after the lead, written once
   after READY with no reply awaited.
3. **The entry proof**, through the existing resync probe:
   `_resync_shell(settle=0.0)` (`src/otto/host/login_proxy.py:518`) with one new
   parameter, the frame's lines: `proof` rides the probe line in place of the
   prefix, and `readback` is read back as a checked reply field.
   `_resync_shell` returns it; `_handshake` checks it. So the child's first
   line carries history suppression, as every new shell's first line must
   (`login_proxy.py:535`): the child inherits the person's exported `HISTFILE`.
   It carries only that. The history statements are the only ones that must
   come before the child records a line; the script-parsing statements need
   not, because the proof line has no `!`, `#` or TAB, and those options hold
   from the next line on anyway (`command_frame.py:1136`).
4. **The work shell's handshake.** The ordinary handshake, typed again in the
   work shell, with the whole shell prefix as on every other new shell: the
   script-parsing statements, the history suppression again (idempotent), the
   terminal normalisation and measurement and the exec state. It is the same
   composition, not a copy, so a work shell whose startup file changed the
   terminal is measured there. It saves nothing: the save is
   decided once per session, by whether a save was attempted, not by whether a
   record came back, so a save that failed in the person's shell is never
   retried in the work shell and an otto setting never passes for the person's.

**Size.** The study measured its guarded proof, carrying the whole shell
prefix, at 945 bytes with history suppressed and 749 kept
(`logs/30_C_bytes.log`). Its variable guard and readback were 120 bytes; the
alias guard and readback are 174 (77 to define the mark, 50 for the `if`
around the prefix, 47 for the readback with its terminator): 999 and 803.
Leaving the script-parsing statements (336 bytes) to the work shell's
handshake gives about 663 bytes suppressed and 467 kept, 359 under 1022 at
the most: computed, not measured, and pinned by a length test. The history
suppression is 196 bytes of the 663. Folding the proof into the handshake
would save one exchange, but the calibration and the guarded prefix together
do not fit, and the room is kept for later prefix statements.

**Refusal.** An answer without the mark (the shell started no child, or the
start line was lost), or none by the resync deadline (`login_proxy.py:401`),
refuses the open with a `ConsoleError` after the terminal is put back; never
retried (`session.py:5201`), never a fallback to the person's shell.

**Cost.** Three exchanges and one write where there was one exchange, no timer.
`SessionManager.open_budget` (`session.py:4873`) counts the resync deadline and
a second readiness deadline per attempt where the console options can keep the
shell (`login` or `logout` false), so a caller bounding the open with it sees
the refusal, not its own timeout. Every other session is byte-identical.

**Inside the work shell.** A later handshake (frame entry after a hook) is the
ordinary one and starts nothing. Login-proxy hops run after the open
(`session.py:5199`), so their `su` runs inside the work shell and gets the
prefix from `_new_shell_prefix` (`session.py:1336`) as today; so do
`switch_user` and `as_user`, and a scoped switch returns to the work shell.

### Close

`TelnetSession.close` (`session.py:2982`) types two lines before the client's
release, each with its own condition, neither awaited, through one helper that
writes a framed line and warns on failure:

1. the terminal restore, as today, only where a record was saved
   (`_put_terminal_back`, `session.py:1129`);
2. the work-shell exit, the `exit` of the lines kept at entry, only where the
   session entered a work shell and its current frame still offers one (a hook
   may have entered another dialect, `enter_frame` replaces the frame,
   `session.py:1596`).

They are independent: a console with no `stty` record still leaves its work
shell. Both keep today's guard (not into a running command, an app shell or a
dead session, `session.py:1145`), made one shared property. A line that cannot
be computed, rendered or written (a frame's `restore_terminal` or `work_shell`
that raises included) does not stop the release. In a switched or hopped shell
the exit does nothing.

### After an otto that died

The person types `exit` once and is back in their own shell. Or the next otto
session's line 1 lands in the leftover work shell, the start line starts
nothing there, the proof keeps the mark, and that session's close returns the
person's shell. Work shells never nest.

## What a user sees change (breaking)

Breaking under `docs/contributing.md:396`, against v0.20.0, which ran the
console session in the shell it found. It applies to a bash-family console
whose shell outlives the session (the table above; never the login bridge):

- **Commands no longer run in the shell that was already there.** The work
  shell inherits the directory, exported variables and umask, and reads the
  startup files of an interactive non-login shell (`~/.bashrc`, `$ENV`,
  `~/.zshrc`; not `~/.profile`). It does not see unexported variables,
  functions, aliases or options typed into that shell by hand, so a lab that
  prepares a console by hand (`. ./env.sh`, then `login: false`) sees different
  results unless it exports what its commands need.
- **What otto's commands set no longer carries to the next session** (`cd`,
  `export`, `set`, a function), except where the next session reuses a work
  shell left behind. Per-session preparation goes in a `session_setup` hook or
  the shell's startup file.
- **A console whose shell cannot start a child is refused.** Measured: restricted
  bash, and a shell whose name does not end in `sh` (a dash reached as `-su`).
  The message names the console, says its shell outlives otto's session so otto
  works in a child shell, says which part failed (none started or the start
  line was lost; or the child did not answer), and gives the two remedies: set
  `console_options.login` and `logout` to `true` so otto logs in and out, or
  give the console a shell that can start a copy of itself.

## Limits that stay

- A shell without an `alias` builtin (BusyBox `hush`, or an ash built without
  alias support) cannot hold the work mark, so the open is refused with that
  cause named; the `login: true` remedy needs a login prompt on the line. Not
  measured: no such shell is on the bed.
- A close line that is lost, or read by a program still running, leaves the
  work shell with otto's settings; one `exit` returns the person's shell.
- A stopped job makes the work shell refuse `exit` once the shell has seen it
  stop: it stays, the next otto session reuses it, and the person resumes or
  ends the job, then types `exit`. otto kills no job. A job stopped by otto's
  last command that the shell has not yet reaped does not stop the exit on dash;
  its group is then orphaned, and the kernel's hang-up may end it.
- An unscoped `switch_user` or a login-proxy hop left open leaves its shell on
  the line above the work shell, and each later session nests one more pair;
  today it leaves the switched shell alone. A GitHub issue records it.
- A hook that enters a dialect with no work shell (an application frame) leaves
  the work shell under it at close.
- A close that finds a read still running skips an owed logout
  (`console.py:670`); that session used no work shell, so the shell keeps
  otto's settings as today.
- After an otto that died, the terminal settings the next session saves are
  otto's (echo off), and its close puts those back.
- A work shell's handshake that never answers fails as any handshake does
  (`_fail_init`, `session.py:1603`): the work shell stays, the terminal is not
  put back, and the open's one retry lands in that work shell and reuses it.
- A refusal because the child did not answer leaves that child on the line,
  and the terminal restore is typed into it.
- Line 1 and the start line can land in the person's history; the lead keeps
  them out under `ignorespace`, and that is all it promises. On mksh and ksh93,
  and on a zsh whose startup file sets `INC_APPEND_HISTORY` (likely also
  `SHARE_HISTORY`, not measured), the proof line itself can reach the history
  file, as today's first line does: zsh records a line before it runs it.
- A zsh work shell with no zsh startup file in `HOME` prints zsh's new-user menu
  at every session; the lead's space aborts the menu, no file is written, and
  the work shell is entered. `touch ~/.zshrc` stops the menu.
- Keys a person types while otto's close runs may be read by an exiting ksh or
  mksh work shell and lost.
- Every open leaves line 1's variables (`__otto_base`, `__otto_e`,
  `__otto_tty`, as today) in the person's shell: line 1 runs there before the
  work shell starts. `__otto_probe` does not stay: line 1 unsets it. The start
  line sets `__otto_c` in the person's shell and unsets it only once the work
  shell exits, so it stays while the work shell runs and after a work shell
  is left behind.
- `RUNNING_SHELL` finds the shell's name on `PATH` (below), so where that name
  is another program (BusyBox started as `busybox sh`, with dash as `sh`), the
  work shell is that program.
- A zsh work shell with history suppressed prints `locking failed for /dev/null` as it exits.

## Which shell the work shell runs

One resolver, `RUNNING_SHELL = "${BASH:-${0#-}}"` in `command_frame.py`: the
program of the shell reading the line. bash names itself by `$BASH` (its `$0`
may be `-su`); the others by `$0` less a login dash, looked up on `PATH`. The
start line starts it only when the name ends in `sh`.

`SESSION_SHELL` (`command_frame.py:132`, `"${SHELL:-/bin/sh}"`) stays for its
own question, the shell sudo's `shell` form and the stopper start
(`src/otto/host/privilege.py:622`, `src/otto/host/process_stop.py:129`), as
`sudo -s` reads `$SHELL`. The work shell must speak the dialect the handshake
just proved on the line; `$SHELL` names a program nothing proved, and on a
`login: false` console it can name another shell or be unset. Each constant's
docstring names the other and says why.

## Where the code disagrees with the study and the checks

- The design check asks the entry to re-apply and re-measure the terminal. The
  handshake's terminal statements add 174 bytes
  (`src/otto/host/terminal_mapping.py:131-172`) to a proof already near 1000,
  over 1022. So the proof carries the mark and the history suppression, and
  the measurement and the script-parsing statements ride the ordinary
  handshake typed in the work shell.
- The study's work mark is a variable; the code already rejected a variable for
  a shell-local mark (`command_frame.py:768-780`), so the mark is an alias.
- The rulings first named zsh's `NOCHECKJOBS NOHUP`. The study's own log shows
  it ends a stopped job (`logs/35_history_and_misc.log:17`). `CHECKJOBS
  NOCHECKRUNNINGJOBS NOHUP` keeps the stopped-job refusal whatever a startup
  file set, as every other shell refuses.
- `_resync_shell` raises a login-proxy message on its deadline
  (`login_proxy.py:654`); the session turns that into the console refusal.
- `_handshake` does not use `_resync_shell` today: it calls `confirm_live`
  itself (`session.py:1453`). The entry adds the resync after READY.
- `_saves_terminal()` decides by whether a record exists (`session.py:1355`),
  so a failed save is tried again on the next handshake; it decides by whether
  a save was attempted.
- No lab host under `tests/` declares `login: false` on a bash-family console;
  only the Zephyr consoles run without a login, on frames with no work shell.

## Tests that prove it

- the truth table, over `owes_logout`, `work_shell` and the bridge; a raw
  landing whose hook logs in decides at frame entry;
- on every shell of the pty matrix, history kept and suppressed: the work
  shell is a child of the person's shell and runs the same program; after the
  close the person's shell has the same pid, `set +o`, TAB binding, `!`
  setting, `HISTFILE`, unexported variable, function, alias and `stty -g`
  record, and no command otto ran is in its history file; inside the session,
  `!word`, `#` and TAB behave as script lines;
- the mark is never exported: `set -a` in the work shell's startup file, a
  pre-exported name, and a shell started inside the work shell that is not
  taken for it;
- the refusal: restricted bash, dash as `-su`, a name not ending in `sh`, a
  lost start line, and a child that never answers: a `ConsoleError` with the
  message content above, the person's settings and terminal as before, the
  manager not retrying, the refusal within `open_budget`;
- a work shell whose startup file changes echo and the line mapping: the
  session's measurement is the work shell's;
- a failed save in the person's shell is not retried in the work shell;
- a console with no `stty` record enters and leaves its work shell; a repeated
  frame entry starts no second one; a hook that enters a frame with no work
  shell gets no exit typed; the close types the restore then the exit, each
  only under its condition, once;
- an otto that died: one `exit` returns the person's shell; the next session
  reuses the work shell and its close returns the person's shell;
- a stopped job, with zsh's `CHECKJOBS` on and off: the work shell stays and
  the job is alive; a running job: the exit happens and the job is alive;
- a close skipped while a program owns the input, then a new session: nothing
  reached the program, and the work shell is reused and left;
- a custom frame registered with its own lines (and one registered by
  reference) is driven through open, proof and close; a `work_shell` that
  returns something else is refused when the frame is checked;
- cuts: line 1 (past its lead) and the entry proof are cut-sweep kinds, the
  proof in a shell with no mark, a newly started child and a leftover work
  shell, answering the mark only where it is owned; a cut inside the lead
  answers as the whole line; no tail of the start line lets the person's shell
  claim the mark or receive a setting, while a tail that still starts a child
  is allowed;
- length: line 1, the start line and the entry proof each fit 1022 bytes; the
  proof holds no script-parsing statement and no `!`, `#` or TAB;
  ordinary sessions keep their bytes; the bridge types no start line;
- on the bed, a console used with `login: false`: the person's shell keeps TAB
  completion, `!` and an unexported variable after otto's session.

## Not measured

- A real console beyond one Ubuntu console at default pacing (the bed test).
  Delivery in pieces, UART pacing and typeahead across the child's start were
  measured on a local pty only. The open's time on a real console.
- The alias mark, the `if` guard and the readback on the shell matrix (the
  study measured a variable mark); zsh's `NOCHECKRUNNINGJOBS` (the study
  measured `NOCHECKJOBS`); the proof's exact length.
- A shell really reached by `su -` (simulated with `exec -a -su`); BusyBox
  older than 1.36.1 and its history file; zsh's login-shell test; traps.
- Startup files that print, prompt, are slow, or `exec` another shell; only
  ones that change echo and the line mapping, set `-a`, or wait for input are
  tested.
