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

The shared vocabulary is {class}`~otto.utils.Status`: `Success`, `Failed`,
`Error`, `Unstable`, `Skipped`.

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
2. it **raises an {class}`~otto.errors.OttoError` subclass**; or
3. it returns a plain value and **documents why** — the predicate and
   accessor verbs (`exists`, `is_installed`, `is_uninstalled`, `ls`,
   `read_file`) plus `login` (which returns `None`) are the whole list.

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
- **raises** — `tests/unit/test_error_base.py` sweeps every public exception
  class and fails on one that does not reach `OttoError`.

## Where the code lives

- {mod}`otto.result` — `Result`, `CommandResult`, `Results`, and the
  exit-code derivation
- {mod}`otto.errors` — `OttoError`, the root of the raised half
- {mod}`otto.utils` — the shared `Status` vocabulary
