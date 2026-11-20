# Lab templating over externally owned inventories

**Issue:** #1176. Remote Terraform (#803) and Ansible/AWX (#1179) sources are future work (section 12).
**Status:** approved 2026-11-18. Reviewed in two rounds by Astra and Opus; the maintainer accepted the recommended option for D1–D6 (section 11).

## 1. Why

Many otto users run large fleets of labs that are nearly identical: the same
element shapes and the same otto settings, differing by values that follow a
rule (an address, a rack, a console port, a board count). Today each lab is
written out in full in `lab.json`. The copies drift, and a fix to one lab
misses the rest. This is the "one decision, one place" defect class, in data.

At the same time, the facts about those machines (what exists, where it is,
how to reach it) already live in tools the team uses to deploy them: NetBox
for physical equipment, Ansible or Terraform for virtual testbeds. Writing
those facts again for otto creates a second source of truth.

The goal is to let a team:

- keep machine facts where they already live, with otto reading them as a
  consumer, from several tools at once;
- write otto's own attributes (how otto talks to a host, which hosts form a
  lab) once, as templates that stamp out every instance; and
- never rewrite an existing `lab.json` to do so.

## 2. Goals, non-goals, anti-goals

**Goals**

1. **Several inventories at once.** A project can read NetBox for physical
   hosts and a JSON or Jsonnet inventory for fixture VMs in the same run, plus
   any registered backend. A virtual host may live in any of them: a fixture
   VM in `inventory.json` (`"is_virtual": true`) or in NetBox. The design
   leaves room for remote Ansible and Terraform servers (section 12).
2. **NetBox Virtual Machines**, as opt-in records with `is_virtual: true`.
3. **One template language for otto's attributes in v1: Jsonnet.** It renders
   in memory, over the records of every configured inventory, into ordinary
   lab documents.
4. **Jsonnet is also the escape hatch for data.** A team without an
   inventory server can write its inventory records in Jsonnet. In v1 this is
   the familiar-format input for every team that does not run NetBox.
5. **Several layouts per project**, and **host-level definitions reusable
   inside full lab layouts**.
6. **One source of truth for each fact and each relationship.** Machine facts
   come from the inventory that owns them. Instance membership comes from
   upstream when upstream declares it (an explicit instance map), and is
   derived (by grouping on a fact such as `rack`) only when it is not.
7. **The result can be explained.** A user can see what a template rendered
   and why a host got each value.

**Non-goals (v1)**

- Template languages other than Jsonnet. Jinja2 (Ansible's language) and then
  HCL (Terraform's) are planned for later versions. They plug into the same
  renderer slot (section 6.6), fed the same inputs.
- Role aliases for tests, such as `get_host(role="dut")`. Worth doing, as a
  follow-up issue. Marked as my call.
- Remote Ansible (AWX) and Terraform (HCP Terraform-compatible) inventory
  sources. Deferred until a dedicated server can prove them (section 12).
- Writing to any external system. otto never writes to NetBox, and future
  adapters will never write to theirs either.

**Anti-goals: things that would make this a failure even if it works**

- An otto-specific template syntax, inheritance keys or merge rules. otto
  adopts Jsonnet as it is.
- A second lab loader. Rendered output takes exactly the parse, validation,
  composition and host-resolution path a hand-written `lab.json` takes.
- Breaking any existing project. Every current `lab.json`, `[inventory]`,
  `[creds]` and `[[lab.sources]]` loads unchanged, with the same result.
- Silently binding a lab to the wrong machine: the same key in two
  inventories, or the same rack name at two sites.
- Writing rendered files to disk. Rendering happens in memory. A team that
  wants generated files reviewed in git can still render them in CI
  themselves, as they could today.

## 3. Constraints

- **Backwards compatibility now matters**: otto has early users. Everything
  here is additive.
- **Python floor 3.10**, with a 3.15 canary. otto supports CPython
  3.10–3.14 on Linux, macOS and Windows, and documents binary-only air-gapped
  installs (`docs/installation.md`). A new dependency must not break an
  install on any of them (section 6.9).
- **One package, no extras** (`pyproject.toml`). A new runtime dependency is
  a packaging decision and goes through the import budget and the `.venv`
  budget.
- **otto never invents identity.** Keys come from the inventory. Lab and
  element names come from the template author.
- **Rely on the tool.** otto reads each tool's own evaluated view and never
  re-implements its logic.
- **No threads with asyncio, and the lab build stays synchronous.** The
  `Inventory` and `LabRepository` protocols and `build_lab` are sync, and
  teams' own backends implement them. Backend I/O is blocking, as the NetBox
  backend already does it during the lab build (pynetbox, sync `requests`).
  Marked as my call.
- **One decision, one place** (AGENTS.md). In particular, all four lab-file
  readers (section 7.1) move onto one reader.

## 4. Approaches considered

Two independent reviews informed these choices: one by Astra (with the
repository) and one by a naive Opus reviewer (with the user docs only).

### 4.1 Linking otto attributes to external records

- **A. Regex on inventory keys as the meaning of `inventory`.** Rejected as
  the *mechanism*. A literal key such as `dut.01` would suddenly also match
  `dut-01`. Capture groups that mint lab names turn a key into a location,
  which the inventory docs forbid. Templates never match records by
  regex (section 5).
- **B. Select records by attributes and group them, emitting exact
  references.** Adopted. This is the established paradigm: Kubernetes label
  selectors, Ansible `constructed` / `keyed_groups`, NetBox config contexts
  and Prometheus service discovery. In v1 it lives as a Jsonnet library
  (section 6.5), not as a new otto file format.
- **C. otto attributes stored inside Ansible, NetBox or Terraform data.**
  Rejected as the default. It puts otto's schema into files otto does not
  own, and gives three different inheritance models. Kept only in a narrow
  form: an adapter may pass *named* external variables through as labels
  (section 6.2), so values live upstream and otto's schema stays in otto.
- **Explicit instance map** (instance → member keys, from upstream). Adopted
  as an optional adapter capability (section 6.2). It is the only way to
  report an instance whose hosts have *all* disappeared.

### 4.2 Template engines

- **No engine; an otto layout schema in JSON.** Rejected. It invents a schema
  that grows loops and expressions over time.
- **Several built-in engines now.** Rejected for v1, on maintenance cost.
- **One engine (Jsonnet) at a renderer slot, more engines later.** Adopted
  (maintainer's decision). Jsonnet fits otto's JSON documents: every JSON
  value is valid Jsonnet, it is hermetic, and it has functions, imports and
  comprehensions.
- **ytt** was Astra's first choice for the values-file model. Not chosen,
  because the maintainer decided on Jsonnet as the single v1 engine.

### 4.3 Key resolution across several inventories

- **(a) Named inventories, with identity = (inventory name, key).** Adopted.
- **(b) Search in order, with a key found in two inventories an error.**
  Rejected. Proving uniqueness needs every inventory, so an unrelated outage
  (an unreachable Terraform backend) would block a known NetBox host.
- **(c) First match wins.** Rejected. Reordering settings could silently
  redirect a test to different hardware.

### 4.4 What otto reads from Ansible and Terraform (future)

The maintainer ruled that Ansible and Terraform sources are **remote servers
only**; their local files are never otto's source of truth. Local files enter
otto through JSON or Jsonnet. The reading approach for the future adapters is
recorded in section 12. Rejected along the way:

- Running the local CLIs (`terraform output -json`, `ansible-inventory
  --list`). Sound for "rely on the tool", but it makes local files the source
  of truth and requires the binaries on every controller.
- Parsing `.tf` source, raw state files or Ansible YAML/INI. Desired state
  with unresolved variables, a format HashiCorp says may change, or a
  re-implementation of Ansible's group_vars, plugins and precedence.

## 5. Decided by the maintainer

- otto is a consumer of external inventory data, as it is of NetBox today.
  Those tools own their data.
- v1 ships named inventories, NetBox Virtual Machines and Jsonnet. Remote
  Ansible and Terraform servers are future work (section 12); when they come,
  they are remote servers only, never local files.
- Several built-in inventory backends plus registered alternatives, usable
  simultaneously, for example NetBox for physical hosts and Jsonnet for
  fixture VMs (later, Ansible or Terraform servers for virtual ones).
- Named inventories, with machine identity = inventory name + key.
- Virtual hosts may live in any backend. NetBox Virtual Machine support is in
  this work.
- otto attributes are Jsonnet files, and that scaffolding works over every
  inventory.
- **No regex in templates.** Templates match records with Jsonnet's own
  capabilities (comparisons, `std.member`, `std.startsWith`, comprehensions)
  over groups, labels and facts. otto adds no native functions.
- Jsonnet is the only template engine in v1. Later versions add Ansible's
  (Jinja2) and then Terraform's (HCL) template languages.
- Rendering happens in memory. Nothing is written to disk.
- **Jsonnet packaging (section 6.9):** one platform-independent otto wheel,
  with the binding chosen by environment markers: `gojsonnet` on Linux, the
  C++ `jsonnet` on Windows and Apple-silicon macOS, and no binding on Intel
  macOS.

**D1–D6 (section 11):** six decisions the reviews left to the maintainer.
He accepted the recommended option for each; they are marked
**[D1]**–**[D6]** where they apply below.

## 6. Design

### 6.1 Named inventories

A project declares inventories as named entries, like lab sources:

```toml
[[inventories]]
name = "physical"
backend = "netbox"
url = "https://netbox.example"

[[inventories]]
name = "virtual"
backend = "json"
path = "lab_data/virtual.jsonnet"

[[inventories]]
name = "fixtures"
backend = "json"
path = "lab_data/inventory.json"
```

**One process-wide catalog.**

- The `[[inventories]]` envelope reserves exactly `name`, `backend`,
  `cache_ttl` and `creds`; every other key belongs to the backend.
- Names are process-wide. Two active projects that declare the same name must
  agree on backend, normalized settings, cache policy and credentials binding,
  reusing `CompiledInventory.same_as` and `CompiledCreds.same_as`
  (`src/otto/inventory/config.py`). Otherwise bootstrap fails naming both
  files. Project-over-user resolution applies per name. For `default`, the
  inventory and credentials agreements stay the independent walks below.
- Qualified references resolve by name across the whole catalog.

**`default` is exactly today's inventory.**

- The inventory named `default` is what today's two independent walks
  produce, unchanged: the `[inventory]` walk and the `[creds]` walk, project
  over user, each finding its own first declaration, with today's agreement
  checks and anchoring. So project A's `[inventory]` with project B's
  `[creds]`, and the user-file/project splits in both directions, keep
  working (`tests/unit/inventory/test_config.py`).
- A `[[inventories]]` entry named `default` takes part in that walk as an
  `[inventory]` would. Declaring both forms in one file is an error.
- **[D6] The `[creds]` twin of D1.** A project that declares any
  `[[inventories]]` and also a `[creds]` table, without binding `default`
  itself, is refused at bootstrap. The error points at the entry's `creds`
  sub-table. Otherwise its `[creds]` would bind to the process `default`
  (possibly the user's personal inventory), so `physical` would silently lose
  its credentials and the project's passwords would merge into records for
  machines it does not own.
- Credentials compose only through `merge_creds`
  (`src/otto/inventory/creds.py`), which identifies a credential by login and
  protocol scope. Explicit `creds` sub-tables bind to their `[[inventories]]`
  entry only; a store is never an implicit source for another inventory.

**References.**

- Qualified: `"inventory": {"name": "physical", "key": "edge-17"}` consults
  only `physical`. Generated documents always use this form. The field is
  `name`, not `source`, so it never reads as a lab source. Marked as my call.
- Bare: `"inventory": "edge-17"` resolves through `default`, never through
  "the only inventory" and never by searching for the key. **[D1]** A project
  that declares any `[[inventories]]` may use bare references only if it
  explicitly binds `default` itself (keeping its legacy `[inventory]`, or a
  `[[inventories]]` entry named `default`). Otherwise its bare references are
  an error naming the qualified form. This closes the migration trap: a
  project moves its `[inventory]` to `physical` while the user file still has
  a personal `[inventory]`, and its old bare reference would otherwise bind to
  the user's machine.
- One normalizer turns every reference form into (inventory name, key). The
  runtime join, the doctor (`src/otto/inventory/doctor.py`, which checks
  strings only today), orphan reporting and the JSON schema
  (`src/otto/models/jsonschema.py`) all use it.

**`supplies` belongs to each inventory.** Resolution selects the named
inventory first, then calls the existing `resolve_host_entry` with that
inventory and its local key. No composite inventory advertises a union of
fields.

**Inventory commands and the doctor.** `otto inventory lookup|list|export|
diff|refresh` take `--inventory NAME`, defaulting to `default`. The doctor
checks every inventory and attributes each finding to its name. Its choice
of `default` for existing configurations is unchanged (section 9).

### 6.2 Record vocabulary for discovery

Records keep every existing field, and gain two normalized ones:

- `groups: list[str]`: memberships (NetBox role and tags in v1; later,
  Ansible groups or a Terraform output field).
- `labels: dict[str, str | int]`: scalar values used for selection and
  substitution (`pod`, `console_port`, `vlan`). Adapters validate types at
  their boundary: strings such as `"007"` stay strings; booleans, arrays and
  objects are refused, naming the record and label.

Rules:

- Each adapter maps its native concepts onto these explicitly, through its
  settings. A NetBox role and a similarly named tag become equivalent only
  when the mapping says so. NetBox custom-field extraction is shared with the
  existing `custom_fields` and `extra_custom_fields` handling
  (`src/otto/inventory/netbox.py`), with the destination stated per mapping.
- `groups` and `labels` join the existing supplies-exemption definition
  (`src/otto/models/inventory.py`): discovery data, never a host field.
- `extra` stays opaque and is never a template input.
- The docs' rule "records carry facts only" gains one sentence: labels are
  facts *about the instance*, available to templates, and never otto
  behaviour (`os_type`, terms and option tables stay in otto's files).

**One lab may draw from several inventories.** A router lab can take its DUT
from `physical` (NetBox) and its traffic generator from `virtual` (a
Jsonnet fixture inventory). Templates group records from different inventories on a shared
label, with the grouping scope declared (section 6.5).

**Optional capability: instances.**

- An adapter whose upstream declares instance membership exposes it through
  an optional, structurally detected discovery capability returning
  `{records, instances}`. "Unsupported" and "supported but empty" are
  distinct results.
- Instance members are the adapter's **own** keys. Upstream data never spells
  otto's project-local inventory names; cross-inventory joins happen in
  templates.
- Examples: the JSON instances document format in v1,
  `rig-7 → {dut: edge-17, tgen: ixia-9}`; later, an Ansible group per rig or
  a Terraform output map.
- Only an explicit instance map can report an instance whose hosts have all
  gone.
- The JSON inventory gains instances through an explicitly selected document
  format (a top-level format marker), because `instances` and any other bare
  top-level name are already legal host keys. Flat legacy documents keep their
  meaning.
- Instances round-trip losslessly through `SnapshotCache`, export and diff. A
  change in membership only changes the fingerprint.

### 6.3 Adapters

Inventory backends register through the existing inventory registry
(`src/otto/inventory/registry.py`), and lab sources through the lab-source
registry (`src/otto/labs/registry.py`), exactly as a team's own would. If a
team already registered a name that a new built-in uses in the same registry
(in v1 the `jsonnet` lab source; later the `awx` and `terraform` inventory
backends), the team's registration keeps the name, with a warning naming
both. Marked as my call.

- **JSON (existing).** Gains `groups`, `labels` and the instances document
  format. Its `path` may also name a `.jsonnet` file (section 6.6).
- **NetBox (existing).**
  - Virtual Machines are **opt-in**: a `virtual_machines = true` setting with
    its own `vm_filter` and address mapping. Existing device-only
    configurations make zero VM requests, so a token limited to devices keeps
    working.
  - VMs become records with `is_virtual: true`, keeping cluster and site.
  - One duplicate check runs across devices and VMs together; a collision
    fails naming both object kinds and ids.
  - Gains mappings for role and tags to `groups`, and custom fields to
    `labels`.
- **Ansible and Terraform:** future work (section 12).

### 6.4 The Jsonnet lab source

A new lab-source backend renders Jsonnet entry files into lab documents in
memory:

```toml
[[lab.sources]]
backend = "json"
paths = ["lab_data"]                     # existing hand-written labs, unchanged

[[lab.sources]]
backend = "jsonnet"
paths = ["lab_data/layouts/*.jsonnet"]
inventories = ["physical", "virtual"]    # the only inventories it reads or references
scope = ["router-lab.", "switch-lab."]   # optional contribution scope (section 6.8)
```

**Inputs.** Each entry file is evaluated with one top-level argument, `otto`:

- `inventories`: for each inventory the source declared, a public projection
  of its records, built below `CredsOverlay` with record credentials and
  `extra` removed. It reuses the snapshot module's stated-field logic
  (`src/otto/inventory/snapshot.py`) with an allowlist of public fields. Each
  record carries `inventory`, `key`, the public facts, `groups`, `labels`, and
  instances where the adapter provides them.
- `project`: the project name.

No environment variables, no external variables, no native functions and no
credentials are passed.

**Imports.** One import callback serves `import`, `importstr` and
`importbin`, for both Jsonnet consumers (sections 6.4 and 6.6):

- It resolves only under dedicated library roots (`lab_data/lib/` by default,
  configurable) and canonicalizes symlinks. The entry file's own directory is
  **not** a root, because entry files often sit beside `creds.json` and
  `inventory.json`.
- It refuses any configured inventory, credentials or settings file, even
  inside a root.
- `otto.libsonnet` is reserved and always importable.
- `JSONNET_PATH` and ambient search paths never apply.
- Evaluation runs with stack and output limits, and `std.trace` output is
  discarded rather than written to stderr.

**Output.** A JSON object with any subset of `labs`, `elements` and `links`.
It goes through `wrap_secret_values`, `parse_lab_sections` and
`parse_lab_document` with the entry file as its origin. Then otto checks:

- every inventory reference, and every instance member, names an inventory
  the source declared;
- canonical host ids (after otto's existing `slug()`) are unique within the
  source;
- everything the source contributes falls inside its declared `scope`, if it
  has one (section 6.8).

It composes inside its source by union and across sources by today's
element-wise override, like JSON files, subject to **[D3]** (section 6.5).

**Errors** name the entry file, and the Jsonnet line where the evaluator
reports one. Validation errors after rendering name the entry file plus the
lab, element, host index and field.

**One evaluation per operation.** Each operation (a run, a `--list-labs`, a
doctor pass) evaluates each entry file once against one pinned inventory
view, shared by listing, loading, host summaries and the doctor. Fingerprints
cover the entry files, their transitive imports and the absent import
candidates that could later shadow them, otto's library version, and each
declared inventory's fingerprint.

### 6.5 `otto.libsonnet`: the scaffolding

The scaffolding lives in a small, readable, versioned Jsonnet library that
ships with otto. A team may read it, wrap it or ignore it. It provides:

- **Matching:** plain Jsonnet predicates over a record: `inGroup(name)`,
  `label(name, value)` and `fact(name, value)`, composable with Jsonnet's own
  boolean operators. A template may write any predicate of its own the same
  way. There is no regex and no otto native function (section 5).
- **Instances.** `instances(records)` where the adapter provides them, or
  `groupBy(records, scope)` where they must be derived. A derived scope must
  include the location fields that make it unique, for example
  `['site', 'rack']`. A record missing a scope value fails its instance.
  Grouping may span inventories; it never requires members to share an
  inventory, so a NetBox DUT and a Jsonnet-fixture traffic generator can form one
  rig.
- **Counts:** `expect(role, records, n)` makes an instance whose role does
  not have exactly `n` members unavailable, with the reason, while other labs
  load. Records are never paired by enumeration order. Jsonnet's `error`
  aborts the whole file, so the failure is returned as data: the `labs` table
  entry gains `"unavailable": "<reason>"` (section 6.8).
- **Naming.** Lab and element names are **author-supplied**, built from the
  instance identity (all scope values, e.g. `east-R7`) and the role, never
  from a bare inventory key. Emitted names are checked for duplicates.
- **Membership.** When the library writes generated lab names into an
  element's `labs` membership patterns, it escapes them in pure Jsonnet, so a
  name with regex metacharacters matches only itself
  (`src/otto/models/lab.py`). Hand-written membership is unchanged.
- **Emission:** `element(name, record, host)` emits an element with a
  qualified inventory reference.
- **Provenance.** The library may emit origin data (instance, role, host
  definition) beside the document for `otto lab explain` (section 6.7).

**[D3] Generated overrides.** A generated element whose canonical id matches
an element from another source, and that resolves to a **different machine**
(a different qualified inventory identity), is an error unless the `jsonnet`
source declares the override explicitly (`overrides = ["edge-17"]`). A
generated element that resolves to the same machine overrides as today, with
today's warning. Legacy JSON-to-JSON overrides are unchanged.

**Cross-lab merging.** `Lab.__add__` (`src/otto/labs/loader.py`) merges two
hosts with the same id and the same IP today. It also requires their
qualified inventory identities to match where both carry one, and fails loud
otherwise: two isolated VMs that share a private IP are different machines.
Host summaries get the same check.

Example: one `router-lab` per site and rack (one DUT from NetBox, one traffic
generator from a Jsonnet fixture inventory):

```jsonnet
// lab_data/layouts/router-lab.jsonnet
local o = import 'otto.libsonnet';
local hosts = import 'hosts.libsonnet';   // lab_data/lib/hosts.libsonnet

function(otto)
  local recs = otto.inventories.physical + otto.inventories.virtual;
  o.layout({
    instances: o.groupBy(recs, ['site', 'rack']),
    name(i): 'router-lab.%s-%s' % [i.site, i.rack],
    roles: {
      dut:  { match: o.inGroup('dut'),  count: 1, host: hosts.routerDut },
      tgen: { match: o.inGroup('tgen'), count: 1, host: hosts.linuxTgen },
    },
  })
```

```jsonnet
// lab_data/lib/hosts.libsonnet: host-level definitions, reused by many layouts
{
  routerDut(r): { os_type: 'router-appliance', valid_terms: ['ssh', 'console'],
                  console_options: { server: 'consrv1', port: r.labels.console_port } },
  linuxTgen(r): { os_type: 'unix', valid_terms: ['ssh'] },
}
```

**Host-level reuse** comes in two complementary forms, with no new otto
inheritance system: Jsonnet functions and objects in `.libsonnet` files, as
above, and the existing `[os_profiles]` for static per-kind defaults.

**Shared equipment.** A shared console server is either a hand-written
element joining by pattern (`"labs": ["router-lab\\..*"]`), or emitted once by
a template with membership in every lab that uses it. **[D5]** A link between
a generated host and a host from another source is resolved after
composition (section 7.3).

The library's exact function names and signatures are left to the builder,
within the rules above.

### 6.6 The renderer slot

Jsonnet sits behind one internal renderer interface: inputs (entry file, the
`otto` argument, import roots) in, one JSON value out, through the import
callback of section 6.4. Two consumers use it:

- the `jsonnet` lab source (section 6.4); and
- the JSON inventory backend, when its `path` names a `.jsonnet` file. That
  file's output depends only on its declaration and its tracked imports: it
  gets **no** inputs (not even `project`, since a user-file inventory has no
  project and two agreeing declarations must render the same records). Its
  import roots are dedicated directories anchored at the declaration, under
  the rules of section 6.4. Its fingerprint and stat paths cover the import
  closure.

Data flows one way only: inventories feed lab templates, and a lab template
never feeds an inventory.

The interface is internal in v1. It becomes registrable when Jinja2 arrives,
so the second engine shapes the public contract. Marked as my call.

### 6.7 Inspection

- **`otto lab render <entry-file> [--source NAME]`** prints the rendered lab
  document before source composition, membership filtering or host
  construction. It works even when the document then fails validation. It
  refuses to print a document containing a recognized literal secret, using
  the one secret recognizer (`SECRET_DATA_KEYS`, `src/otto/models/base.py`),
  rather than printing an altered one.
- **`otto lab explain <host-id> --lab NAME`** prints, for one selected lab:
  - the lab source and entry file behind the host;
  - instance, role and host definition, where the scaffolding emitted them
    (plain Jsonnet gets the entry file only);
  - for each field, which layer won: inventory record, credentials store, OS
    profile, template, `[host_preferences]` or CLI;
  - each consumed inventory's name and age.

  Field winners are recorded where the merges happen (`resolve_host_entry`,
  `merge_creds`, `_merge_host_dict` in `src/otto/host/factory.py`), never
  rebuilt in the CLI.

Both are thin CLI layers over library functions.

### 6.8 Failure scope, staleness and caching

**Today's rule stays the default.** The composite asks every source for every
lab (`src/otto/labs/composite.py`), and a failing source fails every lab load,
naming the source. Nothing below weakens that for a source without a scope.

**Opt-in contribution scope.**

- A `jsonnet` source may declare `scope`: exact lab names or literal
  prefixes. These are settings, not template regex.
- otto enforces it on rendered output: every declared lab, every element
  membership and every link the source contributes must fall inside it.
  Shared equipment that joins labs by an open pattern stays in a hand-written
  source.
- When a scoped source fails, labs provably outside its scope load normally.
  Labs inside it fail with the source's reason (shown as unavailable when
  their names are known from cache). Cached names serve diagnostics only;
  they never prove that a failed source could not contribute.
- The composite gains a distinct source-unavailable result for this.

**Unavailable labs.**

- A `labs` entry may carry `"unavailable": "<reason>"`. Hand-written JSON
  accepts it too (one loader), which also suits a lab under maintenance. A
  later declaring source replaces the entry wholesale, by the existing rule.
- `--list-labs` shows such a lab with its reason; `--lab <name>` fails naming
  it.
- The reason and `HostDrop` (`src/otto/labs/drops.py`) share one underlying
  diagnostic. Lab availability is authoritative state; a drop is derived from
  it when enumeration omits something.

**Protocol compatibility.**

- `list_labs() -> list[str]`, `load_lab(..., inventory=...)` and
  `list_host_summaries(..., inventory=...)` stay exactly as they are. A legacy
  backend receives the `default` inventory, `None`, or, in a project that
  refuses bare references (D1), a refusing stand-in that raises the D1
  diagnostic before any lookup. It only ever sees string references. A
  backend that resolves references privately, without `resolve_host_entry` or
  `inventory.lookup`, is outside D1's guarantee; the extending docs say so.
- New behaviour comes through optional capabilities, detected structurally
  like `SupportsHostSummaries`: one receiving a named-inventory view, and
  `list_lab_status(*, view) -> list[LabStatus]`. A core adapter turns a legacy
  `list_labs()` into all-available statuses.
- The conformance suite (`src/otto/testing/conformance.py`) checks the new
  capabilities only where a backend implements them.

**[D2] Stale data.** `SnapshotCache` today serves a snapshot of any age after
an `InventoryError` (`src/otto/inventory/cache.py`). v1 adds typed errors
that a backend may raise, used by registered backends now and by the future
Ansible and Terraform adapters:

- NetBox's behaviour, and any backend raising a plain `InventoryError`, is
  unchanged.
- An upstream **data** error (`InventoryDataError`: missing output, parse
  failure, missing field) never falls back to a snapshot.
- A **transient** outage (`InventoryUnreachableError`) uses a snapshot for execution
  only within a per-inventory `max_stale` (default: none, so expired data is
  diagnostic-only). The age is always shown. `max_stale` is policy, not
  data: it is excluded from the snapshot's identity, so raising it during an
  outage serves the existing snapshot instead of orphaning it.
- The decision lives in the existing cache, tested for expired, corrupt,
  missing and TTL-zero cases.

**Caching.** Remote backends go behind the existing `SnapshotCache` with a
`cache_ttl`, as NetBox does: normalized, credential-free records and
instances only. Raw upstream output is never cached.

**Completion.**

- **Templated labs and hosts tab-complete like hand-written ones**, cold
  cache included (maintainer, 2026-11-18: templating must not cost users
  completion). Completion evaluates a Jsonnet source when its inputs changed,
  as it parses `lab.json` today. Evaluation is local and hermetic, with no
  network and no subprocess.
- A warm TAB stays stat-only. The evaluated result is cached, keyed by the
  stat data of the entry files, their imports and absent import candidates,
  plus each consumed inventory's fingerprint, so a warm TAB keeps today's
  file-operation budget.
- Evaluation runs under completion's existing deadline
  (`src/otto/config/completion_cache.py`) with the evaluation limits of
  section 6.4. A source that overruns or fails is reported as a drop, with
  its reason, in `otto cache info`; it never blocks TAB.
- Remote inventories behave in completion exactly as today: a cold or
  expired snapshot may be fetched within completion's existing deadline. A
  no-fetch rule was considered to protect against subprocess adapters; with
  those deferred (section 12) v1 keeps today's behaviour, so nothing
  user-visible changes. Revisit when section 12's adapters land. Marked as my
  call.
- Freshness for Jsonnet sources is stat-only over entry files, imports and
  absent candidates.

### 6.9 Packaging

- One platform-independent otto wheel. The binding is chosen by PEP 508
  environment markers in `[project] dependencies`:
  - `gojsonnet` on Linux (x86-64 and aarch64, `manylinux2014`, matching the
    documented air-gap recipe in `docs/installation.md`);
  - the C++ `jsonnet` on Windows and Apple-silicon macOS;
  - no binding on Intel macOS, where neither publishes a wheel. Using a
    template there fails naming the platform; otto itself installs.
- Known gaps, documented: Alpine/musl Linux has no `gojsonnet` wheel; there is
  no `gojsonnet` wheel for 3.15 yet (relevant only when the 3.15 canary is
  promoted).
- One shim is the only place either binding is imported. It is imported
  lazily, so a project without Jsonnet pays nothing at import time.
- The template test suite runs under both bindings in CI, because the C++ and
  Go implementations differ in error wording and some standard-library edge
  cases.
- Before phase 3 lands, the marker set is proved against every documented
  `--only-binary` offline target and the import and `.venv` budgets.

## 7. Integration points

1. **One lab-document reader and one file-discovery rule.** Four places
   decode lab files today: the runtime repository
   (`src/otto/labs/json_repository.py`), the doctor
   (`src/otto/init/doctor.py`), link completion
   (`src/otto/config/completion_cache.py`) and init's next-step suggestion
   (`src/otto/init/next_steps.py`). They all move onto one reader that returns
   (document, origin) for a JSON file or a rendered entry file. The
   entry-to-files rule (`expand_lab_paths`, which keeps only `.json` today and
   is hard-wired in `src/otto/labs/sources.py`) becomes a per-backend fact on
   `LabSourceFacts`, so completion and the doctor see Jsonnet sources through
   the same expander. Import closures reuse
   `otto.config.corpus_snapshot.classify`.
2. **Named-inventory view for listing.** Today `load_lab()` receives an
   inventory and `list_labs()` does not (`src/otto/labs/protocol.py`). The new
   optional capabilities (section 6.8) give listing and loading the same
   view, so `--list-labs` and `--lab NAME` agree.
3. **Links resolved after composition. [D5]** Each repository resolves its
   declared links against its own addressing map today
   (`src/otto/labs/json_repository.py`), so a generated link to a console
   server from another source fails with "unknown host". Links are resolved
   once, after the composite merges hosts, through the one
   `resolve_declared_links` (`src/otto/link/derive.py`). Each link carries
   (document origin, document-local index), which also fixes today's defect
   of reporting every link against the constant `LAB_FILENAME`.
4. **Inventory provenance** gains the inventory name
   (`src/otto/host/inventory_ref.py`).
5. **Caches.** The inventory cache wraps each named inventory before
   credentials apply, as today.

## 8. Testing and acceptance

- **Unchanged loading.**
  - Every existing lab and inventory fixture loads unchanged with the same
    result.
  - Project A's `[inventory]` with project B's `[creds]`, and both
    project/user splits, resolve as today.
  - An unchanged third-party lab backend and inventory backend go through
    registration, listing, loading, completion and conformance.
  - A team registration named `jsonnet` (a lab source) keeps its
    name, with the warning.
- **Template matches hand-written JSON.** A rendered fleet and its
  hand-expanded JSON go through the same configured sources and give
  identical labs: lab names, host ids, console options, links, inventory
  references, duplicate rejection within a source and element-wise override
  across sources.
- **Wrong-machine guards.**
  - Two inventories both hold `edge-17`; the selected machine and credentials
    never depend on declaration order.
  - East/R7 has only a DUT and west/R7 has only a traffic generator: two
    unavailable labs, never one runnable lab.
  - `dut.01` and `dut-01` in one inventory: the duplicate canonical id is
    rejected.
  - Two isolated VMs with the same id and IP in different inventories: the
    cross-lab merge fails loud.
  - The migration trap of section 6.1: the bare reference fails before any
    lookup.
  - A generated element replacing a different machine without `overrides`
    fails.
  - Reordering inventory declarations, and sources whose contributions do not
    overlap, changes nothing. Reversing an intentional override changes its
    winner.
- **Failure scope.**
  - A failed unscoped source fails every lab load, as today.
  - A failed scoped source: labs outside the scope load; labs inside fail.
  - A failed source that contributes a DUT to `bench` without declaring
    `bench` fails `bench`.
  - Both declaration orders for `unavailable`; a first run with no cache.
- **Staleness.** A fake remote backend's data error never becomes a
  successful load at an old address. Expired, corrupt, missing and TTL-zero
  snapshots.
- **Hermeticity.** A recognizable test secret in `creds.json` or in a
  record's credentials never appears in rendered output, stdout, stderr,
  errors or snapshots. A secret in `extra` never reaches a template input or
  rendered output. (The inventory snapshot keeps `extra` byte-for-byte as
  today; it is opaque record data, not a credential.) Imports of
  `creds.json`, `inventory.json` and a path outside the roots fail.
- **Adapters.**
  - NetBox: a device-only configuration makes zero VM requests; a same-name
    device/VM pair fails naming both.
  - Instances: a cold/warm cache parity test that changes only membership.
- **Completion.** A templated host and lab complete on a cold cache. A warm
  TAB performs zero Jsonnet evaluations and stays stat-only. Editing an
  entry file or an imported library re-evaluates once. A template that
  overruns the deadline is a reported drop, never a hung TAB. One evaluation
  per entry file during a real operation.
- **Inspection.** `render` and `explain` produce the expected output.
- **Packaging.** The marker set installs binary-only on every documented
  offline target; Intel macOS installs otto without a binding.
- **Docs.**
  - A worked-example page under `docs/configuration/lab/`, linked from
    `lab-config.md` and `host-sources.md`.
  - The inventory page gains named inventories, NetBox Virtual Machines and
    the `.jsonnet` inventory path.
  - One page shows the full precedence chain from record through credentials
    store, OS profile, template, `[host_preferences]` and CLI.
  - The source/inventory distinction is made explicit, since Ansible users
    call all of this "inventory".
  - The "labs are declared, never derived" principle gains its stated
    exception for templates.

## 9. Delivery

1. **Contracts and named inventories.** Sections 6.1 and 6.2, the
   protocol-compatibility, availability and staleness contracts of section
   6.8, the instance snapshot round-trip, the JSON instances document format,
   the reference normalizer, `--inventory NAME`, section 7.2, 7.4 and 7.5,
   plus fake-backend conformance fixtures that phases 2 and 3 build against.
   Everything else depends on it.
2. **NetBox Virtual Machines** (section 6.3), against the phase-1 fixtures.
3. **Jsonnet:** packaging (6.9), the renderer slot and import callback, the
   lab source, the `.jsonnet` inventory path, `otto.libsonnet`, scopes,
   inspection, completion of templated labs and hosts, the reader and
   file-discovery consolidation, and post-composition links (sections 6.4–6.9,
   7.1 and 7.3).

   **Examples and walkthroughs use templating (maintainer, 2026-11-19).**
   Phase 3 is not done until:
   - The testbed lab configs template their Ubuntu VMs. In
     `tests/_fixtures/lab_data/tech1-inventory/`, the VM name and address
     correlate (`testN` is `10.10.200.(10+N)`), which is how users are
     expected to template their own labs. The tech1 configs render the same
     hosts from one Jsonnet function instead of restating each VM.
   - Examples use templating wherever a repeated host or lab shape appears.
   - The lab configuration walkthroughs teach templating as a step.
   - Every page that explains templating passes a naive-reader walkthrough.
     A Haiku agent, given only the docs, sets up a templated lab from the
     walkthrough. Each point where it fails or misreads is fixed in the docs,
     and the walkthrough is rerun until it succeeds.

Phases 2 and 3 run in parallel once phase 1 lands. The last pull request adds
one real end-to-end acceptance test: NetBox and Jsonnet inventories →
template → composed lab.

**Scope rulings (maintainer, 2026-11-19).**

- **Phase 2 lands first.** NetBox Virtual Machines landed ahead of phase 1,
  as their own pull request, because they need none of phase 1's contracts.
  They read every field through pynetbox. A field missing from a trimmed
  list response is loaded from that object's detail page once, inside the
  fetch session.
- **Completion keeps today's design in phase 1.** Phase 1 changes completion
  only where qualified inventory identity requires it: resolving through
  the named-inventory view, `inventory_ref` on summaries, D1 refusals
  reported as drops, and machine identity in the JSON same-id fold. Making
  completion offer exactly what `--lab` dispatch loads, including
  cross-project membership, is tracked separately in #1185.
- **A lab whose load fails is partial in completion, as today.**
  Completion offers the hosts it could resolve and records a drop; dispatch
  refuses the lab.
- **The doctor keeps today's `default`.** For existing configurations, the
  doctor's `default` remains the project's own tables, else the user file.
  The active catalog supplies only named inventories that the project
  references but does not declare. The doctor-vs-dispatch divergence for
  `default` is tracked in #1186.

## 10. Left to the builder

- The exact `otto.libsonnet` API, within section 6.5's rules.
- Settings key names for NetBox mappings, scopes and overrides.
- Jsonnet evaluation limits, measured on a representative large fleet.

## 11. Maintainer decisions D1–D6

The reviewers left these to the maintainer. On 2026-11-18 he accepted the
recommended option for each, as written into the design above. The rejected
alternatives are kept for the record.

- **[D1] Bare references in a project that adopts named inventories.**
  Decided: allowed only if the project explicitly binds `default`;
  otherwise an error naming the qualified form. Alternative: always resolve
  through the process-wide `default` (simpler, but leaves the migration trap
  of section 6.1).
- **[D2] Stale data for remote backends.** Decided: data errors
  never fall back; transient outages use a snapshot for execution only within
  an opt-in per-inventory `max_stale`, so by default expired data is
  diagnostic-only. Alternative: NetBox's rule (any age, with a warning) for
  transient outages.
- **[D3] Generated elements overriding other sources.** Decided: an
  override that changes the machine needs an explicit `overrides` entry; one
  that keeps the machine overrides as today. Alternative: today's
  last-source-wins with a warning for all.
- **[D4] Terraform secrets** (applies when the future Terraform adapter
  lands, section 12). Decided: read only the designated output;
  the docs make it the operator's public contract and offer a separately
  produced artifact for stronger assurance. Alternative: read all outputs to
  see Terraform's `sensitive` flag and refuse a sensitive one, at the cost of
  bringing every other output's plaintext into otto's process.
- **[D5] Links across lab sources.** Decided: resolve after composition,
  so a generated link can reach a shared console server from another source.
  Alternative: keep today's same-source rule for v1 and document it, which
  makes shared equipment and its links live in the same source.
- **[D6] `[creds]` in a project that adopts named inventories.** Decided
  2026-11-18: refused unless the project binds `default` itself (section
  6.1). Raised by the phase-1 plan review.

## 12. Future work: remote Ansible and Terraform servers

Deferred on 2026-11-18 until a dedicated server can host them as a proving
ground. The v1 contracts (named inventories, `groups`/`labels`, the instances
capability, typed errors and D2, and the snapshot cache) are what these adapters will build on, so they need no v1 rework.

Recorded so the work can resume without re-research (2026-11-18 findings;
verify against the real servers first):

- **Ansible: AWX** (the free, open-source upstream of Red Hat Ansible
  Automation Platform, with the same API). Its inventory script endpoint,
  `/api/v2/inventories/<id>/script/`, returns standard Ansible inventory JSON
  (hosts, groups, `_meta.hostvars`), evaluated by the server. The
  `hostvars=1` query parameter is documented only in community examples so
  far. Mapping: hostname as key, `ansible_host` as address with hostname
  fallback, groups to `groups`, configured hostvars to `labels`, rig groups to
  instances. Ansible plugins drop empty groups, so an Ansible source cannot
  report a rig whose hosts have all gone.
- **Terraform: the HCP Terraform / Terraform Enterprise API**
  (state-version outputs, which carry a per-output `sensitive` flag; revisit
  D4 with that in hand). One adapter would serve HCP Terraform's free tier
  (500 managed resources; the legacy free plan ended 2026-03-31) and
  self-hosted open-source TFE alternatives such as OTF or Terrakube, whose
  coverage of the state-version-outputs endpoint is unverified. OpenTofu
  pairs with those for a fully open-source stack. Plain remote state (S3,
  GCS) with no server API is out of scope: reading the state file directly
  depends on a format HashiCorp says may change.
- **Shape:** both are HTTP backends like NetBox, registered through the
  existing inventory registry, with no local binaries. Acceptance: recorded
  API fixtures in unit tests, plus one test against each real server.
- **Tracking:** Terraform is #803 (rescoped to this approach on 2026-11-18);
  AWX is #1179.
