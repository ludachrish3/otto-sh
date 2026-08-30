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

`supplies` is the partition. The same `test1` element now carries only otto's
vocabulary and a key:

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

Credentials are a `creds_file` beside it, so the inventory file itself can be
world-readable:

```{literalinclude} ../../examples/getting-started-inventory/creds.json
:language: json
```

The same keys, each holding the logins that key's host accepts.
{ref}`credentials-creds-file` is the home for the rest — where to keep the
file, what mode it wants, and what a record may not carry once it exists.

## Asking the inventory

```{literalinclude} ../../examples/getting-started/captures/inventory-lookup-test1.txt
:language: text
```

```{literalinclude} ../../examples/getting-started/captures/inventory-list.txt
:language: text
```

`otto init --lab` doubles as the doctor: it re-reads the lab area against the
loader every command uses, so a dead reference, a field stated on both sides,
a missing `creds_file` or one readable by more than its owner are all named
before any host is contacted ({doc}`../../guide/configuration/inventory` lists
each check).

## Growing out of the file

The JSON inventory is stage one of three. Stage two is NetBox — the same
`supplies` rule, the fields NetBox natively holds, and `otto inventory
export`/`diff` to migrate — and stage three is keeping NetBox otto-healthy.
{doc}`../../guide/configuration/inventory` walks all three under *Adoption
path*; this page does not repeat it. When the answer is neither JSON nor
NetBox, {doc}`../../library/inventory-backends` is the contract a backend
implements and the conformance test it must pass.

Both forms of this lab load to the same hosts — every machine fact,
interface and directly-loginable credential. The one thing the twin cannot
carry is the proxied credential {doc}`../customizations` adds, because a
proxy is registered by an `init` module and the twin has none. That is not a
claim, it is a test: a guard in otto's own test suite builds both and compares
them.
