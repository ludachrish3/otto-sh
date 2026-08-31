# Host inventory

Otto reads a host from two places. The **lab file** holds otto's business —
which labs exist, which element joins which, and how otto should talk to each
host. The **inventory** holds the machine's business — its management address,
its test-network interfaces, its credentials, the versions it declares, where
it sits in the building. A host entry that says `"inventory": "<key>"` gets
that second half from the inventory backend you configure, and keeps everything
otto-specific in the lab file.

This is for a lab that wants its otto configuration in the repo, reviewed with
the code, while the machine facts are owned **once**, outside it — by a JSON
file the lab team maintains, or by a NetBox instance that already exists. None
of it is required: a host entry that carries its own `ip` and `creds` works
exactly as it always has, and the two forms sit side by side in one lab file.

## Two layers, one rule

> **Data lives in exactly one layer. Keys may be asserted in both, and must
> agree.**

The configured inventory declares, once, **which record fields it supplies**:
`supplies` in the settings table for the `json` backend, the native set plus
your custom-field mappings for `netbox`. For a host entry that references the
inventory, those fields are inventory-owned; every other field is otto-owned
and stays inline. The partition is per *deployment* — decided at startup from
configuration, never discovered from what a particular record happens to
contain.

Stating an inventory-owned field beside `inventory` is an error at load:

```text
Lab file 'lab_data/lab.json': element 'chassis' hosts[1] in lab 'rig': 'ip' is
inventory-owned — it comes from inventory key 'carrot-b1'; remove it here, or
drop 'inventory' and declare the host inline
```

The check runs on the **raw** entry, before the fill, so the fill cannot fool
it. A field written as `null` states nothing, on either side: `null` inline is
not a collision, and `null` in a record is not a value — the entry's own
default applies.

There is deliberately no per-field precedence — no "take it from NetBox if the
field is filled in, else from the lab file". That rule reads as convenience and
behaves as a trap: the day somebody fills the field in, the lab file's value
goes silent with no error anywhere.

**Keys are the exception, and are cross-checked rather than filled.**
`element_id` may be stated in the record as a fact and on the element as
identity. When the inventory supplies it and both sides state it they must be
equal, or the load fails naming both values; it is never copied. An element's
identity is consumed before any host exists, and an identity that materialised
from a record would make "which element is this" depend on a backend call.

## The key

One opaque string per host entry, minted by whoever owns the inventory and
treated by otto as an uninterpreted identifier:

```json
{ "inventory": "carrot-b1", "os_type": "unix", "hop": "gw" }
```

- **Never an IP or a hostname.** The inventory exists because those change; a
  key that changes with the data is not a key.
- **Never an otto host id or `(element, element_id)`.** That is otto's naming,
  per lab and per project — if the inventory knew it, the decoupling would be
  fictional.
- **Never a location.** Sites, racks and slots are facts that drift, and they
  are carried as data.

Use the name people already call the machine — the DNS hostname where there is
one. During the bridge (see [Adoption path](#adoption-path)) the keys you mint
in a JSON file are the names the NetBox devices will eventually carry, so
swapping the backend touches no lab file. Keys are immutable by policy:
renaming one breaks every lab file referencing it, which is the one coupling
this design intends.

`"inventory": null`, and no `inventory` key at all, both mean "references
nothing" — the entry is inline. The empty string is an error, not a third
spelling of inline.

## Record fields

A record is a subset of these. Names are host-field names, one for one, so the
join is a plain key copy and there is no mapping table to drift.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `ip` | string | **Required.** The management address every record must carry. |
| `interfaces` | object | Test-network interfaces, `{"eth2": {"ip": ..., "subnet": ...}}`; a bare string is shorthand for `{"ip": ...}`. |
| `creds` | array of objects | `{"login": ..., "password": ...}` entries. Configure `creds_file` and these come from there instead — see {ref}`credentials-creds-file`. |
| `hw_version` | string | Hardware version. |
| `sw_version` | string | The version the device is *declared* to run — a declaration, never an observation: what you find on the device is never merged back into the record. |
| `os_name` | string | Free-form OS name. `os_type` stays otto-owned — its values select the host class. |
| `os_version` | string | OS version. |
| `board` | string | The named type of the board. |
| `site` | integer or string | Site the machine is installed at. A digit-only string (`"3"`) reads as the integer `3`. |
| `rack` | integer or string | Rack within the site, coerced the same way as `site`. |
| `shelf` | integer | Shelf / rack position. |
| `slot` | integer | Physical slot number. |
| `is_virtual` | boolean | `true` for a VM or emulator. Default `false`. |
| `element_id` | integer | A **key**, not data: cross-checked against the element's `id`, never filled. |
| `extra` | object | Opaque table otto never reads. Reaches the host as `host.inventory_ref.extra`. |

Unknown field names are refused naming the key — a record is a boundary
document like every other otto file. `_`-prefixed keys inside a record are
comment space.

`element_id` and `extra` sit outside the `supplies` partition: a record may
carry either whatever the deployment declares, because one is asserted rather
than filled and the other has no host field to collide with.

## Configuration

An inventory is declared **once per user**, in otto's user-level settings file:

```{note}
`~/.otto/settings.toml` is a new file — otto does not create it, and `otto
init` does not scaffold it, because an inventory is not project-shaped.
Create it by hand. `OTTO_HOME` relocates otto's home wholesale, and this file
with it. See [The workspace home](../cli/index.md#the-workspace-home).
```

The same table shape works in both places it may be written:

```toml
[inventory]
backend = "json"                   # a registered backend name
path = "~/lab/inventory.json"      # json kwarg: "~" expands; relative anchors
creds_file = "~/.otto/creds.json"  # backend-independent
cache_ttl = "24h"                  # remote backends only; "0" disables caching
```

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"                      # the token never sits in a file
filter = { site = "lab-a", status = "active" }  # any NetBox device filter
custom_fields = { sw_version = "sw_version" }   # optional, and opt-in
creds_file = "~/.otto/creds.json"
```

`backend`, `creds_file` and `cache_ttl` are otto's; every other key belongs to
the backend the entry selected, and is validated knowing which one that is. The
`json` backend takes `path` (required) and `supplies`; any other key is an
error naming it.

### Resolution order

First hit wins:

1. `[inventory]` in an **active repo's** `.otto/settings.toml` — the
   per-project override.
2. `[inventory]` in `~/.otto/settings.toml`.
3. Nothing. Inline hosts work as always; a referenced entry fails with an
   error naming **both** places it could have been declared.

There is no implicit discovery: an inventory is always declared, in one of
exactly two files, and a process has exactly one. An empty `[inventory]` table
declares nothing and falls through to the user file.

When more than one active repo declares `[inventory]`, the tables must be
**identical** — same backend, same kwargs after anchoring, same `creds_file`
**and** the same `cache_ttl`. Otherwise bootstrap fails naming both settings
files. Two inventories would reintroduce precedence through the back door, and
`cache_ttl` is in the comparison because it is behaviour, not decoration: one
repo saying `"0"` and another `"24h"` would let declaration order decide
whether the process caches at all.

```{note}
The doctor (`otto init`) validates **this** repo's declaration against the user
file; it never sees the other repos in your workspace. A disagreement between
two repos is a bootstrap error — you meet it the first time you run otto with
both of them active, not when you run the doctor in one.
```

### Paths

A relative `path` or `creds_file` anchors to the directory of the settings file
that declared it: the **repo root** for a project override (the directory
holding `.otto/`), `~/.otto` for the user file. `~` expands, absolute paths are
used as written — the rule every otto settings path follows, for the same
reason: a committed relative path must resolve the same wherever the repo is
checked out. See [Path resolution](settings.md#path-resolution).

### The scaffolded block

`otto init` writes a commented `#[inventory]` block into a new
`.otto/settings.toml`, next to the other optional sections. It is a template,
not a working declaration — uncomment it only to override the user file for
this project, and remember `path` is required once you do.

## The json backend

The bridge format: a JSON object mapping key → record, with nothing
otto-shaped in it, so a future export from the system that ends up owning the
data produces the same file.

```json
{
  "$schema": "~/.otto/schemas/inventory.schema.json",
  "_note": "keys are the device names NetBox will carry; never rename a key",
  "carrot-b1": {
    "ip": "10.10.200.11",
    "interfaces": { "eth2": { "ip": "192.168.1.11", "subnet": "192.168.1.0/24" } },
    "site": "hq", "rack": 3, "shelf": 2, "slot": 1,
    "board": "cx-4", "sw_version": "4.2",
    "extra": { "asset_tag": "A-1042" }
  }
}
```

- `$schema` and `_`-prefixed top-level keys are comment space. Every other key
  is an inventory key, and its value validates as a record — an error names the
  key and the field.
- `supplies` is a list of record field names, defaulting to every fillable
  field. A record carrying a field **outside** `supplies` is an error naming
  the key and the field: the file must not hold what the deployment says the
  lab files hold.
- The file is read once per process, on the first lookup, and held in memory.
- The label is `json:<path>`, and the freshness fingerprint is the resolved
  path with its mtime and size, so shell completion caches against it.
- It is never wrapped in the snapshot cache — its file *is* the snapshot.

`otto init` generates `inventory.schema.json` beside `lab.schema.json`, so an
editor can validate this file as you type it; the editor wiring is in
{doc}`../cli/schema/editors`.

(credentials-creds-file)=

## Credentials: `creds_file`

Credentials are universal and secret, so they get one home whatever the
backend: a JSON file keyed by the same inventory keys, named by `creds_file`
and read by otto rather than by the backend.

```json
{
  "carrot-b1": [
    { "login": "root", "password": "…" }
  ]
}
```

- Keep it outside every repository, at mode `0600`. The doctor warns when it is
  group- or world-readable, and when it is named but missing.
- Configuring `creds_file` makes `creds` inventory-owned — you do not list it
  in `supplies`, and a referenced host entry may not carry `creds` inline.
- A backend record that carries `creds` while `creds_file` is configured is an
  error naming the key. One home per field; the overlay never chooses between
  two sources.
- Without `creds_file`, records carry their own `creds` (the `json` backend
  allows it; NetBox has nowhere to keep them).
- The file is read on the first lookup, like everything else here, so a lab
  with no referenced entry never opens it.

This is what keeps a stage-1 inventory file free of secrets — shareable,
diffable, committable if you want — and it means moving to NetBox migrates no
credentials at all: the creds file stays exactly where it is.

## The NetBox backend

Nothing is required of NetBox beyond a device per referenced host whose `name`
is the key, an address the configured `ip_source` can read, and a read-only
token.

| Key | Meaning |
| --- | ------- |
| `url` | Base URL of the instance. A trailing slash is normalised away, so two spellings are one inventory. |
| `token_env` | Name of the environment variable holding the API token; default `NETBOX_TOKEN`. The token itself never sits in a settings file. |
| `verify` | Passed to the HTTP client: `false` disables TLS verification, a string is a CA bundle path and must be **absolute** (`~` expands). |
| `filter` | A NetBox device filter, forwarded verbatim (`{ site = "lab-a" }`). No filter means every device. |
| `ip_source` | Where the management address comes from: `"primary_ip4"` (default), `"oob_ip"`, or `"cf:<custom field>"`. |
| `custom_fields` | Record field → NetBox custom-field name. Each mapped field **joins** `supplies`. |
| `extra_custom_fields` | A list of custom-field names to carry through opaquely in `extra`. They are not record fields and never join `supplies`. |
| `timeout` | Seconds one request to NetBox may take; default `30`, and it must be a positive number. It bounds an instance that has gone *unreachable* — without it the kernel's TCP connect timeout (around two minutes) does, and it does so **before** the stale snapshot below is served. |

A relative `verify` path is refused rather than anchored: an `[inventory]` table
is committed, and a relative path there would resolve against whatever
directory otto happened to be run from.

The token is read at the **first fetch**, not at construction. A repo that
declares a netbox table can therefore still run every verb that does not need
the inventory — and a lab with no referenced entry never talks to NetBox at
all.

### The mapping

The one deliberate mapping table in otto:

| Record field | NetBox device |
| ------------ | ------------- |
| the key | `name` |
| `ip` | the address at `ip_source`, prefix length stripped |
| `site` | `site.name` |
| `rack` | `rack.name` (absent when unracked) |
| `shelf` | the integer part of `position` — which NetBox serialises as a string, and which may be a half-U value like `"3.5"` |
| `board` | `device_type.model` |
| `os_name` | `platform.name` |
| `is_virtual` | always `false` — devices only |
| `extra` | `id`, `serial`, `asset_tag`, `status`, `tags` (names), plus any `extra_custom_fields` |
| `slot`, `hw_version`, `sw_version`, `os_version`, `element_id` | **only if mapped** in `custom_fields`; otherwise the lab file carries them |
| `interfaces` | not populated — this backend fetches devices, not their interfaces |

So `supplies` for a NetBox inventory is `ip`, `site`, `rack`, `shelf`, `board`,
`os_name`, `is_virtual` **plus exactly the custom fields you map**. Mapping one
is the explicit act that moves a field from the lab files into NetBox, after
which the doctor flags every lab file still stating it inline.

### Custom fields

An instance full of custom fields is harmless: the backend reads exactly the
ones `custom_fields` maps and ignores every other one, so nothing can leak into
a record by accident.

- Any record field may be mapped **except** `ip`, `creds` and `interfaces`.
  `ip` has `ip_source` (two ways to say one thing is a way for them to
  disagree), `creds` come from `creds_file` and nowhere else, and `interfaces`
  is a structure no custom field holds.
- A mapped field of the wrong NetBox type — `element_id` mapped to a text field
  — fails the record's validation naming the device and the field.
- `extra_custom_fields` must be a **list**, and may not name any of `id`,
  `serial`, `asset_tag`, `status` or `tags`: this backend already puts those in
  `extra` from the device itself.

### What a fetch skips, and what fails

The whole filtered set is fetched once, on first use, into a dict keyed by
device name; every lookup after that is a dict hit.

- A device with **no name** is skipped — NetBox allows it, and keyed by name
  they would all collide on the string `None`.
- A device with **no address** at `ip_source` is skipped.

Both are dropped from `list_keys()`, and `otto inventory list` reports how many
of each the fetch passed over — an operator whose device is "missing from otto"
needs to be told it was selected and skipped rather than never seen. Looking up
an addressless device by name says exactly that, naming the device and the
`ip_source`.

Two devices sharing a name within the filtered set is an error naming both
device ids. Connection, TLS, authentication and API failures all raise a single
inventory error naming the URL — never a raw traceback out of the HTTP client.

## Caching remote inventories

A whole-set fetch produces exactly a stage-1 document, so caching it is the
`export` writer plus a timestamp. A backend is wrapped in the snapshot cache
when it is **not** the `json` backend, `cache_ttl` is greater than zero, and
its `fingerprint()` is `None` — the backend's own statement that it cannot
report freshness. NetBox says so unconditionally; a third-party backend that
returns a string opts out, because it has a better answer than a timestamp.

- **`cache_ttl`** is `"0"`, or `<n>m` / `<n>h` / `<n>d`. Deliberately narrow:
  no fractions, no whitespace, no leading zeros, no other units — one spelling
  per duration. The default is `"24h"`, because NetBox changes on a human
  cadence. `"0"` means every process fetches, which is how an uncached backend
  behaves.
- **Where.** `<otto home>/inventory-cache/` — `~/.otto/inventory-cache/` unless
  `OTTO_HOME` says otherwise. One snapshot per distinct inventory
  configuration, named for a hash of it, written whole-or-not-at-all at mode
  `0600` with a small meta file beside it. `otto inventory refresh` prints the
  snapshot's full path, which saves you guessing the hash.
- **Fresh.** A snapshot younger than the TTL is served **without contacting the
  backend** — the ordinary otto invocation costs one file read. Older, a fetch
  runs and the snapshot is rewritten atomically.
- **Unreachable, with a snapshot of any age.** The snapshot is served and a
  warning names its age and its fetch time in UTC. A lab that loaded yesterday
  should load today; the warning is what keeps the staleness visible. With no
  snapshot at all, the failure is the error, as it would be uncached.
- **The snapshot is a stage-1 document.** You can copy one out of
  `inventory-cache/` and point a `json` inventory at it.

```{important}
Lab-free commands never install a log handler, so the `otto inventory` read
verbs print the stale-snapshot notice **themselves**, before their own output.
That matters most for the two that would otherwise answer in silence: `export`
would write a stale artefact and `diff` would report "no differences" against a
stale left side.
```

`otto inventory refresh` fetches unconditionally, whatever the TTL says, and
reports the replaced snapshot's timestamp in **your local time** with its age.
Run against an inventory that has no snapshot — a `json` one, or a remote one
with `cache_ttl = "0"` — it exits 1 saying which of the two it is, rather than
shrugging: `otto inventory refresh && …` must not proceed as though a fetch had
happened.

## What the doctor checks

`otto init` in a project validates every host entry the way the loader will,
against the inventory that project would resolve. Problems fail the run
(exit 1); warnings never change the exit code. The resolved inventory's label
is printed once, so you can see which one answered:

```text
inventory: json:/home/me/lab/inventory.json
```

### Problems

- A dead reference — a key the inventory does not hold — naming the file, the
  element, the host index, the key and the inventory label.
- A referenced entry when **no** inventory is configured, naming both places it
  could have been declared.
- A malformed reference (the empty string, a non-string).
- An inventory-owned field stated inline beside `inventory`. When an entry does
  both — states a supplied field *and* names a key that does not exist — the
  collision is what you are told about first: it is the error you can fix
  without the inventory answering at all.
- A broken `[inventory]` declaration, or an unparseable user settings file,
  reported **once** rather than once per referencing entry. Those entries are
  skipped for that run and validate again once the declaration is fixed.

### Warnings

- A **stale snapshot** the doctor was served because the remote backend was
  unreachable, naming its age and the `otto inventory refresh` that replaces
  it. The log line that also carries this fires only once per process, so
  `otto init` reports it in the table itself, exactly as the `otto inventory`
  verbs do — a green table against a snapshot days old is the one thing this
  gate must not print.
- **Orphan records** — keys no lab file in this project references, up to ten
  of them by name and a count for the rest. During the bridge the inventory is
  expected to be wider than any one project, so this is information, not a
  defect. A project that has adopted nothing yet gets it listing everything,
  which is exactly the state the bridge starts from — the warning is how you
  watch that list shrink.
- A `creds_file` that is group- or world-readable, naming the mode, or one that
  is named and does not exist.

## The verbs

`otto inventory` reads the configured inventory. Every verb is read-only, needs
no lab and touches no host — see {doc}`../cli/inventory/index` for the full
treatment, including the three exit codes `diff` uses.

| Verb | What it answers |
| ---- | --------------- |
| `lookup KEY` | The resolved record, the backend label and `supplies` — creds as login names only |
| `list` | Every key with its address, and what the backend skipped |
| `export PATH` | The inventory as a stage-1 JSON file: sorted keys, no `creds` |
| `diff PATH [OTHER]` | The inventory against a stage-1 file, or two files against each other |
| `refresh` | A forced fetch of a cached remote inventory |

## Completion

Otto's shell-completion cache stores the host ids it offers, and re-reads them
when its fingerprint changes. The inventory contributes to that fingerprint:
the file's path, mtime and size for a `json` inventory, the snapshot's content
hash for a cached remote one. So NetBox-backed completion caches exactly as a
file-backed one does, and no TAB keystroke depends on the inventory service
being reachable.

Two situations leave completion **uncached**. Both stay correct — the ids still
come from a real load — and cost only speed:

- The inventory reports `fingerprint()` as `None` with no snapshot cache in
  front of it to supply one. That combination means the deployment turned the
  cache off with `cache_ttl = "0"`: a backend that answers `None` while a TTL
  is set is exactly the one the cache wraps.
- The freshness probe **raises** — a networked backend's `fingerprint()` timing
  out, say. Otto never lets that reach your shell, but a failed probe leaves no
  stable value to key an entry on, so nothing is stored under it either.

## Adoption path

Three stages, each with the verb that proves it. The **references** never
change between them — that is what the key's bridge rule is for — so a lab file
is touched at stage 2 only where NetBox turns out not to supply a field the
JSON file did (step 3 below).

### Stage 1 — the JSON inventory

- **One file**, owned like code — its own small repository, or a directory the
  lab team reviews changes to. A `$schema` line, sorted keys (`otto inventory
  export` normalises them). Split it along *ownership* lines if you must, never
  along otto lines: the `json` backend reads one `path`, and one file is the
  bridge's virtue.
- **Decide the naming scheme once**, in that file's README, before the first
  entry. The keys are the device's canonical name — see [The key](#the-key).
- **Records carry facts only**: the management address, test-network
  interfaces, `is_virtual`, the location fields where you know them, versions
  as the declared state. If a value only matters to otto, it belongs in the lab
  file.
- **Declare `supplies` to match what your eventual owner will supply.** The lab
  files are then already in their stage-2 shape, and `otto inventory diff`
  compares like with like. Leaving it at the default (everything) is fine for a
  file-only future; it just means more re-homing later.
- **Credentials in `creds_file` from day one**, mode `0600`, outside every
  repository. The inventory file then holds no secrets, and stage 2 migrates
  none.
- **Checks**: `otto inventory list` parses and counts, `otto init` in each
  project finds dead references and orphans, `otto inventory lookup KEY`
  explains a host that built oddly.

### Stage 2 — moving to NetBox

1. In NetBox, make sure every device that will be referenced exists with its
   `name` **equal to its inventory key**. Rename the NetBox device, never the
   key.
2. Decide what NetBox supplies. The native set costs nothing: an address
   `ip_source` can read, plus whatever of site / rack / position / device type
   / platform is already filled in. **Map a custom field only if NetBox already
   has one you want otto to read**, or you have decided to add it. Everything
   unmapped stays in the lab files.
3. Mind the fields NetBox does **not** supply. `interfaces` is the one to check
   first — this backend fetches devices, not their interfaces — so a stage-1
   file that owned `interfaces` re-homes them into the lab files at this step.
4. Pick a `filter` that selects the lab devices: a site, a role, a tenant, a
   tag — whatever NetBox already distinguishes them by. It bounds the fetch; it
   is not an otto convention.
5. If you mapped custom fields, populate them from the JSON file. A script over
   `otto inventory export` output is the obvious tool: otto never writes to
   NetBox.
6. Point `[inventory]` at NetBox and keep `creds_file` exactly as it was.
   During the fractured phase one project can do this alone, with its own
   `[inventory]` override, while the others stay on the file.
7. Run `otto inventory diff ~/lab/inventory.json` and iterate in NetBox until
   the table is empty — it tells you field by field what is left. Then run
   `otto init` in every project: zero dead references.
8. Keep `otto inventory export ~/lab/inventory.json` as the periodic,
   *reviewable* snapshot. The offline fallback and fast completion are handled
   by the automatic cache without it.

### Stage 3 — keeping NetBox otto-healthy

Two things otto depends on:

- **The device name is the key.** Treat it as immutable. Retiring a device — a
  status outside your `filter`, or a deletion — surfaces as a dead reference in
  every project's doctor, which is the correct signal delivered where the fix
  is. A rename is a retirement plus a new device plus a deliberate lab-file
  edit, and that friction is intentional.
- **The address `ip_source` reads stays filled in.** A device that loses it is
  skipped by the fetch and reported by `otto inventory list`; a host that
  references it fails to build.

Practices that help, none of which otto requires:

- A NetBox custom validation rule refusing to save a device in your filter
  without an address — the UI then catches what otto would report later.
- If you map custom fields, keep them typed as the mapping expects
  (`element_id` and `slot` integer, versions text). Grouping them is purely for
  the NetBox UI.
- **Versions are declarations.** What a device is found to be running is an
  observation; when the two disagree, update NetBox by hand. otto never writes
  to it — the inventory is the source of record, not a cache of observations.
- **Read-only, per-user API tokens** through `token_env`; never a shared token
  in a file.
- **Review NetBox change through otto's eyes**: `otto inventory diff` against
  the last export shows what moved, and `otto init` in CI catches a dead
  reference before a run does.

## Writing your own backend

A JSON file and NetBox are the two backends otto ships. A team whose facts
live elsewhere — a CMDB, a spreadsheet export, an internal API — writes a
backend: a class implementing the inventory protocol, registered from an
`init` module and selected by name in `[inventory]`. The contract, the
snapshot-cache opt-in, and the conformance helper that proves a backend
against it are in {doc}`../../library/inventory-backends`.

## Worked example — the unix lab

Three virtual hosts in a `unix` lab, one of which is also a member of
`busybox`. These four files are the ones otto's own test suite loads: the suite
proves that the referenced hosts below build **identically** to the same three
hosts declared inline, and a guard compares the blocks on this page against
those files, so what you read here is what otto is tested against. (The `_note`
keys are comment space — `_`-prefixed keys are ignored in every otto JSON
file.)

`~/lab/inventory.json` — facts, no otto vocabulary, no secrets:

<!-- fixture: tech1-inventory/inventory.json -->
```json
{
    "$schema": "~/.otto/schemas/inventory.schema.json",
    "_note": "keys are the device names NetBox will carry; never rename a key",
    "test1": {
        "ip": "10.10.200.11",
        "interfaces": {
            "eth2": {"ip": "192.168.1.11", "subnet": "192.168.1.0/24"},
            "bbeth-1350": {"ip": "198.51.100.18", "subnet": "198.51.100.16/30"}
        },
        "is_virtual": true,
        "site": "lab-a",
        "rack": 1,
        "shelf": 3,
        "extra": {"asset_tag": "VM-0011"}
    },
    "test2": {
        "ip": "10.10.200.12",
        "interfaces": {
            "eth2": {"ip": "192.168.1.12", "subnet": "192.168.1.0/24"}
        },
        "is_virtual": true,
        "site": "lab-a",
        "rack": 1,
        "shelf": 3
    },
    "test3": {
        "ip": "10.10.200.13",
        "interfaces": {
            "eth2": {"ip": "192.168.1.13", "subnet": "192.168.1.0/24"}
        },
        "is_virtual": true,
        "site": "lab-a",
        "rack": 1,
        "shelf": 4
    }
}
```

`~/.otto/creds.json` — one home for credentials, mode `0600`, keyed by the same
keys:

<!-- fixture: tech1-inventory/creds.json -->
```json
{
    "_note": "one home for credentials, whatever the inventory backend",
    "test1": [
        {"login": "vagrant", "password": "vagrant"},
        {"login": "test", "password": "Password1"}
    ],
    "test2": [
        {"login": "vagrant", "password": "vagrant"},
        {"login": "test", "password": "Password1"}
    ],
    "test3": [
        {"login": "vagrant", "password": "vagrant"},
        {"login": "test", "password": "Password1"}
    ]
}
```

`~/.otto/settings.toml` — declared once per user. `supplies` names what this
deployment lets the inventory own; `os_version`, `sw_version` and the rest stay
lab-file fields here:

<!-- fixture: tech1-inventory/user-settings.toml -->
```toml
[inventory]
backend = "json"
path = "~/lab/inventory.json"
creds_file = "~/.otto/creds.json"
supplies = ["ip", "interfaces", "is_virtual", "site", "rack", "shelf", "board", "os_name"]
```

`lab_data/lab.json` in the project — otto's business only. Every host entry is
a reference plus otto-owned fields; nothing here is an address or a credential:

<!-- fixture: tech1-inventory/lab.json -->
```json
{
    "_note": "every machine fact comes from inventory.json; every otto fact stays here. Declared inline, these three hosts come out identical.",
    "labs": {
        "unix": {
            "resources": ["test1", "test2", "test3"],
            "metadata": {"description": "unix regression bed"}
        },
        "busybox": {
            "resources": ["test1", "bb1161", "bb1211", "bb1281", "bb1310", "bb1350"]
        }
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix", "busybox"],
            "resources": ["test1-chassis"],
            "metadata": {"role": "hub"},
            "hosts": [
                {
                    "inventory": "test1",
                    "os_type": "unix",
                    "docker_capable": true,
                    "valid_terms": ["ssh", "telnet"],
                    "valid_transfers": ["scp", "sftp", "ftp", "nc"]
                }
            ]
        },
        {
            "name": "test2",
            "labs": ["unix"],
            "hosts": [
                {
                    "inventory": "test2",
                    "os_type": "unix",
                    "docker_capable": true,
                    "valid_terms": ["telnet", "ssh"],
                    "valid_transfers": ["nc", "scp", "sftp", "ftp"],
                    "resources": ["test2-console"]
                }
            ]
        },
        {
            "name": "test3",
            "labs": ["unix"],
            "hosts": [
                {
                    "inventory": "test3",
                    "os_type": "unix",
                    "docker_capable": true,
                    "roles": ["docker"],
                    "valid_terms": ["ssh", "telnet"],
                    "valid_transfers": ["scp", "sftp", "ftp", "nc"]
                }
            ]
        }
    ]
}
```

**How it correlates at load.** The `test2` element flattens to
`{"element": "test2", "inventory": "test2", "os_type": "unix", …}`. The join
looks up `test2`, finds no inventory-owned field stated inline — had the entry
also said `"ip": …`, that is the error at the top of this page — copies `ip`,
`interfaces`, `is_virtual`, `site`, `rack` and `shelf` onto the entry, and the
creds file supplies `creds`. The host spec then validates the whole thing
exactly as it would an inline entry, the host id is still `test2` from the
element, and the host carries its provenance as
`host.inventory_ref` — the key, the backend label and the record's `extra`.

The equivalence otto's suite proves is over this `unix` lab: its three hosts,
referenced and inline, agree field for field. The `busybox` guests
(`bb1161`…) stay inline here — they are QEMU guests behind `test1` with no life
outside this bed, which is exactly what the inline form is for.

Moving this deployment to NetBox starts with one file, `~/.otto/settings.toml`:

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"
filter = { site = "lab-a", status = "active" }
creds_file = "~/.otto/creds.json"
```

…with devices named `test1`/`test2`/`test3` carrying primary IPv4s, site
`lab-a`, rack `1` and positions `3`/`3`/`4`, all of which NetBox models
natively. `supplies` is then the native set — which does **not** include
`interfaces`, so this deployment's test-network interfaces move back into the
lab file at that step. `otto inventory diff ~/lab/inventory.json` is what shows
you that, field by field, before you switch; run it until only the differences
you intend are left.
