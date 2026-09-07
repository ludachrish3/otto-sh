# Creds store backend, layered creds, and the three-file `otto init` scaffold

**Date:** 2026-09-06
**Status:** Implemented (branch worktree-creds-store)
**Depends on:** `2026-08-28-host-inventory-layer-design.md` (shipped; this spec
supersedes its §9.4 and reverses its §8 "`otto init` does not scaffold an
inventory"), `2026-09-05-element-object-design.md` (shipped; the `Element`
the join receives), `2026-07-04-login-proxy-and-app-shell-design.md` (the
`proxy`/`via`/`params` half of a cred)

## 1. Goal

Three things, one shape:

1. **Credentials get their own backend seam.** `creds_file` today is a core
   `[inventory]` kwarg naming one JSON file. It becomes a `[creds]` table
   beside `[inventory]` and `[[lab.sources]]`, selecting a registered
   **creds store** by name, with `json` as the one first-party store and a
   registry for third parties — a vault, a secrets manager, a team database.
2. **Creds compose across three layers, by login, with one priority order.**
   A cred entry (`login`, `password`, `proxy`, `via`, `params`) may appear in
   the lab file, in an inventory record, and in the creds store. Within a
   login, every field comes from the highest layer that states it: lab file
   over inventory over store. This is deliberately lax — the layers may
   overlap — because one rule that every layer follows is easier to learn and
   to migrate into than a per-field partition, and the inventory spec's own
   rule ("data lives in exactly one layer") is kept for every *other* field.
3. **`otto init` scaffolds the three-file shape.** `lab_data/lab.json` with a
   referenced host, `lab_data/inventory.json`, `lab_data/creds.json`, and a
   `settings.toml` with live `[inventory]` and `[creds]` tables — so a new
   repo starts in the shape the inventory design wants it to end up in, and
   the docs describe how the three files compose.

### In scope

- `[creds]` settings table, resolution, cross-repo agreement, anchoring (§4).
- `otto.creds`: the `CredsStore` protocol, the `json` store, the registry, the
  conformance helper, the JSON schema (§5).
- The by-login merge, applied twice (store→record, record→lab file), with its
  ordering and errors (§6).
- Doctor findings, completion-cache fingerprinting, `otto inventory` verbs (§7).
- The `otto init` scaffold: three files, live tables, README (§8).
- Docs: the composition prose, the `[creds]` reference, the third-party page,
  the getting-started twin gaining the proxied cred (§9).
- Removal of `creds_file` with a pointing error (§4.4).

### Out of scope

- A second first-party store. The seam is designed against the json store and
  documented for third parties; a vault backend is a follow-up with a real
  vault in front of us.
- Any change to the inventory backends, the snapshot cache, or NetBox.
- Per-host or per-lab creds scoping. The store is keyed by inventory key, full
  stop — a host with no inventory key carries its creds inline as today.
- Rotating, writing, or caching secrets. Otto reads a store; it never writes
  one, and a snapshot never holds a credential (unchanged, §9.5 of the
  inventory spec).
- Tightening the priority order later (a strict partition, a "store wins"
  switch). The order is expected to shift as otto develops; this spec picks
  the lax version and says so in the docs.

## 2. Vocabulary

| Term | Meaning |
| ---- | ------- |
| **lab file** | `lab.json`: labs, elements, host entries, links. otto-owned. The **highest** creds layer. |
| **inventory** | The configured `Inventory` backend: machine facts by **inventory key**. The **middle** creds layer, when its records carry `creds`. |
| **creds store** | The configured `CredsStore` backend: `{inventory key: [CredSpec, ...]}`. Optional. The **lowest** creds layer. |
| **inventory key** | One opaque string per referenced host entry, the join key across all three layers. |
| **cred entry** | One `CredSpec`: `login` (required), `password`, `proxy`, `via`, `params`. The same shape in every layer. |
| **stated** | A field present in an entry with a non-`null` value. `null` states nothing, in every layer (the inventory spec's R7, one level down). |

## 3. Three files, one key, one order — the composition rule

```text
lab_data/lab.json                 inventory.json               creds.json (optional)
────────────────────────          ─────────────────────        ──────────────────────
elements[].hosts[]:               "device-01.lab.example":     "device-01.lab.example": [
  inventory: "device-01.lab…"  ──►  ip, interfaces, site,  ──►   {login, password},
  os_type, valid_terms,             rack, shelf, board, …          {login, password}
  valid_transfers, hop, …           creds: [...] (optional)     ]
  creds: [ {login, proxy, via} ]
  (optional)
        HIGHEST  ───────────────────►  middle  ─────────────────►  LOWEST
```

Three statements, each of which the loader enforces:

1. **The inventory key is the only thing the files share.** A host entry that
   says `"inventory": "<key>"` gets its machine facts from the inventory
   record under that key and its creds from the creds store under that same
   key. Nothing else — not an IP, not an element name, not a host id — is ever
   used to line the files up.
2. **The creds store is optional; the inventory is not, for a referenced
   entry.** Without `[creds]`, creds come from the inventory record and the
   lab file; without `creds` in the record, from the store and the lab file;
   with neither, from the lab file alone. Inline hosts (no `inventory` key)
   carry their creds inline as they always have. `[creds]` without
   `[inventory]` is a bootstrap error: the store is keyed by inventory keys,
   so nothing could ever look one up (§4.3).
3. **Entries are matched by `login`, and within a login the highest layer
   that states a field wins.** The join key is the inventory key, then the
   login — never a list position; `login` is required on every entry in every
   layer, which is what makes it the key. Lab file over inventory over store,
   field by field. Order matters only for the *result*: otto treats the first
   cred as the default login and the first proxy-less cred as the default
   `via`, so the merged list is sequenced — and **the lab file's order is the
   order**, because the lab file is the one layer written with otto's
   default-login rule in mind; an inventory or a store lists logins in
   whatever order the data was entered. Lab-file logins come first in
   lab-file order, then record-only logins in record order, then store-only
   logins in store order. A lab file that wants to fix the order of logins it
   does not otherwise touch lists them by `login` alone — a login-only entry
   is a placeholder whose fields all come from the layers below.

`creds` is therefore the one host field the inventory partition **composes**
rather than **refuses**: a referenced entry may state `creds` inline even when
the inventory supplies it, and an inventory record may carry `creds` even when
a store is configured. Every other supplied field keeps the inventory spec's
rule — stated inline beside a reference, it is an error naming the field.

Why lax, and what it costs: a per-field precedence goes silent the day both
sides state the field — a password set in the lab file hides the store's with
no error anywhere. The inventory spec refused that for machine facts and this
spec keeps the refusal for them. For creds the trade is made the other way,
on purpose: one order that every layer obeys is simpler to teach and lets a
team move creds between layers one entry at a time, without the load failing
in between. The docs say this in as many words. One consequence is worth its
own sentence there: a `proxy` names project code — a login proxy an `init`
module registers — so an inventory or store shared across projects that
carries a route loads only in projects that register that proxy; the doctor
names the cred and the missing proxy.

### 3.1 What keeps working

Every deployment shape that loads today loads under this spec:

| Shape | Under this spec |
| ----- | --------------- |
| Inline `lab.json` (own `ip`, own `creds`), no inventory, no store | Unchanged. The entry never enters the join; `otto init` detects the lab area and scaffolds nothing into it. |
| `lab.json` + inventory whose records carry `creds`, no store | Works. Record creds, lab-file entries layered over them by login. |
| `lab.json` + inventory that supplies no `creds` (NetBox, or json with `supplies` excluding it), no store | Works. Inline `creds` on the referenced host are ordinary inline creds, as today (§6.3). |
| `lab.json` + inventory + `creds_file` | The settings key errors, pointing at `[creds]` (§4.4); same file, one table move. The file's contents need no change — routes in it are allowed. |
| A referenced host with `creds` inline while the inventory supplies `creds` | **Was an error; now composes** (§6). The one behavioural relaxation. |

## 4. Configuration — `[creds]`

### 4.1 The table

```toml
[inventory]
backend = "json"
path = "lab_data/inventory.json"
supplies = ["ip"]

[creds]
backend = "json"                  # a registered creds-store name
path = "lab_data/creds.json"      # json store kwarg; anchors like every settings path
```

- `CredsConfigSpec(OttoModel)`: `backend: str`, `extra="allow"` for the
  store's own kwargs (the `[inventory]` / `[reservations]` precedent).
  `SettingsModel.creds: CredsConfigSpec | None = None` and
  `UserSettingsModel.creds` likewise.
- `backend` is otto's; every other key belongs to the selected store and is
  validated knowing which one that is (`compile_creds`, the
  `compile_inventory` precedent): the json store takes `path` (required, a
  non-empty string); any other key is an error naming it. A third-party store
  validates its own kwargs in its constructor; otto wraps a `TypeError` /
  `ValueError` into an error naming the settings file and the backend.
- A relative `path` anchors to the directory of the settings file that
  declared it — the repo root for a project table, `~/.otto` for the user
  file — the rule `[inventory]` already follows, and for the same reason.

### 4.2 Resolution and agreement

`[creds]` lives in the same two homes as `[inventory]` and resolves the same
way, first hit wins: an active repo's `.otto/settings.toml`, else
`~/.otto/settings.toml`, else none. It resolves **independently** of
`[inventory]`: a user file may declare both while a project overrides only
`[inventory]`, and vice versa. Each table finds its own first declaration.

When more than one active repo declares `[creds]`, the tables must be
identical after anchoring — same backend, same kwargs — or bootstrap fails
naming both files. Agreement is checked **per table**: the `[inventory]`
tables among themselves (as today), the `[creds]` tables among themselves
(`CompiledCreds.same_as`, the same origin-excluding comparison). The one
resolved inventory and the one resolved store then combine into
`CompiledInventory.creds: CompiledCreds | None` (replacing `creds_file: Path
| None`), which `construct_inventory` reads to build the overlay.

### 4.3 `[creds]` without `[inventory]`

An error at bootstrap naming the declaring file: "`[creds]` is keyed by
inventory key and no `[inventory]` is declared in either settings file; declare
one, or remove `[creds]`". A store nothing can look up is a configuration
that silently does nothing, which is the failure mode this whole layer
exists to refuse.

### 4.4 `creds_file` is removed

`InventoryConfigSpec` drops the field. Because that model is `extra="allow"`,
a stray `creds_file` would otherwise be swallowed as a backend kwarg and
surface as the json backend's "unknown key" error — or, for a third-party
backend, be passed to a constructor that never asked for it. So the model
carries an explicit validator: `creds_file` present anywhere in the table is
a `ValueError` reading "`creds_file` has moved: declare `[creds] backend =
"json"` / `path = "<the same path>"` beside `[inventory]` (see
docs/guide/configuration/inventory.md)". No alias, no deprecation window —
nothing outside this repository depends on the key, and two spellings of one
thing is a way for them to disagree.

## 5. `otto.creds` — protocol, json store, registry

A new package, one-way: `otto.inventory` imports `otto.creds`; `otto.creds`
imports only `otto.models`, `otto.registry`, `otto.utils`, `otto.errors`.
Declared in `tach.toml`. Because `otto.inventory` sits on the bootstrap path,
every `otto.inventory` import of `otto.creds` is **function-local** (the
discipline `otto.inventory.config`'s docstring already imposes on
`otto.config`): the overlay type-annotates the store under `TYPE_CHECKING`
and imports nothing from the package at module scope. The import-budget
caps therefore see no new module until a store is actually constructed.

### 5.1 The `CredsStore` protocol

```python
@runtime_checkable
class CredsStore(Protocol):
    @property
    def label(self) -> str: ...                     # e.g. "json:/home/me/lab_data/creds.json"
    def lookup(self, key: str) -> list[CredSpec]: ...       # [] when the key has no entry
    def list_keys(self) -> "list[str] | None": ...          # sorted; None = cannot enumerate
    def fingerprint(self) -> "str | None": ...              # changes when entries may have; None = not cacheable
```

plus the optional `SupportsStatPaths` capability the inventory protocol
already defines, reused rather than redeclared.

- **Entries are `CredSpec`, the same model the lab file uses.** A store may
  carry routes. The first-party json store does; a vault backend will return
  `login` and `password` only, and the lab file adds the route (§6).
- **`lookup` returns `[]` for an unknown key, and raises for a store that
  cannot answer.** A missing entry is not an error at the store: an embedded
  host needs no creds, and whether a unix host without any is a problem is
  the host spec's call (`min_length=1`), made after the merge. A store that
  cannot be read — file missing, unparsable, service down — raises
  `CredsError` (`otto.creds.errors`, an `OttoError`) naming the store label.
  `CredsOverlay.lookup` is the one place a store is consulted at load, and it
  re-raises a `CredsError` as an `InventoryError` carrying the same text, so
  the lab loader's existing file / element / index prefixing and the doctor's
  existing catch both apply with no new sites.
- **`list_keys` may be `None`.** A vault that can hand out an entry by name
  but not enumerate names says so, and the doctor's orphan check (§7.1)
  skips rather than reporting "no orphans" against an empty list. The json
  store enumerates.
- **Construction does no I/O**, the rule for every backend here: the json
  store reads its file on the first `lookup`, so a lab with no referenced
  entry never opens it.
- **Duplicate logins under one key are a store error** naming the key and the
  login — the same rule the host spec applies to a lab-file list.
- The constructor contract for a registered class is
  `cls(repo_dir=declaring_directory, **table_kwargs)` — the inventory
  precedent, with `repo_dir` meaning the directory the declaration anchored
  to.

### 5.2 The `json` store

`JsonCredsStore(path)`: exactly the file today's `load_creds_file` reads.

```json
{
  "$schema": "../.otto/schemas/creds.schema.json",
  "_comment": "Credentials by inventory key. lab.json entries with the same login override these field by field.",
  "device-01.lab.example": [
    { "login": "admin", "password": "CHANGE_ME" }
  ]
}
```

- `$schema` and `_`-prefixed top-level keys are comment space; every other key
  is an inventory key whose value is a list of `CredSpec` (error names the
  file, the key, the index, the field).
- Parsed once, on first use. `list_keys()` sorted. `fingerprint()` is the
  path, mtime and size, `|missing` when absent; `stat_paths()` is `[path]`.
  Label `json:<path>`.
- Mode `0600` advised; the doctor warns otherwise (§7.1). `otto init` writes
  it so (§8).
- `load_creds_file` moves here as `parse_creds_document(data, *, source)`;
  the old name goes.

### 5.3 Registry and conformance

- `otto.creds.registry`: `CREDS_BACKENDS: Registry[Callable[..., CredsStore]]`,
  `register_creds_backend(name, cls, *, overwrite=False)`,
  `get_creds_backend_class(name)`. Built-in `json` pre-registered at import
  through the public path.
- `otto.testing.assert_creds_store_conforms(store, *, known_key=None)`:
  protocol satisfied; `label` a non-empty str; `lookup` of the probe key
  `__otto_conformance_no_such_key__` is `[]` and never raises; with
  `known_key`, `lookup` returns a non-empty `list[CredSpec]`, idempotent
  (equal on a second call), with unique logins; `list_keys()` a sorted
  `list[str]` or `None`, and when a list, every listed key resolves to a
  non-empty list and `known_key` appears in it; `fingerprint()` `str | None`.
  Uses `ExpectCollector` like its siblings; the json store passes it in
  otto's own suite.
- `creds.schema.json` beside `inventory.schema.json`: `{key: [CredSpec]}`
  with `$schema` and `_*` comment keys allowed, built by `build_schemas`,
  scaffolded and drift-checked by `otto init --schemas` like the others; the
  VS Code wiring maps `**/creds*.json` to it.

### 5.4 `CredsOverlay` — the first merge

`CredsOverlay(inner: Inventory, *, store: CredsStore)` replaces the `path=`
constructor. It stays the outermost wrapper and still claims `creds` in
`supplies`, folds the store's `fingerprint()` and `stat_paths()` into its
own, and reads nothing until the first `lookup`. What changes is `lookup`:
instead of refusing a record that carries `creds`, it returns the record with
`creds = merge_creds(store.lookup(key), record.creds)` — the store is the
lower layer, the record the higher (§6.1). `construct_inventory` builds it
when `CompiledInventory.creds` is set. The "a backend that supplies creds is
never snapshot-cached" rule is untouched.

## 6. The merge — creds by login, twice

### 6.1 `merge_creds(lower, higher)`

One pure function in `otto.inventory.creds`, over plain dicts (the join's
currency):

```python
def merge_creds(lower: list[dict[str, Any]], higher: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compose two cred layers by login: position from *lower*, fields from *higher*."""
```

- Matching is by `login` only. For each entry take its **stated** fields —
  present and not `None`. A login named in both layers gets
  `{**lower_stated, **higher_stated}`; a login named in one gets its own.
  The result is sequenced **higher layer first**: every `higher` login in
  `higher`'s order, then every `lower`-only login in `lower`'s order (§3
  statement 3). Applied twice — store→record in the overlay, record→lab file
  in the join — this yields lab-file logins, then record-only, then
  store-only. Nothing about *which* entries merge depends on where they sit
  in a list.
- An entry whose `login` is not a non-empty string is an `InventoryError`
  ("cred entry without a 'login'") — the host spec would say the same later,
  but a merge keyed on a missing login would fold every such entry into one
  and hide the count.
- The **same login twice within one layer** is an `InventoryError` naming the
  login. Across layers it is the whole point.
- A higher layer **overrides** a stated field; it cannot **remove** one (a
  `null` states nothing, so `"proxy": null` in the lab file does not strip a
  proxy the store gave). The docs say so.
- `CredSpec` validates every merged entry afterwards (the host spec does this
  for the lab-file layer; the overlay does it explicitly for the store→record
  merge, wrapping a failure as an `InventoryError` naming the key and the
  login). Two layers each valid on their own can only produce a valid entry
  when merged — `via`/`params` travel with their `proxy` — so this catches a
  layer that was already invalid, with the key in the message.

### 6.2 In `resolve_host_entry`

When `"creds"` is in `inventory.supplies`:

- `creds` is exempt from the inline-collision check (a new
  `MERGED_INVENTORY_FIELDS = frozenset({"creds"})` beside
  `INVENTORY_KEY_FIELDS` in `otto.models.inventory`, so the rule has one
  definition the doctor, the schema and the conformance helper can all read).
- The fill for `creds` is `merge_creds(record_creds, inline_creds)`: the
  record's stated creds (already store-merged by the overlay) as the lower
  layer, the entry's own `creds` — when present and not `null` — as the
  higher. Every other supplied field is copied as today.
- Nothing else about the join changes: the collision check still runs on the
  raw entry before the fill for every other supplied field; `element_id` is
  still cross-checked; `InventoryRef` is unchanged.

### 6.3 Without supplied creds

If `"creds"` is not in `inventory.supplies` — no `[creds]`, and the inventory
does not supply creds from its own records — a referenced host's inline
`creds` are ordinary inline creds, exactly as on an inline host. The merge is
not consulted. This is the NetBox-with-no-store deployment, and it keeps
working.

### 6.4 The worked case

Store: `[{vagrant, vagrant}, {test, Password1}]`. Record: no `creds`. Lab
file: `[{vagrant}, {test}, {root, proxy: sudo-root, via: vagrant}]` — two
login-only placeholders fixing the order, then the route. Merged:
`[{vagrant, vagrant}, {test, Password1}, {root, sudo-root via vagrant}]` —
the getting-started inline lab, to the letter (§9.1). Had the lab file
listed only the route, `root` would have come first and been the default
login; that is the lab file's call to make, which is why its order wins.

## 7. Doctor, completion, verbs

### 7.1 `otto init` findings

Problems (fail the run):

- `[creds]` declared with no `[inventory]` resolvable (§4.3).
- A broken `[creds]` declaration — unknown backend, unknown kwarg, missing
  `path` — reported **once**, alongside a broken `[inventory]`, with
  referencing entries skipped for that pass (the existing rule).
- A merge error — duplicate login within a layer, an entry without a login,
  a merged entry `CredSpec` refuses — surfaces through `_validate_lab` with
  the file / element / index prefix, because the doctor resolves entries
  through the real join. A `proxy` no loaded `init` module registers surfaces
  the same way, from the host spec.

Warnings (never change the exit code):

- **Store file mode.** `creds_mode_warning` generalises: for every path the
  store's `stat_paths()` names, mode `& 0o077` warns naming the mode and the
  `chmod 600` remedy; a named path that does not exist warns too. Silent for a
  store with no stat paths.
- **Orphan creds.** Keys the store lists that the inventory does not hold
  (`store.list_keys() - set(inventory.list_keys())`), capped at ten by name
  like the inventory's own orphan warning. Skipped when either side cannot
  enumerate.

The label line gains a second row when a store resolves: `creds:
json:/…/creds.json` under `inventory: json:/…/inventory.json`.

### 7.2 Completion cache

Nothing new to compute: the process inventory's `fingerprint()` already folds
the overlay's store fingerprint in, and `stat_paths()` already appends it, so
a rotated password invalidates completion the way an edited inventory does,
and the shim's stat-only revalidation keeps working for the json store.

### 7.3 `otto inventory` verbs

Unchanged in behaviour: `lookup` still prints creds as login names only,
`export` and `diff` still exclude creds, `refresh` still peels the overlay to
reach the cache. `lookup` and `list` print a `creds:` row naming the store
label beside the `backend:` row when one is configured, so both answering
backends are visible.

## 8. The `otto init` scaffold

### 8.1 Files

`otto init --lab` (and `--all`, and the interactive "lab area") writes, never
overwriting an existing file:

**`lab_data/lab.json`** — the example element's host is referenced:

```json
{
  "$schema": "../.otto/schemas/lab.schema.json",
  "_comment": "otto lab database: 'labs' declares each lab, 'elements' groups hosts and says which labs they join, 'links' declares data-plane routes. A host that says \"inventory\" gets its machine facts from inventory.json and its creds from creds.json under that key; everything otto-specific stays here, and a creds entry here overrides the same login below it. Keys starting with _ are comments.",
  "labs": { "example_lab": { "resources": ["example-device"] } },
  "elements": [
    {
      "_comment": "Example element — replace these values. 'labs' lists regex patterns full-matched against lab names; 'hosts' are the machines it holds. The host below is REFERENCED: its 'inventory' value is the machine's own name (typically its DNS hostname), never an otto id.",
      "name": "example-device",
      "labs": ["example_lab"],
      "hosts": [
        {
          "inventory": "device-01.lab.example",
          "os_type": "unix",
          "valid_terms": ["ssh"],
          "valid_transfers": ["scp", "sftp"]
        }
      ]
    }
  ],
  "links": []
}
```

The key is deliberately **not** the element name: the docs' rule is that a key
is the machine's own name, typically its DNS hostname, never an otto id or
element name, and the scaffold should model the rule rather than the shortcut.
`.example` is the reserved documentation TLD, as `192.0.2.1` is TEST-NET.

**`lab_data/inventory.json`:**

```json
{
  "$schema": "../.otto/schemas/inventory.schema.json",
  "_comment": "Machine facts by inventory key — true whatever tool asks. Only the fields [inventory] supplies may appear; the rest stay in lab.json. Never rename a key: every lab.json entry naming it breaks.",
  "device-01.lab.example": { "ip": "192.0.2.1" }
}
```

**`lab_data/creds.json`**, written mode `0600`:

```json
{
  "$schema": "../.otto/schemas/creds.schema.json",
  "_comment": "Credentials by inventory key: the lowest layer. An entry with the same login in inventory.json or lab.json overrides these field by field. Replace CHANGE_ME; keep this file out of version control or at mode 0600.",
  "device-01.lab.example": [ { "login": "admin", "password": "CHANGE_ME" } ]
}
```

No `.gitignore` is written or edited (decided): version-control policy is the
repo owner's, and the README, the `_comment` and the doctor's mode warning
carry the advice. The scaffold's own file is `0600` so the doctor is green on
a fresh repo.

**`.otto/settings.toml`** — the `#[inventory]` commented block becomes two
live tables, with the netbox variant kept as commented prose beneath:

```toml
# --- [inventory] + [creds] — where a referenced host's facts come from ------
# A lab.json host that says "inventory": "<key>" gets the fields listed in
# `supplies` from the inventory record under that key, and its creds from the
# creds store under the same key. Creds layer by login: lab.json over the
# inventory record over the creds store, field by field. The usual home for
# both tables is ~/.otto/settings.toml (declared once per user); a table here
# overrides it for this repo. Grow `supplies` as inventory.json takes over
# more fields. See docs/guide/configuration/inventory.md.
[inventory]
backend = "json"
path = "lab_data/inventory.json"
supplies = ["ip"]

# [creds] is optional: without it, creds come from inventory records and
# lab.json alone.
[creds]
backend = "json"
path = "lab_data/creds.json"

# A NetBox inventory: the token never sits in a file.
#[inventory]
#backend = "netbox"
#url = "https://netbox.example"
#token_env = "NETBOX_TOKEN"
#filter = { site = "lab-a", status = "active" }
#cache_ttl = "24h"
```

`supplies = ["ip"]` rather than the default (every fillable field) so that a
new user who adds `interfaces` to their `lab.json` host is not refused for
stating an inventory-owned field. Widening `supplies` is the explicit act
that moves a field, and the comment says so.

The `test_init_templates.py` drift guard — uncomment-and-validate against
`SettingsModel` — covers the new live tables by construction, and the
scaffolded repo must pass `otto init` (its own doctor) with zero problems and
zero warnings in a test.

### 8.2 Detection and the doctor

- `_detect_lab` stays "`lab.json` exists": an older repo with `lab.json` alone
  is a present lab area, never re-scaffolded into. The scaffold writes all
  three files when the area is missing.
- `_validate_lab` needs nothing new: the referenced entry resolves through the
  real join against this repo's `[inventory]` and `[creds]`, so a missing
  `inventory.json` or `creds.json` is the same problem a dead reference is,
  naming the file.
- `_inventory_for` builds the store too (it already calls
  `build_inventory_from_declarations`, which gains the `[creds]` table from
  the same settings data).

### 8.3 README

`lab_data/README.md` opens with the three files and the one key, states the
composition rule in the §3 form (facts in `inventory.json`, creds in
`creds.json`, otto fields in `lab.json`, creds layered lab file over inventory
over store by login, the creds store optional), and points at
`docs/guide/configuration/inventory.md`. The per-field reference for the host
entry keeps its list but marks `ip` as "from the inventory when the entry is
referenced; inline otherwise" and `creds` as "layered — see above".

### 8.4 Next steps

Unchanged. `otto --lab example_lab --list-hosts` exercises the join on a fresh
repo.

## 9. Documentation

One home per topic; link, never restate.

- **`guide/configuration/inventory.md`** is the home. It gains a "Three files,
  one key, one order" section at the top (the §3 diagram and the three
  statements, plus the paragraph on why lax and what it costs), the `[creds]`
  table replaces `creds_file` throughout, the "Credentials" section is
  rewritten around the store, the merge, the ordering, and the errors; the
  partition rule's statement gains its one exception (`creds` composes); the
  scaffold note reverses ("`otto init` writes all three files and live
  tables; the user-level file is still where a shared inventory lives"); the
  doctor list gains the §7.1 findings; NetBox's "always uses `creds_file`"
  becomes "uses a `[creds]` store, since NetBox holds no credentials".
- **`guide/configuration/settings.md`**: a `[creds]` definition-list entry;
  the user-level section shows both tables; `creds_file` gone.
- **`guide/configuration/lab-config.md`**: the `creds` row and "Referencing
  the inventory" say that `creds` composes rather than collides, and link to
  the inventory page.
- **`guide/configuration/host-sources.md`**: the `creds` field reference notes
  that on a referenced host an inline entry layers over the inventory and the
  store by login.
- **`getting-started/defining-hosts/inventory.md`**: `[creds]` in the included
  settings; the `sudo-root` route shown inline on `test1`; the closing caveat
  ("the one thing the twin cannot carry") is replaced by the statement that
  both states carry every cred, proxied ones included, and that the guard
  proves it.
- **`library/inventory-backends.md`**: "Credentials are not your problem"
  becomes "Credentials are layered": a backend may carry them or leave them
  to `[creds]`, and points at `CredsOverlay(store=…)`.
- **New `library/creds-backends.md`**: the `CredsStore` contract table, the
  two easy-to-miss rules (no I/O at construction; `[]` for an unknown key),
  `register_creds_backend`, the constructor contract, the conformance
  helper, and a minimal example class. Linked from `library/index.md`'s
  toctree, the `registries.md` inventory table, and `extending-backends.md`'s
  see-also list.
- **`cli/inventory/index.md`**: the `creds:` row in `lookup`/`list` output;
  `creds_file` mentions become `[creds]`.
- **`cli/schema/editors.md`**: the `**/creds*.json` association.
- **`api/creds.rst`** (new, in the API toctree) and `api/inventory.rst`'s
  credentials section pointing at it.
- **CHANGELOG** (root `CHANGELOG.md`, Keep a Changelog): `creds_file` removed
  with the pointer; `[creds]` added; creds layer by login; `otto init`
  scaffolds three files.

### 9.1 The getting-started twin

`docs/examples/getting-started-inventory/`:

- `.otto/settings.toml`: `[creds] backend = "json" path = "creds.json"`
  replaces `creds_file`; gains `libs = ["../getting-started/libs"]` and
  `init = ["gs_example"]`, with a comment that the route on `test1` names
  project code the two examples share — which is the point being made.
- `lab_data/lab.json`: `test1`'s host gains
  `"creds": [{"login": "vagrant"}, {"login": "test"}, {"login": "root", "proxy": "sudo-root", "via": "vagrant"}]`
  — the two placeholders pin the order the inline lab has, the third adds
  the route.
- `creds.json`: unchanged.
- `tests/unit/docs/test_getting_started_example.py`: the twin comparison
  asserts `[(login, password, proxy, via, params)]` equal for all three unix
  hosts — the `if c.proxy is None` filter and its comment go. Mutating the
  route out of the twin's `lab.json` must fail this test.

## 10. Breaking changes

| Was | Is |
| --- | -- |
| `[inventory] creds_file = "…"` | Error pointing at `[creds]` (§4.4) |
| `CredsOverlay(inner, path=…)` | `CredsOverlay(inner, store=…)` |
| `otto.inventory.load_creds_file` | `otto.creds.parse_creds_document`; `JsonCredsStore` |
| `CompiledInventory.creds_file` | `CompiledInventory.creds: CompiledCreds \| None` |
| A record carrying `creds` while a creds file is configured: error | Composes (record over store) |
| Inline `creds` on a referenced host while the inventory supplies `creds`: error | Composes (lab file over record) |
| `otto init --lab` writes `lab.json` | Writes `lab.json`, `inventory.json`, `creds.json`; live tables |

## 11. Testing

Every test below must be shown red by the named mutation before it counts
(the repo rule: a test that cannot fail is a defect).

- **Settings** (`tests/unit/models`, `tests/unit/config`): `[creds]` parses
  in both models; `creds_file` errors with the pointer text (mutation: drop
  the validator → the key is swallowed as a kwarg); `[creds]` without
  `[inventory]` errors naming the file (mutation: remove the check → a store
  is built and never consulted); two repos with differing `[creds]` fail
  naming both (mutation: skip the `CompiledCreds.same_as` loop).
- **`otto.creds`** (`tests/unit/creds`): json store parses, refuses a bad
  entry naming file/key/index/field, refuses duplicate logins, `lookup` of an
  unknown key is `[]`, `list_keys` sorted, `fingerprint`/`stat_paths`, no I/O
  at construction (a missing file constructs and fails on first lookup);
  registry round-trip and unknown-name listing; `assert_creds_store_conforms`
  passes the json store and fails a store returning `None` from `lookup`, one
  raising on an unknown key, and one whose `list_keys` names a key that
  resolves empty.
- **The merge** (`tests/unit/inventory/test_creds_merge.py`): higher layer's
  logins first in its order, then lower-only logins; fields from the higher
  layer where both state one; a login-only placeholder takes every field
  from below; `null` does not remove; duplicate login within a layer errors;
  missing login errors. Mutations: sequence lower-first; treat `null` as
  stated; drop the per-layer duplicate set.
- **The overlay** (`test_creds.py`): record creds layered over store creds;
  a store `CredsError` re-raised as `InventoryError`; fingerprint/stat_paths
  folding; `supplies` gains `creds`.
- **The join** (`tests/unit/inventory/test_resolve.py`): inline `creds`
  beside a reference composes when the inventory supplies creds (the
  parametrised "every supplied field inline is an error" test excludes
  `creds` via `MERGED_INVENTORY_FIELDS`); passes through untouched when it
  does not; the merged list validates through `UnixHostSpec` with `via`
  naming a store login. Mutation: put `creds` back in the collision set.
- **Doctor** (`tests/unit/inventory/test_doctor.py`, `tests/unit/cli/test_init_validate.py`):
  mode warning over `stat_paths`, missing-file warning, orphan-creds warning
  capped at ten and skipped when a side returns `None`, `[creds]`-without-
  `[inventory]` problem, second label row.
- **Scaffold** (`tests/unit/cli/test_init_scaffold.py`): three files
  written, `creds.json` mode `0600`, nothing overwritten on a second run, the
  scaffolded repo passes `otto init` with zero problems and zero warnings, the
  scaffolded `lab.json` host resolves through `resolve_host_entry` against the
  scaffolded tables to a dict `validate_host_dict` accepts (mutation: change
  the key in one file), the settings template drift guard still uncomments
  and validates.
- **Schema**: `creds.schema.json` built, validates the scaffolded file,
  rejects an entry with an unknown field; `otto init --schemas` scaffolds and
  drift-checks it; the VS Code wiring names it.
- **Docs examples**: the twin guard as §9.1; `test_inventory_worked_example`
  updated for `[creds]`; the docs build (`make docs`, Sphinx `-W`) green.
- **Gates**: the usual per-task gate, plus `nox -s tests_hostless-3.14`
  before the squash, `make lint-arch` for the new `tach.toml` module, and the
  import-budget guard for `otto.creds` on the bootstrap path.

## 12. Migration for an existing deployment

1. Move `creds_file = "X"` from `[inventory]` to `[creds] backend = "json"`,
   `path = "X"`. Same file, same anchoring, same contents.
2. Run `otto init` in each project: zero problems means the join is whole.

## 13. Implementation amendments

- §6.1 `merge_creds(lower, higher)` → `merge_creds(lower, higher, *, key=None, lower_name="lower", higher_name="higher")` — errors name the layer and the inventory key, and list field names only (a value may be a password).
- §6.1/§5.4 the store→record merge's `ValidationError` message → also names the login, not only the key and the store label — the login cannot always be read off `CredSpec`'s own message (a field-level failure such as `password: 123` has none), so `_revalidate_merged_cred` reads it off the merged dict itself before raising.
- §7.1 `creds_mode_warning` (one string or `None`) → `creds_mode_warnings` (a list, one per stat path) — a store may name several files.
- §7.1 orphan check "skipped when either side cannot enumerate" → also skipped when `list_keys()` raises `CredsError` — an unreadable store cannot enumerate; the mode pass names the missing file.
- §7.1/§8.2 a missing store file → produces both a warning (the mode pass) and, for a referencing entry, a problem (the lookup raises) — the two passes answer different questions.
- §8.1 the NetBox variant shown as commented `#[inventory]`/`#backend = …` TOML → indented prose comment lines (`#   backend = "netbox"`) — the settings-template drift guard uncomments and validates every `#`-prefixed line, and would otherwise parse a second `[inventory]` table.
- §8.1 `otto init --lab` help text → now names all three files (`lab_data/lab.json + inventory.json + creds.json`), not two — the scaffold grew a file and the CLI help was one of the sites still describing the old shape.
- §9 "the `[creds]` table replaces `creds_file` throughout" → one literal `creds_file` sentence stays, under `### [creds]` in `docs/guide/configuration/inventory.md` — it is the token the `creds_file has moved` error points a reader to search for.
- §9 the CHANGELOG entries specified for a hand edit → not applied; `CHANGELOG.md` is generated by `make changelog` from Conventional Commit subjects and is never hand-edited (`docs/contributing.md`). The four entries are carried as proposed squash-commit subjects instead (two marked `!`), for `make changelog` to render at release.
- §9 `docs/api/creds.rst` → created together with the `otto.creds` package (the commit implementing §5), not by the documentation task; the documentation task amends it (adds the intro paragraph and cross-links) rather than creating it from scratch.
- The depended-upon `2026-08-28-host-inventory-layer-design.md` §2.4's documented split-lab layout, `paths = [d, d/"*.json"]` → `paths = [d, d/"elements/*.json"]` — `inventory.json` and `creds.json` (§8.1) now live in `d` itself, so a bare `d/"*.json"` glob would sweep them in as lab files too; the split-file glob must sit one directory level down.
