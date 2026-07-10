# otto tunnel

`otto tunnel` creates, lists, and removes **host-resident bidirectional
tunnels** — an ordered chain of `socat` processes, tagged and spawned
directly on lab hosts, that carries a service's traffic end-to-end across
one or more hops. A tunnel rides one or more **links**: the topology edges
declared in `lab.json`, or derived from each host's management `hop` (see
{ref}`lab-links` in {doc}`lab-config`). Links document routes that exist;
`otto tunnel add` is what actually stands traffic up over them — one `add`
builds exactly one tunnel, so a second `add` on the same route with a
different port is a second, coexisting tunnel. Links are the static
underlay, tunnels the dynamic overlay riding it — for impairing a link's
traffic (delay, loss, rate, ...) rather than tunneling over it, see
{doc}`link`.

Every capability is a plain callable first — `otto tunnel` is a thin CLI
wrapper over `otto.tunnel.add_tunnel` / `remove_tunnel` /
`remove_all_tunnels` / `discover_tunnels`. See the
{doc}`API reference <../api/tunnel>` to call them directly from an
instruction, a suite, or your own script.

```{note}
Every tunnel is **bidirectional** — a new flow can originate from either
end, each served by its own mirrored chain of processes. There is no
`--one-way` flag.
```

## Creating a tunnel: `otto tunnel add`

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
| `--protocol` | no (default `tcp`) | `tcp` or `udp`. Selects the `socat` address family the two endpoints speak; every intermediate hop always relays the carrier stream over plain TCP regardless. |
| `--dest` | no (default: loopback on the far endpoint) | Deliver the far endpoint's traffic on to a **third** host instead of terminating on that host's loopback — see *Relaying with `--dest`*, next. |

### `@iface` interface pinning

Each `--hosts` entry may pin a specific interface with `host@iface`, where
`iface` is a key in that host's `interfaces` map in `lab.json` (see
[Network interfaces](lab-config.md#network-interfaces)). The pin is only
**required** when the host defines more than one interface — with zero or
one interface, otto resolves it automatically. Naming an interface the host
doesn't have, or omitting `@iface` on a host with more than one, is a
load-time error that lists the interfaces it does have. Docker container
entries never take `@iface` — see [Docker container endpoints](#docker-container-endpoints)
below.

### Multi-hop chains

`--hosts` names the *exact* ordered path — otto builds only the chain you
specify; it never auto-routes from the lab's topology. `--hosts a,c,b`
tunnels through `c` as an explicit intermediate hop; `--hosts a,b` is
direct. Every hop in the chain — intermediate or endpoint — needs a working
`bash` and `socat` (see [Host requirements](#host-requirements)); an
intermediate hop only relays the carrier TCP stream, it never terminates
the tunneled protocol itself.

Each `add` places exactly **two** tagged `socat` processes on every host in
the chain — one per direction. Each chain is already bidirectional per flow
on its own: `socat` relays both ways over each connection, and a UDP `fork`
child stays connected to its peer, so **replies to an in-flight exchange
return over the same chain that carried the original packet**. What the
mirrored second chain adds is **initiation from the far end** — a brand-new
flow can start at either endpoint, not just the first-listed one. A chain
host may not appear twice in `--hosts`, and the reverse of an existing
tunnel's path (`b,c,a` after `a,c,b`) is rejected as a conflict (see
*Conflicts and preconditions* below) rather than treated as a new tunnel.

### Relaying with `--dest`

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
**Why loopback, not the endpoint's own IP:** under bidirectionality, the
endpoint's own data-plane IP is already bound by the *reverse* chain's
ingress listener — delivering there by default would loop a datagram
straight back into the tunnel. Loopback (or any non-bind address)
sidesteps that. A service that insists on binding a wildcard address
without `SO_REUSEADDR` can still collide with the ingress bind; that
failure is loud, surfaced by `add`'s post-launch verify.
```

### Conflicts and preconditions

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

`add` spawns the tagged processes — downstream-first per direction, so a
listener always exists before anything upstream tries to connect to it —
then **verifies** every one of the expected processes actually came up
before reporting success. If any is missing (a bind collision, a port
race, a host that turned out not to have `socat`), `add` tears down
everything it already started and raises, naming exactly what failed. No
half-built tunnel survives a failed `add`.

## Listing tunnels: `otto tunnel list`

```bash
otto --lab veggies tunnel list
```

`otto tunnel list` shows every live tunnel `discover_tunnels` finds right
now — the running, tagged `socat` processes ARE the record; there is no
separate ledger. Each row is:

`ID · ENDPOINTS (a ↔ b) · VIA · PORT · PROTO · AGE · STATUS`

- **VIA** lists the intermediate hops in path order, plus `→ <dest>` when
  the tunnel has a `--dest` override.
- **AGE** is the oldest observed process's age, humanized (`3h`, `2d`, ...).
- **STATUS** is `ok` when every expected process was found; `degraded
  (<present>/<expected>)` when some are missing on hosts that *were*
  reachable; either form gets a trailing `?` when at least one chain host
  couldn't be scanned this pass, so absence there means "unknown," not
  "gone."

## Removing tunnels: `otto tunnel remove`

```bash
otto tunnel remove <id>
otto tunnel remove --all
otto tunnel remove --all -y
```

`remove <id>` discovers every tagged process for that id across every host
that might be running one, kills them, then **re-scans** the hosts it just
killed on to confirm they're actually gone. `remove --all` reaps **every**
otto tunnel it finds — not just ones this invocation or this user created;
tunnel ownership isn't tracked (see [Tunnel identity](#tunnel-identity)
below). Because `--all` is destructive and owner-agnostic, it asks for
confirmation first; pass `-y` / `--yes` to skip the prompt (e.g. from a
script or CI cleanup step).

If any killed process is still alive on the post-kill scan, `remove` names
it as a survivor and exits non-zero — never a silent trust of the kill
command's own exit code.

## Tunnel identity

- Every tunnel gets an id of the form `tun-<hex>-<port>` — a
  12-hex-character hash of the ordered chain (every hop, in order, plus
  protocol) plus a readable `-<port>` suffix, e.g. `tun-0a17f76fb561-6001`.
  The port stays visible in `list`, in `remove <id>`, and in every tagged
  process's `argv[0]`, so two tunnels on the same route with different
  ports are visibly distinct.
- Path order is **not** normalized: `--hosts a,c,b` and `--hosts b,c,a`
  hash differently, but the second is rejected as a conflict (see
  *Conflicts and preconditions*) rather than coexisting — the same two
  hosts can't hold two ingress binds on the same port.
- `--dest` is deliberately excluded from the id: the same route + port +
  protocol with a different `--dest` is the same plumbing claim on the same
  ingress bind, so it's a conflict, not a sibling.
- Tunnel ids live in a disjoint id-space from declared `lab.json` links,
  which get a readable `name` or `<a-host>--<b-host>` handle and never wear
  the `tun-<hex>` form — a live tunnel and the declared route it happens to
  realize never collide.

(docker-container-endpoints)=

## Docker container endpoints

A Docker container host may be a tunnel **endpoint** — the first or last
`--hosts` entry — but never an intermediate relay hop, and its chain
neighbor must be **its own parent host** (the docker-capable host that runs
it). `add` rejects any other placement or neighbor at add time, naming the
parent it expected.

A container entry never takes `@iface` — containers have no modeled
`interfaces` — its data-plane IP is instead resolved through its parent via
`docker inspect` at add time. The container's two tagged `socat` processes
launch through the **container's own** command execution (a `docker exec`
by way of the parent), and because containers have no systemd user
manager, the launch always falls back to the `setsid`-detached path (see
*Old-OS portability* below) rather than `systemd-run --user`.

```bash
otto --lab veggies tunnel add --hosts sprout,carrot_seed,carrot_seed.compose.web --port 8080
```

Here `carrot_seed.compose.web` is a container whose parent is `carrot_seed`
— a valid chain because the container neighbors its own parent.

## Host requirements

A host can only carry a tunnel process — appear in `--hosts`, or be scanned
by discovery/removal — if it has a working `bash` (for the `exec -a`
argv-tagging trick tunnel processes use to stay discoverable) and `socat`
on its `PATH`. Missing either fails `add` loudly, naming the host; there is
no auto-install. This applies to every hop in the chain, not just the
endpoints.

Whether a host qualifies is the
[`has_bash`](lab-config.md#common-optional) capability, not a check against
a specific host class: it defaults to `true` for Unix hosts (including the
built-in `local` host and Docker containers) and `false` for embedded
targets, and can be overridden per host in `lab.json` for a host that
defies the norm. `add` live-checks both `bash` and `socat` (`command -v`)
on every chain host regardless; `has_bash` is the declared capability that
separately gates which hosts discovery (`list`, `remove`) bothers to scan
at all.

## Host-down behavior

`otto tunnel` is best-effort and transparent about failure, never silently
wrong:

- **`list` / discovery** shows tunnels found on every host it could reach,
  marks a tunnel's status uncertain (a trailing `?`) when a chain host
  couldn't be scanned, and names each unreachable host. It never silently
  drops a host from the picture.
- **`remove`** kills tunnels on every host it could reach, names the hosts
  it couldn't, reports any process still alive after the kill as a
  survivor, and **exits non-zero** whenever any of that happened — so a
  script checking the exit code learns the reap was incomplete instead of
  being told it succeeded while a stray `socat` may still be running.

## Old-OS portability

Tunnel processes launch detached and owner-agnostic so they outlive the
`otto tunnel add` invocation and the SSH session that ran it:
`systemd-run --user --collect` on hosts with a user systemd manager,
falling back to a plain `setsid`-detached background process where they
don't (older distros, and always inside Docker containers — see above).
The `socat` address forms, the `exec -a` argv-tagging trick, and the
discovery `ps` command all stay within an old-stable portability floor
(pre-`etimes`, procps/socat compatible back to Linux 2.6.32-era
userland). The docker-endpoint e2e suite exercises this floor against a
`centos:7` (arm64) container — no systemd, so the `setsid` launch path,
old-procps `etime` parsing, and old-bash `exec -a` are what actually run
there. True CentOS-6/2.6.32 validation remains a documented manual check,
gated on an x86_64 host joining the bed.

## Live discovery

Tunnel discovery is built as a `(command, pure parser)` pair — one portable
`ps` command run on every `has_bash` host plus a pure function turning its
output into observations — the same shape the monitor's
{class}`~otto.monitor.parsers.MetricParser` contract expects (command,
parse, interval). This phase keeps `otto.tunnel` monitor-free; wiring
tunnel discovery into `otto monitor` as a first-class parser (with
topology/edge views) is a later phase. See
[Custom parsers](monitor.md#custom-parsers) in {doc}`monitor` for the
parser contract this is designed to plug into.

Every tagged process's `argv[0]` carries an `otto-tunnel:v1:` sentinel that
self-describes that one process's role, direction, and the tunnel's full
path — any single surviving process (visible in `ps`) reconstructs the
whole intended tunnel, so discovery survives every other chain host being
down.

## Shell completion

`--hosts` tab-completes lab host ids; once a host and a comma are typed,
the remaining candidates narrow to hosts on the same `/24` as the
last-entered one (a simple heuristic — true per-interface subnet
awareness, and container parent-adjacency awareness, come later).

`remove <id>` tab-completes from a short-lived cache (2-minute TTL) of
tunnel ids. `otto tunnel list` populates it with a fresh live scan on its
way out; `otto tunnel add` does not touch it. `otto tunnel remove` empties
it rather than repopulating it, so a `list` right after a `remove` is what
re-warms completion for what's left. A cold or emptied cache simply offers
no suggestions until the next `list`.

## Library API

Every command above has a callable counterpart in `otto.tunnel` — see the
{doc}`API reference <../api/tunnel>` for full signatures:

```python
from otto.cli.run import instruction
from otto.configmodule import get_lab
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel

@instruction()
async def add_multi_hop_tunnel():
    lab = get_lab()
    added = await add_tunnel(
        lab, [("carrot_seed", None), ("compost", None), ("tomato_seed", None)], port=6001
    )
    live = await discover_tunnels(lab)          # every tunnel found right now
    report = await remove_tunnel(lab, added.tunnel.id)
```
