# otto tunnel

`otto tunnel` creates, lists, and removes **host-resident bidirectional
tunnels** — an ordered chain of `socat` processes, tagged and spawned
directly on lab hosts, that carries a service's traffic end-to-end across
one or more hops. A tunnel rides one or more **links**: the topology edges
declared in `lab.json`, or derived from each host's management `hop` (see
{ref}`lab-links` in {doc}`../setup/lab-config`). Links document routes that exist;
`otto tunnel add` is what actually stands traffic up over them — one `add`
builds exactly one tunnel, so a second `add` on the same route with a
different port is a second, coexisting tunnel. Links are the static
underlay, tunnels the dynamic overlay riding it (see
{doc}`../../architecture/subsystems/network` for the model) — for impairing a
link's traffic (delay, loss, rate, ...) rather than tunneling over it, see
{doc}`link`.

Every capability is a plain callable first — `otto tunnel` is a thin CLI
wrapper over `otto.tunnel.add_tunnel` / `remove_tunnel` /
`remove_all_tunnels` / `discover_tunnels`. See the
{doc}`API reference <../../api/tunnel>` to call them directly from an
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
| `--protocol` | no (default `tcp`) | The service protocol the endpoints speak, validated against the selected carrier's supported protocols. The default `socat` carrier supports `tcp` and `udp` and always relays between hops over a plain-TCP carrier stream. |
| `--dest` | no (default: loopback on the far endpoint) | Deliver the far endpoint's traffic on to a **third** host instead of terminating on that host's loopback — see *Relaying with `--dest`*, next. |
| `--carrier` | no (default `socat`) | Tunnel transport — a registered `TunnelCarrier` name, applied chain-wide. See [Custom carriers](#custom-carriers) below. |

### `@iface` interface pinning

Each `--hosts` entry may pin a specific interface with `host@iface`, where
`iface` is a key in that host's `interfaces` map in `lab.json` (see
[Network interfaces](../setup/lab-config.md#network-interfaces)). The pin is only
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

Each `add` places tagged processes on every host in the chain; how they are
laid out — two per host, mirrored so a brand-new flow can start at either
endpoint — is covered in {doc}`../../architecture/subsystems/network`. A chain
host may not appear twice in `--hosts`, and the reverse of an existing tunnel's
path (`b,c,a` after `a,c,b`) is rejected as a conflict (see *Conflicts and
preconditions* below) rather than treated as a new tunnel.

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
**Why loopback, not the endpoint's own IP:** that IP is already bound by the
reverse chain's ingress listener, so delivering there by default would loop a
datagram straight back into the tunnel (see
{doc}`../../architecture/subsystems/network`). A service that insists on binding
a wildcard address without `SO_REUSEADDR` can still collide with the ingress
bind; that failure is loud, surfaced by `add`'s post-launch verify.
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

`add` then spawns the tagged processes and **verifies** every one of them
actually came up before reporting success. If any is missing (a bind collision,
a port race, a host that turned out not to have `socat`), `add` tears down
everything it already started and raises, naming exactly what failed — no
half-built tunnel survives a failed `add`. How the spawn is ordered to make that
safe is covered in {doc}`../../architecture/subsystems/network`.

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

## Previewing: `--dry-run`

`--dry-run` (`-n`) is a **global** option — `otto -n --lab veggies tunnel add
…`. **A dry run contacts no device at all**, not even for the read-only probes
and not even for docker's container-liveness check. So every answer it gives
comes from `lab.json` and the options you typed, and it says plainly what it
could not check.

`otto tunnel` is one of the commands that opts into a deeper preview instead of
stopping at the CLI seam — see {doc}`../dry-run` for the contract every command
follows, what the opt-in buys, and `--probe`.

```console
$ otto -n --lab veggies tunnel add --hosts carrot_seed@eth2,pepper_seed@eth2,tomato_seed@eth2 --port 8080
dry run carrot_seed <-> tomato_seed: no device was contacted — nothing was read and nothing was changed
  would: build tun-80b8500dedcf-8080: carrot_seed@eth2 -> pepper_seed@eth2 -> tomato_seed@eth2,
    tcp:8080, delivering to 127.0.0.1 on tomato_seed
  would: carry fwd traffic on port 49152 and rev on 49153 — PROVISIONAL, see the first
    `not checked` line
  would: start each process below detached, with its argv[0] replaced by an `otto-tunnel:v1`
    sentinel …
  would: tomato_seed fwd/egress: socat TCP4-LISTEN:49152,fork,reuseaddr TCP4:127.0.0.1:8080
  would: pepper_seed fwd/relay: socat TCP4-LISTEN:49152,fork,reuseaddr TCP4:192.168.1.12:49152
  would: carrot_seed fwd/ingress: socat TCP4-LISTEN:8080,bind=192.168.1.11,fork,reuseaddr
    TCP4:192.168.1.13:49152
  … (2n lines: one per process, fwd then rev)
  not checked: which ports are already bound anywhere on the chain …
  not checked: whether this tunnel already exists …
  not checked: whether the chain hosts actually have socat and/or bash …
  not checked: the 6 launches themselves and the post-add verify …
```

Read both halves. The `would:` lines are the exact argv, but:

:::{warning}
**The two carrier ports are provisional, and every argv above names them.** A
real `add` first probes every hop with `ss -Htln` / `netstat -tln` and skips
what is already listening; a dry run has only your `--port` to go on, so it
picks 49152/49153 on every lab. If a real run finds either taken, all 2n
command lines change. `AddedTunnel.carrier_fwd` / `carrier_rev` are `None`
under a dry run for exactly this reason — the provisional pair is in the plan,
labelled, and nowhere else.
:::

What each command shows:

- **`add`** — the resolved chain with its addresses, the tunnel id (which
  hashes path + protocol + port, none of it read off a device), the 2n argv,
  and the provisional carrier pair. Refusals that need no device are still
  made: an unknown host, an ambiguous or unknown `@iface`, a chain shorter
  than two hops, a repeated host, a container in an illegal position, a
  `--dest` inside the path, an unsupported protocol, and the
  [`has_bash=False` refusal](#host-requirements).
- **`remove`** — the *scope* of the reap: which `has_bash` hosts would be
  scanned, and what the kill would match. It never prints
  `removed (none found)`; that line is a claim about live processes.
- **`list`** — one line saying no host was scanned. Every row of that table is
  an observed process, so there is nothing left to show; a dry run also leaves
  the `remove <TAB>` completion cache alone rather than emptying it from a scan
  that never ran. The exception is a lab that declares no `has_bash` host:
  there is nothing to scan, so `list -n` gives the same complete, empty answer
  a real run gives — banner and all — including the cache write.

**A container endpoint previews much less.** A container's tunnel address is
its docker bridge ip, read with `docker inspect` on the parent, and the hops
either side of it connect *to* that address — so a dry run has no argv to show
for any hop, not just for the container. It says so rather than guessing:

```console
$ otto -n --lab veggies tunnel add --hosts carrot_seed.repo1.api,carrot_seed@eth2,tomato_seed@eth2 --port 8080
dry run carrot_seed.repo1.api <-> tomato_seed: no device was contacted — nothing was read and nothing was changed
  would: build tun-91e5f6330d1d-8080: carrot_seed.repo1.api -> carrot_seed@eth2 -> tomato_seed@eth2,
    tcp:8080, delivering to 127.0.0.1 on tomato_seed
  would: carry fwd traffic on port 49152 and rev on 49153 — PROVISIONAL …
  not checked: every process argv, because 'carrot_seed.repo1.api' is a container endpoint …
```

Programmatically, a dry run is visible on the return value:
`AddedTunnel.plan` / `RemovedReport.plan` is a `DryRunPlan` (with `.would` and
`.unchecked`), and `TunnelDiscovery.not_measured` is `True` whenever a scan was
declined — a third state, distinct from "reachable and empty" and from
`unreachable`, because nothing was asked. `discover_tunnel_records` (the monitor's source) raises
`TunnelNotMeasuredError` rather than returning `[]`, so the dashboard keeps its
last known tunnel set instead of blanking the overlay.

## Tunnel identity

Every tunnel gets an id of the form `tun-<hex>-<port>`, e.g.
`tun-0a17f76fb561-6001` — the port stays visible in `list`, in `remove <id>`,
and in every tagged process's `argv[0]`, so two tunnels on the same route with
different ports are visibly distinct. Ownership is **not** encoded in the id:
`remove --all` reaps every otto tunnel it finds, whoever created it.

For how the id is derived (a hash of the ordered chain), why the path is
deliberately not normalized, why `--dest` is excluded, and why tunnel ids never
collide with declared `lab.json` link handles, see
{doc}`../../architecture/subsystems/network`.

(docker-container-endpoints)=

## Docker container endpoints

A Docker container host may be a tunnel **endpoint** — the first or last
`--hosts` entry — but never an intermediate relay hop, and its chain
neighbor must be **its own parent host** (the docker-capable host that runs
it). `add` rejects any other placement or neighbor at add time, naming the
parent it expected.

Docker is a **testing aid, never a requirement**: no tunnel command ever
starts a container. `add` requires a container endpoint to already be
running (start it with `otto docker up` first) and fails loudly when it
isn't; `list` and `remove` probe a declared-but-down container read-only
and treat it as carrying no tunnel processes — scanning a lab never
composes a docker stack as a side effect.

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
[`has_bash`](../setup/lab-config.md#common-optional) capability, not a check against
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
`otto tunnel add` invocation and the SSH session that ran it — see
{doc}`../../architecture/subsystems/network` for the launch mechanism
(`systemd-run --user` versus a `setsid` fallback on hosts without a user systemd
manager, including inside Docker containers). The `socat` address forms, the
`exec -a` argv-tagging trick, and the
discovery `ps` command all stay within an old-stable portability floor
(pre-`etimes`, procps/socat compatible back to Linux 2.6.32-era
userland). The docker-endpoint e2e suite exercises this floor against a
`centos:7` (arm64) container — no systemd, so the `setsid` launch path,
old-procps `etime` parsing, and old-bash `exec -a` are what actually run
there. True CentOS-6/2.6.32 validation remains a documented manual check.

## Live discovery

`otto tunnel list` finds tunnels by scanning live processes: a portable `ps` on
every `has_bash` host plus a pure parser, with each tagged process's `argv[0]`
self-describing the whole tunnel so any single survivor reconstructs it — which
is why discovery survives every other chain host being down. The design — and
how it reuses the monitor's `(command, parser)` parser shape — is covered in
{doc}`../../architecture/subsystems/network`; see also
[Custom parsers](../monitor.md#custom-parsers) in {doc}`../monitor` for the
parser contract it is shaped to plug into. Tunnels appear live in the
monitor's topology view, riding the links their path traverses — see
[Topology view](../monitor.md#topology-view) in {doc}`../monitor`.

## Shell completion

`--hosts` tab-completes lab host ids; once a host and a comma are typed,
the remaining candidates narrow to hosts on the same `/24` as the
last-entered one — a simple heuristic with no per-interface subnet or
container parent-adjacency awareness.

`remove <id>` tab-completes from a short-lived cache (2-minute TTL) of
tunnel ids. `otto tunnel list` populates it with a fresh live scan on its
way out; `otto tunnel add` does not touch it. `otto tunnel remove` empties
it rather than repopulating it, so a `list` right after a `remove` is what
re-warms completion for what's left. A cold or emptied cache simply offers
no suggestions until the next `list`.

## Custom carriers

Tunnel transport is pluggable the same way link impairment is — see
{doc}`../hosts/extending-backends` for the shared registration philosophy. A
`TunnelCarrier` builds the argv for one tagged process's role:

```python
class TunnelCarrier:
    supported_protocols: ClassVar[frozenset[str]] = frozenset()
    requirements_command: ClassVar[str] = ""
    tools_description: ClassVar[str] = ""

    def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port) -> list[str]: ...
    def relay_args(self, carrier_port, next_ip) -> list[str]: ...
    def egress_args(self, protocol, service_port, deliver_ip, carrier_port) -> list[str]: ...
```

`otto.tunnel.socat.SocatCarrier` (`supported_protocols = {"tcp", "udp"}`) is
the only first-party registrant, built on `socat`. A custom carrier
registers from an `init` module, before any lab data loads:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from typing import ClassVar

from otto.tunnel import TunnelCarrier, register_carrier


class MyCarrier(TunnelCarrier):
    supported_protocols: ClassVar[frozenset[str]] = frozenset({"tcp"})
    requirements_command: ClassVar[str] = "command -v my-tool >/dev/null 2>&1 && echo ok || echo no"
    tools_description: ClassVar[str] = "my-tool"

    def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port):
        return ["my-tool", "listen", f"{bind_ip}:{service_port}", f"{next_ip}:{carrier_port}"]

    def relay_args(self, carrier_port, next_ip):
        return ["my-tool", "relay", str(carrier_port), next_ip]

    def egress_args(self, protocol, service_port, deliver_ip, carrier_port):
        return ["my-tool", "deliver", str(carrier_port), f"{deliver_ip}:{service_port}"]


register_carrier("my_carrier", MyCarrier)
```

`supported_protocols` is mandatory and non-empty — a carrier that could
never validate any tunnel is rejected at registration time. Select it per
tunnel with `otto tunnel add --carrier my_carrier`; the carrier applies
chain-wide (every hop in that one tunnel), and the choice isn't part of a
tunnel's identity or wire format — `remove` reaps a tunnel's processes by
pid regardless of which carrier built them.

## Library API

Every command above has a callable counterpart in `otto.tunnel` — see the
{doc}`API reference <../../api/tunnel>` for full signatures:

```python
from otto.cli.run import instruction
from otto.config import get_lab
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel


@instruction()
async def add_multi_hop_tunnel():
    lab = get_lab()
    added = await add_tunnel(
        lab, [("carrot_seed", None), ("compost", None), ("tomato_seed", None)], port=6001
    )
    live = await discover_tunnels(lab)  # every tunnel found right now
    report = await remove_tunnel(lab, added.tunnel.id)
```
