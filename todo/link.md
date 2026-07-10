
# `Link` specification

> **Working design notes** for the `Link` object and `otto link` CLI. These
> notes back the GUI overhaul (move to Untitled UI) and expose new CLI ways to
> manage links in a lab. Changes that ripple into the monitor overhaul are
> tracked against `docs/superpowers/specs/2026-07-05-monitor-untitled-ui-redesign-design.md`.
>
> Status: **foundation specced** (2026-07-06). Decisions marked _(decided)_ are
> settled; _(open)_ items belong to later sub-projects. The foundation (#1) is
> distilled into the formal spec:
> `docs/superpowers/specs/2026-07-06-link-foundation-design.md`. This file remains
> the living scratchpad for the whole `link.md` stack.

## What is a `Link`?

`Link`s are any connection between 2 hosts or elements — a first-class **edge**
object, which otto has never had (connectivity is only *implied* today by the
`hop` field, the near-vestigial `interfaces` map, and shared
`element`/`element_id`). Links come from three **provenances**:

1. **Implicit** — derived from `hop` chains (host A reaches host B through host
   C ⇒ edges A–C, C–B). This is the SSH/telnet *management* path. No new data;
   just a derived view (the topology derivation already sketched in
   `topology_plan.md`).
2. **Declared** — defined in the lab file as additional routes that are *not*
   the management path. These carry other traffic: UDP, HTTP, RTP, etc.
3. **Dynamic** — created at runtime via `otto link add` (tunnels), tracked in a
   shared registry, torn down via `otto link remove`.

The three-way split is what `otto link add`'s conflict rule needs: a new link
can't collide with an implicit, declared, or already-added dynamic link.

**Endpoints reference `(host, interface)`, not just a host.** Impairment
(`tc qdisc … dev <iface>`) and capture (`tcpdump -i <iface>`) both act on a
specific interface, so the link model is anchored on interfaces (see below).

## Design staging (decided 2026-07-06)

`link.md` is really ~6 sub-projects stacked on one foundation. Each gets its own
spec → plan → implement cycle; **the foundation is designed and built first**
because every CLI verb *and* the GUI consume it.

| # | Sub-project | Depends on |
|---|---|---|
| **1** | **Foundation: `Link` model + `lab.json`** — rename `hosts.json`→`lab.json`, add a `links` schema section, evolve `interfaces` into per-interface objects, a unified implicit/declared/dynamic `Link` type + topology derivation, and the dynamic-link registry contract | — |
| 2 | `otto link` CLI + **add/remove/list** (UDP tunnels via socat over the existing SSH TCP forward) | 1 |
| 3 | **Impairment**: `LinkImpairer` protocol + NetEm + `otto link impair` — ★ SHIPPED 2026-07-10 (worktree-link-foundation) | 1 |
| 4 | **Capture**: `otto link capture` / `otto host capture` (tcpdump + pcap filters) | 1 |
| 5 | **Management hosts**: monitor source-attribution to *other* elements | 1 |
| 6 | **GUI Phase 2 topology + link inspector** (consumes 1; reserves slots for 3/5) | 1 |

Reusable plumbing that already exists: asyncssh TCP/SOCKS local/remote
forwarding + multi-hop tunnel teardown (`host/transport.py`
`HopTransport.forward_port()`), `host.run()`/`host.oneshot()` as the shell-out
primitive, and the generic `Registry` + `Protocol` + `host_families` mold for
pluggable backends. Genuinely greenfield: NetEm/tc, tcpdump, socat, and
management-host source attribution.

## Lab configuration changes

The `hosts.json` paradigm needs to change. The file must capture more than
hosts — it must also capture the **declared links** between hosts (routes not
traditionally used for ssh/telnet access, carrying UDP/HTTP/RTP/etc.). It's also
best to rename `hosts.json` → **`lab.json`** (affects `otto init`:
`HOSTS_JSON_ENTRY`, `_scaffold_lab`/`_detect_lab`/`_validate_lab`, the glob
patterns, and the `labs = [...]` path comment in the settings template).

**Top-level shape _(decided 2026-07-06)_:** `lab.json` becomes an **object with
two sections** — `{"hosts": [...], "links": [...]}`. The `hosts` value is the
**existing array of host objects, schema untouched** (ids stay derived from
`element`/`board`/`slot`); `links` is a new array of declared-link objects. The
loader changes from array-concat to per-section merge across lab dirs (union
`hosts`, union `links`); `_load_json_hosts`'s `isinstance(data, list)` guard
becomes an object-with-sections guard.

Whether the tunneled *protocol* must be declared on a link is still being
weighed — at minimum TCP-vs-UDP matters for the tunnel mechanism (§ UDP
Tunnels).

### Interface specification _(decided 2026-07-06)_

Evolve `interfaces` from `dict[name → ip-string]` to
`dict[netdev-name → InterfaceSpec]`:

- **Key = the network-device name** (`eth0`, `eth1`, …), so impairment/capture
  read the device straight off the key.
- **Value = an `InterfaceSpec` object** (`{ip: "…", …}`) — extensible, so MAC,
  CIDR, role/plane, speed, up/down state, etc. can be added later without
  reworking the shape.
- **String shorthand (ergonomic):** a bare `"eth0": "192.168.1.5"` coerces to
  `{"eth0": {"ip": "192.168.1.5"}}` via a pydantic before-validator, so simple
  labs stay terse. Not a migration bridge — we **hard-cut over** to `lab.json`
  (no dual-format loader, no back-compat array reader; existing-user migration is
  explicitly out of scope, decided 2026-07-06).
- **Touch-points:** `RemoteHost.address_for()` and `SnmpOptions.address` resolve
  by key today and keep working — they just read `.ip` off the object now.

### Declared link entry _(decided 2026-07-06)_

A `links` entry describes a data-plane route:

- **`endpoints`** — exactly 2, each `{ "host": <id>, "interface": <netdev-key> }`.
  Interface is **recommended but required only when the host has multiple
  interfaces defined**; with a single interface (or none) otto assumes it and its
  IP. Impair/capture need a resolved interface, so an endpoint that resolves
  ambiguously isn't impairable/capturable until named.
- **`protocol`** — optional in JSON, **defaults to `"tcp"`** (no `None`);
  **informational for declared links** (documents what the route carries:
  udp/http/rtp/…). It becomes *functional* for dynamic links (`otto link add`),
  where it drives socat setup (UDP vs TCP).
- **`name`** — optional friendly handle; the id is otherwise derived from the
  endpoints.
- **`impair` / `management`** — optional, reserved (sub-projects #3/#5): an
  in-path impairment host + its selected impairer.
- **No `labs` field** — membership is **derived** (below).

**Lab membership (derived, may span labs):** a link belongs to **every lab that
either endpoint belongs to** (union of the endpoints' `labs`). A link can
legitimately **span labs**, so it is never forced into a single one. Loading lab
L surfaces every link with ≥1 endpoint in L; a cross-lab link appears in both,
with the out-of-lab endpoint rendered as a dangling/stub node in that lab's
topology. Endpoint host-ids are validated as known references.

## Runtime `Link` type _(decided 2026-07-06)_

One `Link` object regardless of provenance, so the CLI, topology derivation, and
GUI all speak the same type:

```python
@dataclass
class Link:
    a: LinkEndpoint        # resolved host id + interface (netdev) + ip
    b: LinkEndpoint
    protocol: str = "tcp"  # "udp"/... otherwise; defaults to tcp (no None)
    provenance: Provenance # IMPLICIT | DECLARED | DYNAMIC
    id: str                # deterministic (endpoints+ports+proto)
    name: str | None
    # impair / management reserved for #3 / #5; no owner field (see § Dynamic Link state)
```

The three provenances differ in **cost**, so the accessor splits by cost rather
than pretending they're uniform:

- **`lab.static_links()` (sync)** — implicit (derived from each host's `hop`) ∪
  declared (from `lab.json`). Free, straight off the loaded lab. Powers the GUI
  base topology and the implicit/declared rows of `otto link list --all`.
- **`discover_dynamic_links(lab)` (async)** — the live-discovery layer
  (`asyncio.gather`). Powers `otto link list` (default) and the GUI's TTL-cached
  dynamic overlay.
- **`all_links(lab)` (async)** — the union, for `otto link list --all` and the
  `add` conflict check.

Reconciliation is by `id`: a dynamic link coinciding with a declared/implicit one
shares the id, so it merges rather than double-counts; `add`'s conflict check is
"does this id already exist in any layer."

## Features

### Traffic Impairment

NetEm is the first-party implementation of the general feature to impair traffic
(drop packets, jitter, corruption, rate, etc.). It conforms to a pluggable
`LinkImpairer` Protocol (new `Registry` + `register_impairer`/`build_impairer` +
`host_families` selector, mirroring the transfer/term/reservation backends). A
link/host's optional `impair` field selects a registered impairer by name,
validated against the registry the way `term`/`transfer` are today.

- **Expiry is optional** _(decided 2026-07-06)_ — `otto link impair` takes an
  opt-in `--expire <seconds>` self-healing watchdog, but the **default is
  indefinite** because testers routinely run long tests that need impairment to
  persist. Expiry is a convenience, never forced.
- **Mgmt-interface refusal is mandatory** — impairing the device otto reaches the
  host through is an instant self-lockout, so `impair` refuses the management
  `ip`'s interface regardless of expiry.

### UDP Tunnels

Very few links are natively UDP, but UDP (or other protocols) sometimes need to
be tunneled from one host to another. A series of SSH tunnels + socat bridges on
each node is the leading mechanism (see `udp_hop_forwarding.md`: UDP↔TCP socat
on each end of an existing SSH TCP forward). Known port numbers must be
specifiable on each end. **Tunnel teardown must be ergonomic** — and, crucially,
these tunnels are **host-resident**: the socat/forward processes run *on the lab
hosts* and persist after the `otto link add` process exits (which is exactly why
they can be left around, and why state is discovered live — see below). TCP is
assumed unless UDP is selected. Every spawned process is **tagged with an otto
sentinel** (see § Dynamic Link state) so it is discoverable and unambiguously
ours.

**SSH carrier options (hop-aware phase forward-note, 2026-07-08):** when the
carrier is an `ssh -L` forward (the deferred multi-hop phase — sub-project #2's
direct-L2 tunnels use a plain TCP carrier, no SSH), it may need tunnel-specific
SSH options set *strictly for the tunnel process*, independent of otto's
management SSH sessions: keepalive intervals
(`ServerAliveInterval`/`ClientAliveInterval`), `ExitOnForwardFailure`,
connection timeouts, cipher/compression, etc.

### Dynamic Link state — live discovery, no ledger _(decided 2026-07-06)_

**Rejected:** a shared persistent registry/DB (file could go stale and would be
"extremely tricky to remedy"). **Adopted:** the **running processes on the hosts
are the single source of truth**, discovered on demand. Status is always TRUE
(observed), never guessed or memorized.

- **Discovery:** `asyncio.gather` a discovery command across all lab **Unix**
  hosts (embedded hosts can't host tunnels → skipped), parse the results into
  `Link` records. No file, no DB, nothing to reconcile.
- **Otto-owned identification (the crux):** every tunnel process otto spawns
  carries a **structured sentinel** that both marks it as otto's *and encodes its
  full record*, so discovery reconstructs the link from the process itself —
  e.g. launched via `exec -a "otto-link:<id>:<proto>:<a-host>:<a-if>:<a-port>:<b-host>:<b-if>:<b-port>"`.
  `pgrep -af '^otto-link:'` returns exactly ours and nothing else.
  - **No owner in the sentinel** _(decided 2026-07-06)_ — the marker is
    **owner-agnostic** so *any* user can discover and reap *all* otto tunnels with
    zero friction. No owner column, no owner-scoped removal.
  - **id** = deterministic hash of endpoints+ports+proto, so `add` is idempotent
    and a collision = a genuine duplicate.
  - **age** comes from the OS (`ps -o etimes` / process start time), not a stored
    timestamp — reuse kernel truth.
  - **one link = several tagged processes** (socat on A, forward on the hop, socat
    on B) sharing the same `id`; discovery gathers per-host and **groups by id**
    to reconstruct the whole link.
- **Teardown:** `otto link remove <id>` = gather → match sentinel → kill. Reads
  reality; no stored teardown recipe to desync.
- **Conflict check** for `add` = live discovery ∪ implicit (hop) ∪ declared
  (lab.json), all derived fresh.
- **Rate-limiting:** CLI scans on demand (freshness wins). The GUI polls behind a
  short **TTL cache** on the dynamic layer only; implicit + declared links are
  free (already in the loaded lab), only dynamic costs round-trips.
- **State boundary — pure argv, zero persisted state** _(decided 2026-07-06)_.
  No file, no DB, no host-local marker; the tagged process argv is the whole
  record. Truest to "always TRUE."

### Management Hosts

Some hosts exist mainly to manage other hosts or links — the source of arbitrary
commands with performance counts, reflecting stats about *other* hosts/links.
(Today the monitor is strictly per-host self-report; source-attribution to a
*different* element is new work — sub-project #5.) In the GUI these render as an
optional **"Sources" overlay**: management hosts with dashed "reports-for" edges
to the elements they feed, a toggle defaulting **off**. How to define/record a
management host is still open (§ Open Questions).

### GUI topology — container rendering (#6, forward-note 2026-07-08)

Docker **container hosts** must be represented in the Phase-2 topology GUI,
rendered **visually distinct** and **nested *inside* their parent host** (a
container-within-host visual), not as free-floating peers. Partially
anticipated already — the foundation surfaces containers as `local↔container`
implicit edges — but the nested/distinct rendering is the new requirement.
Bleeds into #6; noted here so it isn't lost. (Also nudges #2 onward to keep the
tunnel spawn/discovery routed through `host.oneshot`, which is docker-exec-backed
for `DockerHost`, so container tunnel endpoints stay possible in a later phase.)

## CLI

An `otto link` command group manages existing links and adds new ones. Mirror
`src/otto/cli/reservation.py` (Typer group + callback + `@command()` leaves),
register lazily via `builtin_commands.py`, reuse `_host_id_completer`.

### `otto link add`

Adds tunnels (`Link`s) from one host to another. Each link has a potential
protocol (UDP; others possible; TCP assumed otherwise). Must not conflict with
existing links (implicit, in `lab.json`, or added this session). Endpoints
specified by host id (+ interface).

### `otto link remove`

Removes dynamically-added links (not implicit hop links or declared `lab.json`
links). Discovers the tagged processes and kills them by sentinel id. **Not
owner-scoped** — any user can reap any otto tunnel (the point is cleaning up
whatever's left around). A bulk `--all` may prompt for confirmation.

### `otto link list` _(new)_

Lists links via **live discovery** (§ Dynamic Link state). **Defaults to dynamic
links** (id · endpoints · protocol · age — no owner column); `--all` folds in
implicit (hop-derived) + declared (`lab.json`) links for the full picture.
Because state is observed, there's nothing "stale" to prune — a dead/killed
tunnel simply stops appearing. This is the same data the GUI topology's link
layer consumes (behind its TTL cache).

### `otto link impair`

Impairs a link using a `LinkImpairer` (drop, jitter, corrupt, rate, …). NetEm is
the first class conforming to the Protocol. Links can have an associated
management host whose optional `impair` field selects a registered impairer.

### `otto link capture`

Captures traffic on a link (torn between link-command, host-command, or both —
links managed by a NetEm host are a good vector for IP-pair-specific capture;
capturing *all* traffic in/out of a host fits `otto host` better). pcap
tcpdump-style filtering allowed; all interfaces captured by default for hosts;
an `--interface` option validated against the host's configured interfaces
(now trivial — they're the `interfaces` map keys). Passed to the host's
`tcpdump`; other tcpdump options (frame/snap length, etc.) are must-have.

## Testing strategy _(2026-07-06)_

The live-discovery design makes most correctness unit-testable as pure functions;
the real network behavior needs the bed.

- **Hostless unit (CI, bulk of correctness):** `lab.json` parse/merge, `Link`
  model + topology derivation, **marker encode↔decode round-trips**, the
  **discovery-output parser** (canned `pgrep`/`ps` text → reconstructed records,
  incl. correctly *excluding* non-otto socats), conflict detection, and the
  tc/tcpdump/socat **command builders** (assert exact argv, run nothing).
- **Live-bed e2e (`e2e`/`hops` markers, real peer Unix VMs with
  `socat`/`tcpdump`/`tc` installed):** UDP tunnel add → push datagram end-to-end
  → assert receipt → remove → assert gone; kill a tunnel out-of-band → assert
  `list` reflects reality; spawn a *non-otto* socat → assert excluded; netem
  delay/loss on a data interface → measure RTT/loss delta → restore; capture with
  a filter → assert pcap contents.
- **Emulating complex networks:** peer VMs share an L2 net (all directly
  reachable), so to exercise **multi-hop** tunnels/realistic paths, force
  indirection via iptables (drop direct A→C, allow A→B→C) or netns, with declared
  links describing the intended data plane.
- **Safety (design + test, load-bearing on a shared bed):**
  1. **Never impair/block the management interface** — `tc` on the eth otto
     reaches the host through = instant self-lockout. Refuse to impair the mgmt
     `ip`'s device (mandatory). Auto-expiring netem is **optional** (`--expire`,
     default indefinite — long tests need persistence); handy for tests that want
     a botched rule to self-heal, but never forced.
  2. **Test-scoped ownership marker + guaranteed-teardown fixtures** so a test
     only reaps its own tunnels and never orphans processes on failure.
  3. Honor dev-VM rules: no heavy parallel load, don't power VMs, fail-loud on
     host-down.

## Backlog (from #2 final fix wave, 2026-07-09)

Deferred out of the #2 fix wave that closed the whole-branch review; not
blocking, but real gaps worth picking up in #2b/#3:

- **`list`'s `via <exit>` column** — spec §9.2 always intended `port` / `age` /
  `via <exit>` columns, but the shipped `otto link list` only renders `id` ·
  `endpoints` · `protocol`. Concretely: a **relay tunnel's exit host is
  currently invisible** in `list` output — `add --dest` records the exit host
  as an *origin* in `Observation`/`discover_observations`, and
  `discover_dynamic_links`'s grouping (`_group_and_resolve` in
  `src/otto/link/discovery.py`) keeps only the logical `a`/`b` endpoints, so
  the relay hop never surfaces to the CLI. Fix = thread origin hosts through
  to the printer and render `via <exit>` when the exit isn't a logical
  endpoint.
- **`remove` post-removal verify step** — spec §9.3 says "kill … verify gone";
  today `remove`/`_reap` (`src/otto/link/manage.py`) trust a `Status.Success`
  `kill` and stop there, with no re-scan confirming the process actually
  exited. Add an optional re-discovery pass after the kill (bounded by
  `_LINK_HOST_TIMEOUT`) that confirms the id is gone, folding a lingering
  process into `RemovedReport.unreachable` (or a new field) rather than
  silently trusting the shell exit code.
- **Consolidate `sentinel.parse_discovery` with `discover_dynamic_links`** —
  `src/otto/link/sentinel.py`'s `parse_discovery` and
  `src/otto/link/discovery.py`'s `_group_and_resolve` both group tagged
  processes by id from raw text, but their **merge semantics diverge**:
  `parse_discovery` merges per-id fields (keeps the first non-`None` port per
  end across duplicate observations — see its docstring), while
  `_group_and_resolve` just takes the *first* `Link` seen per id
  (`by_id.setdefault`) and discards the rest. `parse_discovery` looks like an
  earlier/parallel implementation that `discover_observations` +
  `parse_process_discovery` superseded but never replaced; worth auditing
  whether `parse_discovery` is still load-bearing anywhere (it has its own
  test coverage in `test_sentinel.py`) before picking one merge strategy and
  deleting the other.

## Open Questions

- **State boundary** — pure process-argv discovery (zero persisted state) vs. a
  self-cleaning host-local marker for richer metadata (§ Dynamic Link state).
- **Marker robustness** — is `exec -a`-set argv reliable across the Unix host
  family for all the process types we spawn (socat, ssh-forward)? Fallback if a
  host's `ps`/`pgrep` can't show the full argv?
- **Tunnel mechanism** — how the host-resident path is actually built (in-host
  `ssh -L` vs socat relay chains hop-by-hop). Sub-project #2 detail, but it
  determines what processes exist to discover.
- **Management-host definition** — a `RemoteHost` with a role/capability marker?
  How is "reports-for element X" recorded so the monitor and GUI can attribute
  its series? (Bleeds into sub-project #5 / the monitor backend contract.)
- **Capture home** — `otto link capture` vs `otto host capture` vs both.

## Resolved

- **Interface keying** — key = netdev name, value = extensible `InterfaceSpec`,
  string shorthand for back-compat _(2026-07-06)_.
- **Design staging** — foundation-first, 6 sub-projects, each its own spec
  _(2026-07-06)_.
- **`lab.json` top-level shape** — object with `hosts` + `links` sections; host
  array + schema unchanged; per-section merge across lab dirs _(2026-07-06)_.
- **No existing-user migration** — hard cutover to `lab.json`; no dual-format
  loader _(2026-07-06)_.
- **Declared link entry** — endpoints (host + optional interface, required only
  when a host has >1 interface); protocol informational for declared / functional
  for dynamic socat setup; **derived cross-lab membership** (union of endpoint
  labs — a link may span labs) _(2026-07-06)_.
- **Dynamic-link state = live discovery, no ledger** — running tagged processes
  on the hosts are the source of truth; `asyncio.gather` a discovery command,
  parse the otto sentinel from argv; **pure argv, zero persisted state**; `otto
  link list` (default dynamic, `--all` for everything); GUI behind a TTL cache
  _(2026-07-06)_.
- **Owner-agnostic sentinel** — no username in the process; any user reaps all
  otto tunnels frictionlessly; no owner column, no owner-scoped removal; age from
  `ps` _(2026-07-06)_.
- **Unified runtime `Link` type** — one type across provenances; cost-split
  accessors (`static_links` sync / `discover_dynamic_links` async / `all_links`);
  reconciled by deterministic `id` _(2026-07-06)_.
- **Impairment expiry optional** — `--expire` opt-in, default indefinite (long
  tests need persistence); mgmt-interface refusal mandatory _(2026-07-06)_.
