# otto tunnel add

```text
otto tunnel add --hosts <h0[@if0],h1[@if1],...,hn-1[@ifn-1]> --port <P> [--protocol tcp|udp] [--dest <host[@if]>]
```

```bash
otto --lab veggies tunnel add --hosts carrot_seed,tomato_seed --port 6001
otto --lab veggies tunnel add --hosts carrot_seed@eth1,tomato_seed@eth1 --port 6001 --protocol udp
otto --lab veggies tunnel add --hosts carrot_seed,compost,tomato_seed --port 6001
```

| Option | Required | Description |
| ------ | -------- | ----------- |
| `--hosts` | yes | Ordered, comma-separated `host[@iface]` path — **two or more entries**. The first and last entries are the tunnel's two endpoints; anything between is an explicit intermediate hop. |
| `--port` | yes | The service port, used at **both** endpoints — a client sends to `--port` on either endpoint host, and (absent `--dest`) it's delivered to `--port` on the other. One value keeps the tunnel traceable by port at every hop. |
| `--protocol` | no (default `tcp`) | The service protocol the endpoints speak, validated against the selected carrier's supported protocols. The default `socat` carrier supports `tcp` and `udp` and always relays between hops over a plain-TCP carrier stream. |
| `--dest` | no (default: loopback on the far endpoint) | Deliver the far endpoint's traffic on to a **third** host instead of terminating on that host's loopback — see *Relaying with `--dest`*, next. |
| `--carrier` | no (default `socat`) | Tunnel transport — a registered `TunnelCarrier` name, applied chain-wide. See [Custom carriers](../../../library/network-api.md#custom-tunnel-carriers). |

## `@iface` interface pinning

Each `--hosts` entry may pin a specific interface with `host@iface`, where
`iface` is a key in that host's `interfaces` map in `lab.json` (see
[Network interfaces](../../configuration/lab-config.md#network-interfaces)). The pin is only
**required** when the host defines more than one interface — with zero or
one interface, otto resolves it automatically. Naming an interface the host
doesn't have, or omitting `@iface` on a host with more than one, is a
load-time error that lists the interfaces it does have. Docker container
entries never take `@iface` — see
[Docker container endpoints](endpoints.md#docker-container-endpoints).

## Multi-hop chains

`--hosts` names the *exact* ordered path — otto builds only the chain you
specify; it never auto-routes from the lab's topology. `--hosts a,c,b`
tunnels through `c` as an explicit intermediate hop; `--hosts a,b` is
direct. Every hop in the chain — intermediate or endpoint — needs a working
`bash` and `socat` (see [Host requirements](endpoints.md#host-requirements)); an
intermediate hop only relays the carrier TCP stream, it never terminates
the tunneled protocol itself.

Each `add` places tagged processes on every host in the chain; how they are
laid out — two per host, mirrored so a brand-new flow can start at either
endpoint — is covered in {doc}`../../../architecture/subsystems/network`. A chain
host may not appear twice in `--hosts`, and the reverse of an existing tunnel's
path (`b,c,a` after `a,c,b`) is rejected as a conflict (see *Conflicts and
preconditions* below) rather than treated as a new tunnel.

## Relaying with `--dest`

By default a tunnel delivers to loopback (`127.0.0.1`) on both endpoint
hosts — the local service is expected to listen there, or on any address
other than the endpoint's own tunnel bind (see the loop-hazard note below).
Passing `--dest C` overrides delivery on the **far** endpoint only: the last
`--hosts` entry keeps its own two `socat` processes, but instead of handing
traffic to its own loopback it relays onward to `C`, so the packet `C`
receives is sourced from the far endpoint's own interface — an ordinary
`far-endpoint → C` packet, not a loopback- or SSH-sourced one the way an
`ssh -L` forward would deliver it:

```bash
otto --lab veggies tunnel add --hosts carrot_seed,tomato_seed --port 6001 --dest sprout
```

Here `carrot_seed` and `tomato_seed` are the tunnel's two endpoints and
`sprout` is where the far side's traffic actually lands — appearing to
`sprout` as if it came directly from `tomato_seed`. There is currently no
symmetric override for the near endpoint (`carrot_seed` here) — it always
delivers to its own loopback.

```{note}
**Why loopback, not the endpoint's own IP:** that IP is already bound by the
reverse chain's ingress listener, so delivering there by default would loop a
datagram straight back into the tunnel (see
{doc}`../../../architecture/subsystems/network`). A service that insists on binding
a wildcard address without `SO_REUSEADDR` can still collide with the ingress
bind; that failure is loud, surfaced by `add`'s post-launch verify.
```

## Conflicts and preconditions

`add` resolves every hop (and `--dest`), computes the tunnel's id, and
checks it against every **live** tunnel discovered right now — declared
`lab.json` links make no port claims, so they play no part in this check:

- **Id idempotency** — an existing tunnel with the exact same id (same
  ordered path, protocol, and port) is a duplicate: `add` refuses.
- **Endpoint-bind conflict** — no existing tunnel may already hold an
  ingress bind on the same `(host, port, protocol)` as either endpoint of
  the new one. This is what rejects a reversed path (`b,c,a` after
  `a,c,b` — both need ingress binds on the same two hosts) and any
  same-port re-plumbing over a different path or `--dest`.

`add` then spawns the tagged processes and **verifies** every one of them
actually came up before reporting success. If any is missing (a bind collision,
a port race, a host that turned out not to have `socat`), `add` tears down
everything it already started and raises, naming exactly what failed — no
half-built tunnel survives a failed `add`. How the spawn is ordered to make that
safe is covered in {doc}`../../../architecture/subsystems/network`.

