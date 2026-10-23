# SNMP monitoring

The optional `snmp` block configures SNMP polling for a host's metrics — add
it to a host entry in `lab.json` and `otto monitor` starts collecting for that
host.  See {doc}`../../cli/monitor/metrics` for what otto does with the readings.

The `address` and `port` are the endpoint reachable from the otto host — for
an embedded device behind a hop this is typically the local end of a UDP relay
on the hop host, not the device's own address.  `community` defaults to
`"public"`.  Presentation (label, chart group, unit) is supplied by the
descriptor registry, not by lab data.

| Field | Type | Description |
|-------|------|-------------|
| `snmp` | object | The SNMP polling block.  Omit it to leave the host unpolled. |
| `snmp.address` | string | SNMP agent IP address. |
| `snmp.port` | integer | SNMP UDP port. |
| `snmp.community` | string | SNMP community string. |
| `snmp.oids` | array of strings | OIDs to poll — raw dotted OIDs, named bundles (below), or a mix of both. |

Each entry in `oids` is either a raw dotted OID or one of otto's built-in
**named bundles**, which expand to a group of related OIDs and register
their descriptors as a side effect.  Bundles and raw OIDs mix freely in the
same list:

- `otto-core` — the five core scalars (uptime, overall CPU, heap used, heap
  free, thread count).
- `otto-net:N` — network OIDs for interfaces `0..N-1`.  `otto-net` alone
  (no `:N`) means `otto-net:1`, i.e. just interface `0`.
- `otto-fs:N` — filesystem OIDs for filesystems `0..N-1`, same `:N` default.

```json
{
    "snmp": {
        "address": "10.10.200.14",
        "port": 16101,
        "oids": [
            "otto-core",
            "otto-net:2",
            "otto-fs:1",
            "1.3.6.1.4.1.99999.1.5.0"
        ]
    }
}
```

`N` must be a positive integer.  An unknown bundle name fails fast at
monitor startup rather than silently polling nothing.  See
[Per-interface and per-filesystem OIDs](../../cli/monitor/metrics.md#per-interface-and-per-filesystem-oids)
in {doc}`../../cli/monitor/index` for what each expanded OID charts.
