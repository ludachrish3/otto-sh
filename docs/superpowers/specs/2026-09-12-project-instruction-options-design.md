# Project instructions: per-repo options on orchestrated instructions

**Status:** approved by Chris 2026-09-12 (brainstorm in session; decisions recorded here).
**Extends:** `2026-08-16-first-party-default-instructions-design.md`, whose §6 deferred
"a first-class option-merge scheme". This spec is that scheme, and it lifts the
"every instruction with a first-party name is refused" rule into a general one.

## The design, one sentence

A **project instruction** is an `@instruction(options=...)` declared as a
`ProjectActions` method — a name, a walk shape, a result combiner, and one body per
repo; otto's six defaults are declared exactly that way on the base class, a repo
overrides or adds one the same way, and `otto run <name>` exposes the union of every
registered body's options while each body receives its own typed class.

## Vocabulary

- **Standalone instruction** — `@instruction` on a free function, as today. One body,
  one repo, its own command. Unchanged by this spec.
- **Project instruction** — `@instruction` on a `ProjectActions` method. One body PER
  REPO; `otto run <name>` walks every applicable repo's body through the orchestrator.
  The six defaults (`install`, `uninstall`, `cleanup`, `get-logs`, `install-tools`,
  `status`) are project instructions otto declares.
- "Verb" is NOT used for either: the CLI docs already use it for top-level `otto`
  subcommands ("one page per verb").

## Why (recorded so the rationale outlives the transcript)

Today the six default instructions carry a fixed, lab-wide flag set
(`src/otto/project/instructions.py`) and a repo's `ProjectActions` override sees only
`self.repo` and `self.ctx`. A repo cannot add `--variant` to `otto run install`. The
tempting workaround — a differently named instruction that calls the orchestrator with
extra state — recreates the split-brain the first-party design exists to prevent,
because an `ensure("installed")` marker would not carry the argument.

The governing concern (Chris): **provide convenient first-party defaults and let
third-party code extend the interface by the exact same mechanism.** The six defaults
are not special commands with a private override path; they are the first-party
instances of an interface a repo extends. Two repos with a body for one name compose by
the orchestrator's walk, the way `install` already composes across repos.

Two things Chris took back during the brainstorm, recorded so they are not
re-proposed: (1) automatic repo-wide options with no explicit inheritance — today's
composition IS explicit inheritance (`class _Opts(RepoOptions)`) and that stays the one
thing a user writes; (2) merging same-named *standalone* instructions — only project
instructions merge, because only they have an execution order.

## 1. Project instructions

A project instruction has:

| Property | Set by | Meaning |
| --- | --- | --- |
| `name` | method name (or explicit `name=`) | `otto run <name>`; the walk's label in failures |
| `walk` | `"forward"` / `"reverse"` | dependency order (a dependency before its dependents), or the reverse |
| `continue_on_failure` | bool | True attempts every repo and reports the first failure seen; False stops at the first failing repo |
| `require_dependencies` | bool | True refuses to start when a kept repo's required dependency was dropped by the loaded lab (building on a missing provider cannot succeed); False walks whatever is present, which is what a teardown wants |
| `combine_results` | callable | `{repo_name: body_return} -> the instruction's single return value`; default is the first-failure combiner with the repo name stamped into the message |
| `render` | callable or None | turns the combined value into what the leaf prints and exits on; None means the leaf's existing handling (Result / CommandResult / None / bare payload) |
| bodies | one per repo | the `ProjectActions` method, with its own options class |

**All walk-shape properties (`walk`, `continue_on_failure`, `require_dependencies`,
`combine_results`, `render`) are fixed by the FIRST declaration of the name.**
`otto.project.actions` is imported before any repo init (bootstrap phase 2), so otto's
six are fixed before a repo can speak. A repo redeclaring a first-party instruction with
any of those keywords fails at init, naming the keyword. A repo adding a new project
instruction sets them; a second repo redeclaring that name inherits them and may not
restate them differently.

`continue_on_failure` is a walk property, not a combiner concern, because it decides
whether the next repo runs at all; the combiner only ever sees the results that exist.

### Declaration

```python
class ProjectActions:
    @instruction(options=InstallOptions, walk="forward",
                 continue_on_failure=False, require_dependencies=True)
    async def install(self, opts: InstallOptions) -> Result: ...

    @instruction(options=CleanupOptions, walk="reverse",
                 continue_on_failure=True, require_dependencies=False)
    async def cleanup(self, opts: CleanupOptions) -> Result: ...

    @instruction(options=StatusOptions, walk="forward",
                 continue_on_failure=True, require_dependencies=False,
                 combine_results=aggregate_status, render=print_status)
    async def status(self, opts: StatusOptions) -> InstallState: ...
```

A repo:

```python
from otto.project import InstallOptions, ProjectActions, register_project_actions

@options
class WidgetInstallOpts(InstallOptions):          # MUST inherit the first-party class
    variant: Annotated[str, typer.Option(help="Firmware variant.")] = "field"

@register_project_actions
class WidgetActions(ProjectActions):
    @instruction(options=WidgetInstallOpts)          # overrides a first-party body
    async def install(self, opts: WidgetInstallOpts) -> Result: ...

    @instruction(options=DeployOpts, walk="forward",
                 continue_on_failure=False, require_dependencies=True)
    async def deploy(self, opts: DeployOpts) -> Result:   # a new project instruction
        ...
```

`@instruction` on a method (detected by the leading `self` parameter — a function
declared inside a class body) registers into the **project-instruction table** keyed by
name instead of building a standalone Typer command. On a free function it behaves
exactly as today. A method body has `self.ctx`, repo-scoped, so nothing is handed in
beyond its options.

### First-party options classes are the base a repo inherits

`otto.project` exports `InstallOptions`, `UninstallOptions`, `CleanupOptions`,
`GetLogsOptions`, `InstallToolsOptions` and `StatusOptions` — `@options` classes in
`__all__`, each carrying exactly the flags that instruction has today.

**Rule, checked at registration:** an override of a first-party instruction must
declare an options class that inherits the first-party class for that name; otherwise
init fails naming the repo, the instruction and the class it must inherit. Without the
rule a lone repo overriding `install` with an unrelated class would make `--ensure`
vanish from the CLI, and `await super().install(opts)` could not read its fields. A
repo's own new project instruction has no base, so it declares freely. The same-class-
object merge rule (§2) then guarantees every repo's `--ensure` is one flag.

A repo that overrides a body and wants otto's default behaviour for part of it calls
`await super().install(opts)`; the base body reads only the fields otto declared.

### Bodies and the six defaults

The thin wrappers in `otto.project.instructions` are deleted. The `otto run` app builds
each project instruction's Typer command from the table at bootstrap, so the six
defaults and a repo's `deploy` reach the CLI by one path and appear in the same
`--list-instructions` panels (first-party panel for otto's six; a repo's added project
instruction under that repo).

Lab-wide steps stay in the orchestrator, never in a body: the debug-log sweep, toolchain
removal, impairment reset and tunnel reap remain the fixed tail of `cleanup`, and the
debug sweep the tail of `uninstall` and `get-logs`. Their flags (`--debug-logs`,
`--reset-impairments`, `--remove-tunnels`, …) are fields on otto's options class for
that instruction; the orchestrator reads them off the same instance it hands each repo.
So a repo's `CleanupOpts(CleanupOptions)` carries `--debug-logs` by inheritance and the
orchestrator reads it from the repo's instance without the repo doing anything.

`install --ensure` and `--recover-partial` are fields on `InstallOptions`; the
orchestrator's `ensure_installed` reads them from the same instance (§3), so the CLI and
the fixture cannot disagree on the recovery rule.

`status` keeps its current semantics: bodies return `InstallState`, `combine_results`
is the existing tri-state fold with the counted-repo rule and scoping rows, `render`
prints the per-repo table (and the cleanliness table under `--full`) and returns the
`_STATE_ANSWERS` Result the leaf exits on.

### The first-party name guard, generalised

A **standalone** `@instruction` may not take a project instruction's name — any project
instruction, not just the six. The message stays: override or extend by a
`ProjectActions` method. A **method** declaration of a first-party name is the
sanctioned override and passes. The registry-order backstop for a hand-built
`InstructionEntry` is unchanged.

## 2. Options: union, ownership, collisions

At bootstrap, per project instruction, otto collects the options class of every
registered body — **active or not**. Activation is enforced at use (project-activation
spec), and a flag set that changed with `-I/-E` or the loaded lab would break `--help`
and the completion cache. The CLI flag set for `otto run <name>` is the union of their
fields, expanded by the same `options_params` path `_wrap_with_options` uses today.

Merge rule, per field name across the bodies of one project instruction:

- **Same declaring class object on every side** — one flag. Its parsed value is delivered
  to every body whose class carries that field.
- **Different declaring classes** — bootstrap error naming the instruction, the field,
  and the repos on each side, with the hint: *share one base class, in a required
  dependency or a library package, or rename the field.*
- **Same repo, two instructions, same field** — no conflict; they are separate commands.

"Declaring class" is the class in the MRO whose `__annotations__` introduce the field.
Two independent repos each writing `lab_env: str` collide even with identical type and
default: strict now, per Chris; automatic namespacing (`--widget-lab-env`) is the
recorded future relaxation if loud collisions prove unfriendly.

At call time each body receives its **own** class, constructed from the subset of parsed
flags its class declares and validated by pydantic exactly as `_wrap_with_options` does.
Per-repo IDE typing is therefore exact: a body only ever sees the class it annotated.

### Ownership (documented, not enforced)

A shared base lives in a **required** dependency or a library package, never in an
optional repo. Bootstrap imports every configured repo's init, so an optional repo that
is configured but inactive this run is still importable and the inheritance works; but
an optional repo **absent from the workspace** is not on `sys.path`, the importing
repo's init dies on the import, and the activation design turns that into "broken
sibling" — the dependent silently drops out of the run because of a flag base. The
collision hint names this rule; the docs state it beside the sharing recipe.

Repo dependencies are a DAG (the orchestrator topologically sorts them) and options
modules are leaves (typer + `otto.options`), so following ownership down the graph can
never cycle.

### Inactive repos' flags

They exist on the command because the union is over registered bodies. The
orchestrator's existing applicability filter skips the body, so a value passed for an
inactive repo is accepted and unused — consistent with today, where an inactive repo's
instruction still appears and dispatch refuses it.

## 3. The ensure path

`ensure_installed` / `ensure_uninstalled` / `ensure_clean` keep calling the orchestrator,
so a marker runs the same bodies `otto run <name>` runs. What changes is where each
body's options come from. Under `otto test` there are no project-instruction flags; the
fixture builds each repo's options class for the instruction as follows:

- a field takes the suite's value when the suite's `Options` class and the repo's
  options class both inherit that field **from the same declaring class object** (the
  same rule §2 uses for "one flag");
- every other field takes its default;
- pydantic then validates the instance, so a bad default fails the test naming the
  field rather than installing something odd.

Matching by declaring class, not by name (Chris chose B): a suite field that merely
spells the same name as an unrelated install field does not leak in, and the CLI and
the fixture agree on what "shared" means.

No new flags appear on `otto test`. A repo that wants a test to steer its install
promotes the field into the base its suites already inherit — the one piece of explicit
inheritance the user writes, and the piece they write today.

## 4. Bootstrap, completion cache, failure modes

- **Registration order.** `otto.project.actions` is imported before any repo init, so
  the six names and their walk-shape keywords exist before a repo declares anything.
- **Completion cache.** Instructions are serialised with their options today; a project
  instruction serialises the merged flag set under its name, so cached `--help` and
  completion match a live bootstrap. The fingerprint already covers every init module's
  source, so a changed options class invalidates it.
- **Collision at bootstrap fails the whole invocation**, not the repo. A flag set the
  user cannot see is not something to degrade around; the activation design's
  broken-sibling softening applies to import failures, not declared conflicts.
- **Redeclaring a walk-shape keyword** on an existing name fails at init naming the
  keyword and both declaring repos (otto counts as a declarer).
- **An override without `options=`** is refused for a first-party name (the inheritance
  rule above requires a class). For a repo's own project instruction it is a body with
  no flags.

## 5. Testing

- Project-instruction table unit tests: union; same-class-object dedupe delivers one
  value to both bodies; cross-repo collision message names instruction, field and repos;
  first declaration fixes keywords; a repo restating a keyword is refused; a standalone
  function on a project-instruction name is refused; a method on a first-party name
  passes; an override whose options class does not inherit the first-party class is
  refused.
- Ensure matcher unit tests: shared-base field flows; same-name-different-class does
  not; defaults fill; a validation error names the field.
- Existing orchestrator and default-instruction tests carry over — bodies, walk order,
  the lab-wide tails and the `status` fold are unchanged; their flags move onto options
  classes.
- **Differential test:** `otto run install --ensure --lab-env x` and a suite with
  `ensure("installed")` under `--lab-env x` must produce identical per-repo option
  instances. This is the split-brain guard and the test that MUST be able to fail (the
  fixture path built by name, or with defaults only, must turn it red).

## 6. Documentation

- **Getting started gains two pages**, giving this sequence after `defining-hosts/index`:
  1. `defining-products-and-tools` — "now that hosts exist, here is what goes on them":
     registering products and dev tools, and what the six default project instructions
     do with zero further configuration. Sits right after defining hosts.
  2. `customizations` (existing, host customization) stays next.
  3. `customizing-project-instructions` — subclassing `ProjectActions`, overriding one
     default with an inherited options class, adding a per-repo flag, adding a new
     project instruction. The advanced step, last.
  Both new pages link to the guide/library pages for the full treatment (one home per
  topic; link, never restate).
- `docs/guide/cli/run/defaults.md` and `docs/library/writing-instructions.md` gain the
  project-instruction declaration, the merge rule, the inheritance rule and the
  ownership rule; doctests stay lab-free. The "collision error" section is rewritten for
  the generalised guard.
- `otto init`'s scaffold comment that names the six refused names is updated to the
  project-instruction rule and shows the inherited-options override shape.

## 7. Compatibility

**This is a breaking change by otto's own rule** (`docs/contributing.md`, "Branching and
commits"): `ProjectActions`, `register_project_actions` and the `otto.project`
orchestrator functions are deep import paths the docs teach (`defaults.md`,
`writing-instructions.md`, the `otto init` scaffold comment), and this spec changes their
signatures:

- `ProjectActions.install(self)` → `install(self, opts)`; `uninstall(self,
  get_product_logs=)`, `cleanup(...)`, `get_logs(...)`, `install_tools(...)` lose their
  keyword parameters in favour of an options instance. An existing subclass override
  written against the old shape stops being called correctly.
- `otto.project.install(ensure=, recover_partial=)`, `uninstall(...)`, `cleanup(...)`,
  `get_logs(...)`, `install_tools(...)`, `ensure_installed(recover_partial=)` take an
  options instance (or nothing, for defaults) instead of keywords.
- `otto.project.instructions` is deleted.

Unchanged, so scripts and users do not notice: every `otto run <name>` flag spelling
and default; `@instruction` on a free function; the ensure marker vocabulary; the
`status` exit codes.

No compatibility shim: otto is pre-1.0 (0.12.x), a `!` commit bumps MINOR, and a shim
that accepted both override shapes by signature inspection would leave two calling
conventions in the codebase for the sake of subclasses that, today, are the scaffold
comment and the docs example. The commit carries `feat(project)!:` and a
`BREAKING CHANGE:` footer naming the three bullets; `make check-breaking` will demand
it once `public_api.txt` changes.

## Deferred (recorded so they aren't re-litigated silently)

- Automatic flag namespacing on cross-repo collision (`--<repo>-<field>`).
- Class-level shared options on `ProjectActions` (one class every project instruction
  reads), as an extension of per-instruction options — Chris: "B first, eventually both".
- Per-host / per-board option patchwork (still deferred from the first-party spec).
- Orchestrator override / conductor hook.
