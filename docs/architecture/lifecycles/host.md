# otto host — verbs from methods

`otto host <id> <verb>` is not a hand-written command set: every verb is
*synthesized* from a `@cli_exposed` coroutine method on the resolved host's
class. The Python API is the source of truth and the CLI is a projection of
it — add a method, get a subcommand.

## Class-scoped synthesis

The group behind `otto host` (`HostGroup` in `otto/cli/expose.py`) collects
`@cli_exposed` methods across **every registered host class** — built-in and
project-registered alike — and then filters the visible, dispatchable set to
the verbs defined on the class of the host actually named on the command
line. Scoping falls out of method *definedness*: `UnixHost` defines `lsmod`,
so `otto host router1 lsmod` exists; an `EmbeddedHost` doesn't, so the verb
isn't offered there. A project that registers `MyHost` with a `@cli_exposed`
method gets its verb with no extra wiring — the same first/third-party
symmetry as everywhere else ({doc}`../subsystems/registries`).

Each verb's flags come from the method's own signature, via the same
options-to-parameters machinery instructions and suites use; `Arg`/`Opt`/
`Exclude` annotations fine-tune what the CLI projection looks like without
touching the Python call shape.

## Rendering and exit codes

A verb's return value is rendered by one shared path: members of the
{class}`~otto.result.Result` family drive both output and the exit code
(`result.exit_code`, ssh-like semantics — {doc}`../utilities/results`);
`None` means side-effect-only success; any other value is the documented
third-party fallback — printed as-is, exit `0`. Command output itself
streams live during execution, so a successful `run` verb prints nothing
extra at the end.

## Completion, scoped like the verbs

Tab completion mirrors the synthesis model at every position. Host ids come
from the completion cache's snapshot (falling back to a live lab scan on a
cold cache — completion never runs user code,
{doc}`../subsystems/registries`); note the built-in `local` host in the
candidates:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-ids.html
```

Once a host id is typed, the verb candidates are *that host's class menu* —
the same definedness scoping that decides what is dispatchable:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-verbs.html
```

And option values backed by registries complete from the registry — here the
term backends, so a project-registered backend completes exactly like a
built-in:

```{raw} html
:file: ../../_static/generated/termynal/complete-term-backends.html
```

## What is unique about `host`

- The full preamble applies, but *read-only* verbs (`ls`, `exists`,
  `read-file`, …) are registered with `output_dir=False` — inspecting a host
  should not litter `--xdir` with empty run directories.
- Per-invocation `--term` / `--transfer` / `--hop` overrides apply option
  overlays to the one resolved host before the verb runs
  ({doc}`../../guide/host/connections`).

## `otto host --help`

```{raw} html
:file: ../../_static/generated/termynal/help-host.html
```
