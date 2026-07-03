# otto run — instructions

An instruction is a *procedure*: one async function with full lab access —
deploy, flash, collect — registered with
{func}`@instruction() <otto.cli.run.instruction>` and dispatched as an
`otto run` subcommand. Where {doc}`suites <test>` produce verdicts across
many test methods, an instruction has one body and one outcome.

## What registration synthesizes

The decorator stores an entry in the `INSTRUCTIONS` registry
({doc}`../subsystems/registries`) and builds a Typer sub-app around the
function, transforming its signature on the way:

- **Options expansion.** If the function declares a parameter annotated with
  an options dataclass, the decorator expands the dataclass's fields —
  including inherited ones, which is how repo-wide `RepoOptions` bases work
  ({doc}`../../guide/options`) — into individual CLI flags, and reconstructs
  the populated instance at call time. Suites use the same machinery for
  their `Options` class, so one options hierarchy serves both.
- **Context injection.** A parameter annotated `OttoContext` is stripped from
  the CLI signature and injected from the active context at call time — the
  DI-friendly way for an instruction to reach hosts without global lookups
  ({doc}`index`).

## What is unique about `run`

The full preamble applies (lab, output dir, reservation gate), and then the
body is just the user's coroutine on the invocation's event loop. The
instruction's returned {class}`~otto.result.Result` (if any) becomes the
process exit code ({doc}`../utilities/results`); artifacts belong in
`get_context().output_dir` ({doc}`../../guide/run`).

Because instructions live in the `INSTRUCTIONS` registry, tab completion of
their names comes for free — these candidates are the demo repo's registered
instructions, resolved by the real completion machinery:

```{raw} html
:file: ../../_static/generated/termynal/complete-instructions.html
```

## `otto run --help`

```{raw} html
:file: ../../_static/generated/termynal/help-run.html
```
