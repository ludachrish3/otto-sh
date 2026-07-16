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
  string and the shell `retcode` (`-1` means the command never ran).
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

## Where the code lives

- {mod}`otto.result` — `Result`, `CommandResult`, `Results`, and the
  exit-code derivation
- {mod}`otto.utils` — the shared `Status` vocabulary
