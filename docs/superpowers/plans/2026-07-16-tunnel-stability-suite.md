# Tunnel Stability Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dedicated `make stability-tunnel` tier that soaks the tunnel machinery (churn, concurrency, traffic, adversity, host-down health, monitor loop) against the live 3-VM bed, per the approved spec `docs/superpowers/specs/2026-07-16-tunnel-stability-suite-design.md`.

**Architecture:** Shared bed-hygiene helpers extracted from `tests/e2e/test_tunnel_e2e.py` into `tests/_fixtures/tunnel_bed.py`; a new `tests/e2e/tunnel_stability/` package whose conftest owns reap/sweep/watermark guarantees; one focused test module per mode family; a no-VM Collector tick soak in `tests/unit/monitor/`. Tests drive the library API (`add_tunnel` / `discover_tunnels` / `remove_tunnel` / `remove_all_tunnels`) over the veggies bed (carrot/tomato/pepper = test1/2/3 at 10.10.200.11/.12/.13), management-ip resolution except where a task says otherwise.

**Tech Stack:** pytest + pytest-asyncio + pytest-repeat, asyncssh-backed `UnixHost`, `otto.tunnel`, `otto.link` (port-scoped impairment), `otto.monitor.collector.MetricCollector`, GNU make.

## Global Constraints

- Every module in `tests/e2e/tunnel_stability/` carries exactly: `pytest.mark.stability`, `pytest.mark.integration`, `pytest.mark.hops`, `pytest.mark.xdist_group("link_tunnels_e2e")` (spec §2).
- Service ports come only from the 15100–15199 block, per the module map in `_harness.py` (spec §1). Never reuse 15000–15004 (owned by `test_tunnel_e2e.py`).
- Internal loop depth = `OTTO_TUNNEL_SOAK_CYCLES` env var, default 5, read in ONE place (`_harness.SOAK_CYCLES`).
- Host-down is NEVER a `pytest.skip` — always a loud, host-named failure (dev-VM rule).
- Never power VMs, never add iptables partition rules, no parallel test load: all live runs are single-process (`uv run pytest <path> --no-cov`, no `-n`). Timeout ceilings scale with the knob and are generous — never kill a live-bed run at a tight timeout.
- Live-bed verification steps in this plan run with `OTTO_TUNNEL_SOAK_CYCLES=2` to bound wall-clock; the final task runs full defaults.
- Commits: conventional prefix + `Assisted-by: Claude (claude-fable-5)` trailer (worktree policy). Work happens on a worktree branch (e.g. `worktree-tunnel-stability-suite`), never directly on main.
- `ty` runs only at `nox -s typecheck` / `make typecheck` — budget a typecheck after src edits (Task 6 is the only task that may touch `src/`).
- `nox -s lint` = `ruff check` **and** `ruff format --check` — run `uv run ruff format <files>` before committing.
- No `from __future__ import annotations` in NEW files (Sphinx nitpicky trap). (`tests/_fixtures/labdata.py` already has one; leave existing files as they are.)
- **Bed prerequisite for Task 8:** the 192.168.1.x data plane must be on the dedicated eth2 NIC (Vagrantfile + tech1 lab.json changed 2026-07-16, staged with this plan). That requires Chris running `vagrant reload test1 test2 test3` (halt+up) from his host checkout — a NIC attach, not re-provisioning. Every other task rides the management ips and is redeploy-independent. If Task 8's precheck fires, STOP and surface it; never work around it.

**Key API signatures used throughout (verified on main @ `fd51384`):**

```python
# otto.tunnel (src/otto/tunnel/manage.py, discovery.py, records.py)
async def add_tunnel(lab, hosts: list[tuple[str, str | None]], *, port: int,
                     protocol: str = "tcp", dest=None, carrier=DEFAULT_CARRIER) -> AddedTunnel
    # AddedTunnel.tunnel.id, .carrier_fwd, .carrier_rev
async def remove_tunnel(lab, tunnel_id: str) -> RemovedReport
    # .removed_ids, .killed, .unreachable, .survivors
async def remove_all_tunnels(lab) -> RemovedReport
async def discover_tunnels(lab) -> TunnelDiscovery      # .tunnels, .unreachable
    # DiscoveredTunnel: .tunnel, .present (set[ProcKey]), .status (str),
    #                   .uncertain (bool), .health ("ok"|"degraded"|"uncertain")
async def discover_observations(lab) -> tuple[list[tuple[str, Observation]], list[str]]
DISCOVERY_PS_COMMAND: str
def parse_process_discovery(text: str) -> list[Observation]
_TUNNEL_HOST_TIMEOUT = 30.0                              # discovery.py:25
async def discover_tunnel_records(lab) -> list[TunnelRecord]   # records.py; raises
    # TunnelScanFailedError when ALL scannable hosts are unreachable

# otto.monitor.collector
MetricCollector(hosts=[], tunnel_source=<async callable -> list[TunnelRecord]>)
    # .session_id, ._publish (callable attr), ._db (None ok), .get_tunnel_records(),
    # async ._tunnel_pass()

# otto.link
async def impair_link(lab, ident, params: ImpairmentParams, *, from_host=None,
                      expire=None, selector: Selector | None = None) -> ImpairReport
async def repair_link(lab, ident) -> ...
Selector(port: int, protocol: str)      # e.g. Selector(15140, "udp")
ImpairmentParams(delay_ms=..., loss_pct=...)
Link(a=LinkEndpoint(host=..., interface=..., ip=...), b=..., name=...)  # lab.links.append(...)

# hosts / fixtures
tests._fixtures.labdata: host_data(ne) -> dict, make_host(ne, **kwargs) -> UnixHost
UnixHost(ip=..., element=..., creds=[Cred(**c)], term="ssh", transfer="scp",
         log=LogMode.QUIET, interfaces={...}, ssh_options=SshOptions(connect_timeout=5))
host.exec(cmd, timeout=15, log=LogMode.QUIET) -> CommandResult (.is_ok, .value)
host.run(cmd, sudo=True, log=LogMode.QUIET) -> (await ...).only  (Result)
otto.host.interface.Interface(ip=..., subnet=...)
otto.host.daemon.kill_command(pids) -> str
```

---

### Task 1: Extract `tests/_fixtures/tunnel_bed.py` from `test_tunnel_e2e.py`

**Files:**
- Create: `tests/_fixtures/tunnel_bed.py`
- Modify: `tests/e2e/test_tunnel_e2e.py` (delete moved code, import instead)

**Interfaces:**
- Consumes: `tests/_fixtures/labdata.py` (`host_data`, `make_host`), `otto.tunnel.discovery` (`DISCOVERY_PS_COMMAND`, `parse_process_discovery`).
- Produces (imported by every later task): `VEGGIES`, `SSH_PORT`, `REACHABLE_TIMEOUT`, `LISTEN_TIMEOUT`, `POLL_INTERVAL`, `BIND_CONFIRM_TIMEOUT`, `build_bed_host(ne, **overrides) -> UnixHost`, `resolved_ip(ne) -> str`, `assert_reachable(element, ip) -> None` (async), `assert_no_leftover_tunnel_processes() -> None` (async), `listener_script(port, outfile, timeout) -> str`, `wait_for_udp_bound(host, ip, port, timeout=...)` (async), `spawn_udp_listener(host, port, outfile, timeout)` (async), `wait_for_listener_output(host, outfile, timeout=..., interval=...)` (async), `remove_remote_file(host, path)` (async), `send_udp(ip, port, payload)`, `random_outfile() -> str`.

- [ ] **Step 1: Create `tests/_fixtures/tunnel_bed.py`**

Move the following from `tests/e2e/test_tunnel_e2e.py` verbatim (only renames noted; keep every docstring — they carry live-bed lessons):

```python
"""Shared live-bed helpers for tunnel e2e + stability suites.

Extracted verbatim from tests/e2e/test_tunnel_e2e.py (2026-07-16) so the
single-pass e2e module and tests/e2e/tunnel_stability/ share ONE source of
truth for bed hygiene. See that module's docstring for the veggies topology
and the management-ip resolution story.
"""

import asyncio
import contextlib
import shlex
import socket
import time
import uuid

from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery
from tests._fixtures.labdata import host_data, make_host

VEGGIES = ("carrot", "tomato", "pepper")

SSH_PORT = 22
REACHABLE_TIMEOUT = 10
LISTEN_TIMEOUT = 20.0
POLL_INTERVAL = 1.0
BIND_CONFIRM_TIMEOUT = 5.0


def build_bed_host(ne: str, **overrides) -> UnixHost:
    """Build a real ``UnixHost`` from the veggies lab data (mgmt-ip resolution)."""
    kwargs = {"term": "ssh", "transfer": "scp", "log": LogMode.QUIET}
    kwargs.update(overrides)
    return make_host(ne, **kwargs)


def resolved_ip(ne: str) -> str:
    # (docstring moved verbatim from test_tunnel_e2e.py::_resolved_ip)
    return host_data(ne)["ip"]
```

Then move these functions verbatim, dropping the leading underscore from each public name: `_assert_reachable` → `assert_reachable`, `_assert_no_leftover_tunnel_processes` → `assert_no_leftover_tunnel_processes` (its body's `_build_host(ne)` calls become `build_bed_host(ne)` — the sweep never needs `docker_capable`), `_listener_script` → `listener_script`, `_wait_for_udp_bound` → `wait_for_udp_bound`, `_spawn_udp_listener` → `spawn_udp_listener` (calls `wait_for_udp_bound`), `_wait_for_listener_output` → `wait_for_listener_output`, `_rm` → `remove_remote_file`, `_send_udp` → `send_udp`, `_random_outfile` → `random_outfile`. Constants `_BIND_CONFIRM_TIMEOUT`/`_SSH_PORT`/etc. are replaced by the module-level public ones above.

- [ ] **Step 2: Rewire `tests/e2e/test_tunnel_e2e.py`**

Delete the moved functions/constants from `test_tunnel_e2e.py` and import instead:

```python
from tests._fixtures.tunnel_bed import (
    BIND_CONFIRM_TIMEOUT,
    LISTEN_TIMEOUT,
    POLL_INTERVAL,
    assert_no_leftover_tunnel_processes,
    assert_reachable,
    build_bed_host,
    listener_script,
    random_outfile,
    remove_remote_file,
    resolved_ip,
    send_udp,
    spawn_udp_listener,
    wait_for_listener_output,
    wait_for_udp_bound,
)
```

Keep the module's own `_build_host` as a one-liner preserving its docker capability:

```python
def _build_host(ne: str) -> UnixHost:
    """Build a real, docker-capable ``UnixHost`` from the veggies lab data."""
    return build_bed_host(ne, docker_capable=True)
```

Update every call site in the module to the new names (`_resolved_ip(` → `resolved_ip(`, `_send_udp(` → `send_udp(`, `_assert_reachable(` → `assert_reachable(`, `_spawn_udp_listener(` → `spawn_udp_listener(`, `_wait_for_listener_output(` → `wait_for_listener_output(`, `_rm(` → `remove_remote_file(`, `_random_outfile(` → `random_outfile(`, `_assert_no_leftover_tunnel_processes(` → `assert_no_leftover_tunnel_processes(`, `_wait_for_udp_bound(` → `wait_for_udp_bound(`). The container-specific helpers (`_proc_net_udp_needle`, `_wait_for_container_udp_bound`, `_spawn_container_listener`, `_wait_for_container_file`, `_oldos_repo`, `_foreign_socat_port`) STAY in `test_tunnel_e2e.py` — no other module needs them.

- [ ] **Step 3: Verify collection and hostless suites**

Run: `uv run pytest tests/e2e/test_tunnel_e2e.py --collect-only -q --no-cov`
Expected: 5 tests collected, no import errors.

Run: `uv run pytest tests/unit/tunnel --no-cov -q`
Expected: all pass (no production code touched).

- [ ] **Step 4: Prove the extraction live (one single-pass test)**

Run: `uv run pytest tests/e2e/test_tunnel_e2e.py::test_direct_tunnel_bidirectional --no-cov -q`
Expected: `1 passed` in ~1–2 min. (Requires the lab VMs up; a host-down failure names the VM — that is bed state, not this refactor.)

- [ ] **Step 5: Lint, format, commit**

Run: `uv run ruff format tests/_fixtures/tunnel_bed.py tests/e2e/test_tunnel_e2e.py && uv run ruff check tests/_fixtures/tunnel_bed.py tests/e2e/test_tunnel_e2e.py`
Expected: clean.

```bash
git add tests/_fixtures/tunnel_bed.py tests/e2e/test_tunnel_e2e.py
git commit -m "refactor(tests): extract shared tunnel bed helpers into tests/_fixtures/tunnel_bed.py

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 2: Stability package skeleton — conftest, harness, knobs

**Files:**
- Create: `tests/e2e/tunnel_stability/__init__.py` (empty)
- Create: `tests/e2e/tunnel_stability/conftest.py`
- Create: `tests/e2e/tunnel_stability/_harness.py`

**Interfaces:**
- Consumes: Task 1's `tunnel_bed` exports.
- Produces: fixtures `tunnel_lab` (a `Lab` over all three peers), `reap_tunnels` (list to append created tunnel ids to), autouse `_fd_watermark` + module-final `_final_leftover_sweep`; harness constants `SOAK_CYCLES`, `INGRESS`/`EXIT`/`RELAY`, all `PORT_*` constants, `soak_timeout(per_cycle, base=120.0) -> float`; helpers `assert_discovered(lab, tunnel_id, *, procs)` (async), `assert_gone(lab, tunnel_id)` (async), `add_remove_cycle(lab, reap, chain, *, port, procs, protocol="udp") -> str` (async), `stream_listener_script(port, outfile, timeout) -> str`.

- [ ] **Step 1: Write `_harness.py`**

```python
"""Soak knobs, port map, and churn helpers for the tunnel stability suite.

Spec: docs/superpowers/specs/2026-07-16-tunnel-stability-suite-design.md.
Every knob is read HERE and nowhere else.
"""

import os

from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel

SOAK_CYCLES = int(os.environ.get("OTTO_TUNNEL_SOAK_CYCLES", "5"))
"""Internal loop depth per soak test. `make stability-tunnel CYCLES=N` sets it."""

INGRESS = "carrot_seed"  # test1, 10.10.200.11
EXIT = "tomato_seed"  # test2, 10.10.200.12
RELAY = "pepper_seed"  # test3, 10.10.200.13

# Port map (spec §1): the 15100–15199 block, disjoint from test_tunnel_e2e's
# 15000–15004. One module per row; never borrow across rows.
PORT_CHURN_DIRECT = 15100
PORT_CHURN_MULTIHOP = 15101
PORT_CHURN_ALTERNATING = 15102
PORTS_REMOVE_ALL = (15103, 15104, 15105)
PORTS_POPULATION = tuple(range(15110, 15120))
PORT_RACING = 15120
PORT_DISCOVERY_CHURN = 15121
PORT_SURVIVOR = 15130
PORTS_TRAFFIC_NEIGHBORS = (15131, 15132)
PORT_IMPAIRED = 15140
PORT_DEGRADE = 15141
PORT_PHANTOM_REAL = 15150
PORT_PHANTOM_CHAIN = 15151
PORT_SIGSTOP = 15152
PORTS_MONITOR_CHURN = (15160, 15161)


def soak_timeout(per_cycle: float, base: float = 120.0) -> float:
    """Per-test ceiling scaled to the knob. Generous by design — the live bed
    is never killed at a tight timeout (dev-VM rule); a genuine wedge still
    fails, just with slack for slow SSH days."""
    return base + per_cycle * SOAK_CYCLES


async def assert_discovered(lab, tunnel_id: str, *, procs: int) -> None:
    """The tunnel is discovered, status ``ok``, with exactly *procs* processes."""
    discovery = await discover_tunnels(lab)
    found = next((d for d in discovery.tunnels if d.tunnel.id == tunnel_id), None)
    assert found is not None, f"tunnel {tunnel_id!r} not in discover_tunnels"
    assert found.status == "ok", f"expected status 'ok', got {found.status!r}"
    assert len(found.present) == procs, f"expected {procs} processes, got {len(found.present)}"


async def assert_gone(lab, tunnel_id: str) -> None:
    discovery = await discover_tunnels(lab)
    assert not any(d.tunnel.id == tunnel_id for d in discovery.tunnels), (
        f"{tunnel_id!r} still discoverable after remove"
    )


async def add_remove_cycle(lab, reap, chain, *, port: int, procs: int, protocol: str = "udp") -> str:
    """One full verified lifecycle: add → discovered ok → remove clean → gone.

    Returns the cycled tunnel id (deterministic per (chain, port) — spec §4 of
    the tunnel design: ``tun-<12hex>-<port>``)."""
    added = await add_tunnel(lab, chain, port=port, protocol=protocol)
    reap.append(added.tunnel.id)
    await assert_discovered(lab, added.tunnel.id, procs=procs)
    report = await remove_tunnel(lab, added.tunnel.id)
    assert added.tunnel.id in report.removed_ids
    assert report.survivors == [], f"survivors after remove: {report.survivors!r}"
    reap.remove(added.tunnel.id)
    await assert_gone(lab, added.tunnel.id)
    return added.tunnel.id


def stream_listener_script(port: int, outfile: str, timeout: float) -> str:
    """Python source (run remotely, detached) that appends every datagram's
    payload to *outfile*, one per line, until 'STOP' arrives or *timeout*
    passes. Binds 127.0.0.1 for the same overlap reason as
    ``tunnel_bed.listener_script``."""
    return (
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.settimeout(1.0)\n"
        f"deadline = time.monotonic() + {timeout}\n"
        f"out = open({outfile!r}, 'a', buffering=1)\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        data, _addr = s.recvfrom(65535)\n"
        "    except socket.timeout:\n"
        "        continue\n"
        "    text = data.decode('utf-8', 'replace')\n"
        "    out.write(text + '\\n')\n"
        "    if text == 'STOP':\n"
        "        break\n"
    )
```

- [ ] **Step 2: Write `conftest.py`**

```python
"""Package-wide bed hygiene (spec §1): reap + sweep + watermark, always on."""

import asyncio
import contextlib
import gc
import os

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.tunnel import remove_tunnel
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    VEGGIES,
    assert_no_leftover_tunnel_processes,
    assert_reachable,
    build_bed_host,
)

_FD_TOLERANCE = 4


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture(autouse=True)
def _fd_watermark():
    """Local-side leak bracket: the process's open-FD count must return to
    baseline (±tolerance) once the lab fixture has closed every host. Autouse
    and dependency-free, so pytest instantiates it BEFORE (and finalizes it
    AFTER) `tunnel_lab` — the bracket wraps the hosts' whole lifetime. One
    gc pass absorbs collector timing before the verdict."""
    gc.collect()
    before = _open_fds()
    yield
    gc.collect()
    after = _open_fds()
    if after > before + _FD_TOLERANCE:
        gc.collect()
        after = _open_fds()
    assert after <= before + _FD_TOLERANCE, (
        f"local fd leak across test: {before} -> {after} open fds"
    )


@pytest_asyncio.fixture
async def tunnel_lab():
    """Real ``Lab`` over the 3-VM veggies bed; host-down fails LOUD, never skips."""
    for ne in VEGGIES:
        await assert_reachable(ne, host_data(ne)["ip"])
    lab = Lab(name="tunnel_stability")
    for ne in VEGGIES:
        lab.add_host(build_bed_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


@pytest_asyncio.fixture
async def reap_tunnels(tunnel_lab):
    """Guaranteed teardown: reap every tunnel this test created, even on failure."""
    created: list[str] = []
    yield created
    for tunnel_id in created:
        with contextlib.suppress(Exception):
            await remove_tunnel(tunnel_lab, tunnel_id)


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep():
    """Module-final bed hygiene: FAIL (never skip) if any tagged process
    survived. Sync fixture with its own asyncio.run — it fires after every
    per-test event loop has closed (same pattern as test_tunnel_e2e.py)."""
    yield
    asyncio.run(assert_no_leftover_tunnel_processes())
```

- [ ] **Step 3: Create empty `tests/e2e/tunnel_stability/__init__.py`, verify collection + lint**

Run: `uv run pytest tests/e2e/tunnel_stability --collect-only -q --no-cov`
Expected: `no tests ran` / 0 collected, **no errors** (empty package with valid conftest).

Run: `uv run ruff format tests/e2e/tunnel_stability/ && uv run ruff check tests/e2e/tunnel_stability/`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/tunnel_stability/
git commit -m "test(tunnel): stability-suite skeleton — conftest hygiene + soak harness

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 3: Baseline churn module (`test_churn.py`)

**Files:**
- Create: `tests/e2e/tunnel_stability/test_churn.py`

**Interfaces:**
- Consumes: conftest fixtures (`tunnel_lab`, `reap_tunnels`), `_harness` (`SOAK_CYCLES`, `add_remove_cycle`, `soak_timeout`, ports, host ids), `tunnel_bed` UDP helpers.
- Produces: nothing imported by later tasks (leaf module).

- [ ] **Step 1: Write the module**

```python
"""Baseline add/remove churn (spec §3, test_churn): the tunnel lifecycle under
repetition. Traffic is probed on the FIRST and LAST cycle only; process-level
verification runs every cycle (spec's wall-clock/signal trade)."""

import uuid

import pytest

from otto.tunnel import add_tunnel, discover_tunnels, remove_all_tunnels, remove_tunnel
from tests._fixtures.tunnel_bed import (
    LISTEN_TIMEOUT,
    random_outfile,
    remove_remote_file,
    resolved_ip,
    send_udp,
    spawn_udp_listener,
    wait_for_listener_output,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_CHURN_ALTERNATING,
    PORT_CHURN_DIRECT,
    PORT_CHURN_MULTIHOP,
    PORTS_REMOVE_ALL,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    assert_gone,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=60.0)),
]


async def _probe_traffic(lab, *, ingress_ne: str, listen_host_id: str, port: int) -> None:
    """One end-to-end datagram through the tunnel (the tunnel_bed pattern)."""
    listener_host = lab.hosts[listen_host_id]
    outfile = random_outfile()
    payload = f"otto-soak-{uuid.uuid4().hex}".encode()
    try:
        await spawn_udp_listener(listener_host, port, outfile, timeout=LISTEN_TIMEOUT)
        send_udp(resolved_ip(ingress_ne), port, payload)
        received = await wait_for_listener_output(listener_host, outfile)
        _src, _, recv_payload = received.partition(" ")
        assert recv_payload == payload.decode(), (
            f"expected {payload.decode()!r} through the tunnel, got {received!r}"
        )
    finally:
        await remove_remote_file(listener_host, outfile)


@pytest.mark.asyncio
async def test_direct_churn(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x (add 2-hop -> ok/4 procs -> remove clean -> gone); traffic on
    the first and last cycle."""
    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_CHURN_DIRECT, protocol="udp")
        reap_tunnels.append(added.tunnel.id)
        await assert_discovered(tunnel_lab, added.tunnel.id, procs=4)
        if cycle in (0, SOAK_CYCLES - 1):
            await _probe_traffic(
                tunnel_lab, ingress_ne="carrot", listen_host_id=EXIT, port=PORT_CHURN_DIRECT
            )
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
        await assert_gone(tunnel_lab, added.tunnel.id)


@pytest.mark.asyncio
async def test_multihop_churn(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x the 3-hop lifecycle: ok/6 procs, relay pair on the middle host."""
    chain = [(INGRESS, None), (EXIT, None), (RELAY, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_CHURN_MULTIHOP, protocol="udp")
        reap_tunnels.append(added.tunnel.id)
        discovery = await discover_tunnels(tunnel_lab)
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"cycle {cycle}: tunnel not discovered"
        assert found.status == "ok", f"cycle {cycle}: {found.status!r}"
        assert len(found.present) == 6, f"cycle {cycle}: {len(found.present)} procs"
        relay_procs = [key for key in found.present if key[0] == EXIT]
        assert len(relay_procs) == 2, f"cycle {cycle}: relay procs {relay_procs!r}"
        if cycle in (0, SOAK_CYCLES - 1):
            await _probe_traffic(
                tunnel_lab, ingress_ne="carrot", listen_host_id=RELAY, port=PORT_CHURN_MULTIHOP
            )
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
        await assert_gone(tunnel_lab, added.tunnel.id)


@pytest.mark.asyncio
async def test_alternating_shape_frees_the_port(tunnel_lab, reap_tunnels) -> None:
    """2-hop and 3-hop alternately on the SAME service port: each remove must
    genuinely free the port for a differently-shaped successor (spec §3 —
    catches lingering binds a same-shape re-add could mask)."""
    two_hop = [(INGRESS, None), (EXIT, None)]
    three_hop = [(INGRESS, None), (EXIT, None), (RELAY, None)]
    for cycle in range(SOAK_CYCLES):
        chain, procs = (two_hop, 4) if cycle % 2 == 0 else (three_hop, 6)
        await add_remove_cycle(
            tunnel_lab, reap_tunnels, chain, port=PORT_CHURN_ALTERNATING, procs=procs
        )


@pytest.mark.asyncio
async def test_remove_all_sweep_cycling(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x (build 3 mixed-shape tunnels -> remove_all_tunnels -> clean):
    the owner-agnostic reap path under repetition."""
    shapes = [
        ([(INGRESS, None), (EXIT, None)], PORTS_REMOVE_ALL[0]),
        ([(EXIT, None), (RELAY, None)], PORTS_REMOVE_ALL[1]),
        ([(INGRESS, None), (EXIT, None), (RELAY, None)], PORTS_REMOVE_ALL[2]),
    ]
    for cycle in range(SOAK_CYCLES):
        ids = []
        for chain, port in shapes:
            added = await add_tunnel(tunnel_lab, chain, port=port, protocol="udp")
            reap_tunnels.append(added.tunnel.id)
            ids.append(added.tunnel.id)
        report = await remove_all_tunnels(tunnel_lab)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        assert set(ids) <= set(report.removed_ids), (
            f"cycle {cycle}: removed {report.removed_ids!r}, expected ⊇ {ids!r}"
        )
        for tunnel_id in ids:
            reap_tunnels.remove(tunnel_id)
        post = await discover_tunnels(tunnel_lab)
        assert post.tunnels == [], f"cycle {cycle}: discovery not empty: {post.tunnels!r}"
```

- [ ] **Step 2: Verify collection**

Run: `uv run pytest tests/e2e/tunnel_stability/test_churn.py --collect-only -q --no-cov`
Expected: 4 tests collected.

- [ ] **Step 3: Run live at CYCLES=2**

Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_churn.py --no-cov -q`
Expected: `4 passed` in roughly 4–8 min. If a cycle fails, the assertion names the cycle number — diagnose before rerunning (root-cause-first rule).

- [ ] **Step 4: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_churn.py && uv run ruff check tests/e2e/tunnel_stability/test_churn.py`

```bash
git add tests/e2e/tunnel_stability/test_churn.py
git commit -m "test(tunnel): baseline churn soak — direct/multihop/alternating/remove-all

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 4: Makefile wiring — `stability-tunnel` tier

**Files:**
- Modify: `Makefile` (`.PHONY` line 3, COUNT block ~line 55, stability targets ~line 461–514, help text ~line 626)
- Modify: `docs/contributing.md:310` (tier table row; full docs pass is Task 13)

**Interfaces:**
- Consumes: the `stability`/`hops` markers on Task 3's module.
- Produces: `make stability-tunnel` (used by every later live-run step), `STABILITY_TUNNEL_COUNT`, `STABILITY_TUNNEL_CYCLES`.

- [ ] **Step 1: Prove the current tier-2 selection count (pre-change baseline)**

Run: `uv run pytest -m "stability and integration and not embedded" --collect-only -q --no-cov 2>/dev/null | tail -1`
Record the count — call it N. (The new churn module must NOT be in this set once `and not hops` lands, and N must not change.)

- [ ] **Step 2: Edit the Makefile**

Add to `.PHONY` (line 3): `stability-tunnel` (next to `stability-unix`).

After the `STABILITY_UNIX_COUNT` block (~line 55), add:

```make
# Iteration count for `make stability-tunnel`. Default is 1 (the tests loop
# internally via OTTO_TUNNEL_SOAK_CYCLES); honor COUNT only when explicitly
# passed on the command line.
STABILITY_TUNNEL_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Internal soak depth for `make stability-tunnel` (cycles per test). Default 5;
# override with CYCLES=N (a smoke run: CYCLES=2).
STABILITY_TUNNEL_CYCLES := $(if $(filter command line,$(origin CYCLES)),$(CYCLES),5)
```

In `stability-unix` (~line 476), change the marker expression:

```make
	    -m "stability and integration and not embedded and not hops" \
```

After the `stability-unix` target, add (recipe lines are TABS):

```make
stability-tunnel: ## Tunnel soak against the live bed (churn/concurrency/traffic/adversity/health/monitor-loop). Requires lab VMs. JUnit XML in reports/junit/stability-tunnel/. COUNT=N repeats the suite (default 1); CYCLES=N sets internal loop depth (default 5).
	OTTO_DETECT_ASYNCIO_LEAKS=1 OTTO_TUNNEL_SOAK_CYCLES=$(STABILITY_TUNNEL_CYCLES) uv run pytest \
	    tests/e2e/tunnel_stability \
	    -m "stability and hops" \
	    --count=$(STABILITY_TUNNEL_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-tunnel)
```

In the aggregate `stability` target (~line 490), after the `stability-unix` line and before the Tier-3 echo, insert:

```make
	@echo
	@echo "── Tier 2b (tunnel soak) ──"
	@$(MAKE) stability-tunnel $(if $(filter command line,$(origin COUNT)),COUNT=$(COUNT)) $(if $(filter command line,$(origin CYCLES)),CYCLES=$(CYCLES))
```

Unlike the sibling tiers, `COUNT` is forwarded ONLY when the user explicitly
set it: the aggregate's bare invocation otherwise passes the global
`COUNT ?= 10` default as an *explicit* command-line value (that behavior is
deliberate for `stability-embedded` — see its comment — but ×10 of a
~20-minute internally-looping suite is a 3.5-hour surprise, not a default).
Bare `make stability` therefore runs the tunnel tier once at CYCLES=5;
`make stability COUNT=3` runs every tier — tunnel included — at ×3.

In the help text (~line 626), update the `stability-*` line:

```make
	@printf '  \033[36m%-30s\033[0m %s\n' 'stability-*'  'pytest-repeat soak          (unit · unix · tunnel · embedded; bare stability = all tiers)'
```

- [ ] **Step 3: Update the contributing.md tier table row (line 310)**

```markdown
| Stability / soak | `make stability` (or `stability-unit` / `stability-unix` / `stability-tunnel` / `stability-embedded`) | lab VMs (`-unit` needs none) |
```

- [ ] **Step 4: Verify selection algebra**

Run: `uv run pytest -m "stability and integration and not embedded and not hops" --collect-only -q --no-cov 2>/dev/null | tail -1`
Expected: exactly N from Step 1 (tier 2 unchanged).

Run: `uv run pytest tests/e2e/tunnel_stability -m "stability and hops" --collect-only -q --no-cov 2>/dev/null | tail -1`
Expected: 4 tests (Task 3's module).

Run: `uv run pytest -m "not stability and not browser" --collect-only -q --no-cov 2>/dev/null | grep tunnel_stability | wc -l`
Expected: `0` (nothing here leaks into `make coverage`'s selection).

Run: `make -n stability-tunnel`
Expected: the dry-run prints the pytest command with `OTTO_TUNNEL_SOAK_CYCLES=5` and `--count=1`.

Run: `make -n stability-tunnel COUNT=3 CYCLES=2`
Expected: `--count=3` and `OTTO_TUNNEL_SOAK_CYCLES=2`.

- [ ] **Step 5: Commit**

```bash
git add Makefile docs/contributing.md
git commit -m "build: add the stability-tunnel soak tier; fence tier 2 with 'and not hops'

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 5: Concurrency module (`test_concurrency.py`)

**Files:**
- Create: `tests/e2e/tunnel_stability/test_concurrency.py`

**Interfaces:**
- Consumes: conftest fixtures, `_harness` (ports, `SOAK_CYCLES`, `add_remove_cycle`, `soak_timeout`).
- Produces: `test_racing_conflicting_adds` (adjudicated in Task 6).

- [ ] **Step 1: Write the module**

```python
"""Concurrency soaks (spec §3, test_concurrency): standing population,
racing conflicting adds, discovery under churn."""

import asyncio
import time
from collections import deque

import pytest

from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.manage import AddedTunnel
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_DISCOVERY_CHURN,
    PORT_RACING,
    PORTS_POPULATION,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=90.0)),
]

_VALID_STATUS_PREFIXES = ("ok", "degraded (")


def _status_is_wellformed(status: str) -> bool:
    """A discovery status is 'ok'/'degraded (…)', optionally '?'-suffixed
    (uncertain). Anything else is an impossible state."""
    bare = status.rstrip("?")
    return bare == "ok" or bare.startswith("degraded (")


@pytest.mark.asyncio
async def test_concurrent_population(tunnel_lab, reap_tunnels) -> None:
    """A standing population of 3 tunnels; each cycle retires the oldest and
    adds a fresh one. After every mutation, discovery reports EXACTLY the live
    set — strict equality, so a dirty bed fails loud rather than hiding."""
    ports = deque(PORTS_POPULATION)
    shapes = [
        [(INGRESS, None), (EXIT, None)],
        [(EXIT, None), (RELAY, None)],
        [(INGRESS, None), (EXIT, None), (RELAY, None)],
    ]
    live: list[tuple[str, int]] = []  # (tunnel_id, port), oldest first

    async def _assert_exact_live_set() -> None:
        discovery = await discover_tunnels(tunnel_lab)
        assert {d.tunnel.id for d in discovery.tunnels} == {tid for tid, _ in live}, (
            f"discovery {sorted(d.tunnel.id for d in discovery.tunnels)!r} != "
            f"live {sorted(tid for tid, _ in live)!r}"
        )

    for i in range(3):
        added = await add_tunnel(
            tunnel_lab, shapes[i % len(shapes)], port=ports.popleft(), protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)
        live.append((added.tunnel.id, added.tunnel.service_port))
        await _assert_exact_live_set()

    for cycle in range(SOAK_CYCLES):
        oldest_id, oldest_port = live.pop(0)
        report = await remove_tunnel(tunnel_lab, oldest_id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(oldest_id)
        ports.append(oldest_port)
        await _assert_exact_live_set()
        added = await add_tunnel(
            tunnel_lab, shapes[cycle % len(shapes)], port=ports.popleft(), protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)
        live.append((added.tunnel.id, added.tunnel.service_port))
        await _assert_exact_live_set()

    for tunnel_id, _port in live:
        report = await remove_tunnel(tunnel_lab, tunnel_id)
        assert report.survivors == []
        reap_tunnels.remove(tunnel_id)


@pytest.mark.asyncio
async def test_racing_conflicting_adds(tunnel_lab, reap_tunnels) -> None:
    """Two simultaneous add_tunnel calls for the SAME port+endpoints: exactly
    one succeeds, the loser raises, zero tagged residue after the winner is
    removed. Spec §3/§8: this asserts the INTENDED contract; the current
    _check_conflicts scan-before-launch TOCTOU may violate it — if so, Task 6
    lands the product fix rather than bending this test."""
    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        results = await asyncio.gather(
            add_tunnel(tunnel_lab, chain, port=PORT_RACING, protocol="udp"),
            add_tunnel(tunnel_lab, chain, port=PORT_RACING, protocol="udp"),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, AddedTunnel)]
        losers = [r for r in results if isinstance(r, BaseException)]
        for winner in winners:  # track before asserting, so reap covers a double-win
            reap_tunnels.append(winner.tunnel.id)
        assert len(winners) == 1 and len(losers) == 1, (
            f"cycle {cycle}: exactly-one-wins violated: {results!r}"
        )
        await assert_discovered(tunnel_lab, winners[0].tunnel.id, procs=4)
        report = await remove_tunnel(tunnel_lab, winners[0].tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(winners[0].tunnel.id)


@pytest.mark.asyncio
async def test_discovery_under_churn(tunnel_lab, reap_tunnels) -> None:
    """A poller hammers discover_tunnels while add/remove cycles run. Every
    snapshot is internally consistent; a tunnel whose remove returned before
    the snapshot STARTED never reappears."""
    chain = [(INGRESS, None), (EXIT, None)]
    stop = asyncio.Event()
    snapshots: list[tuple[float, set[str]]] = []  # (scan start monotonic, ids)
    removed_at: dict[str, float] = {}  # tunnel id -> monotonic when remove returned

    async def poller() -> None:
        while not stop.is_set():
            started = time.monotonic()
            discovery = await discover_tunnels(tunnel_lab)
            for d in discovery.tunnels:
                assert _status_is_wellformed(d.status), f"impossible status {d.status!r}"
            snapshots.append((started, {d.tunnel.id for d in discovery.tunnels}))

    async def churner() -> None:
        for _ in range(SOAK_CYCLES):
            tunnel_id = await add_remove_cycle(
                tunnel_lab, reap_tunnels, chain, port=PORT_DISCOVERY_CHURN, procs=4
            )
            removed_at[tunnel_id] = time.monotonic()

    poll_task = asyncio.create_task(poller())
    try:
        await churner()
    finally:
        stop.set()
        await poll_task

    assert snapshots, "poller never completed a scan"
    # The cycled id is deterministic per (chain, port): every cycle reuses it,
    # so 'gone forever' only holds after the LAST removal.
    final_removals = {tid: ts for tid, ts in removed_at.items()}
    for tid, removed_ts in final_removals.items():
        ghosts = [started for started, ids in snapshots if started > removed_ts and tid in ids]
        assert not ghosts, f"{tid!r} reappeared in {len(ghosts)} post-remove snapshot(s)"
```

- [ ] **Step 2: Verify collection**

Run: `uv run pytest tests/e2e/tunnel_stability/test_concurrency.py --collect-only -q --no-cov`
Expected: 3 tests.

- [ ] **Step 3: Run the two non-racing tests live at CYCLES=2**

Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_concurrency.py::test_concurrent_population tests/e2e/tunnel_stability/test_concurrency.py::test_discovery_under_churn --no-cov -q`
Expected: `2 passed` in ~3–6 min. (The racing test is adjudicated separately in Task 6 — do not fold its outcome into this step.)

- [ ] **Step 4: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_concurrency.py && uv run ruff check tests/e2e/tunnel_stability/test_concurrency.py`

```bash
git add tests/e2e/tunnel_stability/test_concurrency.py
git commit -m "test(tunnel): concurrency soaks — population, racing adds, discovery under churn

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 6: Adjudicate the racing-adds contract (product fix only if red)

**Files:**
- Possibly modify: `src/otto/tunnel/manage.py`
- Possibly create: test in `tests/unit/tunnel/test_manage_add.py` (append)

**Interfaces:**
- Consumes: `test_racing_conflicting_adds` from Task 5.
- Produces: a green racing test, by evidence or by fix.

- [ ] **Step 1: Run the racing test alone, five times**

Run: `for i in 1 2 3 4 5; do OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_concurrency.py::test_racing_conflicting_adds --no-cov -q || break; done`

**If all five runs pass:** the contract holds under the in-process race (the second launch's specific-ip bind fails and its verify-rollback cleans up). Record that in the commit message of Step 4 and skip Steps 2–3.

**If any run fails:** confirm the mechanism before fixing (root-cause-first): the failure shapes to expect are (a) two winners — both `_check_conflicts` scans ran before either launch, or (b) one "winner" whose processes are dead — the loser's rollback (`_kill_tunnel_on`, which reaps by tunnel id, and racing adds share the deterministic `tun-<12hex>-<port>` id) killed the winner's processes too. Reproduce once with `-x` and read which assert fired.

- [ ] **Step 2 (red path only): Serialize same-id adds in-process**

In `src/otto/tunnel/manage.py`, add near the module top:

```python
# Two concurrent add_tunnel calls for the SAME (path, port, protocol) share a
# deterministic tunnel id; their rollback reaps BY id, so an unserialized race
# lets the loser's rollback kill the winner's processes. Serialize per id —
# the second entrant then sees the first's processes in _check_conflicts and
# fails loud, which is the intended exactly-one-wins contract. In-process
# only by design: cross-process racers are adjudicated by the endpoint
# socat's specific-ip bind failing, and its own verify-rollback (which we do
# NOT reach here) — see tests/e2e/tunnel_stability/test_concurrency.py.
_ADD_LOCKS: dict[str, asyncio.Lock] = {}


def _add_lock(tunnel_id: str) -> asyncio.Lock:
    return _ADD_LOCKS.setdefault(tunnel_id, asyncio.Lock())
```

Then in `add_tunnel`, wrap the body from the `_check_conflicts` call through the post-add verify in `async with _add_lock(tunnel.id):` (the tunnel id is computed before the conflict check; only indentation of the existing block changes).

- [ ] **Step 3 (red path only): Unit test the serialization**

Append to `tests/unit/tunnel/test_manage_add.py`, reusing that file's existing fake lab/host fixtures (adapt the two marked lines to the fixture names already defined at the top of that file — the assertion logic stays exactly this):

```python
@pytest.mark.asyncio
async def test_racing_same_spec_adds_exactly_one_wins(fake_lab):  # ← existing fixture
    """Same (chain, port) raced in-process: one AddedTunnel, one ValueError."""
    chain = [("host_a", None), ("host_b", None)]  # ← this file's fake host ids
    results = await asyncio.gather(
        add_tunnel(fake_lab, chain, port=15999, protocol="udp"),
        add_tunnel(fake_lab, chain, port=15999, protocol="udp"),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, AddedTunnel)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1 and len(losers) == 1, repr(results)
    assert isinstance(losers[0], ValueError)  # the duplicate-id conflict, loud
```

Run: `uv run pytest tests/unit/tunnel/test_manage_add.py --no-cov -q` — expected: all pass.
Run: `make typecheck` (src was touched) — expected: clean.
Re-run Step 1's five-round loop — expected: five green rounds.

- [ ] **Step 4: Commit (message states which path was taken)**

```bash
git add -A src/otto/tunnel/manage.py tests/unit/tunnel/test_manage_add.py 2>/dev/null
git commit -m "fix(tunnel): serialize same-id add_tunnel races (exactly-one-wins)  # or, green path:
# test(tunnel): racing-adds contract verified green 5x live — no product change needed

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 7: Traffic module (`test_traffic.py`)

**Files:**
- Create: `tests/e2e/tunnel_stability/test_traffic.py`

**Interfaces:**
- Consumes: conftest fixtures, `_harness` (`stream_listener_script`, `SOAK_CYCLES`, ports), `tunnel_bed` (`send_udp`, `resolved_ip`, `wait_for_udp_bound`, `remove_remote_file`, `random_outfile`).
- Produces: leaf module.

- [ ] **Step 1: Write the module**

```python
"""Survivor traffic soak (spec §3, test_traffic): one long-lived tunnel keeps
carrying sequence-numbered datagrams while neighbors churn. Delivery ratio
>= 0.95 (asserting 100% over UDP is flake-by-design); sent and received counts
are both recorded from what each side actually emitted."""

import asyncio
import shlex
import uuid

import pytest

from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, remove_tunnel
from tests._fixtures.tunnel_bed import (
    random_outfile,
    remove_remote_file,
    resolved_ip,
    send_udp,
    wait_for_udp_bound,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_SURVIVOR,
    PORTS_TRAFFIC_NEIGHBORS,
    RELAY,
    SOAK_CYCLES,
    add_remove_cycle,
    assert_discovered,
    soak_timeout,
    stream_listener_script,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=90.0, base=180.0)),
]

_DELIVERY_FLOOR = 0.95
_SEND_INTERVAL = 0.1


@pytest.mark.asyncio
async def test_survivor_traffic_during_neighbor_churn(tunnel_lab, reap_tunnels) -> None:
    tomato = tunnel_lab.hosts[EXIT]
    outfile = random_outfile()
    run_tag = uuid.uuid4().hex[:8]

    # Long-lived tunnel + long-lived listener on its far end.
    survivor = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORT_SURVIVOR, protocol="udp"
    )
    reap_tunnels.append(survivor.tunnel.id)
    listen_budget = soak_timeout(per_cycle=90.0, base=120.0)
    script = stream_listener_script(PORT_SURVIVOR, outfile, timeout=listen_budget)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    await tomato.exec(cmd, timeout=15, log=LogMode.QUIET)
    await wait_for_udp_bound(tomato, "127.0.0.1", PORT_SURVIVOR)

    sent: list[str] = []
    stop = asyncio.Event()

    async def sender() -> None:
        n = 0
        while not stop.is_set():
            payload = f"{run_tag}-{n}"
            send_udp(resolved_ip("carrot"), PORT_SURVIVOR, payload.encode())
            sent.append(payload)
            n += 1
            await asyncio.sleep(_SEND_INTERVAL)

    sender_task = asyncio.create_task(sender())
    try:
        # Neighbor churn around the survivor, status checkpoint each cycle.
        for cycle in range(SOAK_CYCLES):
            await add_remove_cycle(
                tunnel_lab,
                reap_tunnels,
                [(INGRESS, None), (EXIT, None)],
                port=PORTS_TRAFFIC_NEIGHBORS[0],
                procs=4,
            )
            await add_remove_cycle(
                tunnel_lab,
                reap_tunnels,
                [(INGRESS, None), (EXIT, None), (RELAY, None)],
                port=PORTS_TRAFFIC_NEIGHBORS[1],
                procs=6,
            )
            await assert_discovered(tunnel_lab, survivor.tunnel.id, procs=4)
    finally:
        stop.set()
        await sender_task

    try:
        # Post-churn liveness probe: a DISTINCT payload must still arrive.
        final_probe = f"{run_tag}-final"
        received_text = ""
        for _attempt in range(5):
            send_udp(resolved_ip("carrot"), PORT_SURVIVOR, final_probe.encode())
            await asyncio.sleep(1.0)
            result = await tomato.exec(
                f"cat {shlex.quote(outfile)} 2>/dev/null || true", timeout=15, log=LogMode.QUIET
            )
            received_text = result.value or ""
            if final_probe in received_text:
                break
        assert final_probe in received_text, "survivor tunnel dead after churn ended"

        for _ in range(5):  # STOP is UDP too; send it redundantly
            send_udp(resolved_ip("carrot"), PORT_SURVIVOR, b"STOP")
            await asyncio.sleep(0.2)

        received = {
            line for line in received_text.splitlines() if line.startswith(f"{run_tag}-")
        } - {final_probe}
        assert sent, "sender never ran"
        ratio = len(received & set(sent)) / len(sent)
        assert ratio >= _DELIVERY_FLOOR, (
            f"delivery ratio {ratio:.3f} < {_DELIVERY_FLOOR} ({len(received)}/{len(sent)})"
        )
    finally:
        await remove_remote_file(tomato, outfile)

    report = await remove_tunnel(tunnel_lab, survivor.tunnel.id)
    assert report.survivors == []
    reap_tunnels.remove(survivor.tunnel.id)
```

- [ ] **Step 2: Verify collection, then run live at CYCLES=2**

Run: `uv run pytest tests/e2e/tunnel_stability/test_traffic.py --collect-only -q --no-cov` — expected: 1 test.
Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_traffic.py --no-cov -q`
Expected: `1 passed` in ~3–5 min.

- [ ] **Step 3: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_traffic.py && uv run ruff check tests/e2e/tunnel_stability/test_traffic.py`

```bash
git add tests/e2e/tunnel_stability/test_traffic.py
git commit -m "test(tunnel): survivor traffic soak under neighbor churn

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 8: Adversity module (`test_adversity.py`) — impaired-port churn + degrade/recover

**Files:**
- Create: `tests/e2e/tunnel_stability/test_adversity.py`

**Interfaces:**
- Consumes: conftest fixtures; `otto.link` (`impair_link`, `repair_link`, `repair_all`, `ImpairmentParams`, `Selector`, `Link`, `LinkEndpoint`); `otto.host.interface.Interface`; `tunnel_bed`; `_harness`.
- Produces: leaf module.

**Background (bed contract, spec decision 6):** the product refuses to impair the netdev carrying a host's management ip (`ensure_not_mgmt`, self-lockout guard — correct and per-netdev, since tc qdiscs are device-scoped). The bed therefore moved the `192.168.1.x` data plane to a **dedicated eth2 NIC** (VirtualBox internal network `otto-dataplane`; Vagrantfile + tech1 `lab.json` changed 2026-07-16, redeploy required — see the fixture's loud precheck). The impaired-churn test declares a `Link` on eth2 and impairs it with the port-scoped `Selector`: ssh is untouched twice over (different netdev AND the selector scopes the netem band to the tunnel's UDP port). Endpoints resolve to the declared eth2 ips via explicit `@interface` specs; traffic probes send from a bed host, because the dev VM has no data-plane address.

- [ ] **Step 1: Write the module**

```python
"""Adversity soaks (spec §3, test_adversity): control-plane correctness while
the data path is impaired, and the degrade->reap->re-add cycle under repetition.

The impaired path is the bed's REAL data plane: a dedicated eth2 NIC carrying
192.168.1.x on each peer (spec decision 6 — the mgmt netdev is refused by the
impair placement guard, so the data plane lives on its own device). Nothing is
created or deleted on the peers here; the fixture only asserts the bed
contract holds and fails loud with redeploy instructions if not."""

import asyncio
import contextlib
import shlex
import uuid

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.host.daemon import kill_command
from otto.host.interface import Interface
from otto.link import ImpairmentParams, Link, LinkEndpoint, Selector, impair_link, repair_link
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import discover_observations
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    LISTEN_TIMEOUT,
    VEGGIES,
    assert_reachable,
    build_bed_host,
    random_outfile,
    remove_remote_file,
    spawn_udp_listener,
    wait_for_listener_output,
)
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_DEGRADE,
    PORT_IMPAIRED,
    SOAK_CYCLES,
    soak_timeout,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=240.0)),
]

_DP_DEV = "eth2"
_DP_SUBNET = "192.168.1.0/24"
_CARROT_DP_IP = "192.168.1.11"
_TOMATO_DP_IP = "192.168.1.12"
_HOST_CMD_TIMEOUT = 30
_PROBE_COUNT = 30  # ≥1 of 30 must arrive; at 10% loss/direction P(all lost) ≈ 1e-30


async def _assert_dataplane_provisioned(host, ip: str) -> None:
    """Fail LOUD (with redeploy instructions) if the bed predates the eth2 NIC."""
    result = await host.exec(
        f"ip -o addr show dev {_DP_DEV} 2>/dev/null || true",
        timeout=_HOST_CMD_TIMEOUT,
        log=LogMode.QUIET,
    )
    assert ip in (result.value or ""), (
        f"{host.id}: {_DP_DEV} does not carry {ip} — the bed predates the dedicated "
        f"data-plane NIC; run 'vagrant reload test1 test2 test3' (halt+up, not a bare "
        f"provision) with the current Vagrantfile"
    )


@pytest_asyncio.fixture
async def dataplane_lab():
    """2-host lab whose carrot/tomato carry the declared eth2 data plane."""
    for ne in VEGGIES:
        await assert_reachable(ne, host_data(ne)["ip"])
    lab = Lab(name="tunnel_adversity")
    carrot = build_bed_host(
        "carrot", interfaces={_DP_DEV: Interface(ip=_CARROT_DP_IP, subnet=_DP_SUBNET)}
    )
    tomato = build_bed_host(
        "tomato", interfaces={_DP_DEV: Interface(ip=_TOMATO_DP_IP, subnet=_DP_SUBNET)}
    )
    lab.add_host(carrot)
    lab.add_host(tomato)
    await _assert_dataplane_provisioned(carrot, _CARROT_DP_IP)
    await _assert_dataplane_provisioned(tomato, _TOMATO_DP_IP)

    lab.links.append(
        Link(
            a=LinkEndpoint(host=INGRESS, interface=_DP_DEV, ip=_CARROT_DP_IP),
            b=LinkEndpoint(host=EXIT, interface=_DP_DEV, ip=_TOMATO_DP_IP),
            name="soak-seg",
        )
    )
    try:
        yield lab
    finally:
        with contextlib.suppress(Exception):
            await repair_link(lab, "soak-seg")
        # The netdev is permanent bed infrastructure — prove repair left it
        # pristine rather than deleting it.
        for host in (carrot, tomato):
            qdisc = await host.exec(
                f"tc qdisc show dev {_DP_DEV}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
            )
            assert "netem" not in (qdisc.value or ""), (
                f"{host.id}: lingering netem on {_DP_DEV} after repair: {qdisc.value!r}"
            )
        await asyncio.gather(*(h.close() for h in (carrot, tomato)), return_exceptions=True)


async def _send_udp_from(host, ip: str, port: int, payloads: list[str]) -> None:
    """Fire datagrams from *host* (the dev VM has no data-plane address)."""
    lines = "\\n".join(payloads)
    script = (
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        f"for line in \"{lines}\".split(\"\\n\"):\n"
        f"    s.sendto(line.encode(), ({ip!r}, {port}))\n"
        "    time.sleep(0.05)\n"
    )
    await host.exec(
        f"python3 -c {shlex.quote(script)}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
    )


@pytest.mark.asyncio
async def test_churn_under_port_scoped_impairment(dataplane_lab) -> None:
    """delay+loss on ONLY the tunnel's UDP service port, on the eth2 data
    plane (ssh untouched twice over — different netdev AND port-scoped):
    add/verify/remove stays fully correct every cycle; a lossy traffic probe
    (30 datagrams) still delivers at least one."""
    # NOTE: the shared reap_tunnels fixture reaps against tunnel_lab, which
    # this test does not use — requesting it would build three extra host
    # connections for nothing. Track and reap against dataplane_lab manually,
    # with the same guarantees (finally below + module-final sweep).
    created: list[str] = []
    tomato = dataplane_lab.hosts[EXIT]
    chain = [(INGRESS, _DP_DEV), (EXIT, _DP_DEV)]
    sel = Selector(PORT_IMPAIRED, "udp")
    await impair_link(
        dataplane_lab, "soak-seg", ImpairmentParams(delay_ms=80.0, loss_pct=10.0), selector=sel
    )
    try:
        for cycle in range(SOAK_CYCLES):
            added = await add_tunnel(dataplane_lab, chain, port=PORT_IMPAIRED, protocol="udp")
            created.append(added.tunnel.id)
            discovery = await discover_tunnels(dataplane_lab)
            found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
            assert found is not None and found.status == "ok", (
                f"cycle {cycle}: control plane wrong under impairment: "
                f"{found.status if found else 'not discovered'!r}"
            )
            if cycle in (0, SOAK_CYCLES - 1):
                outfile = random_outfile()
                tag = uuid.uuid4().hex[:8]
                payloads = [f"{tag}-{i}" for i in range(_PROBE_COUNT)]
                try:
                    await spawn_udp_listener(
                        tomato, PORT_IMPAIRED, outfile, timeout=LISTEN_TIMEOUT
                    )
                    # Sender runs ON tomato (the dev VM has no data-plane
                    # address): datagrams enter carrot's eth2 ingress, ride
                    # the tunnel back, and land on tomato's loopback listener.
                    await _send_udp_from(tomato, _CARROT_DP_IP, PORT_IMPAIRED, payloads)
                    received = await wait_for_listener_output(tomato, outfile)
                    assert received.split(" ", 1)[-1].startswith(tag), (
                        f"cycle {cycle}: no probe datagram delivered under 10% loss "
                        f"({_PROBE_COUNT} sent): {received!r}"
                    )
                finally:
                    await remove_remote_file(tomato, outfile)
            report = await remove_tunnel(dataplane_lab, added.tunnel.id)
            assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
            created.remove(added.tunnel.id)
    finally:
        for tunnel_id in created:
            with contextlib.suppress(Exception):
                await remove_tunnel(dataplane_lab, tunnel_id)
        with contextlib.suppress(Exception):
            await repair_link(dataplane_lab, "soak-seg")


@pytest.mark.asyncio
async def test_repeated_degrade_recover(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x (add -> out-of-band kill one hop's pids -> 'degraded (...)' ->
    remove reaps the remainder -> the SAME spec re-adds cleanly). The re-add is
    the loop's next iteration: degradation must leave no residue on the port
    or id."""
    carrot = tunnel_lab.hosts[INGRESS]
    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, chain, port=PORT_DEGRADE, protocol="udp")
        reap_tunnels.append(added.tunnel.id)

        observations, _unreachable = await discover_observations(tunnel_lab)
        pids = [
            obs.pid
            for origin, obs in observations
            if obs.parsed.tunnel.id == added.tunnel.id and origin == INGRESS
        ]
        assert pids, f"cycle {cycle}: no tagged pids on {INGRESS!r} before kill"
        result = await carrot.exec(kill_command(pids), timeout=15, log=LogMode.QUIET)
        assert result.is_ok, f"cycle {cycle}: out-of-band kill failed: {result.value!r}"

        degraded = await discover_tunnels(tunnel_lab)
        found = next((d for d in degraded.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"cycle {cycle}: tunnel vanished after partial kill"
        assert found.status.startswith("degraded ("), (
            f"cycle {cycle}: expected degraded, got {found.status!r}"
        )
        assert found.health == "degraded", f"cycle {cycle}: health {found.health!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
```

- [ ] **Step 2: Verify collection, then run live at CYCLES=2**

Run: `uv run pytest tests/e2e/tunnel_stability/test_adversity.py --collect-only -q --no-cov` — expected: 2 tests.
Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_adversity.py --no-cov -q`
Expected: `2 passed` in ~4–8 min. If the fixture's data-plane precheck fires, the bed predates the eth2 NIC — it needs Chris's `vagrant reload test1 test2 test3` with the current Vagrantfile, NOT a test or guard change. Do not proceed past this task on a pre-eth2 bed.

- [ ] **Step 3: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_adversity.py && uv run ruff check tests/e2e/tunnel_stability/test_adversity.py`

```bash
git add tests/e2e/tunnel_stability/test_adversity.py
git commit -m "test(tunnel): adversity soaks — port-scoped impairment churn, repeated degrade/recover

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 9: Health module part 1 — phantom host (`test_health.py`)

**Files:**
- Create: `tests/e2e/tunnel_stability/test_health.py`

**Interfaces:**
- Consumes: conftest fixtures; `otto.host.options.SshOptions`, `otto.host.login_proxy.Cred`; `_harness`; `otto.tunnel.discovery._TUNNEL_HOST_TIMEOUT` (import as private, it IS the budget being asserted).
- Produces: `build_phantom_host()` and `PHANTOM_IP` reused by Task 10's SIGSTOP test in the same module.

- [ ] **Step 1: Write the module (phantom half)**

```python
"""Host-down health detection (spec §4): phantom host (unreachable from the
start) and SIGSTOP wedge (was up, went down mid-life, recovers). No VM is
powered off anywhere; no partition rules are installed."""

import asyncio
import time

import pytest

from otto.host.login_proxy import Cred
from otto.host.options import SshOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import _TUNNEL_HOST_TIMEOUT
from tests._fixtures.labdata import host_data
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORT_PHANTOM_CHAIN,
    PORT_PHANTOM_REAL,
    SOAK_CYCLES,
    soak_timeout,
)

# (Task 10 appends the SIGSTOP test and adds its imports then — `contextlib`,
# `Lab`, `build_bed_host`, `PORT_SIGSTOP`. Importing them now would fail this
# task's lint gate as unused.)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=300.0)),
]

PHANTOM_IP = "10.10.200.99"  # spec §8: must stay outside bed VM allocations
PHANTOM_ID = "phantom_seed"
_SCAN_BUDGET = _TUNNEL_HOST_TIMEOUT + 10.0  # boundedness, measured not assumed


def build_phantom_host() -> UnixHost:
    """A REAL UnixHost at a black-hole ip: real transport, real connect
    timeout (bounded locally at 5s so a phantom scan costs seconds, not the
    full discovery budget)."""
    creds = [Cred(**c) for c in host_data("carrot")["creds"]]
    return UnixHost(
        ip=PHANTOM_IP,
        element="phantom",
        creds=creds,
        term="ssh",
        transfer="scp",
        log=LogMode.QUIET,
        ssh_options=SshOptions(connect_timeout=5),
    )


async def _assert_black_hole() -> None:
    """A live host at PHANTOM_IP is a loud config error, never a false pass."""
    try:
        await asyncio.wait_for(asyncio.open_connection(PHANTOM_IP, 22), timeout=3)
    except (OSError, asyncio.TimeoutError):
        return
    raise AssertionError(
        f"{PHANTOM_IP} answered tcp/22 — the phantom ip is allocated; pick a new one"
    )


@pytest.mark.asyncio
async def test_phantom_host_health_cycle(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x { add-through-phantom fails loud + rolls back; discovery on
    the mixed lab stays bounded, names the phantom, keeps the real tunnel ok;
    remove reports the phantom unreachable }. Cycled because repeated
    timed-out connects are the classic transport/fd leak (the watermark
    fixture is watching)."""
    await _assert_black_hole()
    phantom = build_phantom_host()
    tunnel_lab.add_host(phantom)

    real_chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, real_chain, port=PORT_PHANTOM_REAL, protocol="udp")
        reap_tunnels.append(added.tunnel.id)

        # (a) add through the phantom: loud, named, fully rolled back. The
        # loud shape is either the tunnel layer's host-named RuntimeError
        # (probe/tool-check timeout) or the transport's address-named OSError
        # (fast connect failure) — both name the culprit; pin the naming, not
        # the class.
        with pytest.raises(Exception, match=rf"{PHANTOM_ID}|{PHANTOM_IP}"):
            await add_tunnel(
                tunnel_lab,
                [(INGRESS, None), (PHANTOM_ID, None)],
                port=PORT_PHANTOM_CHAIN,
                protocol="udp",
            )
        rollback_check = await discover_tunnels(tunnel_lab)
        assert not any(
            d.tunnel.service_port == PORT_PHANTOM_CHAIN for d in rollback_check.tunnels
        ), f"cycle {cycle}: failed add left processes behind"

        # (b) discovery: bounded, phantom named, real tunnel unaffected.
        started = time.monotonic()
        discovery = await discover_tunnels(tunnel_lab)
        elapsed = time.monotonic() - started
        assert elapsed < _SCAN_BUDGET, f"cycle {cycle}: scan took {elapsed:.1f}s"
        assert PHANTOM_ID in discovery.unreachable, (
            f"cycle {cycle}: unreachable {discovery.unreachable!r}"
        )
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None and found.status == "ok", (
            f"cycle {cycle}: real tunnel not ok: {found.status if found else 'missing'!r}"
        )

        # (c) remove: phantom reported, real tunnel reaped clean.
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert PHANTOM_ID in report.unreachable, (
            f"cycle {cycle}: remove unreachable {report.unreachable!r}"
        )
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)
```

Note: `tunnel_lab`'s teardown closes every host in the lab — the phantom included (its `close()` is a no-op on a never-connected host, and any half-open attempt dies with the 5s connect ceiling).

The `PHANTOM_ID` name: `make_host`/`UnixHost` slug `element="phantom"` into the host id per the host-id rules (`element` IS the human name). If the constructed id differs from `phantom_seed`, read `phantom.id` after construction and use that — set `PHANTOM_ID = build_phantom_host().id` is NOT allowed at import time (it builds a host at collection); instead assert inside the test: `assert phantom.id == PHANTOM_ID` right after construction, and fix the constant to the real slug on first run.

- [ ] **Step 2: Verify collection, then run live at CYCLES=2**

Run: `uv run pytest tests/e2e/tunnel_stability/test_health.py --collect-only -q --no-cov` — expected: 1 test.
Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_health.py --no-cov -q`
Expected: `1 passed` in ~2–4 min (each phantom-touching scan costs ~5s, three scans per cycle).

- [ ] **Step 3: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_health.py && uv run ruff check tests/e2e/tunnel_stability/test_health.py`

```bash
git add tests/e2e/tunnel_stability/test_health.py
git commit -m "test(tunnel): phantom-host health soak — bounded scans, named unreachables, clean rollback

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 10: Health module part 2 — SIGSTOP wedge

**Files:**
- Modify: `tests/e2e/tunnel_stability/test_health.py` (append)

**Interfaces:**
- Consumes: Task 9's module scaffolding.
- Produces: `wedge helpers` reused by Task 12 (`sshd_listener_pid`, `assert_sshd_responsive` — move to `_harness.py` if Task 12 prefers; they are defined here first).

- [ ] **Step 1: Append the SIGSTOP test**

```python
# --- SIGSTOP wedge: was up, went down mid-life, recovers (spec §4) -----------

_SSHD_PID_CMD = (
    "systemctl show ssh -p MainPID --value 2>/dev/null"
    " || systemctl show sshd -p MainPID --value"
)
_ARM_SECONDS = 180  # auto-CONT well past the wedged phase's worst case
_WEDGED_SCAN_BUDGET = _TUNNEL_HOST_TIMEOUT + 15.0


async def sshd_listener_pid(control) -> int:
    """The sshd LISTENER pid (systemd MainPID) — per-connection children keep
    serving while it is stopped, which is exactly the point: our control
    channel survives, NEW connections hang at the banner. Asserts the MainPID
    is NOT the pid serving this very session (spec §8): stopping that one
    would freeze the control channel we recover through."""
    result = await control.exec(_SSHD_PID_CMD, timeout=15, log=LogMode.QUIET)
    pid = int((result.value or "0").strip().splitlines()[-1])
    assert pid > 0, f"could not resolve sshd MainPID: {result.value!r}"
    session_parent = await control.exec("sh -c 'echo $PPID'", timeout=15, log=LogMode.QUIET)
    assert str(pid) != (session_parent.value or "").strip(), (
        f"MainPID {pid} IS our session's server process — refusing to stop it"
    )
    return pid


async def assert_sshd_responsive(ip: str) -> None:
    """A FRESH connect must produce an SSH banner — connect alone is not
    enough (the kernel backlog completes handshakes for a stopped listener)."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 22), timeout=10)
    try:
        banner = await asyncio.wait_for(reader.readline(), timeout=10)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    assert banner.startswith(b"SSH-"), f"no ssh banner from {ip}: {banner!r}"


def _fresh_two_host_lab() -> Lab:
    """New host objects => new connections => the wedge actually bites (a
    pooled pre-wedge connection would falsely show the host healthy)."""
    lab = Lab(name="tunnel_sigstop_probe")
    for ne in ("carrot", "tomato"):
        lab.add_host(build_bed_host(ne, ssh_options=SshOptions(connect_timeout=5)))
    return lab


@pytest.mark.asyncio
async def test_sigstop_wedge_uncertain_then_recovers(tunnel_lab, reap_tunnels) -> None:
    tomato_ip = host_data("tomato")["ip"]
    control = tunnel_lab.hosts[EXIT]  # established connection; survives the STOP
    added = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORT_SIGSTOP, protocol="udp"
    )
    reap_tunnels.append(added.tunnel.id)

    pid = await sshd_listener_pid(control)
    stopped = False
    try:
        # Arm auto-recovery BEFORE stopping: a failed teardown cannot wedge the bed.
        await control.exec(
            f"sudo -n setsid sh -c 'sleep {_ARM_SECONDS}; kill -CONT {pid}' "
            f"</dev/null >/dev/null 2>&1 &",
            timeout=15,
            log=LogMode.QUIET,
        )
        stop_result = await control.exec(f"sudo -n kill -STOP {pid}", timeout=15, log=LogMode.QUIET)
        assert stop_result.is_ok, f"kill -STOP failed: {stop_result.value!r}"
        stopped = True

        # Fresh lab: tomato unreachable, tunnel 'uncertain', scan bounded.
        wedged_lab = _fresh_two_host_lab()
        try:
            started = time.monotonic()
            discovery = await discover_tunnels(wedged_lab)
            elapsed = time.monotonic() - started
            assert elapsed < _WEDGED_SCAN_BUDGET, f"wedged scan took {elapsed:.1f}s"
            assert EXIT in discovery.unreachable, f"unreachable: {discovery.unreachable!r}"
            found = next(
                (d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None
            )
            assert found is not None, "tunnel vanished during the wedge"
            assert found.health == "uncertain", (
                f"expected 'uncertain' (unknown, not missing), got {found.health!r} "
                f"/ status {found.status!r}"
            )
        finally:
            await asyncio.gather(
                *(h.close() for h in wedged_lab.hosts.values()), return_exceptions=True
            )

        # Recover, then prove it through ANOTHER fresh lab.
        cont_result = await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        assert cont_result.is_ok, f"kill -CONT failed: {cont_result.value!r}"
        stopped = False
        await assert_sshd_responsive(tomato_ip)

        recovered_lab = _fresh_two_host_lab()
        try:
            discovery = await discover_tunnels(recovered_lab)
            found = next(
                (d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None
            )
            assert found is not None and found.health == "ok", (
                f"post-recovery health: {found.health if found else 'missing'!r}"
            )
        finally:
            await asyncio.gather(
                *(h.close() for h in recovered_lab.hosts.values()), return_exceptions=True
            )

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == []
        reap_tunnels.remove(added.tunnel.id)
    finally:
        if stopped:  # test body failed mid-wedge: recover NOW, loudly if we can't
            with contextlib.suppress(Exception):
                await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        try:
            await assert_sshd_responsive(tomato_ip)
        except Exception as exc:  # noqa: BLE001 — bed-state instructions belong in the failure
            raise AssertionError(
                f"tomato sshd is NOT responsive after the SIGSTOP test — the armed "
                f"auto-CONT fires within {_ARM_SECONDS}s; if it doesn't, run "
                f"'sudo kill -CONT {pid}' on test2 or 'make vm-health': {exc!r}"
            ) from exc
```

Add to the module's imports (top of file): `import contextlib`, `from otto.config.lab import Lab`, `from tests._fixtures.tunnel_bed import build_bed_host`, and `PORT_SIGSTOP` in the existing `_harness` import list — Task 9 deliberately left them out to keep its lint gate clean.

- [ ] **Step 2: Run the SIGSTOP test live**

Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_health.py::test_sigstop_wedge_uncertain_then_recovers --no-cov -q`
Expected: `1 passed` in ~2–3 min (one wedged scan bounded at ≤45s dominates). Afterward verify bed hygiene by hand once: `ssh vagrant@10.10.200.12 true` succeeds.

- [ ] **Step 3: Run the whole health module live, lint, commit**

Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_health.py --no-cov -q`
Expected: `2 passed`.

Run: `uv run ruff format tests/e2e/tunnel_stability/test_health.py && uv run ruff check tests/e2e/tunnel_stability/test_health.py`

```bash
git add tests/e2e/tunnel_stability/test_health.py
git commit -m "test(tunnel): SIGSTOP-wedge health — uncertain during, ok after, bed never left wedged

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 11: Monitor unit tick soak (`test_collector_tunnel_soak.py`)

**Files:**
- Create: `tests/unit/monitor/test_collector_tunnel_soak.py`

**Interfaces:**
- Consumes: `MetricCollector`, `TunnelRecord` (same fakes style as `tests/unit/monitor/test_collector_tunnels.py` — `_Script`, `_rec` are re-declared here, files must be independently readable).
- Produces: leaf module. Marked `concurrency` (spec §2): rides `make stability-unit` ×50 AND stays in `make coverage`.

- [ ] **Step 1: Write the failing-able test module**

```python
"""Long-sequence soak of the collector's tunnel loop (spec §5): last-known
state under alternating healthy/raising scans, loop survival, warn-once latch,
no state growth. Single-shot guards live in test_collector_tunnels.py — these
are the N-tick extensions. Marked `concurrency`: no-VM, rides stability-unit."""

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest

from otto.models.monitor import TunnelRecord
from otto.monitor.collector import MetricCollector

pytestmark = pytest.mark.concurrency


def _rec(tid: str, status: str = "ok") -> TunnelRecord:
    return TunnelRecord(
        id=tid,
        protocol="udp",
        service_port=15160,
        hops=["a", "b"],
        status=status,  # type: ignore[arg-type]
        carriers_present=4,
        carriers_expected=4,
    )


class _Script:
    """A tunnel_source scripted per call: a list -> that set; an Exception -> raise.
    Unlike test_collector_tunnels.py's, this one CYCLES its script forever."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> list[TunnelRecord]:
        result = self._results[self.calls % len(self._results)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _collector(source: _Script) -> MetricCollector:
    c = MetricCollector(hosts=[], tunnel_source=source)
    c.session_id = "soak"
    c._publish = lambda frag: None  # type: ignore[method-assign]
    return c


def test_long_alternating_sequence_never_blanks() -> None:
    """60 ticks of healthy/failing/degraded/empty interleave: after every tick
    the retained set equals the LAST SUCCESSFUL scan — a failure never blanks,
    a success always replaces."""
    healthy = [_rec("tun-a-1")]
    degraded = [_rec("tun-a-1", status="degraded"), _rec("tun-b-2")]
    script = [healthy, RuntimeError("down"), degraded, RuntimeError("down"), [], healthy]
    source = _Script(*script)
    c = _collector(source)
    expected_last: list[TunnelRecord] = []
    for tick in range(60):
        step = script[tick % len(script)]
        if not isinstance(step, Exception):
            expected_last = step
        asyncio.run(c._tunnel_pass())
        assert c.get_tunnel_records() == expected_last, f"tick {tick}: retained set wrong"
        assert len(c.get_tunnel_records()) <= 2, f"tick {tick}: state grew"


def test_warn_latch_across_repeated_failure_bursts(caplog: pytest.LogCaptureFixture) -> None:
    """Each failure BURST warns exactly once (plus one recovery warn); a
    10-tick burst is not 10 warnings."""
    source = _Script(
        [_rec("tun-a-1")],
        *([RuntimeError("down")] * 10),
        [_rec("tun-a-1")],
        *([RuntimeError("down")] * 10),
        [_rec("tun-a-1")],
    )
    c = _collector(source)
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.collector"):
        for _ in range(23):
            asyncio.run(c._tunnel_pass())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # burst1 fail + burst1 recovery + burst2 fail + burst2 recovery = 4
    assert len(warnings) == 4, [w.message for w in warnings]


def test_raising_source_never_kills_the_run_loop() -> None:
    """run() with a source that raises on every other call: the loop keeps
    ticking to the end of its duration instead of dying at the first raise."""
    from otto.monitor.collector import MonitorTarget

    class _Host:
        name = "h1"
        id = "h1"

        async def run(self, commands: list[str], **kwargs: Any) -> Any:
            raise RuntimeError("no shell in this test")

    source = _Script([_rec("tun-a-1")], RuntimeError("down"))
    c = MetricCollector(
        targets=[MonitorTarget(host=_Host(), parsers={})],  # type: ignore[arg-type]
        tunnel_source=source,
    )
    asyncio.run(c.run(interval=timedelta(milliseconds=10), duration=timedelta(milliseconds=120)))
    assert source.calls >= 4, f"loop died early: only {source.calls} scans"
    assert c.get_tunnel_records() == [_rec("tun-a-1")]
```

- [ ] **Step 2: Run — expect green (regression-style guards over landed behavior)**

Run: `uv run pytest tests/unit/monitor/test_collector_tunnel_soak.py --no-cov -q`
Expected: `3 passed`.

- [ ] **Step 3: Prove each guard CAN fail (mutation check — the ten-guards lesson)**

Temporarily apply this mutation to `src/otto/monitor/collector.py::_tunnel_pass` — in the `except` branch, add `self._tunnels = []` as its first line — then run Step 2's command.
Expected: `test_long_alternating_sequence_never_blanks` and `test_raising_source_never_kills_the_run_loop` FAIL.
Second mutation: in the same `except` branch, replace the `if not self._tunnel_scan_failing:` gate with `if True:`. Run again — expected: `test_warn_latch_across_repeated_failure_bursts` FAILS.
**Revert both mutations** (`git checkout -- src/otto/monitor/collector.py`) and re-run — expected: `3 passed`.

- [ ] **Step 4: Verify tier membership, lint, commit**

Run: `uv run pytest -m concurrency tests/unit/monitor --collect-only -q --no-cov 2>/dev/null | tail -1`
Expected: includes the 3 new tests.

Run: `uv run ruff format tests/unit/monitor/test_collector_tunnel_soak.py && uv run ruff check tests/unit/monitor/test_collector_tunnel_soak.py`

```bash
git add tests/unit/monitor/test_collector_tunnel_soak.py
git commit -m "test(monitor): collector tunnel-loop tick soak — never-blank, warn-latch, loop survival

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 12: Live monitor loop under churn (`test_monitor_loop.py`)

**Files:**
- Create: `tests/e2e/tunnel_stability/test_monitor_loop.py`

**Interfaces:**
- Consumes: conftest fixtures; `MetricCollector`; `discover_tunnel_records` (the production composition-site wiring, `src/otto/cli/monitor.py:225`); Task 10's `sshd_listener_pid`/`assert_sshd_responsive` (import from `tests.e2e.tunnel_stability.test_health`).
- Produces: leaf module. Docker-free throughout (monitor e2e rule): drives the Collector directly — no web server, no browser.

- [ ] **Step 1: Write the module**

```python
"""Live Collector tunnel loop over the bed (spec §5): convergence after each
churn settle (one deterministic _tunnel_pass = one tick), CLI/monitor seam
parity, and last-known-state under a SIGSTOP wedge. Docker-free by design."""

import asyncio
import contextlib
import time

import pytest

from otto.host.options import SshOptions
from otto.logger.mode import LogMode
from otto.monitor.collector import MetricCollector
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import _TUNNEL_HOST_TIMEOUT
from otto.tunnel.records import discover_tunnel_records
from tests._fixtures.labdata import host_data
from tests.e2e.tunnel_stability._harness import (
    EXIT,
    INGRESS,
    PORTS_MONITOR_CHURN,
    SOAK_CYCLES,
    soak_timeout,
)
from tests.e2e.tunnel_stability.test_health import assert_sshd_responsive, sshd_listener_pid

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=300.0)),
]

_TICK_BUDGET = _TUNNEL_HOST_TIMEOUT + 15.0
_ARM_SECONDS = 180


def _spy_collector(lab) -> tuple[MetricCollector, list[dict]]:
    """Composition-site wiring (cli/monitor.py:225) with spy sinks: real
    discovery over the real bed, no web server."""
    published: list[dict] = []
    c = MetricCollector(hosts=[], tunnel_source=lambda: discover_tunnel_records(lab))
    c.session_id = "tunnel-soak"
    c._publish = published.append  # type: ignore[method-assign]
    return c, published


async def _timed_pass(collector: MetricCollector) -> float:
    started = time.monotonic()
    await collector._tunnel_pass()
    return time.monotonic() - started


@pytest.mark.asyncio
async def test_loop_converges_with_churn_and_seam_parity(tunnel_lab, reap_tunnels) -> None:
    """After each churn settle, ONE pass converges the record set to the live
    set; each record's status equals the DiscoveredTunnel.health the CLI
    reads for the same bed state (seam parity, asserted not assumed)."""
    collector, published = _spy_collector(tunnel_lab)
    elapsed = await _timed_pass(collector)
    assert elapsed < _TICK_BUDGET
    assert collector.get_tunnel_records() == [], "bed not clean at start"

    chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(
            tunnel_lab, chain, port=PORTS_MONITOR_CHURN[0], protocol="udp"
        )
        reap_tunnels.append(added.tunnel.id)
        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"cycle {cycle}: tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id], f"cycle {cycle}: {records!r}"

        discovery = await discover_tunnels(tunnel_lab)
        found = next(d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id)
        assert records[0].status == found.health, (
            f"cycle {cycle}: wire status {records[0].status!r} != CLI health {found.health!r}"
        )
        assert records[0].hops == [INGRESS, EXIT], f"cycle {cycle}: hops {records[0].hops!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == []
        reap_tunnels.remove(added.tunnel.id)
        await _timed_pass(collector)
        assert collector.get_tunnel_records() == [], f"cycle {cycle}: not converged to empty"
    assert published, "collector never published a fragment"


@pytest.mark.asyncio
async def test_loop_holds_last_known_under_wedge_then_reconverges(
    tunnel_lab, reap_tunnels
) -> None:
    """SIGSTOP tomato's sshd listener mid-monitoring: ticks keep completing
    within budget, the tunnel is HELD (as 'uncertain', never blanked), and the
    set reconverges to 'ok' after CONT."""
    from otto.config.lab import Lab
    from tests._fixtures.tunnel_bed import build_bed_host

    control = tunnel_lab.hosts[EXIT]
    tomato_ip = host_data("tomato")["ip"]

    # The collector scans its OWN lab (fresh host objects) so that closing a
    # host forces the next scan through a brand-new connection — a pooled
    # pre-wedge connection would never feel the wedge.
    monitor_lab = Lab(name="monitor_wedge")
    for ne in ("carrot", "tomato"):
        monitor_lab.add_host(build_bed_host(ne, ssh_options=SshOptions(connect_timeout=5)))
    collector, _published = _spy_collector(monitor_lab)

    added = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORTS_MONITOR_CHURN[1], protocol="udp"
    )
    reap_tunnels.append(added.tunnel.id)
    pid = await sshd_listener_pid(control)
    stopped = False
    try:
        await _timed_pass(collector)
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id]
        assert records[0].status == "ok"

        await control.exec(
            f"sudo -n setsid sh -c 'sleep {_ARM_SECONDS}; kill -CONT {pid}' "
            f"</dev/null >/dev/null 2>&1 &",
            timeout=15,
            log=LogMode.QUIET,
        )
        stop_result = await control.exec(f"sudo -n kill -STOP {pid}", timeout=15, log=LogMode.QUIET)
        assert stop_result.is_ok
        stopped = True
        await monitor_lab.hosts[EXIT].close()  # next scan opens a fresh, wedged connection

        elapsed = await _timed_pass(collector)
        assert elapsed < _TICK_BUDGET, f"wedged tick took {elapsed:.1f}s"
        records = collector.get_tunnel_records()
        assert [r.id for r in records] == [added.tunnel.id], "wedge blanked the set"
        assert records[0].status == "uncertain", f"held status {records[0].status!r}"

        cont = await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        assert cont.is_ok
        stopped = False
        await assert_sshd_responsive(tomato_ip)
        await monitor_lab.hosts[EXIT].close()  # reconnect cleanly post-recovery

        await _timed_pass(collector)
        records = collector.get_tunnel_records()
        assert records and records[0].status == "ok", f"did not reconverge: {records!r}"

        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert report.survivors == []
        reap_tunnels.remove(added.tunnel.id)
        await _timed_pass(collector)
        assert collector.get_tunnel_records() == []
    finally:
        if stopped:
            with contextlib.suppress(Exception):
                await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        await asyncio.gather(
            *(h.close() for h in monitor_lab.hosts.values()), return_exceptions=True
        )
        try:
            await assert_sshd_responsive(tomato_ip)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"tomato sshd NOT responsive after wedge test — auto-CONT fires within "
                f"{_ARM_SECONDS}s; else 'sudo kill -CONT {pid}' on test2: {exc!r}"
            ) from exc
```

- [ ] **Step 2: Verify collection, then run live at CYCLES=2**

Run: `uv run pytest tests/e2e/tunnel_stability/test_monitor_loop.py --collect-only -q --no-cov` — expected: 2 tests.
Run: `OTTO_TUNNEL_SOAK_CYCLES=2 uv run pytest tests/e2e/tunnel_stability/test_monitor_loop.py --no-cov -q`
Expected: `2 passed` in ~4–6 min.

- [ ] **Step 3: Lint, format, commit**

Run: `uv run ruff format tests/e2e/tunnel_stability/test_monitor_loop.py && uv run ruff check tests/e2e/tunnel_stability/test_monitor_loop.py`

```bash
git add tests/e2e/tunnel_stability/test_monitor_loop.py
git commit -m "test(monitor): live tunnel-loop soak — convergence, seam parity, wedge holds last-known

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 13: Documentation pass

**Files:**
- Modify: `docs/contributing.md` (the Regression-test categories section around lines 300–312; the tier-table row was already updated in Task 4)

**Interfaces:** none (prose only).

- [ ] **Step 1: Document the tier and its knobs**

In `docs/contributing.md`, directly under the command table (after line ~312), add:

```markdown
`make stability-tunnel` soaks the tunnel machinery against the live bed:
add/remove churn (2- and 3-hop), concurrent populations, racing adds,
discovery-under-churn, a traffic soak, port-scoped-impairment churn,
degrade/recover cycling, and host-down health (phantom ip + SIGSTOP — no VM
is ever powered off). `COUNT=N` repeats the whole suite (default 1);
`CYCLES=N` sets each test's internal loop depth (default 5; `CYCLES=2` is the
smoke setting). These tests carry `stability + integration + hops`;
`stability-unix` excludes them via `not hops`. The no-VM collector tick soak
(`tests/unit/monitor/test_collector_tunnel_soak.py`) is marked `concurrency`
instead — it rides `make stability-unit` and stays in coverage.
```

- [ ] **Step 2: Docs gate**

Run: `make docs-lint`
Expected: clean. (A full `make docs` clean rebuild is not needed: this change is prose in one markdown file with no `:doc:`/autodoc references — the incremental-`-W` trap doesn't apply.)

- [ ] **Step 3: Commit**

```bash
git add docs/contributing.md
git commit -m "docs: document the stability-tunnel tier, its CYCLES/COUNT knobs, and marker algebra

Assisted-by: Claude (claude-fable-5)"
```

---

### Task 14: Final verification — full tier at defaults + gates

**Files:** none (verification only).

- [ ] **Step 1: Full suite, default depth**

Run: `make stability-tunnel`
Expected: all tests pass (≈20–25 min single pass at CYCLES=5). Do NOT interrupt a slow run (dev-VM rule).

- [ ] **Step 2: Verify via JUnit, not exit-code piping**

Run: `uv run python scripts/junit_failures.py reports/junit/stability-tunnel/stability-tunnel.xml`
Expected: no failures listed (this sidesteps the `make ... | tail` exit-code trap).

- [ ] **Step 3: Selection-algebra regression**

Run: `uv run pytest -m "stability and integration and not embedded and not hops" --collect-only -q --no-cov 2>/dev/null | tail -1`
Expected: the same N recorded in Task 4 Step 1.

Run: `uv run pytest -m "not stability and not browser" --collect-only -q --no-cov 2>/dev/null | grep -c tunnel_stability || true`
Expected: `0`.

- [ ] **Step 4: Repo gates**

Run: `make lint && make typecheck`
Expected: clean (typecheck matters if Task 6 touched `src/`).

Run: `make coverage` — the standard per-task gate; the new `concurrency`-marked unit soak runs inside it once, everything `stability`-marked stays out. Verify the gate result via `uv run python scripts/junit_failures.py reports/junit/coverage/coverage.xml`.

- [ ] **Step 5: Hand off**

No commit here. Summarize: tier green at defaults, selection algebra proven stable, gates green — ready for the finishing-a-development-branch flow (merge decision is Chris's).
