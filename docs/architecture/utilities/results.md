# Results and exit codes

Every host verb — and, through the CLI, every command built on one — speaks
one vocabulary for "what happened": the {mod}`otto.result` family. There is
no separate exit-code logic anywhere in the CLI to drift out of sync; codes
are *derived* from results.

## The Result family

```{inheritance-diagram} otto.result.CommandResult otto.result.Results
:parts: 1
```

- {class}`~otto.result.Result` — status + optional payload (`value`) + human
  diagnostic (`msg`). Truthiness follows {attr}`~otto.result.Result.is_ok`
  (Success or Skipped), never the payload — `if result:` always asks "did it
  work?".
- {class}`~otto.result.CommandResult` — one shell command: adds the `command`
  string, the shell `retcode`, and `timed_out`. `retcode -1` no longer implies
  "never ran" on its own — it is shared by "never ran", "timed out", and
  "skipped: cumulative budget exhausted". `timed_out` is how a caller tells a
  timeout apart from an ordinary failure, since `retcode` alone cannot.
- {class}`~otto.result.Results` — the aggregate `run()` returns: a `Result`
  that is also a `Sequence[CommandResult]`. Its status is the first non-ok
  entry's status; `only` asserts exactly one command ran and returns it;
  `first_failure` finds the culprit in a batch. Transfer verbs aggregate
  per-file results the same way.
- {class}`~otto.result.NotRunResult` — a `CommandResult` for a command a dry
  run declined to issue. The one member of the family that is deliberately
  *not* data: reading `value` raises
  {class}`~otto.result.CommandNotRunError` naming the command and the host,
  so the always-wrong class of caller — the one that parses a device's answer
  — breaks at the line that mistook a non-measurement for a measurement,
  while `status`, `retcode`, `timed_out` and `command` stay plain reads for
  fire-and-forget callers.

The shared vocabulary is {class}`~otto.utils.Status`: `Success`, `Failed`,
`Error`, `Unstable`, `Skipped`, `NotRun`. `Skipped` is ok and `NotRun` is not,
and the split is load-bearing: `Skipped` marks a *genuine* skip (a test step,
a transfer fold) that callers should sail past, so it cannot be made non-ok;
`NotRun` means a dry run declined to contact a device, and every `is_ok`
branch must take its failure arm rather than proceed on a fiction.

(command-output-contract)=

## What a command's `value` holds

A {class}`~otto.result.CommandResult`'s `value` is the command's output with
**exactly one trailing newline removed**: nothing stripped, nothing added.
That holds on the ssh `exec` channel; on a local host, for both `exec` and
`run` (a bash session on pipes, with no terminal); and on a shell session
whose frame calibrates its terminal (the `bash` and `ash` frames) on every
term (`ssh`, `telnet`, `console`) and both routes (`run` and `exec`). A
docker container's `exec` runs `docker exec` through its parent host, so its
`value` follows whichever of these routes the parent takes:

| The command prints | `value` |
| --- | --- |
| nothing (`true`) | `""` |
| `3` and a newline | `"3"` |
| `a`, then a blank line | `"a\n"` |
| a blank line, then `a` | `"\na"` |
| `abc` with no newline | `"abc"` |
| `a\r\nb\rc` and a newline | `"a\r\nb\rc"` |

Trailing spaces and tabs are kept, and so are the program's own carriage
returns. stderr arrives in the same stream as stdout. The bytes are decoded as
UTF-8, with any invalid byte replaced. (The ssh channel and local `exec` share
one line rule, {func}`otto.host.session.output_line`.)

The Zephyr frames (`zephyr` and `zephyr-serial`) keep their existing parsing:
they trim whitespace from both ends of the output and delete every `\r`.

To return the exact output, otto undoes two things that sit between the program and otto,
and nothing else:

- **Telnet's own encoding.** A telnet server sends a bare carriage return as
  `CR NUL` (RFC 854). otto decodes that on every telnet and console stream
  before it reads anything, unless the server negotiated BINARY.
- **The terminal's newline translation.** `run` runs on a shell session, and
  so does every shell-routed `exec` (see the note below). A shell session runs
  on a terminal. By default a Linux terminal sends each `\n` as `\r\n` (`onlcr`).
  otto never changes that setting. The session's readiness handshake measures
  it instead with a **calibration line**, a `printf` of known bytes that runs
  just before the handshake's READY marker ({ref}`per-host-session-setup`),
  and the session undoes exactly that translation. The measurement is the
  session's `terminal_mapping`, and `otto host <id> probe` reports it per
  term:
  - `identity`: nothing is translated, so nothing is undone;
  - `onlcr`: each `\n` arrives as `\r\n`, so each `\r\n` is folded back to
    `\n`, the exact inverse;
  - `unknown(<what was seen>)`: some other translation that can't be undone
    exactly, or no calibration line came back at all (a target with no
    `printf`). The output comes back as it arrived, and the session logs one
    WARNING saying which;
  - `unmeasured`: a dialect with no calibration line, such as Zephyr's.

  The ssh `exec` channel has no terminal, so there is nothing to undo there,
  and `terminal_mapping` always reports the shell session's terminal.

A command that changes its own terminal (`stty -onlcr`, or a program that puts
the terminal in raw mode and leaves it that way) changes what later output on
that session looks like. The session measured its terminal when it opened, and
it doesn't measure again until a new session opens.

The log is a separate view. Live output lines are cleaned for reading, so
carriage returns are removed and the shell route skips blank lines. `value` is
the record.

`send()` writes to the raw stream and `expect()` reads it raw, with `\r\n`
kept: the fold applies only to a command's `value`.

```{note}
**Upgrading.** Before this, every shell-routed command trimmed blank lines
from both ends of the output, deleted every `\r`, and left telnet's NUL bytes
in. That is `run()`, and `exec()` wherever it runs on a shell session: on
telnet and console, and on ssh when a session-setup hook, a proxied login or a
call that needs a shell (*expects*, `sudo`, a switched user) routes it through
the pooled shell. If you compare `value` exactly for a command that prints
blank lines or carriage returns, expect those now. `.strip()` callers are
unaffected by the blank-line change; output that contains its own carriage
returns (CRLF files, progress lines) now keeps them.

NVT decoding also changes what `expect()` patterns see on telnet and console:
a pattern that matched telnet's padding NULs (`\r\x00`) must drop them.
```

## Exit codes

`Result.exit_code` is `0` when ok, else the status value.
`CommandResult.exit_code` follows the ssh convention users already know:

| Situation | Exit code |
| --- | --- |
| Command succeeded | `0` |
| Command ran and failed | the shell's own `retcode` |
| Command never ran (connection/timeout) | `255` |
| Failed without a retcode | the `Status` value |

A `@cli_exposed` host verb returning any `Result` gets these semantics on the
CLI for free; returning a plain value exits `0`. See
{doc}`../subsystems/hosts` for how verbs become CLI commands.

## The convention, and its two gates

Public otto API says what happened in exactly one of three ways:

1. it **returns a `Result`-family value** ({class}`~otto.result.Result`,
   {class}`~otto.result.CommandResult`, {class}`~otto.result.Results`);
2. it **raises** — a named {class}`~otto.errors.OttoError` subclass when the
   failure is one otto has a name for, otherwise the plain stdlib exception
   that fits (rejecting an argument with a bare `ValueError` is ordinary and
   is not going to change); or
3. it returns a plain value and **documents why** — the predicate and
   accessor verbs (`exists`, `is_installed`, `is_uninstalled`, `ls`,
   `read_file`) plus `login` (which returns `None`) are the whole list.

The naming, not the raising, is what the convention governs: of otto's 330
bare stdlib raises, 152 sit in a public function today, and `except
OttoError` is a question about otto's *named* failures rather than a
catch-all. {mod}`otto.errors` says what each clause does and does not reach.

The extension surfaces follow it too: a {class}`~otto.host.product.Product`'s
`stage` / `install` / `uninstall` return a `Result` — usually the one
`host.run` or `host.put` already produced, so the command's retcode and output
reach the process exit code rather than being flattened on the way out.

`Status` is the vocabulary carried *inside* a result, **never a return type
of its own**: a caller handed a bare `Status` cannot see the exit code, the
command, or the output that explains it.

Both halves are gated rather than documented-and-hoped:

- **returns** — `.ast-grep/rules/no-bare-status-return.yml` fails any public
  function in `src/otto` whose return annotation mentions `Status` anywhere,
  including inside a composite: `tuple[Status, str]`, `Status | None`,
  `dict[str, tuple[Status, str]]` (run by `make lint-arch`).
- **raises** — a test sweeps every public exception class and fails on one
  that does not reach `OttoError`, on one that is not declared in its table
  of expected roots, and on one whose only declared stdlib root is bare
  `Exception` without being listed as deliberately rootless (that row would
  otherwise assert nothing, since every exception is an `Exception`).

  Note the scope: the gate is about the exceptions otto *defines*. It says
  nothing about raise sites, and deliberately so — see above.

## Where the code lives

- {mod}`otto.result` — `Result`, `CommandResult`, `Results`, and the
  exit-code derivation
- {mod}`otto.errors` — `OttoError`, the root of the raised half
- {mod}`otto.utils` — the shared `Status` vocabulary
