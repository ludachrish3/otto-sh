"""Live-bed e2e tests for ``otto tunnel check``.

Drives the real :func:`otto.tunnel.check_tunnel` against the three-VM unix
bed (test1/test2/test3), with the same host construction, fail-loud-on-host-down
rule and module hygiene bracket as ``tests/e2e/test_tunnel_e2e.py`` (see its
docstring for the topology and why these library-built hosts resolve to their
management ips).

The check starts its own tagged socat echoes (``otto-check:v1:…``) and builds
throwaway tunnels, and is responsible for removing both. So besides the tunnel
e2e's ``otto-tunnel:`` bracket, every test here asserts that no check echo and
no tagged tunnel process is left on any hop once the check returns, and the
module bracket proves the bed free of check echoes going in and coming out.

Each test prints its rendered report (run with ``-s``): those rows are the
bed's real measurements.

Service ports come from the 15300-15399 block in
``tests/_fixtures/tunnel_bed.py``'s ``PORT_BLOCKS``. The check itself builds
on a scratch port above every hop's ephemeral range, which it picks.
"""

import asyncio
import contextlib
import re
import secrets

import pytest
import pytest_asyncio
from rich.console import Console

from otto.check import Verdict, render_sections
from otto.config.lab import Lab
from otto.host.daemon import kill_command, parse_ps_output
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.check import TunnelCheckReport, check_tunnel, tunnel_sections
from otto.tunnel.check_probes import CHECK_PREFIX, SWEEP_COMMAND, EchoTag, echo_launch_command
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    UNIX,
    assert_bed_clean_before_module,
    assert_no_leftover_tunnel_processes,
    assert_reachable,
    build_bed_host,
    kill_tagged,
    wait_for_tcp_bound,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
]

_PORT_CHECK = 15300
_PORT_PLANTED = 15301
_PORT_SWEEP_CHECK = 15302
_PORT_DEST = 15303
_PORT_CONCURRENT_A = 15304
_PORT_CONCURRENT_B = 15305

_MODULE_ID = "tests/e2e/test_tunnel_check_e2e.py"

_PATH = [("test1", None), ("test2", None), ("test3", None)]

_TAGGED = re.compile(rf"^{CHECK_PREFIX}:v\d+:")
"""A real check echo's tag: the prefix, then a version segment."""


# ---------------------------------------------------------------------------
# Lab and bed hygiene
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tunnel_lab():
    """Real ``Lab`` over the 3-VM unix bed (test1/test2/test3)."""
    for ne in UNIX:
        await assert_reachable(ne, host_data(ne)["ip"])

    lab = Lab(name="tunnel_check_e2e")
    for ne in UNIX:
        lab.add_host(build_bed_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


async def _check_echoes() -> list[tuple[str, int, str]]:
    """Every ``otto-check:`` tagged process on the bed, as ``(host, pid, token)``.

    Parsed from the raw :data:`SWEEP_COMMAND` output with the generic daemon
    parser, not the check's own sentinel parser, so an echo of any version
    counts: this is the oracle for the product's cleanup, not a reuse of it.
    The version segment is required because the scan's own ``grep`` argv
    carries the bare ``otto-check:`` needle and shows up in its ``ps`` snapshot.
    """
    hosts = [build_bed_host(ne) for ne in UNIX]
    found: list[tuple[str, int, str]] = []
    try:
        for host in hosts:
            result = await host.exec(SWEEP_COMMAND, timeout=15, log=LogMode.QUIET)
            assert result.is_ok, f"sweep scan failed on {host.id}: {result.value!r}"
            found.extend(
                (host.id, p.pid, p.token)
                for p in parse_ps_output(result.value or "", CHECK_PREFIX)
                if _TAGGED.match(p.token)
            )
    finally:
        await asyncio.gather(*(h.close() for h in hosts), return_exceptions=True)
    return found


def _echo_report(found: list[tuple[str, int, str]], claim: str) -> str:
    rows = [f"  {host} pid={pid} {token}" for host, pid, token in found]
    return "\n".join([claim, *rows])


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep():
    """Bed hygiene bracketing the module: no tunnel process and no check echo, in and out.

    The same bracket as ``tests/e2e/test_tunnel_e2e.py``'s, plus the check's
    echoes: clean on the way in is what makes a leftover on the way out this
    module's.
    """
    asyncio.run(assert_bed_clean_before_module(_MODULE_ID))
    found = asyncio.run(_check_echoes())
    assert not found, _echo_report(
        found,
        f"otto-check echoes were ALREADY on the bed BEFORE {_MODULE_ID} ran -- "
        "not this module's leak; clear them (kill by pid) and re-run:",
    )
    yield
    asyncio.run(assert_no_leftover_tunnel_processes(_MODULE_ID))
    found = asyncio.run(_check_echoes())
    assert not found, _echo_report(found, f"{_MODULE_ID} LEAKED otto-check echoes:")


@pytest_asyncio.fixture
async def reap_check_leftovers(tunnel_lab):
    """Guaranteed teardown for a test that failed mid-check: remove what it left.

    Every echo a check starts names the throwaway tunnel it serves, so the
    tunnels those echoes name are removed and the echoes killed, the same
    order the product's sweep uses. A test that passed leaves nothing here;
    anything found is also reported as an error, so cleaning up never hides a leak.
    """
    yield
    found = await _check_echoes()
    if not found:
        return
    tunnel_ids = {token.split(":")[3] for _host, _pid, token in found if token.count(":") >= 4}
    for tunnel_id in sorted(tunnel_ids - {"-"}):
        with contextlib.suppress(Exception):
            await remove_tunnel(tunnel_lab, tunnel_id)
    for host_id in {host for host, _pid, _token in found}:
        pids = [pid for host, pid, _token in found if host == host_id]
        with contextlib.suppress(Exception):
            await tunnel_lab.hosts[host_id].exec(
                f"{kill_command(pids)} || true", timeout=15, log=LogMode.QUIET
            )
    pytest.fail(_echo_report(found, "check echoes were left behind (now reaped):"))


async def _assert_nothing_tagged_left(lab: Lab) -> None:
    """No check echo and no tagged tunnel process survives on any hop."""
    echoes = await _check_echoes()
    assert not echoes, _echo_report(echoes, "check echoes survived the check:")
    for host in lab.hosts.values():
        result = await host.exec(DISCOVERY_PS_COMMAND, timeout=15, log=LogMode.QUIET)
        assert result.is_ok, f"tunnel scan failed on {host.id}: {result.value!r}"
        left = parse_process_discovery(result.value or "")
        assert not left, f"tunnel processes survived on {host.id}: " + ", ".join(
            f"pid {o.pid} {o.parsed.tunnel.id}" for o in left
        )


def _print_report(title: str, report: TunnelCheckReport) -> None:
    """Show the bed's real measurements under ``-s``, rendered as the CLI does."""
    console = Console(width=120, color_system=None)
    console.print(f"\n===== {title} =====", markup=False)
    render_sections(console, tunnel_sections(report))


def _not_passing(report: TunnelCheckReport) -> list:
    return [
        (c.protocol, r.feature, r.verdict.value, r.detail)
        for c in report.columns
        for r in c.results
        if r.verdict is not Verdict.PASS
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tunnel_check_passes_over_three_hops_for_tcp_and_udp(
    tunnel_lab, reap_check_leftovers
) -> None:
    """Both protocols pass every row over test1 → test2 → test3, and nothing is left."""
    report = await check_tunnel(tunnel_lab, _PATH, port=_PORT_CHECK)
    _print_report("test1 → test2 → test3, both protocols", report)

    assert report.refusal is None, report.refusal
    assert [c.protocol for c in report.columns] == ["tcp", "udp"]
    bad = _not_passing(report)
    assert not bad, bad
    assert report.proven == "hop chain: payload-verified (tcp, udp)"
    assert report.scratch_port is not None
    assert report.scratch_port != _PORT_CHECK
    await _assert_nothing_tagged_left(tunnel_lab)


async def _plant_killed_run(lab: Lab, token: str) -> str:
    """Leave what a check killed between its build and its teardown leaves; return the tunnel id.

    A tagged throwaway tunnel test1 → test2, and the fwd echo whose tag names
    it, listening where that tunnel delivers: loopback on test2.
    """
    test2 = lab.hosts["test2"]
    added = await add_tunnel(
        lab, [("test1", None), ("test2", None)], port=_PORT_PLANTED, protocol="tcp"
    )
    tag = EchoTag(token, added.tunnel.id, "tcp", "fwd-echo", "test2")
    launched = await test2.exec(
        echo_launch_command(tag, "tcp", "127.0.0.1", _PORT_PLANTED),
        timeout=15,
        log=LogMode.QUIET,
    )
    assert launched.is_ok, f"could not plant the echo on test2: {launched.value!r}"
    await wait_for_tcp_bound(test2, "127.0.0.1", _PORT_PLANTED)
    discovered = await discover_tunnels(lab)
    assert added.tunnel.id in [t.tunnel.id for t in discovered.tunnels], (
        "the planted tunnel must be listed before the check, or what the sweep does proves nothing"
    )
    return added.tunnel.id


@pytest.mark.asyncio
async def test_tunnel_check_sweeps_a_killed_run(
    tunnel_lab, reap_check_leftovers, monkeypatch
) -> None:
    """A planted check echo naming a live throwaway tunnel is swept, and the tunnel removed.

    ``ps`` on a real host reports the planted echo's true age, a few seconds,
    and a real sweep leaves anything that young alone (the next test). So
    here the sweep's age bound, ``otto.check.sweep.SWEEP_MIN_AGE_S``, is
    patched to -1, which even an age of 0 s is past: the planted run stands
    in for one killed longer ago than the bound, and this proves the sweep
    path itself on the bed.
    """
    monkeypatch.setattr("otto.check.sweep.SWEEP_MIN_AGE_S", -1)
    planted_id: str | None = None
    token = secrets.token_hex(3)
    test2 = tunnel_lab.hosts["test2"]
    try:
        planted_id = await _plant_killed_run(tunnel_lab, token)

        report = await check_tunnel(tunnel_lab, _PATH, port=_PORT_SWEEP_CHECK)
        _print_report("sweep of a killed run's echo and tunnel", report)

        swept = [line for hop in report.hops for line in hop.swept]
        assert any(
            line.startswith("swept otto-check echo fwd-echo on test2 (earlier or concurrent run")
            and f"removed tunnel {planted_id}" in line
            for line in swept
        ), swept
        discovered = await discover_tunnels(tunnel_lab)
        assert planted_id not in [t.tunnel.id for t in discovered.tunnels]
        planted_id = None
        assert not report.failed(), _not_passing(report)
        await _assert_nothing_tagged_left(tunnel_lab)
    finally:
        # Only reached with work to do when the check did not sweep them.
        await kill_tagged(test2, f"{CHECK_PREFIX}:v1:{token}")
        if planted_id is not None:
            with contextlib.suppress(Exception):
                await remove_tunnel(tunnel_lab, planted_id)


@pytest.mark.asyncio
async def test_tunnel_check_leaves_a_leftover_too_young_to_sweep(
    tunnel_lab, reap_check_leftovers
) -> None:
    """A leftover seconds old may be a running check's: the sweep leaves it and its tunnel.

    The check still passes beside it, and says what it left. The test then
    removes the planted tunnel and echo itself, as a user would with
    ``otto tunnel remove``.
    """
    planted_id: str | None = None
    token = secrets.token_hex(3)
    test2 = tunnel_lab.hosts["test2"]
    try:
        planted_id = await _plant_killed_run(tunnel_lab, token)

        report = await check_tunnel(tunnel_lab, _PATH, port=_PORT_SWEEP_CHECK)
        _print_report("a leftover too young to sweep", report)

        swept = [line for hop in report.hops for line in hop.swept]
        assert any(
            re.fullmatch(
                r"left otto-check echo fwd-echo on test2 \(started \d+ s ago — may be a "
                r"running check\)",
                line,
            )
            for line in swept
        ), swept
        discovered = await discover_tunnels(tunnel_lab)
        assert planted_id in [t.tunnel.id for t in discovered.tunnels]
        assert not report.failed(), _not_passing(report)
    finally:
        await kill_tagged(test2, f"{CHECK_PREFIX}:v1:{token}")
        if planted_id is not None:
            with contextlib.suppress(Exception):
                await remove_tunnel(tunnel_lab, planted_id)
    await _assert_nothing_tagged_left(tunnel_lab)


@pytest.mark.asyncio
async def test_tunnel_check_dest_that_runs_socat_is_verified_end_to_end(
    tunnel_lab, reap_check_leftovers
) -> None:
    """A --dest with socat and bash gets the full proof: the whole path payload-verified."""
    report = await check_tunnel(
        tunnel_lab, [("test1", None), ("test2", None)], port=_PORT_DEST, dest=("test3", None)
    )
    _print_report("test1 → test2 → dest test3, both protocols", report)

    assert report.refusal is None, report.refusal
    assert report.dest_proof == "full"
    assert report.proven == "whole path payload-verified to test3 (tcp, udp)"
    assert not report.failed(), _not_passing(report)
    await _assert_nothing_tagged_left(tunnel_lab)


@pytest.mark.asyncio
async def test_two_concurrent_checks_on_shared_hosts_are_safe(
    tunnel_lab, reap_check_leftovers
) -> None:
    """Two checks race on the same path at once, same tick, no stagger: both pass.

    Both share every hop's sweep and free-port probe, but each draws its own
    scratch port at random (``otto.tunnel.check._scratch_rng``), so their
    echoes and throwaway tunnels don't fight over one scratch port, and each
    of their two ``add_tunnel`` calls also draws its own
    carrier ports at random (``otto.tunnel.manage._carrier_rng``), so the
    builds themselves don't collide either, even started in the very same
    instant with no stagger. Both must pass every row, neither's teardown
    finds the other's tunnel or echoes gone mid-run, and the bed is clean
    once both are done.
    """
    report_a, report_b = await asyncio.gather(
        check_tunnel(tunnel_lab, _PATH, port=_PORT_CONCURRENT_A),
        check_tunnel(tunnel_lab, _PATH, port=_PORT_CONCURRENT_B),
    )
    _print_report("concurrent check A, test1 → test2 → test3", report_a)
    _print_report("concurrent check B, test1 → test2 → test3", report_b)

    for label, report in [("A", report_a), ("B", report_b)]:
        assert report.refusal is None, f"{label}: {report.refusal}"
        bad = _not_passing(report)
        assert not bad, f"{label}: {bad}"
        assert report.proven == "hop chain: payload-verified (tcp, udp)", label
        assert report.scratch_port is not None, label
        assert not any(c.concurrently_swept for c in report.columns), (
            f"{label}: reported a mid-run removal by the other check's sweep"
        )
    assert report_a.scratch_port != report_b.scratch_port
    await _assert_nothing_tagged_left(tunnel_lab)
