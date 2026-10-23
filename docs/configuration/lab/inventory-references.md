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

Which fields the inventory owns is a per-deployment decision, not a per-host
one, and it is made in `[inventory]` — see {doc}`../inventory` for the settings
table, the backends, and what happens at load.
