# Referencing the inventory

A host entry may name an **inventory key** instead of carrying the machine's
own facts:

```json
{ "inventory": "carrot-b1", "os_type": "unix", "hop": "gw" }
```

Otto then fills in the fields the configured inventory declares it supplies —
typically `ip`, `interfaces` and the location fields — and every one of those
must be **absent** from the entry, or the load fails naming the field and the
key.  `creds` is the exception: stated here beside a reference it **composes**
over the inventory's, field by field within a login ({ref}`credentials-layered`).
Fields the inventory does not supply stay here exactly as for an inline host,
and an entry with no `inventory` key is unaffected: the two forms sit side by
side in one file.

`"inventory": null` means the same as no `inventory` key at all — the entry is
inline.  The empty string is an error.

## Bare and qualified references

A reference has two spellings.  The **bare** one above is a key on its own,
and it always means the inventory named `default`: the one an `[inventory]`
table declares, in the project's settings file or your own.  Otto never
searches other inventories for a bare key.  A project that declares
`[[inventories]]` without binding `default` refuses bare keys
({ref}`named-inventories`).

The **qualified** one names the inventory as well as the key:

```json
{ "inventory": { "name": "physical", "key": "edge-17" }, "os_type": "unix" }
```

It is an object with exactly two members, `name` and `key`, both non-empty
strings; any other member (a misspelled `kye`, say) is an error that names
it.  `{"name": "default", "key": "edge-17"}` and `"edge-17"` reference the
same record.  The member is called `name`, not `source`, because it names an
inventory, and `[[lab.sources]]` already means where lab files come from: a
reference must never read as one of those.

A qualified reference consults only the inventory it names, and the fields
that inventory supplies are the ones that must be absent from the entry: two
inventories may supply different fields.  A name no active project or your
user file declares is an error that lists the declared ones
({ref}`named-inventories`).

Which fields the inventory owns is a per-deployment decision, not a per-host
one, and it is made in `[inventory]` — see {doc}`../inventory` for the settings
table, the backends, and what happens at load.

```{important}
**A referenced Unix host's probe pin (BusyBox included) goes here, beside
`inventory`, never in the inventory record:** paste what
`otto --lab <lab> host <id> probe` prints into this entry. Unpinned, each otto
command that needs its userland probes it again. {doc}`probing-hosts` shows
what the pin looks like and where it goes.
```

## Provenance

A host built from a reference carries where its facts came from as
`host.inventory_ref`: the `key`, the inventory's `backend` label, the record's
opaque `extra`, the inventory's `name` (`default` for a bare reference) and
`qualified`, whether the entry named it.  An inline host carries an empty one,
whose `name` is `""`.

## When two declarations are one machine

Combining labs with `+` (`--lab lab_a+lab_b`) keeps one host per id, the
later lab's.  Two declarations of one id must describe the same machine, or
the load fails naming both:

- Their addresses must be equal.
- An inline declaration at that address is the same machine as any other
  there.
- Two references must name the same inventory: `physical/edge-17` and
  `virtual/edge-17` (each written `name/key`, as the error writes them) at
  one private address are two isolated machines, not one.
- Two qualified references must also name the same key.  A bare key is an
  alias, as it always was: two bare keys at one address are one machine.

Every declaration of the id counts, not only the last one merged, so the
verdict never depends on the order or grouping of the labs: `physical/edge-17`,
an inline host and `virtual/edge-17` at one address fail in every order, and so
do `{"name": "default", "key": "a"}`, a bare `"a"` and `{"name": "default",
"key": "b"}`.
