"""The tunnel-bed leftover guard must say WHOSE processes it found.

The guard in ``tests/_fixtures/tunnel_bed.py`` scans a shared 3-VM bed for
``otto-tunnel:``-tagged processes. Because the bed is shared, "something is
running" and "*this module* leaked something" are different claims, and the
guard used to make only the first while wording it as the second — it blamed
``test_cli_cycle_add_list_remove_list_docker_free`` (ports 15000-15004) for a
tunnel on port 15130 left by an interrupted stability run. That cost a full
investigation on 2026-07-21; these tests pin the distinction so it costs
seconds instead.

Realistic input: the ps line below is copied from that incident, so the
report is exercised through the same ``parse_process_discovery`` the live
guard uses rather than hand-built dataclasses.
"""

import pytest

from otto.tunnel.discovery import parse_process_discovery
from tests._fixtures import tunnel_bed
from tests._fixtures.tunnel_bed import format_leftover_report, owning_suite

# pid etime args… — the exact shape `ps_scan_command` emits.
_STABILITY_PS_LINE = (
    "530366 07:11:31 otto-tunnel:v1:tun-45bf687b4607-15130:udp:15130:49152:fwd:ingress:0::"
    "carrot_seed%2Ctomato_seed UDP4-LISTEN:15130,bind=10.10.200.11,fork,reuseaddr "
    "TCP4:10.10.200.12:49152"
)


def _found(ps_line: str = _STABILITY_PS_LINE) -> list[tuple[str, object]]:
    observed = parse_process_discovery(ps_line)
    assert observed, "fixture ps line must parse, or these tests prove nothing"
    return [("carrot_seed", observed[0])]


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        (15130, "tunnel_stability"),  # the port that caused the misattribution
        (15004, "test_tunnel_e2e"),
        (15000, "test_tunnel_e2e"),
        (15199, "tunnel_stability"),
    ],
)
def test_owning_suite_names_the_block_owner(port: int, expected: str) -> None:
    """A service port maps to the suite that reserves its block."""
    assert expected in (owning_suite(port) or "")


def test_owning_suite_is_none_for_unreserved_ports() -> None:
    """Ports outside the declared blocks must not be attributed to a suite."""
    assert owning_suite(22) is None


def test_preexisting_report_exonerates_the_running_module() -> None:
    """A dirty-at-setup bed must be reported as NOT this module's doing."""
    report = format_leftover_report(
        _found(), module="tests/e2e/test_tunnel_e2e.py", preexisting=True
    )
    lowered = report.lower()
    assert "not" in lowered, f"must disclaim this module's authorship, got:\n{report}"
    assert "before" in lowered, f"must say the processes predate the module, got:\n{report}"
    # Names the true owner rather than the module that merely observed it.
    assert "tunnel_stability" in report, f"must name the owning suite:\n{report}"


def test_leaked_report_blames_the_running_module() -> None:
    """A tunnel appearing only at teardown IS the module's leak — say so."""
    report = format_leftover_report(
        _found(), module="tests/e2e/test_tunnel_e2e.py", preexisting=False
    )
    assert "tests/e2e/test_tunnel_e2e.py" in report
    assert "leaked" in report.lower(), f"must name it a leak:\n{report}"
    assert "not caused by this module" not in report.lower()


def test_report_carries_the_facts_needed_to_act() -> None:
    """Host, pid, tunnel id, port and a copy-pasteable reap command."""
    report = format_leftover_report(
        _found(), module="tests/e2e/test_tunnel_e2e.py", preexisting=True
    )
    for fact in ("carrot_seed", "530366", "tun-45bf687b4607-15130", "15130"):
        assert fact in report, f"missing {fact!r} from report:\n{report}"
    assert "remove_tunnel" in report, f"must give the reap recipe:\n{report}"


def test_report_shows_age_so_staleness_is_obvious() -> None:
    """Age is the tell that a process predates the run — surface it."""
    report = format_leftover_report(
        _found(), module="tests/e2e/test_tunnel_e2e.py", preexisting=True
    )
    assert "age" in report.lower(), f"must report process age:\n{report}"


# ---------------------------------------------------------------------------
# The two live gates, driven over a faked bed scan (no VMs).
# ---------------------------------------------------------------------------


def _fake_scan(monkeypatch, found) -> None:
    async def _observe() -> list:
        return found

    monkeypatch.setattr(tunnel_bed, "observe_tunnel_processes", _observe)


@pytest.mark.asyncio
async def test_setup_gate_rejects_a_dirty_bed(monkeypatch) -> None:
    """Foreign leftovers must stop the module before it wastes bed time."""
    _fake_scan(monkeypatch, _found())
    with pytest.raises(AssertionError) as exc:
        await tunnel_bed.assert_bed_clean_before_module("tests/e2e/test_tunnel_e2e.py")
    assert "not this module's leak" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_setup_gate_passes_on_a_clean_bed(monkeypatch) -> None:
    """The common case must stay silent — no false alarm on an empty scan."""
    _fake_scan(monkeypatch, [])
    await tunnel_bed.assert_bed_clean_before_module("tests/e2e/test_tunnel_e2e.py")


@pytest.mark.asyncio
async def test_final_sweep_blames_this_module(monkeypatch) -> None:
    """Anything present at teardown is this module's, since setup proved clean."""
    _fake_scan(monkeypatch, _found())
    with pytest.raises(AssertionError) as exc:
        await tunnel_bed.assert_no_leftover_tunnel_processes("tests/e2e/test_tunnel_e2e.py")
    assert "leaked" in str(exc.value).lower()
