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

## What is unique about `host`

- The full preamble applies, but *read-only* verbs (`ls`, `exists`,
  `read-file`, …) are registered with `output_dir=False` — inspecting a host
  should not litter `--xdir` with empty run directories.
- `host_id` completion is served from the completion cache's host-id snapshot,
  falling back to a live lab scan on a cold cache — completion never runs
  user code ({doc}`../subsystems/registries`).
- Per-invocation `--term` / `--transfer` / `--hop` overrides apply option
  overlays to the one resolved host before the verb runs
  ({doc}`../../guide/host/connections`).
