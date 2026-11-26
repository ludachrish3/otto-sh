# Configuration-first scope: every surface sees only the active projects

Status: approved, reviewed by Astra and Fable (two rounds each), with the
maintainer's decisions recorded in §9 (2026-11-25). Supersedes the narrower
"`lab_patterns` reaches the lab listing and the test suite" draft, whose
reviewed design is stated in §4.1–§4.3.

## 1. Problem

`[project] lab_patterns` / `host_patterns` and the `-I` / `-E` switches decide
which projects are *active* (`otto.config.scope.active`, the one authority;
resolution order explicit switch > lab inference > default-on). But each
surface opts in on its own, so most never did. A 2026-11-24 sweep of
`origin/main` f3b7bd70c found:

- **Scoped today:** fleet walks (`admissible_ids` → `scoped_ids` →
  `project_targets`), `fleet_of_interest`, `get_hosts_in_play` /
  `otto reservation check`, `ctx.cov` / `otto cov get|clean`, the orchestrator
  verbs (`otto run install|uninstall|cleanup`), `otto monitor --live`, product
  and dev-tool providers, docker facts, and instruction *dispatch*.
- **Unscoped:** every listing (`--list-labs`, `--show-lab`, `--list-hosts`,
  `--list-products|--list-tools`, `otto run --list-instructions`,
  `otto run --help`, `--list-tests`, `--list-markers`), test collection and
  runs, every explicit host lookup (`otto host <id>`, `--hop`, the probe, the
  verb menu, `otto link`, `otto tunnel`, docker parent choice, `otto cov
  report`, `ctx.get_host`), every TAB payload, and every "available / did you
  mean" hint.

With dozens of labs and hundreds of hosts this is clutter, and it is a defect
class: a new surface is unscoped unless its author remembers to call `active()`.
The raw inventories are kept beside this spec's work (not committed).

## 2. Rulings (Chris, 2026-11-24)

- R0. **Down-select early and often.** Project configuration first scopes every
  test, instruction, host and lab a user sees or acts on. Scoping is the
  default; an unscoped surface is a named, documented exception.
- R1. `--list-labs` lists only the labs a project's `lab_patterns` match;
  nothing is shown about the others.
- R2. A declared project matching no listed lab keeps its panel and warns. An
  empty-but-valid panel (its labs come from another project's sources) shows a
  dim line saying so.
- R3. Out-of-scope projects are dropped from `otto test`, `--list-tests` and
  `--list-markers`.
- R4. **Hosts are scoped by active projects, explicit `otto host <id>`
  included** (supersedes `project-scope.md`: "Explicit targeting is not bounded
  by it").
- R5. **TAB stays fast:** completion is scoped on the lab axis only (lab names
  and patterns); TAB never loads a lab, so `host_patterns` do not apply there.
- R6. **Help is decluttered:** an inactive project's instructions do not appear
  in `otto run --help` or the listings (supersedes "every registered
  instruction stays listed so `-I` is discoverable"). Typing one still gets the
  refusal that names `-I`.
- R7. **An out-of-scope project's options do not appear as flags** (Chris,
  2026-11-25). A registered options class whose registering project does not
  participate is not offered, checked or completed on any command; typing its
  flag gets the parser's bare "No such option" (Chris, 2026-11-26): no lookup among
  the filtered classes and no hint naming the project or `-I`. otto's own classes
  and undeclared projects' classes always apply. Built by #1331 stages 2–3 on
  `participating(...)` (§3.3).
- Unchanged: a project with no `[project]` table applies everywhere; with no
  lab selected there is no lab inference, so every project not switched off is
  active.

## 3. The mechanism: one scoped view, two tiers

Surfaces differ only in **what they know when they run**, so the view has two
tiers, and both answer through the same precedence code.

### 3.1 Tier L (lab loaded) — unchanged authority

After `ensure_inline_lab` / the command preamble, `active(name, ctx)` and
`OttoContext.admissible_ids` are the answers (host axis included). Nothing new
is decided here.

### 3.2 Tier P (pre-lab: lab names and switches only)

Help, eager `--list-*` callbacks, `--list-tests` without a lab, and the
cache-writer run before any lab exists. What they know: the discovered
projects, the lab selection (`-l` or `OTTO_LAB`) split into component names,
`-I`, `-E`. The verdict
is already written once — `classify_load_errors.inactive_reason`
(`src/otto/session/projects.py:183`): switched off > forced on > lab-axis
`inactive_before_lab`. That closure is lifted to a public function in
`otto.config.scope`:

```python
def pre_lab_inactive_reason(project, *, labs, include, exclude) -> Literal["excluded", "out_of_scope"] | None
```

`classify_load_errors` calls it; so does everything in tier P. Its precedence
already matches `active()` (both normalise, both apply exclusion before
inclusion); required-dependency fatality stays outside it, in
`classify_load_errors`. Parity tests pin both. A host-starved
project is active in tier P (undetectable before the lab) and becomes inactive
in tier L — documented, and the only difference between the tiers.

### 3.3 One entry for "which projects take part"

```python
def participating(projects, *, ctx=None, selection=None) -> Participation
```

`Participation` holds `kept`, `dropped` (each with its shared reason, §3.4) and
the tier it was decided in. With a context (tier L) it asks `active()`; with
only a `selection` (tier P) it asks `pre_lab_inactive_reason`. The
orchestrator's `_applicable` keep/drop/announce core is rebuilt on it (its
dependency arms stay on top; order, normalised dropped keys and the dependency
refusal under `announce=False` are kept and pinned by tests), and every surface below reads it. Announcing
(WARNING per dropped project, as `otto run install` does today) is the caller's
choice: runs announce, listings and help do not (R1/R6: nothing shown).

### 3.4 One "why inactive" reason

The reason branch in `otto.session.activation.check_instruction_active`
(excluded / out_of_lab_scope / host_starved + facts) is extracted to one
function returning reason + facts. `InstructionInactiveError`, the
orchestrator's skip line, the suite's "not searched" line and the new host
refusal (§4.5) render from it, with CLI spellings (`-I`, `-E`, `-l`).

### 3.5 Targetable hosts

The user-facing host set is a new `OttoContext.targetable_ids()` (named apart
from `otto.fleet.get_hosts_in_play`, which excludes `local`): today's
`admissible_ids(require_nonempty=False)` — live, so a container registered
mid-run is judged when asked — plus the built-in runner, recognised with
`is_builtin_host` (`src/otto/host/builtin_hosts.py:34`), not by the id `local`
(a lab may declare an ordinary host with that id; that one stays scoped). The
existing id-keyed exclusion in scope resolution
(`exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID})`, `run_context.py:903`) moves
to the same built-in identity test, or a lab host named `local` would still be
left out of every universe. With
no declaring project it is the whole lab (the existing fallback).

**Containers follow their parent (Q4).** A container host is admitted exactly
when its parent is, instead of by its own id against `host_patterns`. The rule
lives in `scoped_ids` (the one admission function, `scope.py:860`), so fleet
walks with `include_containers`, the reservation in-play set and
`otto host <container>` all follow it; `get_container_host` resolves through
the same scoped lookup. One rule, every reader.

**Default scoped, raw by name.** `OttoContext.get_host` (and `otto.get_host`,
`otto.lab.get_host`, `ProjectContextView` through it) resolves only targetable
ids. Whole-lab access goes through `ctx.unscoped.lab` (§3.7; the host layer's
`_ContextResolver` reads it), and every site that must stay whole-lab moves
onto it explicitly:

- hops, multi-hop and console servers (`RemoteHost._lab_host` →
  `_ContextResolver`), a hop named with `--hop` (Q1, §3.8),
  and the reservation check of a named out-of-fleet hop;
- the power controller;
- the link self-lockout guard and endpoint check (`link/manage.py:546-592`) and
  `repair_link` under cleanup's impairment reset;
- tunnel discovery and reap, `otto tunnel list|remove` and cleanup's state
  sweeps per Q8 (a user must be able to list and remove what otto created on a
  host that has since left scope), and the monitor's tunnel source
  (`monitor/live.py:162`, today via `fleet.get_lab()`).

Container→parent references are object references and are unaffected.

**Refusal.** An id that resolves in the lab but is not targetable refuses with
the host-level reason that already exists (`OUT_OF_SCOPE_REASON` /
`SWITCHED_OFF_REASON`, `scope.py:748-752`), never a project-inactivity reason —
`-I` cannot widen hosts (`scoped_ids` never reads it; `docs/cli/projects.md`:
"`-I` does not invent a fleet"). The remedy it names is the true one: drop
`-E <p>` when only switched-off projects target the host; otherwise widen
`host_patterns` / `lab_patterns`, naming the declaring projects'
`settings.toml`. When nothing at all is targetable, the existing fleet-shaped
`_EMPTY_FLEET` / `_EXCLUDED_FLEET` text renders instead. The miss stays a
`KeyError` subclass, as today (`run_context.py:734`). An id not in the lab at
all keeps today's "No host with ID" error, its candidate list now targetable
ids only.

### 3.6 TAB (tier P from the cache)

- **Writer.** The completion cache's writers merge into one
  (`cache_sections._collect_names` and the copy in `cli/main.py:1311`). Per
  project it stores `lab_patterns` (or "undeclared") and its root; payloads with
  owners keep **contributor sets** instead of flattening: instructions (otto
  body or contributing projects, from each `ProjectInstructionBody.project`),
  the serialized command tree's instruction nodes, test/marker tables (already
  per project root), docker use-cases/images/services (several contributors).
- **Shim.** The root `-I` / `-E` values are carried through every descent the
  way `root_lab_values` is (comma split, normalised, repeats accumulated); today
  each `Resolution` starts with an empty option map. The shim imports only the
  stdlib, so the tier-P predicate lives in a stdlib-only leaf module that both
  `otto.config.scope` and the shim import, pinned by a parity test.
- **Answers.** Filter by contributor: an entry shows when otto contributes it or
  a participating project does. Hosts stay per-lab buckets (R5). `-l` offers the
  labs some project's patterns match (undeclared projects contribute all their
  labs). No new file operations; the filtering is linear in the cached payload.
- **Measured, not assumed.** Every warm TAB surface already has a file-op
  ceiling per minor (PR #1301, #1257; surface table in
  `docs/architecture/subsystems/completion.md`). "No new file operations" is
  proved there: the eight gated surfaces keep their ceilings (they only go
  down), cached facts are seeded through the surviving writer once the two are
  merged, and contributor filtering adds no new dispatch path (else the
  harness's site map gains a surface). `make import-snapshot ONLY=<keys>`
  re-measures the touched surfaces.

### 3.7 Scoped by default at registration; opt out by name (Q7, approved)

**Ruling (Chris, 2026-11-25):** otto's first-party verbs use the same
infrastructure third-party plugins use. Verb registration defaults to scoping,
so every verb — otto's and a plugin's — is scoped without knowing it; a
mechanism disables scoping strictly in the verbs and locations that need the
whole universe of projects and labs.

**Verb level — a third dispatch switch beside `lab_free` and `gate`.** The
existing per-verb switches are a `CommandSpec` field set at
`register_cli_command` / `cli_command` plus a per-leaf callback attribute,
combined by one reader (`leaf_is_lab_free`, `leaf_is_gated`,
`src/otto/cli/invoke.py:1485-1512`). Scoping is the same shape:

- `CommandSpec.scoped: bool = True`, settable at registration
  (`register_cli_command(..., scoped=False)`), and `SCOPED_ATTR =
  "__cli_scoped__"` for one leaf under a scoped group;
- `leaf_is_scoped(spec, callback)` is the one reader.

All 15 built-in registrations (`src/otto/cli/builtin_commands.py`) and every
plugin registration are scoped by omission.

**Where it is installed — a context field, default on.** Contexts are built in
more places than the preamble: `ensure_inline_lab` (root flags), lab-free verbs
that open their own (`otto monitor --live`, `otto reservation check`),
`open_context`, and the test runner's `_session_context`. So `scoped` is a field
of `OttoContext` defaulting to `True`, set by the one context builder
(`ensure_lab_context` and `open_context` share it). `leaf_is_scoped` is resolved
once into the invocation state before any context opens and passed through;
the leaf's verdict is the only way to get `False`. A reused (inherited) context
keeps its own value. A context with no lab is scoped on the project axis only
(tier P, §3.2); scoping never forces a lab load.

**The core never reads the views.** Scope resolution (`scopes_of`,
`resolve_scopes`), admission (`admissible_ids` → `scoped_ids`) and load-error
classification read only the unscoped lab and the full bootstrap snapshot. The
public views consume their answers. This makes the definitions one-way: the
scoped project list derives from the unscoped one, and `targetable_ids` derives
from the unscoped lab, so nothing recurses.

**What a scoped verb's code reads.** The public objects are the scoped ones; the
body never filters:

- `ctx.lab` / `otto.get_lab()` return a **scoped lab view** (`ScopedLab`):
  - reads: `hosts` is a read-only `Mapping` of the targetable ids (computed
    live from the canonical lab, so a container registered mid-run is judged
    when read), holding the canonical host objects themselves (backlinks
    unchanged); `links` and `static_links()` are the canonical ones with both
    endpoints targetable, full addressing kept; `name`, `component_names`,
    `resources`, `metadata` are forwarded unfiltered (a merged `a+b` keeps its
    name);
  - writes go to the one canonical mutable `Lab`: `add_host`, removal,
    replacement and rollback (docker's `compose.py:610-616,916,1105,1168`), and
    `host._lab` always points at the canonical lab;
  - `lab + other` and `dataclasses.replace` on a view raise, naming
    `ctx.unscoped.lab`;
  - typing: `Lab` is `@final` and `open_context` branches on
    `isinstance(lab, Lab)` (`run_context.py:1539`), so the view is not a `Lab`
    subclass. A `LabView` protocol both satisfy becomes the public type of
    `ctx.lab` / `get_lab()`; internal `isinstance(…, Lab)` sites read
    `ctx.unscoped.lab`. `open_context(lab=…)` and `fleet_of_interest(lab, …)`
    accept a `LabView` and unwrap a `ScopedLab` to its canonical lab (today a
    non-`Lab`, non-`str` value falls into `open_context`'s list branch,
    `run_context.py:1539-1545`). Type change → `!` (§7).
- `ctx.get_host` / `otto.get_host` resolve targetable ids only (§3.5);
- `ctx.projects` (and `current_projects()`, through it) is the participating
  projects, in discovery order. With no run installed `current_projects()`
  falls back to the composition (`declared.py:665`): that is tier P with no lab
  and no switches, so every project;
- `all_hosts` / `run_on_all_hosts` / `do_for_all_hosts` are scoped as today.

In an unscoped verb the same names return the canonical lab and every project.

**Empty scope refuses.** "Empty" is `admissible_ids(require_nonempty=False)`
with the built-in runner left out (`targetable_ids` always holds the runner, so
it cannot be the test), while some project declares a scope. A scoped, gated,
lab-bound verb then refuses in the preamble with the existing `_EMPTY_FLEET` /
`_EXCLUDED_FLEET` text, rather than letting `for h in get_lab().hosts.values()`
silently do nothing (the failure `get_hosts_in_play`'s docstring says the walk
refusal exists to prevent). Not refused: leaves that touch no host
(`gate=False` or `__cli_gate__ = False`: `cov report`, `docker use-cases`),
`otto reservation check` (an empty in-play set is its legal answer,
`cli/reservation.py:127-133`, `admissible_ids(require_nonempty=False)`), and
an explicit target that is the built-in runner (`otto host local …`). The root
flags `--list-hosts` / `--show-lab` print the same text as a warning and list
only the runner.

**Location level — one opt-out namespace.** `ctx.unscoped.lab` and
`ctx.unscoped.projects` are the only whole-universe reads (one token to grep,
one guard pattern). They are public: a plugin teardown verb or a custom host
class resolving its console server may legitimately need them. The guard test
(§6) covers otto's own `src/`: it allowlists the modules that may read
`ctx.unscoped`, the module-level `otto.bootstrap.get_projects` /
`get_ordered_projects`, and `Lab.hosts` on a canonical lab, and fails on any new
reader. `otto.bootstrap.get_projects` is documented as whole-universe, and the
plugin docs that teach it (`docs/extend/python-library.md:582,603`,
`docs/configuration/settings.md:476`) move to `ctx.projects`.

Internal readers, classified before the property changes (each named in the
allowlist or moved):

- **unscoped (`ctx.unscoped`)** — scope resolution and admission (above);
  `classify_load_errors` and the dependency closure; the orchestrator's
  `_applicable` input (its dropped set finds missing providers,
  `orchestrator.py:323,346`); the driving-project lookups
  (`orchestrator.py:169`, `monitor/live.py:120`); coverage configuration
  (`coverage_settings.py:51`, `run_context.py:678`, `coverage/get.py:136`,
  `coverage/instrumentation.py:142`, `coverage/collect.py:167,407`,
  `suite/run.py:511,1859`) and the suite's configuration and diagnostics;
  docker fact lookup (`docker/resolve.py:384` → `declared.py:842`); the
  completion-cache writer and its background refresh (`cli/main.py:1311`,
  `cache_sections.py:193`), which must record every project whatever `-l` the
  refreshing command had; hop, console-server and power-controller resolution
  (`_ContextResolver.hosts`, `run_context.py:193`); named-host reservation
  reporting (`cli/host.py:203` → `reservations/check.py:467`); the link
  self-lockout guard and cleanup's impairment reset (`orchestrator.py:796`);
  tunnel reap/discovery and the monitor tunnel source;
- **scoped (default)** — test collection (participants), docker fragment
  selection (`docker/deployment.py:622`), `cov report` product lookup
  (`coverage/reporter.py:907`), and every listing and resolution in §4.

**Instructions and root verbs.** Instructions already record their owner; a root
CLI verb records the project whose init import registered it (first-party verbs
have no owner). With a lab selected, a verb or instruction of a
non-participating project is hidden from help and TAB and refuses on dispatch
with the shared reason (§3.4) — unless an active project requires that project,
the rule load-error classification already applies (`_required_by`,
`session/projects.py:205`). Hiding a plugin's root verb changes what runs → `!`.

**Verbs that opt out** (each named at its registration or leaf): `otto init`
(the doctor checks every source), `otto lab render` / `otto lab explain`
(diagnose sources, `cli/lab.py:49,108`), `otto env`, `otto cache`. State otto
created is handled by Q8. Monitor review reads a recorded file and opens no
lab: unaffected. `otto inventory` reads `get_projects()` directly
(`cli/inventory.py:141,143`) although its registration promises the active
projects' `[inventory]` tables; it moves to `participating` (tier P), so it
honours `-l` and `-E` like every other verb. Root flags are
not verbs: `--list-labs` applies its own per-project lab filter (§4.1), and
`--show-lab` / `--list-hosts` read the scoped context `ensure_inline_lab`
installs.

**Hooks.** A session-setup or login-proxy hook can run for an out-of-scope hop
(§3.8). `SetupContext` carries ids only (`session_setup.py:59-66`), so it leaks
nothing; a hook that needs its own host object reads
`ctx.unscoped.lab.hosts[setup.host_id]`, and the hook docs say so.

**By design, stated in the docs:** `otto.load_lab()` returns the lab it was
asked to build; a container's parent object reference, hop ids and link
endpoint ids are plain attributes of host objects; `RemoteHost._lab_host`, which
custom host classes inherit, resolves through the unscoped resolver.

**Plugin-author docs.** `docs/extend/extending-cli.md` (beside `__cli_gate__`)
documents `scoped` / `__cli_scoped__` and leads with one sentence:
"Everything otto hands your verb — `ctx.lab`, `get_host`, `ctx.projects`, the
walks — is already narrowed to this run's projects and hosts; you never
filter." The rule's one home stays `docs/configuration/lab/project-scope.md`.
`docs/extend/cli-exposed-verbs.md:259` uses "unscoped" for the provider gate's
admitted cases; that wording is renamed. `docs/extend/network-api.md:273`'s
`get_lab()` example is re-read.

### 3.8 Infrastructure hosts (Q1, approved)

Every host joins labs only through its element's `labs` patterns (required,
non-empty; "every lab" is spelled `[".*"]` on the element), and a hop must be
in the loaded lab or resolution raises (`remote_host.py:686`). So shared
infrastructure is already declared by each lab that uses it; nothing is global.
The project axis is different: `host_patterns` is also the fleet walk, so
requiring a jump host there would make `install`/`cleanup`/`cov` act on the
bastion. Rule: a host reached as a hop (lab-declared, or named with `--hop`)
needs only lab membership; as a target it is scoped like any host.

Concretely: `otto host`'s `_resolve_host` splits into the scoped target lookup
and one whole-lab hop lookup, which `otto host --hop` and remote-path
completion (`cli/remote_completion.py:310,312`) both call; named-host
reservation reporting receives `ctx.unscoped.lab` (`cli/host.py:203`,
`reservations/check.py:467`); a hop's hooks follow §3.7 "Hooks".

## 4. Surfaces

| # | Surface | Tier | Change |
|---|---|---|---|
| 4.1 | `--list-labs` | per-project lab axis | R1/R2 as reviewed (see below) |
| 4.2 | `otto test`, `--list-tests`, dry-run | L with a lab, P without | as reviewed (see below) |
| 4.3 | `--list-markers` | L / P | same `participating` as 4.2; no inactive table read or collected |
| 4.4 | `otto run --help` (slow and cached fast path), `otto run <TAB>`, `--list-instructions` | L / P | an instruction shows when otto contributes a body or a participating project does; a project-only verb with no participating contributor is hidden. Typing it still reaches dispatch and its refusal |
| 4.5 | `otto host <id>`, `--hop`, probe, verb menu, remote-path completion target | L | resolve through the scoped `get_host` (§3.5); `--hop` per Q1; remote-path completion builds its lookup context with the switches |
| 4.6 | `--list-hosts` (both), `--show-lab` (hosts, and links only when both endpoints are targetable), "Available hosts" hints, `ctx.get_host` "Available:" | L | targetable ids only; the hop resolver's own hint stays whole-lab |
| 4.7 | `ctx.get_host`, `otto.get_host`, `otto.lab.get_host` (library) | L | same scoped resolution (one function); `ProjectContextView`'s "deliberately NOT narrowed" note is rewritten; release note names the migration (add the id to `host_patterns`) |
| 4.8 | `otto link`, `otto tunnel add|check` user-named endpoints | L | scoped resolution; guards, reaps and `tunnel list|remove` stay whole-lab (§3.5) |
| 4.9 | docker parents: `--parent`, the default parent, the `--parent`-less fan-out, placeholder registration | L | one candidate rule inside `_capable` / `default_docker_parent` (`docker/observe.py:39-80`), so every caller agrees; refusal "no docker-capable host in scope; capable but outside: [ids]; widen host_patterns in <file>" (Q5). `get_container_host` follows Q4 (§3.5) |
| 4.10 | `otto cov report` product definitions | L | targetable ids only |
| 4.11 | `--list-products` / `--list-tools` | L / P | `participating` instead of `surviving_projects()` (honours `-E` / `-I`); with `-l`, `lab_rows` and `_unused_providers` also filter rows by owning participant and displayed hosts by targetable ids |
| 4.12 | TAB: `-l`, instructions, test names / `-m`, docker names and `--project`, host ids, docker `--parent` | P (cache) | §3.6 |
| 4.13 | did-you-mean hints (instructions, tests, hosts, links) | the surface's tier | suggest only from the scoped set |
| — | Unchanged on purpose | — | `-I`/`-E` completion and did-you-mean (must list every project); the `otto init` doctor; `otto monitor` review mode (reads a recorded file); everything in §3.5's internal list |

### 4.1–4.3 (reviewed slice, unchanged in substance)

Reviewed twice by Astra and Fable and approved by Chris; the only change since
is that the test filter is now `participating` (§3.3).

**`--list-labs` (4.1).** `lab_patterns` are matched in one place,
`otto.config.scope.applies_to_lab`, with `applies_to_any` over a set of lab
names beside it. A project's panel (`_lab_rows`) keeps a lab row, available
or not, only when its scope applies to that lab; a `⚠ source X unavailable`
row describes a source, not a lab, and always stays. R2 is judged over the
whole listing (every lab any project's sources declare), not per panel:

- A declared scope that applies to none of the listed labs gets one yellow
  row, `⚠ no lab listed: lab_patterns (<patterns>) in this project's
  .otto/settings.toml match no lab any source declares`. The row is
  suppressed unless absence is provable: no row while any source is
  unavailable, and none on the per-project fallback (the sources could not
  be listed together, so each panel reads its own project's report), because
  the missing lab could be one that failed to load.
- A declared scope with no row of its own that applies to a lab another
  project's sources declare gets the dim line `its labs come from another
  project's lab sources`.
- An undeclared project gets neither.

**`otto test`, `--list-tests`, `--list-markers` (4.2, 4.3).** The projects
are narrowed before `_read_known`, so a project that does not take part is
never read or collected; `multi` (the `junit_<project>.xml` layout) is judged
over every project with tests before the filter, so dropping one never flips a
layout. Each project left out gets one "not searched" line naming why and the
`-I` / `-E` fix. A run with no participating project raises
`NoTestsMatchedError`, carrying those lines, ahead of the coverage scan.
`Listing` carries the participating projects. `--list-tests -l` loads the lab
through `ensure_inline_lab`; without `-l`, the factored lab-less context
carries the `-I` / `-E` switches.

## 5. Docs

`docs/configuration/lab/project-scope.md` becomes the one home of the rule
(R0, the two tiers, the internal-resolution exceptions); every surface's page
links it rather than restating it. Rewrite "Explicit targeting is not bounded by
it". Update `docs/cli/projects.md` (the enforcement list becomes "every
surface", with the exceptions table), the test, host, run and completion pages,
and the `otto test` / `otto host` help.

## 6. Tests

- Parity: `participating` drives tier L and tier P over the same inputs; they
  agree everywhere except the host-starved case.
- One driver per surface row in §4 with an in-scope and an out-of-scope project
  over a two-lab, hostless (TEST-NET) workspace.
- Internal resolution: a hop to an out-of-scope jump host still connects (unit,
  resolver-level); the built-in runner always resolves; a lab host named
  `local` is scoped.
- Guard: an AST test (modelled on `tests/unit/test_match_key_guards.py`)
  allowlists the modules that may read the whole-universe accessors: the
  `unscoped` token (`ctx.unscoped`), the module-level
  `otto.bootstrap.get_projects` / `get_ordered_projects` / `compose()`, and a
  named set of loader-internal modules that build and mutate the canonical
  `Lab`. (An AST cannot tell `.hosts` on a canonical lab from the same read on a
  view, so the guard keys on those tokens and modules, not on `.hosts`.) A new
  reader fails until it uses the scoped entry or joins the allowlist — R0's
  "named exception", executable.
- Install point: a scoped verb, an unscoped verb, `ensure_inline_lab`,
  `open_context`, `otto monitor --live` and `run_tests` all see the same
  `scoped` answer; no recursion between the views and scope resolution.
- `ScopedLab`: writes reach the canonical lab, backlinks stay canonical,
  `static_links()` filtered on both endpoints, `+` / `replace` refused.
- Docker: the default parent with scoping, including a tie that collapses.
- Shim: repeated and nested `-I` / `-E`; the stdlib leaf predicate equals
  `pre_lab_inactive_reason`.
- TAB: shim answers filtered by `-l` / `-I` / `-E` from the cache, with the
  existing O(1) file-op budget unchanged.

## 7. Breaking

`!` on the commits that change what runs or resolves: tests (4.2), host
resolution (4.5–4.8, including library `ctx.get_host`; the module docstring
promise that "a scoping typo never bricks an explicit `otto host`" is
reversed), docker parents (4.9, the default parent can change), the public type of
`ctx.lab` / `get_lab()` (`LabView`), `ctx.projects` narrowing, and hiding a
plugin's root verb when its project does not participate. Listings, help and TAB narrowing
carry no `!` (nothing a user has to change).

## 8. Shape

Umbrella issue (R0) plus one issue per phase; one PR per phase, each landing on
the previous:

1. Core (§3.2–3.4) + project-list surfaces: 4.1–4.3, `--list-instructions`,
   4.11, 4.13 (tests). Carries this spec. `otto run --help` and TAB stay as
   today in this phase: scoping the slow help path alone would give one command
   two answers depending on the cache.
2. Hosts: §3.5, 4.5–4.10, 4.13 (hosts, links), 4.11's displayed-host filter
   (it needs `targetable_ids`; phase 1 filters 4.11 by owning project only),
   §3.7's registration switch (`CommandSpec.scoped`, `__cli_scoped__`,
   `leaf_is_scoped`) with the scoped views and named opt-outs, and the guard test.
3. Cache and TAB: §3.6, 4.4 (both help paths together), 4.12, 4.13
   (instructions). The cache's flags (#1331 stage 2's TAB half) follow
   `participating(...).kept` alone: no required-dependency edges are cached, so
   a required project that does not participate offers no flags.

## 9. Decisions (Chris, 2026-11-25)

Approved as proposed: Q2 (configuration-only remedy), Q3 (one rule for mixed
workspaces), Q4 (a container is targetable when its parent is), Q5 (docker
parents scoped through one candidate rule), Q6 (dim no-lab line on `--list-*`).
Q1 approved as §3.8 (a hop needs only lab membership; as a target it is
scoped). Q7 approved and extended as §3.7: registration defaults to scoping for
first-party and third-party verbs alike, with named opt-outs.

- Q8 (approved 2026-11-25). **State otto created**, narrowed to what otto can find without a
  placement record (the #836–#838 ruling: no placement record). Tunnels
  (`tunnel/records.py` plus live discovery) and link impairments (`repair_all`
  finds otto's own qdisc sentinel on every static link) are found lab-wide, so
  `otto tunnel list|remove`, `otto link` repair and cleanup's impairment reset
  and tunnel reap read `ctx.unscoped.lab`: a trace on a host that has since
  left scope is still listed and removed. Coverage counters, staged products and
  dev tools are found from today's host and product definitions, so `cov clean`
  and `run cleanup`'s product/coverage steps stay scoped; a host that left scope
  is cleaned by selecting a scope that includes it.

## 9a. Questions as asked

- Q1. **`--hop` naming an out-of-scope jump host:** reachable (a hop is a path,
  not a target; the lab-declared form already bypasses) — both reviewers agree.
- Q2. **No per-invocation escape** for an out-of-scope host: `-I` cannot widen
  hosts, so the only remedies are dropping `-E` or editing `host_patterns`. Is
  that intended ("configuration first"), or should `-I <p>` also add `<p>`'s
  hosts?
- Q3. **Mixed workspace:** one declared project already narrows fleet walks for
  every project, undeclared ones included ("no project declared" is the
  fallback condition, not "this project did not"). Under R4 that now narrows
  `otto host` too. Keep that one rule?
- Q4. **Containers:** admission is by the container's own id against
  `host_patterns` (`scope.py:978`), so a container `otto docker` just created
  is refused unless its id matches. Proposed: a container is targetable when
  its parent is (containers are leaves of their parent).
- Q5. **Docker parents** are scoped through one candidate rule (both
  reviewers); the default parent can change when scoping breaks a tie.
- Q6. **No lab selected:** every project not switched off is shown (both
  reviewers). Add one dim stderr line on `--list-*` only — "no lab selected,
  showing every project; -l <lab> narrows"?
