# In-path impairment

By default (**endpoint mode**) a link's two directions land on the netem
placement resolved by their own physical endpoint — the {ref}`lab-links`
`endpoints[].interface`. A link can instead declare an `impair` field: a
**bare host id** naming an in-path middlebox that services the link's
impairment instead:

```json
{
    "name": "dataplane",
    "endpoints": [
        { "host": "carrot_seed", "interface": "eth1.100" },
        { "host": "tomato_seed", "interface": "eth1.200" }
    ],
    "impair": "pepper_seed"
}
```

With `impair` set, both directions place on `pepper_seed` instead of the
endpoints, and the facing interface toward each endpoint is auto-resolved — you
never declare it. See {doc}`../../../architecture/subsystems/network` for how that
resolution works and why a middlebox that isn't actually in the path fails
loud.

The link-level `impair` field names only *where* impairment is serviced; the
host-level `impairer` pin (see [Custom impairers](../../../library/network-api.md#custom-link-impairers)) separately
selects *which* `LinkImpairer` a host uses — one field per concern.

```{note}
**Netdev granularity.** A netem qdisc attaches to an *interface*, not a
flow. If two links resolve their placements onto the **same** middlebox
interface — e.g. two endpoints sharing one segment behind the middlebox —
impairing one link impairs both: they share the qdisc, a second `impair`
merges over the first exactly as a re-impair of the same link would, and
`otto link list` will truthfully report both links impaired. There is
currently no flow-scoped (per-destination) impairment on a shared
interface; in the common one-interface-per-segment middlebox layout,
placements never collide.
```

