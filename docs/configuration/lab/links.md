(lab-links)=

# Links

A `links` entry in `lab.json` declares a data-plane route between two hosts —
distinct from the `hop` field's SSH/telnet *management* path. It is resolved
into a runtime `Link` object (`otto.link`) at lab-load time: the edge that
`otto tunnel add` (see {doc}`../../cli/tunnel/index`) rides to actually stand up traffic, and
that `otto link impair` (see {doc}`../../cli/link/index`) targets to impair it:

```json
{
    "name": "data-plane-a",
    "endpoints": [
        { "host": "test1", "interface": "eth1" },
        { "host": "test2", "interface": "eth1" }
    ],
    "protocol": "udp"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `endpoints` | array of exactly 2 objects | The two ends of the route, each `{"host": <id>, "interface": <netdev name>}`. |
| `endpoints[].host` | string | Host id (see {ref}`lab-files`) — must resolve to a host loaded from *some* lab file. |
| `endpoints[].interface` | string | A key in that host's `interfaces` map (see {doc}`network-interfaces`). **Required only when the host defines more than one interface** — with one interface (or none) otto assumes it and its IP. Omitting it on a host with more than one interface is a load-time validation error ("ambiguous interface — specify one of {…}"), since otto can't disambiguate. |
| `protocol` | string | Optional; defaults to `"tcp"`. Informational for a declared link (documents what the route carries — e.g. `"udp"`, `"rtp"`); the analogous `otto tunnel add --protocol` flag is what actually drives socat UDP-vs-TCP when a tunnel is built over this route. |
| `name` | string | Optional friendly handle; the link's id is otherwise derived from its endpoints. |
| `impair` | string | Optional in-path middlebox **host id** — a bare string, validated as a known host reference at lab load. When set, `otto link impair` (see {doc}`../../cli/link/index`) places both directions' netem on that middlebox's live-resolved facing interfaces instead of the link's own endpoints. `None` (the default) is endpoint-anchored impairment. |
| `management` | string | Optional; accepted but currently has no effect. |

**Lab membership is derived, not authored** — a link carries no membership of
its own. It belongs to the union of both endpoints' *elements'* labs: loading
lab `unix` surfaces every link with at least one endpoint in `unix`, even one
whose *other* endpoint lives in a different lab (that far endpoint renders as
a stub/dangling node). A link can legitimately span two labs.
