# Lab definition v2 — labs, elements, metadata, and the strictness posture

**Date:** 2026-08-27
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** nothing
**Depended on by:** `2026-08-27-getting-started-docs-overhaul-design.md` (the worked
example documents this shape; docs work on host-entry syntax holds until this lands)

## 1. Goal

Stabilize the lab-definition interface before otto's first users harden around it.
Four moves, taken together in one breaking change while breaking is still cheap:

1. Make **labs first-class**: a `labs` section declares every lab by name and
   carries what belongs to the lab as a whole — its reservable `resources` and
   its `metadata`. The lab is the reservable thing; hosts are portions of it.
2. Introduce an **elements** layer above hosts, giving `element` / `element_id`
   a structural home. The element is the smallest unit assignable to a lab, and
   it joins labs by **pattern**, so one element can serve a whole family of
   labs named by a scheme.
3. Give users a sanctioned, collision-proof home for custom data — an opaque
   `metadata` table at lab, element, and host level.
4. Keep `extra="forbid"` everywhere. The typo-naming validation error is a
   getting-started feature, not friction; `metadata` is the extensibility valve.

### In scope, beyond the code

- `guide/configuration/lab-config.md` (the canonical field reference) and
  `guide/configuration/host-sources.md` are rewritten for the v2 shape, the
  `labs` / element / `metadata` fields, pattern membership, and the
  element-wholesale composite rule. `lab-config.md` carries the migration note
  that the §11 error links. The reservations guide is updated for lab-level
  resources. Other pages that show the old shape (`library/lab-source-backends.md`,
  `library/cli-exposed-verbs.md`, the current `getting-started.md` fragments)
  are updated to parse — the full Getting Started rework is the dependent spec.
- `otto init` scaffolds v2: a declared example lab with resources, the
  `EXAMPLE_HOST_ENTRY` wrapped in an element entry, and `LAB_README_TEMPLATE`
  walking the shape and pointing at the reference for the full field list
  instead of carrying a second, drifting list.
- Test fixtures under `tests/_fixtures/lab_data/` migrate to v2 by hand (the
  §11 error is the only "tool").

### Out of scope

- `otto host probe` enhancements (port recon, protocol guessing) — separate effort.
- Per-OS-profile monitor-metric defaults (the BusyBox socket mismatch) — an
  `[os_profiles]` defaults-data change, rides the probe workstream.
- Monitor stat down-selection (`--monitor-stats`) — explicitly deferred in the todo.
- Reservation-scheduler awareness of overlapping labs (§8.3 explains what is
  deferred and what covers the gap meanwhile).
- Any change to host *runtime* identity semantics: `make_host_id()`, the
  `slug()` stability contract, `logical_indices()`, and link-id derivation are
  untouched by construction.

## 2. The v2 file shape

Top-level sections: `labs`, `elements`, `links` (each optional per file;
`$schema` and `_`-prefixed keys remain comment space). Top-level `hosts` is
**gone** (§11).

```json
{
    "$schema": "../.otto/schemas/lab.schema.json",
    "labs": {
        "unix": {
            "resources": ["unix-bed"],
            "metadata": {"description": "unix regression bed"}
        },
        "busybox": {"resources": ["unix-bed"]},
        "embedded": {"resources": ["embedded-bed"]}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix", "busybox"],
            "metadata": {"rack": "B4"},
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "os_type": "unix",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "metadata": {"owner": "infra"}
                }
            ]
        },
        {
            "name": "dut",
            "id": 3,
            "labs": ["embedded(\\..*)?"],
            "hosts": [
                {"ip": "10.0.0.5", "board": "cpu",
                 "creds": [{"login": "admin", "password": "admin"}]},
                {"ip": "10.0.0.6", "board": "mgmt",
                 "creds": [{"login": "root", "password": "root"}]}
            ]
        }
    ],
    "links": []
}
```

### 2.1 The `labs` section — the registry of labs

Keyed by lab name. **A lab exists if and only if some source declares it here.**
An entry holds `resources` (optional list of reservation identifiers, free
strings matched byte-for-byte by the reservation backend, as today) and
`metadata` (optional, opaque object); a strict spec, so future lab-level fields
have a home waiting. An entry in a file that contains none of the lab's
elements is legal — another source may supply them.

Declaration is what makes pattern membership (§7) sound: with patterns, the set
of lab names is no longer derivable from the elements, so `list_labs()` reads
the declared names (union across sources) and `load_lab(name)` requires *name*
to be declared somewhere before it looks for members.

### 2.2 The element entry

Holds exactly: `name` (required; same slug-nonempty rule as today's `element`),
`id` (optional, **int** ≥ 0; today's `element_id` — it is the *element's* id,
and the host's derived string `id` is composed from it, a distinction the docs
state once), `labs` (required, non-empty list of membership patterns, §7),
`metadata` (optional, opaque object), `hosts` (required, non-empty).

Identity, membership, and metadata only — **no** operational fields, no
inheritance. Hosts inheriting element-level defaults (creds, hop, …) was
considered and rejected for now: it adds an element→host merge layer on top of
the existing profile→host→preference merge, and it can be added later without
breaking this shape.

The element is the **smallest unit assignable to a lab**: every host of an
element is in every lab the element is in. A chassis whose boards must belong
to *different* labs is modeled as separate elements (distinct names); `os_type`
stays per-host regardless, so a Zephyr board and its unix mgmt host may share
an element when they share membership.

### 2.3 Host entries

Lose `element`, `element_id`, `labs`, and `resources` — all four now live
above the host, and with `extra="forbid"` a straggler is a loud error naming
the key. Hosts gain `metadata` (optional, opaque object). Everything
operational stays exactly where it is.

### 2.4 A lab is composed from many files

Hundreds of elements do not belong in one file. Every file a json source
names is a complete v2 document that may carry **any subset** of the three
sections, and one source composes all of its files by union: the `labs`
table is the union of every file's table, `elements` is the concatenation,
`links` likewise. An element in one file joins a lab declared in another;
a `labs`-only file is a fine place for the declarations of a whole site.

Naming the files: a json source's `paths` entry is today a directory
(contributing its `lab.json`) or a `.json` file. It gains **globs**: an entry
containing `*`, `?`, or `[` is expanded relative to the repo root — sorted,
files only, `.json` only — so `paths = ["lab_data/labs.json",
"lab_data/elements/*.json", "lab_data/sites/**/*.json"]` splits a lab by
element, by site, or however the maintainer likes. A glob that matches
nothing contributes nothing (like today's absent file). `otto init`
scaffolds a single `lab.json`; the docs show the split layout.

Within ONE source, a duplicate is a typo, never an override (the rule
`host-sources.md` already states for host ids): the same element `(name,
id)` in two files of a source, or the same lab declared in two files of a
source, fails naming both files. Override semantics remain the opt-in of a
second `[[lab.sources]]` entry (§6).

The single home of "which files does a source read" stays
`CompiledLabSource.lab_files()`; the json backend, the completion-cache
fingerprint, and the `otto init` doctor all read through it, so glob
expansion happens in exactly one place.

## 3. Boundary-only flattening

The change lives at the parse/spec layer. The loader flattens each element and
its lab onto the child hosts before host-spec validation:

- `element.name` → the host's `element` constructor argument
- `element.id` → `element_id`
- `element.metadata` → `element_metadata` (a **copy per host**, same
  mutation-isolation idiom as the old `resources` — two hosts of one element
  must not share a mutable dict)
- the resolved lab → `lab_info` (§4), alongside the existing `source_lab`
  stamp the factory already applies

Downstream, nothing moves: host ids, `slug()`, `logical_indices()` ordering,
link derivation, project scoping patterns (which key on `source_lab` + host
id), `HostSummary`, and the conformance harness all see the values they see
today. Custom `LabRepository` backends are unaffected in protocol — they
traffic in constructed `Lab`s — though a backend that populated
`lab.resources` from hosts now populates it directly (the example backend is
updated).

Touch points:

- `parse_lab_sections` / `_LAB_SECTIONS` (`labs/json_repository.py`): sections
  become `{"labs", "elements", "links"}`; `hosts` triggers the migration error (§11).
- `JsonFileLabRepository.load_lab`: requires the lab declared (§2.1), selects
  elements whose patterns match the name (§7), flattens, builds. `list_labs`
  reads declarations. `list_host_summaries` walks elements→hosts and evaluates
  patterns against the declared names.
- `models/host.py`: `HostSpec` drops `element` / `element_id` / `labs` /
  `resources`, gains `metadata: dict[str, Any]`. New boundary models
  `ElementSpec` (name / id / labs / metadata / hosts) and `LabEntrySpec`
  (resources / metadata), both on `OttoModel`, both stripping `_`-comment keys.
- Runtime dataclasses (`RemoteHost` contract, `UnixHost`, `EmbeddedHost`): drop
  `resources`; gain `metadata: dict`, `element_metadata: dict`, and
  `lab_info: LabInfo`. `element` / `element_id` / `element_metadata` /
  `lab_info` join the drift guard's `_NON_SPEC_RUNTIME_FIELDS` — stamped by the
  layers above, no longer spec fields. `DockerContainerHost` stops inheriting
  `resources` from its parent (there is nothing to inherit).
- `config/lab.py`: `Lab` gains `metadata: dict[str, dict[str, Any]]` keyed by
  lab name; `Lab.resources` is now *declared* (from the `labs` entry), not
  derived from hosts. `a+b` composite loads union both — key-disjoint by
  construction, since `+` merges *different* labs.
- `reservations/check.py`: `required_resources(lab)` becomes `lab.resources`
  alone (§8).
- `labs/composite.py`: `_winning_resources` / `_lab_level_extras` are deleted —
  resources come from the winning `labs` entry, nothing to recompute.
- Schema generation (`models/jsonschema.py`): `labs` section schema; element
  wrapper schema wraps the existing per-family host `anyOf`; host sub-schemas
  drop the four hoisted fields and add `metadata`.

## 4. Metadata and lab context on the host

`metadata` is **opaque to otto** at every level: a JSON object, validated only
as `dict[str, Any]`, never interpreted, never merged field-wise. Surfacing:

| Level   | Declared on           | Read as                                       |
| ------- | --------------------- | --------------------------------------------- |
| host    | host entry            | `host.metadata`                               |
| element | element entry         | `host.element_metadata` (copy per host)       |
| lab     | `labs` section entry  | `lab.metadata[<lab name>]`; `host.lab_info`   |

**The resolved lab rides on every host.** `host.source_lab` (the component lab
name, already stamped by the factory and already the scoping key) stays.
Alongside it, `host.lab_info` is a small frozen `LabInfo(name, resources,
metadata)` record — one structured attribute rather than three parallel ones,
so a future lab-level field reaches hosts without growing the host contract.
This is what makes a host self-describing when it is handed to code in
isolation or iterated in a fleet walk, without a trip back to the `OttoContext`
lab. For `a+b` loads `lab_info` names the *component* lab, matching
`source_lab`'s existing rule.

This resolves the strictness-vs-extensibility tension permanently: user data
lives in a namespace that can never collide with a field otto adds later, so
`extra="forbid"` costs users nothing. `products` / `dev_tools` remain
code-attached, never lab data. The `_`-comment idiom remains unchanged
(stripped, not stored) — comments and data are different things.

## 5. Strictness posture (decision)

`extra="forbid"` stays on every lab-data boundary model. Rationale, recorded so
the question stays settled: the alternative (`extra="allow"`, collect unknowns
into metadata implicitly) silently eats typos — `applet_scpp` would vanish into
metadata instead of erroring with the key name. The user friction that prompted
this reconsideration was *under-documentation* of valid fields, which the docs
overhaul and generated snippets address; weakening validation would have made
that same friction worse, not better.

## 6. Multi-source composition

The existing philosophy — later source wins **wholesale at record granularity,
with a warning naming both sources; never a field-level blend** — extends to the
new levels. One rule everywhere; nothing partial ever composes silently:

- **Unit of host replacement becomes the element.** On an element `(name, id)`
  collision for the same lab across sources, the later source's element wins
  wholesale: hosts, membership patterns, and element metadata together.
  Overriding one board of a four-board chassis from a local source means
  restating the whole element entry — accepted cost; in exchange, a hybrid
  element (this source's hosts, that source's metadata) is unrepresentable.
  *Implementation limit (2026-08-28, ruling R19):* replacement happens per lab
  load, so it covers exactly the labs **both** elements match. An override
  that drops a membership pattern cannot remove the element from a lab the
  earlier source's element still matches — the source protocol exposes
  elements only through `load_lab(name)`. To take an element out of a lab,
  change it at the source that declares it. A `list_elements()`-style protocol
  hook that would let the composite evict by element key is a follow-up.
- **`labs` entries replace wholesale per lab name** (resources and metadata
  together), same warning shape.
- `Lab.__add__` (merging *different* labs) keeps its own rule set, per the
  existing "the two rule sets must never blend" doctrine; its metadata and
  resources unions are key-disjoint / set-union by construction.

## 7. Membership by pattern, and sub-labs

An element's `labs` is a non-empty list of regular expressions; the element is
a member of lab *N* when any pattern **fullmatches** *N* — the same
`re.fullmatch` rule `[project] lab_patterns` / `host_patterns` already use, so
users learn one matching rule. A bare name is a pattern that matches itself
(`"unix"`); `".*"` includes the element in every declared lab; `"unix(\\..*)?"`
includes it in `unix` and every dotted sub-lab of it. The docs note that `.`
is a metacharacter (a lab named `lab.1` is matched by `lab\\.1`).

Because membership is computed against the *requested* name, per-source
loading stays simple: `load_lab("unix")` in one source is "elements whose
patterns match `unix`", with no need to see other sources' declarations. Only
enumeration (`list_labs`) and existence (§2.1) need the declared set.

### 7.1 Sub-labs fall out; nothing new is invented

The dot-notation idea evolves naturally from these two choices rather than
being forced: a **sub-lab is just another declared lab** whose name follows a
scheme (`unix.rack-b4`), with its own `resources` and `metadata`, and elements
opt in through their patterns. Reserving a portion of a lab means declaring
that portion as a lab. Elements remain the smallest divisible unit; host-level
lab addressing is deliberately **not** provided — a user who wants "only these
hosts" declares a sub-lab of the elements that hold them. This costs no new
syntax, no new loader logic, and no new runtime concept.

## 8. Reservations: the lab is the reservable unit

### 8.1 Model

`resources` lives only on the `labs` entry. `required_resources(lab)` is
`lab.resources` (for `a+b`, the union of the components' declared sets). Hosts
carry no resources; containers inherit none. `Lab.resources` is declared data,
never derived.

### 8.2 What this simplifies

The composite's resource recomputation (`_winning_resources`,
`_lab_level_extras`) existed only because resources were derived from hosts
that might be overridden. With declared resources it is deleted outright.

### 8.3 The corner this opens, and what covers it

Today two labs sharing a host automatically share that host's resource, so
reserving `unix` and `busybox` at once contends correctly with no author
effort. With lab-level resources, two labs that share elements contend only if
their authors declared a shared resource identifier. The declared model is the
right one (it matches the external reservation system, which reserves named
things, not otto hosts), but the loss of automatic overlap detection is real.
Two mitigations ship with this change; scheduler-level overlap awareness is
deferred:

- **Doctor check** in `otto init`, at the composite level where every
  declaration is visible: for every pair of labs that share at least one
  element, **both declare at least one resource**, and whose `resources` sets
  are disjoint, warn naming the labs, the shared elements, and the remedy
  (declare a shared resource, or declare one as a sub-lab of the other so the
  naming makes the relationship visible). A lab that declares no resources
  reserves nothing and is never half of such a pair (ruling R18 — otherwise
  every pair of resource-less labs would warn).
- **Docs**: the reservations guide and the worked example state the rule —
  labs that share elements must share a resource identifier.

## 9. Corner-case ledger

All fail-loud unless noted:

| Case | Ruling |
| ---- | ------ |
| `load_lab(N)` and no source declares `N` in a `labs` section | `LabNotFoundError` naming every source and stating that labs must be declared. |
| `N` declared, but no element in any source matches it | Error naming the declaring source — a declared lab with no members is a definition mistake, not an empty lab. |
| Element with empty or missing `labs` | Error. An unreachable element is a mistake; use `".*"` for global inclusion. |
| Element pattern that is not a valid regex | Error at parse, naming the element and the pattern. |
| Element pattern that matches no declared lab across all sources | Doctor warning in `otto init` (dead membership); not a load error — a shared file may legitimately serve projects that declare different labs. |
| Two labs share elements but declare disjoint, non-empty `resources` | Doctor warning (§8.3); two resource-less labs do not warn. |
| Duplicate element `(name, id)` in one file, or across the files of ONE source | Error naming both files. One element, one entry — splitting invites divergent metadata; within a source a duplicate is a typo (§2.4). |
| Same lab declared in two files of ONE source | Error naming both files (§2.4). |
| A file carrying only a `labs` table, or only `elements` | Allowed — files compose by union within a source. |
| A `paths` glob that matches nothing | Contributes nothing (debug log), like an absent `lab.json` today; the composite's existence rule reports the lab as undeclared if that leaves nothing. |
| Element with empty `hosts` | Error. |
| `element` / `element_id` / `labs` / `resources` on a host entry | Error via `extra="forbid"`, message names the key. |
| `labs` entry for a lab with no elements in this file | Allowed — elements may come from another source. |
| Same-lab element `(name, id)` collision across sources | Later source wins wholesale, warning names both — for every lab both elements match (§6 implementation limit). |
| `labs` entry collision across sources | Later source wins wholesale (resources + metadata), warning names both. |
| Two hosts of one element mutate `element_metadata` | Isolated — each host holds a copy. |
| `metadata` value that is not a JSON object | Error (must be a table/object at every level). |
| Duplicate host ids *within* one element or across elements | Unchanged from today's id-collision handling — ids are still derived per host. |
| Host-id collision across *distinct* element keys after the element-level source merge (e.g. element `dut` id 3 board-less vs element `dut3`) | Today's id-collision rule runs on the merged result, after element replacement — the element merge never hides a host-id clash. |

## 10. Riders on the same break

Small cleanups that should ship inside the one sanctioned break rather than
linger as compat:

- Drop the `log = true/false` back-compat coercion (`_coerce_log_bool`); the
  error now names the `LogMode` values. v2 files are canonical from day one.
- Fix the generated-schema asymmetry: inject registry enums for
  `valid_impairers` exactly as `valid_terms` / `valid_transfers` get today.
- `userland_options` **keeps its tri-state shape** (decision, recorded): unset =
  "probe at runtime" is a real third state a boolean cannot express;
  `"present"/"absent"` strings stay uniform with the 3-4-valued siblings
  (`elevation`, `timeout_style`) and leave room for future values. The gap is
  documentation, not the shape.
- Element `id` stays an **int** (confirmed). Pydantic's lax mode still
  coerces a JSON `3.0` to `3` — the factory already documents that divergence
  and it is unchanged here.

## 11. Migration: loud error only (decision)

Hard cutover, no dual-shape support — carrying `hosts` and `elements` forever is
the painted-in corner this effort exists to avoid. A top-level `hosts` key fails
at parse with an error that states the move (hosts → elements; per-host `labs`
and `resources` → the element and the `labs` section), shows a short
before/after sketch, and links the migration note in the docs. **No migration
tool** — considered and cut; the transform is mechanical enough to do by hand
with the docs at your side, and the tool would outlive its usefulness in weeks.

## 12. Tooling that makes the schema usable

- **Schema staleness**: the VSCode gaps observed in the field were stale
  scaffolded schemas, not generator gaps — schemas are written once at
  `otto init` and drift as otto upgrades. Stamp the generating otto version into
  each schema doc; `otto init`'s doctor (which already diffs generated vs
  on-disk) reports version mismatch as stale with the re-run remedy.
- **Doctor checks** from §8.3 and §9 (dead patterns, disjoint-resource
  overlap) join the same `otto init` validation pass.
- **Snippets**: scaffold a `.vscode` snippets file generated from the live
  models — a lab declaration, an element wrapper, a host entry with required
  fields pre-populated per family, a cred entry. Generated, so it cannot drift
  from the spec.
- **Init banner**: print both completion steps (`otto --install-completion` and
  `source ~/.bash_completions/otto.sh`). The init e2e test runs the banner's
  command list, so the added step is exercised automatically.

## 13. Testing strategy

Every guard below must be demonstrated red first (mutate-and-observe-red):

- **Shape**: v2 fixture parses; `hosts` top-level fails with the migration
  error (red: feed a v1 fixture); duplicate `(name, id)` errors; empty `hosts`
  errors; each hoisted field on a host errors naming the key; empty element
  `labs` errors; an invalid regex errors naming the element.
- **Declaration and membership**: undeclared lab → `LabNotFoundError` with the
  declaration guidance; declared-but-memberless → error naming the source;
  pattern membership uses fullmatch (red: assert `re.search` semantics — a
  pattern `"unix"` must NOT admit `unix2`); `".*"` reaches every declared lab;
  `list_labs` returns declared names only (red: a name that appears only in an
  element pattern must not be listed).
- **Flattening**: for the migrated fixtures, the built hosts' `element` /
  `element_id` / `id` / `logical_index` values are pinned as literals recorded
  from the v1 world before the switch (a v1 file cannot be loaded after the
  break, so the equivalence is pinned, not computed); `element_metadata` copies
  are mutation-isolated (red: alias the dict); `lab_info` carries the component
  lab's name, resources, and metadata, including under `a+b`.
- **Reservations**: `required_resources` equals the declared set (red: a host
  carrying a stray attribute must not contribute); `a+b` unions; containers
  contribute nothing; the composite serves the winning `labs` entry's
  resources. The disjoint-resource doctor warning fires for a shared-element
  pair and stays silent when a resource is shared.
- **Composite**: element-wholesale replacement (red: assert the finer host-level
  merge — must fail); the warning names both sources; `labs`-entry replacement.
- **Metadata**: surfaces at all three levels; `Lab.__add__` unions metadata
  keys; non-object metadata errors.
- **Drift guard**: `_NON_SPEC_RUNTIME_FIELDS` update is pinned by the existing
  spec/runtime pairing test.
- **Schema**: generated lab schema validates the v2 fixture and rejects a v1
  file; `valid_impairers` enum present (red: today's generator).
- **Docs-field completeness guard** (lands here, since `lab-config.md` is
  rewritten for v2 anyway; the docs spec relies on it): every field of every
  registered host spec, plus the element and `labs`-entry specs, appears in
  `docs/guide/configuration/lab-config.md` — the existing gap-sync idiom. Red:
  delete one field's row. This is what keeps the reference complete as fields
  are added later.
- Existing suites (completion cache, conformance, scoping, link derivation) run
  unchanged — they are the proof the change stayed boundary-only.

## 14. Planning refinements (2026-08-27, found while writing the plan)

Mechanism-level corrections the implementation plan argues from; none changes
a §14 decision.

- **`HostSpec` keeps `element` and `element_id`.** The flat host-dict API —
  `create_host_from_dict`, `host_identity`, `validate_host_dict`,
  `addressing_from_dict`, and every custom backend that builds hosts from its
  own records — needs them to compose the id. The *file-entry* layer is what
  forbids them: `ElementSpec` rejects any hoisted key (`element`,
  `element_id`, `labs`, `resources`) inside a host entry with an error naming
  the key and the element, and `flatten()` injects `element` / `element_id`
  before the flat dict reaches the factory. `labs` and `resources` leave
  `HostSpec` outright.
- **Lab existence is backend-agnostic: a lab exists iff some source lists it
  in `list_labs()`.** For the json backend `list_labs()` returns the names
  declared in its `labs` sections. A single json source loads through
  `CompositeLabRepository` too (`config.lab.load_lab` wraps it), so the
  existence and declared-but-memberless rules live in ONE place, the
  composite, where every source's declarations are visible. A json source's
  own `load_lab(name)` is a *contribution*: members whose patterns match plus
  the `labs` entry if it declares the name; it raises `LabNotFoundError` only
  when it has neither.
- **`labs`-entry replacement is keyed on declaration**: a later source that
  lists the lab in `list_labs()` supplies `resources` + `metadata` wholesale;
  a source that only contributes members supplies no lab-level data.
- **`lab_info` is stamped by `config.lab.load_lab`'s attribution sweep** (the
  existing per-component loop that stamps `source_lab`), after the composite
  merge — the only point where a component lab's final resources/metadata are
  known. `element_metadata` is stamped by the factory (a new
  `create_host_from_dict(..., element_metadata=...)` loader argument), before
  providers run, like `source_lab`.
- **`HostSummary` gains `lab_patterns: list[str]`** (default empty). The json
  backend fills it from the element and resolves `labs` against its own
  declared names; the composite re-resolves every summary's patterns against
  the union of all sources' `list_labs()`, so completion for a lab declared
  in one source still offers elements from another. Custom backends that
  return concrete `labs` are unaffected.
- **Doctor warnings are a separate channel from doctor problems**: dead
  patterns and disjoint-resource overlaps print as yellow warnings after the
  `otto init` table and never set the exit code.
- The init e2e test hand-lists the banner commands rather than reading the
  banner, so the added completion step is pinned by a unit test on the banner
  text instead.

## 15. Decisions log

| Decision | Choice | Where discussed |
| -------- | ------ | --------------- |
| Sequencing vs docs overhaul | Schema first; docs start second, written once against v2 | session 2026-08-27 |
| Element scope | Identity + membership + metadata only; no inheritable defaults | session 2026-08-27 |
| Lab membership | On the element, as fullmatch patterns; elements are the smallest assignable unit | session 2026-08-27 (Chris) |
| Lab existence | Declared in the `labs` section; enumeration reads declarations | session 2026-08-27 (consequence of patterns) |
| `resources` | Lab-level only; the lab is the reservable unit | session 2026-08-27 (Chris) |
| Sub-lab addressing | A sub-lab is a declared lab with a scheme name; no host-level addressing | session 2026-08-27 |
| Lab context on hosts | `host.lab_info` (name, resources, metadata) alongside existing `source_lab` | session 2026-08-27 |
| Multi-source merge unit | Element, wholesale; labs entries likewise | session 2026-08-27 |
| Migration aid | Loud error only; no migrator tool | session 2026-08-27 |
| Strictness | Keep `extra="forbid"` + explicit `metadata` | session 2026-08-27 |
| `userland_options` shape | Keep tri-state literals | session 2026-08-27 |
| Element `id` type | int, ≥ 0 (confirmed) | session 2026-08-27 |
| Multi-file composition | Any subset of sections per file; union within a source; `paths` globs; in-source duplicates are errors | session 2026-08-27 (Chris) |
