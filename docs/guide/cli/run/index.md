# otto run

`otto run` executes **instructions** -- async functions that have full access
to the lab's hosts.  Each instruction becomes its own subcommand with typed
CLI options.

## `otto run --help`

```{raw} html
:file: ../../../_static/generated/termynal/help-run.html
```

## Instructions you already have

Six of them — `install`, `uninstall`, `cleanup`, `get-logs`, `install-tools`
and `status` — ship with otto and work in any lab whose repos have declared
products, with no code of your own.  They are ordinary instructions over the
`otto.project` library, listed in their own panel by
`otto run --list-instructions`.  A repo customizes what they do by registering
a `ProjectActions` subclass, never by defining an instruction of the same name
(which is refused at startup).  See {doc}`defaults`.

## Running instructions

```bash
otto --lab my_lab run deploy                # run with defaults
otto --lab my_lab run deploy --debug        # pass a flag
otto run --list-instructions                # see all available instructions
```

An instruction belongs to the repo that registered it, and otto refuses to
dispatch one whose repo is not active for this invocation — the loaded labs
decide that by default, and `-I`/`-E` override it. See {doc}`../projects`.

Because instructions are registered by name, tab completion of their names
comes for free — these candidates are the demo repo's registered
instructions, resolved by the real completion machinery:

```{raw} html
:file: ../../../_static/generated/termynal/complete-instructions.html
```

## Logging and artifacts

Every `otto run` invocation creates an output directory under `--xdir`:

```text
<xdir>/run/<timestamp>_<instruction_name>/
```

The timestamp is UTC with millisecond precision (e.g.
`run/20260702_143512_042_deploy/`), so directories sort chronologically —
see the [CLI reference](../index.md#output-directories) for the
layout every command uses.  Use the active context's `output_dir` to
write artifacts there:

```python
from otto import get_context

output_file = get_context().output_dir / "results.json"
```

## Dry run

Use `--dry-run` (or `-n`) to preview what would happen without running any
commands on hosts:

```bash
otto --lab my_lab --dry-run run deploy
```

Commands and file transfers are skipped, but connections are still verified.

## Options

Run registered instructions.

```text
otto run <instruction> [OPTIONS]
otto run --list-instructions
```

| Option | Description |
| ------ | ----------- |
| `--list-instructions` | List all available instructions and exit |

Each instruction defines its own options via Typer annotations.  Use
`otto run <instruction> --help` to see them.

The six first-party instructions (`install`, `uninstall`, `cleanup`,
`get-logs`, `install-tools`, `status`) walk each repo's
{ref}`fleet of interest <project-scope>` rather than the whole loaded lab.  A
repo whose declaration admits no host in this run — no loaded lab applies to
it, or none of their hosts match its `host_patterns` — is skipped with a
`WARNING` and reported by `otto run status` as `not applicable` or
`no matching hosts`; if it is the project driving the run, the verb aborts
instead.  See {doc}`defaults`.

```{toctree}
:caption: Topics
:hidden:

defaults
```
