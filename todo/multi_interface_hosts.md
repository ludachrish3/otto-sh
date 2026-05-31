# Multi-interface host definitions (IP-per-interface map)

## Motivation

Today a host has a single primary `ip`. Real hosts (and the Zephyr test bed)
often have several reachable addresses: a management interface, a data-plane
interface, a relay/agent endpoint, etc. The SNMP work made this concrete — the
agent is reached at a *different* address than the telnet `ip` (a relay
endpoint), so `SnmpOptions.address` was added as a one-off override that
defaults to the host's `ip`.

Chris's proposed direction: let a host declare a **dict of IP addresses keyed by
interface name**, so any feature that needs an address can name an interface
instead of hard-coding an IP — e.g.:

```json
"interfaces": { "mgmt": "10.10.200.14", "data": "192.0.2.1" },
"ip": "mgmt",                      // or keep ip as the primary literal
"snmp": { "interface": "mgmt", "port": 16101, "oids": [...] }
```

## Scope (deferred — not started)

This touches the host model broadly, which is why it's queued rather than folded
into the SNMP work:

- `RemoteHost` / `UnixHost` / `EmbeddedHost`: an `interfaces: dict[str, str]`
  field; decide whether `ip` stays a literal or becomes an interface key.
- `storage/factory.py` + `validate_host_dict`: parse/validate the map.
- Resolution helper: `host.address_for(name_or_literal)` that returns an IP
  given either an interface name or a literal address (back-compat).
- Consumers opt in: `SnmpOptions.address` would accept an interface name and
  resolve via the map; transport/hop code could too.
- Lab schema docs + migration for existing single-`ip` hosts.

## Forward-compatibility note

`SnmpOptions.address` is intentionally a plain string that defaults to the
host's `ip`. When the interface map lands, `address` can resolve against it
(literal IP still works), so nothing here blocks that future — the SNMP block
won't need a breaking change.
