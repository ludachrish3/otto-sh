# Host inventory

Otto reads a host from two places. The **lab file** holds otto's business —
which labs exist, which element joins which, and how otto should talk to each
host. The **inventory** holds the machine's business — its management address,
its test-network interfaces, its credentials, the versions it declares, where
it sits in the building. A host entry that says `"inventory": "<key>"` gets
that second half from the inventory backend you configure, and keeps everything
otto-specific in the lab file.

This is for a lab that wants its otto configuration in the project, reviewed with
the code, while the machine facts are owned **once**, outside it — by a JSON
file the lab team maintains, or by a NetBox instance that already exists. None
of it is required: a host entry that carries its own `ip` and `creds` works
exactly as it always has, and the two forms sit side by side in one lab file.

## Three files, one key, one order

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

`otto init` writes these three files and the two settings tables that tie
them together (unless `--inline-hosts`: then one `lab.json` and commented
tables). Three statements, each of which the loader enforces:

1. **The inventory key is the only thing the files share.** A host entry that
   says `"inventory": "<key>"` gets its machine facts from the inventory
   record under that key and its creds from the creds store under that same
   key. Nothing else — not an IP, not an element name, not a host id — is ever
   used to line the files up.
2. **The creds store is optional; the inventory is not, for a referenced
   entry.** Without `[creds]`, creds come from the inventory record and the
   lab file; without `creds` in the record, from the store and the lab file;
   with neither, from the lab file alone. Inline hosts — no `inventory` key —
   carry their creds inline as they always have. `[creds]` without
   `[inventory]` is an error: the store is keyed by inventory keys, so
   nothing could ever look one up.
3. **Creds are matched by `login`, and within a login the highest layer that
   states a field wins.** Lab file over inventory record over creds store,
   field by field; a `null` states nothing and removes nothing. The lab file's
   order is the login order, then record-only logins in record order, then
   store-only logins in store order. A lab file that wants to fix the order of
   logins it does not otherwise touch lists them by `login` alone.

`creds` is therefore the one host field the inventory partition **composes**
rather than **refuses**: every other supplied field must be absent inline
beside a reference (the rule below). A password set in the lab file hides the
store's with no error. A `proxy` names project code — a login proxy an `init`
module registers — so an inventory or store shared across projects that carries
a route loads only in projects that register that proxy; the doctor names the
cred and the missing proxy.

## Two layers, one rule

> **Data lives in exactly one layer. Keys may be asserted in both, and must
> agree.**

(`creds` is the one exception — see [Three files, one key, one
order](#three-files-one-key-one-order).)

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

The check runs on the **raw** entry, before the fill. A field written as `null`
states nothing, on either side: `null` inline is not a collision, and `null` in
a record is not a value — the entry's own default applies.

There is no per-field precedence — no "take it from NetBox if the field is
filled in, else from the lab file".

**Keys are the exception, and are cross-checked rather than filled.**
`element_id` is a **cross-checked fact**, not an identity key: it is opt-in,
named in `supplies` like any other field.  Only when a deployment does that
does a stated record value get checked — when the inventory supplies it and
both the record and the element state it, they must agree, or the load fails
naming both values.  A deployment that never names `element_id` in `supplies`
(the common case) never fills it and never checks it.  (An element's identity
is its name's slug, not its `id` — see {ref}`host-identity`.)

## The key

One opaque string per host entry, minted by whoever owns the inventory and
treated by otto as an uninterpreted identifier:

```json
{ "inventory": "carrot-b1", "os_type": "unix", "hop": "gw" }
```

- **Never an IP or a hostname.** The inventory exists because those change; a
  key that changes with the data is not a key.
- **Never an otto host id or an element name.** That is otto's naming, per lab
  and per project.
- **Never a location.** Sites, racks and slots are facts that drift, and they
  are carried as data.

Use the name people already call the machine — the DNS hostname where there is
one. During the bridge (see [Adoption path](#adoption-path)) the keys you mint
in a JSON file are the names the NetBox devices will eventually carry, so
swapping the backend touches no lab file. Treat keys as immutable: renaming one
breaks every lab file referencing it.

`"inventory": null`, and no `inventory` key at all, both mean "references
nothing" — the entry is inline. The empty string is an error, not a third
spelling of inline.

## Record fields

A record is a subset of these. Names are host-field names, one for one.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `ip` | string | **Required.** The management address every record must carry. It takes what a host's `ip` takes ({ref}`host-ip-field`). |
| `interfaces` | object | Test-network interfaces, `{"eth2": {"ip": ..., "subnet": ...}}`; a bare string is shorthand for `{"ip": ...}`. |
| `creds` | array of objects | `{"login": ..., "password": ...}` entries, optionally with a route. The **middle** creds layer: overridden by the lab file, overriding the creds store — see {ref}`credentials-layered`. |
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
| `element_id` | integer | A **cross-checked fact**, not data: named in `supplies` to opt in, then checked against the element's `id` — never filled either way. |
| `groups` | array of strings | Memberships: a role, a tag, a group (`["edge", "pod-7"]`). Discovery data for lab templates; never copied onto a host. Every entry must be a string. |
| `labels` | object | Scalar facts about the instance the machine belongs to (`{"pod": "007", "vlan": 12}`). Each value is a string or an integer, kept as written: `"007"` stays a string. A boolean, a fractional number, an array or an object is refused, naming the key and the label. Never copied onto a host. |
| `extra` | object | Opaque table otto never reads. Reaches the host as `host.inventory_ref.extra`. |

Unknown field names are refused naming the key. `_`-prefixed keys inside a
record are comment space.

`element_id`, `extra`, `groups` and `labels` sit outside the `supplies`
partition: a record may carry any of them whatever the deployment declares.

`groups` and `labels` are a record's discovery vocabulary: what a lab template
selects machines by and substitutes into a layout. No host field has their
names, and the join never copies them onto a host. Today only a JSON record
states them. A NetBox record carries no `groups` or `labels` yet: mapping
NetBox roles and tags to `groups`, and custom fields to `labels`, is tracked
in [#1192](https://github.com/ludachrish3/otto-sh/issues/1192).

```{important}
**A referenced Unix host's probe pin (BusyBox included) goes in the lab file,
beside `inventory`, never in the record:** paste what
`otto --lab <lab> host <id> probe` prints into that host entry. Unpinned, each
otto command that needs its userland probes it again. {doc}`lab/probing-hosts`
shows what the pin looks like and where it goes.
```

## Configuration

An inventory is declared **once per user**, in otto's user-level settings file:

```{note}
`~/.otto/settings.toml` is a new file — otto does not create it, and
`otto init` does not scaffold it. Create it by hand. `OTTO_HOME` relocates
otto's home wholesale, and this file with it. See
[The workspace home](../cli/index.md#the-workspace-home).
```

The same table shape works in both places it may be written:

```toml
[inventory]
backend = "json"                   # a registered backend name
path = "~/lab/inventory.json"      # json key: "~" expands; relative anchors
cache_ttl = "24h"                  # remote backends only; "0" disables caching

[creds]
backend = "json"
path = "~/.otto/creds.json"  # the creds store; optional
```

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"                      # the token never sits in a file
filter = { site = "lab-a", status = "active" }  # any NetBox device filter
custom_fields = { sw_version = "sw_version" }   # optional, and opt-in

[creds]
backend = "json"
path = "~/.otto/creds.json"  # the creds store; optional
```

`backend` and `cache_ttl` are otto's; every other key belongs to the backend
the entry selected, and is validated knowing which one that is. The
`json` backend takes `path` (required) and `supplies`; any other key is an
error naming it.

### `[creds]`

The creds store is declared the same way, in the same two files, and resolves
independently — a project may override `[inventory]` alone and inherit the
user file's `[creds]`, or the reverse. `backend` is otto's; the `json` store
takes `path` (required) and nothing else; a third-party store takes whatever
its config model declares. When more than one active project declares `[creds]`,
the tables must parse to the same configuration (defaults filled in, paths
anchored), exactly as for `[inventory]`.

`creds_file`, the key that used to live under `[inventory]`, is gone: a
settings file still carrying it fails validation with a message pointing here.
Move the path to `[creds] backend = "json"` / `path = "…"`; the file's contents
need no change.

(named-inventories)=

### Named inventories

A project may declare several inventories, each under a name, as an array of
`[[inventories]]` tables in its `.otto/settings.toml`, and your user file
`~/.otto/settings.toml` may declare them too
([Field reference](settings.md#field-reference),
[User-level settings](settings.md#user-level-settings)):

```toml
[[inventories]]
name = "physical"
backend = "netbox"
url = "https://netbox.example"

[[inventories]]
name = "virtual"
backend = "json"
path = "lab_data/virtual.json"

[[inventories]]
name = "fixtures"
backend = "json"
path = "lab_data/inventory.json"
```

An entry reserves exactly four keys: `name`, `backend`, `cache_ttl` and
`creds`. Every other key belongs to the backend, as under `[inventory]`, and
`creds_file` is one of them. A host entry consults a named inventory only
through the **qualified** reference, which names it:

```json
{"inventory": {"name": "physical", "key": "edge-17"}}
```

**Names are process-wide.** Every active project and the user file share one
catalog. Two active projects that declare the same name must agree on it, as
two `[inventory]` tables must: the same backend, the same parsed settings
(defaults filled in, paths anchored), the same `cache_ttl` and the same
credentials binding (both without a `creds` sub-table, or both with the same
store). Otherwise bootstrap fails, naming both settings files and the
entry. A name a project declares wins over the same name in the user file;
a name only the user file declares comes from there.

**`default` is today's inventory.** The inventory a bare
`"inventory": "edge-17"` reference resolves through is named `default`, and
it is exactly what [Resolution order](#resolution-order) below builds from
`[inventory]` and `[creds]`. A `[[inventories]]` entry named `default` takes
part in that order as an `[inventory]` table would, so one file may declare
`default` in one form or the other, never both. A configuration with no
`[[inventories]]` behaves exactly as before.

**Credentials bind to one inventory.** An entry's `creds` sub-table
(`[inventories.creds]`, the same keys as `[creds]`) is the creds store of
that entry alone. `[creds]` binds to `default` only: a store is never an
implicit source for another inventory. A project that declares any
`[[inventories]]` and a `[creds]` table without binding `default` itself (by
`[inventory]` or an entry named `default`) is refused at bootstrap, because
that `[creds]` would bind to the process `default`, possibly the user's own
inventory, and the named inventory would lose its credentials. Move the
table into the owning entry:

```toml
[[inventories]]
name = "physical"
backend = "json"
path = "lab_data/physical.json"

[inventories.creds]
backend = "json"
path = "lab_data/creds.json"
```

**Bare references need `default` bound by the project.** A project that
declares any `[[inventories]]` may use bare references only if it binds
`default` itself, by keeping its `[inventory]` or by an entry named
`default`. Otherwise every bare reference in it fails with one error that
quotes the qualified form to write instead. This closes a migration trap:
a project moves its `[inventory]` into an entry named `physical` while your
user file still declares a personal `[inventory]`, and the project's old bare
references would otherwise resolve against your personal machines. A
qualified `{"name": "default", ...}` reference is always allowed.

**A lab source is not an inventory**
({ref}`Host sources <sources-are-not-inventories>`). A source's host entry *references* an
inventory; the two are declared, named and resolved separately, which is why
the reference's field is `name` and never `source`.

### Resolution order

`default`, the inventory bare references resolve through, is found first hit
wins:

1. `[inventory]` in an **active project's** `.otto/settings.toml` — the
   per-project override. An `[[inventories]]` entry named `default` counts
   as that file's `[inventory]`.
2. `[inventory]` in `~/.otto/settings.toml`, or its entry named `default`.
3. Nothing. Inline hosts work as always; a referenced entry fails with an
   error naming **both** places it could have been declared.

`[creds]` is found the same way, independently (a `default` entry's `creds`
sub-table counts as that file's `[creds]`). Every other name follows the same
order on its own: the active projects that declare it, else the user file
([Named inventories](#named-inventories)).

There is no implicit discovery: an inventory is always declared, in one of
exactly two files, and a process has exactly one of each name. An empty
`[inventory]` table declares nothing and falls through to the user file.

When more than one active project declares `[inventory]`, the tables must be
**identical** — same backend, the same parsed configuration (defaults
filled in, paths anchored), **and** the same `cache_ttl`; the `[creds]`
tables are compared among themselves the same way. Otherwise bootstrap fails
naming both settings files.

```{note}
The doctor (`otto init`) validates **this** project's declaration against the user
file; it never sees the other projects in your workspace. A disagreement between
two projects is a bootstrap error — you meet it the first time you run otto with
both of them active, not when you run the doctor in one.
```

### Paths

A relative `path` (under `[inventory]` or `[creds]`) anchors to the directory
of the settings file that declared it: the **project root** for a project override
(the directory holding `.otto/`), `~/.otto` for the user file. `~` expands,
absolute paths are used as written — the rule every otto settings path follows.
See [Path resolution](settings.md#path-resolution).

### The scaffolded tables

Unless run with `--inline-hosts`, `otto init` writes live `[inventory]` and
`[creds]` tables into a new `.otto/settings.toml`, pointing at
`lab_data/inventory.json` and `lab_data/creds.json`, with `supplies = ["ip"]`
— so a new project starts in the three-file shape and grows `supplies` as the
inventory takes over more fields. With `--inline-hosts` it writes one
`lab.json` and leaves both tables commented out. The user-level file is still
where a shared inventory lives: delete the project tables once one exists, or
keep them as this project's override. Keep `[creds]` only while the project
binds `default`: once it declares `[[inventories]]`, a `[creds]` table without
`[inventory]` (or an entry named `default`) beside it is refused (*Credentials
bind to one inventory*, under [Named inventories](#named-inventories)). Two
active projects that both keep their scaffolded tables fail bootstrap, naming
both files: tables compare by their parsed configuration, paths **anchored**,
and each project's own `lab_data/inventory.json` anchors to its own root, so
the paths never match. Move the tables to `~/.otto/settings.toml`, or delete
one project's.

## The json backend

A JSON object mapping key → record, with nothing otto-shaped in it.

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

### Declaring instances: the `otto.inventory.v2` document

An *instance* is a set of keys that belong together, such as the machines of
one test rig. A json inventory declares instances in a second document
format, selected by a top-level `"$format"` marker:

```json
{
  "$schema": "~/.otto/schemas/inventory.schema.json",
  "$format": "otto.inventory.v2",
  "records": {
    "edge-17": { "ip": "10.0.0.17", "groups": ["dut"] },
    "ixia-9": { "ip": "10.0.0.90" }
  },
  "instances": {
    "rig-7": { "roles": { "dut": ["edge-17"], "tgen": ["ixia-9"] } },
    "rig-8": { "members": ["edge-18", "ixia-10"] },
    "rig-9": {}
  }
}
```

- **Why a marker.** Every bare top-level name, `instances` and `records`
  included, is already a legal inventory key in the flat document above. otto
  never guesses the format from the keys. A document is v2 only when its
  `"$format"` value is a string. Without that marker the document is flat and
  keeps its meaning, so a flat file with a host named `$format`, `records` or
  `instances` reads exactly as before.
- **The v2 document.** `records` is the flat document's key-to-record map,
  with the same rules. `instances` maps an instance name to its instance.
  Both are optional. At the top level, `$schema` and `_`-prefixed keys are
  comment space. Inside `records` and `instances`, and inside each instance,
  only `_`-prefixed keys are. Any other top-level key is an error, and so is
  any `$format` string other than `otto.inventory.v2`.
- **Roles and plain members.** `roles` maps a role name to the keys that play
  it (`rig-7`'s `dut` is `edge-17`). `members` lists keys that belong to the
  instance without a role. Each list is a set of non-empty keys, which otto
  sorts. Two rules apply, and a break of either is an error naming the
  instance and the key:
  - a key appears at most once in one list;
  - a key in `members` plays no role, so it may not also appear in `roles`.

  One key may play several roles. Role names must be non-empty.
- **Members are this inventory's own keys.** A member needs no record in
  `records`. A rig whose machines have all gone is still reported, which only
  an explicit instance map can do.
- **Three results.** A flat document declares no instances: the inventory
  *does not support* them. A v2 document without instances supports them and
  declares none. An instance with no members, like `rig-9`, is legal.

A backend of your own declares instances through the discovery method
{ref}`described for backend authors <inventory-discovery>`.

### A Jsonnet inventory file

A json inventory's `path` may name a `.jsonnet` file instead of a `.json`
one. Which it is, is decided on the path once symlinks are resolved: a
symlink to a `.jsonnet` file is rendered, whatever its own name. otto
renders it, in memory, and reads the JSON it renders exactly as it
reads a written file: flat or `otto.inventory.v2`, with the same `supplies`
rule and the same errors. A file that stands in for an inventory you do not
have yet, such as NetBox, is the common case: the
{ref}`templated-lab example <lab-templating-example>` uses two.

```toml
[[inventories]]
name = "physical"
backend = "json"
path = "inventories/physical.jsonnet"
import_paths = ["inventories/lib"]   # optional; none by default
```

- **It takes no arguments.** The file renders to the document itself, never a
  function: an inventory has no project, and two projects that declare the
  same inventory must read the same records.
- **`import_paths`** lists the directories its imports resolve under, in
  order. It defaults to none, so only `otto.libsonnet` is importable. Each
  entry is anchored like `path` ([Paths](#paths)). The rules are a lab
  template's ({ref}`Imports <lab-templating-imports>`): the file's own
  directory is not a root, and no settings, inventory or credentials file is
  ever served.
- **It is rendered once per inventory view**, on its first lookup, like a
  written file is read once; the next operation builds a new view and renders
  it again. Its freshness fingerprint covers the file, every file its
  imports could resolve to and `otto.libsonnet`, by their metadata, so
  completion caches against it without rendering.
- **A render failure is an inventory error** naming the file
  (`<path>: could not be rendered: <reason>`).

`import_paths` is accepted on a `.json` path too, and ignored there.

Moving from the stand-in to NetBox changes the entry's `backend` and its
settings ([The NetBox backend](#the-netbox-backend);
{ref}`its virtual machines <netbox-virtual-machines>` for VMs). Nothing that
references the inventory changes only if the stand-in's keys are the names
NetBox gives the devices and its records state only what a NetBox record
carries. A NetBox record carries no `groups` or `labels` yet
([Record fields](#record-fields)), so a template that selects by them, as the
templated-lab example's `inGroup('dut')` does, finds no members once the
inventory is NetBox.

(credentials-layered)=

## Credentials: the `[creds]` store and the layered merge

Credentials have a store of their own, keyed by the same inventory keys and
read by otto rather than by the inventory backend:

```json
{
  "$schema": "~/.otto/schemas/creds.schema.json",
  "carrot-b1": [
    { "login": "root", "password": "…" }
  ]
}
```

- Every entry is the same `creds` object a lab file spells — `login`
  (required), `password`, and optionally the route fields `proxy`, `via`,
  `params`, and the scope field `protocols` ({doc}`host-sources` has the
  field reference). An entry's login and `protocols` scope may not repeat
  under one key ({ref}`cred-protocols`).
- Keep the file outside every repository, or at mode `0600`. The doctor warns
  when it is group- or world-readable, and when it is named but missing.
- The store is read on the first lookup, like everything else here, so a lab
  with no referenced entry never opens it. An unknown key is not an error at
  the store — it is an empty list, and whether a unix host without creds is a
  problem is the host spec's call, made after the merge.

Configuring `[creds]` makes the inventory supply `creds`: you do not list it
in `supplies`. What happens then is the merge of [Three files, one key, one
order](#three-files-one-key-one-order), applied twice:

1. **Store → record.** The record's `creds`, if it states any, layer over the
   store's by identity — login plus `protocols` scope. A record may carry
   creds beside a configured store; it overrides the store's fields for the
   logins it names.
2. **Record → lab file.** The host entry's inline `creds`, if any, layer over
   the result the same way, and the entry's order becomes the list's order.

A worked case — store `[{vagrant, vagrant}, {test, Password1}]`, no record
creds, lab file `[{vagrant}, {test}, {root, proxy: sudo-root, via: vagrant}]` —
yields `[{vagrant, vagrant}, {test, Password1}, {root, sudo-root via
vagrant}]`: the two login-only placeholders pin the order, the third entry adds
a route no store could know. Had the lab file listed only the route, `root`
would have come first and been the default login.

Scope is part of the key, never a field the layers compose: a store entry
`admin` scoped to `ftp` overlays only a lab entry with that same login and
scope, and sits beside an unscoped `admin` as a second entry
({ref}`cred-protocols`).

Two errors the merge itself raises, each naming the layer and the inventory
key: an entry whose login and scope repeat within one layer, and an entry
with no `login`. A composed entry the cred rules refuse (`via` or `params`
without `proxy`) is caught one step later, where that entry is next
validated: at the store-to-record step the overlay names the inventory key
and both layers; at the record-to-lab-file step it surfaces from the host
spec, naming the lab file, the element, the host and the cred. A `proxy` no
loaded `init` module registers fails the same way, naming the cred.

Other stores plug into the same seam: {doc}`../extend/creds-backends` is
the contract and the conformance helper. NetBox holds no credentials, so a
NetBox-backed inventory always pairs with a `[creds]` store — moving from the
json inventory to NetBox migrates no credentials at all.

## The NetBox backend

Nothing is required of NetBox beyond a device per referenced host whose `name`
is the key, an address the configured `ip_source` can read, and a read-only
token. A host NetBox models as a **virtual machine** can be the key too, once
you set `virtual_machines = true` ({ref}`netbox-virtual-machines`).

| Key | Meaning |
| --- | ------- |
| `url` | Base URL of the instance. A trailing slash is normalised away, so two spellings are one inventory. |
| `token_env` | Name of the environment variable holding the API token; default `NETBOX_TOKEN`. The token itself never sits in a settings file. |
| `filter` | A NetBox device filter, forwarded verbatim (`{ site = "lab-a" }`). No filter means every device. |
| `virtual_machines` | Also read NetBox virtual machines; default `false`. Off, the backend never asks NetBox's virtualization app anything, so a token that can read only devices keeps working. |
| `vm_filter` | A NetBox virtual machine filter, forwarded verbatim as `filter` is to devices (`{ cluster = "lab-vms" }`). No filter means every virtual machine. Setting it without `virtual_machines = true` is an error. |
| `ip_source` | Where a device's management address comes from: `"primary_ip4"` (default), `"oob_ip"`, or `"cf:<custom field>"`. |
| `vm_ip_source` | Where a virtual machine's management address comes from: `"primary_ip4"` (default) or `"cf:<custom field>"`. NetBox has no out-of-band address on a virtual machine, so `"oob_ip"` is refused. Devices keep their own `ip_source`. Setting it to anything but the default without `virtual_machines = true` is an error. |
| `custom_fields` | Record field → NetBox custom-field name. Each mapped field **joins** `supplies`. |
| `extra_custom_fields` | A list of custom-field names to carry through opaquely in `extra`. They are not record fields and never join `supplies`. |
| `timeout` | Seconds one request to NetBox may take; default `30`, and it must be a positive number. It bounds an instance that has gone *unreachable* — without it the kernel's TCP connect timeout (around two minutes) does, and it does so **before** the stale snapshot below is served. |

otto verifies the NetBox instance's certificate against the **operating
system's certificate store** — the same trust a browser on that machine has —
and against nothing else: there is no bundle-path key and no way to switch
verification off. An inventory error ending in `SSLError: …` therefore means
the operating system does not trust the instance. Install your organisation's
CA (or, for a self-signed instance, its own certificate) in the OS store with
the same steps a dashboard viewer uses — see [each viewer trusts the
CA](../cli/monitor/serving.md#creating-the-certificates) — and the error goes
away with no settings change. On Linux without root, set `SSL_CERT_FILE` to
the certificate (or `SSL_CERT_DIR` to a hashed directory); these are
OpenSSL's own overrides and `curl`, `git` and otto all honour them — on
macOS and Windows the platform store is the only source, so the CA must be
installed there.

The token is read at the **first fetch**, not at construction. A project that
declares a netbox table can therefore still run every verb that does not need
the inventory — and a lab with no referenced entry never talks to NetBox at
all.

### The mapping

How record fields map to NetBox device fields (a virtual machine maps the same
way, less `rack`, `shelf` and `board`; {ref}`netbox-virtual-machines` lists the
differences):

| Record field | NetBox device |
| ------------ | ------------- |
| the key | `name` |
| `ip` | the address at `ip_source`, prefix length stripped; a value that is not an address ({ref}`host-ip-field`) fails the fetch, naming the device and the `ip_source` |
| `site` | `site.name` |
| `rack` | `rack.name` (absent when unracked) |
| `shelf` | the integer part of `position` — which NetBox serialises as a string, and which may be a half-U value like `"3.5"` |
| `board` | `device_type.model` |
| `os_name` | `platform.name` |
| `is_virtual` | `false` for devices, `true` for virtual machines (`virtual_machines = true`) |
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
  `ip` has `ip_source`, `creds` come from the `[creds]` store or the lab file,
  and `interfaces` is a structure no custom field holds.
- A mapped field of the wrong NetBox type — `element_id` mapped to a text field
  — fails the record's validation naming the device and the field.
- `extra_custom_fields` must be a **list**, and may not name any of `id`,
  `serial`, `asset_tag`, `status` or `tags`: this backend already puts those in
  `extra` from the device itself. With `virtual_machines = true`, `cluster` is
  taken too.

(netbox-virtual-machines)=
### Virtual machines

Teams that model lab fixtures as NetBox virtual machines turn them on next to
the devices:

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
filter = { site = "lab-a" }            # devices
ip_source = "oob_ip"                   # devices are managed out of band
virtual_machines = true
vm_filter = { cluster = "lab-vms" }    # virtual machines
vm_ip_source = "primary_ip4"           # the default; a VM has no oob_ip
```

Each virtual machine becomes a record keyed by its `name`, mapped as a device
is, with these differences:

| Record field | NetBox virtual machine |
| ------------ | ---------------------- |
| `ip` | the address at `vm_ip_source`, read as a device's is read at `ip_source` |
| `site` | `site.name`, when the virtual machine has a site |
| `os_name` | `platform.name`, when it has a platform |
| `rack`, `shelf`, `board` | not populated: a virtual machine has no rack, position or device type |
| `is_virtual` | `true`, always |
| `extra` | `id`, `cluster` (the cluster's name), `status`, `tags` (names), plus any `extra_custom_fields` |

Custom-field mappings apply to virtual machines unchanged, with one exception:
a mapped `is_virtual` applies to devices only (an emulator modelled as a
device can say it is virtual), because a virtual machine is virtual whatever a
custom field says. A virtual machine with no name, or no address at
`vm_ip_source`, is skipped the way a device is, and `otto inventory list`
reports it as a virtual machine.

Devices and virtual machines share **one** set of keys. A device and a virtual
machine with the same name fail the fetch, naming both:

```text
netbox inventory https://netbox.example: duplicate name 'edge-17' (device id 3 and virtual machine id 7)
```

Rename one of them in NetBox, or narrow `filter` or `vm_filter` so that only
one is selected.

Both listings share one session, so `timeout`, certificate checks and error
reporting are the same for both. A token that cannot read virtual machines
fails the fetch with the inventory error described below, which then says
which listing failed:

```text
netbox inventory https://netbox.example: listing virtual machines: RequestError: …
```

### What a fetch skips, and what fails

The whole filtered set is fetched once, on first use, into a dict keyed by
device name (and virtual machine name, with `virtual_machines = true`); every
lookup after that is a dict hit.

- A device with **no name** is skipped.
- A device with **no address** at `ip_source` is skipped.

Both are dropped from `list_keys()`, and `otto inventory list` reports how many
of each the fetch passed over, so a device "missing from otto" shows as
selected and skipped rather than never seen. Looking up an addressless device
by name says exactly that, naming the device and the `ip_source`.

A filter that trims NetBox's list output (`fields`, `omit`, `brief`) still
works: an object the list left any field otto reads out of is read from its
detail page instead, one extra request per object, inside the same fetch.
Everything otto takes from that object, its name included, then comes from
the detail page alone (when it is found by `id`), so an object renamed or corrected since the listing is
read as it is now. That page is found by the object's `id`, or by its `url`
when a `fields` projection left out `id`; a projection that keeps neither
fails the fetch, naming the object and the field. Loaded by `url`, the object
keeps any value the list served that its detail page omits. A full list response costs
no such request, and neither does an object the list already shows is
skipped: one served with no name, or with an empty address. A field the
detail page lacks fails the fetch, naming the object and the field, except
the two above: no name means unnamed and no address field means addressless,
even when the list served one.

Two devices sharing a name within the filtered set is an error naming both
device ids. Connection, TLS, authentication and API failures all raise a single
inventory error naming the URL — never a raw traceback out of the HTTP client.

## Caching remote inventories

A backend is wrapped in the snapshot cache when its registration declares
`snapshot_cache=True`, `cache_ttl` is greater than zero, and its
`fingerprint()` is `None` — the backend's own statement that it cannot report
freshness. The registration decides, not the backend's name: the built-in
`json` backend declares `snapshot_cache=False`, NetBox declares `True` and
reports no fingerprint unconditionally, and a third-party backend that
returns a string opts out ({doc}`../extend/inventory-backends`).

- **`cache_ttl`** is `"0"`, or `<n>m` / `<n>h` / `<n>d` — no fractions, no
  whitespace, no leading zeros, no other units. The default is `"24h"`. `"0"`
  means every process fetches, which is how an uncached backend behaves.
- **Where.** `<otto home>/inventory-cache/` — `~/.otto/inventory-cache/` unless
  `OTTO_HOME` says otherwise. One snapshot per distinct inventory
  configuration — the backend's parsed table, defaults filled in, plus
  `cache_ttl` — named for a hash of it (a key a newer otto added, such as
  NetBox's `virtual_machines`, is left out while it holds its default, so a
  table written before it keeps its snapshot), written whole-or-not-at-all at mode
  `0600` with a small meta file beside it. Two projects whose tables parse to the
  same configuration share one snapshot. `otto inventory refresh` prints the
  snapshot's full path, which saves you guessing the hash.
- **Fresh.** A snapshot younger than the TTL is served **without contacting the
  backend** — the ordinary otto invocation costs one file read. Older, a fetch
  runs and the snapshot is rewritten atomically.
- **A failed fetch, with a snapshot on disk.** Whether the snapshot is served
  depends on the error the backend raised (below). When it is served, a
  warning names its age and its fetch time in UTC; when it is refused, the
  error names them instead. With no readable snapshot at all, the failure is
  the error, as it would be uncached.
- **The snapshot is a stage-1 document.** You can copy one out of
  `inventory-cache/` and point a `json` inventory at it.

```{note}
**Upgrading.** The snapshot key now comes from the backend's normalized
configuration and `cache_ttl`, so every existing snapshot misses once and is
fetched again. The old snapshot files are left in `inventory-cache/`; delete
them at leisure.
```

```{important}
The `otto inventory` read verbs print the stale-snapshot notice before their
own output — including `export`, whose artefact is then stale, and `diff`,
whose "no differences" is then against a stale left side.
```

`otto inventory refresh` fetches unconditionally, whatever the TTL says, and
reports the replaced snapshot's timestamp in **your local time** with its age.
Run against an inventory that has no snapshot — a `json` one, or a remote one
with `cache_ttl = "0"` — it exits 1 saying which of the two it is, so
`otto inventory refresh && …` does not proceed as though a fetch had happened.

### When an expired snapshot is served

A backend reports a failed fetch with one of three errors, and the error
decides whether the snapshot on disk may stand in for the fetch:

| The backend raised | The snapshot is served |
| ------------------ | ---------------------- |
| {class}`~otto.inventory.InventoryError` (a plain one) | At any age, with the warning. The built-in NetBox backend raises only this, so NetBox behaves exactly as before, and a `[inventory]` table for NetBox takes no `max_stale`. |
| {class}`~otto.inventory.InventoryUnreachableError` (a transient outage) | Only when the backend's `max_stale` is set and the snapshot's age is at most `max_stale`, with the warning. Otherwise the fetch fails with an `InventoryUnreachableError` naming the snapshot and its age. |
| {class}`~otto.inventory.InventoryDataError` (bad data from the source) | Never. The fetch fails with an `InventoryDataError` naming the snapshot and its age. |

**`max_stale`** is a setting of a backend that raises
`InventoryUnreachableError`, written beside its other keys, in the
`cache_ttl` grammar (`"48h"`, `"2d"`). Its default is unset, so an
unreachable backend's expired snapshot is never served. `max_stale = "0"`
means the same as leaving it unset: an expired snapshot is never served
(unlike `cache_ttl = "0"`, which turns caching off). Five things to know:

- It only bounds serving after a **failed** refresh. A fresh snapshot,
  younger than `cache_ttl`, is served without a fetch whatever `max_stale`
  says.
- It counts the snapshot's **total** age since it was fetched, not the time
  past `cache_ttl`. With `cache_ttl = "24h"` and `max_stale = "48h"`, a
  snapshot fetched 30 hours ago is served during an outage, one fetched 49
  hours ago is not, and one exactly 48 hours old still is.
- It is policy, not data: it is **not** part of the snapshot's identity, so
  raising it during an outage serves the snapshot you already have rather
  than looking for a new one. Two declarations of one inventory must still
  agree on it.
- The age is taken when the fetch gives up, so a slow timeout cannot serve
  a snapshot that passed `max_stale` while otto waited. The warning and the
  refusal name the command that refreshes this inventory:
  `otto inventory refresh --inventory physical` for a named one.
- Which keys a backend takes is the backend's to say: check its
  documentation for `max_stale`.

## What the doctor checks

`otto init` in a project validates every host entry the way the loader will,
against the inventories that project would resolve. Problems fail the run
(exit 1); warnings never change the exit code. The resolved inventory's label
is printed once, so you can see which one answered:

```text
inventory: json:/home/me/lab/inventory.json
```

and a second row, `creds: json:/…/creds.json`, when a store resolves.

The doctor checks every inventory the project uses, not only `default`
({ref}`named-inventories`):

- `default` is the project's own `[inventory]` (or its `[[inventories]]`
  entry named `default`), else the user file's. That is how the doctor chose
  it before named inventories, even where `otto` run with several active
  projects resolves `default` differently; see #1186.
- Each name the project's own settings file declares is built from that file
  alone, so `otto init` checks exactly what it just wrote.
- Any other name a lab file references, such as a `physical` another active
  project declares, comes from the active projects' inventories when the
  project is active. One name at a time: a disagreement about `default`, or
  about another name, never fails the name being checked. A project not yet
  on `OTTOPATH` takes such names from the user file's `[[inventories]]`.

Each inventory other than `default` that resolves gets its own label row:

```text
inventory physical: json:/home/me/lab/physical.json
```

When the doctor checks more than `default`, every finding about one
inventory begins with that inventory's name, so two inventories with the same
label are told apart:

```text
physical: inventory 'json:/home/me/lab/hosts.json': 1 record(s) referenced by no lab file here: b …
```

A project that uses only `default` gets its findings exactly as before, with
no name in front.

### Problems

- A dead reference — a key the inventory does not hold — naming the file, the
  element, the host index, the key and the inventory label (and the
  inventory's name, as above).
- A reference to an inventory name that is not configured, listing the names
  that are.
- A bare reference in a project that declares `[[inventories]]` without
  binding `default` itself, naming the qualified form to write instead. It
  is never counted as a reference to `default`.
- A referenced entry when **no** inventory is configured, naming both places it
  could have been declared.
- A malformed reference (the empty string, a non-string).
- An inventory-owned field stated inline beside `inventory`. When an entry does
  both — states a supplied field *and* names a key that does not exist — the
  collision is what you are told about first.
- A broken `[inventory]` declaration, or an unparseable user settings file,
  reported **once** rather than once per referencing entry. Those entries are
  skipped for that run and validate again once the declaration is fixed. The
  same holds for any inventory that fails to build: a broken `[[inventories]]`
  entry, or two active projects that declare one name differently, which is
  reported naming both files. The other inventories are still checked. A
  project with no lab files, one that only supplies inventories to its
  siblings, still has each inventory its own settings file declares built,
  and a failure is reported once under `settings`.
- `[creds]` declared with no `[inventory]` resolvable, naming the file.

### Warnings

Each is reported for `default` and for each inventory the project's own
settings file declares.

- A **stale snapshot** the doctor was served because the remote backend was
  unreachable, naming its age and the `otto inventory refresh` that replaces
  it. `otto init` reports it in the table itself, as the `otto inventory` verbs
  do.
- **Orphan records** — keys no lab file in this project references, up to ten
  of them by name and a count for the rest. Only the references that select
  that inventory count: `{"name": "physical", "key": "a"}` counts for
  `physical` alone, and never for a `virtual` that also holds `a`. During the bridge the inventory is
  expected to be wider than any one project, so this is information, not a
  defect. A project that has adopted nothing yet gets it listing everything,
  which is exactly the state the bridge starts from — the warning is how you
  watch that list shrink.
- A creds-store file that is group- or world-readable, naming the mode, or one
  that is named and does not exist.
- **Orphan creds** — keys the store holds that the inventory does not, up to
  ten by name: a renamed or retired inventory key leaves its creds behind.

## The verbs

`otto inventory` reads the configured inventory. Every verb is read-only, needs
no lab and touches no host — see {doc}`../cli/inventory/index` for the full
treatment, including the three exit codes `diff` uses.

Each verb reads `default` unless `--inventory NAME` names one of the
[named inventories](#named-inventories): `otto inventory list --inventory
physical` lists `physical`'s records, and builds no other inventory.

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

Completion resolves the inventories **once per process**, the way commands do:
`default` and every [named inventory](#named-inventories), each from the
projects on `OTTOPATH` that agree on its declaration, else the user file
([Resolution order](#resolution-order)). A project that only *references* keys
therefore completes against an inventory another project — or the user file —
declares, and a broken declaration of any name empties completion for every
project, not just the one carrying it, until it is fixed. A qualified
reference completes through its named inventory, and every named inventory
contributes to the fingerprint: editing `virtual`'s file refreshes completion
as editing `default`'s does. A workspace without `[[inventories]]` fingerprints
exactly as before. A bare reference that a project refuses
([Named inventories](#named-inventories)) offers no host, and the refusal is
listed with the other dropped entries. `otto inventory … --inventory <TAB>`
offers every declared name, read from settings alone: no inventory is built or
fetched for it.

Completion is not yet availability-aware: it still offers the hosts of a lab
marked `unavailable` ({ref}`lab-files`), which `--lab` refuses to load.
[#1185](https://github.com/ludachrish3/otto-sh/issues/1185) tracks bringing
completion in line with what a command would dispatch.

An entry completion could not build is skipped silently, never warned about,
so when a host does not come up, ask
[`otto cache info`](../cli/cache/index.md#info): its closing block shows the
inventory as completion resolved it, the hosts offered, and every entry
dropped with the reason.

Two situations leave completion **uncached**. Both stay correct — the ids still
come from a real load — and cost only speed:

- The inventory reports `fingerprint()` as `None` with no snapshot cache in
  front of it to supply one — the case when `cache_ttl = "0"` turns the cache
  off.
- The inventory's `fingerprint()` **raises** — a broken backend. A
  `fingerprint()` answers from local state and never touches the network
  ({ref}`the contract <inventory-fingerprint-contract>`), so a raise is a bug in
  the backend, not an outage. Otto never lets it reach your shell, and nothing
  is cached.

## Adoption path

Three stages, each with the verb that proves it. The **references** never
change between them ([The key](#the-key)), so a lab file is touched at stage 2
only where NetBox turns out not to supply a field the JSON file did (step 3
below).

### Stage 1 — the JSON inventory

- **One file**, owned like code — its own small repository, or a directory the
  lab team reviews changes to. A `$schema` line, sorted keys (`otto inventory
  export` normalises them). Split it along *ownership* lines if you must, never
  along otto lines: the `json` backend reads one `path`.
- **Decide the naming scheme once**, in that file's README, before the first
  entry. The keys are the device's canonical name — see [The key](#the-key).
- **Records carry facts only**: the management address, test-network
  interfaces, `is_virtual`, the location fields where you know them, versions
  as the declared state. If a value only matters to otto, it belongs in the lab
  file. `labels` are facts *about the instance*, available to templates, and
  never otto behaviour: `os_type`, terms and option tables stay in otto's
  files.
- **Declare `supplies` to match what your eventual owner will supply.** The lab
  files are then already in their stage-2 shape, and `otto inventory diff`
  compares like with like. Leaving it at the default (everything) is fine for a
  file-only future; it just means more re-homing later.
- **Credentials in a `[creds]` store from day one**, mode `0600`, outside every
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
6. Point `[inventory]` at NetBox and keep `[creds]` exactly as it was.
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
  every project's doctor. A rename is a retirement plus a new device plus a
  lab-file edit.
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
  to it.
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
against it are in {doc}`../extend/inventory-backends`.

## Worked example — the unix lab

Three virtual hosts in a `unix` lab, one of which is also a member of
`busybox`. The referenced hosts below build **identically** to the same three
hosts declared inline. (The `_note` keys are comment space — `_`-prefixed keys
are ignored in every otto JSON file.)

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

`~/.otto/creds.json` — the creds store, mode `0600`, keyed by the same
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
supplies = ["ip", "interfaces", "is_virtual", "site", "rack", "shelf", "board", "os_name"]

[creds]
backend = "json"
path = "~/.otto/creds.json"
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
                    "valid_terms": ["telnet", "ssh", "console"],
                    "valid_transfers": ["nc", "scp", "sftp", "ftp"],
                    "console_options": {"server": "test1", "port": 4001},
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
                    "docker_priority": 10,
                    "valid_terms": ["ssh", "telnet"],
                    "valid_transfers": ["scp", "sftp", "ftp", "nc"]
                }
            ]
        }
    ]
}
```

**How it correlates at load.** For the `test2` host entry — `{"inventory":
"test2", "os_type": "unix", …}` — the loader builds `test2`'s `Element` and
calls `resolve_host_entry(host_data, inventory=inventory, element=element)`: it looks up
`test2`, finds no inventory-owned field stated inline — had the entry also
said `"ip": …`, that is the error at the top of this page — copies `ip`,
`interfaces`, `is_virtual`, `site`, `rack` and `shelf` onto the entry, and the
creds store supplies `creds`, the lab file may layer routes over them, and the
host spec then validates the whole thing
exactly as it would an inline entry, the host id is still `test2` from the
element, and the host carries its provenance as
`host.inventory_ref` — the key, the backend label and the record's `extra`.

The `busybox` guests (`bb1161`…) stay inline — they are QEMU guests behind
`test1` with no life outside this bed, which is what the inline form is for.

Moving this deployment to NetBox starts with one file, `~/.otto/settings.toml`:

```toml
[inventory]
backend = "netbox"
url = "https://netbox.example"
token_env = "NETBOX_TOKEN"
filter = { site = "lab-a", status = "active" }

[creds]
backend = "json"
path = "~/.otto/creds.json"
```

…with devices named `test1`/`test2`/`test3` carrying primary IPv4s, site
`lab-a`, rack `1` and positions `3`/`3`/`4`, all of which NetBox models
natively. `supplies` is then the native set — which does **not** include
`interfaces`, so this deployment's test-network interfaces move back into the
lab file at that step. `otto inventory diff ~/lab/inventory.json` is what shows
you that, field by field, before you switch; run it until only the differences
you intend are left.
