# `otto tunnel` (#2b): rename, bidirectional multi-hop, docker endpoints, old-OS proof — design

> Sub-project **#2b** of the link stack. Builds directly on #2 (`otto link` CLI +
> live socat tunnels, squash-merged to main as `bc37dba`). Scope decided with
> Chris 2026-07-09. Prior specs: `2026-07-06-link-foundation-design.md` (#1),
> `2026-07-08-link-cli-tunnels-design.md` (#2). Living scratchpad: `todo/link.md`.

## 1. Goal

Rename the user-facing unit from **link** to **tunnel** (the CLI, library API,
sentinel, and ids), make every tunnel **bidirectional**, extend it across
**explicit multi-hop chains** with per-hop socat TCP relays, admit **docker
containers as tunnel endpoints**, and prove the old-OS portability claims with
a **centos:7 (arm64) container** e2e that doubles as the docker-endpoint test.
Fold in the #2 review backlog: `via` column, degraded/partial status, post-add
and post-remove verification, and the discovery-parser consolidation.

**Vocabulary (binding):** a **link** is a topology *edge* (implicit hop-derived
or declared in `lab.json`) — it stays in `otto.link` untouched. A **tunnel** is
one end-to-end forwarding path built by one `add`: service port + protocol +
ordered host path (+ optional `--dest`). Its per-hop segments ride links.
Links = edges that exist; tunnels = paths we build over them.

## 2. Decisions log (2026-07-09, Chris)

| # | Decision |
| --- | --- |
| D1 | Rename goes **full-stack for the tunnel machinery**: CLI `otto tunnel`, API `add_tunnel`/`remove_tunnel`/`remove_all_tunnels`/`discover_tunnels`, sentinel prefix `otto-tunnel`, ids `tun-…`, new `otto.tunnel` package. `Link` survives only as the topology-edge concept. |
| D2 | The static-link CLI view (`otto link list --all`) is **dropped** — no replacement command in #2b; the GUI (#6) inherits topology browsing. Static derivation itself stays in `otto.link`; the `add`-time conflict check against static links is dropped as vestigial (§7 — `tun-` ids cannot collide with static handles, and edges make no port claims). |
| D3 | Full #2b in **one phase** (this spec). |
| D4 | **One `add` = one tunnel.** Two adds between the same hosts on different ports = two tunnels. No endpoint-pair aggregation type. |
| D5 | Multi-hop paths are **explicit**: `--hosts a,c,b` is the exact ordered chain. No auto-routing from topology. |
| D6 | Intermediate hops are carried by **socat TCP relays** (one per direction per hop). ssh-carrier stays out: ssh moves TCP only, so the endpoint UDP↔TCP shims remain regardless — it can never get an endpoint below 2 processes, and it adds an ssh client process + credential/keepalive management per hop. |
| D7 | **Tunnels are bidirectional, always** — no `--one-way` flag. Exactly **2 tagged socats on every chain host**. |
| D8 | Docker containers participate as **endpoints only**, and a container's chain neighbor **must be its `parent` host** — validated at add time. No auto-insertion of the parent, no containers as relays. |
| D9 | Old-OS validation = the **centos:7 arm64 container** e2e (dual-purpose with the docker-endpoint test). True CentOS-6/2.6.32 remains a documented manual check if an x86_64 host ever joins the bed. |
| D10 | The centos:7 image + compose service live in a **test repo** (`tests/repoN/docker/…` pattern) and run through otto's own `compose_up`, not Vagrant provisioning. The existing repo1/repo2 stacks must not inherit the centos pull. |
| D11 | Sentinel contract break is accepted (no otto users): new prefix `otto-tunnel:v1:…`, `otto-link:` parsing deleted. |

## 3. Package split & rename inventory

### 3.1 New package `src/otto/tunnel/`

| File | Contents |
| --- | --- |
| `model.py` | `Tunnel`, `TunnelHop`, `Direction`, `Role`, `make_tunnel_id` |
| `sentinel.py` | v2 codec: `encode_sentinel`, `parse_sentinel` (see §5) |
| `socat.py` | pure builders (moved from `otto.link.socat`, extended per §6): `ingress_socat_args`, `relay_socat_args`, `egress_socat_args`, `launch_command`, `DISCOVERY_PS_COMMAND`, `FREE_PORT_PROBE_COMMAND`, `parse_listening_ports`, `pick_free_port` |
| `discovery.py` | `discover_tunnels(lab)` — gather + group + status (see §9) |
| `manage.py` | `add_tunnel`, `remove_tunnel`, `remove_all_tunnels`, endpoint/port resolution, verify passes |
| `__init__.py` | re-exports the public API (§12) |

`otto.tunnel` imports from `otto.link` (a tunnel rides links); never the
reverse.

### 3.2 `otto.link` keeps (unchanged unless noted)

- `model.py`: `Link`, `LinkEndpoint`, `Provenance`, `make_static_link_id`.
  `make_dynamic_link_id` is **deleted** (superseded by `make_tunnel_id`);
  `Link.__post_init__` provenance rules drop their dynamic-id branch.
- `derive.py` + `lab.static_links()`: implicit/declared edge derivation.
- **Deleted from `otto.link`:** `socat.py`, `manage.py`, `discovery.py`,
  `sentinel.py` (including `parse_discovery` — the duplicate-grouping backlog
  item resolves by there being exactly one grouping implementation, in
  `otto.tunnel.discovery`). `all_links` and
  `discover_dynamic_links`/`discover_dynamic_links_status` are deleted with
  them (D2 removed their only consumer; the monitor-facing surface is now
  `discover_tunnels`).

### 3.3 CLI, completion, docs

- `src/otto/cli/link.py` → `src/otto/cli/tunnel.py`: Typer group `otto tunnel`
  with `add`/`list`/`remove`. `list --all` deleted (D2). Registration in
  `builtin_commands.py` renamed (`output_dir=False` stays).
- `completion_cache.py`: `DYNAMIC_LINKS_KEY = "__dynamic_links__"` →
  `DYNAMIC_TUNNELS_KEY = "__dynamic_tunnels__"`;
  `record_dynamic_link_ids`/`read_dynamic_link_ids` →
  `record_tunnel_ids`/`read_tunnel_ids`; TTL stays
  `DYNAMIC_LINKS_TTL_SECONDS = 120` → `DYNAMIC_TUNNELS_TTL_SECONDS = 120`.
- Docs sweep: `docs/guide/link.md` (rename/rewrite to tunnel guide),
  `docs/guide/cli-reference.md`, `docs/guide/lab-config.md` (links-the-edges
  wording stays; CLI references update), `docs/guide/monitor.md`,
  `docs/api/link.rst` (+ new `docs/api/tunnel.rst`). Historical
  superpowers specs/plans are NOT rewritten.
- Tests: `tests/unit/link/` splits — edge-model tests stay in
  `tests/unit/link/`, tunnel-machinery tests move to `tests/unit/tunnel/`;
  `tests/e2e/test_link_tunnels_e2e.py` → `tests/e2e/test_tunnel_e2e.py`.
- `has_bash` semantics unchanged: tunnel discovery scans only
  `getattr(h, "has_bash", False)` hosts.

## 4. Tunnel model & ids

```python
class Direction(StrEnum):
    FWD = "fwd"   # first-listed → last-listed host
    REV = "rev"

class Role(StrEnum):
    INGRESS = "ingress"
    RELAY = "relay"
    EGRESS = "egress"

@dataclass(frozen=True)
class TunnelHop:
    host: str                 # host id (containers use their dotted id)
    interface: str | None     # netdev key; None = single/assumed (containers: always None)

@dataclass
class Tunnel:
    id: str                   # "tun-" + 12 hex + "-" + service_port
    protocol: str             # "udp" | "tcp"
    service_port: int
    path: tuple[TunnelHop, ...]   # ordered, len >= 2
    dest: str | None = None       # far-end (b-side) delivery override host id
```

- `make_tunnel_id(path, protocol, service_port)` = `"tun-"` + first **12 hex**
  of sha256 over the canonical string
  `protocol + "|" + ",".join(f"{hop.host}@{hop.interface or ''}" for hop in path)`,
  then `f"-{service_port}"` appended as a **readable suffix** (the port is not
  hashed — same scheme as #2's `lnk-<hex>-<port>`). Path order is **not**
  normalized: the hash covers the ordered chain, so `a,c,b` ≠ `a,d,b` ≠
  `b,c,a` (a reversed duplicate is instead rejected by the endpoint-bind
  conflict rule, §7).
- `dest` is deliberately outside the id, mirroring #2 (same route + port +
  proto with a different dest is the same plumbing claim on the same ingress
  bind — a conflict, not a sibling).
- The static-link id namespace (`name`/`a--b`) and `tun-` never collide.

## 5. Sentinel v2: `otto-tunnel:v1:`

Every tunnel process's `argv[0]` (set via `bash -c 'exec -a …'`, unchanged):

```text
otto-tunnel:v1:<id>:<proto>:<svc-port>:<carrier-port>:<direction>:<role>:<hop-index>:<dest>:<path>
```

**11 colon-joined segments** (prefix, version, then 9 fields), each
percent-encoded with `quote(safe="")`; empty segment = None. Fields:

- `id`, `proto`, `svc-port` — as in §4.
- `carrier-port` — the carrier port of **this process's direction** (§6.2).
- `direction` — `fwd` | `rev`.
- `role` — `ingress` | `relay` | `egress`. Redundant with
  hop-index+direction but kept explicit so `ps` output is self-explanatory to
  a human tracing a tunnel by hand.
- `hop-index` — this host's 0-based position in the fwd-ordered path.
- `dest` — far-end delivery override host id, or empty.
- `path` — the full fwd-ordered chain, one segment: each hop rendered
  `host@iface` (or bare `host`), entries individually percent-encoded then
  joined with `,`, the joined string percent-encoded once more so the segment
  contains no raw `:` or `,` ambiguity.

Properties preserved from v1: **owner-agnostic** (no username), **fully
self-describing** (any single surviving process reconstructs the entire
intended tunnel — the record survives every other host being down), unknown
versions/prefixes parse to `None` (skipped, never an error). The
`otto-link:v1` codec and its parsing are **deleted** (D11);
`DISCOVERY_PS_COMMAND`'s grep pattern changes to `' otto-tunnel:'`.

Stability contract restarts at `otto-tunnel:v1`: from the first release with
users, evolve only by adding versions and keeping old ones parseable.

## 6. Bidirectional chain construction

### 6.1 Process layout — exactly 2 socats per host

For path `h₀,…,hₙ₋₁` (n ≥ 2), each direction is an independent chain;
per-flow reply traffic already rides each chain bidirectionally (socat relays
both ways per connection; UDP `fork` children stay peer-connected). What the
mirror adds is **initiation from either end**.

| Host | FWD process (carrier p_fwd) | REV process (carrier p_rev) |
| --- | --- | --- |
| `h₀` | ingress: `{PROTO}-LISTEN:<svc>,bind=<h₀-ip>,fork,reuseaddr → TCP4:<h₁-ip>:p_fwd` | egress: `TCP4-LISTEN:p_rev,fork,reuseaddr → {PROTO}4:<deliver₀>:<svc>` |
| `hᵢ` (0<i<n−1) | relay: `TCP4-LISTEN:p_fwd,fork,reuseaddr → TCP4:<hᵢ₊₁-ip>:p_fwd` | relay: `TCP4-LISTEN:p_rev,fork,reuseaddr → TCP4:<hᵢ₋₁-ip>:p_rev` |
| `hₙ₋₁` | egress: `TCP4-LISTEN:p_fwd,fork,reuseaddr → {PROTO}4:<deliverₙ₋₁>:<svc>` | ingress: `{PROTO}-LISTEN:<svc>,bind=<hₙ₋₁-ip>,fork,reuseaddr → TCP4:<hₙ₋₂-ip>:p_rev` |

2n processes total; every host carries exactly 2. Address keywords stay the
old-stable set (`UDP4-LISTEN`/`TCP4-LISTEN`/`UDP4`/`TCP4`, `fork`,
`reuseaddr`, plus `bind=`).

### 6.2 Ports

- **Service port** (`--port`): the same number binds on both endpoint hosts —
  the same-port-everywhere traceability rule, now symmetric.
- **Two carrier ports**, one per direction, each constant along its entire
  chain (`p_fwd` on every fwd hop, `p_rev` on every rev hop). Two are required
  because an intermediate host runs one relay listener per direction and they
  cannot bind the same TCP port. Both are picked from `[49152, 65535]`, free
  on **every** chain host: the free-port probe (`ss`/`netstat` fallback,
  unchanged) runs on all chain hosts, the used-sets union, and
  `pick_free_port` returns two distinct ports. A host whose probe fails
  (tools missing — e.g. a minimal container) contributes nothing to the
  union; an actual bind collision is then caught by the post-add verify.

### 6.3 Binds, delivery, and the loop hazard

The #2 default (egress delivers to the exit host's own data IP) becomes a
**loop** under bidirectionality: that address is now bound by the reverse
ingress, so a delivered datagram would U-turn into the reverse tunnel. Rules:

- **Ingress binds specifically** to the endpoint's resolved data-plane IP
  (`bind=<ip>`): the `@iface` selection becomes functional. Resolution order
  per endpoint is unchanged from #2 (named iface → single iface → error on
  ambiguous/address-less), containers per §8.
- **Default delivery target is `127.0.0.1`** on the endpoint host: the local
  service listens on loopback (or any address other than the ingress bind).
- `--dest <host[@iface]>` overrides the **far-end** (`hₙ₋₁`-side) delivery
  target with a third host's resolved IP, exactly as shipped — traffic then
  appears at `dest` sourced from `hₙ₋₁`. The near-end delivery stays
  loopback (no symmetric override until a use case demands one).
- **Documented caveat:** a service binding wildcard `0.0.0.0:<svc>` without
  `SO_REUSEADDR` can collide with the ingress bind. The failure mode is loud:
  the ingress socat exits at bind time and the post-add verify reports it.

### 6.4 Launch order, rollback, post-add verify

- Per direction, launch **downstream-first** (egress → relays → ingress) so a
  listener exists before anything upstream can connect to it. FWD chain fully
  launches, then REV.
- Launch template is the shipped `launch_command` verbatim: `systemd-run
  --user --collect --quiet` when available, `setsid` fallback otherwise, argv
  tagged via `bash -c 'exec -a "$1" "${@:2}"'`.
- **Post-add verify:** after launching, run the discovery scan over the chain
  hosts and require all **2n** expected `(host, direction, role)` processes
  present. On any miss: reap everything observed for the id (the §10 kill
  path), then raise with a per-host account of what failed to start. This is
  what turns quiet bind failures (§6.3 caveat, port races, missing socat)
  into loud add-time errors. No half-tunnels survive a failed add.
- Every remote `oneshot` on these paths stays bounded by
  `_TUNNEL_HOST_TIMEOUT = 30.0` (renamed from `_LINK_HOST_TIMEOUT`) via
  `asyncio.wait_for`, and `_require_tools`' socat/bash preflight extends over
  all chain hosts.

## 7. Conflict rules (at `add`)

Checked against **live discovery only** (static links are edges, not port
claims — the #2 id-collision check against implicit/declared links is dropped
with D2's simplification):

1. **Id idempotency:** an existing tunnel with the same id ⇒ error
   ("already exists", names the id). Same rule as #2: a collision is a
   genuine duplicate, not a merge.
2. **Endpoint-bind conflict:** no existing tunnel may hold an ingress on the
   same `(host, service_port, protocol)` as either endpoint of the new one.
   This is what rejects `b,c,a` after `a,c,b` (both need ingress binds on the
   same two endpoints) and any same-port re-plumbing with a different path or
   dest.

## 8. Docker container endpoints

- A `DockerContainerHost` may appear **only at position 0 or n−1** (D8), and
  its chain neighbor must be **its own `parent`** (`docker_host.py`'s
  `parent` field, compared by host id). Violations fail at add time with a
  message naming the required parent.
- `@iface` on a container entry is **rejected** (containers have no modeled
  `interfaces`); the container's data IP is resolved at add time via the
  parent: `docker inspect -f
  '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <container_id>`
  (run through `parent.oneshot`; empty output ⇒ loud error). Reachability
  holds by construction: the only allowed neighbor is the parent, and the
  bridge IP is reachable from the parent.
- The container's socat launches through the **container host's own
  `oneshot`** (docker-exec via parent) with the same `launch_command`.
  Containers have no systemd user manager, so `command -v systemd-run` fails
  and the **`setsid` branch runs by construction** — a `docker exec` child
  orphaned by its exec session reparents to the container's PID 1 and
  survives. Discovery and kill likewise ride the container's `oneshot`;
  containers are already scanned today (`has_bash=True` default).
- Delivery/bind rules are §6.3 with the inspected IP as the bind address and
  loopback-in-container as the default delivery.
- `--hosts` completion includes container hosts; the adjacency-aware
  completer extends the shipped L2 filter with parent↔container edges.

## 9. Discovery, status, `list`

- `discover_tunnels(lab)` gathers `DISCOVERY_PS_COMMAND` across `has_bash`
  hosts (bounded, best-effort, `asyncio.gather` — unchanged skeleton), parses
  sentinels, and groups by tunnel id. From any one observation the full
  intended tunnel is reconstructed (§5); observations fill in per-process
  presence and `etime`-derived age.
- Return shape:

```python
@dataclass
class DiscoveredTunnel:
    tunnel: Tunnel
    present: set[tuple[str, Direction, Role]]   # observed processes
    missing: set[tuple[str, Direction, Role]]   # expected − observed (reachable hosts only)
    age_seconds: int    # oldest observed process age (max etime); CLI humanizes
    uncertain: bool     # ≥1 chain host was unreachable/timed out

@dataclass
class TunnelDiscovery:
    tunnels: list[DiscoveredTunnel]
    unreachable: list[str]                      # host ids that failed the scan
```

- **Status semantics:** `ok` = `missing` empty and not `uncertain`;
  `degraded` = some expected process absent on a host that *was* scanned;
  `uncertain` (rendered as a trailing `?`) = a chain host was unreachable, so
  absence there is unknown — this closes the #2 partial-marker backlog item
  honestly: observed truth, flagged uncertainty, no guessing.
- `otto tunnel list` columns:
  `ID · ENDPOINTS (a ↔ b) · VIA · PORT · PROTO · AGE · STATUS`, where `VIA`
  renders the intermediate hops in order plus `→ <dest>` when a delivery
  override exists (closes the `via <exit>` backlog item), and `STATUS`
  renders `ok` / `degraded (3/4)` / with `?` suffix per above. Empty state
  and unreachable-host summary line stay as shipped.
- The CLI records ids to the completion cache (`__dynamic_tunnels__`, TTL
  120 s) exactly as shipped.

## 10. `remove` — kill + verify

- `remove_tunnel(lab, id)`: discover, kill every observed pid for the id on
  every host carrying one (containers via their `oneshot`), checking each
  kill's `CommandResult.is_ok` — then a **bounded re-discovery pass over the
  affected hosts** confirms the id is gone. Survivors are reported in
  `RemovedReport.survivors` (new field: `list[tuple[host, pid]]`) and the CLI
  exits non-zero naming them — never a silent trust of kill's exit code
  (closes the post-remove-verify backlog item).
- `remove_all_tunnels` iterates the same path; `otto tunnel remove --all`
  keeps its confirmation prompt.
- `systemd-run --collect` means the kill leaves no failed-unit cruft
  (validated in #2); the setsid/container path needs no unit cleanup.

## 11. CLI surface (complete)

```text
otto tunnel add    --hosts h0[@if][,h1…,hn-1] --port N [--protocol udp|tcp] [--dest host[@if]]
otto tunnel list
otto tunnel remove <id> | --all [--yes]
```

- `--hosts` takes 2+ entries; consecutive-pair reachability validation as
  shipped for host↔host pairs, parent-adjacency rule for container entries.
- `--protocol` defaults `tcp` (unchanged). `--port` required (unchanged).
- Completers: chain-aware host completion (L2 + parent↔container edges),
  `remove` id completion from the `__dynamic_tunnels__` cache.
- Deleted: `list --all` (D2).

## 12. Library API (`otto.tunnel`)

```python
async def add_tunnel(lab, hosts, port, protocol="tcp", dest=None) -> AddedTunnel
async def remove_tunnel(lab, tunnel_id) -> RemovedReport
async def remove_all_tunnels(lab) -> RemovedReport
async def discover_tunnels(lab) -> TunnelDiscovery
```

Library-first: the CLI is a thin caller, as in #2. `AddedTunnel` carries the
`Tunnel` plus both carrier ports and the per-host launch account.
`discover_tunnels` is the monitor-facing surface (the GUI's TTL-cached
dynamic layer consumes `TunnelDiscovery` unchanged in #6's timeframe).

## 13. Old-OS proof: centos:7 fixture through otto's own docker stack

- **Fixture:** `tests/repo2/docker/oldos/Dockerfile` — `FROM centos:7`
  (multi-arch; arm64 runs natively on the peers), sed the yum repos to
  `vault.centos.org`, `yum install -y socat`, keep-alive CMD (repo2's
  `while sleep 3600` pattern) — plus `tests/repo2/docker/oldos/compose.yml`
  defining one `oldos` service. **Isolation rule (D10):** the existing repo2
  `[[docker.composes]]` entry is untouched; the tunnel e2e scaffolds its own
  settings declaring repo2's dir with *only* the oldos compose and a unique
  compose project name (the `test_docker_e2e_cli.py` pattern), so existing
  docker e2e stacks never pull centos:7. If plan-time inspection shows
  per-settings compose selection isn't expressible, fall back to a tiny
  dedicated `tests/repo_oldos/` — decide in the plan, not during
  implementation.
- **What it proves natively** (bash 4.2, procps-ng 3.3.10, no systemd, yum
  era): the `setsid` launch fallback, formatted-`etime` parsing on old
  procps, the `exec -a` tagging on old bash, and old-stable socat address
  forms — every userland claim the code actually makes. It is **not** a
  2.6.32 kernel; true CentOS-6 validation stays a documented manual check
  gated on an x86_64 bed member (this paragraph is that documentation).
- First `compose_up` on a peer pulls/builds once; `build_images`' context-hash
  skip + image-store caching make every later run a cache hit. Nothing needs
  long-term persistence — re-up is the mechanism.

## 14. Testing strategy

**Unit (hostless, CI):**

- socat builders: exact argv for all three roles × both directions, `bind=`
  presence on ingress only, loopback default vs `--dest` delivery, two
  distinct carrier ports, old-stable keyword set.
- sentinel v2: encode↔parse round-trips (ifaces with `:`/`,`, empty iface,
  dest, long paths), malformed/foreign/`otto-link:`-era tokens → `None`,
  segment-count pinning.
- id: determinism, order sensitivity (`a,c,b` ≠ `b,c,a`), dest exclusion,
  `tun-` + 12-hex + port format.
- discovery grouping: multi-host observation merge, `present`/`missing`
  computation, degraded vs uncertain, unreachable propagation, non-otto
  exclusion, malformed-etime tolerance (folds the #2 coverage minors).
- manage: conflict rules (id, endpoint-bind, reversed-path rejection),
  container adjacency validation (wrong neighbor, container-as-relay,
  `@iface`-on-container), rollback-on-partial-launch, post-add verify, remove
  verify + survivors, port-intersection picking, probe-failure tolerance,
  timeout bounding. Test fakes use real `CommandResult` (the #2 lesson).
- CLI: parsing, completers (chain-aware + cache), `list` rendering incl.
  `VIA`/`STATUS`, exit codes.
- completion cache: renamed key + TTL behavior.

**Live-bed e2e (`tests/e2e/test_tunnel_e2e.py`, markers unchanged:
`integration` + `hops` + `xdist_group`; fail-loud on host-down):**

1. Direct a↔b UDP: datagram a→b delivered, **and reply + b-side-initiated
   datagram delivered a-ward** (proves both mirror chains).
2. Multi-hop a→c→b UDP: end-to-end delivery, relay processes present on c,
   `list` renders the `VIA` column.
3. Container endpoint: compose-up the centos:7 `oldos` service on a peer,
   tunnel `<other-peer>,<parent>,<container>`, datagram delivered to a
   loopback listener *inside the container* (socat `-u UDP4-RECVFROM` →
   file; no python dependency in-container), discovery shows it, remove
   verifies clean. This single test is the docker-endpoint proof AND the
   old-OS/setsid proof (D9).
4. Non-otto socat exclusion + out-of-band kill → `degraded` status (migrated
   and extended from #2's suite).

**Gates:** full `make coverage` (94% floor), nox lint/typecheck/docs — the #2
bar.

## 15. Out of scope (deferred)

ssh-carrier hops (D6); auto-routing from topology (D5); containers as relays
(D8); endpoint-pair Tunnel aggregation (D4); a near-end `--dest` override
(§6.3); impair (#3), capture (#4), management hosts (#5), GUI topology (#6);
CentOS-6-on-x86_64 (D9); the static-id iface-collision minor (lives in
`otto.link`'s static namespace, untouched by this phase).

## 16. Implementation logistics

Worktree `link-foundation` reset onto current main (done 2026-07-09; main
moves under us — other agents are landing work — so rebase again before the
final review/merge). Subagent-driven development per task, fable
whole-branch review at the end, squash-merge by Chris. Sentinel/id/CLI
contract breaks need no migration (no users; only our bed has live v1
processes — reap them with the *old* branch's remove or a manual
`pkill -f otto-link:` before first use of the new CLI on the bed).
