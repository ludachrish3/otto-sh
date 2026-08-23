"""Live-bed e2e tests for ``otto.link`` impairment (Task 11).

Drives the real ``otto.link`` library API (``impair_link`` / ``repair_link`` /
``repair_all`` / ``read_link_states``) against the three-VM unix bed,
mirroring ``tests/e2e/test_tunnel_e2e.py``: host construction via
``host_data``/``make_host``, fail-loud-on-host-down, guaranteed teardown, and
a single ``xdist_group`` so this fixed topology never runs concurrently with
itself.

Topology (fixture-created over real ``sudo ip``/``tc``, torn down after)
--------------------------------------------------------------------------
The peers' only shared network (``10.10.200.0/24`` on ``eth1``) is the mgmt
path, so this module builds a VLAN data plane instead -- verified live: VLAN
tags pass the VirtualBox network, and ``tc qdisc replace ... netem`` on a VLAN
sub-interface moves ping RTT without touching the untagged mgmt path.

- VLAN 100 (``10.10.201.0/24``): test1 ``eth1.100`` = .11, test3
  ``eth1.100`` = .13
- VLAN 200 (``10.10.202.0/24``): test2 ``eth1.200`` = .12, test3
  ``eth1.200`` = .13
- test3: ``net.ipv4.ip_forward=1`` (prior value recorded and restored)
- test1: route to ``10.10.202.0/24`` via test3's VLAN-100 address; test2:
  route to ``10.10.201.0/24`` via test3's VLAN-200 address (both routes die
  with the VLAN links they ride)
- test3: two narrow ``iptables FORWARD`` ACCEPT rules, ``eth1.100<->eth1.200``
  only -- discovered live (not in the original brief): test3 is
  docker-capable, and Docker's own iptables integration installs a default
  ``FORWARD DROP`` policy plus its own accept chains, so the VLAN forwarding
  path is silently dropped without this. Deleted on teardown before the VLAN
  links themselves (a dangling rule referencing a since-deleted interface
  name is otherwise harmless but pointless to leave behind).
- Links, constructed as runtime ``Link`` objects on ``lab.links`` (no
  ``lab.json`` file involved):

  - ``edge`` -- test1@eth1.100 <-> test3@eth1.100 (endpoint-mode target,
    same subnet, direct)
  - ``dataplane`` -- test1@eth1.100 <-> test2@eth1.200, ``impair="test3"``
    (in-path target; traffic genuinely routes through test3)

Guaranteed teardown
--------------------
``impair_lab`` is a module-scoped fixture (setup pays the VLAN/route/sysctl
cost once for all 5 tests): its ``finally`` block repairs both links, deletes
the VLAN sub-interfaces on all three peers (which removes any qdiscs and
routes still riding them), restores test3's prior ``ip_forward``, and closes
every host -- each step individually suppressed so one failure never skips
the rest.

``_final_leftover_sweep`` is a separate, autouse, module-scoped, *synchronous*
fixture (own throwaway ``asyncio.run``, fresh hosts -- mirrors
``test_tunnel_e2e.py``'s ``_final_leftover_sweep``) that does a final bed scan
after every test in this module has run: no ``otto-impair:`` timer processes,
no netem on the mgmt ``eth1`` (the load-bearing safety assertion), no VLAN
sub-interfaces left behind. Autouse fixtures instantiate before explicitly
requested ones within the same scope (a documented pytest guarantee), so this
sweep is set up *before* ``impair_lab`` and therefore torn down *after* it --
its assertions only ever run once ``impair_lab``'s own teardown (and its
module-scoped event loop) has fully completed.

xdist / dev-VM rules
---------------------
Pinned to a single ``xdist_group`` (never runs concurrently with itself) and
intended as a single pass per the dev-VM load rule (no heavy parallel load,
never power VMs, never SIGTERM a wedged live-bed run at a tight timeout).
Host-down is a loud ``RuntimeError`` naming the unreachable VM -- never a
``pytest.skip``.
"""

import asyncio
import contextlib

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.host.interface import Interface
from otto.host.unix_host import UnixHost
from otto.link import (
    FlowDirection,
    ImpairmentParams,
    Link,
    LinkEndpoint,
    equivalent,
    impair_link,
    read_link_states,
    repair_all,
    repair_link,
)
from otto.link.netem import parse_qdisc_show
from otto.link.sentinel import IMPAIR_PS_COMMAND, parse_impair_ps
from otto.logger.mode import LogMode
from otto.utils import wait_for_async
from tests._fixtures.bed_hygiene import argv_pattern
from tests._fixtures.labdata import host_data, make_host

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_impair_e2e"),
]

_TEST1 = "test1"  # test1, 10.10.200.11
_TEST2 = "test2"  # test2, 10.10.200.12
_TEST3 = "test3"  # test3, 10.10.200.13

_SSH_PORT = 22
_REACHABLE_TIMEOUT = 10
_HOST_CMD_TIMEOUT = 15.0

_VLAN100_DEV = "eth1.100"
_VLAN200_DEV = "eth1.200"
_VLAN100_ID = 100
_VLAN200_ID = 200
_VLAN100_NET = "10.10.201.0/24"
_VLAN200_NET = "10.10.202.0/24"

_TEST1_VLAN_IP = "10.10.201.11"
_TEST3_VLAN100_IP = "10.10.201.13"
_TEST2_VLAN_IP = "10.10.202.12"
_TEST3_VLAN200_IP = "10.10.202.13"

# Narrow FORWARD-chain holes for test3's Docker-installed default DROP
# policy (see the module docstring's topology section) -- ONLY between the
# two VLAN sub-interfaces, nothing broader.
_FORWARD_RULES = (
    f"-i {_VLAN100_DEV} -o {_VLAN200_DEV} -j ACCEPT",
    f"-i {_VLAN200_DEV} -o {_VLAN100_DEV} -j ACCEPT",
)

_EXPIRE_SECONDS = 8
_EXPIRE_POLL_INTERVAL = 2.0
_EXPIRE_POLL_MAX = 60.0

_RTT_DELTA_MIN_MS = 150.0  # 100ms delay each way -> ~200ms RTT delta, generous margin
_RTT_RESTORE_TOLERANCE_MS = 20.0

_SCOPED_PORT = 5201
_CLEAN_PORT = 5202
_SCOPED_DELAY_MS = 200.0
_TCP_DELTA_MIN_MS = 150.0

_VLAN_INTERFACES: dict[str, dict[str, Interface]] = {
    "test1": {_VLAN100_DEV: Interface(ip=_TEST1_VLAN_IP)},
    "test2": {_VLAN200_DEV: Interface(ip=_TEST2_VLAN_IP)},
    "test3": {
        _VLAN100_DEV: Interface(ip=_TEST3_VLAN100_IP),
        _VLAN200_DEV: Interface(ip=_TEST3_VLAN200_IP),
    },
}


# ---------------------------------------------------------------------------
# Host / lab construction
# ---------------------------------------------------------------------------


def _build_host(ne: str) -> UnixHost:
    """Build a real ``UnixHost`` from the unix lab data, wired with its VLAN sub-interface(s)."""
    return make_host(
        ne, term="ssh", transfer="scp", log=LogMode.QUIET, interfaces=_VLAN_INTERFACES[ne]
    )


async def _assert_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) on a down VM -- never skip (dev-VM rule)."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, _SSH_PORT), timeout=_REACHABLE_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element} ({ip}) unreachable on :{_SSH_PORT} -- bed down? "
            f"(link impairment e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def _root(host: UnixHost, cmd: str) -> None:
    """Run *cmd* on *host* with sudo; assert it actually succeeded (fixture setup only)."""
    result = (await host.run(cmd, sudo=True, log=LogMode.QUIET)).only
    assert result.is_ok, f"{host.id}: {cmd!r} failed: {result.value!r}"


async def _root_best_effort(host: UnixHost, cmd: str) -> None:
    """Run *cmd* on *host* with sudo, tolerating failure -- idempotent cleanup."""
    await host.run(f"{cmd} 2>/dev/null || true", sudo=True, log=LogMode.QUIET)


async def _add_vlan(host: UnixHost, dev: str, vlan_id: int, ip_cidr: str) -> None:
    """Create VLAN sub-interface *dev* on *host*'s ``eth1``, address it, and bring it up."""
    await _root(host, f"ip link add link eth1 name {dev} type vlan id {vlan_id}")
    await _root(host, f"ip addr add {ip_cidr} dev {dev}")
    await _root(host, f"ip link set {dev} up")


async def _avg_rtt_ms(host: UnixHost, target_ip: str) -> float:
    """Ping *target_ip* from *host* three times and return the average RTT in ms."""
    result = await host.exec(
        f"ping -c 3 -i 0.3 -W 2 {target_ip}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
    )
    assert result.is_ok, f"ping {target_ip} from {host.id!r} failed: {result.value!r}"
    marker = "rtt min/avg/max/mdev"
    for line in (result.value or "").splitlines():
        if marker in line:
            return float(line.split("=")[1].split("/")[1])
    raise AssertionError(
        f"no {marker!r} line in ping output from {host.id!r} to {target_ip}: {result.value!r}"
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def impair_lab():
    """Real ``Lab`` over a VLAN data plane built on the 3-VM unix bed.

    ``loop_scope="module"`` matches the fixture scope so pytest-asyncio backs
    it with a persistent event loop the fixture (and its dependent tests)
    outlive their creating test under -- the default ``function`` loop_scope
    would close after the first test and corrupt the cached SSH connections.
    """
    for ne in ("test1", "test2", "test3"):
        await _assert_reachable(ne, host_data(ne)["ip"])

    lab = Lab(name="impair_e2e")
    test1 = _build_host("test1")
    test2 = _build_host("test2")
    test3 = _build_host("test3")
    for host in (test1, test2, test3):
        lab.add_host(host)

    # Idempotent against a crashed prior run: pre-delete before creating.
    await _root_best_effort(test1, f"ip link del {_VLAN100_DEV}")
    await _root_best_effort(test3, f"ip link del {_VLAN100_DEV}")
    await _root_best_effort(test3, f"ip link del {_VLAN200_DEV}")
    await _root_best_effort(test2, f"ip link del {_VLAN200_DEV}")
    for rule in _FORWARD_RULES:
        await _root_best_effort(test3, f"iptables -D FORWARD {rule}")

    await _add_vlan(test1, _VLAN100_DEV, _VLAN100_ID, f"{_TEST1_VLAN_IP}/24")
    await _add_vlan(test3, _VLAN100_DEV, _VLAN100_ID, f"{_TEST3_VLAN100_IP}/24")
    await _add_vlan(test2, _VLAN200_DEV, _VLAN200_ID, f"{_TEST2_VLAN_IP}/24")
    await _add_vlan(test3, _VLAN200_DEV, _VLAN200_ID, f"{_TEST3_VLAN200_IP}/24")

    prior_ip_forward = (
        await test3.exec(
            "sysctl -n net.ipv4.ip_forward", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
        )
    ).value.strip()
    await _root(test3, "sysctl -w net.ipv4.ip_forward=1")

    await _root(test1, f"ip route add {_VLAN200_NET} via {_TEST3_VLAN100_IP}")
    await _root(test2, f"ip route add {_VLAN100_NET} via {_TEST3_VLAN200_IP}")

    # test3 is docker-capable: Docker's own iptables integration installs a
    # default FORWARD DROP policy, which silently drops the routed dataplane
    # traffic above (discovered live -- see the module docstring). Punch two
    # narrow holes, ONLY between the two VLAN sub-interfaces.
    for rule in _FORWARD_RULES:
        await _root(test3, f"iptables -I FORWARD 1 {rule}")

    edge = Link(
        a=LinkEndpoint(host=_TEST1, interface=_VLAN100_DEV, ip=_TEST1_VLAN_IP),
        b=LinkEndpoint(host=_TEST3, interface=_VLAN100_DEV, ip=_TEST3_VLAN100_IP),
        name="edge",
    )
    dataplane = Link(
        a=LinkEndpoint(host=_TEST1, interface=_VLAN100_DEV, ip=_TEST1_VLAN_IP),
        b=LinkEndpoint(host=_TEST2, interface=_VLAN200_DEV, ip=_TEST2_VLAN_IP),
        name="dataplane",
        impair=_TEST3,
    )
    lab.links.extend([edge, dataplane])

    try:
        yield lab
    finally:
        with contextlib.suppress(Exception):
            await repair_all(lab)
        for rule in _FORWARD_RULES:
            with contextlib.suppress(Exception):
                await _root_best_effort(test3, f"iptables -D FORWARD {rule}")
        with contextlib.suppress(Exception):
            await _root_best_effort(test1, f"ip link del {_VLAN100_DEV}")
        with contextlib.suppress(Exception):
            await _root_best_effort(test3, f"ip link del {_VLAN100_DEV}")
        with contextlib.suppress(Exception):
            await _root_best_effort(test3, f"ip link del {_VLAN200_DEV}")
        with contextlib.suppress(Exception):
            await _root_best_effort(test2, f"ip link del {_VLAN200_DEV}")
        with contextlib.suppress(Exception):
            await _root(test3, f"sysctl -w net.ipv4.ip_forward={prior_ip_forward}")
        await asyncio.gather(*(h.close() for h in (test1, test2, test3)), return_exceptions=True)


async def _assert_bed_hygiene() -> None:
    """Scan all three peers: no otto-impair timers, no netem on mgmt eth1, no VLAN devices left.

    Uses freshly built hosts (never the ones ``impair_lab`` just tore down) so
    this is an independent confirmation, not a re-read through a connection
    that already witnessed the cleanup.
    """
    hosts = [_build_host(ne) for ne in ("test1", "test2", "test3")]
    try:
        leaks: list[str] = []
        for host in hosts:
            ps_result = await host.exec(
                IMPAIR_PS_COMMAND, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
            )
            timers = parse_impair_ps(ps_result.value or "")
            if timers:
                leaks.append(f"{host.id}: leftover otto-impair timers {timers!r}")

            qdisc_result = await host.exec(
                "tc qdisc show dev eth1", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
            )
            if parse_qdisc_show(qdisc_result.value or "") is not None:
                leaks.append(
                    f"{host.id}: mgmt interface eth1 still carries a netem qdisc: "
                    f"{qdisc_result.value!r}"
                )

            link_result = await host.exec(
                "ip -o link show", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
            )
            link_text = link_result.value or ""
            leaks.extend(
                f"{host.id}: {dev!r} still present"
                for dev in (_VLAN100_DEV, _VLAN200_DEV)
                if dev in link_text
            )

            if host.id == _TEST3:
                forward_result = await host.run("iptables -S FORWARD", sudo=True, log=LogMode.QUIET)
                forward_text = forward_result.only.value or ""
                if _VLAN100_DEV in forward_text or _VLAN200_DEV in forward_text:
                    leaks.append(
                        f"{host.id}: FORWARD chain still references a VLAN sub-interface: "
                        f"{forward_text!r}"
                    )
    finally:
        await asyncio.gather(*(h.close() for h in hosts), return_exceptions=True)
    assert not leaks, "link impairment e2e left the bed dirty:\n" + "\n".join(leaks)


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep():
    """Module-final bed hygiene: FAIL (never skip) if anything survived.

    Plain sync fixture (not ``pytest_asyncio``) running its own throwaway
    ``asyncio.run`` -- it fires strictly after ``impair_lab``'s teardown (and
    its module-scoped event loop) has already completed; see the module
    docstring for why the ordering is guaranteed.
    """
    yield
    asyncio.run(_assert_bed_hygiene())


# ---------------------------------------------------------------------------
# Test 1: endpoint-mode delay + repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_endpoint_impair_delay_and_repair(impair_lab: Lab) -> None:
    """``impair_link`` on ``edge`` lands on both endpoints' own netdevs, adds
    real RTT, is visible via ``read_link_states``, and ``repair_link`` heals it."""
    test1 = impair_lab.hosts[_TEST1]
    baseline = await _avg_rtt_ms(test1, _TEST3_VLAN100_IP)

    report = await impair_link(impair_lab, "edge", ImpairmentParams(delay_ms=100.0))
    try:
        placements = {(p.placement.host_id, p.placement.netdev) for p in report.applied}
        assert placements == {(_TEST1, _VLAN100_DEV), (_TEST3, _VLAN100_DEV)}, (
            f"expected placements on test1/test3's own eth1.100, got {placements!r}"
        )

        impaired = await _avg_rtt_ms(test1, _TEST3_VLAN100_IP)
        delta = impaired - baseline
        assert delta >= _RTT_DELTA_MIN_MS, (
            f"expected >={_RTT_DELTA_MIN_MS}ms RTT delta from 100ms delay each way, got "
            f"{delta:.1f}ms (baseline={baseline:.1f}ms, impaired={impaired:.1f}ms)"
        )

        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        a_to_b = edge_state.by_direction[FlowDirection.A_TO_B]
        b_to_a = edge_state.by_direction[FlowDirection.B_TO_A]
        assert a_to_b is not None, "a->b direction unreadable"
        assert b_to_a is not None, "b->a direction unreadable"
        assert a_to_b.whole == ImpairmentParams(delay_ms=100.0), (
            f"expected a->b whole-link state 100ms delay, got {a_to_b!r}"
        )
        assert b_to_a.whole == ImpairmentParams(delay_ms=100.0), (
            f"expected b->a whole-link state 100ms delay, got {b_to_a!r}"
        )
    finally:
        await repair_link(impair_lab, "edge")

    healed = await _avg_rtt_ms(test1, _TEST3_VLAN100_IP)
    assert abs(healed - baseline) <= _RTT_RESTORE_TOLERANCE_MS, (
        f"expected RTT back within {_RTT_RESTORE_TOLERANCE_MS}ms of baseline after repair, got "
        f"{healed:.1f}ms (baseline={baseline:.1f}ms)"
    )


# ---------------------------------------------------------------------------
# Test 2: in-path placements + endpoint purity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_inpath_placements_and_endpoint_purity(impair_lab: Lab) -> None:
    """``impair_link`` on ``dataplane`` lands both directions on test3 (the
    in-path middlebox) only -- test1's own interface stays untouched -- and
    still moves real RTT; ``repair_link`` heals it."""
    test1 = impair_lab.hosts[_TEST1]
    baseline = await _avg_rtt_ms(test1, _TEST2_VLAN_IP)

    report = await impair_link(impair_lab, "dataplane", ImpairmentParams(delay_ms=100.0))
    try:
        assert all(p.placement.host_id == _TEST3 for p in report.applied), (
            f"in-path placements must all land on test3, got "
            f"{[p.placement.host_id for p in report.applied]!r}"
        )
        assert {p.placement.netdev for p in report.applied} == {_VLAN100_DEV, _VLAN200_DEV}, (
            f"expected test3's two facing netdevs, got "
            f"{[p.placement.netdev for p in report.applied]!r}"
        )

        impaired = await _avg_rtt_ms(test1, _TEST2_VLAN_IP)
        delta = impaired - baseline
        assert delta >= _RTT_DELTA_MIN_MS, (
            f"expected >={_RTT_DELTA_MIN_MS}ms RTT delta from 100ms delay each way, got "
            f"{delta:.1f}ms (baseline={baseline:.1f}ms, impaired={impaired:.1f}ms)"
        )

        # purity: the in-path target's own facing netdev stays pure -- the whole
        # point of in-path mode is that the impairment lives on the middlebox.
        purity = await test1.exec(
            f"tc qdisc show dev {_VLAN100_DEV}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
        )
        assert parse_qdisc_show(purity.value or "") is None, (
            f"test1's {_VLAN100_DEV} must stay pure in in-path mode, got: {purity.value!r}"
        )
    finally:
        await repair_link(impair_lab, "dataplane")

    healed = await _avg_rtt_ms(test1, _TEST2_VLAN_IP)
    assert abs(healed - baseline) <= _RTT_RESTORE_TOLERANCE_MS, (
        f"expected RTT back within {_RTT_RESTORE_TOLERANCE_MS}ms of baseline after repair, got "
        f"{healed:.1f}ms (baseline={baseline:.1f}ms)"
    )


# ---------------------------------------------------------------------------
# Test 3: expire self-heals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_expire_self_heals(impair_lab: Lab) -> None:
    """An ``expire``d impairment clears itself off the kernel (a->b direction)
    within the timer window, and its tagged timer process is gone with it."""
    test1 = impair_lab.hosts[_TEST1]
    await impair_link(impair_lab, "edge", ImpairmentParams(delay_ms=100.0), expire=_EXPIRE_SECONDS)
    try:
        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        a_to_b = edge_state.by_direction[FlowDirection.A_TO_B]
        assert a_to_b is not None, "a->b direction unreadable"
        assert a_to_b.whole == ImpairmentParams(delay_ms=100.0)

        async def _a_to_b_healed() -> bool:
            nonlocal edge_state
            states = await read_link_states(impair_lab)
            edge_state = next(s for s in states if s.link.id == "edge")
            direction = edge_state.by_direction[FlowDirection.A_TO_B]
            return direction is not None and direction.whole is None and not direction.scoped

        # Sleep-first: the impairment cannot have expired at t=0 (the timer is
        # still counting down), so the first probe is worth nothing.
        await wait_for_async(
            _a_to_b_healed,
            _EXPIRE_POLL_MAX,
            interval=_EXPIRE_POLL_INTERVAL,
            probe_first=False,
            on_timeout=lambda: (
                f"link 'edge' a->b did not self-heal within {_EXPIRE_POLL_MAX}s: "
                f"{edge_state.by_direction!r}"
            ),
        )

        ps_result = await test1.exec(
            IMPAIR_PS_COMMAND, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
        )
        timers = [t for t in parse_impair_ps(ps_result.value or "") if t.link_id == "edge"]
        assert not timers, f"expire timer for 'edge' still running on test1: {timers!r}"
    finally:
        await repair_link(impair_lab, "edge")


# ---------------------------------------------------------------------------
# Test 4: merge over re-impair + out-of-band clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_merge_and_out_of_band_clear(impair_lab: Lab) -> None:
    """A second ``impair_link`` merges onto the first's kernel state, using an
    AWKWARD rate tc canonicalizes on display -- so this permanently guards the
    canonicalization fix: ``rate 10mbps`` is 10 MB/s, which tc reads back as
    ``80Mbit`` (bytes -> bits, x8). ``read_link_states`` must match by MEANING
    via ``equivalent()`` -- a spelling comparison would false-fail -- and an
    out-of-band ``tc qdisc del`` is observed truth, reflected immediately.

    The FIRST impair also permanently guards the tick-quantization tolerance
    fix (final-review residual, 2026-07-10): it uses ``delay 0.7`` (700us),
    the previously-failing sub-ms value. netem quantizes delay to 64ns kernel
    ticks, and this bed reads ``delay 0.7ms`` back as ``0.699ms`` -- a genuine
    ~1us kernel-tick delta, not a spelling reformat. Before the fix, this
    impair's own post-apply verify (inside ``impair_link``, comparing via
    ``equivalent()``) false-failed on exactly this value, applied, then
    rolled back. That verify now passing IS the live proof that
    ``equivalent()``'s time-field tolerance (``max(2us, 0.5%)``) absorbs the
    quantization delta without masking a real mismatch.
    """
    test1 = impair_lab.hosts[_TEST1]
    # Sub-ms delay: establishes a base for the merge to layer onto, AND is
    # the live tolerance proof -- this impair's own post-apply verify reads
    # the tick-quantized value back and must accept it via equivalent().
    await impair_link(impair_lab, "edge", ImpairmentParams(delay_ms=0.7), from_host=_TEST1)
    # mbps is BYTES/s in tc: 10mbps -> 80Mbit on read-back; plus a plain loss.
    # Without canonicalization the SECOND impair's own verify would false-fail
    # here (merged rate "10mbps" vs observed "80Mbit"), before we ever read state.
    await impair_link(
        impair_lab, "edge", ImpairmentParams(loss_pct=5.0, rate="10mbps"), from_host=_TEST1
    )
    try:
        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        observed_state = edge_state.by_direction[FlowDirection.A_TO_B]
        assert observed_state is not None, "a->b direction unreadable"
        observed = observed_state.whole
        assert observed is not None, "expected the merged impairment to be present on the kernel"
        expected = ImpairmentParams(delay_ms=0.7, loss_pct=5.0, rate="10mbps")
        assert equivalent(observed, expected), (
            "merged kernel state must match by MEANING despite tc canonicalization "
            f"(rate 10mbps->80Mbit) and tick quantization (delay 0.7ms->0.699ms): "
            f"observed {observed!r}, expected {expected!r}"
        )

        # An operator running tc directly, no otto involved.
        oob = (
            await test1.run(f"tc qdisc del dev {_VLAN100_DEV} root", sudo=True, log=LogMode.QUIET)
        ).only
        assert oob.is_ok, f"out-of-band tc qdisc del on test1 failed: {oob.value!r}"

        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        healed_state = edge_state.by_direction[FlowDirection.A_TO_B]
        assert healed_state is not None, "a->b direction unreadable"
        assert healed_state.whole is None, (
            "read_link_states must reflect the out-of-band clear, not stale otto-side state: "
            f"{healed_state!r}"
        )
        assert not healed_state.scoped, (
            "read_link_states must reflect the out-of-band clear, not stale otto-side state: "
            f"{healed_state!r}"
        )
    finally:
        await repair_link(impair_lab, "edge")  # no-op clean: a->b already clear out-of-band


# ---------------------------------------------------------------------------
# Test 5: management-interface refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_mgmt_link_refused(impair_lab: Lab) -> None:
    """A link pinned to the bed's real mgmt netdev (``eth1``) is refused --
    proven against the real bed's own address tables, not a fake one."""
    test1 = impair_lab.hosts[_TEST1]
    test2 = impair_lab.hosts[_TEST2]
    mgmt_link = Link(
        a=LinkEndpoint(host=_TEST1, interface="eth1", ip=test1.ip),
        b=LinkEndpoint(host=_TEST2, interface="eth1", ip=test2.ip),
        name="mgmt-refused",
    )
    impair_lab.links.append(mgmt_link)
    with pytest.raises(ValueError, match="management interface"):
        await impair_link(impair_lab, mgmt_link.id, ImpairmentParams(delay_ms=10.0))


# ---------------------------------------------------------------------------
# Test 6: scoped read-back equivalence + repair to pristine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_impair_readback_and_pristine_repair(impair_lab: Lab) -> None:
    """A scoped impair round-trips through kernel read-back by MEANING, and a
    bare repair returns the netdev to pristine (no root qdisc artifacts)."""
    from otto.link import DirectionState, Selector

    test1 = impair_lab.hosts[_TEST1]
    sel = Selector(_SCOPED_PORT, "tcp")
    report = await impair_link(
        impair_lab,
        "edge",
        ImpairmentParams(delay_ms=_SCOPED_DELAY_MS),
        from_host=_TEST1,
        selector=sel,
    )
    try:
        assert report.applied[0].selector == sel
        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        a = edge_state.by_direction[FlowDirection.A_TO_B]
        assert isinstance(a, DirectionState)
        assert set(a.scoped) == {sel}
        assert equivalent(a.scoped[sel], ImpairmentParams(delay_ms=_SCOPED_DELAY_MS))
        assert a.whole is None
        assert not a.foreign
    finally:
        await repair_link(impair_lab, "edge")
    qdisc = await test1.exec(
        f"tc qdisc show dev {_VLAN100_DEV}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
    )
    line = (qdisc.value or "").splitlines()[0] if (qdisc.value or "").strip() else ""
    assert "prio 1:" not in line, f"expected pristine root after bare repair, got: {qdisc.value!r}"
    assert "netem" not in line, f"expected pristine root after bare repair, got: {qdisc.value!r}"


# ---------------------------------------------------------------------------
# Test 7: differential traffic proof + two concurrent selectors
# ---------------------------------------------------------------------------


async def _tcp_rtt_ms(host: UnixHost, target_ip: str, port: int) -> float:
    """Wall-clock ms for one tiny TCP echo exchange, timed ON the host so ssh
    overhead cancels out of the differential comparison."""
    cmd = (
        "python3 - <<'EOF'\n"
        "import socket, time\n"
        f"t0 = time.monotonic()\n"
        f"s = socket.create_connection(('{target_ip}', {port}), timeout=10)\n"
        "s.sendall(b'x')\n"
        "s.recv(1)\n"
        "s.close()\n"
        "print((time.monotonic() - t0) * 1000)\n"
        "EOF"
    )
    result = await host.exec(cmd, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET)
    assert result.is_ok, f"tcp echo to {target_ip}:{port} failed: {result.value!r}"
    return float((result.value or "").strip().splitlines()[-1])


@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_differential_and_two_selectors(impair_lab: Lab) -> None:
    """The impaired port is measurably slower than an unimpaired port on the
    SAME link (the spec's differential traffic proof), and a second concurrent
    selector applies independently."""
    from otto.link import DirectionState, Selector

    test1 = impair_lab.hosts[_TEST1]
    test3 = impair_lab.hosts[_TEST3]
    for port in (_SCOPED_PORT, _CLEAN_PORT):
        # Bracket-tricked (Wave 14): the bare spelling matched the pkill's own
        # wrapper shell too — see tests/_fixtures/bed_hygiene.argv_pattern.
        await _root_best_effort(test3, f"pkill -f '{argv_pattern(f'TCP4-LISTEN:{port}')}'")
        await test3.exec(
            f"setsid socat TCP4-LISTEN:{port},fork,reuseaddr EXEC:cat </dev/null >/dev/null 2>&1 &",
            timeout=_HOST_CMD_TIMEOUT,
            log=LogMode.QUIET,
        )
    try:
        base_scoped = await _tcp_rtt_ms(test1, _TEST3_VLAN100_IP, _SCOPED_PORT)
        base_clean = await _tcp_rtt_ms(test1, _TEST3_VLAN100_IP, _CLEAN_PORT)

        await impair_link(
            impair_lab,
            "edge",
            ImpairmentParams(delay_ms=_SCOPED_DELAY_MS),
            selector=Selector(_SCOPED_PORT, "tcp"),
        )
        try:
            impaired = await _tcp_rtt_ms(test1, _TEST3_VLAN100_IP, _SCOPED_PORT)
            clean = await _tcp_rtt_ms(test1, _TEST3_VLAN100_IP, _CLEAN_PORT)
            delta = impaired - base_scoped
            assert delta >= _TCP_DELTA_MIN_MS, (
                f"impaired port {_SCOPED_PORT} should be >= {_TCP_DELTA_MIN_MS}ms slower "
                f"(baseline {base_scoped:.1f}ms, impaired {impaired:.1f}ms)"
            )
            assert clean - base_clean < _TCP_DELTA_MIN_MS, (
                f"unimpaired port {_CLEAN_PORT} must stay fast "
                f"(baseline {base_clean:.1f}ms, now {clean:.1f}ms)"
            )

            await impair_link(
                impair_lab,
                "edge",
                ImpairmentParams(loss_pct=5.0),
                selector=Selector(_CLEAN_PORT, "tcp"),
            )
            states = await read_link_states(impair_lab)
            edge_state = next(s for s in states if s.link.id == "edge")
            a = edge_state.by_direction[FlowDirection.A_TO_B]
            assert isinstance(a, DirectionState)
            assert set(a.scoped) == {
                Selector(_SCOPED_PORT, "tcp"),
                Selector(_CLEAN_PORT, "tcp"),
            }
        finally:
            await repair_link(impair_lab, "edge")
    finally:
        for port in (_SCOPED_PORT, _CLEAN_PORT):
            await _root_best_effort(test3, f"pkill -f '{argv_pattern(f'TCP4-LISTEN:{port}')}'")


# ---------------------------------------------------------------------------
# Test 8: per-selector expiry clears only its selector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_expire_clears_only_its_selector(impair_lab: Lab) -> None:
    """--expire on one selector self-heals just that selector; its sibling and
    the scoped root survive until an explicit repair."""
    from otto.link import DirectionState, Selector

    test1 = impair_lab.hosts[_TEST1]
    keep, expire_sel = Selector(_SCOPED_PORT, "tcp"), Selector(_CLEAN_PORT, "tcp")
    await impair_link(
        impair_lab, "edge", ImpairmentParams(delay_ms=50.0), from_host=_TEST1, selector=keep
    )
    try:
        await impair_link(
            impair_lab,
            "edge",
            ImpairmentParams(delay_ms=80.0),
            from_host=_TEST1,
            selector=expire_sel,
            expire=_EXPIRE_SECONDS,
        )
        remaining: set = set()

        async def _only_keep_survives() -> bool:
            nonlocal remaining
            states = await read_link_states(impair_lab)
            edge_state = next(s for s in states if s.link.id == "edge")
            a = edge_state.by_direction[FlowDirection.A_TO_B]
            remaining = set(a.scoped) if isinstance(a, DirectionState) else set()
            return remaining == {keep}

        # Sleep-first: the selector's expiry timer is still counting down at
        # t=0, so the first probe is worth nothing.
        await wait_for_async(
            _only_keep_survives,
            _EXPIRE_POLL_MAX,
            interval=_EXPIRE_POLL_INTERVAL,
            probe_first=False,
            on_timeout=lambda: (
                f"expected only {keep.describe()} to survive expiry, got "
                f"{sorted(s.describe() for s in remaining)!r}"
            ),
        )
        ps_result = await test1.exec(
            IMPAIR_PS_COMMAND, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
        )
        stale = [
            t
            for t in parse_impair_ps(ps_result.value or "")
            if t.link_id == "edge" and t.selector == expire_sel
        ]
        assert not stale, f"expired selector's timer still running: {stale!r}"
    finally:
        await repair_link(impair_lab, "edge")
