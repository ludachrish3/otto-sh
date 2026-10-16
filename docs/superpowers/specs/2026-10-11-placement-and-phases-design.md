# Placement and phases — design (spec 5 of the #590 contract-first series)

**Status:** v3, **approved by the owner 2026-10-11**, with D2 decided as (a), then revised the same
day by the owner's rulings below. **Date:** 2026-10-11.
- **v1** was rejected by both Astra (Codex) and Fable. Their main reasons:
  - it contradicted spec 3b §6, which moves the host specs into `otto.host`;
  - its analysis skipped `otto.declared`, a file no tach module owns, which hides a real
    `host → declared → bootstrap → host` loop;
  - two of its phases broke the ratchet as drawn: the reservations fix added an in-cycle edge,
    and the Lab aggregate joined the cycle;
  - its `session → docker` registry-string hook hid a runtime cycle instead of removing it;
  - the instruction inversion conflicted with `RequireRepo`;
  - tach needed `layers_explicit_depends_on = true` for the declared graph to mean anything.
- **v2** was rebuilt on a whole-tree import model. Both reviewers rated it "ready after
  fixes". Their cross-review settled D1 (keep container auto-start by installing it through the
  public `otto.host` binding).
- **v3** folded in every fix:
  - the first-party instructions became a revision-bearing source of the derived views;
  - the facade sweep and three missing import sites joined the model;
  - the bootstrap initiator is installed when `otto.bootstrap` is imported;
  - the audit checks static and string edges together, same-layer targets included;
  - P3's checklist covers every moved binding;
  - plus Fable's six minor fixes.
- **Owner approval (2026-10-11):** D2 = (a). The registry-string loop between `otto.link.impairer`
  and `otto.link.netem` that v3 had listed as an exception is removed by design (§3.3), not
  approved. The same day the owner decided S5-4: container auto-start is removed. That moots D1
  and its exception; P4 is now the removal (§3.4). Then S5-5: the two remaining exceptions go.
  Layer-mixing facades get a module of their own (§3.11), and `otto.bootstrap` splits into its
  composed-state accessors, which move down, and the composition root, which moves up (§3.9).
  The end state has no named exception.
- **Review of S5-5 (Fable) and the owner's rulings (2026-10-11).**
  - As first drawn, the bootstrap split changed two public entry points
    (`otto.context:open_context`, `otto.suite:run_tests`) and a hand-built context's `repos` in a
    process that never loaded `otto.bootstrap`. That was proposed as decision D3. The owner ruled
    that there is **no public behavior change** beyond S5-4 and D2, so D3 is withdrawn. Instead,
    every public spelling of a name that can start composition loads the composition root when
    the name is resolved (§3.12), and test isolation follows the state to its owners (§3.13).
  - The owner then relaxed S5-1: a landing may grow or hold the ratchet, as long as the series
    ends at an empty `BASELINE`. P1 and P9 are each split into two landings for review (§6).

**Amends:**
- spec 1 (`2026-10-04-public-api-manifest-design.md`): this spec carries out the work spec 1 §9
  and §7 queue for spec 5;
- spec 2 (`2026-10-06-run-state-contracts-design.md`): its §4 rows for `otto.host` (auto-start,
  now removed) and `otto.docker` (the repos accessor);
- spec 3b (`2026-10-06-host-construction-design.md`): carries out its §6 move;
- spec 4 (`2026-10-06-repo-and-scope-inputs-design.md`), S-3: discovery moves into `otto.config`.

§7 lists each amendment.

**Series.** #590 removes the import cycle in five specs. Specs 1–4 settled the public paths and
the seams; spec 5 places every module in a role layer and removes the cycle in eleven landings
(P1a, P1b, P2–P8, P9a, P9b). No landing changes a public path. Each landing is its own plan and
its own squashed commit. The ratchet reaches an empty baseline in P9a, and P9b sets tach to forbid
cycles outright.

## 0. Owner decisions (2026-10-11)

| # | Decision |
|---|---|
| S5-1 | **Landing (revised the same day).** Phases land separately, each as its own plan and squashed commit, gated and pushed on its own. A landing may grow, hold or shrink the ratchet; the series ends at an empty `BASELINE` with `forbid_circular_dependencies = true`. A landing that grows the ratchet adds the new `BASELINE` entries in the same commit (the ratchet test refuses unlisted additions), and its commit body names the entries, the reason, and the later phase that removes them. §6 records every growth and its removing phase. The freedom is for reviewability and cohesion, never for deferring work. |
| S5-2 | **Scope.** The cycle, plus the cleanups spec 1 queued for spec 5: otto.lab's implementation, the modules behind the 27 retired paths (spec 1 appendix G), and internal imports through facades. Each cleanup lands in the phase whose modules it touches, so a module moves once. |
| S5-3 | **Approach.** Layers by role, not by minimum cut. The end state declares the layers in tach, sets `forbid_circular_dependencies = true`, and keeps the ratchet test as an empty-baseline guard. |
| S5-4 | **Container auto-start is removed.** Touching a declared-but-stopped container no longer brings its stack up; it fails fast with an error naming the explicit ways to start it. A breaking behavior change, landed as its own marked commit (phase P4, §3.4). |
| S5-5 | **No named exceptions.** (1) A package facade whose `__init__` exports from more than one layer is its own tach module, at or above the highest layer it exports from, and its lower implementation files are declared at their own layers (§3.11). (2) `otto.bootstrap` splits: the composed-state accessors move down beside the composed-repos catalog, and the composition root moves up, to just below `otto.cli`, where it imports `otto.project` normally (§3.9). |
| S5-6 | **No public behavior change beyond S5-4 and D2** (ruling on the S5-5 review). Every public spelling of every name behaves as today; the composing entry points load the composition root when resolved (§3.12). |
| D2 | **(a) Admit.** When `otto.bootstrap` was never imported, `scope_for_repo` admits rather than starting bootstrap (§3.5). |

Also binding on this spec:
- **Spec 1 §8 P-3:** no public path changes.
- **Spec 1 §9:** internal code imports from the module that defines a name, never through a facade.
- **Spec 1 §7, "File-operation accounting":** no growth on `import_otto`, `version_repo`,
  `completion_repo_warm`, `completion_repo_handover` or `help_repo_warm`; growth elsewhere is named;
  declines are locked in on 3.10–3.14.

Settled in review and recorded as design, not reopened:
- the host specs move into `otto.host` with nominal checks (spec 3b §6);
- `Repo.get_instructions_panel` stays, served by a catalog in a lower layer;
- there is no single run-state record: each piece of run state is its own contract;
- there is no upward registry-string hook, and tach's `include_string_imports` is never set.

## 1. Goal and success criteria

After the last landing:
1. **Ratchet empty, cycles forbidden.** `BASELINE` in `tests/unit/test_import_cycle_ratchet.py` is
   empty, the test stays, and `tach.toml` has `forbid_circular_dependencies = true`.
2. **Layers declared.** `tach.toml` declares the ten layers of §2. Every one of the 77 tach
   modules has a layer and an explicit `depends_on`, and `layers_explicit_depends_on = true` is set.
3. **Every file is owned.** No `.py` file under `src/otto` is outside a tach module. Today 11 are.
4. **Strings are audited.** Every first-party string the import system resolves at runtime
   (facade lazy-table bindings, `Ref` and `import_module` targets) is pinned by a shrink-only audit.
   The audit checks the union of static and string edges for cycles and layer violations, and
   the end state has **no named exception** and no transitional exemption (§5.3).
5. **Public paths and behavior.** No public path changes, and no path recorded in
   `api/public.toml` changes. Every public name behaves as today from every public spelling
   (`otto.<name>` and `otto.<namespace>.<name>` alike), with exactly two decided changes: S5-4,
   the removal of container auto-start (§3.4), and D2, the provider gate's admit (§3.5). §3.12
   shows how the composing entry points keep their behavior after the bootstrap split.
6. **File operations.** Each landing is measured before and after, and none grows a protected
   surface (§4.2).

### 1.1 How this design was checked

The phases were derived on a **whole-tree import model**. The model and its scripts live outside
the repository; they are regenerated at planning time from the plan's base commit.

**What it scans.** Every `.py` file under `src/otto` (375 files) and every import site in them
(4,359): module-scope, function-local, and `TYPE_CHECKING`-only (recorded, never an edge). It also
records the strings the import system resolves: facade lazy-table entries, literal `Ref` and
`import_module` targets, and 46 computed targets on today's tree (the audit regenerates that
inventory on each landing's tree, §5.3). Annotations that pydantic or dataclasses
evaluate at runtime were checked separately and add no cross-module edge: no pydantic model names
a `TYPE_CHECKING`-only import, and the runtime type resolvers only read user handlers or their
own module.

**How it was validated against today's tree.**
- For every tach module with a `depends_on`, the scanned edges equal it exactly. Real
  `tach check --exact`, run on a copy of the tree with all 47 current and new modules declared,
  passes.
- It reproduces the ratchet's `BASELINE`: one cycle of 22 modules with 105 in-cycle edges.
- With every file owned, the cycle is 24 modules and 114 edges, because `otto.declared` and
  `otto.env` join it. 27 real edges are missing from the declared graph today. Two of them hide
  loops:
  - `host → declared → bootstrap → host`, through `host/product.py:40-43`, `declared.py:448` and
    `bootstrap.py:225-226`;
  - `env → bootstrap → … → session → env`, through `env/manage.py:302` and
    `session/dependencies.py:31`.

**What it is not.** The simulator applies each phase as operations on import sites (move a file,
split names out, re-point, drop, add) and computes the graph the transformed tree would have. It
is an abstract import-graph model, not executable transformed code. So every future `depends_on`
and every per-phase count in this spec is **simulator-derived**. Each implemented phase must pass,
on the real tree:
- `tach check --exact`;
- the ratchet test;
- the string audit (§5.3);
- `scripts/import_budget.py` measurements, with ceilings on 3.10–3.14.

If real tach disagrees with this spec, the plan stops and the model is corrected before landing.

**tach behavior the design relies on** (probed on tach 0.35.1):
- With layers on and the default configuration, an undeclared downward import between layers
  passes; with `layers_explicit_depends_on = true` it fails.
- An upward import is a layer violation.
- A module with no layer is exempt from layer checks, so layers can be assigned as modules leave
  the cycle.
- A root `otto` tach module is accepted.
- tach maps an import to the longest-prefix module, counts function-local imports, and ignores
  `TYPE_CHECKING` imports.

## 2. Target layers

Bottom to top. A module may import its own layer and anything below it, and only through an edge
it declares.

| layer | role |
|---|---|
| foundation | Leaf definitions, the slots a higher layer fills (below), and the catalogs read from below. Nothing reaches up. |
| host | Connections, transfer, host classes, and the host specs. |
| specs | Pydantic models for lab, settings and inventory data; the settings home. |
| topology | The Lab model and the services built on hosts: link, check, tunnel, and the monitor's lower files. |
| sources | Where labs, inventory, creds and reservations come from. |
| composition | Settings, `Repo`, repo discovery, the completion cache, `env`. |
| run | The live run: the context implementation (`otto.context.state`), session's six files, the fleet accessors, docker. |
| workflows | instructions, project, init, and `otto.monitor` (its facade and the monitor service). |
| root | The composition root, `otto.bootstrap`: it runs the repos' inits and the checks that need every layer, and imports `otto.project` normally. Beside it, the two public workflows whose entry points can start composition with no run in hand, `otto.suite` and `otto.coverage` (§3.12). |
| entry | cli, testing, examples, the facades `otto`, `otto.lab`, `otto.context` and `otto.session`, `_shim`, `__main__`. |

### 2.1 One installer per slot

A low module may hold a slot that a higher layer fills. Each slot has exactly one named installer,
and its write API is privileged: `tests/unit/test_slot_installers.py` fails if any other module
imports or calls it. Readers may use a slot's read API from its layer upward. This keeps a slot
from becoming a general service locator.

| slot | lives in | installer | privileged API |
|---|---|---|---|
| active run | `otto.invocation` | `otto.context` (`set_context`, `reset_context`) | `install_run`, `restore_run` |
| composed repos | `otto.declared` | `otto.bootstrap` | `publish_composed`, `clear_composed` |
| bootstrap initiator | `otto.declared` | `otto.bootstrap` (root), at import | `install_initiator` |
| first-party actions | `otto.instruction_catalog` | `otto.project.actions` | `declare_first_party_actions` |
| completion snapshot | `otto.registry` | `otto.cli.main` | `set_completion_names` |
| completion snapshot withdrawal | `otto.registry` | `otto.bootstrap`'s `invalidate` | `clear_completion_names` |

**Amended by P5, 2026-10-14.** Building P5 changed this section, and §3.5, §3.9 and §3.13, in
these places. Here it added the last two rows. The CLI's completion
snapshot moved from `otto.bootstrap` into `otto.registry`, a foundation module every command
resolution already loads, and `otto.bootstrap` re-exports its reader, `get_completion_names`.
The reason: `cli/main.py` reads the snapshot function-locally. Had that read gone through the
root, the root's new imports would have ridden onto the nine verb `--help` surfaces, +8 file
operations each; read from `otto.registry`, those surfaces decline by −4 instead. The snapshot
has two writes, so it takes two rows, each with its own single writer: the CLI entry installs it
and `invalidate` withdraws it.

**Amended by P8, 2026-10-15.** The active run's slot is in `otto.invocation`, not a module of its own (§3.8).

### 2.2 Module → layer map (end state, 76 tach modules)

The `depends_on` column is simulator-derived (§1.1); real tach confirms it landing by landing.

| layer | tach module | files | depends_on (end state) |
|---|---|---|---|
| foundation | `otto._shim_complete` | 1 | — |
| foundation | `otto._webassets` | 2 | — |
| foundation | `otto.config.scope` | 1 | errors, models.dependencies |
| foundation | `otto.console` | 1 | — |
| foundation | `otto.declared` | 1 | config.scope, invocation, registry |
| foundation | `otto.errors` | 1 | — |
| foundation | `otto.filesystem` | 1 | — |
| foundation | `otto.instruction_catalog` | 1 | errors, registry |
| foundation | `otto.invocation` | 1 | — |
| foundation | `otto.kmodcov` | 3 | version |
| foundation | `otto.layout` | 1 | — |
| foundation | `otto.lifecycle` | 1 | invocation, logger |
| foundation | `otto.link.impairer` | 1 | link.params, registry |
| foundation | `otto.link.netem` | 1 | link.params |
| foundation | `otto.link.params` | 1 | — |
| foundation | `otto.logger` | 5 | console |
| foundation | `otto.models.base` | 1 | — |
| foundation | `otto.models.dependencies` | 1 | — |
| foundation | `otto.params` | 1 | errors, registry |
| foundation | `otto.registry` | 1 | errors |
| foundation | `otto.result` | 1 | errors, utils |
| foundation | `otto.snmp` | 1 | — |
| foundation | `otto.tls` | 1 | — |
| foundation | `otto.utils` | 1 | errors |
| foundation | `otto.version` | 1 | — |
| host | `otto.host` | 77 | config.scope, console, declared, errors, invocation, kmodcov, layout, lifecycle, link.impairer, logger, models.base, registry, result, snmp, utils |
| specs | `otto.config.home` | 1 | models, models.dependencies |
| specs | `otto.config.user_settings` | 1 | config.home, models |
| specs | `otto.models` | 10 | declared, host, layout, link.impairer, logger, models.base, models.dependencies, utils, version |
| topology | `otto.check` | 10 | errors, host, logger, result, version |
| topology | `otto.link` | 12 | check, errors, host, lifecycle, link.impairer, link.netem, link.params, logger, models, result |
| topology | `otto.monitor.broadcast` | 1 | — |
| topology | `otto.monitor.errors` | 1 | errors |
| topology | `otto.monitor.event_ops` | 1 | errors, models |
| topology | `otto.monitor.events` | 1 | models |
| topology | `otto.monitor.formats` | 1 | — |
| topology | `otto.monitor.log_sourced` | 1 | monitor.parsers |
| topology | `otto.monitor.parsers` | 1 | models, monitor.rates, registry |
| topology | `otto.monitor.rates` | 1 | — |
| topology | `otto.monitor.snmp` | 1 | models.base, monitor.parsers, monitor.rates, registry, snmp |
| topology | `otto.monitor.store` | 1 | models, monitor.events, monitor.parsers |
| topology | `otto.monitor.tls` | 1 | monitor.errors |
| topology | `otto.tunnel` | 14 | check, errors, host, lifecycle, logger, models, registry |
| sources | `otto.creds` | 6 | errors, models, models.base, registry, utils |
| sources | `otto.inventory` | 12 | config.home, config.user_settings, creds, errors, host, models, models.base, registry, tls, utils |
| sources | `otto.labs` | 10 | host, inventory, link, models, models.base, registry, utils |
| sources | `otto.reservations` | 10 | errors, host, invocation, models, models.base, registry, utils |
| composition | `otto.config` | 14 | config.home, config.scope, errors, host, instruction_catalog, inventory, labs, link, models, models.base, models.dependencies, registry, reservations, utils |
| composition | `otto.env` | 4 | config, config.home, errors, models.dependencies, version |
| run | `otto.context.state` | 1 | config, config.scope, declared, host, invocation, labs, params, reservations, session.dependencies, session.errors, session.lab, session.projects |
| run | `otto.docker` | 11 | config, config.scope, declared, errors, host, invocation, lifecycle, logger, models, registry, result, utils |
| run | `otto.fleet` | 1 | config.scope, context.state, host, reservations |
| run | `otto.session.activation` | 1 | config.scope, models.dependencies, session.errors |
| run | `otto.session.dependencies` | 1 | config.scope, env, session.errors |
| run | `otto.session.errors` | 1 | errors |
| run | `otto.session.lab` | 1 | docker, inventory, labs, session.errors |
| run | `otto.session.logs` | 1 | declared, host, logger, session.errors |
| run | `otto.session.projects` | 1 | config.scope, models.dependencies, session.errors |
| workflows | `otto.init` | 9 | config, config.user_settings, errors, host, inventory, kmodcov, labs, models, models.base, utils |
| workflows | `otto.instructions` | 1 | context.state, instruction_catalog, params, project, session.activation |
| workflows | `otto.monitor` | 10 | _webassets, config.scope, console, context.state, declared, errors, filesystem, host, lab.fleet, link, logger, models, monitor.broadcast, monitor.errors, monitor.event_ops, monitor.events, monitor.formats, monitor.parsers, monitor.snmp, monitor.store, monitor.tls, result, tunnel, utils |
| workflows | `otto.project` | 8 | config.scope, context.state, errors, host, instruction_catalog, layout, link, models.dependencies, params, registry, result, tunnel, utils |
| root | `otto.bootstrap` | 1 | config, config.scope, declared, errors, host, models, project, registry |
| root | `otto.coverage` | 47 | _webassets, bootstrap, config, config.scope, context.state, declared, errors, host, lab.fleet, layout, models.base, result, utils, version |
| root | `otto.suite` | 11 | bootstrap, config, config.home, config.scope, context.state, coverage, declared, errors, host, invocation, lab.fleet, labs, lifecycle, models, monitor, monitor.errors, params, project, registry, utils, version |
| entry | `otto` | 1 | — |
| entry | `otto.__main__` | 1 | _shim |
| entry | `otto._shim` | 1 | _shim_complete, cli, version |
| entry | `otto.cli` | 28 | _shim_complete, bootstrap, check, config, config.home, config.scope, config.user_settings, context.state, coverage, declared, docker, env, errors, host, init, instruction_catalog, instructions, inventory, invocation, kmodcov, lab.fleet, labs, lifecycle, link, link.params, logger, models, models.dependencies, monitor, monitor.errors, params, project, registry, reservations, result, session.activation, session.dependencies, session.errors, session.lab, session.logs, session.projects, suite, tunnel, utils, version |
| entry | `otto.context` | 1 | bootstrap |
| entry | `otto.examples` | 9 | otto, host, inventory, labs, models, monitor.parsers, reservations |
| entry | `otto.lab` | 1 | — |
| entry | `otto.session` | 1 | bootstrap |
| entry | `otto.testing` | 3 | bootstrap, context.state, creds, host, inventory, invocation, labs, models, models.base, reservations, result, suite |

**Amended by P7, 2026-10-15.** `otto.lab.model` and `otto.labs.errors` are not modules: the lab model lives with its loader in `otto.labs.loader` (§3.7). The end state has two fewer layered modules (§5.2).

**Amended by P8, 2026-10-15.** There is no `otto.active_run`: the slot is in `otto.invocation` (§3.8), so the end state has one fewer tach module and one fewer layered module (§5.2). `otto.lab.fleet` depends on `otto.reservations`: the fleet's in-play read returns the gate's own `_hosts_in_play` (§3.8), a downward edge from run to sources.

**Amended by P9a, 2026-10-16.** The fleet is `otto.fleet`, a module of its own (§3.9, D-P9a-2). Every `lab.fleet` in this map's dependency cells (`otto.monitor`, `otto.coverage`, `otto.suite`, `otto.cli`) and in the `otto.lab` placement below reads `fleet`.

In the end state every file is owned and every module layered. There are no static upward edges,
and the graph **between declared tach modules** has no cycle. Imports inside one tach module are
not edges and are not checked: `monitor/collector.py:591` and `monitor/export.py:23` import each
other, and both stay inside `otto.monitor`.

**Placements that may surprise a reader**
- **`otto.host` sits below `otto.models`.** The specs build hosts (spec 3b), and after P3 the host
  specs live inside `otto.host`. `otto.models` keeps the lab, settings and inventory records, which
  import host classes.
- **`otto.config.scope` and `otto.declared` are foundation.** The host layer needs `repo_targets`,
  `scope_for_repo` and `declared_for_host` (`host/product.py:795`, `:864`; `host/dev_tool.py:304`).
  After P5 neither reaches up: `declared` reads a catalog that bootstrap fills (§3.5).
- **`otto.config` spans three layers.** The package is composition, `config.home` and
  `config.user_settings` are specs, and `config.scope` is foundation. This is safe only because
  of the ancestor rule (§4.3).
- **`otto.lab` is a facade with no dependencies.** Its `__init__` holds only `TYPE_CHECKING`
  imports and a lazy table (`lab/__init__.py:36-49`), so it sits in entry. Its implementation is
  `otto.labs.loader` (`Lab`, `load_lab`; sources) and `otto.lab.fleet` (run).
- **`otto.monitor` is the monitor facade and the monitor service, one tach module at workflows.**
  Its `__init__` exports the live runner (workflows), so under S5-5's rule the module that owns it
  sits at workflows. The service files stay in that module with it: the collector runtime
  (`collector`, `db`, `export`, `factory`, `session`, which import each other in a loop), the
  files that use it (`archive_edit`, `review`, `server`), and `live.py`, which as a separate module
  would import the runtime the facade's module holds and close a loop through the facade's binding
  to it. Nothing below workflows imports any of them. The eleven lower files are one-file modules
  at topology (§3.11).
- **`otto.context` and `otto.session` are entry facades over run-layer implementations.** Each
  exports a name that can start composition (`OttoContext`, `open_context`; `install_logging`),
  so each facade's resolver loads the composition root first (§3.12), and a module that imports
  the root must sit above it. Their implementations, `otto.context.state` and session's six
  files, stay in run, and internal code imports those (spec 1 §9).
- **`otto.suite` and `otto.coverage` sit in root, beside `otto.bootstrap`.** Their public entry
  points can start composition with no run in hand (`run_tests`, `prepare_run`; `clean_coverage`,
  `collect_coverage`, `get_coverage`), so their facades load the root too. Only entry modules
  import them (`otto.cli`, `otto.testing`, the root facade), and `otto.suite` imports
  `otto.coverage` within the layer. No file moves.
- **`otto.bootstrap` is the composition root, just below entry.** It composes the top layer (it
  imports `otto.project`), so nothing below root may import it. What lower layers read, the
  composed state, lives in `otto.declared` and `otto.config.repo` (§3.9).
- **`otto.examples` is teaching code.** It imports through facades (`examples/lab_repository.py:47`),
  is exempt from spec 1 §9, and so sits in entry.

## 3. Seams

Each seam gives its mechanism, the import sites it drops and adds, and why behavior is unchanged.

### 3.1 Completion collectors move into the CLI (P1b)

**Today** the completion cache reaches up into `otto.cli`:
- four walkers over the Typer app, `config/completion_cache.py:1414` (`collect_current_commands`),
  `:1453` (`collect_test_verb_options`), `:1498` (`_serialize_cli_children`) and `:1540`
  (`collect_cli_commands`);
- `config/completion_tree.py:149` reads `PENDING_SUBCMD_ARGS_KEY` (`cli/main.py:300`), and `:293`
  imports the default app;
- `completion_cache.py:2390` calls `bootstrap.discover()` for the repos.

**Design**
- The four walkers move to `cli/registry.py`, which the help and handover surfaces already load.
  They keep their function-local imports and stay out of `otto.cli.registry.__all__` (a tier-2
  public namespace).
- `Section.collect` becomes `collect(repos, cli)`; the CLI supplies the collectors. Call sites:
  `config/cache_sections.py:174`, `:175`, `:187`, `:190`, `:197-200`, and the tests
  `tests/unit/config/test_cache_sections.py:340`, `:360` and `test_shim_section.py:79`.
- `build_shim_payload(repos, *, app, pending_key)`: the CLI passes the app and the key, so
  `completion_tree.py:149` and `:293` go.
- `collect_project_names(repos)`: the writer passes `result.repos`, the same list
  `discover().repos` returns, so `:2390` goes.
- The placement-debt exemption in `.ast-grep/rules/no-cli-import-outside-cli.yml:21-23` and the
  DEBT comment at `tach.toml:95-99` are removed; the rule now enforces the end state.

**Unchanged** because the same functions run on the same objects; only who supplies the objects
changes.

### 3.2 Instruction catalog (P2)

`instructions.py` splits in two:
- **`otto.instruction_catalog`** (new, foundation) takes its first 740 lines: the tables
  (`STANDALONE_INSTRUCTIONS`, `PROJECT_ACTIONS`, `PROJECT_INSTRUCTIONS`, `INSTRUCTIONS`), the
  first-party name constants, the derivations and their checks, the `instruction` decorator and
  `_Handler`. Its runtime imports are `otto.errors` and `otto.registry` only.
- **`otto.instructions`** (workflows) keeps execution (`run_instruction`, `bind_handler_kwargs`,
  `_handler_of`) and re-exports `instruction` at its public path.

Three seams replace today's upward imports.

**1. Catalog data.** `Repo.get_instructions_panel` (`config/repo.py:585-603`) reads the catalog.
`config → instructions` goes, and the public member stays.

**2. First-party contribution, a revision-bearing source.** Today `_declared_bodies` imports
`otto.project.actions` (`instructions.py:297`) every time a view derives. A bare variable would
not do as a replacement: a `RegistryView` re-derives only when one of its sources' revisions moves
(`registry.py:1446-1474`). `PROJECT_INSTRUCTIONS` and `INSTRUCTIONS` are such views
(`instructions.py:480-484`, `:508-512`). A variable filled after a view's first read moves no
revision, so the stale view would be served forever.

The design:
- **`FIRST_PARTY_ACTIONS`** is a one-row `Registry` in the catalog, keyed `"otto"`, otto-owned,
  with `repo=None` and no `RequireRepo` capability (`registry.py:901-908` does not apply).
  `PROJECT_INSTRUCTIONS`'s sources become `[FIRST_PARTY_ACTIONS, PROJECT_ACTIONS]`, and
  `_declared_bodies` reads from it. Filling it moves its revision, so every derived view
  re-derives on the next read.
- **Install.** `declare_first_party_actions(cls)` is the only writer (§2.1).
  `otto.project.actions` exposes `declare()`, called at its import and by any explicit caller.
  - It is **atomic**: it goes through the registry's transactional `register`, so a refused
    declaration stores nothing.
  - It is **idempotent**: the same class again is a no-op that moves no revision; a different
    class raises `DuplicateRegistration`.
- **Reset.** The table belongs to the registry engine's instance set, so the per-test snapshot
  and restore (`registry.py:1180-1195`) covers it, and a restore bumps its revision. The
  execution face calls `otto.project.actions.declare()` before every lookup, never relying on the
  import side effect, so a table emptied by a test is declared again.
- **Collisions are refused before initialization, as today.** Today `_check_tables`
  (`instructions.py:363-371`) always sees the first-party bodies because it imports them. With
  the table possibly empty, two rules keep that guarantee:
  1. Every first-party name check uses the static `FIRST_PARTY_INSTRUCTIONS` (the six names),
     filled or not. That covers `_check_tables`' standalone ∩ first-party test and
     `_check_standalone`'s guard (`:403-416`). A standalone registration of one of the six names
     is refused whether it comes from a repo or from outside any repo (repo `None`); today both
     are refused, through the import.
  2. The table's own validation runs `_check_tables` against the current standalone and project
     tables, and checks the declared class's body names equal `FIRST_PARTY_INSTRUCTIONS`.
     Whichever side registers second is refused, as today.
- **Acceptance test.** In a fresh process, read the catalog early
  (`Repo.get_instructions_panel()` or `INSTRUCTIONS.items()`), then initialize (import
  `otto.project.actions`, or run `bootstrap()`), then read again. The second read shows the six
  first-party project instructions attributed to otto. A repo body declared for one of the six
  before initialization does not keep the spec: after initialization otto's spec wins, because
  `FIRST_PARTY_ACTIONS` is the first source.

**3. Nominal validation.** The check at `instructions.py:423` becomes
`issubclass(entry.cls, <the class in FIRST_PARTY_ACTIONS>)`. An empty table refuses with today's
message.

**Library use.** `run_instruction` calls `otto.project.actions.declare()` function-locally, so a
library caller that never bootstrapped still sees otto's six instructions.

**Re-pointed to the catalog**, under spec 1 §9: `project/actions.py:39`, `project/commands.py:16`,
`project/orchestrator.py:50`, `project/plan.py:17`, and `cli/run.py:10` (its names,
`INSTRUCTIONS`, `InstructionEntry` and `ProjectInstruction`, all move to the catalog).

**Amended by P2, 2026-10-12.** Building P2 changed this section, and §2.1, §4.2 and §4.3, in
these places:
- **The catalog takes the measured 800 body lines, not 740.** The difference is the slot's code
  (`FIRST_PARTY_ACTIONS`, `declare_first_party_actions` and its validator).
- **`cli/run.py` also re-points its historic `instruction` re-export** to the catalog. No otto
  module then imports the face at module scope, so `run`, `help`, `test` and `monitor` load the
  catalog in place of the face. Measured on `run`: the face's 3 file operations swap for the
  catalog's 3, total +0.
- **Three internal importers the list above missed are re-pointed** under the facade rule:
  - `cli/invoke.py:220`, split so `InstructionEntry` comes from the catalog and
    `bind_handler_kwargs` from the face;
  - `cli/invoke.py:1156` (`INSTRUCTIONS`);
  - `cli/registry.py:308` (`INSTRUCTIONS`).
- **The acceptance test drops its last clause** ("a repo body declared for one of the six before
  initialization does not keep the spec"). With the slot empty, the nominal check refuses every
  repo actions class, so no such body can exist before initialization.
  `test_first_party_actions.py::test_an_empty_slot_refuses_every_repo_actions_class` pins that
  refusal instead.
- **Install runs as otto in any context.** `declare_first_party_actions` registers inside
  `otto.registry._registering_for_otto()`, a private context manager beside the public
  `registering_repo`. It sets the registry's marker to `None` and restores it, so the row is otto's
  even when the first import of `otto.project.actions` happens inside a repo's init. The public
  `registering_repo` is unchanged (S5-6).
- **Reset.** A per-test restore can empty a slot filled after the snapshot while
  `otto.project.actions` stays imported. `tests/conftest.py` gains a session-scoped fixture,
  `_first_party_actions_declared`, that declares once before any per-test snapshot;
  `_isolate_registries` is unchanged.
- **`FIRST_PARTY_BODIES` is deleted** (hard cutover; it was never public).
- **§2.1: `tests/unit/test_slot_installers.py` exists from P2,** with one slot (first-party
  actions). Its scan resolves every reference to the slot's home rather than matching names:
  - it follows absolute and relative imports, aliases, star imports, `import_module`,
    `sys.modules[...]`, re-exports across modules (only module-scope bindings export), and every
    binding form (assignment and unpacking, `:=`, `with ... as`, `for`, parameter defaults);
  - strings count when qualified with the home (`"<module>:<name>"`, `"<module>.<name>"`) or
    passed to `getattr`-family calls on the home; a bare name string does not;
  - it flags a raw `register`/`unregister` on the table outside its home;
  - a same-named object of another module is not flagged, so later slots can privilege a common
    name such as `install`.
- **§4.2, row P2: the swap is disproved** (owner ruling D-P2-1, §7). See §4.2 for the measured
  result.
- **§4.3: the contract table is `IMPORT_CONTRACTS`** (with `IMPORT_MUST_LOAD`), not
  `IMPORT_MUST_NOT_LOAD`. The catalog's row uses its allowlist form, `IMPORT_ONLY_DECLARED`:
  importing a module may load, of otto's own modules, only itself and its declared dependencies.
  Dependencies a module imports only inside functions are named on the row as lazy, and must not
  load at import; every other declared dependency must.

### 3.3 Host specs into otto.host; the impairer contract into otto.link.params (P3)

This carries out spec 3b §6 (`2026-10-06-host-construction-design.md:691-736`).

**File moves**
- `git mv models/host.py host/specs.py`.
- `git mv models/options.py host/option_specs.py`.

**The shared leaf, `otto.models.base`** (foundation), receives:
- `Cred` and `cred_identity` from `host/login_proxy.py`, which re-exports them;
- `CredSpec` from `models/host.py`;
- `resources_nonempty` from `models/lab.py:36`.

This is spec 3b's leaf option (a); with `CredSpec` in the spec module instead, `models.base` would
import the host package. The module docstring (`models/base.py:1`) is rewritten: the leaf holds
what `otto.host` and `otto.models` both need (`OttoModel`, `CredSpec` and the runtime `Cred`
dataclass), not only a pydantic base.

**Nominal checks.** `_validate_host_spec`'s `issubclass` (`host/os_profile.py:183-199`) now runs
inside one package. No marker and no new base class.

**The link carve, and the impairer contract.** `models/host.py:82` and `models/jsonschema.py:60`
re-point from the `otto.link` facade to `otto.link.impairer`, which is carved as a tach module
together with `otto.link.params` and `otto.link.netem`.

Carving them apart would expose a runtime loop:
- the registry's stock entry names netem by string,
  `IMPAIRERS.register("netem", ClassEntry(Ref("otto.link.netem:NetEmImpairer")))`
  (`link/impairer.py:152-153`);
- `netem.py` imports `LinkImpairer`, `ScopedState`, `FIRST_SELECTOR_BAND` and `MAX_SELECTORS`
  from `impairer` (`link/netem.py:16-21`).

The fix: those four names, the impairer **contract**, move from `link/impairer.py` into
`link/params.py`.
- The contract uses only the standard library and the parameter types already in `params`
  (`ImpairmentParams`, `Selector`). `params` therefore stays stdlib-only, and no module is added.
- `netem` then imports only `params`. `impairer` keeps the registry (`IMPAIRERS`,
  `register_impairer`, `build_impairer`, the stock `Ref` and `_is_unresolved_stock`) and imports
  the contract from `params`.
- The stock `Ref` stays a string edge `impairer ⇢ netem` that closes no loop, so it is an ordinary
  classified entry (§5.3), not an exception.

What this changes and what it keeps:
- **Laziness.** Reading `IMPAIRERS` still never imports `netem`. The import contract
  `"from otto.link import IMPAIRERS"` must not load `otto.link.netem`
  (`tests/unit/test_import_contracts.py:126-131`), and it still holds.
- **Public paths.**
  - `otto.link:LinkImpairer` and `otto.link:ScopedState` keep their path; their lazy entries
    re-point to `otto.link.params` (`link/__init__.py:27-28`, `:69-70`).
  - `otto.link.impairer` and `otto.link.netem` are not declared namespaces, and no golden line
    names them.
  - `api/public.toml`'s `[formats.link-impairment-sentinel]` record (`api/public.toml:234-236`)
    names `otto.link.params:IMPAIR_SENTINEL_*`. That path is unchanged, and `params` keeps those
    lists.
- **Re-pointed importers**, under spec 1 §9:
  - `link/check.py:70`, `link/judge.py:22`, `link/manage.py:34` and `link/netem.py:16`;
  - the docstring references at `link/judge.py:6` and `link/manage.py:1374`, `:1380`;
  - the tests `tests/unit/link/test_impairer_registry.py`, `test_netem.py`,
    `test_manage_impair.py`, `test_check_live.py`, `test_judge.py` and
    `tests/unit/registry/cases/test_case_class_seams.py`.
- `params`' module docstring grows from "typed impairment parameters" to the impairment
  vocabulary: parameters, selectors, the read-back shape and the impairer contract.

**Public-binding checklist (complete).** Spec 3b (`:712-713`) keeps the `otto.models` exports as
facade bindings. Every binding whose target moves in P3:

| facade | names | target after P3 |
|---|---|---|
| `host/__init__.py:259-262` | `EmbeddedHostSpec`, `HostSpec`, `PowerControlSpec`, `UnixHostSpec` | `otto.host.specs` |
| `models/__init__.py:71` | `CredSpec` | `otto.models.base` |
| `models/__init__.py:72` | `ToolchainSpec` | `otto.host.specs` |
| `models/__init__.py:97-104` | `FtpOptionsSpec`, `NcOptionsSpec`, `ScpOptionsSpec`, `SftpOptionsSpec`, `SnmpOptionsSpec`, `SshOptionsSpec`, `TelnetOptionsSpec`, `TftpOptionsSpec` | `otto.host.option_specs` |
| `link/__init__.py:69-70` | `LinkImpairer`, `ScopedState` | `otto.link.params` |

- Each facade's matching `TYPE_CHECKING` export re-points too.
- The P3 public-path check imports all 16 `facade:name` pairs and asserts each object's
  `__module__`, alongside the golden agreement test.
- The registry `Ref`s at `host/os_profile.py:459`, `:466` and `:473` stop pointing upward. The
  deep path `otto.models.host:UnixHostSpec` was retired in P1b (spec 1 appendix G).

**Amended by P3, 2026-10-12.** Building P3 changed this section, and §4.3 and §5.3, in these
places:
- **No Python module is added, but four tach modules are declared**, all `foundation`:
  `otto.link.params`, `otto.link.impairer`, `otto.link.netem` and `otto.models.base`. The graph
  ends at 56 modules, 271 edges, 43 layered.
- **`models→link` was already a re-pointed import.** At P2, `models/host.py:82` and
  `models/jsonschema.py:60` imported `otto.link.impairer`, not the facade. The tach edge existed
  only because `otto.models` declared `otto.link`; P3 declares `otto.link.impairer` instead.
- **More importers re-point than the list above names**, under the facade rule:
  - `Cred`/`cred_identity` → `otto.models.base`: `host/{connections,interact,privilege,
    remote_host,session,unix_host}.py`, `host/survey/{engine,login}.py`, `inventory/creds.py`;
  - `CredSpec` → `otto.models.base`: `creds/{json_store,protocol}.py`, `testing/conformance.py`;
  - `models/{inventory,jsonschema,snippets,settings}.py` read `otto.host.specs` and
    `otto.host.option_specs` directly (`models/inventory.py` keeps re-exporting
    `coerce_digit_string`).
  - The line numbers in `link/check.py` differed at P2's tip.
- **tach users of the leaf:** `config`, `creds`, `init`, `inventory`, `labs`, `reservations` and
  `testing` declare `otto.models.base`; `coverage` and `monitor.snmp` replace `otto.models` with
  it.
- **`host/element.py` imports `resources_nonempty` at module scope.** `remote_host` loads the leaf
  at module scope for `Cred`, so `element` already loads it, and a function-local import there
  saved nothing.
- **§4.3:** the leaf and the three link modules get `IMPORT_ONLY_DECLARED` rows (each `[]`), not
  new `IMPORT_CONTRACTS` rows. The import-order row is
  `import otto.host.login_proxy; import otto.models.base`. That is proxy first: the reverse
  order cannot catch a leaf that back-imports the proxies at its tail.
- **§5.3:** at the carve, STRING_EDGES gains the 13 `link/__init__.py` facade bindings and the
  stock netem `Ref` (`REGISTRY_BUILTIN`). At the move, `models/__init__.py` gains 11 bindings
  (`OttoModel` and `CredSpec` → `otto.models.base`, `ToolchainSpec` → `otto.host.specs`, the
  eight option specs → `otto.host.option_specs`), and the seven host-spec rows go: 61 → 75 → 79.
  GRANDFATHERED 9 → 2 (bootstrap's two stay until P9b). `CURRENT_LANDING` stays P2 after the
  carve alone (13/46 is no landing's shape) and becomes P3 at the move.

### 3.4 Container auto-start removed (P4, S5-4)

**Today.** `exec`, `run`, `put`, `get` and the session paths on a `DockerContainerHost` call
`_ensure_running` (`host/docker_host.py:254-296`). When the container id is empty (a
declared-but-unresolved placeholder) and the parent finds no running container, it calls
`_auto_up` (`:351` to the end of the method), which brings the stack up. To do that, `_auto_up`
imports upward at five sites:
- `docker.resolve.is_declared_use_case` (`:378`);
- `docker.deployment.deploy` (`:381`);
- `config.fleet.current_repos` and `get_lab` (`:425-426`);
- `docker.compose.compose_up` (`:427`).

These sites are the only reason the host layer depends on `otto.docker`.

**The change (owner, S5-4).** `_auto_up` is deleted.
- **What still happens.** `_ensure_running` resolves a running container's id exactly as today
  (`_resolve_container_id`).
- **Scope.** The new error is raised only for an unresolved placeholder (`container_id` empty)
  whose probe finds no running container. A cached id bypasses the probe (`:284-295`): a
  container that stopped after its id was cached fails as today, from the `docker exec` itself.
  This spec does not redesign liveness.
- **What no longer happens.** When nothing is running, it raises `ContainerNotRunningError`
  instead of starting anything. The class is new in `host/errors.py`:
  `ContainerNotRunningError(OttoError, RuntimeError)`, the pattern of `HostCommandError`
  (`:55`). Every failure on this path is a `RuntimeError` today, so an existing
  `except RuntimeError` still catches it.
- **The message** names the container and the explicit ways to start it:
  - from the shell, `otto docker compose up <project>`, which brings up a repo's stack or deploys
    a declared use-case by name (`docs/cli/docker/use-cases.md:32`);
  - from Python, `otto.docker.deploy(<use-case>)` or `otto.docker.compose_up(...)`, for example
    in a session-scoped fixture or a suite's setup.

  The host does not ask which kind `self.project` names. The message lists both routes, so no
  import of `otto.docker` remains.

**Dead code removed with it.**
- `otto.docker.resolve.is_declared_use_case`, whose only caller was `_auto_up`
  (`docker/resolve.py:82-92`). It is internal: not in `otto.docker`'s lazy table or the golden.
  Its test, `tests/unit/test_run_repos_accessor.py:43-50`, goes with it.
- **Dry-run arms.** `_auto_up`'s `CommandNotRunError` arms (`docker_host.py:394-397` and the
  legacy route's arm), which keep a declined dry run from being refiled as an auto-start failure,
  go with `_auto_up`. They were already unreachable: `_resolve_container_id` refuses a dry run
  first (`refuse_declined_fact`).
- **The `_ensure_running` docstring invariant.** The docstring (`:270-279`) says every caller owes
  a dry run an arm because the method may start a container. It is rewritten: the method starts
  nothing now, but the callers' dry-run arms stay, because the id probe still refuses a dry run.

**Public surface.**
- No golden line changes. `_auto_up` and `_ensure_running` are private; the constructor
  (`public_api.txt:1583`) and the public members, `is_running` (`:1619`) included, keep their
  signatures.
- `otto.host:DockerContainerHost` keeps its lazy entry (`host/__init__.py:179`).
- No exception is added to the string audit.

**Landing.** P4 is its own marked commit with a `BREAKING CHANGE:` footer that names the remedy.
It also carries the docs recipe: a short "Starting containers before you use them" section in
`docs/cli/docker/index.md` with the CLI command and a session-scoped fixture that calls
`otto.docker.deploy`.

**Placement.** The removal must land before P5: removing `host → docker` is what lets
`otto.host` leave the cycle there, and without it P5 still holds 13 members and every later
landing would have to carry `host` in the cycle. Anywhere from first to fourth it deletes the same
single edge; it stays fourth, next to the phase that depends on it.

**Amends spec 2 §4,** whose `otto.host` row has auto-start ask `otto.docker`. That row's
mechanism is gone.

**Collateral the commit carries** (every match for auto-start in `src`, `tests` and `docs`
outside `docs/superpowers`):

| file | change |
|---|---|
| `src/otto/host/docker_host.py` | Delete `_auto_up`. `_ensure_running` raises `ContainerNotRunningError`. Rewrite every docstring that describes auto-start: the class docstring; `_ensure_lock`'s (`:194-196`, "at most one auto-up" becomes "the id is resolved once"); `_ensure_running`'s (`:254-279`); `is_running`'s (`:298-306`, "NEVER auto-starts … skip both probe and auto-up" becomes "never starts anything … skip the probe"); `_resolve_container_id`'s (`:329-337`, where `""` would now become a `ContainerNotRunningError` reporting a container down that was never asked about, instead of an `_auto_up` that starts one); `_docker_exec`'s (`:748-757`, "resolves (and can auto-start)" becomes "resolves"); `open_session`'s (`:848-851`, "via `_auto_up` → `compose_up` — START ONE" goes; the dry-run arm stays because the probe asks the daemon). |
| `src/otto/host/errors.py` | Add `ContainerNotRunningError`. |
| `src/otto/docker/resolve.py:82-92` | Delete `is_declared_use_case`, including its docstring's auto-start clause at `:86`. |
| `src/otto/docker/compose.py:869-870`, `:944` | The comments say a use-case placeholder carries the use-case name in `project` so `_auto_up` can route. They now say the field names what to start, which the error message shows. |
| `src/otto/tunnel/discovery.py:189` | The comment's reason for probing liveness first becomes "exec on a stopped container fails"; the probe stays. |
| `docs/cli/docker/index.md:118-122` | Replace the auto-start paragraph with the fail-fast behavior and a link to the new recipe section. |
| `docs/cli/docker/index.md:230-237` | The mount-table trap. Drop "if the stack is *down* they auto-start it…" and keep the rule that the process translating paths must be the one that brought the stack up. |
| `docs/cli/docker/compose/down.md:32` | "The next access auto-starts the stack again" becomes "the next access fails until the stack is brought up again". |
| `tests/unit/host/test_docker_host.py` | Thirteen auto-start tests are replaced by fail-fast tests (listed below). The new tests pin the error class, that `RuntimeError` catches it, and the message's two routes. `test_is_running_probes_quietly_and_caches_id` (`:1191`) and `test_is_running_false_when_down_never_composes` (`:1211`) stay: their "never auto-start" assertion stays true. |
| `tests/unit/host/test_dry_run.py` | `test_a_dry_run_never_starts_a_container` (`:1834`) stays and drops its `compose_up` spy; its docstring's path ends at the probe. `test_auto_up_hands_a_decline_back_unwrapped` (`:2470`, with its note at `:2456`) is deleted with the arm it tests. |
| `tests/unit/docker/test_dry_run.py:386` | The docstring drops "`_ensure_running` -> `_auto_up` -> `compose_up`, which is the path that STARTS A CONTAINER". |
| `tests/unit/docker/test_compose.py:1504` | The docstring of `test_register_declared_use_case_repo_synthesizes_usecase_ids` drops the auto-up routing reason; the test stays. |
| `tests/unit/test_run_repos_accessor.py:43-50` | Delete the `is_declared_use_case` check. |
| `tests/e2e/docker/test_docker_e2e_cli.py:380-412` | `test_e2e_run_against_unstarted_container_auto_starts` becomes `…_fails_fast`. It asserts the error names `otto docker compose up` and that no stack came up. |
| `tests/e2e/test_tunnel_e2e.py:600` | The docstring's history note keeps its point (no docker I/O from `otto tunnel list`); "auto-started" becomes "would start". `test_container_endpoint_oldos` (`:448`) already brings its stack up explicitly and needs no change. |
| `tests/repo1/pylib/repo1_instructions/run_on_container.py:13` | "(or auto-start-capable)" is dropped: the container must be running. |

The thirteen tests replaced in `tests/unit/host/test_docker_host.py`:

| line | test |
|---|---|
| `:887` | `test_placeholder_auto_ups_stack` |
| `:914` | `test_placeholder_no_repo_raises` |
| `:924` | `test_placeholder_auto_up_failure_raises` |
| `:936` | `test_placeholder_use_case_project_auto_starts_via_deploy` |
| `:983` | `test_use_case_auto_up_picks_the_container_of_its_own_parent` |
| `:1022` | `test_use_case_auto_up_hands_a_decline_back_unwrapped` |
| `:1052` | `test_use_case_auto_up_wraps_a_real_deploy_failure` |
| `:1082` | `test_use_case_auto_up_no_container_for_service_is_refused` |
| `:1111` | `test_legacy_auto_up_with_no_repo_names_the_building_remedy` |
| `:1125` | `test_legacy_auto_up_wide_arm_names_the_building_remedy` |
| `:1143` | `test_legacy_auto_up_with_no_service_container_names_the_building_remedy` |
| `:1157` | `test_concurrent_access_triggers_single_auto_up`, which becomes "concurrent access raises for each caller and starts nothing" |
| `:1225` | `test_put_placeholder_auto_ups` |

Two matches are unrelated and stay unchanged: `docs/contributing.md:19` and `:83` are Vagrant's
`autostart`, and `tests/unit/suite/test_monitor_fixture.py:137` is a monitor event pair.

**Amended by P4, 2026-10-13.** Building P4 changed this section in these places:
- **Owner ruling: a probe's three outcomes (Chris, 2026-10-13).** `_resolve_container_id` folded
  every non-ok probe into "no container", so a daemon that could not answer was reported as a
  container that was not running, and Docker's own text was lost. Before P4, that text reached
  the user through the auto-start failure. `_ensure_running` now tells three outcomes apart:
  - the probe answered and found nothing: `ContainerNotRunningError`, with the message above;
  - the probe ran and failed (`Status.Failed`, e.g. the daemon is down or the socket refuses):
    `ContainerNotRunningError`, whose message says the probe failed on the parent and carries the
    exit status and Docker's text. It never says "no running container was found";
  - the command never completed (`Status.Error`: a timeout, a lost session, no return code):
    the existing `HostUnreachableError`. A transport that raises (`OSError`, `ConnectionError`)
    propagates unchanged.

  `is_running` is unchanged: every non-ok probe still reads as `False`, because tunnel discovery
  relies on it.
- **The message labels each route**, because the CLI deploys use-cases only. `otto docker compose
  up <use-case>` and `otto.docker.deploy(<use-case>)` serve a use-case, and
  `otto.docker.compose_up(...)` serves a repo's own compose files.
- **The thirteen tests become seven functions with thirteen ids.** Six `compose_up` spies elsewhere
  watched a path that no longer exists, so they could never fail. Two were on `is_running`, three
  in `tunnel/test_discovery.py` and one in `tunnel/test_manage_resolve.py`. They are deleted, and
  those tests now assert the commands the parent was asked to run. The concurrency test holds the
  first probe open and goes on only once the second caller is seen waiting on the lock.
- **More collateral than the table lists:** the `errors.py` counts and a `test_error_base.py`
  row, two `host.py` docstrings, `link/manage.py`, `docs/architecture/subsystems/docker-hosts.md`,
  the dry-run decline class in `tests/unit/host/test_dry_run.py`, the `is_declared_use_case`
  helper in `tests/unit/test_run_repos_accessor.py`, and `docker_host.py`'s now-unused logger.
- **No import contract is added.** tach, the ratchet and a scan of `otto.host` for `otto.docker`
  imports each catch the edge coming back. `CURRENT_LANDING` becomes P4; 13/45 was shared with P3.

### 3.5 Composed-repos catalog in otto.declared (P5)

**Today** two readers below the composition root read the repos from it:

| reader | reads | line | starts bootstrap? |
|---|---|---|---|
| `declared._loaded_repos` | `is_bootstrapped`, `get_repos`, `get_ordered_repos` | `declared.py:448` | never (`:438-451`) |
| `config.scope.scope_for_repo` | `bootstrap.get_repos` | `config/scope.py:274` | yes, and it admits on any failure (`:246-253`, `:276`) |

**Design.** This is a contract separate from the active run (§3.8).

**Published results.** `otto.declared` holds the catalog, and `bootstrap()` writes it:
- it publishes the in-progress result (`bootstrap.py:355`) and the complete one (`:394`);
- it clears it on failure (`:401-406`) and in `invalidate()` (`:505-521`).

**The initiator is installed when `otto.bootstrap` is imported.**
- The last module-scope statement of `bootstrap.py` calls `declared.install_initiator(_initiate)`.
  Installing runs nothing.
- `_initiate` looks up `otto.bootstrap.bootstrap` at call time, so a replaced or monkeypatched
  `bootstrap` is honored.
- `bootstrap.py`'s module scope already imports `.errors` and `.registry` (`:33-38`); it adds a
  module-scope import of `otto.declared`, which points downward.
- `invalidate()` clears the published results and keeps the initiator.
- A test pins that importing `otto.bootstrap` installs the initiator without bootstrapping, and
  that `invalidate()` keeps it.
- The test fixtures snapshot the catalog and the slot at `otto.declared` and stop importing
  `otto.bootstrap` before every test (§3.13).

**`declared_for_host` never starts bootstrap.** It replaces `:448` with a read of the published
results only. `_loaded_repos` stays the separate, non-initiating read and never touches the
initiator.

**`scope_for_repo` moves beside the catalog**, from `config/scope.py:235-282` into
`otto.declared`, with its doctrine.
- It reads the published complete result. If there is none, it calls the initiator, which
  bootstraps exactly as `get_repos()` does today.
- With no initiator, or on any failure, it admits (returns `None`). That is decision D2.
- Its importers re-point: `host/dev_tool.py:304` (which keeps `repo_targets` from
  `config.scope`), `host/product.py:864`, and their tests.

**Who sees D2.** Only a library process that does all three:
1. registers a repo-attributed product provider through the public `registering_repo`;
2. never imports `otto.bootstrap`;
3. loads a lab through the public `otto.lab:load_lab` (`lab/__init__.py:49` →
   `config/lab.py:332-338`).

Building the hosts runs the provider gate (`host/factory.py:311-317`, `:335-336` →
`host/product.py:864-870`).
- Today the gate starts bootstrap and may narrow.
- Now it admits, and the provider runs on every host of that lab.
- Because nothing composes in that process, the repos' init modules do not run, so their
  `[[products]]`/`[[dev_tools]]` entries and the providers they register attach to no host
  either (amended by P5, 2026-10-14).

The CLI imports `otto.bootstrap`, and every public entry point that can start composition loads
it when resolved (§3.12), so they behave exactly as today.

**Other moves**
- The bootstrap error types (`bootstrap.py:60-104`) move to `config/scope.py`; `otto.bootstrap`
  re-exports them.
- `SSH_DEBUG_ENV_VAR` moves to `host/connections.py`, its only reader. The env-var list in
  `config/env.py:14-22` keeps a comment pointing at it.
- Docstrings follow the moves:
  - `config/scope.py:20-23`, the reason the module lives in `otto.config`, is rewritten for
    foundation;
  - `host/product.py:861-864` names `otto.declared.scope_for_repo`;
  - `config/scope.py:725-726`'s `otto.config.fleet` reference is updated in P9a.
- **Declared in tach:** `otto.config.scope`, `otto.models.dependencies`, `otto.declared`.

**Amended by P5, 2026-10-14.** Building P5 changed this section in these places:
- **The error types' readers.** `otto.bootstrap` re-exports the four types, the same objects.
  The call-time readers in `config/fleet.py`, `context.py` and `cli/monitor.py` import
  `ProjectScopeError` from `otto.config.scope`, so patching the owner reaches every raise and
  `except` but the root's own. `config/dependencies.py` and `session/{errors,projects}.py`
  keep the re-export until P9b re-points them (§3.9). `config → bootstrap` stays in `BASELINE`.
  `BootstrapWarning` moves with the others; it sits in `bootstrap.py:60-104`.
- **A third `scope_for_repo` importer.** `docker/resolve.py` also imports it at module scope and
  re-points to `otto.declared`. Its tests patch the consumer's binding and stay.
- **Who sees D2** is every path that builds a lab-stamped host, not only `otto.lab:load_lab`:
  `otto:load_lab`, a `LabRepository`'s loader, `create_host_from_dict(..., lab_name=…)` and the
  container hosts `otto.docker.compose` stamps from their parent. The commit footer and the
  bootstrap page name each one.
- **Two slots, one record.** The composed repos and the initiator live on the one `_composition`
  record, so `test_slot_installers.py` lists it as each slot's table.
- **The completion snapshot** moves to `otto.registry` (§2.1), so the root's new imports cost no
  surface anything (§4.2).

### 3.6 Settings types and helpers move down (P6)

- `SETTINGS_FILENAME`, `TOML_SETTINGS_PATH`, `ARCHIVE_SUFFIXES`, `DockerImage`, `DockerCompose`,
  `DockerUseCase`, `DockerSettings` and `MonitorSettings` move from `config/repo.py:37-140` to
  `models/settings.py`; `otto.config`'s lazy entries re-point.
- `split_path_list` and `_PATH_LIST_SEP` (`config/env.py:12`, `:30`) move to `otto.utils`, a
  foundation leaf.
- **Carved:** `otto.config.home`, `otto.config.user_settings`.
- New edges `bootstrap → models` and `config → utils` are outside the cycle by then.

**Amended by P6, 2026-10-14.** Building P6 changed this section in these places:
- **The settings types live in `otto.config.scope`, not `otto.models.settings`.** Chosen by
  measurement: it is the only tested candidate that satisfies both constraints: it keeps
  `typing.get_type_hints(Repo)` and
  `get_type_hints(CompiledSettings)` resolving and grows no harness surface and no public name.
  `otto.config.scope` is a foundation module already loaded wherever `otto.config.repo` is, so the
  repo loader imports the three names it uses at module scope for free; `otto.models.settings`
  reads the types inside the functions that build them, where it read `otto.config.repo` before.
  A `models.settings` or `models.base` home made `from otto.config import DockerSettings`,
  `otto.docker`'s builders and ten public `otto.init` names load pydantic, and a new pydantic-free
  module cost a file on every path that reads settings (§4.2). `otto.config.scope` now holds three
  topics: the scope predicate, the composition root's errors and these types.
- **§2.2's map gains three edges to `otto.config.scope`:** `otto.models` (in place of
  `otto.config`, inside functions only; the ast-grep rule `models-no-module-scope-config-import`
  still bans a module-scope one), `otto.inventory` and `otto.init` (`TOML_SETTINGS_PATH`).
  `bootstrap → models` is not needed: `otto.bootstrap` reads `TOML_SETTINGS_PATH` from
  `otto.config.scope`, which it already declared. `config → utils` is the completion cache's
  collection child reading `_PATH_LIST_SEP`.
- **The carve declares more edges than §6 lists.** Declaring `otto.config.home` and
  `otto.config.user_settings` re-attributes imports tach had counted as `otto.config`:
  `config`, `cli` and `suite` → `config.home`; `cli`, `init` → `config.user_settings`; `inventory`
  → both, in place of `otto.config`. Each is in §2.2's end-state map.
- **One splitter, two copies.** `otto._shim_complete` keeps its own stdlib-only `_PATH_LIST_SEP`:
  the console-script shim may import nothing of otto.
- **Not breaking (D-P6-1).** No declared `__all__` changes. A moved class's `__module__`, and with it
  the pickle path and the class repr, is an implementation detail, as in P3; the commit is unmarked.

### 3.7 Lab model and loader (P7)

`otto.lab` gets its implementation (spec 1 D1).
- `Lab`, `split_lab_names`, `_describe` and `LAB_SEPARATOR` go to `lab/model.py`
  (`otto.lab.model`, topology).
- `git mv config/lab.py labs/loader.py`: `load_lab` and `_json_source` go to sources.
- The return annotation `-> Lab` (`config/lab.py:275`) is quoted and its import moves under
  `TYPE_CHECKING`, function-local where the body needs it, so the loader does not load the model
  at module scope.
- **Re-pointed:**
  - `testing/conformance.py:40` and `conformance_host.py:27`;
  - the root lazy table at `otto/__init__.py:85`;
  - `lab/__init__.py:42-43` and `:48-49`.
- **Tests:** `tests/unit/test_import_contracts.py:49-50`, `:184` and `:193`;
  `tests/unit/test_patch_targets.py:137` and `:165`.
- **Carved:** `otto.labs.errors`.

**Amended by P7, 2026-10-15.** Building P7 changed this section in these places:
- **`Lab` stays with its loader in `otto.labs.loader`; there is no `otto.lab.model`.**
  `git mv config/lab.py labs/loader.py` moved the whole module: `Lab`, `split_lab_names`,
  `LAB_SEPARATOR`, `_describe`, `load_lab` and `_json_source`. Chosen by measurement (D-P7-2,
  §4.2): `otto.lab.model` grew 21 CLI surfaces by 12 to 16 file operations (it loads `otto.lab`'s
  package and the model on every lab-loading and tracked surface); the loader's module grows none.
  The module's imports stay standard-library-only at import time.
- **The fleet and the compose verbs import `Lab` for type checking only.** `config/fleet.py` and
  `docker/compose.py` use `Lab` only in annotations, so they import it under `TYPE_CHECKING` and
  quote those 14 annotations: reaching the fleet or a docker verb loads no lab-model module, and no
  public name grows (D-P7-2: fix the growth at its source). `typing.get_type_hints` on those 14
  functions, 5 of them public, now needs the type-only names supplied (D-P7-3, unmarked).
- **`otto.labs.errors` is not carved.** It existed so a topology-layer `otto.lab.model` could
  raise the repository errors without importing `otto.labs`; with the model in `otto.labs`, every
  reader of the errors sits at or above the sources layer.
- **Re-pointed:** every one of the 32 import sites in `src/` and the 138 in the tests; the lazy
  tables at `otto/__init__.py:85` and `lab/__init__.py:42-43`, `:48-49`.
- **Tests:** the citations `test_patch_targets.py:137`, `:165` and `test_import_contracts.py:184`,
  `:193` name the fleet (`otto.config.fleet`, P9a), not this module. P7 re-points
  `test_import_contracts.py:52` and the two patch strings `cli/test_main.py:115` and
  `config/test_fleet_of_interest.py:384`. The guards that named the module follow it: the
  moved-name scan's gone-module table, the CLI heavy-import ban (`otto.labs.loader`, every
  spelling), `backend-construction-through-entry`'s `files:` path (a new test fails a rule that
  names a path that does not exist), the monitor service's lab ban, and two import contracts that
  keep the fleet and `compose_up` from loading the model.
- **Edges:** `labs`, `session` and `testing` drop `otto.config`; `cli`, `context` and `suite`
  declare `otto.labs` for the lab model they already imported at run time. All are downward and
  outside the cycle.
- **Not breaking (D-P6-1, applied as D-P7-1; D-P7-3).** `__all__`, the golden and the public
  paths `otto.lab.Lab`, `otto.lab.load_lab` and `otto.load_lab` are unchanged. `otto.config.lab`
  was a retired deep path (§3.10) and no longer imports; `Lab`'s `__module__`, pickle path, class
  repr and the module's logger name are `otto.labs.loader`.

### 3.8 Active-run contract (P8)

**`otto.active_run`** (new, foundation) holds the slot that is `context.py:77`'s `_active` today.
- `set_context` installs into it through spec 2's `ContextBinding` (spec 2 §3.4), and
  `reset_context` restores it.
- It exposes a protocol with `lab`, `repos`, `ordered_repos` and
  `admissible_ids(require_nonempty=…)`.
- `require()` raises a plain `RuntimeError` with `get_context`'s exact message (`context.py:84`).
- `otto.context` keeps `get_context` and `try_get_context`.
- `otto.host` never depends on the slot; tach enforces it.

**Reservations** read the active run instead of `config.fleet`, keeping today's semantics site
by site.
- **`_lab_and_hosts` (`reservations/check.py:141-145`).** An explicit `lab` and explicit
  `host_ids` are used as given. A derived host set is `admissible_ids(require_nonempty=False)`
  minus built-in hosts, exactly what `get_hosts_in_play` returns (`config/fleet.py:497-501`).
- **`evaluate_hosts` (`:270-281`).** It uses the same derived set and keeps the exemption for
  built-in hosts.
- Both import `otto.host.builtin_hosts.is_builtin_host`, the owner, so the identity test stays
  (`host/builtin_hosts.py:30-48`). That makes `reservations → host` a new edge, legal because
  `host` left the cycle in P5.
- A new test covers a lab that declares the built-in `local` host: it is excluded from the
  derived set, and kept when named in an explicit `host_ids`.

**Context's helpers come home.** `_apply_option_overrides` and `_flag_holding_out` move from
`config/fleet.py:36`, `:516` into `context.py`.

**Amended by P8, 2026-10-15.** Building P8 changed this section in these places:
- **The slot lives in `otto.invocation`; there is no `otto.active_run`.** `_ACTIVE` (the
  `ContextVar` that was `context.py`'s `_active`), the `ActiveRun` protocol, the read API
  `installed_run`/`require_run` and the privileged `install_run`/`restore_run` sit in the foundation
  leaf already loaded wherever `otto.context` is. Chosen by measurement (D-P8-2, §4.2): a new
  module grew 10 CLI surfaces by 4 file operations and 21 public names; the leaf grows none.
- **The host ban is a test, not tach.** The slot shares `otto.invocation` with the policy and
  resolver `otto.host` reads, so tach cannot forbid the slot alone;
  `tests/unit/test_slot_installers.py` fails any reference from `otto/host` to the slot's API,
  in every spelling, with an anti-vacuity check and a spelling self-test.
- **`check_hosts`, not `evaluate_hosts`.** The gate's second reader is `ReservationGate.check_hosts`
  (`reservations/check.py:233-299` at BASE); both readers share one `_hosts_in_play(run)`, which
  `otto.config.fleet.get_hosts_in_play` now returns for the context too. The fleet's
  `is_builtin_host` wrapper is deleted.
- **`_flag_holding_out` lives in `otto.host.builtin_hosts`, not `context.py`.** Homed in the
  context, a `fleet_of_interest` call loads `otto.context` and `otto.invocation` (+1 module,
  +1 file operation); beside the built-in host it tests for, nothing grows (D-P8-2).
  `_apply_option_overrides` moves into `context.py` as written.
- **Not breaking (D-P6-1, applied as D-P8-1).** Only private names and the internal
  `otto.config.fleet`'s names move; `get_context`'s refusal is unchanged, word for word.

### 3.9 The bootstrap split; docker below session; fleet under otto.lab (P9a, P9b)

**Today `otto.bootstrap` is two things:**
- the **composition root**: it runs the repos' inits and the checks that need every layer
  (`bootstrap.py:322-407`), reaching `otto.project` by name (`:363`, `:383`);
- the **composed-state accessor** that lower layers read (`get_repos`, `get_ordered_repos`,
  `is_bootstrapped`, `BootstrapResult`, discovery).

Lower layers import it for the second role, so it has to sit low. The first role needs
`otto.project`, so it reaches up by name. P9a and P9b separate the two, in that order. The split
follows cohesion: P9a moves the accessors down and finishes the fleet and docker work, which
empties the ratchet; P9b moves discovery, routes the public entry points, moves the root up and
forbids cycles.

#### P9a: accessors down, docker's reads, fleet under otto.lab

- **The composed state moves into `otto.declared`**, beside the catalog P5 put there. The catalog
  *is* `_result` and `_in_progress`, so it gets one home. Moving with it:
  - `BootstrapResult`, `_result`, `_in_progress`;
  - `get_repos`, `get_ordered_repos`, `is_bootstrapped`;
  - the fleet fallbacks `current_repos` and `current_ordered_repos` (`config/fleet.py:435-463`).
    They read the active run first and the catalog second, as today.

  `bootstrap()` writes the catalog through the privileged API of §2.1 (`publish_composed`,
  `clear_composed`); it no longer has globals of its own for it. An initiating read that finds no
  result calls the initiator, the mechanism P5 gave `scope_for_repo`, through one reader,
  `declared.compose()`: the complete result if published, else the in-progress one (the reentrant
  answer of `bootstrap.py:325-335`), else the initiator. `otto.declared` is foundation, already
  loaded wherever `otto.bootstrap` is (`models/settings.py:44`), so no file is added.
- **Every public name stays.** `otto.bootstrap` re-exports what moved, so `__all__`
  (`bootstrap.py:44`) and the golden lines (`public_api.txt:3-47`) are unchanged, and each
  re-export is the same object as its owner's.
- **Readers.** Coverage, suite, monitor and docker re-point to `declared.current_repos` and
  `current_ordered_repos`; `otto.suite` and `otto.coverage` route their composing entry points in
  the same landing (§3.12). The readers that call the composition root itself keep importing
  `otto.bootstrap` until P9b: `context.py`'s `bootstrap()` calls (`:601`, `:1330`) and
  `session/logs.py:57` (`get_repos`, through the re-export). So nothing changes behavior in P9a.
- **Docker's ten remaining fleet reads.**
  - The lab comes from the active run: `docker/build_verbs.py:252`, `docker/compose.py:1003`,
    `docker/observe.py:188`, `:196`, `:268`, `:337`, `:446`, and `docker/deployment.py:584`.
  - The repos come from `declared.current_repos`: `build_verbs.py:33`, `:118`, and
    `deployment.py:584`. There, `current_ordered_repos` sets compose precedence (`:591`) and
    becomes `declared.current_ordered_repos`. `docker/resolve.py:89`'s read went in P4 with
    `is_declared_use_case`.
  - This amends spec 2 §4's `otto.docker` row, which named a `config.fleet` accessor.
- **Fleet moves.** `git mv config/fleet.py lab/fleet.py` (`otto.lab.fleet`, run, declared here).
  - `otto.lab`'s lazy table (`lab/__init__.py:36-41`) and the root's (`otto/__init__.py:82-84`,
    `:86`) re-point.
  - `get_hosts_in_play` stays unexported.
  - The move carries P8's function-local `from ..reservations.check import _hosts_in_play` in `get_hosts_in_play`, so `otto.lab.fleet` declares `otto.reservations` (§2.2), and P9a's edge accounting counts `lab.fleet→reservations` (§6). (Amended by P8, 2026-10-15.)
  - `tests/unit/test_no_import_time_lazy_exports.py:239`, the `otto.config.fleet` monkeypatch
    targets in the tests, and the `"otto.config.fleet"` row of `_NO_COMMAND_MODULE_LOADS`
    (`tests/unit/test_import_contracts.py:49`) re-point to `otto.lab.fleet`.
- **Isolation.** The test fixtures follow `_result` and `_in_progress` to `otto.declared` (§3.13).
- **Layers in this landing.** The ratchet is empty after P9a, so every module is layered. Two
  layers are interim and change in P9b: `otto.bootstrap` sits at composition (lower readers in run
  still import it), and `otto.context` and `otto.session` sit at run (they are not facades yet).
  The two by-name imports of `otto.project` stay, as transitional audit entries (§5.3).

**Amended by P9a, 2026-10-16.** Building P9a changed this part in these places:
- **The fleet moves to `otto.fleet`, not `otto.lab.fleet`** (D-P9a-2): `git mv config/fleet.py
  fleet.py`, a run-layer module declaring `otto.config.scope`, `otto.context`, `otto.host` and
  `otto.reservations`. Under `otto.lab`, the facade's package `__init__` loaded on 8 host and test
  surfaces that only needed the fleet (+12 file operations each); as a module of its own, nothing
  grows. `otto.lab`'s and the root's lazy tables re-point to `otto.fleet`.
- **`config/dependencies.py` takes its error types from `.scope` in P9a, not P9b.** Its import of
  `otto.bootstrap` was the last `config→bootstrap` edge; with it, the cycle does not empty.
- **The composing calls import the root on the call** (D-P9a-3, §3.12): `prepare_run` and the
  three coverage entry points run a function-local `from .. import bootstrap` immediately before
  their `current_repos()` read; `run_tests` composes through the `repos` of the library context it
  builds, and `OttoContext.repos` imports the root itself (function scope, `context.py`).
- **Re-located lines at BASE `e65ea043a`:** `current_repos`/`current_ordered_repos` were
  `config/fleet.py:334-370` (not `:435-463`); `context.py`'s `bootstrap()` calls `:710` and `:1443`
  (not `:601`, `:1330`); docker's reads `build_verbs.py:33`, `:118`, `:254`, `compose.py:1007`,
  `observe.py:188`, `:196`, `:268`, `:337`, `:446`, `deployment.py:585-592`; `lab/__init__.py:38-43`
  (not `:36-41`); `tests/unit/test_no_import_time_lazy_exports.py:141`, `:347` (not `:239`);
  `tests/unit/test_import_contracts.py:51` (not `:49`).
- **The isolation row holds:** the conftest snapshots `otto.bootstrap._discovered` only
  (`_BOOTSTRAP_CACHES`); the catalog's record is `otto.declared._composition`.
- **Not breaking (D-P6-1, D-P7-3, applied as D-P9a-1).** Only private names and the internal
  `otto.config.fleet` move; the four re-exports are the catalog's own objects.

Session's `build_lab` path (`session → docker`) is an ordinary downward edge; no registry-string
hook is needed. The indirect provider route (`docker/compose.py:930`, `:995` →
`host/factory.py:335` → `host/product.py:795` → `otto.declared`) points downward at every hop
once P5 has landed.

#### P9b: discovery, the composing entry points, the root moves up

- **Discovery moves to `otto.config.repo`.** `discover`, `DiscoveryResult`, its cache
  `_discovered`, `get_env` and `discovered_teardown_deadline` all parse the environment and the
  repos' settings: config work (spec 4's S-3, "`otto.config` parses"). They go into one
  delimited section of `config/repo.py`, headed "Discovery (phase 1 of composition)", whose
  docstring says what discovery reads, that it never runs user code, and that its errors ride the
  cached result. The module docstring gains one line naming the section, and `otto.config`'s
  package docstring (`config/__init__.py:1-6`) becomes: "`otto.config` parses, discovery included,
  and `otto.bootstrap` composes; the composed-repos accessors are in `otto.declared`".
  `otto.config.repo` is already loaded wherever `otto.bootstrap` is, so the move adds no file.
  `otto.bootstrap` re-exports `discover` and `get_env`.
- **Lower readers import the owner** (spec 1 §9), and none imports `otto.bootstrap` afterwards:
  - `context.py`'s `bootstrap()` calls at `:601` and `:1330` become `declared.compose()`, and
    `discovered_teardown_deadline` (`:1230`) comes from `otto.config.repo`;
  - `session/logs.py:57` reads `declared.get_repos`; `session/projects.py`, `session/errors.py`
    and `config/dependencies.py` take the error types from `otto.config.scope` (moved in P5);
  - `env/manage.py:302` imports `discover` from `otto.config.repo`.
- **The composing entry points are routed** (§3.12): `otto.context` becomes a package facade
  over `otto.context.state`, `otto.session`'s facade becomes its own module over its six files,
  the root facade's `run_tests` alias binds to `otto.suite`, and the `otto.testing` harness loads
  the root before it builds its own context. These land in the same commit as the re-points
  above, which are what would otherwise change their behavior.
- **`otto.bootstrap` keeps the composition root:** `bootstrap()`; the `[os_profiles]` and
  providing-repo checks; and `invalidate`, which clears the catalog, the discovery cache and the
  completion names. The completion-names snapshot, which only the CLI reads, lives in
  `otto.registry` since P5 (§2.1); `invalidate` clears it through `clear_completion_names`.
  `otto.bootstrap` moves up to the **root** layer and imports `otto.project` normally,
  function-locally inside `bootstrap()`: `otto.project.actions.declare()` before any repo init
  (P2's first-party slot) and `otto.project.commands.check_project_instruction_options()` after
  them, read off the module at
  call time so a caller may still substitute it (`bootstrap.py:378-383`). The two by-name
  `importlib.import_module` calls go, and with them the audit's last transitional entries.
- **The initiator** is installed when `otto.bootstrap` is imported, as since P5. Every public route
  to composition loads it first (§3.12), so library use behaves as today.
- **Declared in tach:** `otto.lab`, `otto.env`, `otto._shim`, `otto.__main__`, the root `otto`,
  `otto.context.state` and `otto.session.{activation, dependencies, errors, lab, logs, projects}`;
  `otto.context` and `otto.session` take the entry layer, `otto.bootstrap` the root layer.
- **Cycles forbidden.** `forbid_circular_dependencies = true`, layers complete; `BASELINE` has
  been empty since P9a.
- **Maintainer guidance.** `docs/architecture/modules.md` gains a "where does a new module go"
  table (one row per layer: what belongs, what it may read, the slots), and
  `scripts/render_module_graph.py` gains a layers section.

**Why this order.** `otto.bootstrap` can import `otto.project` statically only once nothing below
root imports `otto.bootstrap`, so that `otto.project` cannot reach back to it; that is first true
in P9b. The accessors move first because every later step reads them from their owner.

#### Composition lifecycle acceptance

Each row is a test. Rows marked P9a land there with the state move; the rest land in P9b. All run
in the unit suite except the cold-start row, which runs in a fresh process (§3.13).

| case | today's behavior the test pins | lands |
|---|---|---|
| Cold start | In a fresh process, `import otto` loads neither `otto.bootstrap` nor `otto.declared`. The first composing name resolved loads the root and installs the initiator, and composes nothing. The first initiating read composes once; a second read returns the same result object. | P9b |
| Discovery only | `discover()`, `get_env()` and `discovered_teardown_deadline()` never compose: `is_bootstrapped()` stays false and no repo init module is imported. `otto.bootstrap.discover is otto.config.repo.discover`. | P9b |
| Reentrant init | A repo init module that calls `bootstrap()` or `get_repos()` during composition gets the in-progress result (`bootstrap.py:325-335`): final `repos` and `ordered_repos`, and an `errors` list that is the same object the outer call keeps appending to, so a later init's contained failure is visible through it. `is_bootstrapped()` is true inside the window. | P9a |
| Failed composition, then retry | A composition that raises (an uncontainable init failure, or the providing-repo check) leaves nothing published: no in-progress result, no complete result, `is_bootstrapped()` false (`bootstrap.py:355-406`). The next initiating read runs composition again and fails the same way; `scope_for_repo` admits on the failure, as today. | P9a |
| Invalidation | `invalidate()` clears the catalog, the discovery cache and the completion names and keeps the initiator; the next initiating read recomposes. | P9a (catalog), P9b (discovery) |
| Existing-context snapshots | A context that already read its repos keeps that one result across `invalidate()`, for `repos`, `ordered_repos` and the scope verdicts (`tests/unit/test_context_repos.py:78-87`); a cached refusal is re-raised as the same exception, with no second composition (`:53-68`). | P9b |

### 3.10 Spec 1's queued cleanups

**Internal imports through facades** (spec 1 §9): 233 sites in 52 files, not counting
`otto.examples`. By facade:

| facade | sites | facade | sites |
|---|---|---|---|
| inventory | 44 | tunnel | 8 |
| check | 37 | kmodcov | 7 |
| session | 26 | env | 4 |
| models | 26 | host.transfer | 4 |
| reservations | 20 | host.survey | 2 |
| link | 20 | cli | 1 |
| labs | 14 | creds | 1 |
| host | 10 | | |
| init | 9 | | |

Of these, 107 are at module scope, 100 function-local and 26 `TYPE_CHECKING`.
- No site names a symbol a later phase moves, so the whole sweep lands in P1b and no module is
  touched twice. (P9b re-points the importers of `otto.context`, which only then becomes a facade,
  §3.12.)
- One re-point adds an edge at P1b, `link → result`, outside the cycle.
- Sites whose owner is carved later change tach owner then; for example `cli/link.py:19-31`
  becomes `cli → link.params` at P3.
- A rule test lands with the sweep: an otto-internal import of a facade's lazy-table name fails
  unless the importer is in `otto.examples`.

**Amended by P1b, 2026-10-11.** Building P1b changed this section, and §3.1 and §3.11, in these
places:
- **The sweep counts 232 sites in 51 files, not 233 in 52.** The 233rd is `otto/__init__.py`'s
  own `TYPE_CHECKING` copy of its lazy table, which `test_lazy_packages` requires. The rule test
  exempts only that shape: an import directly under a top-level `if TYPE_CHECKING:` in the
  facade's own `__init__`, whose table entry names the same module and attribute.
- **The rule also reads attribute access on a facade package** (`import otto.x as x`, then
  `x.Name`). That re-pointed two `otto._webassets` reads to `otto._webassets.artifacts`.
- **The sweep is a permanent tool,** `scripts/facade_sweep.py`. The rule test's failure message
  names it as the fix.
- **The commits run in this order: sweep, collector move and fold, eleven declarations.**
  Declaring the eleven first gives 26 modules and 115 edges, and folding first breaks audit rule 3.
  The landing's message keeps §6's review order.
- **The fold removes the tach boundary** that kept config, context and tunnel out of the monitor
  service. `tests/unit/test_monitor_service_reads_no_lab.py` replaces it:
  - it is at least as strict as tach and never skips an import tach counts;
  - it is stricter where code runs at runtime;
  - it also reads literal import strings, and it bans `otto.lab` ahead of P7 and P9a;
  - `scripts/monitor_boundary_tach_canary.py` holds it to the installed tach.
- **The collector move (§3.1) changes four details:**
  - `serialize_tree` takes `pending_key` as a keyword-only argument;
  - the supplier `_completion_collectors` is private;
  - `otto.examples` depends on `otto.monitor.parsers`;
  - two tests that called `_collect_names(repos)` were migrated with the callers §3.1 lists.

**P1's two landings.** P1a carries the tach switches and the string audit; P1b carries the
sweep with its rule test, the monitor facade rule and the collector move, in ordered review
sections (§6).

**The 27 retired-path modules** (spec 1 appendix G). A module moves only when its layer requires
it:
- `otto.models.host` moves in P3, `otto.config.lab` in P7, and `otto.config.fleet` in P9a;
- `otto.config.scope` stays and is carved in P5;
- `otto.config.repo` stays and loses its types in P6.

The other 21 need no move: their deep paths are no longer public, and their placement fits.

### 3.11 Facades that mix layers (S5-5, P1b and P9b)

**The rule.** A package facade's `__init__` belongs to some tach module, and its lazy-table
bindings are string edges from that module. So:
- the module that owns a facade's `__init__` sits at or above every layer the facade exports from;
- if the package's own implementation sits lower than that, the facade is its own tach module and
  the lower implementation files are declared as their own modules at their real layers.

Bindings are then downward edges, never exceptions. No file moves, and no public path changes.

**Every package facade, checked** (end state, by the layers of its lazy-table targets):

| facade | module's layer | exports from | action |
|---|---|---|---|
| `otto` (root) | entry | foundation … entry | Already facade-only: every other file is owned by a declared module. Declared in P9b. |
| `otto.lab` | entry | foundation, topology, sources, run | Already facade-only (`lab/__init__.py` holds only strings). Declared in P9b. |
| `otto.context` | entry (from P9b) | foundation (`otto.invocation`), run (`otto.context.state`) | **Changed** in P9b: a package facade over its implementation, and its resolver loads the composition root (§3.12). |
| `otto.session` | entry (from P9b) | run | **Changed** in P9b: the facade is its own module over six one-file modules, and its resolver loads the root (§3.12). |
| `otto.monitor` | topology today | topology, workflows (`live`) | **Changed** (below). |
| `otto.config` | composition | specs, composition | Already compliant: `config.home` and `config.user_settings` are their own modules. |
| `otto.coverage` | root | composition, root | Compliant at root; its resolver loads the root (§3.12). |
| `otto.suite` | root | root | Compliant at root; its resolver loads the root (§3.12). |
| `otto.host` | host | foundation, host | Already compliant. |
| `otto.labs` | sources | foundation, sources | Already compliant. |
| `otto.link` | topology | foundation, topology | Already compliant. |
| `otto.models` | specs | foundation, host, specs | Already compliant. |
| `_webassets`, `check`, `cli`, `coverage.fetcher`, `coverage.merge`, `coverage.store`, `creds`, `docker`, `env`, `host.survey`, `host.transfer`, `init`, `inventory`, `kmodcov`, `logger`, `project`, `reservations`, `testing`, `tunnel` | — | one layer, their own | Nothing to do. |

**Amended by P9a, 2026-10-16.** The `otto.coverage` and `otto.suite` rows' 'its resolver loads the root (§3.12)' is superseded for P9a: their resolvers load nothing, and the composing calls import the root on the call (§3.12 as amended, D-P9a-3).

**`otto.monitor` (P1b).** Its facade exports `LiveReport`, `run_live` and `select_monitor_hosts`
from `monitor/live.py` (workflows) alongside its topology code.
- Declaring each monitor file as its own module does not work. Five files import each other in
  a loop: `collector` ↔ `export`; `db` → `session` → `collector`, `export`, `factory`; `factory`
  → `collector`, `db`. Separate modules would be a new cycle.
- **The design:**
  - The `otto.monitor` module (the facade and the monitor service, workflows) keeps those five, the three files that use them
    (`archive_edit`, `review`, `server`), and `live.py`. `otto.monitor.live` stops being a separate
    tach module. As one, it imports `factory` and `session`, which the facade's module holds, so
    the facade's binding back to it would close a runtime loop.
  - The eleven other files are declared at topology: `broadcast`, `errors`, `event_ops`,
    `events`, `formats`, `log_sourced`, `parsers`, `rates`, `snmp`, `store`, `tls`. Among them
    are the declared public namespaces `otto.monitor.parsers`, `otto.monitor.snmp` and
    `otto.monitor.log_sourced`.
- Nothing below workflows imports the facade module's files. Outside the package, the runtime
  is imported only by `suite/monitor_fixture.py:88` and `suite/plugin.py:1232`, and after the P1b
  sweep no internal code imports through the facade.
- This lands in P1b, where `otto.monitor` and `otto.monitor.live` leave the cycle. The eleven new
  modules join no cycle: the graph between declared tach modules stays acyclic, and imports inside
  `otto.monitor` (`monitor/collector.py:591` ↔ `monitor/export.py:23`) are not edges. The change
  needs no file move. A `tach.toml` comment protects the eleven one-file modules (§5.1).

**`otto` and `otto.lab` (P9b).** Both are declared in P9b, after every module they export from has
left the cycle. Declared earlier, a facade whose bindings reach into the cycle would itself be
pulled in, if the audit's string edges counted. tach does not count them, but the audit does.

**Internal importers.** After the P1b sweep, the model finds no otto-internal import through any
facade's lazy table. The five remaining hits are `from ..logger import management`, a submodule
import, not a facade binding. Spec 1 §9's rule test keeps it that way.

### 3.12 Composing entry points load the composition root (P9a, P9b; S5-6)

**The problem.** After the split, composition is started from below only through the initiator,
and the initiator exists only once `otto.bootstrap` has been imported. Today several public names
compose by themselves: `open_context` calls `bootstrap()` (`context.py:1330-1332`), and a context's
`repos` does too (`context.py:592-605`). Re-pointed to the initiator, they would raise in a process
that never imported `otto.bootstrap`.

**The rule.** A public facade that exports a name whose call can reach an initiating read
**without an `OttoContext` already in hand** imports `otto.bootstrap` in its resolver before it
returns that name. Importing the root installs the initiator and composes nothing; composition
still waits for the original call site, as today. The resolver returns the real object: the same
function or class, with the same signature and identity from every public spelling
(`otto.context.OttoContext is otto.OttoContext is otto.context.state.OttoContext`). A facade that
imports the root sits at or above it, which is §3.11's rule applied to the root.

**Why names that need a context are covered.** A read through an installed or given context can
only happen once an `OttoContext` exists. The public can obtain one only from a spelling that
loads the root (`OttoContext`, `open_context`, `run_tests`'s library sentinel, the `otto.testing`
harness) or from the CLI, which imports the root. So `get_lab`, `all_hosts`, the docker verbs,
`run_live`, `run_instruction`, the project actions and `ProjectContextView` need no routing of
their own.

**Every public boundary that can reach an initiating read** (audited against
`tests/unit/api_snapshot/public_api.txt`; the initiating reads are the `bootstrap()`,
`get_repos()` and `current_repos()` call sites listed in §3.9):

| public spelling | initiating read it reaches | without a context in hand? | routing |
|---|---|---|---|
| `otto.context:OttoContext`, `otto:OttoContext` | `repos`, `ordered_repos` and the verdicts of a hand-built context (`context.py:592-628`) | yes | `otto.context`'s resolver (P9b) |
| `otto.context:open_context`, `otto:open_context` | composition (`context.py:1330-1332`) | yes | `otto.context`'s resolver (P9b) |
| `otto.suite:run_tests`, `otto:run_tests` | the library sentinel's `repos` (`suite/run.py:1680-1683`) | yes | `otto.suite`'s resolver (P9a); the root alias binds to `otto.suite` (P9b) |
| `otto.suite:prepare_run` | `current_repos()` for `cov_tickets_json` (`suite/run.py:449-457`) | yes | `otto.suite`'s resolver (P9a) |
| `otto.coverage:clean_coverage`, `collect_coverage`, `get_coverage` | `current_repos()` when `repos` is `None` (`coverage/collect.py:122-123`, `:275-276`; `coverage/get.py:114-115`), before any context read | yes | `otto.coverage`'s resolver (P9a) |
| `otto.session:install_logging` | `get_repos()` (`session/logs.py:57-61`) | yes | `otto.session`'s resolver (P9b) |
| `otto.testing:assert_host_conforms` and the other host checks | the harness installs its own `OttoContext` (`testing/conformance_host.py:76-91`) | yes | the harness imports the root (P9b) |
| `otto.bootstrap:*` | itself | — | none needed |
| `otto.lab:get_lab`, `get_host`, `all_hosts`, `do_for_all_hosts`, `run_on_all_hosts`; `otto.docker:*` (each verb reads the active lab first, e.g. `build_verbs.py:252-256`, `deployment.py:584-589`); `otto.monitor:run_live` (walks the active lab before it reads repos, `monitor/live.py:148-149`); `otto.instructions:run_instruction`; `otto.project:*`; `otto.context:get_context`, `try_get_context`, `set_context`, `ProjectContextView` | through a context only | no | covered by the instance argument |
| `otto.lab:load_lab`, `otto:load_lab`, `otto.host` construction | `scope_for_repo` (`host/product.py:864-870`), which admits when no initiator is installed | — | D2, as approved; unchanged (§3.5) |

No other public name reaches an initiating read. A new one is caught by the fresh-process cases
of §3.13, which run every row above in a process that has not imported `otto.bootstrap`.

**Mechanism, per facade.** Each resolver keeps its `_LAZY_ATTRS` table and adds a frozen set of
composing names; for those, `__getattr__` first runs a function-local `import otto.bootstrap`, a
static, downward edge tach sees and checks:

| facade | module and layer | composing names | lands |
|---|---|---|---|
| `otto.suite` (`suite/__init__.py:41-46`) | `otto.suite`, root | `run_tests`, `prepare_run` | P9a |
| `otto.coverage` (`coverage/__init__.py:78-83`) | `otto.coverage`, root | `clean_coverage`, `collect_coverage`, `get_coverage` | P9a |
| `otto.context` (new `context/__init__.py`) | `otto.context`, entry, owning only the `__init__` | `OttoContext`, `open_context` | P9b |
| `otto.session` (`session/__init__.py`) | `otto.session`, entry, owning only the `__init__` | `install_logging` | P9b |
| `otto` (root, `otto/__init__.py:77-118`) | `otto`, entry | none of its own: its aliases bind to the namespace facades, which hold the rule | P9b |

**Amended by P9a, 2026-10-16.** The P9a rows (`otto.suite`, `otto.coverage`) are met on the call, not in the resolvers (D-P9a-3): resolving a composing name loads no root, and the root is imported immediately before the call's read of the run's repos. Measured on 3.10, the resolver mechanism grew 5 public names (`run_tests`, `prepare_run` +6 otto modules, +13 modules, +11 file operations; the three coverage entry points +4, +11 to +19, +8 to +14); on the call, none grows. The import-cost table's P9a 'library use' row is superseded: resolution loads nothing. `run_tests` needs no P9a routing: it reads the `repos` of the library context it builds, and `OttoContext.repos` imports the root itself. When P9b re-points `context.py` to `declared.compose()`, the context facade keeps this table's P9b routing for `OttoContext` and `open_context` (nothing under `otto.context.state` imports the root: that would be a run→root edge), and `run_tests` takes a call-time root import in `otto.suite`, as `prepare_run` has here.

- **`otto.context` becomes a package, with no path change.** `git mv context.py
  context/state.py`; the new `context/__init__.py` is a facade in the `otto.lab` shape (strings
  only, `TYPE_CHECKING` imports, a lazy table). Its table serves the fourteen public names of
  `context.py:35-50`: `ContextBinding`, `HostResolver`, `RunPolicy` and `Variant` from their owner,
  `otto.invocation`, and the other ten from `otto.context.state`. Every golden record is keyed
  `otto.context:<name>` (`public_api.txt:171-240`), and there is no deeper public path to break.
  The installer row of §2.1 moves to `otto.context.state`. Like every move in this spec, a class's
  `__module__` follows its owner; the golden keys classes by public path.
- **`otto.session`'s facade becomes its own module.** Its six files are acyclic among themselves
  (`errors` imports `projects` only under `TYPE_CHECKING`) and are declared as one-file modules at
  run. No file moves; the P1b sweep already points internal importers at the six owners.
- **The root keeps its lazy aliases** and adds no executed import to `import_otto`,
  `version_repo` or `completion_repo_warm`. `import otto` runs only the table and the resolver's
  definition (`otto/__init__.py:77-118`); `--version` and a warm TAB are answered by `_shim`
  before any `otto` attribute is read (`_shim.py:57-103`). The `OttoContext`, `get_context`,
  `open_context` and `try_get_context` aliases already name `otto.context` and stay; `run_tests`
  changes from `otto.suite.run` to `otto.suite`, so it passes through that facade's resolver.
  `otto.bootstrap` gets no PEP 562 hook: the pass-through an earlier draft gave it is dropped.
- **Internal code never goes through these facades** (spec 1 §9). The CLI imports the root
  itself, so the resolvers' import is a no-op on every CLI path.

**Internal re-points (P9b).** The model counts 54 runtime import sites of `otto.context` in 17
files (cli 27, suite 10, lab.fleet 6, instructions 4, testing 3, project 2, coverage 1, monitor 1)
and 14 `TYPE_CHECKING` sites. They re-point to `otto.context.state`, or to `otto.invocation` for
the four names it defines, with the scripted sweep P1b uses; a reviewer re-runs the script and
diffs to empty. Non-public names move with the module: `LIBRARY_LAB_NAME`, `T`,
`prepared_policy`, `fill_teardown_deadline`, `set_cli_context` and `reset_cli_context` are reached
at `otto.context.state` (`docs/api/internals/context.rst:8-12` re-points; that page carries no
stability promise). Tests: of the 80 test files that mention `otto.context`, the 17 string patch
targets (`monkeypatch.setattr("otto.context.…")`, `patch("otto.context.…")`) re-point to
`otto.context.state`, as do the imports of non-public names; imports of public names may stay,
since the facade serves them. `tests/unit/test_patch_targets.py` already refuses a patch target
that names a facade binding instead of its owner, so a stale target fails loudly rather than
patching nothing.

**Import cost** (hypotheses; the measurement decides, §4.2):

| surface | P9a | P9b |
|---|---|---|
| `import_otto` (protected) | 0 | 0: `import otto` resolves no name |
| `version_repo` (protected) | 0 | 0: answered in `_shim.py:59-71` |
| `completion_repo_warm` (protected) | 0 | 0: answered by `_shim_complete` |
| `completion_repo_handover` (protected) | 0 | 0: loads none of the four facades; the moved pieces land in `otto.declared` and `otto.config.repo`, which it already loads |
| `help_repo_warm` (protected) | 0 | 0: does not load `otto.context` |
| `help_repo` (cold, not protected) | 0 | **+1**, `otto/context/__init__`: the package is loaded as the ancestor of `otto.context.state`, where `otto.context` loaded before |
| dispatch surfaces (verb bodies, `run`, `test`) | 0 | **+1**, the same module, for the same reason |
| library use (not measured) | resolving `otto.suite:run_tests` or a coverage entry point loads `otto.bootstrap` and its module-scope imports at resolution rather than at the first composing call | the same for `OttoContext`, `open_context` and `install_logging` |

The session facade conversion adds no file anywhere: `session/__init__` is already loaded as an
ancestor wherever a session file is.

**Residual behavior changes: none.** Every row of the audit table behaves as today from every
public spelling. P9a and P9b carry no `!` for this work; the only marked commit in the series is
P4 (S5-4). D2 stays as approved and is unaffected by this section.

### 3.13 Test isolation follows the state (P5, P9a, P9b)

**Today.** The root conftest's autouse `_restore_bootstrap_state` (`tests/conftest.py:1272-1330`)
imports `otto.bootstrap` before every test (`:1312`) and snapshots and restores four of its
globals: `_discovered`, `_result`, `_in_progress`, `_completion_names` (`:1314-1329`). After the
split those globals live in three owners, and importing `otto.bootstrap` installs the initiator,
so a fixture that imports it would hide exactly the case §3.12 protects.

**The fixture becomes `_restore_composition_state`, and it never imports `otto.bootstrap`.**
- It snapshots each piece of state at its owner, reading the attributes unconditionally on a
  loaded owner, so a rename still fails at the snapshot read, as today's docstring requires
  (`:1321-1324`). An owner not yet loaded at setup is reset at teardown if the test loaded it
  (the same as its import-time state). The restore stays reference-only, with the stated limit.
- **The initiator slot keeps one invariant:** it holds `otto.bootstrap._initiate` exactly when
  `otto.bootstrap` is in `sys.modules`. Teardown restores the snapshot, then re-establishes the
  invariant, because a module import cannot be undone. A test that needs "no root loaded" in
  process uses a `no_composition_root` fixture, which clears the slot for the test and restores it
  after; the true cold path is covered only in a fresh process (below).
- The registry restore (`_isolate_registries`) is unchanged; P2's `FIRST_PARTY_ACTIONS` is
  covered by it (§3.2).

| landing | state and its owner after the landing | fixture change |
|---|---|---|
| P5 | `otto.declared`: the published complete and in-progress results and the initiator slot (new); `otto.bootstrap`: `_discovered`, `_result` and `_in_progress`, still its own; `otto.registry`: `_COMPLETION_SNAPSHOT`, formerly `otto.bootstrap`'s `_completion_names` (§2.1) | Snapshot both owners; stop importing `otto.bootstrap`; add the slot invariant and `no_composition_root`. |
| P9a | `otto.declared`: `_result` and `_in_progress` (one home now); `otto.bootstrap`: `_discovered`; `otto.registry`: `_COMPLETION_SNAPSHOT` (since P5) | `_result` and `_in_progress` are read at `otto.declared`. The test helpers that set them (`tests/_fixtures/bootstrap_seam.py`, `tests/integration/conftest.py`, `tests/unit/bootstrap/test_bootstrap.py`) re-point. Patches migrate with their consumers (rule below): a patch of a moved accessor re-points to `otto.declared` in the landing whose consumer re-points. In P9a that is the CLI's readers of `get_repos`, `get_ordered_repos` and `is_bootstrapped`, and the coverage, suite, monitor and docker readers of `current_repos` and `current_ordered_repos` (patched today at `otto.config.fleet`). Patches whose consumer keeps the `otto.bootstrap` alias until P9b stay: `tests/unit/session/test_logs.py:91-94` patches `otto.bootstrap.get_repos`, which `session/logs.py:57-61` reads. Patches of `otto.bootstrap.bootstrap` stay in every landing: it is not a re-export, and the initiator looks it up at call time. |
| P9b | `otto.config.repo`: `_discovered`; `otto.registry`: `_COMPLETION_SNAPSHOT` (since P5) | `_discovered` is read at `otto.config.repo`. The remaining staged patches move with their consumers, in the same commit: `test_logs.py:91-94` to `otto.declared.get_repos` (with `session/logs.py:57`), and patches of `discover`, `get_env` and `discovered_teardown_deadline` to `otto.config.repo` (with the CLI, `context.py:1230` and `env/manage.py:302`). |

**Amended by P9a, 2026-10-16.** No test patched `current_repos`/`current_ordered_repos` at `otto.config.fleet` at BASE; the readers' patches re-pointed are the 21 files patching `otto.bootstrap.get_repos`/`get_ordered_repos`/`is_bootstrapped`. The exclusion is `tests/unit/session/test_logs.py:93` (`get_repos`, expires P9b); the self-test case is `tests/unit/test_patch_targets.py:213` at BASE (not `:180-189`).

`tests/unit/test_env_hermeticity.py`, which re-runs the historical poisoner/victim pair, must stay
green at each of the three landings.

**Patches migrate with their consumers.** A `monkeypatch.setattr` replaces one binding: the one
the code under test reads. So a patch target moves in the same landing as the import it
replaces, never earlier and never later. While a consumer reads a name through an eager
re-export (`from ..bootstrap import get_repos`), its tests patch that re-export; once the
consumer imports the owner, they patch the owner. Each staged landing's plan lists every patch of
a moved name with the consumer it serves, generated by mapping each patch site to the module its
test exercises, and a reviewer re-runs that script.

**A guard for eager re-exports (lands in P5, with the first one).** `test_patch_targets.py`
checks only names served by a package `__init__`'s lazy table (`tests/_fixtures/_lazy_exports.py:49-63`,
`test_patch_targets.py:92-99`); `otto.bootstrap`'s re-exports are eager module attributes, which
it cannot see, and its self-test even accepts `otto.bootstrap.get_repos` (`:180-189`). P5 adds a
second scan to the same test file:
- **What it finds.** Every string patch target `otto.bootstrap.<name>` (and the `setattr(module,
  "<name>")` form on an imported `otto.bootstrap`) where the object `otto.bootstrap.<name>` was
  defined elsewhere: its `__module__` is not `otto.bootstrap`, or, for a non-class, it is the same
  object as an attribute of its owner. The owner is found by identity at test time, so the list
  of re-exports is never written by hand.
- **What it reports.** `file:line otto.bootstrap.<name> -> patch <owner>.<name>`, like the lazy
  scan.
- **Exclusions, each with its expiry.** A short table of `(test file, name)` pairs whose consumer
  still reads the alias. It is empty except in P9a, where it holds
  `tests/unit/session/test_logs.py`, `get_repos` (expires P9b). A stale exclusion fails the test.
- **Self-test.** The case at `:180-189` changes: `otto.bootstrap.get_repos` becomes a reported
  case from P9a on, and the passing example becomes `otto.declared.get_repos`. New cases cover
  a moved error type (from P5), an exclusion, and a stale exclusion.

The same rule and guard apply to the facade patches of P9b (`otto.context.*` →
`otto.context.state`), which the lazy-table scan already sees once `otto.context` is a facade.

**Kept unchanged:** `tests/unit/test_context_repos.py:35-87`, including the snapshot-identity test
(`:78-87`) and the cached-refusal test (`:53-68`). They import `otto.bootstrap` and patch
`otto.bootstrap.bootstrap`, which the initiator honors. Only their imports of `Lab` (P7) and
`LIBRARY_LAB_NAME` (P9b) re-point.

**Fresh-process acceptance (P9b).** Each case runs in a new interpreter (a subprocess with a
scratch `OTTO_SUT_DIRS` repo whose init module records that it ran) that has not imported
`otto.bootstrap`. Each asserts the result and that composition ran at most once:

| case | spellings | expected |
|---|---|---|
| open a context | `from otto.context import open_context`; `otto.open_context` | composes, as today |
| run tests | `from otto.suite import run_tests`; `otto.run_tests` | the library sentinel composes, as today |
| prepare a run | `otto.suite:prepare_run` with `cov_tickets_json` set | composes, as today |
| coverage | `otto.coverage:clean_coverage` with no `repos` | composes, then refuses as today |
| logging | `otto.session:install_logging` | composes, as today |
| hand-built context | `from otto.context import OttoContext`; `otto.OttoContext`; `OttoContext(lab=…).repos` | composes lazily on the first read, once; a refusal is cached |
| hand-built context, result given | `OttoContext(lab=…, bootstrap=result).repos` | never composes |
| no root | only internal owners loaded (`otto.context.state.OttoContext(lab=…).repos`, `otto.declared.get_repos()`) | raises `RuntimeError` naming `otto.bootstrap.bootstrap()` and `otto.open_context` |
| no root, provider gate | `otto.lab:load_lab` with a repo-attributed product provider | admits (D2) |
| laziness | `import otto`; `from otto.context import get_context` | `otto.bootstrap` not in `sys.modules` |

The two P9a routings (`otto.suite`, `otto.coverage`) get their fresh-process cases in P9a, with
the state move that needs them.

## 4. Constraints on every move

### 4.1 Public paths and members

Every moved name stays reachable at its declared path. Lazy-table changes per phase:

| phase | change |
|---|---|
| P3 | the 16 bindings of §3.3 |
| P6 | the `otto.config` settings-type entries |
| P7 | `lab/__init__.py:48-49`; `otto/__init__.py:85` |
| P9a | `lab/__init__.py`'s fleet entries; `otto/__init__.py:82-84`, `:86` |
| P9b | the new `otto/context/__init__.py` table (fourteen names); `otto/__init__.py:106` (`run_tests`) binds to `otto.suite` (§3.12) |

- The golden keys symbols by public path, so no definition move changes it. `otto.cli.registry`
  and `otto.instructions` gain no `__all__` entries.
- `api/public.toml` is unchanged; every path it records keeps resolving.
- `Repo.get_instructions_panel` stays (§3.2), the `DockerContainerHost` constructor is unchanged
  (§3.4), and no base class is added anywhere. S5-4 changes behavior only: no golden line, no
  public path and no public member changes (§3.4).
- **S5-5 keeps every public name.** `otto.bootstrap` re-exports what moves out of it, so all its
  golden lines (`public_api.txt:3-47`) and its `__all__` stay. The golden's MRO lines name
  classes by public path, and those paths are unchanged.

### 4.2 Import cost (hypotheses until measured)

Each phase runs `scripts/import_budget.py --report-json` on its base and tip under 3.10, for every
surface, and records the table in its commit body (spec 1 §7). "Help" means the `help_repo_warm`
dispatch harness. Each statement below is a hypothesis; the measurement decides.

| phase | hypothesis | if disproved |
|---|---|---|
| P1a | Neutral: tach and test files only. | — |
| P1b | Neutral: the walkers move into `cli/registry`, which help and handover already load. The monitor facade rule changes only tach declarations, not what any import loads. | Keep them in a module those surfaces already load. |
| P2 | On `completion_repo_handover`, a swap: the handover loads `instruction_catalog` instead of `instructions`. The four project importers re-point, and `cli/run.py`, now the only module-scope importer of the face, is not on the handover. File operations are measured. | Re-point any protected surface still importing the face. |
| P3 | Neutral: no module is added (the impairer contract joins `params`), and `otto.host` is already loaded wherever `models/host.py` was (`:76-81`). | — |
| P4 | A decline where a container host was touched while stopped: `otto.docker.resolve`, `deployment` and `compose` are no longer imported by the host. No protected surface changes. | — |
| P5 | Neutral where `models.settings` is loaded, which already imports `otto.declared` (`models/settings.py:44`). A surface that loads `otto.bootstrap` without `models.settings` gains `otto.declared`; the measurement names any such surface. | A protected-surface growth goes to the owner before landing. |
| P6 | Neutral: `models.settings` is loaded wherever `config.repo` is. | Home the types in `models.base`. |
| P7 | Neutral if `config.lab` is on no protected surface; otherwise +1 (`otto.lab.model`). The quoted annotation keeps the model off the loader's module scope. | — |
| P8 | +1 leaf (`active_run`) wherever `otto.context` loads. Named, non-protected surfaces: the dispatch surfaces, `run` and `test`. | — |
| P9a | +1 (`otto/lab/__init__`) wherever `fleet` loads and `otto.lab` did not: the dispatch surfaces, none protected. The accessor move is neutral: its pieces land in `otto.declared`, already loaded wherever `otto.bootstrap` is. The suite and coverage routing adds nothing on a CLI path. | A protected-surface growth goes to the owner. |
| P9b | +1 (`otto/context/__init__`) on `help_repo` and the dispatch surfaces, none protected (§3.12). Discovery lands in `otto.config.repo`, already loaded wherever `otto.bootstrap` is; the `otto.project` imports stay inside `bootstrap()`, where the by-name imports already load the same modules. A surface that loaded `otto.bootstrap` only through a lower reader may decline. `import_otto`, `version_repo` and `completion_repo_warm` load none of it before or after. | A protected-surface growth goes to the owner. |

Declines are locked in on 3.10–3.14 by lowering each declined surface's ceiling by exactly its
measured delta, and only when two runs agree. A landing never re-records every surface, which
would absorb unrelated drift (amended by P1b, 2026-10-11). P1b's declines were `monitor` and
`test`, −7 on every minor.

**P2, measured (amended by P2, 2026-10-12).** Row P2's swap does not happen: a repo whose init
imports `otto.instructions` loads both the face and the catalog. Every surface that loads a repo's
init therefore grows by the catalog's exact footprint, +4 file operations on every minor (+1
`otto:*`, +2 `otto:instruction_catalog`, +1 its bytecode). That is 24 of the 38 surfaces: the
protected `completion_repo_handover` and 23 non-protected surfaces (`bootstrap_repo`, the two
warm dispatch surfaces, `help_repo`, the five `host_local`/`host_ssh` surfaces, `test_repo`, and
13 tracked surfaces). Owner ruling D-P2-1 approved exactly that footprint on exactly those
surfaces; any other growth, or a listed surface that did not grow, would have stopped the landing.
No surface declined.

**P3, measured (amended by P3, 2026-10-12).** Row P3 holds: neutral. All 38 surfaces are +0 on
every minor, and none declined. Four runs reproduced base and tip exactly, totals and every
bucket. At unchanged totals, the two moved files' opens are counted under `otto:host` instead of
`otto:models` (4 or 8 operations per surface), and on 3.10 `host_local_exec` moves one operation
from `other` to `fd`. A bucket move that every run reproduces exactly is the change itself, not
noise; the stop rule for a bucket shuffle at an unchanged total applies to a run that does not
reproduce its own base.

**P4, measured (amended by P4, 2026-10-13).** Row P4's predicted decline does not appear. All 38
surfaces are +0 on every minor, no bucket moved, and base and tip each reproduced exactly. The
removed imports were function-local in `_auto_up`, which ran only when a placeholder's probe
found nothing, and no measured surface constructs a placeholder.

**P5, measured (amended by P5, 2026-10-14).** Row P5's predicted growth does not appear. The
only surfaces that loaded `otto.bootstrap` without `models.settings` were the nine verb `--help`
surfaces, which reached it through `cli/main.py`'s function-local completion read. P5 moves the
completion snapshot to `otto.registry` (§2.1), so those nine stop loading the root and decline by
4 file operations on every minor (−1 `otto:*`, −2 `otto:bootstrap`, −1 its bytecode). Every other
surface is +0. Three base-vs-tip pairs agreed. The only difference between runs was one
operation on 3.10 `host_local_exec` that alternated between the `fd` and `other` buckets at an
unchanged total, in either direction, so it is noise, not the change.

**P6, measured (amended by P6, 2026-10-14).** Row P6 holds: neutral. Harness, base `6dc06ed21` vs
the landing, two runs per minor: 3.10: 38 surfaces, 0 changed; 3.11: 38 surfaces, 0 changed; 3.12:
38 surfaces, 0 changed; 3.13: 38 surfaces, 0 changed; 3.14: 38 surfaces, 0 changed. No bucket moved
at an unchanged total. No decline.
Outside the harness, on 3.10, fresh processes, twice: import otto.config.repo: otto 7 -> 7, modules
54 -> 54, file ops 48 -> 48; from otto.config import Repo: otto 7 -> 7, modules 54 -> 54, file ops
48 -> 48; from otto.config import DockerSettings: otto 7 -> 5, modules 54 -> 43, file ops 48 -> 39;
and every public name of `otto.docker`, `otto.config`, `otto.models`, `otto.monitor` and each
namespace that reaches a moved name: SUMMARY 0 grow, 14 shrink, 129 unchanged, 0 failed, 0 not
reproduced of 143 (both runs). On BASE, `otto.config.repo` loads on 26 of the 38
surfaces and `otto.models.settings` on exactly the same 26; the home was chosen against six
measured candidates (§3.6).

**P7, measured (amended by P7, 2026-10-15).** Row P7 holds on the harness: neutral. `config.lab`
loaded on no protected surface (on 21 others: the eight lab-loading surfaces, the eleven tracked
verbs, `tracked_host_probe` and `tracked_host_power`). Harness, base `cfb1c05cd` vs
the landing, two runs per minor: 3.10: 38 surfaces, 0 changed; 3.11: 38 surfaces, 0 changed; 3.12: 38 surfaces, 0 changed; 3.13: 38 surfaces, 0 changed; 3.14: 38 surfaces, 0 changed. On every minor the surfaces that load the lab model moved
four operations from `otto:config` to `otto:labs` at an unchanged total, reproduced in both runs
(21 surfaces on 3.10: `dispatch_local_warm`, `dispatch_repo_warm`, `host_local_exec`, `host_local_get`,
`host_local_put`, `host_ssh_exec`, `host_ssh_login`, `test_repo` and the thirteen `tracked_*` verbs;
on 3.10 `host_local_exec` also swaps one `other` operation for one `fd`); the per-surface lines are
in the squash message. No decline. Outside the harness, on every minor 3.10–3.14, fresh processes,
twice each, with the same deltas on every minor; on 3.10: from otto.lab import Lab: otto 5 -> 4, modules 43 -> 42, file ops 39 -> 38; from otto.lab import load_lab: otto 5 -> 4, modules 43 -> 42, file ops 39 -> 38; from otto import load_lab: otto 4 -> 3, modules 42 -> 41, file ops 38 -> 37; import otto.labs: otto 2 -> 2, modules 25 -> 25, file ops 22 -> 22; from otto.labs import LabRepository: otto 3 -> 3, modules 34 -> 34, file ops 30 -> 30; from otto.lab import get_lab: otto 22 -> 21, modules 118 -> 117, file ops 105 -> 104; and every public name of
`otto.lab`, `otto.labs`, `otto.config` and each namespace that reaches the lab model or the fleet:
3.10 SUMMARY 0 grow, 0 allowed, 51 shrink, 63 unchanged, 0 failed, 0 not reproduced of 114; 3.11 SUMMARY 0 grow, 0 allowed, 51 shrink, 63 unchanged, 0 failed, 0 not reproduced of 114; 3.12 SUMMARY 0 grow, 0 allowed, 51 shrink, 63 unchanged, 0 failed, 0 not reproduced of 114; 3.13 SUMMARY 0 grow, 0 allowed, 51 shrink, 63 unchanged, 0 failed, 0 not reproduced of 114; 3.14 SUMMARY 0 grow, 0 allowed, 51 shrink, 63 unchanged, 0 failed, 0 not reproduced of 114. Nothing grows: the fleet and the compose verbs import `Lab` for
type checking only, so a library user reaching them loads one module fewer, and every shrink is
locked at its measured delta (D-P7-2).

**P8, measured (amended by P8, 2026-10-15).** Row P8's +1 leaf does not appear: the slot is in
`otto.invocation`, already loaded wherever `otto.context` is (§3.8). Harness, base `1b55fcd2b`
vs the landing, two runs per minor: 3.10: 38 surfaces, 2 changed; 3.11: 38 surfaces, 2 changed; 3.12: 38 surfaces, 2 changed; 3.13: 38 surfaces, 2 changed; 3.14: 38 surfaces, 2 changed. No bucket moved at an unchanged total. The two dispatch surfaces (`dispatch_local_warm`, `dispatch_repo_warm`) decline by 4 operations (`otto:config` −4) on every minor, reproduced in both runs: they no longer load `otto.config.fleet`. Outside the harness, on every minor
3.10–3.14, fresh processes, twice each, with the same deltas on every minor; on 3.10: from otto.context import get_context: otto 18 -> 18, modules 114 -> 114, file ops 100 -> 100; import otto.invocation: otto 2 -> 2, modules 35 -> 35, file ops 29 -> 29; from otto.reservations import ReservationGate: otto 4 -> 4, modules 38 -> 38, file ops 32 -> 32; from otto.lab import fleet_of_interest: otto 21 -> 20, modules 117 -> 116, file ops 104 -> 103; call fleet_of_interest(lab, []): otto 25 -> 24, modules 121 -> 120, file ops 109 -> 108; call the gate's derived read under a context: otto 29 -> 28, modules 125 -> 124, file ops 114 -> 113; call get_context() under a context: otto 21 -> 21, modules 117 -> 117, file ops 104 -> 104; and every public name of `otto`,
`otto.context`, `otto.lab`, `otto.reservations` and each namespace that reaches the context, the
gate or the fleet: 3.10 SUMMARY 0 grow, 0 allowed, 10 shrink, 91 unchanged, 0 failed, 0 not reproduced of 101; 3.11 SUMMARY 0 grow, 0 allowed, 10 shrink, 91 unchanged, 0 failed, 0 not reproduced of 101; 3.12 SUMMARY 0 grow, 0 allowed, 10 shrink, 91 unchanged, 0 failed, 0 not reproduced of 101; 3.13 SUMMARY 0 grow, 0 allowed, 10 shrink, 91 unchanged, 0 failed, 0 not reproduced of 101; 3.14 SUMMARY 0 grow, 0 allowed, 10 shrink, 91 unchanged, 0 failed, 0 not reproduced of 101. Nothing grows, and every
shrink is locked at its measured delta (D-P8-2).

**P9a, measured (amended by P9a, 2026-10-16).** Row P9a's +1 does not appear: the dispatch surfaces
never load the fleet, and the fleet is `otto.fleet`, not under `otto.lab` (D-P9a-2). Harness, base
`e65ea043a` vs the landing, two runs per minor: 3.10: 38 surfaces, 0 changed; 3.11: 38 surfaces, 0 changed; 3.12: 38 surfaces, 0 changed; 3.13: 38 surfaces, 0 changed; 3.14: 38 surfaces, 0 changed. Bucket moves, reproduced in both runs, each at an unchanged total: 3.10 host_local_exec 2595 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 host_local_get 2607 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 host_local_put 2608 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 host_ssh_exec 4452 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 host_ssh_login 4452 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 test_repo 7049 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 tracked_host_power 3123 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.10 tracked_host_probe 3123 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-310.pyc': 1}; 3.11 host_local_exec 2588 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 host_local_get 2600 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 host_local_put 2601 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 host_ssh_exec 4445 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 host_ssh_login 4445 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 test_repo 6929 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 tracked_host_power 3112 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.11 tracked_host_probe 3112 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-311.pyc': 1}; 3.12 host_local_exec 2619 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 host_local_get 2631 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 host_local_put 2632 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 host_ssh_exec 4476 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 host_ssh_login 4476 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 test_repo 7224 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 tracked_host_power 3143 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.12 tracked_host_probe 3143 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-312.pyc': 1}; 3.13 host_local_exec 2552 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 host_local_get 2564 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 host_local_put 2565 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 host_ssh_exec 4444 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 host_ssh_login 4444 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 test_repo 7232 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 tracked_host_power 3094 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.13 tracked_host_probe 3094 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-313.pyc': 1}; 3.14 host_local_exec 2634 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 host_local_get 2646 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 host_local_put 2647 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 host_ssh_exec 4531 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 host_ssh_login 4531 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 test_repo 7338 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 tracked_host_power 3176 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}; 3.14 tracked_host_probe 3176 moved {'otto:*': 1, 'otto:config': -4, 'otto:fleet': 2, 'otto:fleet.cpython-314.pyc': 1}. Outside the harness, on every minor 3.10-3.14, fresh processes, twice each;
on 3.10: import otto.bootstrap: otto 8 -> 8, modules 53 -> 53, file ops 46 -> 46; from otto.bootstrap import get_repos: otto 8 -> 8, modules 53 -> 53, file ops 46 -> 46; import otto.declared: otto 4 -> 4, modules 49 -> 49, file ops 41 -> 41; from otto.suite import run_tests: otto 5 -> 5, modules 60 -> 60, file ops 47 -> 47; from otto.coverage import collect_coverage: otto 8 -> 8, modules 46 -> 46, file ops 43 -> 43; from otto.lab import get_lab: otto 20 -> 18, modules 116 -> 114, file ops 103 -> 100; call is_bootstrapped(): otto 8 -> 8, modules 53 -> 53, file ops 46 -> 46; call get_lab() under a context: otto 24 -> 22, modules 120 -> 118, file ops 108 -> 105; call docker's lab read under a context: otto 60 -> 59, modules 271 -> 270, file ops 381 -> 380; and every public name
of `otto`, `otto.lab`, `otto.bootstrap`, `otto.suite`, `otto.coverage`, `otto.docker` and each
namespace that reaches the root, the catalog or the fleet: 3.10 SUMMARY 0 grow, 0 allowed, 11 shrink, 367 unchanged, 0 failed, 0 not reproduced of 378; 3.11 SUMMARY 0 grow, 0 allowed, 11 shrink, 367 unchanged, 0 failed, 0 not reproduced of 378; 3.12 SUMMARY 0 grow, 0 allowed, 11 shrink, 367 unchanged, 0 failed, 0 not reproduced of 378; 3.13 SUMMARY 0 grow, 0 allowed, 11 shrink, 367 unchanged, 0 failed, 0 not reproduced of 378; 3.14 SUMMARY 0 grow, 0 allowed, 11 shrink, 367 unchanged, 0 failed, 0 not reproduced of 378. Nothing grows, and every shrink is locked at its measured delta.

### 4.3 Package ancestors

Importing a carved module runs every ancestor `__init__`, so every ancestor of a carved module
stays lazy. Its runtime body holds only `TYPE_CHECKING` imports, a PEP 562 table, and the
stdlib-only eager imports that `.ast-grep/rules/lazy-package-init-stays-lazy.yml:65-75` pins.
Today that is `config/__init__.py:21` importing `.env`, which is stdlib-only (`config/env.py:3-6`).
P5 and P6 only remove names from `config/env.py`. (P6 removed `split_path_list`, `_PATH_LIST_SEP` and the `os`/`re` imports; `config/env.py` stays stdlib-only.)

Ancestors this covers:
- `otto/config` (scope, home, user_settings);
- `otto/models` (base, dependencies);
- `otto/link` (impairer, params, netem);
- `otto/labs` (errors, loader);
- `otto/lab` (model, fleet);
- `otto/host` (specs, option_specs);
- `otto/monitor` (the eleven declared files of §3.11);
- `otto/context` (`state`) and `otto/session` (its six files). Their facades import the root only
  inside `__getattr__`, never at module scope, so importing an implementation file loads no root;
- the root `otto`.

Each carve adds an `IMPORT_MUST_NOT_LOAD` row to `tests/unit/test_import_contracts.py`. For
example, `import otto.config.scope` loads no `otto.*` module beyond `otto`, `otto.config`,
`otto.config.env`, `otto.config.scope` and its declared dependencies.

**Amended by P5, 2026-10-14.** The carved modules' rows are the P3 amendment's
`IMPORT_ONLY_DECLARED` form, not `IMPORT_MUST_NOT_LOAD`. The example above needs an ancestor
allowance, added as `ancestor_eager_imports`: the top-level relative imports of each ancestor
`__init__`, which the ast-grep rule keeps stdlib-only. A pin checks that `otto.config`'s eager
imports are exactly `otto.config.env`.

**Amended by P6, 2026-10-14.** `otto.config.home` and `otto.config.user_settings` get `IMPORT_ONLY_DECLARED` rows (`otto.models` and `otto.models.dependencies` held to function scope for the home), and `otto.utils` gets one too. `otto.models.settings` is not a tach module, so its row is the `IMPORT_CONTRACTS` forbidden form: it loads no `otto.config`. `IMPORT_CONTRACTS["import otto.config.repo"]` pins that the repo loader loads no settings model.

## 5. Enforcement

### 5.1 tach

- **P1a** sets `layers_explicit_depends_on = true`, declares `layers`, gives an explicit
  `depends_on` to the nine modules that lack one (`utils`, `params`, `errors`, `console`,
  `filesystem`, `snmp`, `tls`, `version`, `_webassets`), and declares `otto.layout` and
  `otto._shim_complete`. None of this changes the graph.
- **P1b** applies the facade rule to `otto.monitor` (§3.11): it declares the eleven lower monitor
  files and folds `otto.monitor.live` into `otto.monitor`. A comment above those eleven entries in
  `tach.toml` says why they are one-file modules: they keep `otto.monitor.parsers`,
  `otto.monitor.snmp` and `otto.monitor.log_sourced` at their public paths and keep the collector
  runtime's internal loop inside `otto.monitor`, so folding them into a subpackage would be a
  public path change. P9b adds the same kind of comment above session's six modules.
- Each later phase declares what it carves, in the commit that frees it. S5-1 would now allow a
  module to be carved earlier and join the cycle for a while, but that only adds `BASELINE`
  entries for a later landing to remove; no carve is cleaner that way, so none is moved.
- **P9a** declares `otto.lab.fleet`. **P9b** declares the last unowned files, `otto.context.state`
  and session's six files, places `otto.context` and `otto.session` in entry and
  `otto.bootstrap` in root, and sets `forbid_circular_dependencies = true`.

**Amended by P9a, 2026-10-16.** P9a declares `otto.fleet`, not `otto.lab.fleet` (§3.9, D-P9a-2); the bullet naming `otto.lab.fleet` is superseded.

### 5.2 Layers, assigned as modules leave the cycle

`layers` is declared in P1a. In each landing, every module outside the cycle gets its layer;
modules still inside it stay unlayered, which exempts them. P9a uses three interim layers
(`otto.bootstrap` at composition, `otto.context` and `otto.session` at run, §3.9) that P9b
replaces with their final ones.

| after | P1a | P1b | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9a | P9b |
|---|---|---|---|---|---|---|---|---|---|---|---|
| modules layered | 19 | 35 | 39 | 43 | 43 | 48 | 54 | 57 | 59 | 65 | 77 |

**Amended by P7, 2026-10-15.** P7 layers `otto.labs` alone (55, not 57): no module is carved for the lab model (§3.7). The later columns assumed `otto.lab.model` and `otto.labs.errors`; without them they are two lower (P8 57, P9a 63, P9b 75) unless a later landing carves a module this map does not show. Each landing confirms its own column, as P7 did; `tests/unit/test_dynamic_import_audit.py`'s `LAYERED_AT` rows for P8, P9a and P9b are amended by the landing that reaches them.

**Amended by P8, 2026-10-15.** P8 layers `otto.reservations` alone (56, not 57): no module is carved for the active run (§3.8). The later columns drop by one more (P9a 62, P9b 74) unless a later landing carves a module this map does not show; `LAYERED_AT` rows past P8 stay for their landings to confirm.

**Amended by P9a, 2026-10-16.** P9a layers 62 modules, as amended by P8: the six interim layers (`otto.bootstrap`, `otto.config` at composition; `otto.context`, `otto.docker`, `otto.session`, `otto.fleet` at run). `LAYERED_AT["P9b"]` stays for P9b to confirm (74 by P8's amendment; the audit's row still reads 77).

The model finds no layer violation at any landing.

### 5.3 String audit (lands in P1a)

`tests/unit/test_dynamic_import_audit.py` enumerates every first-party string the import system
resolves: facade lazy-table bindings, literal `Ref` and `import_module` targets, and `Ref`s built
from literal tables. A string whose target is outside its own tach module is a **string edge**.

**Classes.** Every string edge is listed with exactly one class:
- **facade binding**: a facade's lazy-table entry;
- **composition-root by-name import**: `importlib.import_module("<literal>")` in `otto.bootstrap`;
- **first-party registry built-in**: a literal `Ref`, or one built from a literal table;
- **user-supplied**: a target read from a repo's settings or a registration (computed; below).

**The test fails when:**
1. a string edge is not listed, or a listed entry no longer exists;
2. a string edge points upward and is not a grandfathered entry. "Upward" is judged by the
   layers `tach.toml` declares at that landing, interim ones included (P9a's, §5.2), the same
   layers the static check uses, never by the end-state map;
3. with the grandfathered entries removed, some cycle of static ∪ string edges is not contained
   in the static cycle the ratchet carries at that landing (tach sees static edges only, so this
   is the check `forbid_circular_dependencies` cannot make);
4. a new string edge has the same source and target tach modules as a grandfathered entry
   (no additions on a grandfathered edge);
5. a grandfathered entry is still present once its expiry landing is the current one.

A new exception would need the owner's approval in a spec; the end state has none.

**Grandfathered entries (transitional, each with its expiry).** The audit lands on today's tree,
so it starts with these, and each goes in the landing named:

| source | target | class | why it is grandfathered | gone in |
|---|---|---|---|---|
| `host/__init__.py:259-262` (`EmbeddedHostSpec`, `HostSpec`, `PowerControlSpec`, `UnixHostSpec`) | `otto.models.host` | facade binding | upward (host → specs), inside the static cycle | P3 (§3.3) |
| `host/os_profile.py:459`, `:466`, `:473` | `otto.models.host:UnixHostSpec`, `:EmbeddedHostSpec` | first-party registry built-in | upward (host → specs), inside the static cycle | P3 (§3.3) |
| `bootstrap.py:363` | `otto.project.actions` | composition-root by-name import | inside the static cycle at P1a and P1b; from P2 to P9a it closes a combined loop outside it (`bootstrap` ⇢ `project` → `context` → `bootstrap`); upward in P9a, where `otto.bootstrap` is interim composition | P9b (§3.9) |
| `bootstrap.py:383` | `otto.project.commands` | composition-root by-name import | the same | P9b (§3.9) |

String edges whose two ends are both inside the current static cycle need no entry: rule 3
already contains them, and they leave with the cycle. In the model they are the `otto.monitor` →
`otto.monitor.live` bindings (P1a only; folded into one module in P1b) and the nine `otto.models`
→ `otto.host` bindings of P3 and P4, which point downward and leave the cycle with `otto.host` in
P5.

**Model result, every landing** (string edges judged by each landing's own layers). With the
grandfathered entries removed, no combined cycle reaches outside the static cycle at any landing,
and no unlisted string edge points upward. Upward string edges per landing: 7 from P1a to P2 (the
host spec bindings and `Ref`s), none in P3 to P8, 2 in P9a (`bootstrap.py:363`, `:383`,
composition → workflows while `otto.bootstrap` is interim composition), none in P9b. P9a has four
same-layer string edges. From P9b on the grandfathered list is empty. The end state has eight same-layer string edges: the root
facade's `app`, `register_cli_command` and `cli_command` (entry → entry), its four
`otto.context` aliases (entry → entry), and the stock impairer `Ref` (`link/impairer.py:152`,
foundation → foundation), which closes no loop after §3.3.

**Computed targets.** Each is pinned by file, line and class. The inventory is regenerated on
each landing's own tree, never copied from today's: on today's tree there are 46; P1b moves the
`completion_cache.py:1576` site into `cli/registry.py` with its walker, and P9b adds the
`otto.context` facade's resolver, so the end state has **47**:
- **32 are facade `__getattr__` lookups over literal tables:** the package `__init__` resolvers
  (`otto/context/__init__.py` among them), `otto/__init__.py:118` and `config/__init__.py:75`.
- **4 are user-supplied:** `bootstrap.py:369`, `config/repo.py:728`, `host/product.py:908`,
  `host/dev_tool.py:345`.
- **3 are CLI command tables:** `cli/main.py:407` and the two in `cli/registry.py` (`:240`, and
  the walker moved from `completion_cache.py:1576`).
- **8 resolve registry `Ref`s already audited as literals:** `registry.py:156`, `params.py:413`,
  `:495`, `host/command_frame.py:803`, `host/embedded_filesystem.py:264`,
  `host/transfer/registry.py:346`, and `host/shell_kind.py:203`, `:342`.

P1a reads each site to confirm its class; each later landing re-reads the sites it moves.

**Amended by P1a, 2026-10-11 (accepted by the owner with the P1a plan).** Building the audit
against the code changed this section in four places. Where this block and the text above
disagree, this block holds:
- **Computed sites are keyed by file and enclosing scope, not line,** so an edit elsewhere in
  the file does not fail the audit. One scope may hold two sites (`logger/__init__.py`'s
  `__getattr__`), so the listing is a multiset; every diagnostic still names the line.
- **Five sites change class on reading.** `host/product.py:908` and `host/dev_tool.py:345` loop
  over otto's own literal kind tables, so they are registry `Ref`s over a literal table, not
  user-supplied. `params.py:413` and `:495` (strings an init module passes to
  `register_options`) and `host/shell_kind.py:342` (a product's `class` from settings) are
  user-supplied. The split on today's tree is 31 facade resolvers, 5 user-supplied, 3 CLI command
  tables, 5 `Ref`s over literal tables, the `Ref` engine (`registry.py:156`) and one
  loaded-module lookup (`host/shell_kind.py:203`): 46.
- **Rule 2 finds no upward string edge at P1a, not 7.** The seven host → specs sites join two
  cycle members, which are unlayered at P1a, so the layers the landing declares do not make them
  upward. The "7 upward from P1a to P2" count above came from the end-state map, which rule 2
  forbids using. The seven stay grandfathered because rule 4 applies to them, and they become
  upward-relevant once `otto.host` and `otto.models` take layers.
- **Rules 2 and 3 skip a file no tach module owns.** Such a file (the root `otto/__init__.py`,
  `otto/lab`, `otto/env`, `otto/declared.py`, `otto/_shim.py`, `otto/__main__.py`) is owned,
  for listing, by its top-level package, and its string edges are listed (rule 1). It has no
  layer and no node in tach's graph. Until P9b declares the last of these files, the audit is
  therefore not a whole-tree cycle guarantee, exactly as tach is not (§1.1's two hidden loops).

The final reviews added guards the rules above imply:
- The audit also reads `register_cli_command`'s literal loaders. It refuses what it cannot
  read: `__import__` and the other by-name importers, an aliased import, `Ref` or an importer
  passed as a value, and a lazy table written outside its literal.
- The grandfathered rows and the unowned-file list are pinned shrink-only. An added row, or an
  expiry moved later, fails, because it needs a spec amendment.
- `CURRENT_LANDING` is tied to the tree. Between its landing's row of §5.2's layered count and
  §6's cycle size and the next differing row, the tree may stand anywhere, as it does
  mid-landing. Reaching the next row demands the bump, and the last landing's row is exact. No
  module inside the cycle may have a layer.

### 5.4 Ratchet

The ratchet stays. Each landing edits `BASELINE` in the same commit: it deletes what it removes
(Appendix A) and lists anything it adds, with the removing phase in the commit body (S5-1). In
the model no landing adds an entry. `BASELINE` is empty after P9a, and the test remains as an
empty-baseline guard (`test_import_cycle_ratchet.py:158-174`).

## 6. Phases

Every landing is ratchet-legal: nothing joins the cycle and no in-cycle edge is added unless the
landing lists it (S5-1). Every landing has no layer violation. Each is its own plan and its own
squashed commit. Gates follow the squash-aware rule:
- targeted gates per task;
- then, once before the squash, the full gate suite, `make gate-fresh`, real
  `tach check --exact`, the ratchet, the string audit, and the import-budget table on 3.10–3.14.

The counts below are simulator-derived; the real tree confirms them per landing.

| landing | cycle after | in-cycle edges | ratchet change | members that leave | tach modules declared | new edges outside the cycle |
|---|---|---|---|---|---|---|
| today | 22 | 105 | — | — | — | — |
| P1a enforcement switches and the string audit | 22 | 105 | none (graph-neutral) | — | layout, _shim_complete (and 9 made explicit) | — |
| P1b completion collectors into the CLI; facade sweep; monitor facade rule | 16 | 61 | −44 edges | cli, coverage, init, monitor, monitor.live, suite | the eleven `otto.monitor.*` lower files; `otto.monitor.live` folded into `otto.monitor` | link→result |
| P2 instruction catalog | 13 | 47 | −14 | instructions, project, tunnel | instruction_catalog | — |
| P3 host specs into otto.host; impairer contract into params | 13 | 45 | −2 | — | models.base, link.impairer, link.params, link.netem | — |
| P4 container auto-start removed (S5-4; breaking, its own marked commit) | 13 | 44 | −1 | — | — | — |
| P5 composed-repos catalog; scope and declared drop | 11 | 31 | −13 | check, host | config.scope, models.dependencies, declared | — |
| P6 settings types and helpers down | 7 | 16 | −15 | creds, inventory, link, models | config.home, config.user_settings | bootstrap→models, config→utils |
| P7 Lab model and loader | 6 | 12 | −4 | labs | lab.model, labs.errors | — |
| P8 active-run contract; reservations | 5 | 9 | −3 | reservations | active_run | reservations→host |
| P9a accessors down; docker's reads; fleet under otto.lab | 0 | 0 | −9 (empty) | bootstrap, config, context, docker, session | fleet | cli→declared, cli→fleet, coverage→bootstrap, coverage→declared, coverage→fleet, docker→invocation, fleet→config.scope, fleet→context, fleet→host, fleet→reservations, monitor→declared, monitor→fleet, suite→bootstrap, suite→declared, suite→fleet |
| P9b discovery; composing entry points; root up; cycles forbidden | 0 | 0 | none (already empty) | — | lab, env, _shim, __main__, otto, context.state, the six session files | bootstrap→project, testing→bootstrap |

No landing grows the ratchet. With every file owned, the whole-tree cycle goes 24, 24, 18, 15, 15,
15, 12, 8, 7, 6, 0, 0.

**Why P1 and P9 are two landings each** (S5-1 as revised).
- **P1a** is the enforcement alone: the tach switches and the string audit, graph-neutral, on
  today's tree. A reviewer reads the rules before any code moves, and every later landing,
  P1b included, is checked by them. It is Fable's original ask; the shrink-only rule had forbidden
  it. The monitor declarations stay in P1b, because they are the facade rule applied to the module
  P1b moves out of the cycle; declared in P1a, eleven new modules would join the cycle for one
  landing.
- **P1b** has ordered review sections, in this order: (1) the mechanical facade sweep, generated
  by a script committed with the plan (a reviewer re-runs it and diffs to empty), with spec 1
  §9's rule test; (2) the eleven monitor declarations and the fold of `otto.monitor.live`;
  (3) the completion-collector move with its `BASELINE` deletions.
- **P9a and P9b** split the bootstrap work by cohesion (§3.9): P9a moves state down and finishes
  docker and fleet; P9b moves discovery, routes the public entry points, moves the root up and
  forbids cycles. P9b does not change the ratchet, which S5-1 now allows. P9b's plan orders its
  review sections: state ownership and isolation (discovery, fixtures); reader migration;
  public entry routing (§3.12); final declarations and audit retirement.

**Order.** Each landing depends on the ones before it:
- P5 needs P4: `host` leaves the cycle only once `host → docker` is gone. P4 can sit anywhere
  before P5; it stays fourth, next to the phase that depends on it;
- P8's `reservations → host` is legal only after `host` leaves the cycle in P5;
- P7's layering of `otto.labs` needs `host` (P5) and `inventory`, `link`, `models` (P6) out of the cycle;
- P9a needs P8's active run; P9b needs P9a's accessors in their owner, and moves the root up
  last: `otto.bootstrap` can import `otto.project` statically only once nothing below root still
  imports `otto.bootstrap`, which is first true in P9b.

P3 and P4 delete only 2 and 1 ratchet edges. They stay separate because they touch different
subsystems.

**Amended by P5, 2026-10-14.** The real tree confirms row P5: the cycle is 11 modules and 31
in-cycle edges (59 modules, 291 edges, 48 layered), and `BASELINE` lost exactly Appendix A's
thirteen P5 edges. P5's commit is also marked breaking: D2 changes behavior on a taught public
path, and the owner's 2026-10-12 rule marks any behavior change, so P4 is not the only marked
commit.

**Amended by P6, 2026-10-14.** The real tree confirms row P6: the cycle is 7 modules and 16 in-cycle edges (61 modules, 304 edges, 54 layered, §5.2), and `BASELINE` lost exactly Appendix A's fifteen P6 edges. The move alone (before the carve) already took creds, link and models out (8 / 20); the carve took the inventory. P6's commit is not marked.

**Amended by P7, 2026-10-15.** The real tree confirms row P7: the cycle is 6 modules and 12 in-cycle edges (61 modules, 304 edges, 55 layered, §5.2), and `BASELINE` lost exactly Appendix A's four P7 edges. Row P7's 'tach modules declared' is none (§3.7). The order note for P7 now reads: P7's layering of `otto.labs` needs `host` (P5) and `inventory`, `link`, `models` (P6) out of the cycle. P7's commit is not marked.

**Amended by P8, 2026-10-15.** The real tree confirms row P8: the cycle is 5 modules and 9 in-cycle edges (61 modules, 305 edges, 56 layered, §5.2), and `BASELINE` lost exactly Appendix A's three P8 edges. Row P8's 'tach modules declared' is none, and its new edges outside the cycle are `reservations→host` and `reservations→invocation` (§3.8). P9a's `declared→active_run` and `docker→active_run` read `invocation` instead (`declared→invocation` already exists), and P9a gains `lab.fleet→reservations`: the fleet's in-play read moves with `config/fleet.py` (at P8 it is `config→reservations`, downward and outside the cycle). The order note 'P9a needs P8's active run' now means the slot in `otto.invocation`. P8's commit is not marked.

**Amended by P9a, 2026-10-16.** The real tree confirms row P9a: the cycle is 0 modules and 0 in-cycle edges (62 modules, 317 edges, 62 layered, §5.2), and `BASELINE` is empty, having lost exactly Appendix A's nine P9a edges. The graph adds 15 edges (`cli→declared`, `cli→fleet`, `coverage→bootstrap`, `coverage→declared`, `coverage→fleet`, `docker→invocation`, `fleet→config.scope`, `fleet→context`, `fleet→host`, `fleet→reservations`, `monitor→declared`, `monitor→fleet`, `suite→bootstrap`, `suite→declared`, `suite→fleet`) and drops 3 (`config→bootstrap`, `config→context`, `monitor→config`). Row P9a's 'tach modules declared' is `fleet` (D-P9a-2), and its new-edges cell is the 15 above. P9a's commit is not marked.

**Phases re-checked under the revised S5-1.** P1 is now split (above), and P9's split follows
cohesion rather than the shrink. The carve-with-the-fix rule (§5.1) stays, because carving
earlier would only add `BASELINE` entries. P4's place before P5 is a real dependency, not a
shrink artifact. No other phase had been bent to stay shrink-only, so none changes.

## 7. Decisions record and amendments

**Decisions**

| # | decision | where |
|---|---|---|
| S5-1 | One plan and one squashed commit per landing. **Revised by the owner, 2026-10-11:** a landing may grow, hold or shrink the ratchet; any growth is listed in the same commit with its removing phase; the series ends at an empty `BASELINE` with cycles forbidden. | §6 |
| S5-2 | Scope: the cycle plus spec 1's queued cleanups, folded into the phases that touch them. | §3.10 |
| S5-3 | Layers by role; layers declared in tach; cycles forbidden at P9b; the ratchet stays. | §2, §5 |
| S5-4 | **Owner, 2026-10-11:** container auto-start is removed. A stopped declared container fails fast with `ContainerNotRunningError` (a `RuntimeError`) naming `otto docker compose up <project>` and `otto.docker.deploy` / `compose_up`. Breaking, with its own marked commit (P4). | §3.4 |
| D1 | **Mooted by S5-4.** Review had settled keeping auto-start through the public `otto.host` binding; with auto-start removed there is nothing to install. | §3.4 |
| S5-5 | **Owner, 2026-10-11:** no named exceptions. The facade rule (§3.11) and the bootstrap split (§3.9). | §3.9, §3.11, §5.3 |
| S5-6 | **Owner, 2026-10-11, on the S5-5 review:** no public behavior change beyond S5-4 and D2. The draft decision D3 (composing reads raise when the root was never loaded) is withdrawn; the composing entry points load the root when resolved instead. | §3.12 |
| D2 | **Owner, 2026-10-11: (a) admit.** With `otto.bootstrap` never imported, `scope_for_repo` admits. The alternative, a by-name import of `otto.bootstrap` from foundation, would have been an upward exception. | §3.5 |
| D-P2-1 | **Owner, 2026-10-12, re-confirmed the same day:** approve the catalog's exact +4 footprint on `completion_repo_handover` and the 23 named non-protected surfaces; any other growth, or a listed surface that does not grow, stops the landing. | §4.2 |
| D-P6-1 | **Owner, 2026-10-14:** P6 is unmarked; a moved class's `__module__` — and with it the pickle path and repr — is an implementation detail, as in P3. | §3.6, §4.1 |
| D-P7-2 | **Controller, 2026-10-15:** the lab model stays with its loader in `otto.labs.loader`, and the fleet and the compose verbs import it for type checking only: the import-budget rule (fix growth at its source) leaves no growth anywhere. Variants A–D measured. | §3.7, §4.2 |
| D-P7-3 | **Owner, 2026-10-15: unmarked.** `typing.get_type_hints` on 14 functions (5 public: `compose_up`, `compose_down`, `composed`, `get_lab`, `fleet_of_interest`) now needs the type-only `Lab` supplied; runtime hint resolution of these signatures is an implementation detail, like D-P6-1's `__module__`. Precedent: 19 of 45 already raised; #645. | §3.7 |
| D-P8-2 | **Controller, 2026-10-15:** the active-run slot lives in `otto.invocation` and the membership-flag rule in `otto.host.builtin_hosts`; the import-budget rule (fix growth at its source) leaves no growth anywhere. Variants A, C and D measured; B cannot leave the cycle. | §3.8, §4.2 |
| D-P9a-2 | **Controller, 2026-10-16:** the fleet moves to `otto.fleet`; under `otto.lab` it grew 8 surfaces by 12 file operations. | §3.9, §4.2 |
| D-P9a-3 | **Controller, 2026-10-16:** the P9a composing calls import the root on the call; the resolver mechanism grew 5 public names. | §3.12 |

**Amended by P6, 2026-10-14.** D-P6-1 added; P6's settings-type home was chosen by measurement (§3.6, §4.2), not ruled.

**Amended by P7, 2026-10-15.** D-P7-2 and D-P7-3 added. D-P6-1 decided P7's marking of the move (unmarked) without a new question.

**Amended by P8, 2026-10-15.** D-P8-2 added. D-P6-1 decided P8's marking (unmarked, as D-P8-1) without a new question.

**Amended by P9a, 2026-10-16.** D-P9a-2 and D-P9a-3 added. D-P6-1 and D-P7-3 decided P9a's marking (unmarked, as D-P9a-1) without a new question.

**Decided in the design by evidence, not by the owner**
- `CredSpec` lives in `models.base` (§3.3).
- The impairer contract lives in `otto.link.params`, removing the registry-string loop between
  impairer and netem without a new module or a path change (§3.3).
- The composed-repos catalog lives in `otto.declared` rather than a new module, under spec 1's
  no-growth rule (§3.5).
- The settings types' fallback home is `models.base` if the measurement requires it (§4.2).
- `otto.context` becomes a package facade over `otto.context.state`, and `otto.session`'s facade
  its own module, rather than an upward by-name import from the run layer; `otto.suite` and
  `otto.coverage` sit at root (§3.12).
- P1 and P9 land in two commits each (§6).

**Amendments, made in this spec's commits**
- **Spec 2 §4, the `otto.host` row:** auto-start, and with it the host's query to `otto.docker`, is
  removed (P4, S5-4).
- **Spec 2 §4, the `otto.docker` row:** the repos accessor is `otto.declared.current_repos`, not
  a `config.fleet` one (P9a).
- **Spec 2 §4, hand-built contexts:** `ctx.repos` acquires the repos through the composed-repos
  catalog's initiator. Every public route to an `OttoContext` loads the composition root first,
  so a hand-built context composes exactly as today (P9b, §3.12).
- **Spec 4, S-3** ("`otto.config` parses; `otto.bootstrap` composes"): discovery, which parses,
  moves to `otto.config.repo` (P9b).
- **Spec 1 §9:** carried out (P1b, and P9b for `otto.context`); the rule test makes it permanent.
- **Spec 3b §6:** carried out (P3).

## Appendix A: ratchet edges deleted per landing

Each landing's remaining in-cycle edges are a subset of the previous landing's; no landing adds
one. An edge is deleted because an import goes or because one end leaves the cycle.

| landing | edges deleted |
|---|---|
| P1a (0) | — |
| P1b (44) | cli→bootstrap, cli→check, cli→config, cli→context, cli→coverage, cli→docker, cli→host, cli→init, cli→instructions, cli→inventory, cli→link, cli→models, cli→monitor, cli→monitor.live, cli→project, cli→reservations, cli→session, cli→suite, cli→tunnel, config→cli, coverage→config, coverage→context, coverage→host, coverage→models, init→config, init→host, init→inventory, init→labs, init→models, monitor→host, monitor→link, monitor→models, monitor.live→config, monitor.live→context, monitor.live→monitor, monitor.live→tunnel, suite→config, suite→context, suite→coverage, suite→host, suite→models, suite→monitor, suite→monitor.live, suite→project |
| P2 (14) | config→instructions, instructions→context, instructions→project, instructions→session, project→config, project→context, project→host, project→instructions, project→link, project→models, project→tunnel, tunnel→check, tunnel→host, tunnel→models |
| P3 (2) | host→models, models→link |
| P4 (1) | host→docker |
| P5 (13) | bootstrap→host, check→host, config→host, context→host, docker→host, host→config, inventory→host, labs→host, link→check, link→host, models→host, session→host, session→models |
| P6 (15) | config→inventory, config→link, config→models, creds→models, docker→models, inventory→config, inventory→creds, inventory→models, labs→inventory, labs→link, labs→models, link→models, models→config, reservations→models, session→inventory |
| P7 (4) | config→labs, labs→config, session→config, session→labs |
| P8 (3) | config→reservations, context→reservations, reservations→config |
| P9a (9) | bootstrap→config, config→bootstrap, config→context, context→bootstrap, context→config, context→session, docker→config, session→bootstrap, session→docker |
| P9b (0) | — (`BASELINE` already empty) |

**Amended by P9a, 2026-10-16.** Confirmed: `BASELINE` lost exactly these nine; P9a declares `otto.fleet`, not `otto.lab.fleet`.
