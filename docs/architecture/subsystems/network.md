# Links & tunnels

otto models a lab's connectivity as two layers that share the same lab data
but answer different questions. The user-facing workflows live in
{doc}`otto link <../../guide/network/link>` and
{doc}`otto tunnel <../../guide/network/tunnel>`. **Static links** are the topology's *underlay*
— the `(host, interface)` edges that already exist, and the place where
traffic impairment (`tc`) actually attaches. **Tunnels** are a *dynamic
overlay* — end-to-end forwarding paths that `otto tunnel add` stands up over
that connectivity as host-resident processes. The two are deliberately
separate subsystems: a tunnel is never impaired directly, but impairing a link
a tunnel happens to ride degrades that tunnel's traffic realistically, for
free, because the degradation lives on the link the tunnel rides, not in otto.

Neither layer keeps a private ledger of live state. A link's impairment *is*
its kernel qdisc; a tunnel *is* its running, tagged processes. Everything
`otto link list` / `otto tunnel list` report is reconstructed by reading the
hosts live — which is why both are honest about a host they could not reach
rather than silently dropping it.

## Static links

**One edge type, two origins.** A `Link` is a single model regardless of where
the edge came from: an explicit route declared in `lab.json`'s `links`, or one
*derived* at lab-load time from a host's management `hop` chain
({mod}`otto.link.derive`) — the SSH/telnet path otto already uses to reach a
host is itself a topology edge. Each end of a link is a `(host, interface)`
pair; that interface is the netdev where a netem qdisc attaches. Declared links
carry a readable identity — an explicit `name`, or an `<a-host>--<b-host>`
handle otto derives — in an id-space disjoint from tunnels, so a live tunnel
and the declared route it realizes never collide.

**State lives in the kernel.** There is no impairment ledger. The `tc qdisc`
configuration on a placement's netdev *is* the impairment state; `otto link
list` reconstructs every link's condition from live `tc qdisc show` (plus `tc
filter show` for scoped trees). This is also why otto is conservative about
what it touches: it only ever mutates qdisc trees whose shape it recognizes as
its own, and reports a root qdisc it did not generate as *foreign* rather than
clobbering it.

**Placements: two directions, resolved independently.** A link's two
directions are separate *placements*, each with its own netem qdisc, so they
can be impaired asymmetrically. Where a direction's placement lands is resolved
at impair time ({mod}`otto.link.placement`). In the default **endpoint mode**
each direction lands on its own physical endpoint interface. A link may instead
name an in-path middlebox in its `impair` field, in which case both directions
place on that host — and the *facing* interface is never declared: otto
resolves it by matching each endpoint's IP against the middlebox's live
interface subnets (`ip -o addr show`), so a middlebox with no interface on an
endpoint's subnet fails loud, and that failure doubles as the "is this host
actually in the path" check. The link-level `impair` field names only *where*
the impairment is serviced; a host's separate `impairer` pin names *which*
implementation services it — one field per concern, so adding an impairer never
has to touch a link's placement.

**Whole-interface versus port-scoped.** A placement is impaired one of two
ways, and never both at once. The default degrades the entire netdev — one root
netem qdisc, every packet treated alike. Port scoping instead builds a classful
`prio` tree: the kernel-default three bands carry unmatched traffic exactly as
an unshaped interface would, and each port selector adds one more band with its
own netem leaf and a pair of `u32` filters (source and destination port)
steering that service's traffic into it. The two shapes are mutually exclusive
on one placement because they are different qdisc roots; here too the only state
is the kernel's, and `list` rebuilds the whole scoped tree from `tc qdisc show`
+ `tc filter show`.

**Never sever otto's own path.** A link's netdev may be the very interface otto
reaches a host *through*, so every resolved placement is checked against two
refusals before any host is mutated: the management interface a host is reached
on, and any link touching the local host, are never impairable — including
transitively, when a placement's netdev carries the management path of another
host that reaches otto only by hopping through it. The invariant is simple:
impairing the lab must never lock otto out of the lab.

**Mutations are verified, never half-applied.** Applying an impairment is
merge-read-modify-verify ({mod}`otto.link.manage`): otto reads a placement's
current netem, overlays only the parameters this call changed, writes the
result, then re-reads to confirm it matches. If any placement in a
multi-placement call fails, every placement already touched is rolled back to
its prior state before the error surfaces, so a partly-applied impairment is
never left behind.

## Tunnels

**A tunnel is a chain of tagged processes.** `otto tunnel add` builds exactly
one tunnel: an ordered path of hosts realized as processes spawned directly on
each host in the chain. otto builds only the path named — it never auto-routes
from the lab topology. Each host in the chain carries **two** tagged processes,
one per direction; an endpoint's pair terminates the tunneled protocol
(ingress and egress), an intermediate hop's pair only relays the carrier stream
onward. What actually runs at each role is a pluggable `TunnelCarrier` — socat
by default — applied chain-wide.

**Bidirectional by construction.** Every tunnel is two mirrored chains. A
single chain is already two-way for an in-flight exchange — socat relays
replies back over the same connection that carried the request — so the
mirrored second chain exists to let a *new* flow originate at the far endpoint,
not just the first-listed one. That symmetry is why delivery defaults to
loopback rather than an endpoint's own data-plane IP: that IP is already bound
by the reverse chain's ingress listener, and delivering there would loop a
datagram straight back into the tunnel. `--dest` overrides delivery on the far
endpoint only, relaying onward so the packet arrives sourced from that
endpoint's own interface.

**Identity is the ordered path.** A tunnel's id is `tun-<hex>-<port>` — twelve
hex characters hashed from the ordered chain (every hop, in order, plus
protocol) with the port appended readably. The path is deliberately *not*
normalized: `a,c,b` and `b,c,a` hash differently, and the reverse is rejected
as a bind conflict rather than treated as a new tunnel, because the same two
hosts cannot hold two ingress binds on the same port. `--dest` is excluded from
the id on purpose — the same route, port, and protocol with a different
destination is the same ingress claim, so it is a conflict, not a sibling.
These ids share no space with declared links.

**Building atomically.** `add` resolves every hop, computes the id, and checks
it against the live tunnels discovered right now — declared links make no port
claims, so they never enter this check. It then spawns the processes
downstream-first per direction, so a listener always exists before anything
upstream connects to it, and **verifies** every expected process actually came
up before reporting success. If any is missing, `add` tears down everything it
already started and raises — no half-built tunnel survives a failed `add`.

**Discovery reconstructs from a single survivor.** Tunnels keep no ledger
either: discovery ({mod}`otto.tunnel.discovery`) is a `(command, pure parser)`
pair — one portable `ps` run on every eligible host, plus a pure function
turning its output into observations, the same shape the monitor's parser
contract expects — a resemblance that is now WIRED, not just structural (see
"Tunnels in the monitor" below). Every tagged process carries an
`otto-tunnel:v1:` sentinel in
its `argv[0]` that self-describes that process's role, direction, and the
tunnel's full path, so *any one* surviving process is enough to reconstruct the
whole intended tunnel. Discovery therefore survives every other chain host
being down, and reports a tunnel as `degraded` or uncertain (`?`) rather than
silently dropping a host it could not scan.

**Processes outlive the command that made them.** So a tunnel persists past the
`otto tunnel add` invocation and the SSH session that launched it, each process
is started detached and owner-agnostic — `systemd-run --user --collect` where a
user systemd manager exists, falling back to a plain `setsid`-detached process
where it does not (older distros, and inside Docker containers). The socat
address forms, the `exec -a` argv-tagging trick, and the discovery `ps` command
all stay within an old-stable portability floor, so the same mechanism works on
long-lived lab hardware as on a current distro.

**Tunnels in the monitor.** The live tunnel set rides the monitor's own
session wire as `TunnelRecord` rows ({mod}`otto.models.monitor`) — id,
protocol, service port, ordered hop path, `ok`/`degraded`/`uncertain`
status, carrier counts, and age. `SessionRecord.tunnels` carries the current
set; on the live stream, `MonitorSessionFragment.tunnels` is a
REPLACE-semantics field — `None` means no tunnel update in that fragment, a
list (including `[]`) replaces the session's set wholesale, the same
last-known-state contract the `meta` field already follows. A collector-side
loop drives this: `MetricCollector._tunnel_loop`, a sibling of the metric
bucket loops, runs on the collector's own collection interval and is fed an
injected `discover_tunnel_records` callable rather than importing
`otto.tunnel` directly — the monitor package stays tunnel-blind, and the
adapter from `DiscoveredTunnel` to `TunnelRecord` lives tunnel-side, in
{mod}`otto.tunnel.discovery`'s sibling module `otto/tunnel/records.py`;
`otto.cli.monitor` composes the callable over the *whole lab*, not the
monitored host subset, since a tunnel can traverse hosts otto isn't
otherwise polling. On the monitor side, persistence is last-known-state
only, not a timeline: the `sessions` table's `tunnels_json` column (added to
the v2 schema in place, the `chart_map_json` precedent — no migration) holds
the current set as JSON, overwritten on change, with no per-tick history. A
scan that reaches none of the lab's scannable hosts is a *failed* scan, not
an empty lab:
`discover_tunnel_records` raises rather than returning `[]`, so the
collector's tunnel loop keeps the last known set — never blanks it — and
logs a warning, the same "guard what you emit" rule the metric-collection
paths already follow. The monitor's topology view renders this set as an
overlay along the links each tunnel's hop path traverses; see
{doc}`../../guide/monitor`'s Topology view section for what that looks like.

## Where the code lives

Static links — `otto.link`:

- {mod}`otto.link.model` — the `Link` edge type, shared by declared and derived
  routes
- {mod}`otto.link.derive` — resolves hop-derived edges at lab-load time
- {mod}`otto.link.placement` — endpoint-versus-middlebox placement resolution
  and the facing-interface match
- {mod}`otto.link.params` — `ImpairmentParams`, the unit and merge rules
- {mod}`otto.link.netem` — the `tc`/netem impairer: whole-interface and the
  scoped `prio` tree
- {mod}`otto.link.manage` — the merge-read-modify-verify orchestration behind
  `impair`/`repair`/`list`
- {mod}`otto.link.sentinel` — tags the detached `--expire` timer processes

Tunnels — `otto.tunnel`:

- {mod}`otto.tunnel.model` — `Tunnel`/`TunnelHop` and the ordered-chain id
- {mod}`otto.tunnel.socat` — the socat carrier and the pure command-builder
  layer it wraps
- {mod}`otto.tunnel.manage` — `add`/`remove` orchestration, the post-launch
  verify and teardown
- {mod}`otto.tunnel.discovery` — the `(command, pure parser)` live scan
- `otto/tunnel/records.py` — the discovery-to-`TunnelRecord` adapter that
  feeds the collector's tunnel loop, keeping `otto.monitor` free of any
  import on `otto.tunnel`
- {mod}`otto.tunnel.sentinel` — the `argv[0]` sentinel codec that makes every
  process self-describing
