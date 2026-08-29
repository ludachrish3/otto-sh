# Host inventory layer — tool-agnostic host facts beneath lab.json

**Date:** 2026-08-28
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** `2026-08-27-lab-definition-v2-design.md` (landed on main as `0fba081a`,
unpushed — this spec breaks v2 freely where noted, since nothing depends on it yet)
**Companion:** `2026-08-28-three-level-reservations-design.md` (independent; touches
`resources`, not the inventory)

## 1. Goal

Separate what is **universally true about a machine** from what is **otto's
business about it**, so the first can live in a tool-agnostic store (JSON files
today, a database or NetBox later) and the second stays in otto-specific
`lab.json` files — without reintroducing the per-field composition that v2
deliberately refused.

The bridge is explicit: there is no inventory owner yet, so a JSON file is the
first backend; NetBox is the second, in the same spec, so the protocol is
designed against a real external record rather than guessed. Lab files written
during the bridge must not change when the backend swaps — the primary key is
chosen with that in mind (§3).

### In scope

- The `InventoryRecord` model, the `Inventory` protocol, the `inventory` host
  entry key, the join in the lab loader, provenance on the host, and the
  all-or-nothing enforcement (§2–§7).
- Configuration: a **user-level `~/.otto/settings.toml`** (new — otto has only
  per-repo settings today), a per-project override, "exactly one inventory
  per process" (§8).
- Two first-party backends: `json` and `netbox` (via `pynetbox`, a plain
  runtime dependency — otto stays one package with no extras), one
  credentials rule shared by both, and a **TTL snapshot cache** for remote
  inventories so startup does not wait on NetBox (§9).
- The adoption path — how to lay out the JSON stage, how to move to NetBox
  without rewriting a single reference, and how to keep NetBox otto-healthy —
  as a documented procedure with the verbs that check each step (§19), and a
  complete worked example of the `unix` lab under this spec (§20).
- Location fields on the host: `site`, `rack`, `shelf` beside the existing
  `board` / `slot` (§12).
- `otto init` doctor checks, a JSON schema for the inventory file, the
  completion cache, the read-only `otto inventory` verbs, docs, and the
  conformance surface for third-party inventory backends (§10–§11, §14–§15).
- Ride-along: fold the `_v2` test modules into their siblings (§17).

### Out of scope

- Any change to `resources` or the reservation gate — the companion spec.
- NetBox virtual machines (`virtualization/virtual-machines`); the first
  NetBox backend reads `dcim/devices` only. VMs are a follow-up once a real
  NetBox is in front of us.
- A vault/secrets-manager backend. The secrets seam exists (§9.2 `creds_file`);
  other providers plug into it later.
- Element-level inventory records. The grain is one record per otto host (a
  login endpoint); chassis-level facts already have `element_metadata`.
- Runtime host identity: `make_host_id()`, `slug()`, `logical_indices()` and
  link-id derivation are untouched. The new location fields are **not** part of
  the host id.

## 2. Two layers, one rule

v2's composite rule is "later source wins **wholesale** at record granularity,
never a field-level blend". What we avoided was *precedence among peers over the
same vocabulary*. The inventory is not a peer: it speaks a **disjoint
vocabulary**. Composition here is a **join**, not a merge, and a join across
disjoint field sets has no precedence question — provided the partition is
static and enforced at parse time. It is:

> **Data lives in exactly one layer. Keys may be asserted in both, and must agree.**

- *Data*: the configured inventory **declares which record fields it
  supplies** (`Inventory.supplies`, §10); for a host entry that references
  the inventory those fields are inventory-owned and everything else is
  inline, otto-owned. The partition is therefore per deployment, not per
  host and not per field-value — decided once at bootstrap, from
  configuration, never discovered from what a record happens to contain.
  There is no third state — §5 makes mixing an error, using the same
  mechanism `HOISTED_HOST_KEYS` uses today.

  This is what lets otto meet an existing NetBox where it is: a NetBox
  that models addresses and locations but has no `sw_version` custom field
  simply does not supply `sw_version`, and the lab file carries it. Adding
  the custom field later and mapping it is an explicit act, after which the
  doctor flags every lab file still carrying the field inline. What is
  **not** allowed is the tempting middle: "take it from NetBox if the
  custom field happens to exist, else from the lab file" — that is a
  per-field precedence rule, and the day someone adds the custom field the
  lab file's value would go silent with no error anywhere.
- *Keys*: `element_id` (and nothing else, for now) may appear in the inventory
  record as a fact and on the element as identity. When both are present they
  must be equal; otherwise it is an error naming the file, the element and the
  inventory key. A cross-check, never a fill and never a merge.

Why `element_id` cannot be filled: `(element, id)` is the element's identity,
consumed before any host exists — in-source duplicate checks, the composite's
replacement key, pattern→lab resolution. An identity that materialised after a
per-host lookup would make "which element is this" depend on a backend call.

## 3. Primary key

One opaque string per host entry, minted by whoever owns the inventory, treated
by otto as an uninterpreted identifier:

```json
{ "inventory": "carrot-b1", "os_type": "unix", "hop": "gw", "toolchain": {...} }
```

Rejected candidates, and why:

| Candidate | Why not |
|---|---|
| IP / hostname | The inventory exists because these change; a key that changes with the data is not a key. |
| `(element, element_id)` / host `id` | Otto's naming, per lab and per project. The inventory must not know otto's vocabulary or the decoupling is fictional. |
| Composite tuples (site, rack, slot) | Physical facts that drift; kept as data. |

**Bridge rule.** Keys minted in the JSON inventory during the bridge must be
the strings the eventual owner (NetBox: the device `name`) will resolve. The
lab files referencing them are the long-lived artefact; swapping the backend
must not rewrite a single one of those references. Keys are immutable by
policy — renaming one breaks every lab file that references it, which is the
one intended coupling.

Three key spaces, each owned by one layer, never shared: inventory key
(inventory), element key (lab file), host id (derived by otto). The only
cross-layer reference is the foreign key from lab file to inventory.

## 4. `InventoryRecord`

`otto.models.inventory.InventoryRecord(OttoModel)` — `extra="forbid"` like
every boundary spec; `_`-prefixed keys are comment space like everywhere else.
**Field names are host-spec field names, 1:1.** The fill (§6) is a plain key
copy; there is no mapping table in otto core. (The NetBox backend carries the
one deliberate mapping, §9.2.)

No carve-out: every record field is on the shared `HostSpec` base, so any
record can be received by any host family and no entry can fail validation
merely for being embedded. `hw_version` and `sw_version` were the last two
declared on `UnixHostSpec` alone — the §21 open question this spec shipped
with, since answered by widening them onto the base. An embedded target has a
firmware version as surely as a Unix box has a distro one, and both fields are
otto's own vocabulary rather than facts an inventory tool natively holds: like
`os_version`, they stay out of every backend's default `supplies` and arrive
only where a deployment maps them (§9.2). `supplies` (§10) is therefore the
sole control on which fields a given record may carry.

| Field | Type | Notes |
|---|---|---|
| `ip` | `str` | required — a record with no address is useless |
| `interfaces` | `dict[str, InterfaceSpec]` | as on `HostSpec` |
| `creds` | `list[CredSpec]` | universal but secret; the inventory backend is the secrets seam (§9) |
| `hw_version` | `str \| None` | |
| `sw_version` | `str \| None` | a *declaration* of what the device should run; what it is found to be running is an *observation* — they never merge |
| `os_name` | `str \| None` | free-form; `os_type` stays otto-owned because its values select the host class |
| `os_version` | `str \| None` | |
| `board` | `str \| None` | the named type of the board |
| `site` | `int \| str \| None` | §12 |
| `rack` | `int \| str \| None` | §12 |
| `shelf` | `int \| None` | §12 |
| `slot` | `int \| None` | |
| `is_virtual` | `bool` | default `False` |
| `element_id` | `int \| None` | a **key**, cross-checked, never filled (§2) |
| `extra` | `dict[str, Any]` | opaque; otto never reads it; lands on the host as `host.inventory_ref.extra` (§7) |

`FILLABLE_INVENTORY_FIELDS` is the frozen set of record fields minus
`{"element_id", "extra"}` — the **maximum** a backend may supply. It is
derived from `InventoryRecord.model_fields` at import, never hand-listed, so
adding a record field cannot forget the enforcement. A configured inventory
supplies a declared subset of it (`supplies`, §10); `ip` is always in that
subset — a reference that yields no address is pointless. The fields the
join copies, and the fields §5 forbids inline, are exactly `supplies`.

The set of otto-owned host fields is everything else on the host specs:
`element`, `element_id`, `name`, `os_type`, `user`, `hop`, `has_bash`,
`default_dest_dir`, `max_filename_len`, `debug_log_globs`, `metadata`, `log`,
`log_stdout`, `telnet_options`, `snmp`, `toolchain`, `command_frame`,
`power_control`, the `valid_*` menus and active pins, `docker_capable`,
`shell_history`, the `*_options` tables, `userland_options`, `filesystem`,
`loader`.

## 5. The `inventory` key on a host entry

`HostSpec` gains `inventory: str | None = None`. Semantics:

- `inventory` absent, or present as `null` → the entry is **inline**: it
  carries everything itself, exactly as v2 today. Nothing changes for such
  entries. The two spellings mean the same thing on purpose: `null` is what a
  schema-legal round-trip makes of an entry that never mentioned the
  inventory, and a reference that only exists after a round-trip would be a
  reference otto invented.
- `inventory` present → the entry is **referenced**: any key in the
  configured inventory's `supplies` present inline is an error (fields the
  inventory does not supply stay inline, as for an inline host):

  ```
  element 'chassis' hosts[1]: 'ip' is inventory-owned — it comes from inventory
  key 'carrot-b1'; remove it here, or drop 'inventory' and declare the host inline
  ```

  The check runs on the **raw** entry, before the fill, so it cannot be fooled
  by the fill itself. Because `supplies` is a bootstrap-time fact, the check
  lives in `resolve_host_entry` (which has the inventory in hand), not on the
  spec class; the spec's own validator only rejects an empty `inventory`.
  A field present inline as `null` states nothing, so it does not collide with
  the record either — the same rule one level down.

An empty string — like any other non-string value — is an error
("`inventory` must name a key (a non-empty string)"), never a second spelling
of "inline". The key is validated for shape only (non-empty `str`); its
resolution is §6's job.

## 6. The join

The inventory travels the road `preferences` already travels — an explicit
argument, threaded, never ambient:

- `otto.inventory.resolve_host_entry` is the join itself, and it runs **once
  per entry, in the lab loader** — not inside the factory:

  ```python
  @dataclass(frozen=True)
  class ResolvedEntry:
      host_data: dict[str, Any]   # a plain host dict; the `inventory` key is gone
      ref: InventoryRef           # provenance for the factory to stamp (§7)

  def resolve_host_entry(host_data: dict[str, Any], inventory: Inventory | None) -> ResolvedEntry:
      """Return *host_data* with its inventory reference resolved, or a copy unchanged."""
  ```

  which: returns a copy with an empty `InventoryRef` when the entry references
  nothing (§5); raises
  `InventoryError("host entry references inventory key 'x' but no inventory
  is configured; declare [inventory] in ~/.otto/settings.toml or in the
  project's .otto/settings.toml")` when it does and `inventory is None`;
  otherwise looks the
  key up, applies the §5 inline-forbidden check against
  `inventory.supplies`, cross-checks `element_id` (§2) when the inventory
  supplies it, copies every supplied field the record *states* (a field the
  record left unset, or set to `None`, is "not stated" — the entry's default
  applies), and returns a **new** dict beside the `InventoryRef` the factory
  stamps (§7). Never mutates the entry it was handed. Resolution precedes the
  os-profile merge: a profile may default an otto-owned field; it never
  defaults an inventory-owned one.

- `otto.labs.json_repository._add_host` — the one place a flattened entry
  becomes a host — makes that call before it validates or constructs anything,
  and hands the resulting `ref` on as a loader argument:
  `create_host_from_dict(host_data, preferences=None, lab_name=None, *,
  element_metadata=None, inventory_ref=None)`. The factory takes no inventory
  and performs no lookup. Instead all three of its entry points
  (`create_host_from_dict`, `host_identity`, `validate_host_dict`) call
  `reject_unresolved_reference` first, which **refuses** a dict still carrying
  a non-null `inventory` key, naming the key and the function that should have
  resolved it.

  Why the refusal rather than a second join site: `host_identity` validates
  the raw dict for completion summaries (`json_repository.py`, `examples/`),
  for link derivation (`link/derive.py`) and for preference matching inside
  the factory. A referenced entry has no `ip` until resolved, so each of those
  paths resolves first — the summary path calls `resolve_host_entry` exactly
  as `_add_host` does — and the guard is what turns "forgot to" into an error
  naming the key, instead of a validation failure about a missing `ip` or a
  completion list that quietly omits every referenced host.

- `otto.labs.protocol.LabRepository.load_lab(name, preferences=None,
  inventory=None)` and `SupportsHostSummaries.list_host_summaries(inventory=None)`.
  The composite forwards both. The json backend threads `inventory` down to
  `resolve_host_entry`, on the build path and the summary path alike.
  **Protocol change, deliberate**: a backend that builds
  hosts without the factory must still resolve references, and the conformance
  surface proves it (§14) — the alternative, a json-only join, would let a
  third-party source ignore `inventory` keys silently, the exact failure this
  design exists to prevent.

- `otto.config.lab.load_lab(labnames, search_paths=None, preferences=None,
  repository=None, inventory=None)` forwards to each `repository.load_lab`.
  Callers that today pass `preferences` (`cli/invoke.py`, `config/repo.py`,
  `context.py`, `completion_cache.py`) pass the process inventory from §8.

Failure text: every lookup failure is wrapped by `_add_host`'s existing
`LabRepositoryError` prefix, so a dead reference reads

```
Lab file 'lab_data/lab.json': element 'chassis' hosts[1] in lab 'rig':
inventory key 'carrot-b1' not found in inventory 'json:~/.otto/inventory.json'
```

## 7. Provenance on the host

`RemoteHost` gains `inventory_ref: InventoryRef` beside `lab_info` — the
runtime attribute is deliberately not called `inventory`, which is the *key
string* the lab entry states (§5):

```python
@dataclass(frozen=True)
class InventoryRef:
    key: str = ""          # "" for an inline host
    backend: str = ""      # the backend's label, e.g. "json:~/.otto/inventory.json"
    extra: dict[str, Any] = field(default_factory=dict)   # the record's opaque table, per-host copy
```

`__post_init__` copies `extra` (the `LabInfo` lesson: a frozen dataclass does
not freeze the dict behind a field). Stamped by the factory at build, before
the providers run, like `element_metadata`. Containers and the builtin `local`
host carry an empty `InventoryRef()` — they are not inventory hosts.

Surfaces that read it: `otto inventory lookup KEY` (§11) resolves the same
record a referenced host was built from; every factory error names the key
and the backend label; the doctor's summary names the label once. The bare
host-id listing (`--list-hosts`) stays an id list — provenance is a per-host
question, answered by the verb and the errors, not a column.

## 8. Configuration — exactly one inventory per process

A machine is a machine regardless of project, so the inventory is declared
**once per user**, in a user-level settings file otto does not have yet:
`~/.otto/settings.toml` (under `otto_home()`, so `OTTO_HOME` relocates it).
It is a general file — `[inventory]` is its first table, not its purpose —
parsed by a small `UserSettingsModel(OttoModel)` (`extra="forbid"`, so a
project-only table pasted there errors naming the key) and read once at
bootstrap beside the repo settings.

Resolution order, first hit wins:

1. `[inventory]` in an active repo's `.otto/settings.toml` — the per-project
   override for the fractured phase. If **more than one** active repo declares
   `[inventory]`, they must be identical — same backend, same kwargs after
   anchoring, same `creds_file`, same `cache_ttl` — or bootstrap fails naming
   both declaring files: two inventories would reintroduce precedence through
   the back door. `cache_ttl` counts because it is behaviour, not decoration:
   one repo saying `"0"` and another `"24h"` would otherwise let declaration
   order decide whether the process caches at all. The origins are what the
   comparison excludes — two repos naming the same inventory from their own
   settings files, each anchoring it to its own root, are not a conflict.
2. `[inventory]` in `~/.otto/settings.toml`.
3. Nothing → no inventory. Inline hosts work as today; a referenced entry fails
   with the §6 "no inventory is configured" error, which names both places.

No implicit discovery: an inventory is always declared. (An earlier draft
resolved a bare `~/.otto/inventory.json` implicitly; one mechanism is
simpler than two, and the declaration is one table.)

The same `[inventory]` table shape in both files, mirroring `[reservations]`:

```toml
[inventory]
backend = "json"                      # or "netbox", or a registered name
path = "~/lab/inventory.json"         # json backend kwarg; "~" expands; relative paths anchor per the rule below
creds_file = "~/.otto/creds.json"     # backend-independent (§9.4)

[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"            # the token itself never sits in a settings file
filter = { site = "lab-a", status = "active" } # any NetBox device filter (§9.2)
custom_fields = { sw_version = "sw_version" }  # optional: map ONLY custom fields NetBox already has
creds_file = "~/.otto/creds.json"
```

- `InventoryConfigSpec(OttoModel)`: `backend: str`, `creds_file: str | None`,
  `cache_ttl: str = "24h"` (§9.5, its grammar checked at the boundary),
  `extra="allow"` for the backend's kwargs (the `[reservations]` precedent);
  `SettingsModel.inventory: InventoryConfigSpec | None = None` and
  `UserSettingsModel.inventory` likewise. Kwarg validation happens in
  `otto.inventory.compile_inventory`, which knows the selected backend (the
  `compile_lab_sources` precedent): the json backend takes `path` and the
  optional `supplies`; unknown keys are an error naming them. Relative paths
  anchor to the **repo root** for a project override (the directory holding
  `.otto/`, not `.otto/` itself) and to `~/.otto` for the user file — a
  committed relative path must resolve stably wherever the repo is checked
  out, and the repo root is the directory its other committed paths are
  written against.
- Registry `otto.inventory.registry`: `register_inventory_backend(name, cls)` /
  `get_inventory_backend_class(name)` — the `otto.labs.registry` shape,
  builtins pre-registered at import.
- `build_inventory(repos) -> Inventory | None` performs the resolution above
  once at bootstrap; the result is what every §6 caller passes.
- `otto init` does **not** scaffold an inventory (it is not project-shaped).
  The scaffolded `settings.toml` gains a commented `#[inventory]` block with
  the two examples above, matching the `#[project]` precedent; the template
  drift guard (`test_init_templates.py`) covers it by construction.

## 9. Backends

### 9.1 `json`

The bridge format: a JSON object mapping key → record, nothing otto-shaped in
it, so a future export from the owning system produces the same file.

```json
{
  "$schema": "~/.otto/schemas/inventory.schema.json",
  "carrot-b1": { "ip": "10.10.200.11", "element_id": 1, "site": "hq", "rack": 3, "shelf": 2, "slot": 1,
                 "board": "cx-4", "sw_version": "4.2",
                 "creds": [{"login": "root", "password": "…"}],
                 "extra": {"asset_tag": "A-1042"} }
}
```

- `$schema` and `_`-prefixed top-level keys are comment space; every other
  key is an inventory key whose value validates as an `InventoryRecord`
  (error names the key and the field).
- `supplies` kwarg (list of record field names; default: every fillable
  field). A record carrying a field outside `supplies` is an error naming
  the key — the file must not hold what the deployment says the lab files
  hold. During the bridge, set it to what NetBox will supply (§19.1), so
  the lab files are already in their final shape.
- Parsed once per process on first `lookup`, held in memory. `list_keys()`
  returns the sorted keys. `fingerprint()` (§11) is the path it was handed,
  its mtime and its size. The backend never resolves a path itself: the
  settings layer anchors it (`compile_inventory`, §8) and `construct_inventory`
  resolves it on the way in, so the backend stays a plain reader of one path
  and there is one place symlinks are followed. When a `creds_file` is
  configured its own mtime and size are folded in by the overlay (§9.4).
- Label: `json:<path>`.
- This file is the **stage-1 inventory** — the thing NetBox replaces in
  stage 2 (§19). It is also what `otto inventory export` writes from any
  backend, so a NetBox-backed setup can keep a JSON snapshot of itself.

### 9.2 `netbox`

Uses `pynetbox` (runtime dependency, §16), imported lazily inside
`otto.inventory.netbox` so no other verb pays for it.

- **Construction**: `url`, `token_env` (default `"NETBOX_TOKEN"`), `verify:
  bool | str = True` (TLS verification, or a CA bundle path), `filter:
  dict[str, str]` passed to `dcim.devices.filter(**filter)` (any NetBox device
  filter — a site, a role, a tag; recommended for size, never required),
  `ip_source` (`"primary_ip4"` default, or `"oob_ip"`, or `"cf:<name>"` for a
  custom field, for a NetBox whose lab addresses live elsewhere), optional
  `custom_fields: dict[str, str]` mapping record fields to custom-field
  names, optional `extra_custom_fields: list[str]` naming custom fields to
  copy into `extra`, and `timeout: float = 30.0` — seconds one request may
  take (connect and read), a positive number, so an unreachable NetBox falls
  back to the stale snapshot (§9.5) after at most that long instead of the
  kernel's connect timeout.
- **The token is read at the first fetch, not at construction**, and never
  stored on the instance. Construction does no I/O and touches no environment,
  so a repo that declares a netbox `[inventory]` still runs every inline-only
  verb on a machine with nothing exported; a variable that is unset when the
  fetch happens is an `InventoryError` naming both the variable and the URL.
- **A relative `verify` path is refused at construction** (absolute, or `~`,
  only). It is the one argument that names a file otto does not anchor, and a
  committed settings table that resolves against whatever directory otto was
  run from is a configuration that works until someone changes directory.
- **Nothing is required of NetBox** beyond a device per referenced host
  whose `name` is the key, an address `ip_source` can find, and a read-only
  token. `supplies` (§10) is what NetBox natively models — `ip`, `site`,
  `rack`, `shelf`, `board`, `os_name`, `is_virtual` — **plus only the custom
  fields you map**. Any record field may be mapped **except** `ip`, `creds`
  and `interfaces`: `ip` already has `ip_source` (two ways to say one thing is
  a way for them to disagree), `creds` come from `creds_file` and nowhere else
  (§9.4), and `interfaces` is a structure, not a scalar a NetBox custom field
  can hold. No mapping, no requirement: `slot`, `hw_version`, `sw_version`,
  `os_version`, `element_id` and the rest stay in the lab files until you
  choose to map them, and mapping one is the explicit act that moves it
  (the doctor then flags every lab file still carrying it inline). There is
  no default custom-field name, no expected group, and no tag otto looks
  for. §19.3 lists what *helps* if you want NetBox to enforce things; none
  of it is needed for otto to work.
- **Custom fields, and why an instance full of them is harmless.** The backend
  reads exactly the custom fields `custom_fields` maps and ignores every
  other one — they never enter the record, so they cannot collide with
  anything, and `extra="forbid"` never sees them. One thing can bite, loudly:
  a mapped custom field of the wrong NetBox type (`element_id` mapped to a
  text field) fails the record's validation naming the device and the field.
  Anything else you want carried along for your own code is opt-in via
  `extra_custom_fields`; it lands in `host.inventory_ref.extra` and otto never
  reads it. A name that would shadow one of the keys this backend already puts
  in `extra` (`id`, `serial`, `asset_tag`, `status`, `tags`) is refused at
  construction rather than silently overwriting it.
- **Fetch the whole set, then cache it** (§9.5): a fetch pulls every device
  the filter matches (pynetbox paginates) into a dict keyed by device `name`;
  every lookup is a dict hit. NetBox is sometimes slow — one round of
  requests per TTL window, never one per host and not one per process. A
  duplicate device name within the filtered set is an error at fetch naming
  both device ids.
- **The mapping** (the one deliberate table, documented on the backend page):

  | `InventoryRecord` | NetBox device |
  |---|---|
  | key | `name` |
  | `ip` | `ip_source` (default `primary_ip4.address`) with the prefix length stripped; a device with none is **skipped** — `ip` is required on a record — and a `lookup` of its name is an error naming the device, its id and `ip_source` |
  | `site` | `site.name` |
  | `rack` | `rack.name` (`None` when unracked) |
  | `shelf` | integer part of `position`, which NetBox serialises as a decimal **string** (`"3.5"` for a half-U); `None` when unset |
  | `board` | `device_type.model` |
  | `os_name` | `platform.name` |
  | any other record field (`slot`, `hw_version`, `sw_version`, `os_version`, `element_id`, …) | **only if mapped** in `custom_fields`; otherwise not supplied — the lab file carries it |
  | `is_virtual` | `False` (devices only; VMs out of scope) |
  | `extra` | `serial`, `asset_tag`, `id`, `status`, `tags` (names) |
  | `interfaces` | not populated by the first backend (one more request per device); documented |

- **Credentials**: NetBox stores none (its built-in secrets model left in
  v3), so a NetBox-backed inventory always uses `creds_file` (§9.4). A
  referenced unix host with no `creds` anywhere fails `UnixHostSpec`
  validation (`min_length=1`) naming the key.
- `list_keys()` is the fetched name set **minus what the fetch skipped**: the
  devices with no address at `ip_source`, and the devices NetBox left unnamed
  (`name` is nullable there, and keyed by name they would all collide on one
  string). Both skip lists are exposed read-only — `addressless_device_names`,
  `unnamed_device_ids` — so `otto inventory list` and `refresh` can report what
  was selected and passed over; a device missing from otto must not be
  *silently* missing. `fingerprint()` is the snapshot's content hash (§9.5),
  so completion caches normally (§11).
- Label: `netbox:<url>`.
- Errors: connection, auth and API errors raise `InventoryError` wrapping the
  pynetbox exception text and naming the URL; never a raw `requests` traceback.

### 9.3 Third-party backends

Register a class under a name; its constructor takes `repo_dir` plus the
`[inventory]` kwargs verbatim (the lab-source precedent). `repo_dir` is the
**declaring** directory, the one relative paths anchored to (§8): the
repository root when a project `[inventory]` override selected the backend,
`~/.otto` when the user file did — and the user file is the usual case, since
an inventory is declared once per user. It must satisfy §10
and pass `otto.testing.assert_inventory_conforms` (§14). `creds_file` is
handled outside the backend (§9.4), so a backend never sees credentials
unless it chooses to carry them in its own records.

### 9.4 Credentials — `creds_file`, one home, both backends

`creds_file` is a **core** `[inventory]` kwarg, not a backend's: a JSON file
`{ "<inventory key>": [CredSpec, ...] }`, keyed by the same keys, parsed by
`otto.inventory.creds`, held outside any repository, mode `0600` advised (the
doctor warns when it is group- or world-readable). `build_inventory` wraps the
selected backend in a `CredsOverlay` that supplies `creds` from the file for
every lookup.

The §2 rule applies to it without exception: **when `creds_file` is
configured, a backend record that carries `creds` is an error** naming the
key ("`creds` come from creds_file; remove them from the record"). One home
per field — the overlay never chooses between two sources. Without
`creds_file`, records carry their own `creds` (the json backend allows it;
NetBox cannot) — and such a backend can never be snapshot-cached, since a
snapshot carries no credentials by design (§9.5).

Why promote it from a NetBox detail to the rule for both backends: it makes
the stage-1 inventory file free of secrets — shareable, diffable, committable
if you want — and it means moving to NetBox (§19.2) migrates **no**
credentials at all; the creds file stays where it is and keeps working.

### 9.5 Caching remote inventories

The whole-set fetch (§9.2) produces exactly a stage-1 JSON document (§9.1
shape, no creds), so caching it is the `export` writer plus a timestamp.
`build_inventory` wraps any backend whose `fingerprint()` would otherwise be
`None` — today, `netbox`; third-party remote backends opt in by returning
`None` — in a `SnapshotCache`:

- **Location**: `<otto home>/inventory-cache/<slug>.json`, from
  `otto.config.home.snapshot_cache_dir()` — that module owns every path under
  the home, so the cache never spells one itself and `OTTO_HOME` relocates it
  with everything else. The slug hashes the backend's *label* and its kwargs
  (URL + filter), so two NetBox configurations never share a file and two
  spellings of one URL never keep two snapshots. Beside it,
  `<slug>.meta.json` records the fetch time and the content hash.
- **TTL**: `cache_ttl = "24h"` in `[inventory]` (a duration string; `"0"`
  disables caching — every process fetches, as an uncached backend does).
  Daily is the default because NetBox changes on a human cadence; a lab that
  churns can shorten it, and `otto inventory refresh` forces a fetch at any
  time (§11).
- **Startup**: a snapshot younger than the TTL is used **without contacting
  NetBox** — the common `otto` invocation costs one file read. Older, a fetch
  runs and the snapshot is rewritten atomically (write-then-rename).
- **NetBox unreachable** (connection/auth/API failure) with a snapshot
  present, of any age: the snapshot is used and a **warning names its age**
  (`inventory 'netbox:https://…' unreachable (…): using cached snapshot from
  2026-08-27 09:14 UTC, 31h old`, and the line goes on to name
  `otto inventory refresh` as the way to replace it) — a lab that loaded
  yesterday should load today, and the warning is what makes the staleness
  visible. No snapshot → `InventoryError`, as before. The load-path warning
  stamps **UTC**, because
  it is a log line that may be read anywhere; `otto inventory refresh` renders
  the snapshot it replaced in local time, because that is one operator
  watching their own terminal. Ages coarsen as they grow: minutes under an
  hour, hours under two days (`31h`, not `1d 7h` — `cache_ttl` is written in
  hours and the reader is comparing the two), days and hours beyond. The
  notice is also readable state on the cache, because the verbs that must
  report it install no log handler (§11).
- **A backend that supplies `creds` is never cached.** A snapshot holds no
  credentials by construction (§9.4), so caching such a backend would answer
  *with* creds off the wire and *without* them for the rest of the TTL — a
  referenced unix host failing validation on alternate runs, with nothing in
  the message naming the cache. Neither half is negotiable, so the
  configuration is: construction fails naming the declaring file and the
  backend, and suggests `creds_file` or `cache_ttl = "0"`.
- `fingerprint()` for a cached backend is the snapshot's content hash, so the
  completion cache (§11) works for NetBox exactly as for a json file — and it
  is `None` whenever the snapshot and its meta file have fallen out of step (a
  crash between the two writes, a snapshot hand-copied in), since a
  fingerprint that lies would serve completion a stale answer for a whole TTL.
  The next real resolution repairs the pair. The json backend is never wrapped
  (its file *is* the snapshot).
- `otto inventory export` remains the *reviewable* artefact (a file you name,
  in a place you choose); the cache is otto's own and lives under `~/.otto`
  like the completion and workspace caches.

## 10. The `Inventory` protocol

`otto.inventory.protocol`:

```python
@runtime_checkable
class Inventory(Protocol):
    label: str                                   # for provenance and errors, e.g. "json:~/.otto/inventory.json"
    supplies: frozenset[str]                     # the record fields this instance supplies; always contains "ip"
    def lookup(self, key: str) -> InventoryRecord: ...    # raises InventoryKeyError(key, label)
    def list_keys(self) -> list[str]: ...
    def fingerprint(self) -> str | None: ...     # cache key for completion; None = not cacheable
```

`supplies` is fixed at construction from configuration (§9.1 `supplies`,
§9.2 native set + `custom_fields`), must be a subset of
`FILLABLE_INVENTORY_FIELDS` containing `ip` (checked at construction), and a
record returned by `lookup` must not carry a field outside it (conformance
checks this).

Errors: `InventoryError(OttoError)` for backend failure (I/O, network, parse,
auth); `InventoryKeyError(InventoryError)` for an unknown key. `lookup` must be
idempotent and must return an equal record on repeated calls (conformance
checks this).

## 11. Doctor, schema, completion cache, verbs

- **`otto init` doctor** (`_validate_lab`): builds the process inventory the
  same way bootstrap does, resolves each entry through `resolve_host_entry`
  and validates the result, so a referenced entry is checked as the loader
  will see it. Findings: a dead reference is a **problem** (exit 1) naming
  file, element, index, key and inventory label; "referenced but no inventory
  configured" is a problem; a broken `[inventory]` declaration is a problem
  naming the settings file, and the entries that need it are skipped for that
  run rather than reported as a second, invented failure; an orphan record (a
  key no lab file references) is a **warning** — informational during the
  bridge, where the inventory is expected to be wider than any project. A
  snapshot served because the inventory was unreachable (§9.5) is a warning
  too, printed here rather than logged: `otto init` is a `lab_free` group, so
  it installs no CLI log handler and the load path's warning would reach
  nobody. The doctor prints the resolved inventory label once in its summary.
- **Schema**: `build_schemas` gains `inventory.schema.json` (the json
  backend's file: `additionalProperties` = `InventoryRecord`, `$schema` and
  `_*` keys allowed), exported beside `lab.schema.json`, stamped with
  `x-otto-version`; the scaffold's `VSCODE_SETTINGS_TEMPLATE` (written by
  `_scaffold_editor_wiring`, only-if-absent) gains a `json.schemas` entry
  associating `**/inventory*.json` with it, and `editors.md` shows the manual
  wiring for an existing `settings.json`. `lab.schema.json` picks up
  `inventory`, `site`, `rack`, `shelf` from `HostSpec` automatically, and the
  fields a record fills move out of its `required` list into one arm of an
  `anyOf` whose other arm is `inventory` — constrained to a non-empty string,
  since `required` means *present*, and `{"inventory": null}` or
  `{"inventory": ""}` would otherwise validate a document otto refuses at load
  (§5).
- **Completion cache**: `SCHEMA_VERSION` 12 → 13 (summaries may now carry an
  inventory-supplied `ip`). The fingerprint that decides freshness includes the
  inventory's `fingerprint()` — the json file's path/mtime/size, or the
  snapshot hash for a cached remote backend (§9.5), so NetBox-backed
  completion caches normally. Only a third-party backend that returns `None`
  *and* opts out of the snapshot cache leaves completion uncached — still
  correct, by loading — and the guide says so. Such a digest is **ephemeral**:
  it is stamped with the clock so no later read can match it, and every
  fingerprint-keyed writer stands down rather than storing an entry under it.
  A freshness probe that *raises* is ephemeral for the same reason — an
  inventory whose probe failed has no stable identity, and a third party's
  error string is free to carry a timestamp or a request id. Its text still
  moves the digest, so a broken declaration is never served the working one's
  entry, but nothing is stored: storing would append one dead entry per otto
  invocation to a file every TAB parses, which is strictly worse than no cache
  at all.
- **Verbs** — `otto inventory`, `@cli_exposed` per the repo rule, all exiting
  1 with the §10 error text on failure (`diff` excepted, below). Every verb
  that resolves an inventory and finds none declared says so naming **both**
  places it could have been declared, with the user path resolved through
  `OTTO_HOME` rather than spelled `~/.otto/settings.toml` — a message naming a
  file the reader does not have sends them to edit the wrong one. Every verb
  that reads records prints the stale-snapshot notice (§9.5) itself: the group
  is `lab_free`, so it never installs a CLI log handler, and a `list` that
  answered from a three-day-old snapshot in silence is the failure the notice
  exists for:
  - `lookup KEY` — the resolved record as a rounded Rich table plus the label
    (creds shown as login names only, never passwords); `list` — keys, count,
    label, and what the last fetch skipped (§9.2). Their point is debugging
    the join without editing a lab file.
  - `export PATH` — writes the configured inventory as a stage-1 JSON file
    (§9.1 shape, sorted keys, **no** `creds`, which live in `creds_file`).
    From NetBox this is the offline / fast-completion snapshot; from JSON it
    is a normaliser. Refuses to overwrite unless `--force`.
  - `diff PATH [PATH]` — compares the configured inventory against a stage-1
    JSON file, record by record: keys only on one side, and per-field
    differences (creds excluded), as a table. With a **second** path both
    sides are files — yesterday's export against today's — and no inventory is
    resolved at all, because nothing in the answer depends on one. Three exit
    codes, `diff(1)`'s: **0** no differences, **1** differences, **2** could
    not answer (a missing or unreadable file, no configured inventory, a
    backend that is down). The split is load-bearing for the §19.2 gate: a
    typo'd path must not read to a script as a difference. This is the
    transition check: point `[inventory]` at NetBox, diff against the file you
    were using, and switch when the only differences left are the ones you
    intend (§19.2).
  - `refresh` — forces a fetch of a cached remote inventory and rewrites the
    snapshot (§9.5); prints the record count, the local-time stamp and age of
    the snapshot it replaced, and the snapshot's path. On an inventory that is
    **not** snapshot-cached it exits 1 saying why — the json backend reads its
    file every command, and a remote backend is cached only with `cache_ttl`
    above zero and no fingerprint of its own. A cheerful no-op is the
    silent-failure class: the operator asked for a fetch and did not get one,
    and `otto inventory refresh && <next step>` must not proceed as though
    they had.

## 12. Location fields

`HostSpec` (and therefore the runtime `RemoteHost`, the drift guard's field
list, `lab-config.md` and the completeness guard) gains:

| Field | Type | Notes |
|---|---|---|
| `site` | `int \| str \| None` | flexible: NetBox names sites; some labs number them |
| `rack` | `int \| str \| None` | same reasoning — NetBox names racks |
| `shelf` | `int \| None` | otto's concept; NetBox `position`'s integer part |

`board: str | None` and `slot: int | None` are unchanged. On the two union
fields a `BeforeValidator` coerces an ASCII-digit-only string to `int`, so
`"rack": 3` and `"rack": "3"` are the same value — otherwise pydantic's smart
union keeps the string and the §2 cross-check (should it ever extend to these)
and every equality on them would silently diverge. None of the three joins
`make_host_id()`; `logical_indices()` is unaffected.

Flat, not a nested `location` block, for one reason that outlives the
convenience: §4's 1:1 naming rule. A nested block on one side would need a
mapping on the other, and a mapping is where drift hides.

## 13. Errors and edge cases

| Situation | Behaviour |
|---|---|
| `inventory` key on an entry, no inventory configured | Error at load and in the doctor, naming both configuration places (§8). |
| `creds_file` configured and a record carries `creds` | Error naming the key (§9.4). |
| `creds_file` group/world-readable | Doctor warning naming the mode. |
| A mapped NetBox custom field has the wrong type | Record validation error naming the device and the field (§9.2). |
| Unrelated NetBox custom fields | Ignored; opt-in to `extra` via `extra_custom_fields` (§9.2). |
| `~/.otto/settings.toml` carries a repo-only table | `UserSettingsModel` error naming the key (§8). |
| Unknown key | `InventoryKeyError` → `LabRepositoryError` with file/element/index/key/label (§6). |
| Supplied field inline beside `inventory` | Error on the raw entry naming the field and the key (§5). |
| Unsupplied field inline beside `inventory` | Fine — it is otto-owned in this deployment. |
| Record carries a field outside `supplies` | `InventoryError` naming the key and the field (json backend; conformance rule for others). |
| A custom field is mapped after lab files carried the field inline | Every such entry errors naming the field — the explicit migration signal; fix the lab files or unmap. |
| Record `element_id` ≠ element `id` | Error naming file, element, key and both values (§2). |
| Record lacks a field the host class requires (`creds` for unix) | The host spec's own validation error, prefixed with the key. |
| Record field is `None` | Not stated; the entry's default applies. |
| Record `extra` | Copied onto `host.inventory_ref.extra`; never a host field. |
| Two active repos declare different `[inventory]` tables | Bootstrap error naming both repos. |
| json inventory file malformed / a record fails validation | `InventoryError` naming the file, the key and the field. |
| NetBox: duplicate device names in the filtered set | Error at fetch naming both device ids. |
| NetBox: device without an address at `ip_source` | Skipped from `records` and `list_keys()` (`ip` is required); a reference to it errors naming the device, its id and `ip_source`, and `otto inventory list` reports the skip (§9.2). |
| NetBox: device NetBox left unnamed | Skipped (keyed by name they would all collide); reported by id alongside the addressless ones (§9.2). |
| NetBox unreachable / bad token, snapshot present | Snapshot used, warning names its age (§9.5). |
| NetBox unreachable / bad token, no snapshot | `InventoryError` naming the URL; the load fails — a referenced host cannot be built without it. |
| Snapshot older than `cache_ttl`, NetBox reachable | Refetched and rewritten atomically; no message. |
| `cache_ttl = "0"` | No snapshot; every process fetches; `fingerprint()` is `None`. |
| Snapshot cache directory unwritable | On the read path, a warning naming the path and the load proceeds from the wire — everything under otto's home is derived state, and a cache otto only uses to go faster must not fail a load. `otto inventory refresh` fails naming the path: its whole product is the written snapshot (§9.5). |
| Backend supplying `creds` under a cache | Construction error naming the declaring file and the backend; use `creds_file`, or `cache_ttl = "0"` (§9.5). |
| Inline entry (no `inventory`) | Unchanged from v2; the feature is opt-in per entry. |
| Container / builtin `local` | Empty `InventoryRef()`. |
| Composite override of an element whose hosts reference the inventory | Unchanged: the element replaces wholesale; each side's hosts resolve their own keys. |
| Completion with a non-fingerprintable inventory | Correct, uncached, slower; documented. |

## 14. Testing

Every guard below is written red-first and proved by mutation (the standing
rule: a test that cannot fail is a defect).

- `tests/unit/models/test_inventory_record.py`: field set is host-spec
  names (a guard asserting every `InventoryRecord` field except `extra` is a
  `HostSpec` field — a plain subset check with no exceptions, now that §4's
  carve-out has retired; the guard as first written named the two exempt
  fields AND asserted the exemption was still real, which is what made the
  widening announce itself here rather than pass silently);
  `FILLABLE_INVENTORY_FIELDS` derived, not listed; digit-string
  coercion on `site`/`rack`; `extra="forbid"`.
- `tests/unit/inventory/test_resolve.py`: `resolve_host_entry` fill, the
  inline-forbidden error (parametrised over every supplied field, including a
  field added after the test was written — the guard iterates the frozen set),
  an unsupplied field inline beside a reference is accepted and used, a
  record field outside `supplies` is refused, `supplies` without `ip` is
  refused at construction,
  the `element_id` cross-check both ways, an absent / `None` / unset field =
  not stated, an empty `inventory` string refused; and, in `tests/unit/host/`,
  that provenance is stamped before providers run (a provider that reads
  `host.inventory_ref.key` sees it — the `lab_info` post-hoc lesson) and that
  each factory entry point refuses an unresolved entry naming the key.
- `tests/unit/labs/`: the json backend threads `inventory` to load and to
  summaries; the composite forwards it; a referenced entry's summary carries
  the inventory `ip`; the not-found error text.
- `tests/unit/inventory/`: json backend (parse once, comment keys, bad record
  names key+field, fingerprint changes on rewrite); netbox backend against a
  **local stub** (`http.server` serving canned paginated responses; the real
  `pynetbox` client talks to it — no network, no mocking of pynetbox itself):
  mapping table row by row, prefix stripping, `ip_source` variants,
  unracked/unset → `None`, duplicate names, missing address, `supplies` =
  native set plus exactly the mapped custom fields (an unmapped custom
  field present on the device is ignored — mutation: reading it must turn
  the test red), wrong-typed mapped field names device and field, `filter`
  forwarded, auth failure → `InventoryError`; `build_inventory` resolution order and the
  two-repo conflict.
- `tests/unit/cli/`: `otto inventory lookup/list`; doctor dead-reference
  problem, orphan warning, "no inventory configured" problem; template drift
  guard picks up `#[inventory]`.
- `otto.testing.assert_inventory_conforms(inv, *, expected_keys=None)`: the
  `assert_lab_repository_conforms` shape — protocol satisfied, `lookup`
  idempotent, unknown key raises `InventoryKeyError`, `list_keys()` entries
  all resolve, `fingerprint()` is `str | None`. Each `expected_keys` entry
  must resolve **and** appear in `list_keys()`: a backend that answers a
  lookup for a key it never lists is a backend the doctor's orphan check and
  every enumerating verb see a different inventory through. And, given a
  `LabRepository`, a positive control: a lab whose one host references a key
  **fails** to load without the inventory and **loads** with it (a backend
  that ignores `inventory=` fails this).
- **Referenced ≡ inline**: `tests/_fixtures/lab_data/tech1-inventory/`
  (§20's files) loads the `unix` lab through a json inventory + creds file,
  and every host compares equal — id, address, creds, interfaces, options —
  to the inline `tech1` build, apart from `host.inventory_ref` itself. A
  mutation that drops one fill (say `interfaces`) turns it red. The inline
  `tech1` fixture stays the hermetic one every other test uses, which is why
  the inventory fixture carries *its* values rather than tidier invented ones
  (§20).
- `creds_file` overlay: supplies creds; a record carrying `creds` beside it
  errors naming the key; the doctor's mode warning.
- `export` / `diff`: round-trip (`export` then `diff` is empty), a changed
  field and a missing key each show up, creds never appear in either.
- Pinned identities (`test_v2_equivalence.py`, folded per §17) stay green:
  location fields and `inventory` never enter the id.

## 15. Documentation

- New `guide/configuration/inventory.md`: the two layers and the rule, the key
  and the bridge rule, the record fields (a table the completeness guard reads
  — extend `test_lab_config_field_coverage.py` with an `InventoryRecord` row
  pointed at this page), configuration and resolution order (including the
  new user-level `~/.otto/settings.toml`), the json format, `creds_file`, the
  NetBox mapping and custom-field policy, the doctor findings, the verbs, the
  completion note, **the adoption path (§19) as three subsections**, and
  **the worked example (§20)** — the same files, kept in sync with the
  `tech1-inventory` fixture by a guard that loads the page's JSON blocks
  through the real parsers and compares them to the fixture files.
- `lab-config.md`: rows for `inventory`, `site`, `rack`, `shelf`; a short
  "Referencing the inventory" section under the host entry; the migration
  note is unchanged (no existing file breaks).
- `host-sources.md`: one paragraph pointing at the inventory page — sources
  compose *records*, the inventory supplies *fields*; the two never overlap.
- New `library/inventory-backends.md`: protocol, registry, conformance helper,
  the secrets seam, with the json backend as the worked example.
- `settings.md`: the `[inventory]` table; `getting-started.md` unchanged (the
  overhaul spec owns it; inline hosts remain the getting-started shape).
- `docs/api/`: pages for `otto.inventory` (protocol, errors, backends),
  `otto.models.inventory`, `otto.host.inventory_ref` — the nitpicky build
  fails on an unpublished cross-reference, so they land with the code.

## 16. Dependencies

`pynetbox` becomes a runtime dependency in `pyproject.toml` (with `uv lock`);
`requests` and its transitive wheels join the air-gap wheel matrix, which
`make release` re-measures. The import is lazy inside `otto.inventory.netbox`;
the import-budget **caps** must not move for any existing verb (a raised cap
is a regression, not a fix). Two modules do join every surface's snapshot, by
design and within the caps: `otto.host.inventory_ref`, since `InventoryRef` is
a field on every host class (§7), and `otto.models.inventory`, since
`cache_ttl`'s grammar is validated at the settings boundary and a boundary
model may not import a runtime package. Both are leaf-light, and the budget
check stays green.

## 17. Ride-along: the `_v2` test modules

There is no v1 path, so the suffix is a plan artefact: fold
`test_json_repository_v2.py` into `test_json_repository.py`,
`test_composite_v2.py` into `test_composite.py`, and rename
`test_v2_equivalence.py` → `test_pinned_identities.py`. `lab_json_v2` keeps
its name: its *input* is still v1-shaped flat dicts, which is what the suffix
describes. The lab flexibility todo's "still open" half is closed by v2's
`metadata`, so the file goes with this work, and the two older specs that
cited it point at the multi-source spec instead.

## 18. Decisions

| Question | Decision | Where |
|---|---|---|
| Per-field composition? | No — a join across disjoint vocabularies; data in one layer, keys cross-checked | §2 |
| Primary key | One opaque inventory-owned string per host entry; bridge keys = future NetBox names | §3 |
| Grain | One record per otto host | §1 |
| Record field names | 1:1 with `HostSpec`; no mapping in core | §4 |
| Mixing inline and referenced fields | Error on the raw entry | §5 |
| Join site | The lab loader, once per entry (`resolve_host_entry`); the inventory threaded like `preferences`; the factory refuses an unresolved entry; protocol change accepted | §6 |
| Provenance | `host.inventory_ref: InventoryRef` (the `inventory` field stays the key string) | §7 |
| Scope | User-level `~/.otto/settings.toml` (new, general), per-project override, exactly one per process; no implicit discovery | §8 |
| NetBox client | `pynetbox`, plain runtime dependency, no extras | §9.2, §16 |
| NetBox fetch strategy | Whole filtered set, cached under `~/.otto` with a daily default TTL; stale snapshot served with a warning when NetBox is down | §9.2, §9.5 |
| What NetBox must provide | A device named as the key, an address, a read-only token — nothing else; `supplies` = native fields + only the custom fields you map | §9.2 |
| "From NetBox if the custom field exists, else the lab file"? | No — that is per-field precedence; the partition is declared in configuration, and changing it is an explicit, doctor-checked act | §2 |
| Unrelated NetBox custom fields | Ignored; opt-in extras | §9.2 |
| Credentials | `creds_file`, core, both backends; a record carrying `creds` beside it is an error | §9.4 |
| Transition tooling | `otto inventory export` / `diff` | §11, §19 |
| Location fields | `site`/`rack` `int \| str`, `shelf`/`slot` `int`, `board` `str`; flat | §12 |
| `element_id` | A key: asserted in both layers, must agree, never filled | §2 |
| Remote-inventory completion | Uncached when not fingerprintable; json snapshot advised | §11 |
| `otto inventory` verbs | `lookup KEY`, `list`, `export PATH`, `diff PATH [PATH]`, `refresh` — all read-only against the inventory | §11 |

## 19. Adoption path

Three stages, each with the verb that proves it. No lab file's *references*
change between stages — that is the whole point of §3's bridge rule. The one
thing that can move is a field the new backend does not supply, which stage 2
names explicitly (§19.2).

### 19.1 Stage 1 — the JSON inventory

- **One file**, owned like code: its own small repository (or a directory
  the lab team reviews changes to), a `$schema` line, sorted keys —
  `otto inventory export` normalises it. Split only along *ownership* lines
  (who edits it), never along otto lines (which project reads it); the json
  backend reads one `path`, and one file is the bridge's virtue.
- **Keys** are the device's canonical name — the name people already use for
  it, the DNS hostname where one exists — and are the names the NetBox
  devices will carry (§19.2). Never an IP, never an otto host id, never
  something that encodes location. Decide the naming scheme once, in the
  file's README, before the first entry.
- **Records carry facts only** (§4): the management `ip`, test-network
  `interfaces`, `is_virtual`, the location fields where known, versions as
  the declared state. Nothing otto-specific ever goes in — if a value only
  matters to otto, it belongs in the lab file; if you cannot decide, it is a
  fact (the inventory) unless it names an otto behaviour.
- **Declare `supplies` to match what NetBox will supply** (§9.2: the native
  set, plus any custom field you intend to map): the lab files are then
  already in their stage-2 shape, and `otto inventory diff` compares like
  with like. Leaving `supplies` at its default (everything) is fine for a
  file-only future; it just means more re-homing later.
- **Credentials in `creds_file` from day one** (§9.4), mode `0600`, outside
  every repository. The inventory file then holds no secrets and can be
  shared, diffed and reviewed; stage 2 migrates nothing.
- **Checks**: `otto inventory list` (parses, counts), `otto init` in each
  project (dead references, orphan records), `otto inventory lookup KEY`
  when a host builds oddly.

### 19.2 Stage 2 — moving to NetBox

1. In NetBox, make sure every device that will be referenced exists with
   `name` **equal to its inventory key**. Rename the NetBox device, never the
   key: the key is what lab files reference.
2. Decide what NetBox supplies. The native set costs nothing: an address
   `ip_source` can read (primary IPv4 by default), and whatever of site /
   rack / position / device type / platform is already filled in. **Map a
   custom field only if NetBox already has one you want otto to read**, or
   you have decided to add one — `custom_fields = { sw_version = "<existing
   name>" }`. Everything unmapped stays in the lab files, exactly as in
   stage 1 when `supplies` was declared to match (§19.1). Existing unrelated
   custom fields need nothing (§9.2).
3. Mind the fields NetBox does **not** supply, `interfaces` first: this
   backend fetches devices, not their interfaces, and no custom field may be
   mapped to that field (§9.2). A stage-1 file that owned `interfaces`
   therefore hands them back to the lab entries that reference it at this
   step — the one re-homing the bridge cannot avoid, and the reason §19.1
   suggests declaring `supplies` to match NetBox from the start.
4. Pick a `filter` that selects the lab devices — a site, a role, a tenant, a
   tag; whatever NetBox already distinguishes them by. It bounds the fetch;
   it is not an otto convention.
5. If you did map custom fields, populate them from the JSON file (a script
   over `otto inventory export` output is the obvious tool; otto does not
   write to NetBox — §19.3).
6. Point `[inventory]` in `~/.otto/settings.toml` at NetBox; keep
   `creds_file` exactly as it was. (During the fractured phase a project can
   do this alone with its own `[inventory]` override while others stay on
   the file.)
7. Run `otto inventory diff ~/lab/inventory.json`. Iterate in NetBox until
   the only differences left are the ones you intend — the re-homed
   `interfaces` of step 3, and nothing else — the field-by-field table tells
   you exactly what to fix. `diff` exits 0 for no differences, 1 for
   differences and 2 when it could not answer at all (§11), so a scripted
   gate cannot mistake a mistyped path for a clean migration. Then run
   `otto init` in every project: zero dead references.
8. Keep `otto inventory export ~/lab/inventory.json` as the periodic,
   *reviewable* snapshot for stage 3 — the offline fallback and fast
   completion are handled by the automatic cache (§9.5) without it.

Credentials move nowhere, keys move nowhere, and no lab entry's `inventory`
reference changes: what step 3 hands back to the lab files is the one field
the new backend stopped supplying.

### 19.3 Stage 3 — keeping NetBox otto-healthy

Two things otto actually depends on, then practices that help. Nothing here
asks NetBox to change its shape for otto.

Depended on:

- **The device name is the key.** Treat it as immutable. Retiring a device
  (a status outside your `filter`, or deleting it) surfaces as a dead
  reference in every project's doctor — the correct signal, delivered where
  the fix is. A rename is a retirement plus a new device plus a deliberate
  lab-file edit; the friction is intentional.
- **The address `ip_source` reads stays filled in.** A device that loses it
  drops out of the fetch; a host referencing it fails to build, naming the
  device and `ip_source`, and `otto inventory list` reports the skip (§9.2).

Helps, if you want it:

- A NetBox custom validation rule that refuses to save a device in your
  filter without the address — the UI then catches what otto would report
  later. Optional; otto reports it either way.
- If you map custom fields, keep them typed as the mapping expects
  (`element_id`/`slot` integer, versions text); grouping them is purely for
  the NetBox UI.
- **Versions are declarations.** What a device is found to be running is an
  observation; when it disagrees with NetBox, update NetBox by hand. otto
  never writes to NetBox, and this spec keeps it that way — the inventory is
  the source of record, not a cache of observations.
- **Read-only, per-user API tokens** via `token_env`; never a shared token in
  a file.
- **Review NetBox change through otto's eyes**: `otto inventory diff` against
  the last snapshot shows what changed in NetBox since; `otto init` in CI
  (against NetBox, or against the snapshot for hermetic runs) catches a dead
  reference before a run does.

## 20. Worked example — the `unix` lab

The `tech1` fixture's `unix` lab (three virtual hosts, `test1` also a member
of `busybox`), as it is specified under this spec. A fixture
`tests/_fixtures/lab_data/tech1-inventory/` carries these files, and an
equivalence test proves the referenced form and the inline `tech1` form build
**identical** hosts (§14) — the inline fixture stays the hermetic one every
other test uses. Because that equivalence must hold *exactly*, the records
below carry `tech1`'s own values rather than tidier invented ones: its real
credentials, `test1`'s second interface, and no `os_name` (the inline entries
state none, and a field the record stated would be a field the two forms
disagree on). The fixture's settings file spells its two paths relative to
itself, so the tree is checkout-independent; the paths below are how a real
deployment writes them.

`~/lab/inventory.json` — facts, no otto vocabulary, no secrets:

```json
{
  "$schema": "~/.otto/schemas/inventory.schema.json",
  "_note": "keys are the device names NetBox will carry; never rename a key",
  "test1": {
    "ip": "10.10.200.11",
    "interfaces": { "eth2": { "ip": "192.168.1.11", "subnet": "192.168.1.0/24" },
                    "bbeth-1350": { "ip": "198.51.100.18", "subnet": "198.51.100.16/30" } },
    "is_virtual": true,
    "site": "lab-a", "rack": 1, "shelf": 3,
    "extra": { "asset_tag": "VM-0011" }
  },
  "test2": {
    "ip": "10.10.200.12",
    "interfaces": { "eth2": { "ip": "192.168.1.12", "subnet": "192.168.1.0/24" } },
    "is_virtual": true,
    "site": "lab-a", "rack": 1, "shelf": 3
  },
  "test3": {
    "ip": "10.10.200.13",
    "interfaces": { "eth2": { "ip": "192.168.1.13", "subnet": "192.168.1.0/24" } },
    "is_virtual": true,
    "site": "lab-a", "rack": 1, "shelf": 4
  }
}
```

`~/.otto/creds.json` — the one home for credentials, mode `0600`, keyed by
the same keys:

```json
{
  "test1": [ { "login": "vagrant", "password": "vagrant" }, { "login": "test", "password": "Password1" } ],
  "test2": [ { "login": "vagrant", "password": "vagrant" }, { "login": "test", "password": "Password1" } ],
  "test3": [ { "login": "vagrant", "password": "vagrant" }, { "login": "test", "password": "Password1" } ]
}
```

`~/.otto/settings.toml` — declared once per user. `supplies` is set to what
NetBox will natively supply (§19.1), so the lab file below is already in its
final shape; `os_version`, `sw_version` and the rest stay lab-file fields
in this deployment:

```toml
[inventory]
backend = "json"
path = "~/lab/inventory.json"
creds_file = "~/.otto/creds.json"
supplies = ["ip", "interfaces", "is_virtual", "site", "rack", "shelf", "board", "os_name"]
```

`lab_data/lab.json` in the project — otto's business only: which labs
exist, which element joins which, and how otto should talk to each host.
Every host entry is a reference plus otto-owned fields; nothing here is an
address or a credential:

```json
{
  "$schema": "../.otto/schemas/lab.schema.json",
  "labs": {
    "unix":    { "resources": ["test1", "test2", "test3"], "metadata": { "description": "unix regression bed" } },
    "busybox": { "resources": ["test1", "bb1161", "bb1211", "bb1281", "bb1310", "bb1350"] }
  },
  "elements": [
    { "name": "test1", "labs": ["unix", "busybox"], "metadata": { "role": "hub" },
      "hosts": [ { "inventory": "test1", "os_type": "unix", "docker_capable": true,
                   "valid_terms": ["ssh", "telnet"], "valid_transfers": ["scp", "sftp", "ftp", "nc"] } ] },
    { "name": "test2", "labs": ["unix"],
      "hosts": [ { "inventory": "test2", "os_type": "unix", "docker_capable": true,
                   "valid_terms": ["telnet", "ssh"], "valid_transfers": ["nc", "scp", "sftp", "ftp"] } ] },
    { "name": "test3", "labs": ["unix"],
      "hosts": [ { "inventory": "test3", "os_type": "unix", "docker_capable": true,
                   "valid_terms": ["ssh", "telnet"], "valid_transfers": ["scp", "sftp", "ftp", "nc"] } ] }
  ]
}
```

How it correlates at load: `test2`'s element flattens to
`{"element": "test2", "inventory": "test2", "os_type": "unix", ...}`;
`resolve_host_entry` looks up `test2`, finds no inventory-owned key inline
(had the entry also said `"ip": ...`, that is the §5 error), copies `ip`,
`interfaces`, `is_virtual`, `site`, `rack`, `shelf` onto the entry, and the
`CredsOverlay` supplies `creds` from `creds.json`; `UnixHostSpec` then
validates the whole thing exactly as an inline entry, `make_host_id` yields
`test2` from the element as before, and the host carries
`inventory_ref=InventoryRef(key="test2", backend="json:~/lab/inventory.json")`.
The busybox guests (`bb1161`…) stay inline in this example — they are QEMU
guests behind `test1` with no life outside this bed, which is exactly the
case the inline form is for.

Moving this deployment to NetBox starts with one file,
`~/.otto/settings.toml`:

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"
filter = { site = "lab-a", status = "active" }
creds_file = "~/.otto/creds.json"
```

…with devices named `test1`/`test2`/`test3` carrying primary IPv4s, site
`lab-a`, rack `1` and positions `3`/`3`/`4` — all things NetBox models
natively; no custom field is mapped. `supplies` is then the **native** set,
which does not include `interfaces`, so this deployment's test-network
interfaces move back into the lab entries at that step (§19.2) and every
reference in the lab file stays exactly as it is.
`otto inventory diff ~/lab/inventory.json` is what shows you that, field by
field, before you switch; run it until only the differences you intend are
left. Had NetBox happened to carry a `sw_version` custom field worth reading,
`custom_fields = { sw_version = "sw_version" }` would add it to `supplies`,
and the doctor would then point at any lab file still stating it inline.

## 21. Open questions

- **~~Should `hw_version` and `sw_version` move to the base `HostSpec`?~~**
  **Answered: yes.** They are base fields now, and §4's carve-out is gone. The
  cost the question weighed — two more fields on a family with no use for them
  — was mispriced: an embedded target has a firmware version as surely as a
  Unix box has a distro one, and `ZephyrHost` was already the odd class out
  for lacking the attribute its siblings had. Both are declared beside
  `os_version` on `HostSpec` and on `RemoteHost`'s shared runtime contract,
  with real dataclass fields on `EmbeddedHost` (the drift guard
  `test_host_spec_fields_match_runtime_init` is bidirectional and would
  otherwise refuse the widening). They remain otto's own vocabulary rather
  than native inventory facts: neither joins any backend's default `supplies`,
  so an inventory hands them over only where a deployment maps a custom field
  — exactly `os_version`'s status, which is the precedent this follows.
