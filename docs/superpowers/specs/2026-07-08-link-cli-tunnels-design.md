# `otto link` CLI + live tunnels — design (sub-project #2)

> **Status:** designed 2026-07-08, awaiting review → plan.
> **Stack position:** sub-project **#2** of the Link stack (see
> `todo/link.md` for the 6-part staging and
> `docs/superpowers/specs/2026-07-06-link-foundation-design.md` for #1, the
> merged foundation). Builds directly on #1's `otto.link` package and the
> host-id-rules follow-on, both merged to `main`.
> **Impairment (#3), capture (#4), management hosts (#5), GUI topology (#6)
> remain separate.**

## 1. Purpose

Turn the foundation's static link model into a live, manageable one: a first-
class `otto link` command group that **creates**, **lists**, and **removes**
host-resident tunnels, and the **live discovery** that observes them. The
foundation shipped the pure data model (`Link`, `LinkEndpoint`, `make_link_id`,
the versioned sentinel codec, `parse_discovery`) plus a `discover_dynamic_links`
stub that raises `NotImplementedError`. This phase makes it real.

Every capability is a **plain callable library function** first; the CLI (and,
later, the GUI/monitor) are thin consumers. This is a load-bearing directive: a
generic Python developer must be able to `add_link(...)` / `list` / `remove_link(...)`
from their own code without going through a CLI handler.

## 2. Scope

**In scope (this phase):**

- `otto link add` / `otto link list` / `otto link remove` CLI, plus their
  library-function equivalents.
- Live discovery: wire `discover_dynamic_links`; structured so it is
  **monitor-compatible** (a reusable per-host command + pure parser).
- Host-resident tunnel construction via the **two-socat UDP↔TCP bridge** over a
  **direct L2 TCP carrier**.
- A **relay** endpoint mode (`--dest`): tunnel exits at host B but delivers to a
  third host C so traffic appears sourced from B.
- Tab completion for `--hosts` (host ids) and `remove <id>` (live tunnel ids).
- Link-identity revision: dynamic ids carry a readable port suffix; static links
  get readable handles (§6).

**Out of scope (deferred, with forward-compatible seams left in):**

- **`Tunnel` type** — the ordered-list-of-`Link`s path object. Deferred until a
  phase produces genuine multi-segment paths; this phase's public unit is the
  dynamic `Link`. (No `lnk-<hex>` grouping change needed to add it later.)
- **Multi-hop tunnels** — an `ssh -L` carrier crossing ssh-only jump hosts. The
  bridge shape (ingress/egress socats) is built now so this drops in later;
  `--hosts` of 3+ is valid syntax but rejected this phase.
- **Monitor wiring** — actually scheduling discovery on the collection interval,
  storing edges, topology views. Designed *for* (#5/#6), not built now.
- **Container-endpoint tunnels** — a tunnel whose endpoint is a `DockerHost`.
  Kept *possible* by routing spawn/discovery through `host.oneshot` (docker-exec-
  backed for `DockerHost`), but not part of this phase's contract.
- **Impairment / capture / management-host attribution** — #3/#4/#5.
- **Subnet-aware reachability** — this phase uses a simple L2 heuristic for
  reachability completion; true per-interface subnets come later.

## 3. Settled decisions (index)

1. Full #2 in one phase: add + remove + list + live construction.
2. **Direct L2 tunnels only**; multi-hop deferred.
3. **Two-socat UDP↔TCP bridge** now (ingress + egress + TCP carrier), even for
   direct L2 — forward-compatible with the deferred ssh-carrier.
4. **`Tunnel` type deferred**; unit = dynamic `Link`.
5. Endpoints given as **`--hosts` comma-list** of `host[@iface]`; `--port`
   (same value both ends); `--protocol tcp|udp`; `--dest` relay target.
6. **Dynamic link id = `make_link_id(route)` + `-<port>` suffix**
   (`lnk-<hex>-<port>`); route hash unchanged.
7. **Static links get readable handles** (declared `name`, else `a--b`), *not*
   `lnk-<hex>` — they aren't otto tunnels.
8. **Live discovery two-layer**: per-host `(command, pure parser)` +
   lab-level aggregator. Monitor-compatible; monitor wiring deferred.
9. **Host-down = best-effort + transparent** (attempt everything reachable, name
   every failure loudly; `remove` exits non-zero if it couldn't fully reap).
10. **Logging**: internal host I/O at `QUIET`/`NEVER`; only warnings/errors to
    console; **no per-command log file**.
11. **`remove <id>` completion** from a new short-TTL reserved key in the
    completion cache (§11).
12. **Old-OS portability**: portable `ps` fields, `bash -c 'exec -a'` tagging,
    old-stable socat; CentOS 6 docker container as the portability-test vehicle
    (testing follow-on).

## 4. Module layout

`src/otto/link/` today: `model` · `derive` · `sentinel` · `discovery` (stub) ·
`__init__`. Add two focused modules; keep `otto.link` free of `otto.monitor`
and CLI imports:

- **`socat.py`** — *pure* command/argv builders, zero I/O. Given a resolved
  `Link` + carrier port, produce the exact `bash -c 'exec -a "$1" socat …' …`
  argv for the ingress and egress processes, and the portable `ps` discovery
  command string. This is the "assert exact argv, run nothing" test surface.
- **`manage.py`** — async orchestration: `add_link`, `remove_link`,
  `remove_all_links`. Resolve endpoints, conflict-check, spawn/kill via
  `host.oneshot`, return typed reports.
- **`discovery.py`** — wire `discover_dynamic_links` (the two-layer live
  gather + aggregate) and keep `all_links`. The per-host parser lives here as a
  pure function (§8), reused verbatim by a future monitor adapter.

## 5. Public library API

Re-exported from `otto.link`. The CLI is a thin consumer of exactly these:

```python
lab.static_links() -> list[Link]                       # sync, exists (#1)
await discover_dynamic_links(lab) -> list[Link]        # async, wire the stub
await all_links(lab) -> list[Link]                     # async, exists (#1) + §9

await add_link(
    lab, hosts: list[LinkEndpointSpec], *,             # ordered path (2 this phase)
    port: int, protocol: str = "tcp",
    dest: LinkEndpointSpec | None = None,              # relay target; default = last host
) -> AddedTunnel                                       # the created Link + where it runs

await remove_link(lab, link_id: str) -> RemovedReport  # discover → kill by id
await remove_all_links(lab) -> RemovedReport           # reap every otto tunnel
```

- `LinkEndpointSpec` is a small `(host_id, interface | None)` resolved against
  the lab via `derive._resolve_endpoint` *before* id computation (so a dynamic
  link's id matches its declared route where one exists).
- `AddedTunnel` / `RemovedReport` are frozen dataclasses reporting the `Link`,
  the origin hosts touched, pids, and any per-host failures (§9).

## 6. Link identity

**Dynamic tunnels** need a stable content id — it rides in the sentinel and is
how `remove` reaps. **Static links** (implicit hop edges, declared routes) do
not; slapping a `lnk-<hex>` on them wrongly implies "an otto tunnel."

- **Dynamic:** `id = make_link_id(a, b, protocol) + "-" + str(port)`
  → `lnk-ab12cd34ef56-161`. `make_link_id` (the route hash, ports excluded) is
  **unchanged** — the port lives in the readable suffix. So the port is visible
  in `list`, in `remove <id>`, and in every process `argv[0]`. Distinct ports →
  distinct ids → distinct tunnels; same route+proto+port → same id → a genuine
  duplicate (`add` conflict).
- **Static:** `id` is a readable handle — the declared `name` if present, else
  `<a-host>--<b-host>` (endpoints sorted for symmetry). No hash; implicit hop
  edges follow the same `<a>--<b>` form (e.g. `local--test1`). Static ids are for
  display/reference only — nothing in *this* phase acts on a static link by id —
  so exact collision handling when two static routes share an endpoint pair (e.g.
  different protocols both rendering `a--b`) is deferred to the phase that first
  addresses them by id (#3/#4); the natural resolution is a `/<proto>` suffix
  computed in the lab-assembly pass that already stamps logical indices.

**Foundation revision:** #1's `Link.__post_init__` auto-computed `make_link_id`
for *every* provenance. This phase changes id assignment to be provenance-aware
(readable for static, hashed+suffixed for dynamic). This alters merged behavior;
it is safe because there are no otto users yet and no static ids are persisted
(all derived at load time). Update the `make_link_id` docstring: it now names
the *route* only and is consumed solely by the dynamic-id builder and any
reconciliation; the frozen route-hash algorithm itself is untouched.

## 7. Tunnel construction (`otto link add`)

### 7.1 CLI surface

```text
otto link add --hosts <h1[@if1],h2[@if2]> --port P [--protocol udp] [--dest H[@if]]
```

- **`--hosts`** — ordered comma-list of `host[@iface]`. The tunnel's endpoints
  are the first and last entries. This phase requires **exactly 2** hosts (the
  direct-L2 case); 3+ is valid syntax but rejected with
  "multi-hop paths arrive with the hop-aware phase." `@iface` pins an interface,
  required only when a host has >1 interface (else auto-resolved by
  `_resolve_endpoint`). `@` is chosen because host ids are `[a-z0-9-]` and netdev
  aliases can contain `:` (`eth0:1`) — no collision.
- **`--port P`** — the service port, used on **both** ends (send to `P` on the
  ingress host, arrives at `P` on the destination). One value so a tunnel is
  easy to trace by port across every hop.
- **`--protocol`** — `tcp` (default) or `udp`. Drives the socat address types.
- **`--dest H[@iface]`** — the final delivery target, **defaulting to the last
  `--hosts` entry** (tunnel terminates at the exit host). When set to a *third*
  host C, the exit host relays to C natively so C sees traffic **sourced from the
  exit host**, not from a loopback/sshd process (§7.3). Logical endpoints =
  `(first host, dest)`.

### 7.2 The two-socat bridge (direct L2)

For a tunnel with ingress host **A**, exit host **B**, destination **D**
(`D == B` unless `--dest`), and carrier TCP port `C` (an internal, otto-chosen
free TCP port on B — not user-facing):

- **Egress socat on B** (spawned first): accepts the carrier TCP, delivers to D.
  - udp: `socat TCP4-LISTEN:C,fork,reuseaddr UDP4:<D-ip>:P`
  - tcp: `socat TCP4-LISTEN:C,fork,reuseaddr TCP4:<D-ip>:P`
- **Ingress socat on A**: accepts client traffic on `P`, ships over the carrier.
  - udp: `socat UDP4-LISTEN:P,fork,reuseaddr TCP4:<B-ip>:C`
  - tcp: `socat TCP4-LISTEN:P,fork,reuseaddr TCP4:<B-ip>:C`

A client on A sends to `P` → arrives at D:`P`. Two tagged processes share the
same tunnel id. The carrier is a **direct L2 TCP connection A→B** this phase;
the deferred hop-aware phase swaps in an `ssh -L` carrier without moving the
socat ends. socat addresses stay in old-stable forms (`UDP4-LISTEN`,
`TCP4-LISTEN`, `fork`, `reuseaddr`) for old-OS compatibility.

### 7.3 Why relay makes traffic appear from B

With `--dest C`, B's egress socat targets `C`'s ip, so B originates the final
datagram from its own interface — on C it is a normal `B → C` packet. A plain
`ssh -L` terminating on C would instead deliver as a loopback/sshd-sourced
connection. socat-on-B is both simpler (no extra ssh for the L2 hop) and
achieves the source-appears-as-B goal directly.

### 7.4 Host-resident tagging & detachment

Processes must outlive the `otto link add` invocation and be discoverable. The
sentinel is set as `argv[0]` via `exec -a` (a bash builtin), and `socat_args` is
the **full** argv (it begins with `socat`), so the template must NOT hardcode the
program name:

```bash
bash -c 'exec -a "$1" "${@:2}"' _ '<sentinel>' socat <addr> <addr>
```

`exec -a` is **absent from dash/busybox `sh`**, so we invoke bash explicitly and
**require bash on tunnel-hosting hosts** (the `has_bash` capability, §8).

**Surviving the ssh session (found via the live-bed e2e — invisible to unit
tests).** A `setsid`-detached background process is NOT enough on a systemd host:
otto spawns it through `host.oneshot`'s ssh session, and a process left in that
session's scope is killed the instant the channel closes — `setsid` does not
escape the session cgroup (confirmed: `setsid sleep` survives the identical
launch; `setsid socat` does not). So on systemd hosts the process is launched in
the **user manager's scope** via `systemd-run --user` (no sudo/root; the
transient unit is `--collect`ed on exit, so `remove`'s kill-by-pid leaves no
cruft — verified). On **non-systemd hosts** (the old-OS portability floor)
`systemd-run` is absent and a plain `setsid`-detached process survives normally,
so `launch_command` falls back to that (`command -v systemd-run` decides).

The sentinel is the existing v1 wire format
(`otto-link:v1:<id>:<proto>:<a-host>:<a-if>:<a-port>:<b-host>:<b-if>:<b-port>`)
— **no format/version change**: the id value now carries the `-<port>` suffix,
but the segment layout is unchanged.

### 7.5 Conflict rule & preconditions

- Resolve both endpoints (+ `--dest`), compute the id, and refuse if it already
  exists in `all_links` (implicit ∪ declared ∪ dynamic). Because ids exclude
  raw ports but include the port suffix, a second tunnel on the same
  route+proto+**port** is a conflict; different ports coexist.
- Missing `socat` (or `bash`) on a target host → **fail loud, name the host**;
  no auto-install.
- **`add`'s runtime contract:** it resolves + conflict-checks, spawns the tagged
  processes, and reports which started. It does **not** pre-validate L2
  reachability or guarantee end-to-end delivery — a broken path surfaces as a
  tunnel that carries no traffic (caught by the e2e test), not an `add` error.
  `add` errors only on resolution/conflict failure or a failed spawn.

## 8. Discovery (`discover_dynamic_links`) — monitor-compatible two-layer

**(a) Per-host observation** — a `(command, pure parser)` pair, the unit a
monitor `MetricParser` needs:

- **Command:** portable `ps -eo pid=,etime=,args=` filtered to `otto-link:`
  lines. `etime` (formatted `[[DD-]HH:]MM:SS`) is used, **not** `etimes`
  (procps ≥3.3, too new for RHEL6/2.6.32); `pgrep -a` is avoided for the same
  reason. One command per host yields pid + age + argv together.
- **Parser:** pure function `ps output → list[Observation]`, where
  `Observation = (pid, age_seconds, Link)` (the `Link` via the existing
  `parse_sentinel`). Non-otto lines ignored — never misattribute a stranger's
  socat.

**(b) Lab-level aggregation** — a pure function grouping observations by id into
`Link`s, filling endpoint ips from lab addressing
(`addressing_from_dict`/`HostAddressing`), and recording each id's **origin
hosts** (the hosts a tagged process was found on). An origin host that is not a
logical endpoint is the tunnel's **exit relay** (surfaced as "via B" in `list`).

`discover_dynamic_links(lab)` = `asyncio.gather` (a) across tunnel-hosting hosts
→ (b), returning `list[Link]` (the frozen contract). "Tunnel-hosting" is a
**`has_bash` capability filter** (`getattr(host, "has_bash", False)`), not a
nominal `isinstance` type check: any host — Unix, built-in `local`, Docker
container — that declares (or defaults to) a working `bash` is scanned; a host
with no `has_bash` attribute at all is treated as `False`. Unix-family hosts
default `has_bash = True`; embedded hosts default `False` and are skipped
unless a lab overrides it. `has_bash` is a cheap, declared pre-filter over
*which* hosts discovery bothers to scan; `add`'s precondition check (§7.5)
separately live-probes (`command -v bash`/`socat`) its two endpoint hosts at
construction time — the declaration and the live probe are expected to
agree, but only the probe is authoritative for `add`. Internal callers (`list`,
`remove`, the completion warmer) use the richer per-host observations (pids
for kill, age for display, origin host for relay path). The pure per-host
parser stays in `otto.link`; when #5/#6 wire the monitor, a thin `MetricParser`
adapter wraps it with the command string, interval, and table metadata — no
change to `otto.link`.

## 9. `all_links`, list, remove

### 9.1 `all_links` simplification

Dynamic ids (`lnk-<hex>-<port>`) and static ids (`name` / `a--b`) are **disjoint
id-spaces**, so a live tunnel never collides with the declared route it realizes
— they **coexist** as separate rows sharing no id (the `lnk-<hex>` prefix still
signals the shared route visually). This retires the foundation's
"dynamic-wins-*replaces*-declared, then field-enrich to recover name/ips"
problem: `all_links` is a plain union; its merge-by-id only dedups a genuine
same-id duplicate.

### 9.2 `otto link list`

- **Default: dynamic only.** Columns: `id` · `endpoints`
  (`host[@iface] ↔ host[@iface]`) · `protocol` · `port` · `age` · `via <exit>`
  (only when dest ≠ exit).
- **`--all`:** folds in implicit + declared; adds a `provenance` column. Dead
  tunnels simply stop appearing — nothing to prune.
- Library form: thin printer over `discover_dynamic_links` / `all_links`.

> **Shipped reality (2026-07-09, final fix wave):** the CLI only renders `id` ·
> `endpoints` · `protocol` today; the `port` / `age` / `via <exit>` columns are
> **deferred** — `Observation.age_seconds` and the origin-host (relay) data
> exist internally (`discover_observations`), they just aren't printed yet.
> Tracked in `todo/link.md`. What *did* ship this wave: a discovery-time
> partial-scan warning — when a host couldn't be reached, `list` prints
> `partial scan — could not reach: <hosts>` after the rows, fed by the new
> `discover_dynamic_links_status` helper (spec §10).

### 9.3 `otto link remove`

- **`remove <id>`:** discover the id's tagged processes across hosts, `kill`
  their pids on each origin host, verify gone. The `-<port>` suffix targets one
  tunnel unambiguously.
- **`remove --all`:** reaps every otto tunnel (owner-agnostic by design) behind
  a confirmation prompt; `-y/--yes` skips it.
- Returns a `RemovedReport` (ids/pids/hosts killed, per-host failures).

> **Shipped reality (2026-07-09, final fix wave):** "verify gone" is **not
> implemented** — `remove` issues `kill` and trusts a `Status.Success`
> `CommandResult` (a zero shell exit) as done; it does not re-scan afterward
> to confirm the process actually exited. What the kill step *does* now
> correctly do: check the `kill` command's own exit status (`.is_ok`), not
> just whether `oneshot` raised — a non-zero `kill` (e.g. "no such process")
> lands that host in `RemovedReport.unreachable` rather than being
> misreported as killed. A post-remove verify pass is tracked as a backlog
> item in `todo/link.md`.

## 10. Host-down behavior — best-effort + transparent

One symmetric rule: **attempt everything reachable, be transparent about every
failure.**

- **`list`/discovery:** show tunnels found on reachable hosts; emit a prominent
  warning naming each unreachable host; mark the listing partial. Never silently
  drop a host.
- **`remove`:** kill the reachable ends, name the hosts it couldn't reach, and
  **exit non-zero** so the incompleteness reaches scripts too. Never claim
  success while a socat may linger.

## 11. Logging & completion cache

### 11.1 Logging

`otto link` commands run internal host I/O (`ps`, socat spawn, `kill`) at
`LogMode.QUIET`/`NEVER` so remote command output never reaches the console.
Only **warnings and errors** are logged. **No per-command log file / output
dir** — these commands are short-lived. This is exactly what the three-sink
`LogMode` model gates (command I/O only; warnings/errors always pass).

### 11.2 `remove <id>` completion store

The existing completion cache
(`$OTTO_XDIR/.otto/completion_cache.json`) keys its main entries by a
**config-file fingerprint** — perfect for config-derived names, wrong for live
tunnel ids (no config mtime changes when a tunnel appears/dies). The
`__collected_tests__` reserved key is the precedent for a live/volatile data set
with its own freshness contract. So:

- **A new reserved key `__dynamic_links__`** in the same file, scoped to the
  discovery context (lab), with its own `schema_version`, `generated_at`, and a
  **short TTL (~2 min)** (link state is volatile). Disjoint key → never clobbers
  fingerprint entries.
- **Warm on first cold TAB:** a bounded, best-effort discovery (short timeout +
  cooldown after failure), mirroring `maybe_warm_collected_tests`. First TAB
  slow; instant for the TTL window.
- **Refresh as a side effect of `add`/`remove`:** those commands already scan
  live, so they write the fresh id set on their way out — the user's own
  mutations keep it current for free; the TTL catches out-of-band changes.

  > **Shipped reality (2026-07-09, final fix wave):** only **`list`** (the
  > default, dynamic-only form) actually warms `__dynamic_links__` — it
  > writes the fresh id set after every discovery. **`remove`** empties the
  > cache (`record_dynamic_link_ids(repos, [])`) rather than refreshing it
  > from its own reap, so completion goes cold immediately after a removal
  > until the next `list`. **`add` does not touch the cache at all.**
  > **"Warm on first cold TAB" is not implemented** — `_link_id_completer`
  > only reads the cache (`read_dynamic_link_ids`); there is no
  > `maybe_warm_collected_tests`-style bounded background discovery on a cold
  > TAB. Tracked as a backlog item in `todo/link.md`.
- Worst case: completing a just-vanished id → `remove` cleanly reports "nothing
  found."

### 11.3 `--hosts` completion

- **Committed:** `complete_comma_list(all_host_ids, incomplete)` — the exact
  helper `--lab`/`--tests` use (keeps the typed prefix, filters the in-progress
  segment, drops already-chosen hosts).
- **Stretch (non-gating):** context-aware — narrow candidates to hosts
  **L2-reachable from the last entered host** before the same helper. Reachability
  uses a simple shared-subnet (/24) heuristic until per-interface subnets exist;
  falls back to the committed all-hosts behavior if it gets fiddly.

## 12. Monitor compatibility (design-for, defer wiring)

The per-host discovery command + pure parser (§8a) map 1:1 onto the monitor's
`MetricParser` contract (command / parse / interval, a natural `kind="table"`
parser), and the lab-level aggregator (§8b) is the edge/topology layer #5/#6 own.
This phase builds that structure and keeps `otto.link` monitor-free; the actual
collection-interval wiring, edge storage, and topology views are #5/#6.

## 13. Old-OS portability (down to 2.6.32)

Concrete choices already folded above: portable `ps -eo pid=,etime=,args=` (no
`etimes`, no `pgrep -a`); `bash -c 'exec -a'` tagging with a documented bash
requirement; old-stable socat addresses. **Marker robustness** (does
`exec -a`-set argv survive `ps` on old procps; does detachment persist) is the
key thing to validate on real old userland.

**Testing follow-on (Chris to provision — no VM/Vagrantfile changes here):**

- **Recommended: a CentOS 6 docker container** (`centos:6`) — runs on both host
  arches via Docker's transparent qemu/binfmt (Chris's host is arm64; other devs
  x86_64), spins up in seconds, and doubles as an exercise of docker in tunnels.
  Kernel 2.6.32, procps 3.2.8 (`etime`, no `etimes`), bash 4.1 (`exec -a`),
  `/bin/sh`→bash, socat via EPEL 6.
- **Alternative: a CentOS 6 x86_64 VM** with the provider branched by host arch
  (`virtualbox` on x86_64, `qemu` full-emulation on arm64) — faithful to the
  peer-VM L2 topology but slow under emulation. Genuinely-old userland is
  x86_64-only (arm64 didn't exist in that era), so arm64 hosts must emulate
  either way.
- **Note:** a #6 forward-note (in `todo/link.md`) records that the GUI topology
  should render container hosts visually distinct and nested inside their parent.

## 14. Testing strategy

**Hostless units (bulk of correctness, CI):**

- socat argv builders — ingress + egress exact argv, loopback-dest vs relay-dest,
  udp vs tcp, the `bash -c 'exec -a'` wrapper.
- sentinel round-trips (exist) — plus the `-<port>` suffix id and its parse.
- discovery parser on canned `ps` text — grouping by id, `etime`→seconds,
  origin-host capture, **excluding a stranger's socat**, ip resolution.
- endpoint + `--dest` resolution (reuse `_resolve_endpoint`), the multi-hop
  rejection, and the conflict check.
- `remove` pid-extraction; `all_links` coexistence; both completers (comma-list +
  L2-reachable filter); the `__dynamic_links__` cache warm/refresh/TTL logic.

**Live bed (`e2e`/`hops` markers, real peer Unix VMs with socat):**

- `add` a UDP tunnel test1→test2 `--port P` → send a datagram to `P` on test1 →
  assert receipt on test2 → `list` shows it → `remove` → assert gone.
- **Relay:** `add` test1→test2 `--dest test3` → assert test3 receives with
  **source = test2's ip**.
- Out-of-band-kill a tunnel process → `list` reflects reality.
- Spawn a non-otto socat → assert excluded from `list`.
- A guaranteed-teardown fixture reaps test tunnels even on failure.
- **Old-userland check** (via the CentOS 6 container follow-on): the `ps`/`bash
  exec -a`/`socat` invocations behave and tunnels are discoverable.
- Honor dev-VM rules: no heavy parallel load, never power VMs, fail-loud on
  host-down.

## 15. Foundation revisions this phase makes

- **Provenance-aware `Link.id`** (§6): static links get readable handles instead
  of `lnk-<hex>`; dynamic links get `lnk-<hex>-<port>`. `make_link_id`'s
  route-hash algorithm is unchanged, but its consumers and docstring change.
- **`discover_dynamic_links`** goes from stub to the two-layer live
  implementation; its `list[Link]` signature is preserved.
- **`all_links`** loses the (never-built) enrichment requirement — disjoint id
  spaces make dynamic/declared coexist naturally.

Both are safe to change now: no otto users, no persisted static ids, no live
tunnels predating this phase.
