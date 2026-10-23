# Network interfaces

The optional `interfaces` field maps a network-device name to that device's
address, so {doc}`links <links>` and later impairment/capture tooling can address a
specific device directly instead of just the host's management `ip`.

| Field | Type | Description |
|-------|------|-------------|
| `interfaces` | object | Map of netdev name (e.g. `"eth0"`, `"eth1"`) to an interface definition. |
| `interfaces.<name>` | object or string | `{"ip": "10.0.0.5"}`, or the bare string `"10.0.0.5"` as shorthand for the same object. |
| `interfaces.<name>.subnet` | string | Optional network the interface belongs to, in CIDR form (`"192.168.1.0/24"` — the network address, not a host address). Declares the interface's L3 neighborhood for topology and reachability tooling; when set, the interface `ip` must fall inside it (validated at load). |

```json
{
    "interfaces": {
        "eth0": "10.0.0.5",
        "eth1": { "ip": "192.168.1.5", "subnet": "192.168.1.0/24" }
    }
}
```

A host with no `interfaces` (or exactly one entry) needs no `interface` on a
declared-link endpoint — otto assumes it. A host with more than one entry
requires each declared-link endpoint on it to name which one; see
{ref}`lab-links`.
