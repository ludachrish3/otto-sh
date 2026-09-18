# Data at the boundary

otto consumes data from three outside sources: lab files (`lab.json`),
repo settings (`.otto/settings.toml`), and `OTTO_*` environment variables.
The rule for all of them is the same: **pydantic at the boundary, plain
objects inside**. External data is validated exactly once, by a spec model in
`otto.models`, and what crosses into the rest of the codebase is an ordinary
runtime object that is never re-validated.

## The spec → runtime pattern

Each kind of input has a `*Spec` model whose job ends at construction:

```text
lab.json labs entry   → LabEntrySpec.model_validate(…) → Lab.resources / .metadata
lab.json element      → ElementSpec.model_validate(…)  → .to_element() + .hosts → factory(entry, element=…)
  its host entry      → HostSpec.model_validate(…)     → spec.to_host(cls, …)  → UnixHost
settings.toml tables  → settings spec models           → spec.to_runtime()     → backend objects
OTTO_* environment    → OttoEnvSettings                → typed fields (paths, …)
```

`lab.json` has three nested boundaries, not one. The wrapper specs live in
{mod}`otto.models.lab`: `LabEntrySpec` for a declared lab's `resources` and
`metadata`, `ElementSpec` for one piece of equipment — identity, its
fullmatch `labs` patterns, `metadata`, and the raw host entries it groups.
`ElementSpec.to_element()` builds one runtime {class}`~otto.host.element.Element`
per `ElementSpec`, shared by every host of it, and the loader passes that
instance beside each of the spec's host entries — untouched — to the factory
(`element=`), so the flat host-dict API downstream (the factory,
`host_identity`, custom backends) never carries element fields at all. The
same spec refuses the three hoisted keys (`element`, `element_id`, `labs`)
*inside* a host entry, naming the one it found.

The split keeps validation errors where the *data* is (a bad `lab.json`
field fails with a pydantic error naming the file and field, not a traceback
deep in connection code) and keeps runtime classes free of parsing concerns.
Field names are `snake_case` end to end — JSON, TOML, models, and runtime
attributes all agree, so there is no translation layer.

One deliberate escape hatch: keys beginning with `_` are stripped before
validation at *every* level of a `lab.json` — the document, the `labs` table,
a `labs` entry, an element, a host entry, a link — the sanctioned way to keep
comments in a format that has none (`"_comment": "…"`), alongside a top-level
`$schema`. Everything else unknown is still rejected loudly, and a top-level
`hosts` key (the pre-v2 shape) fails with a migration message rather than an
unknown-section one.

## Host construction

{class}`~otto.models.host.HostSpec` is the abstract boundary model for one
lab-data host entry; `UnixHostSpec` and `EmbeddedHostSpec` extend it with
family-specific fields (menus like `valid_transfers`, embedded strategy
selectors like `filesystem` and `binary_loader`, per-protocol option tables
like `ssh_options`):

```{inheritance-diagram} otto.models.host.UnixHostSpec otto.models.host.EmbeddedHostSpec
:parts: 1
:top-classes: otto.models.host.HostSpec
```

Construction, driven by
{func}`otto.host.factory.create_host_from_dict`, runs in a fixed order:

1. The entry's `os_type` selects an {class}`~otto.host.os_profile.OsProfile`,
   which names the base family — and thereby the host class and which spec
   validates the entry.
2. Profile defaults are merged under the host's own fields (explicit lab data
   always wins over profile defaults), and preference-resolved option
   defaults are folded in ({doc}`hosts`).
3. The spec validates the merged dict; `to_host()` builds the runtime host.
4. Product providers run, attaching products ({doc}`hosts`).

A drift guard in otto's test suite enforces that runtime host fields and spec
fields stay mirrored — adding an init field to a host class without its spec
counterpart fails CI.

## Settings and environment

`OttoEnvSettings` (pydantic-settings) is the single reader of `OTTO_*`
variables. Repo `settings.toml` files are parsed during bootstrap phase 1
into `Repo` objects ({doc}`../lifecycle`); their tables (`[docker]`,
`[reservations]`, `[coverage]`, `[[os_profiles]]`, `[host_preferences]`) each
have spec models. `otto.models.settings` is deliberately a leaf module — it
must not import the packages it configures, or validation would drag the app
graph into every boundary crossing.

## Where labs come from: the labs package

Lab loading is behind a protocol so hosts don't have to come from JSON files:
`LabRepository` (in {mod}`otto.labs.protocol`) is the host-source
contract, the built-in `json` backend reads `lab.json` files from the paths a
source declares, and alternatives (a database, an inventory service)
register a name via {func}`otto.labs.register_lab_repository`.
{func}`otto.testing.assert_lab_repository_conforms` verifies a custom backend
against the contract, and `otto.examples.lab_repository` is a copyable
reference implementation. See {doc}`../../configuration/host-sources`.

A process reads *every* source every repo declares:
{func}`otto.labs.build_lab_sources` constructs each `[[lab.sources]]` entry
and wraps them — always, even a single one — in a `CompositeLabRepository`
that consults them in order and lets a later source override an earlier one
wholesale per record (an element, or a `labs` table entry), with a warning
naming both. The composite satisfies the same protocol, so nothing downstream
can tell how many sources there are; it is also where a lab's *existence* is
decided, which is why one source goes through it too.

A lab exists only because some source *declares* it in a `labs` table. An
element joins labs by regex, so the set of lab names cannot be derived from the
elements — a pattern like `"unix.*"` names nothing in particular — and the
`labs` table is the enumerable record of what exists. The element is the
smallest unit that joins a lab, so reserving a portion of a lab means declaring
that portion as a lab of its own.

The source list exists for one layering: physical devices are global truth —
every team must be served the same records, from a database or a globally
shared file — while the VMs and QEMU guests a project deploys, re-images and
reconfigures are the project's own and belong in its repo. The override warning
in ordinary command output is the whole transparency story: an override is a
deliberate act, and otto says so every time one takes effect.

Naming a host is a separate, cheaper query than loading one: tab completion
and tunnel narrowing go through {func}`otto.labs.host_summaries`, which uses a
backend's optional `SupportsHostSummaries` fast path when it has one and
otherwise falls back to `list_labs` + `load_lab`. Either way the ids come from
the backend, so a custom host source drives completion — before this seam
existed, completion read `lab.json` directly and a custom backend contributed
nothing to it.

Merging is part of loading: `--lab` may be passed multiple times and the
resulting `Lab` objects merge, so a shared lab file and a personal overlay
compose without editing either.

### Inventory design choices

**Copy, never merge.** Unlike lab merging, an inventory backend
({doc}`../../configuration/inventory`) joins host facts into a lab entry by
copy: the inventory declares which fields it supplies, and an entry that
references it may not state them inline, so no machine fact ever has two
sources to disagree. A per-field precedence — "take it from NetBox if the field
is filled in, else from the lab file" — reads as convenience and behaves as a
trap: the day somebody fills the field in, the lab file's value goes silent
with no error anywhere. The collision check runs on the raw entry, before the
fill, so the fill cannot fool it. For the same reason a process has exactly one
inventory: when several active repos declare one, the tables must be identical,
or two inventories would reintroduce precedence through the back door.
`cache_ttl` is part of that comparison because it is behaviour, not decoration
— one repo saying `"0"` and another `"24h"` would let declaration order decide
whether the process caches at all. `element_id` is never filled, only
cross-checked, because a record is per host and an element is shared.

**Credentials compose.** They are the one field that makes the trade the other
way, on purpose. A per-field merge goes silent in exactly the way above — a
password set in the lab file hides the store's — but one order every layer
obeys is simpler to teach, it lets a team move creds between layers one entry
at a time without the load failing in between, and the lab file is where a cred
change is tried before the team-wide inventory or store changes. The lab file's
order is the login order because it is the one layer written knowing that
otto's first cred is the default login. Credentials get a store of their own,
read by otto rather than by the inventory backend, because they are universal
and secret — which is also why a NetBox-backed inventory pairs with a store and
moving to NetBox migrates none.

**The key is the one intended coupling.** It is an opaque string minted by
whoever owns the inventory: never an address (the inventory exists because
those change), never an otto host id or element name (if the inventory knew
otto's per-lab naming, the decoupling would be fictional), and immutable by
policy — a rename surfaces as a dead reference in every project's doctor, the
signal delivered where the fix is, and that friction is intentional. Record
field names are host-field names one for one, so the join is a plain key copy
with no mapping table to drift; the NetBox backend's is the one deliberate
mapping table in otto, and it refuses a custom-field mapping for `ip` because
`ip_source` already says where the address comes from, and two ways to say one
thing is a way for them to disagree.

**Location and caching.** The inventory lives in the user-level
`~/.otto/settings.toml`, which `otto init` never scaffolds, because an
inventory is not project-shaped: a machine is a machine regardless of which
repo you are working in. A remote inventory's snapshot cache defaults to `24h`
because NetBox changes on a human cadence, and `cache_ttl` accepts one spelling
per duration, so two settings files that mean the same thing look the same. An
unreachable backend serves the snapshot of any age with a warning — a lab that
loaded yesterday should load today, and the warning keeps the staleness
visible. Because lab-free commands install no log handler, and the log line
fires once per process, the `otto inventory` verbs and the `otto init` doctor
print that notice themselves: a green doctor table against a snapshot days old
is the one thing that gate must not print.

## Exported schemas

Because every boundary is a pydantic model, otto can *emit* its data
contracts: `otto schema export` writes JSON Schemas for `lab.json`,
`settings.toml`, and reservation files, which editors use for completion and
inline validation ({doc}`../../cli/schema/editors`). The schema is
generated from the exact model that validates ingest, so the export and the
runtime validator cannot disagree — there is no second definition to update,
and the schema version bumps whenever host-spec fields change shape, keeping
downstream lab data diagnosable. The `otto init` doctor flags a scaffolded
schema older than the installed otto because a schema older than the validator
would quietly bless data otto now rejects.

Extensions surface automatically: because project-registered host classes
bring their own spec models ({doc}`hosts`), a repo that extends otto sees its
fields in the export; `--builtins-only` restricts the export to otto's own
types.

## Filesystem awareness

One boundary is physical: where otto *writes*. `otto.filesystem` detects
network filesystems (NFS/SMB), and write-heavy components adapt — the monitor
database uses SQLite WAL journaling on local disks but DELETE journaling on
network mounts (where WAL's shared-memory semantics are unreliable), and log
rotation time-boxes its directory scans so an NFS stat storm cannot stall
startup ({doc}`../utilities/logging`).

## Where the code lives

- {mod}`otto.models.lab` — `LabEntrySpec`, `ElementSpec`: the
  `lab.json` wrapper layers above the host entry
- {mod}`otto.models.host` — `HostSpec`, `UnixHostSpec`, `EmbeddedHostSpec`:
  the spec half of the spec → runtime pattern
- {mod}`otto.models.settings` — settings-table spec models, including
  `OttoEnvSettings`, kept a leaf module
- {mod}`otto.host.factory` — `create_host_from_dict`, the fixed
  construction order
- {mod}`otto.host.os_profile` — `OsProfile`, the base-family → host-class
  selection
- {mod}`otto.labs.protocol` — the `LabRepository` host-source contract
- `otto.testing` / {mod}`otto.examples.lab_repository` — the conformance
  helper and reference implementation for custom lab repositories
- `otto.filesystem` — network-filesystem detection behind the
  write-adaptive components
