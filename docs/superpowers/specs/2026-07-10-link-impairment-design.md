# Link Impairment (#3) — Design

Sub-project #3 of the link stack (after #1 foundation, #2/#2b tunnels).
Impair traffic on lab links with NetEm — delay, jitter, loss, rate, corrupt,
duplicate, reorder — from the CLI, safely, on real beds.

## 1. Taxonomy: links vs tunnels (decided 2026-07-10)

- **Links are the static underlay** — connections between two
  `(host, interface)` endpoints: the `Link` model (`otto.link`), declared in
  `lab.json`'s `links` or implicit from `hop` chains. Links are what impair
  (#3) and capture (#4) target, because interfaces are where `tc qdisc … dev`
  and tcpdump attach.
- **Tunnels are the dynamic overlay** — paths otto lays *over* existing
  connectivity (`otto tunnel`, #2b). A tunnel is not composed of links; its
  hop-to-hop carrier traffic *rides* them. Tunnels are never impaired
  directly — impairing a link a tunnel traverses affects it realistically,
  for free.
- No `TunnelSegment` type (YAGNI — nothing needs to name a hop-to-hop
  stretch; the overlay/underlay split answers "tunnels are made of links").
- The `otto link` CLI group (vacant since the #2b rename) returns as the
  **static-topology surface**: `impair`, `repair`, `list`.

## 2. Scope

**In:** `otto link impair` / `repair` / `list`; endpoint-anchored netem;
in-path (middlebox) netem for links that declare an impairment host;
`LinkImpairer` registry with NetEm first-party; opt-in `--expire`;
mandatory mgmt-interface refusal; lab.json `impair` field (graduates from
reserved); live qdisc discovery.

**Out (dropped, not deferred — decided 2026-07-10):** route diversion.
Product code never manipulates routing to force traffic through a
non-in-path host. A middlebox is topology; topology lives in lab.json. The
e2e *fixture* may scaffold temporary routes on the bed (test-local, torn
down) — see §12. The ad-hoc `--via <host>` option is dropped with it.

**Out (backlog):** impairing container endpoints via the parent's veth
(v1 = plain attempt, fail-loud on missing `NET_ADMIN`/`tc`); non-Linux
impairers (the registry seam exists; nothing ships); middlebox chains;
an escape-hatch flag to force endpoint mode on a link that declares an
impair host; flow-scoped impairment when two links share a middlebox
interface (classful tc + per-destination filters — v1 granularity is the
netdev, see §4).

## 3. CLI surface

```
otto link impair <link> [--delay D] [--jitter J] [--loss P] [--rate R]
                        [--corrupt P] [--duplicate P] [--reorder P]
                        [--from <endpoint-host>] [--expire <seconds>]
otto link repair (<link> | --all)
otto link list
```

`<link>` accepts a link id (`lnk-…`) or its `name`; shell completion offers
the loaded lab's links (static, sync — no live scan needed).

### 3.1 Parameters and units (decided 2026-07-10)

| Params | Bare number means | Accepted forms |
|---|---|---|
| `--delay`, `--jitter` | **milliseconds** (`--delay 50` = 50 ms) | `us`, `ms`, `s` suffixes |
| `--loss`, `--corrupt`, `--duplicate`, `--reorder` | **percent** (`--loss 2` = 2%) | optional `%` suffix |
| `--rate` | **usage error** — no natural default for bandwidth | explicit tc unit required: `kbit`, `mbit`, `gbit`, … |

- Values are typed and validated at CLI parse time (pydantic); the error for
  a bare `--rate` names the accepted units.
- **otto always emits explicit units in the tc argv** — never relies on tc's
  bare-number semantics, which vary by parameter and iproute2 version (the
  #2b old-OS lesson: procps/systemd-run class of landmine).
- netem coupling rules enforced at parse/merge time: `--jitter` and
  `--reorder` require a delay (given now or already present after merge).

### 3.2 Direction (decided 2026-07-10)

Default = **both directions**: netem applied at both resolved placements
(one per direction). `--from <endpoint-host>` restricts to the single
direction *originating* at that endpoint (A→B = egress at A's placement).
`--from` always names an **endpoint** host — never the middlebox — in both
placement modes. Docs call out the RTT math: `--delay 50` both ways
= 100 ms RTT.

### 3.3 Re-impair: merge, per-param last-one-wins (decided 2026-07-10)

`impair` on an already-impaired link/direction **merges**: existing netem
params persist unless the new invocation overrides them (`--delay 20` then
`--loss 2 --delay 10` → loss 2%, delay 10 ms). Setting a param to its zero
value (`--loss 0`, `--delay 0`) clears just that param. `repair` clears
everything. Mechanically: read current params off the placement
(§7 parser), overlay, apply via `tc qdisc replace`.

## 4. Placement model

A **placement** = `(host, netdev, direction)` — where one direction's netem
qdisc lands. Netem-on-a-placement is the single primitive; resolvers map
`(link, direction)` → placement:

- **Endpoint mode (default):** A→B lands on A's endpoint interface, B→A on
  B's. Requires a *named* endpoint interface (`LinkEndpoint.interface` set —
  an endpoint that falls back to the mgmt `ip`/sole interface is not
  impairable; scratchpad rule, and it collides with §9 refusal anyway).
- **In-path mode:** if the link declares an `impair` host (§10), both
  directions land on that middlebox M — A→B on M's interface *facing B*,
  B→A on M's interface *facing A*. Facing interfaces are **auto-resolved
  at impair time** by matching each endpoint's IP against M's live
  interface subnets (`ip -o addr show` — live because `InterfaceSpec.ip`
  carries no prefix; and in the bridged-middlebox topology this resolution
  succeeds by construction). **Resolution failure = clear, fail-loud
  error** ("wanem has no interface on carrot's subnet") — this doubles as
  the in-pathness check. No declared override in v1 (decided 2026-07-10:
  a `facing` override was designed then dropped — it only serves routed /
  overlapping-subnet middlebox topologies nobody has; the fail-loud error
  will reveal the needed shape if one ever appears).

**Granularity is the netdev.** A middlebox servicing multiple links does so
implicitly — each link entry names it (`"impair": "wanem"`), and facing
resolves per link. But netem attaches to an interface, not a flow: if two
links' placements resolve onto the *same* M interface (two endpoints
sharing a segment), impairing one impairs both — they share the qdisc, a
second impair merges over the first (§3.3), and `list` truthfully reports
both links impaired. Flow-scoped impairment on a shared interface
(classful tc + per-destination filters) is backlog (§2). In the canonical
one-interface-per-segment middlebox layout, placements never collide.

## 5. `LinkImpairer` registry & selection (threaded through, decided 2026-07-10)

The scratchpad-decided pluggability, mirroring the transfer-backend pattern
**exactly** — and exercised end-to-end in v1, not just seamed:

- **`LinkImpairer` base**: declares `host_families` (classvar, like
  `BaseFileTransfer`) and builds the apply/read/clear command argvs for a
  placement + param set; uniform `create(cls, ctx)` construction (WS#4).
- **`IMPAIRERS: Registry[type[LinkImpairer]]`** +
  `register_impairer(name, cls, *, overwrite=False)` /
  `build_impairer(name)`. Registration rejects an empty `host_families`
  (could never validate against any host) and records
  `origin = caller_module()` (plays with `_isolate_registries`). Custom
  impairers register from init modules, like custom transfer backends.
- First-party registrant: **`"netem"` only**, `host_families = {"unix"}`.

**Selection interface — the host-level `impairer` pin** (the transfer/term
analog; deliberately NOT named `impair`, which is the link-level middlebox
pointer, §10):

- Optional `impairer: str | None` field on the unix host spec, validated at
  model level like `transfer`: must be a registered impairer name AND
  family-applicable to the host (unknown name → error listing registered
  names; family mismatch → error naming the families it serves). Mirrored
  runtime host field (models/host.py drift guard applies).
- Resolution per **placement host** at impair time: explicit host pin →
  `[host_preferences]` selector → family default (`netem` for unix). Each
  placement resolves independently — in endpoint mode the two endpoints
  may legitimately resolve different impairers.
- No per-invocation CLI override (an impairer is a property of the host's
  tooling, not of one command; YAGNI until a real need).

## 6. State & discovery

**The kernel qdisc config is the only state** — no ledger, same philosophy
as tunnel discovery. `list` and merge both read `tc qdisc show dev <netdev>`
through one unit-testable parser (canned outputs incl. centos:7-era
iproute2 format). Consequences, by design:

- A netem qdisc someone set by hand shows as impaired; `repair` clears it.
- Nothing goes stale: a placement cleared out-of-band simply stops
  appearing.

`otto link list` columns: `id · a ⇄ b (host:iface) · impair-host · status`,
status per direction: `ok` / compact param summary (`a→b delay 50ms loss 2%`)
/ `?` when the placement host is unreachable (listing never fails the whole
table on one dead host; impair/repair on it DO fail loud).

## 7. Expiry (`--expire`, decided 2026-07-06/2026-07-10)

Opt-in self-healing; **default indefinite** (long tests need persistence).
Mechanism: a detached, sentinel-tagged timer process **on the placement
host** (`sleep N` then clear the qdisc), launched through the tunnel-proven
`launch_command` machinery (`systemd-run --user` → `setsid` fallback, argv
sentinel `otto-impair:v1:<link-id>:<netdev>`, percent-encoded segments) so
it survives otto exiting, is discoverable, and is unambiguously ours.
Rules: every `impair`/`repair` first cancels existing timers for the link's
placements; a fresh timer is installed only when `--expire` is given (so a
later indefinite impair doesn't get wiped by a stale timer).

## 8. Elevation

`tc qdisc` needs root: impair/repair (and the expiry timer's clear) run
through the host's elevation mechanism (per-session elevation, Spec A).
A placement host without elevation → fail-loud, host-named error.

## 9. Safety: refusal rules (mandatory)

Both rules guard the same failure class — severing otto's own path — and
apply regardless of `--expire`. Load-bearing on a shared bed; tested in
unit AND e2e.

- **Mgmt-interface refusal:** impairing the device otto reaches a host
  through = self-lockout. Applied to **every resolved placement** (endpoint
  or middlebox): resolve which netdev carries the placement host's
  management `ip` (live `ip -o addr` match) and refuse if it's the
  placement netdev. Covers the middlebox case where M's facing interface
  is also its mgmt interface.
- **Local-host refusal (decided 2026-07-10):** a link with the **local
  host as either endpoint** is never impairable, in ANY placement mode —
  the local host's connectivity to the bed IS otto's management path, so
  impairing such a link (even at a middlebox) degrades otto itself. This
  is a link-level check (endpoint host is the builtin `local` /
  `LocalHost`), evaluated before placement resolution.

## 10. lab.json `impair` field (graduates from reserved)

```json
"links": [{
  "endpoints": [{"host": "carrot", "interface": "eth1"},
                {"host": "tomato", "interface": "eth1"}],
  "impair": "wanem"
}]
```

- `impair` = the in-path middlebox's **host id**, a bare string; validated
  as a known reference like endpoint hosts. That is the field's entire
  job: associating the link with the management host that services its
  impairment. Everything directional (which M netdev faces which
  endpoint) is derived live from the endpoints (§4) — no `facing`
  declaration (designed, then dropped 2026-07-10).
- Shorthand-first like interface entries (`"eth0": "10.0.0.5"` coerces to
  an object): if a second knob ever lands (e.g. impairer selection when a
  second impairer exists), the field grows an object form
  (`{"host": ..., ...}`) accepting the string as shorthand — no schema
  break.
- Impairer selection is NOT this field's job: the link-level `impair`
  says *where* (the middlebox); the host-level `impairer` pin (§5) says
  *with what*. Keeping them separate keys avoids overloading one name
  with two meanings.
- Schema ripple: `lab.json` object-schema export + docs pages updated (the
  #1 machinery).

## 11. Module layout & CLI wiring

- `src/otto/link/` grows: `impair.py` (placement resolution + orchestration:
  merge/apply/clear/verify), `netem.py` (the NetEm `LinkImpairer`: argv
  builders + qdisc-show parser), registry plumbing in `__init__`.
- `src/otto/cli/link.py`: new `link_app` (impair/repair/list) registered as
  the `otto link` group; usage-exits hoisted OUT of try blocks
  (typer.Exit-subclasses-RuntimeError lesson).
- Completion: static links from the loaded lab (sync path — no live scan).

## 12. Testing strategy

- **Hostless unit (bulk of correctness):** typed param parsing/units
  (incl. bare-`--rate` rejection, jitter/reorder-need-delay), netem argv
  builders (assert exact argv, run nothing), qdisc-show parser (canned
  modern + centos:7-era outputs), merge logic (last-one-wins, zero-clears),
  placement resolvers (endpoint; in-path facing auto-resolve from canned
  `ip -o addr` output, incl. the unresolvable fail-loud case), mgmt
  refusal, **local-host link refusal (both placement modes)**,
  `impair`-field validation (unknown host reference), and the **registry
  round-trip**: register a fake `LinkImpairer` (test-local, exercising
  `_isolate_registries`' module-eviction path), pin a host's `impairer` to
  it, run the impair orchestration against fakes, and assert the FAKE's
  argvs are what executes — registration → validation → per-placement
  selection → build → orchestration, end to end. Plus selection-validation
  errors: unknown `impairer` name, family-inapplicable impairer.
- **Live-bed e2e** (real peers, single-pass, fail-loud on host-down):
  1. Endpoint mode: netem delay/loss on a data interface → measured
     RTT/loss delta → `repair` → delta gone.
  2. In-path mode: **fixture-scaffolded** routed topology through pepper
     (fixture adds temporary /32 routes on carrot/tomato + `ip_forward` +
     `send_redirects=0` on pepper, guaranteed teardown) → otto impairs the
     link (placements resolve onto pepper) → delta measured → repair.
     Product code never touches routes; the fixture is the scaffolding,
     exactly like #2b's forced multi-hop.
  3. `--expire` fires → placement self-heals; impair-without-expire cancels
     a prior timer.
  4. `list` reflects an out-of-band `tc qdisc del`.
  - Bed rules: never impair the mgmt interface (the refusal test asserts
    the error, not the lockout); no parallel load; never skip on host-down.

## 13. Decisions log

- 2026-07-10 — taxonomy: link = static underlay, tunnel = dynamic overlay;
  impair/capture target links; no `TunnelSegment`; `otto link` group returns.
- 2026-07-10 — direction default **both**, `--from <endpoint>` for one-way.
- 2026-07-10 — CLI surface = `impair` + `repair` + `list`.
- 2026-07-10 — re-impair **merges**, per-param last-one-wins; zero clears
  a param; `repair` clears all.
- 2026-07-10 — units: bare time = ms, bare percent = %, `--rate` requires
  an explicit unit; otto always emits explicit units to tc.
- 2026-07-10 — in-path (middlebox) placement is **in #3** (Chris's labs
  wire the NetEm VM as a bridge/router — pure placement, no routing work);
  selection is the lab.json `impair` field ONLY (`--via` dropped).
- 2026-07-10 — **route diversion dropped entirely** (not deferred); e2e
  fixture may scaffold routes on the bed as test-local setup.
- 2026-07-10 — **impairer selection threaded through in v1**: host-level
  `impairer` pin (transfer/term analog, validated at load), per-placement
  resolution (pin → preferences → family default `netem`), and a
  fake-impairer registry round-trip unit test proving the mechanism works
  when set — not just a dormant seam.
- 2026-07-10 — **local-host refusal**: a link with the local host as an
  endpoint is never impairable, in any placement mode (same self-lockout
  class as mgmt-interface refusal).
- 2026-07-10 — **no `facing` declaration** (designed as a map, revised to
  a pair-list, then dropped entirely): `impair` is a bare middlebox host-id
  string; facing interfaces are always derived live from the endpoints'
  IPs vs M's interface subnets, fail-loud when unresolvable. An override
  only serves routed/overlapping-subnet middlebox topologies nobody has.
- 2026-07-06 — `--expire` opt-in, default indefinite; mgmt-interface
  refusal mandatory (carried from the scratchpad, reaffirmed).
