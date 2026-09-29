# Instruction registration leaves the CLI — design

**Date:** 2026-09-28
**Status:** approved in conversation; awaiting written-spec review
**Scope:** item 1 of the thin-CLI layering series (audit of 2026-09-28)
**Fixes:** #502, #501 (partly: the library `status()` path is item-1 adjacent, see §9)

## 1. Intent

otto's design principle: the CLI layer does input validation, tab
completion and lifecycle orchestration, then hands off to a library
(otto's own or a third party) that owns the logic, so a Python caller gets
the same behaviour without the CLI.

Today the `@instruction` decorator, the registry entry's Typer factory and
the project-instruction command builder all live in or reach into
`otto.cli`. The consequences the audit measured:

- `otto.project.actions` imports `otto.cli.run` at module top, so
  `import otto.project.orchestrator` loads typer, rich, `otto.cli.run` and
  `otto.cli.invoke`. tach.toml records this as the one deliberate upward
  edge, and names it as the fix the `config -> cli` debt also needs.
- `@instruction` returns the prepared CLI wrapper, not the function. A
  decorated `async def deploy(opts: Opts)` cannot be called as
  `await deploy(Opts())` (TypeError), and `await deploy(opts=Opts(debug=True))`
  silently rebuilds the options from defaults. The `ParamSpec` typing says
  the signature is preserved. (#502)
- `InstructionEntry` holds only `make_app`, so no library runner can exist.
- `otto.params.build_options` raises `typer.BadParameter` into library
  paths (`OptionsSource.build`, `OttoContext.bind_verb_options`).
- `otto.project.commands` builds Typer apps, imports private typer and a
  private `otto.cli.invoke` name, and re-implements the leaf wrapper's
  dry-run and sensitive-field handling.

After this change: no module under `otto.project` or `otto.instructions`
imports `otto.cli`; `@instruction` returns the function unchanged; the
registry holds data and the CLI projects it into Typer; a library
`run_instruction(ctx, name, opts)` dispatches with the same options
binding `otto run` uses; and a gate keeps the edge from returning.

## 2. What this deletes

Named so the plan's final task can grep for each:

| Deleted | Where it lived |
| --- | --- |
| `InstructionEntry.make_app` and every `make_app=` construction | `otto/instructions.py`, `otto/cli/run.py`, `otto/project/commands.py`, tests |
| `_command_for`, `_leaf`, the `typer` import and both `otto.cli` imports | `otto/project/commands.py` |
| The decoration-time `prepare_command_target(func, options)` call and the `target` return | `otto/cli/run.py` |
| The `from ..cli.run import instruction` module-scope import | `otto/project/actions.py` |
| `typer.BadParameter` | `otto/params.py` |
| The `otto.cli` edge and its justification note | `tach.toml`, `otto.project` entry (and the matching half of the `otto.suite` note) |

Rules that end with exactly one home:

| Rule | Home after |
| --- | --- |
| Binding a leaf's parsed values to its handler's parameters (own class, verb classes, `OttoContext` injection, registered-class injection) | `otto.instructions.bind_handler_kwargs` |
| Building the Typer app for an instruction, either kind | `otto.cli.run.build_instruction_app` |
| An options value that fails validation | `otto.params.OptionsValidationError` |

## 3. The registry entry (`otto.instructions`)

`InstructionEntry` becomes pure data, one shape for both kinds:

```python
@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    name: str
    module: str
    handler: "Callable[..., Awaitable[Any]] | None" = None
    options_cls: "type[DataclassInstance] | None" = None
    project: "ProjectInstruction | None" = None
    help: str | None = None
    registered_by: str | None = None
```

- Exactly one of `handler` and `project` is set. `__post_init__` raises
  `ValueError` otherwise.
- `handler` is the undecorated async function. `options_cls` is the class
  named by `options=`, or `None`. `help` is the decorator's `help=`, carried
  so it reaches the command built on resolution; `None` lets the command
  take the handler's docstring.
- `project` is the `ProjectInstruction` (spec plus bodies) that
  `PROJECT_INSTRUCTIONS` already holds; the entry references it rather than
  copying it.
- `module` and `registered_by` keep their meaning and their readers
  (`Repo.get_instructions_panel`, `first_party_instructions_panel`,
  `refuse_inactive_instruction`).

`otto.instructions` stays typer-free: `typer` remains under
`TYPE_CHECKING` only, and the module imports nothing from `otto.cli`.

## 4. The decorator (`otto.instructions.instruction`)

`instruction()` moves from `otto.cli.run` to `otto.instructions` with its
signature and every documented behaviour unchanged:

- Refuses a non-`async def` at decoration (`TypeError`, same message).
- Refuses `options=X` with no parameter annotated `X` (`TypeError`). This
  is the one check the decoration-time `prepare_command_target` call
  provided; it becomes a small pure function, `options_parameter(func,
  opts_cls) -> str | None`, that the decorator, `bind_handler_kwargs` and
  the CLI wrapper all use.
- On a `ProjectActions` method (first parameter `self`): stamps the
  `ProjectInstructionMark` and returns the function, as today. The
  walk-shape keywords are legal only there, as today.
- Refuses a repo claiming a first-party or project-instruction name, keyed
  on `get_registering_repo()`, as today.
- Registers `InstructionEntry(name, module, handler=func,
  options_cls=options, registered_by=repo)`.
- **Returns `func` itself.** The `ParamSpec` typing is then true.

The command name is derived by a local rule, `command_name(func_name)`:
lowercase, underscores to hyphens, which is what
`typer.main.get_command_name` returns for these inputs. A unit test pins
the two equal over a small corpus so a typer change is caught.

The verb-flag collision check does not run at decoration; it never did in
a useful way (the decoration-time prepare ran without the verb). It stays
where it is found today: when `otto run` resolves the command.

`otto.cli.run` re-exports `instruction`, so `from otto.cli.run import
instruction` keeps working. `otto.instructions.instruction` is the
documented import from now on, and the init template switches to it.

## 5. The CLI projection (`otto.cli.run`)

`run_app`'s registry group keeps its `app_of` seam. The callable becomes
`build_instruction_app(entry) -> typer.Typer`, built on resolution:

- **Standalone entry** (`entry.handler` set):
  `prepare_command_target(entry.handler, entry.options_cls, verb="run",
  repo=entry.registered_by)`, the call today's `make_app` makes. The
  verb-flag merge, the collision error, `OttoContext` injection,
  registered-class injection, dry-run finishing and the sensitive-field
  stamp are unchanged.
- **Project entry** (`entry.project` set): `_project_leaf(entry)`, the
  leaf `otto.project.commands._command_for` builds today, moved here
  verbatim in behaviour: merged body options first, then the `run` verb's
  classes; bind on the context; the dry-run branch; then
  `await orchestrator.run_project_instruction(name, kw)`. The private
  `_leaf_declares_preview` import disappears because the leaf now lives in
  the package that owns it.

The wrapper in `otto.cli.invoke._wrap_with_options` calls
`bind_handler_kwargs` (§7) for the split/bind/inject step it performs
today in `_bind_and_build_own`, so the CLI leaf and the library runner
cannot drift on which parameter gets what.

The completion cache's one `entry.make_app()` call
(`otto/config/completion_cache.py:1316`) becomes
`build_instruction_app(entry)`. That module already imports `otto.cli` and
stays on the recorded debt list until item 7; no new edge.

## 6. Project instructions (`otto.project.commands`)

`publish_project_instructions()` keeps its contract (runs after every
repo's init; republishes only its own prior entry; leaves any other prior
entry for the registry to refuse) and shrinks to:

1. `merge_option_params(_body_origins(entry), what=...)` for the early
   cross-repo collision error, as today.
2. `INSTRUCTIONS.register(name, InstructionEntry(name=name,
   module=entry.spec.module, project=entry, registered_by=None),
   overwrite=republish, origin=__name__)`.

`merged_option_params(entry)` stays public; `otto.cli.run._project_leaf`
uses it. The module's `typer` import and both `otto.cli` imports go.
`otto.project.actions` imports `instruction` from `otto.instructions`.

## 7. The library runner (`otto.instructions.run_instruction`)

```python
async def run_instruction(
    ctx: OttoContext, name: str, opts: list[object] | None = None
) -> Any: ...
```

- `ctx` is explicit, per the principles page ("dependencies flow through
  the context"). The runner installs `ctx` as the active context for the
  duration of the call with the existing `set_context`/`reset_context`
  token pair when it is not already active, and leaves an already-active
  one alone. The handler body's `get_host()` and `get_context()` therefore
  see the same object whose options were bound.
- Unknown `name`: the registry's own did-you-mean lookup error.
- `opts` is a list of options instances, the shape `run_tests(options=)`
  takes: the instruction's own class and any class registered for `run`.
  `flatten_option_instances(opts, verb="run")` turns them into kwargs
  (its verb check now also admits the entry's own class). The context
  binds the verb's classes from those kwargs under
  `verb_binding_preserved()`, so the caller's context comes back with its
  prior binding. A registered class not passed is built from defaults; a
  required field with no value raises `OptionsValidationError`.
- **Standalone**: `await entry.handler(**bind_handler_kwargs(ctx, entry,
  kwargs))`.
- **Project**: `await orchestrator.run_project_instruction(name, kwargs)`
  (imported lazily; the orchestrator reads the ambient context by design,
  which the first bullet makes the same object).
- Returns the handler's return value. No rendering, no exit code.

`bind_handler_kwargs(ctx, entry, kwargs) -> dict[str, Any]` is the pure
rule both the runner and the CLI wrapper use: split the own class's
fields out of `kwargs` and build the instance (or take the one supplied),
inject `ctx` into a parameter annotated `OttoContext`, inject
`ctx.options(cls)` into a parameter annotated with a class registered for
the verb, and pass every remaining kwarg through. It imports `otto.context`
lazily; `import otto.instructions` stays as cheap as it is.

## 8. Options validation (`otto.params`)

`build_options` raises `OptionsValidationError(OttoError, ValueError)`
with the same message text it builds today. `otto.cli.invoke` translates
it to `typer.BadParameter` in one helper used by `_bind_and_build_own`
and the project leaf, so `otto run`'s exit-2 usage error is unchanged.
`OttoContext.bind_verb_options`'s docstring is corrected. The rest of
`otto.params` (the `typer.Option` metadata on option fields, the
parameter synthesis) is unchanged; option modules are leaves on `typer`
and `otto.options` by an earlier decision.

## 9. Gates, tach, docs

- **tach.toml**: `otto.project` drops `otto.cli` from `depends_on`; the
  note explaining the upward edge is replaced by one sentence saying it
  is gone, and the matching half of the `otto.suite` note is trimmed. The
  header's component count is re-measured once with
  `forbid_circular_dependencies = true` and the new figure recorded; it is
  not adjusted by hand. `otto.instructions` declares its lazy edges to
  `otto.context`, `otto.params` and `otto.project` (tach counts
  function-scope imports).
- **New ast-grep rule `no-cli-import-outside-cli`**: bans `otto.cli` and
  `..cli` imports in `src/otto/**` at any scope, `severity: error`,
  baseline zero, with an `ignores` list naming each file still carrying
  recorded debt and the item that retires it:
  `config/completion_cache.py`, `config/completion_tree.py`,
  `config/completion_stubs.py`, `reservations/factory.py` (item 7),
  `__init__.py` and `_shim.py` (the package's own CLI entry points,
  permanent). Rule-test pair alongside.
- **`typer-exit-outside-cli`** gains `typer.BadParameter` beside
  `typer.Exit`.
- **Import budgets**: the per-verb import guard checks that
  `import otto.instructions` and `import otto.project` stay typer-free.
  A moved ceiling is presented for approval before re-baselining.
- **Docs**: `docs/architecture/subsystems/execution.md` "Where the code
  lives" corrected (`INSTRUCTIONS` and `@instruction` in
  `otto.instructions`; context injection in `otto.cli.invoke`);
  `docs/cookbook/authoring/writing-instructions.md` uses the
  `otto.instructions` import and gains a "calling an instruction from
  Python" example with `run_instruction`; `docs/cookbook/python-library.md`
  links it; `docs/architecture/subsystems/registries.md`'s instruction row
  describes the data entry. The public-API golden check will flag the new
  names; that is the intended review point.

Out of scope, deliberately: the orchestrator taking a `ctx` parameter
(its ambient-context shape is documented and shared by six verbs);
splitting the typer parameter synthesis out of `otto.params`; moving the
completion cache's typer half (item 7); `status --full` on the library
path (#501) beyond what the runner now makes reachable.

## 10. Testing

- **Decorator**: `@instruction` returns the function object itself;
  `await deploy(Opts(debug=True))` receives that instance; the async
  rule, the missing-options-parameter check and the first-party-name guard
  still raise at decoration; the entry carries `handler` and
  `options_cls`; `command_name` equals `typer.main.get_command_name` over
  a corpus.
- **Runner**: with an `OttoContext` over a hostless `Lab`,
  `run_instruction(ctx, ...)` dispatches a standalone handler with
  `OttoContext` and registered-class parameters injected; binds and then
  restores the verb options; installs the context only when it was not
  active; routes a project name through the orchestrator; raises the
  registry's unknown-name error; raises `OptionsValidationError` for a
  missing required own field.
- **CLI parity**: `tests/unit/cli/test_run*.py`,
  `test_project_instruction_commands.py`, `test_instruction_ownership.py`
  and `test_run_verb_options.py` build apps through
  `build_instruction_app(entry)` and keep their assertions.
  `tests/unit/project/test_ensure_differential.py` stays as is: it is the
  proof that `otto run install`, `await otto.project.install()` and the
  `ensure` fixture did not diverge.
- **Boundary**: the ast-grep rule with its rule-test pair; a unit test
  that `import otto.instructions` and `import otto.project.orchestrator`
  leave `typer` and every `otto.cli` module out of `sys.modules`.
- **Gates**: targeted runs per task; the full squash gate once
  (`make coverage`, `nox -s tests_hostless-3.14`, `make typecheck`,
  applicable bed lanes, `make gate-fresh`) before hand-back. Bed use is
  coordinated with other active agents first.

## 11. Migration

`from otto.cli.run import instruction` keeps working through the
re-export, so `tests/repo*` and the docs examples change only where the
docs are updated. `otto.cli.init_templates` switches to the new import.
Tests that construct `InstructionEntry(make_app=...)` construct
`InstructionEntry(handler=...)` or `InstructionEntry(project=...)`; tests
that call `.make_app()` call `build_instruction_app(entry)`.
