# Host inventory

`test1`'s entry on the previous page states two kinds of thing. *Machine
facts* — address, interfaces, rack, shelf — are true whatever tool asks. *otto
facts* — `os_type`, the transfer menu, `docker_capable` — mean nothing to any
other tool. An inventory is where the first kind lives when a team already
keeps it somewhere: a JSON file to start, NetBox later.

The one rule: **data lives in exactly one layer.** The inventory declares
which fields it supplies; an entry that references it may not also state
those fields inline; the join is a copy, never a merge.

## The entry, by reference

This page's example is the project
`docs/examples/getting-started-inventory/`
({ref}`where to get it <bed-example-projects>`). Declare the inventory once — at user level in `~/.otto/settings.toml`, or
per project as the example does:

```{literalinclude} ../../../examples/getting-started-inventory/.otto/settings.toml
:language: toml
:start-after: "# doc: begin inventory-config"
:end-before: "# doc: end inventory-config"
```

`supplies` is the partition for machine facts. The lab file is again a
template, and this one reads the inventory. The lab source names the
inventories its template may read; `default` is the one `[inventory]`
declares:

```{literalinclude} ../../../examples/getting-started-inventory/.otto/settings.toml
:language: toml
:start-after: "# doc: begin lab-sources"
:end-before: "# doc: end lab-sources"
```

The template makes one element for each record in the inventory, and holds
only otto's facts:

```{literalinclude} ../../../examples/getting-started-inventory/lab_data/lab.jsonnet
:language: jsonnet
```

`otto.inventories.default` is the inventory's records, sorted by key. Each
holds its key and its machine facts, but never a credential.
`o.element(record.key, record, host, ['unix'])` comes from `otto.libsonnet`,
the library otto ships. It makes an element named after the record, a member
of `unix`, with one host: `host` plus a reference to the record,
`{"name": "default", "key": "test1"}` for `test1`
({doc}`../../../configuration/lab/inventory-references`). Every VM's `host`
is `unixVm`, otto's vocabulary, plus what `differs` holds for its key.

`test1`'s entry adds a `creds` list that names no password: two login-only
entries that pin the login order, and the `sudo-root` route from
{doc}`../customizations` (the {ref}`first page's warning
<bed-sudo-root-warning>` about that route applies here too). Creds are the one field that **composes** across
the files rather than living in exactly one of them.

A record added to `inventory.json` becomes a new element in the `unix` lab,
with no edit to the template. {doc}`../../../configuration/lab/templating`
is the reference for templates that read inventories.

The record behind the key, in `inventory.json`, holds the machine facts and
nothing else — `os_type` would be refused there, exactly as `ip` is refused
in the entry above:

```{literalinclude} ../../../examples/getting-started-inventory/inventory.json
:language: json
```

Passwords live in the creds store `[creds]` names — `creds.json` beside it,
which must be kept at mode `0600` (the doctor warns otherwise) — so the
inventory file itself can be world-readable:

```{literalinclude} ../../../examples/getting-started-inventory/creds.json
:language: json
```

The same keys, each holding the logins that key's host accepts. Within a
login, the lab file overrides `inventory.json`, which overrides `creds.json`,
field by field, and the lab file's order is the login order.
{ref}`credentials-layered` is the home for the rest — the store backends, the
merge rules, and what the doctor checks.

The inline entry on the previous page and this referenced one load to the
same host — every machine fact, every interface, and every credential, the
proxied `root` included: the lab file carries the route, `creds.json` the
passwords, and the merge composes them.

## Asking the inventory

```{literalinclude} ../../../examples/captures/inventory-lookup-test1.txt
:language: text
```

```{literalinclude} ../../../examples/captures/inventory-list.txt
:language: text
```

`otto init --lab` doubles as the doctor: it re-reads the lab area against the
loader every command uses, so a dead reference, a field stated on both sides,
a creds-store file readable by more than its owner, or a creds key the
inventory no longer holds are all named before any host is contacted
({doc}`../../../configuration/inventory` lists each check).

## Growing out of the file

The JSON inventory is stage one of three. Stage two is NetBox — the same
`supplies` rule, the fields NetBox natively holds, and `otto inventory
export`/`diff` to migrate — and stage three is keeping NetBox otto-healthy.
{doc}`../../../configuration/inventory` walks all three under *Adoption
path*; this page does not repeat it. When the answer is neither JSON nor
NetBox, {doc}`../../../extend/inventory-backends` is the contract a backend
implements and the conformance test it must pass.
