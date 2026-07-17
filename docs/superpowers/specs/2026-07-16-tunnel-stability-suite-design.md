# Tunnel stability suite (`make stability-tunnel`)

**Date:** 2026-07-16
**Status:** approved design, awaiting implementation plan
**Fills the gap:** the stability suite (`make stability`, three tiers) has zero
tunnel coverage, and tunnel testing today is single-pass only:
`tests/e2e/test_tunnel_e2e.py` proves each behavior once (direct, multi-hop,
container endpoint, one degraded case, one CLI cycle) but nothing exercises the
tunnel machinery under repetition, concurrency, adversity, or host loss. The
health surface is likewise proven only at its happy edges: `discover_tunnels`'s
`ok`/`degraded`/`uncertain` tri-state and the `unreachable` reporting on scan
and remove have no test where a host is genuinely unreachable through a real
transport, and the monitor's tunnel loop (`Collector._tunnel_loop`, landed
`fd51384`) has no stability coverage for its last-known-state contract.

## Decisions (from brainstorming, 2026-07-16)

1. **Scope: both health surfaces.** Scan-time health (`discover_tunnels`,
   add/remove reporting) AND the monitor's continuous `_tunnel_loop`. The
   dynamic-tunnels seam landed on main (`fd51384`, health tri-state unified in
   `038c057`) mid-brainstorm, so the monitor phase is implementable now — same
   seam serves CLI and monitor.
2. **Host-down is simulated without touching VM power.** Two shapes: a
   *phantom host* (real `UnixHost` at an unused bed-subnet ip — unreachable
   from the start, real connect timeout through the real transport) and a
   *SIGSTOP wedge* (freeze the peer's sshd **listener pid only** — was up,
   went down mid-life, then recovers). No `vagrant halt`, no iptables
   partition: power-cycling is minutes per cycle and interferes with every
   other bed user; a partition rule that fails teardown wedges the bed.
   Recovery-after-reboot testing is explicitly out of scope.
3. **All eight proposed soak modes are in** (concurrent population, racing
   conflicting adds, discovery under churn, remove-all sweep cycling, traffic
   soak + neighbor churn, churn over an impaired link, repeated
   degrade/recover, resource watermarks) plus the baseline 2-hop/3-hop
   add/remove churn.
4. **A dedicated Makefile tier**, `make stability-tunnel`, default
   `COUNT=1` (like `stability-embedded`): the tests loop internally via one
   env knob instead of relying on pytest-repeat multiplication. `stability-unix`
   must not sweep these in.
5. **Architecture: shared soak harness + one focused module per mode family**
   (approach A). Bed hygiene helpers are extracted from
   `tests/e2e/test_tunnel_e2e.py` into a shared home so both suites use one
   source of truth; modes stay hand-written tests with hand-aimed assertions
   (no data-driven scenario runner — worse live-bed diagnostics for little
   gain).
6. **Bed change: the 192.168.1.x data plane moves to a dedicated eth2 NIC**
   (VirtualBox internal network `otto-dataplane`; Vagrantfile + tech1
   `lab.json` updated 2026-07-16, same-day follow-up to the stacked-on-eth1
   provisioning). Rationale: `otto link impair` refuses any placement on the
   netdev carrying a host's management ip (the self-lockout guard —
   correct, and per-netdev because tc qdiscs are device-scoped), so a data
   plane stacked on the mgmt netdev could never be impaired. On its own
   netdev, impairing it is legal — the adversity mode rides the real data
   plane instead of building VLAN scaffolding. Requires a `vagrant reload`
   of test1/2/3 (+ zephyr) — a NIC attach, not just re-provisioning.
7. **Recovery mechanisms are part of the contract** (added 2026-07-17,
   mid-execution): finding and hardening failure modes is not enough — the
   suite must also prove that (a) a misbehaving tunnel is **plainly visible
   to the user** through the real `otto tunnel list` CLI (degraded renders
   `degraded (n/m)`, an unreachable chain host renders the `?` uncertainty
   suffix — asserted against real CLI output on the live bed, not just
   library state; monitor-side visibility is the collector-record
   assertions in §5, the web rendering being covered by the merged
   dynamic-tunnels tests), and (b) **removal of a misbehaving tunnel is
   supported whenever hosts are reachable**: a degraded tunnel removes to
   zero residue (adversity mode), and a remove attempted while a chain
   host is wedged completes partially with the unreachable host **named**
   in `RemovedReport.unreachable` — never silently — then a post-recovery
   remove completes the reap to zero residue (SIGSTOP mode).

## 1. Suite layout & shared harness

```text
tests/_fixtures/tunnel_bed.py         ← extracted from test_tunnel_e2e.py, no
                                        behavior change: _build_host,
                                        _assert_reachable, lab construction,
                                        reap tracking, the leftover sweep
                                        (strict sentinel-parser based), UDP
                                        send/listen/bind-confirm helpers
tests/e2e/tunnel_stability/
  __init__.py
  conftest.py        — tunnel_lab / reap_tunnels fixtures, module-final leak
                       sweep, watermark fixture, CYCLES knob
  _harness.py        — churn driver, sequence-numbered traffic generator,
                       port-scoped impairment context manager, phantom host
                       builder, SIGSTOP wedge helper
  test_churn.py      — baseline 2/3-hop churn, alternating shape, remove-all
  test_concurrency.py — concurrent population, racing adds, discovery under churn
  test_traffic.py    — survivor traffic soak during neighbor churn
  test_adversity.py  — churn under impairment, repeated degrade/recover
  test_health.py     — phantom-ip and SIGSTOP health detection
  test_monitor_loop.py — Collector tunnel loop over the live bed
tests/unit/monitor/test_collector_tunnel_soak.py — no-VM tick soak (§5)
```

`tests/e2e/test_tunnel_e2e.py` switches to importing from `tunnel_bed.py`;
its tests are otherwise untouched.

**Conftest guarantees (always on, every module):**

- *Reap + sweep.* Every created tunnel id is tracked in `reap_tunnels` and
  individually reaped in fixture teardown even when the test body raises. A
  module-final sweep runs `DISCOVERY_PS_COMMAND` on all three peers, decodes
  through `parse_process_discovery` (the strict sentinel parser — see the
  self-match trap documented in `test_tunnel_e2e.py`), and **fails** on any
  survivor. Never skips.
- *Watermark fixture (autouse, outermost).* Brackets each test outside the lab
  fixture: captures the local process's open-FD count (`/proc/self/fd`) before
  the lab is built and after every host is closed, and fails on growth beyond
  a small fixed tolerance, with one `gc.collect()`-and-retry to absorb
  collector timing. Tagged-process cleanliness on the peers is the sweep's job
  (exactly zero, strict); the FD bracket is the local-side leak guard — the
  timed-out-connect paths in `test_health.py` are exactly where transport FD
  leaks hide.

**Knobs and ports.** Internal loop counts read `OTTO_TUNNEL_SOAK_CYCLES`
(default 5); the Makefile target forwards `CYCLES=N`. Per-test timeout
ceilings are computed from the knob at collection time
(`pytest.mark.timeout(base + per_cycle * CYCLES)`) and sized generously — the
live bed is never killed at a tight timeout. Service ports come from a
dedicated block disjoint from the existing e2e's 15000–15004:

| module           | ports        |
|------------------|--------------|
| test_churn       | 15100–15109  |
| test_concurrency | 15110–15129  |
| test_traffic     | 15130–15139  |
| test_adversity   | 15140–15149  |
| test_health      | 15150–15159  |
| test_monitor_loop| 15160–15169  |

## 2. Markers & selection algebra

Every module in the package carries:

```python
pytestmark = [
    pytest.mark.stability,     # out of `make coverage`
    pytest.mark.integration,   # resource: requires the lab VMs
    pytest.mark.hops,          # resource: requires all 3 Vagrant VMs
    pytest.mark.xdist_group("link_tunnels_e2e"),  # same group as test_tunnel_e2e —
                                                  # never concurrent on the bed
]
```

No existing test carries `stability` **and** `hops` together (verified
2026-07-16), so the selection change is surgical:

- `stability-unix` becomes `-m "stability and integration and not embedded
  and not hops"` — selects exactly what it selects today.
- `stability-tunnel` selects `-m "stability and hops"`.
- `make coverage` is untouched (`not stability` already excludes everything
  here). The unit tick soak (§5) carries `concurrency` instead — fast, no-VM,
  stays in coverage, rides `stability-unit` ×50.

## 3. Soak modes

All live-bed modes drive the library API (`add_tunnel` / `discover_tunnels` /
`remove_tunnel` / `remove_all_tunnels`) over the veggies bed
(carrot/tomato/pepper = test1/2/3), management-ip resolution, exactly like the
existing library-API e2e tests — with one exception: the impaired-churn mode
rides the eth2 data plane (decision 6), since that is the netdev being
impaired. `CYCLES` = the env knob.

### test_churn.py

- **Direct churn.** `CYCLES`× { add carrot↔tomato (udp) → discovery reports
  `ok` with 4 processes → remove → `survivors == []` → discovery no longer
  lists it }. UDP delivery probed on the **first and last** cycle only
  (every-cycle probing roughly doubles wall-clock for little signal;
  process-level verification runs every cycle).
- **Multi-hop churn.** Same at carrot→tomato→pepper: `ok` with 6 processes,
  relay pair on tomato.
- **Alternating shape.** 2-hop and 3-hop alternately **on the same service
  port** — each remove must genuinely free the port for a differently-shaped
  successor (catches lingering binds and half-reaped residue that a same-shape
  re-add could mask).
- **Remove-all sweep.** Build 3 mixed-shape tunnels → `remove_all_tunnels` →
  `survivors == []` and empty discovery → repeat `CYCLES`×. The
  owner-agnostic reap path under repetition.

### test_concurrency.py

- **Concurrent population.** A standing population of 3 tunnels on distinct
  ports; each cycle retires the oldest and adds a fresh one. After every
  mutation, discovery must report exactly the live set — no ghosts, no
  stragglers.
- **Racing conflicting adds.** `asyncio.gather` of two `add_tunnel` calls for
  the same port + endpoints. Asserted contract: **exactly one succeeds, the
  loser raises, and after both settle (plus cleanup of the winner) zero tagged
  processes remain.** Known risk, accepted during brainstorming:
  `_check_conflicts` scans before launch, so two in-flight adds have a TOCTOU
  window and this test may expose a real product race. If it does, the product
  fix is **in scope for this work stream** (post-add duplicate-id detection →
  reap the younger add and fail it loud), with its own unit tests. The
  stability test asserts the intended contract, not current behavior.
- **Discovery under churn.** One task runs add/remove cycles while a second
  polls `discover_tunnels` continuously. Every snapshot must be internally
  consistent: statuses only from {`ok`, `degraded (…)`, `uncertain`}, and a
  tunnel whose `remove_tunnel` returned before the snapshot *started* must not
  appear. No hangs, no exceptions, bounded total wall-clock. Two concurrent
  SSH-driven tasks is deliberate (it exercises the connection-pool dynamics)
  and stays within the dev-VM load rule.

### test_traffic.py

- **Survivor soak.** One long-lived tunnel carries sequence-numbered UDP
  datagrams at a steady low rate for the whole test while neighbor tunnels
  (2-hop and 3-hop, other ports) churn around it. Asserts: delivery ratio
  ≥ 0.95 (a quiet LAN is near-lossless, but asserting 100% over UDP is
  flake-by-design), a post-churn probe datagram still delivers, and the
  survivor's discovery status is `ok` at every checkpoint. Sent and received
  counts are both recorded from what each side actually emitted — guard what
  you emit, never infer one side from the other.

### test_adversity.py

- **Churn under impairment.** A `Link` declared on the peers' eth2 data
  plane (decision 6 — impairing the mgmt netdev is refused, and rightly so),
  with the port-scoped impairment tooling applying delay + loss **only to
  the tunnel's UDP service port**. The ssh control plane is untouched twice
  over: it rides a different netdev entirely, and the selector scopes the
  netem band to the tunnel's port. The tunnel's endpoints resolve to the
  declared eth2 ips (explicit `@interface` endpoint specs); traffic probes
  send from a bed host, since the dev VM has no data-plane address.
  Add/verify/remove cycles must stay fully correct; the traffic probe
  tolerates the configured loss (a burst of datagrams, not a single one).
  Impairment is removed in `finally`, and the test asserts the netdev is
  netem-free afterwards.
- **Repeated degrade/recover.** `CYCLES`× { add → out-of-band kill of one
  hop's tagged pids (`discover_observations` + `kill_command`, the
  test_tunnel_e2e pattern) → discovery reports `degraded (…)` → remove reaps
  the remainder (`survivors == []`) → re-add the same spec succeeds }. The
  degrade machinery under repetition instead of the existing single pass; the
  re-add proves degradation leaves no residue that blocks the port or id.

## 4. Health detection (test_health.py)

### Phantom host — unreachable from the start

A real `UnixHost` pointing at an unused bed-subnet ip (e.g. `10.10.200.99` —
must stay outside any future VM allocation) joins the lab alongside
carrot/tomato. Per cycle (`CYCLES`×, because repeated timed-out connects are a
classic FD/transport leak source and the watermark fixture is watching):

- `add_tunnel` with the phantom in the chain fails loud **naming the
  phantom**, and the rollback leaves zero tagged processes on the real peers —
  the reap-on-failed-add path with a genuinely dead transport, not a mock.
- `discover_tunnels` on the mixed lab: a real carrot↔tomato tunnel stays
  `ok` (the phantom is not in its chain), `unreachable` names the phantom, and
  the scan completes within the tunnel layer's timeout budget plus margin —
  boundedness measured with a wall-clock assertion, not assumed.
- `remove_tunnel` reports the phantom in `RemovedReport.unreachable` while
  reaping the real tunnel cleanly.

### SIGSTOP wedge — was up, went down, recovers

Freeze **only tomato's sshd listener pid** (resolved via systemd's `MainPID`),
so existing connections — including the test's control channel — keep
working while *new* connections hang at the banner exchange. Sequence:

1. Create a real tunnel via normal host objects; keep a *control* host object
   holding an established connection to tomato.
2. **Arm auto-recovery before stopping:** a detached
   `setsid sh -c 'sleep <2× test budget>; kill -CONT <pid>'` on tomato via the
   control channel — a failed teardown cannot leave the bed wedged.
3. `kill -STOP <listener pid>` via the control channel.
4. Build a **fresh** lab (new host objects → new connections must be opened)
   and run `discover_tunnels`: tomato lands in `unreachable`, the tunnel's
   status is `uncertain` ("unknown, not missing" — never falsely `degraded`),
   and the scan completes within the timeout budget.
5. `kill -CONT` via the control channel; a second fresh-lab discovery returns
   `ok` — the recovery transition, no reboot involved.
6. Remove cleanly. Teardown probes a fresh ssh connect to tomato and fails
   loud with recovery instructions (`kill -CONT`, `make vm-health`) if the bed
   is left non-responsive.

## 5. Monitor `_tunnel_loop` stability

The seam as landed: `Collector(tunnel_source=Callable[[], Awaitable[list[TunnelRecord]]] | None)`;
`_tunnel_pass` sorts records by id, keeps the **last known set on a failed
scan** (never blanks), and latches `_tunnel_scan_failing` so a persistent
failure warns once, not per tick. `DiscoveredTunnel.health` is the single
tri-state (`uncertain` > `degraded` > `ok`) consumed by both the CLI and the
wire adapter.

- **Unit tick soak** (`tests/unit/monitor/test_collector_tunnel_soak.py`,
  marked `concurrency` — rides `stability-unit` ×50 and stays in coverage):
  a scripted `tunnel_source` drives many ticks through a real `Collector`.
  Asserts: (a) last-known-state holds under alternating
  healthy/raising/unreachable-shaped results — a failed scan never blanks the
  set; (b) a raising source never kills the loop (next tick proceeds); (c) the
  warn-once latch: N consecutive failures produce one warning, and a
  success→failure edge re-arms it; (d) retained state does not grow across
  ticks (the record list is replaced, not accumulated).
- **Live loop under churn** (`test_monitor_loop.py`, in this package):
  a real `Collector` wired composition-site-style
  (`tunnel_source` → `discover_tunnels` over the bed lab) runs its tick while
  add/remove churn executes. Asserts: after each churn settle, the collector's
  tunnel set converges to the live set within one tick interval plus margin; each record's health equals the
  `DiscoveredTunnel.health` the CLI would report for the same scan (the
  CLI/monitor seam-parity claim, asserted rather than assumed); with the
  SIGSTOP wedge applied mid-run, ticks keep completing within their budget and
  the tunnel set holds at last-known state instead of blanking, then converges
  again after `CONT`. Docker-free throughout (the monitor e2e rule) — this
  drives the `Collector` directly; no web server, no browser.

## 6. Makefile wiring & budget

```make
stability-tunnel: ## Tunnel soak against the live bed (churn, concurrency,
                  ## adversity, health, monitor loop). Requires lab VMs.
                  ## JUnit XML in reports/junit/stability-tunnel/.
                  ## COUNT=N repeats the whole suite (default 1);
                  ## CYCLES=N sets internal loop depth (default 5).
    OTTO_DETECT_ASYNCIO_LEAKS=1 OTTO_TUNNEL_SOAK_CYCLES=$(STABILITY_TUNNEL_CYCLES) \
    uv run pytest tests/e2e/tunnel_stability \
        -m "stability and hops" \
        --count=$(STABILITY_TUNNEL_COUNT) \
        -p no:cacheprovider \
        --no-cov \
        $(call junitxml,stability-tunnel)
```

(Recipe shown with spaces for the docs linter; the real Makefile recipe is
tab-indented, matching the sibling tiers.)

Single-process — bed serialization is the constraint, not CPU, and the shared
`xdist_group` already forbids intra-bed concurrency. `STABILITY_TUNNEL_COUNT`
defaults to 1 via the same `$(origin COUNT)` pattern the other tiers use;
`STABILITY_TUNNEL_CYCLES` defaults to 5 and honors `CYCLES=N`.
`OTTO_DETECT_ASYNCIO_LEAKS=1` matches every other stability tier.

`make stability` gains the tier between unix and embedded:
unit → unix → **tunnel** → embedded, same keep-going semantics. The `.PHONY`
line and the `stability-*` help text gain the new target.

Iteration knobs match the sibling tiers' `COUNT` convention: `make
stability-tunnel COUNT=N` repeats the whole suite via pytest-repeat (default
1, the `$(origin COUNT)` pattern), and `CYCLES=N` sets the internal loop
depth. The aggregate `stability` target forwards `COUNT`/`CYCLES` to the
tunnel tier **only when explicitly given on the command line** — its bare
invocation otherwise passes the global `COUNT ?= 10` default explicitly
(deliberate for the other tiers, but ×10 of a ~20-minute internally-looping
suite is a multi-hour surprise, not a default). Bare `make stability` runs
the tunnel tier once at CYCLES=5.

**Budget at defaults (CYCLES=5, COUNT=1):** churn ≈ 5 min, concurrency ≈ 3,
traffic ≈ 3, adversity ≈ 4, health ≈ 4, monitor loop ≈ 3 — **≈ 20–22 min
single-pass**, scaling roughly linearly with `CYCLES` (a `CYCLES=2` smoke run
≈ 8–10 min).

## 7. Implementation phases

1. **Skeleton:** extract `tests/_fixtures/tunnel_bed.py` (test_tunnel_e2e.py
   imports from it, its tests untouched), package conftest (reap/sweep/
   watermark/knob), `test_churn.py`, Makefile target + `stability-unix`
   marker-expression change. Proves the whole selection/target/hygiene
   skeleton end-to-end.
2. **Concurrency:** `test_concurrency.py`; if racing adds exposes the TOCTOU
   race, the product fix in `otto.tunnel.manage` plus its unit tests land
   here.
3. **Traffic + adversity:** `test_traffic.py`, `test_adversity.py`.
4. **Health:** `test_health.py` (phantom + SIGSTOP).
5. **Monitor loop:** unit tick soak + `test_monitor_loop.py`.

Each phase leaves `make stability-tunnel` green and `make coverage`
unaffected.

## 8. Known risks

- **Racing adds may expose a real product race** (TOCTOU between
  `_check_conflicts` and launch). Accepted: the fix is in scope (§3); the test
  asserts the intended contract.
- **SIGSTOP wedge relies on systemd `MainPID`** to isolate the listener from
  per-connection children. The bed peers are Ubuntu/netplan-era systemd —
  holds today; the helper asserts the pid is distinct from the control
  session's own sshd child before stopping.
- **UDP delivery threshold (0.95)** is a flake-tolerance judgment call; if the
  bed proves perfectly lossless in practice, tighten it in a follow-up rather
  than starting strict and flaking.
- **Wall-clock creep:** every mode is knob-scaled, but the bed is shared —
  if 20 min proves disruptive, the lever is `CYCLES` (or dropping the default
  to 3), not deleting modes.
- **Phantom ip collision:** `10.10.200.99` must stay outside future bed VM
  allocations; the helper asserts the ip does not answer a TCP connect before
  using it as a phantom (a live host there = loud config error, not a false
  pass).
