# Host inventory

`test1`'s entry on the previous page states two kinds of thing. *Machine
facts* — address, interfaces, rack, shelf — are true whatever tool asks. *otto
facts* — `os_type`, the transfer menu, `docker_capable` — mean nothing to any
other tool. An inventory is where the first kind lives when a team already
keeps it somewhere: a JSON file to start, NetBox later.

The one rule: **data lives in exactly one layer.** The inventory declares
which fields it supplies; an entry that references it may not also state
those fields inline; the join is a copy, never a merge. Two sources of the
same fact is the situation this design refuses to have.

## The entry, by reference

Declare the inventory once — at user level in `~/.otto/settings.toml`, or
per project as the example does:

```{literalinclude} ../../examples/getting-started-inventory/.otto/settings.toml
:language: toml
:start-after: "# doc: begin inventory-config"
:end-before: "# doc: end inventory-config"
```

`supplies` is the partition for machine facts. The same `test1` element now
carries otto's vocabulary, a key — and a `creds` list that names no password:
two login-only entries that pin the login order, and the `sudo-root` route
from {doc}`../customizations`. Creds are the one field that **composes**
across the files rather than living in exactly one of them:

```{literalinclude} ../../examples/getting-started-inventory/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test1"'
:end-before: '"_doc_end": "test1"'
```

The record behind the key, in `inventory.json`, holds the machine facts and
nothing else — `os_type` would be refused there, exactly as `ip` is refused
in the entry above:

```{literalinclude} ../../examples/getting-started-inventory/inventory.json
:language: json
```

Passwords live in the creds store `[creds]` names — `creds.json` beside it,
which must be kept at mode `0600` (the doctor warns otherwise) — so the
inventory file itself can be world-readable:

```{literalinclude} ../../examples/getting-started-inventory/creds.json
:language: json
```

The same keys, each holding the logins that key's host accepts. Within a
login, `lab.json` overrides `inventory.json`, which overrides `creds.json`,
field by field, and `lab.json`'s order is the login order.
{ref}`credentials-layered` is the home for the rest — the store backends, the
merge rules, and what the doctor checks.

## Asking the inventory

```{literalinclude} ../../examples/getting-started/captures/inventory-lookup-test1.txt
:language: text
```

```{literalinclude} ../../examples/getting-started/captures/inventory-list.txt
:language: text
```

`otto init --lab` doubles as the doctor: it re-reads the lab area against the
loader every command uses, so a dead reference, a field stated on both sides,
a creds-store file readable by more than its owner, or a creds key the
inventory no longer holds are all named before any host is contacted
({doc}`../../guide/configuration/inventory` lists each check).

## Growing out of the file

The JSON inventory is stage one of three. Stage two is NetBox — the same
`supplies` rule, the fields NetBox natively holds, and `otto inventory
export`/`diff` to migrate — and stage three is keeping NetBox otto-healthy.
{doc}`../../guide/configuration/inventory` walks all three under *Adoption
path*; this page does not repeat it. When the answer is neither JSON nor
NetBox, {doc}`../../library/inventory-backends` is the contract a backend
implements and the conformance test it must pass.

Both forms of this lab load to the same hosts — every machine fact, every
interface, and every credential, the proxied `root` included: the twin's
`lab.json` carries the route, `creds.json` the passwords, and the merge
composes them. That is not a claim, it is a test: a guard in otto's own test
suite builds both and compares them cred for cred.
