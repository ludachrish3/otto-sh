"""``otto tunnel`` CLI: --hosts parsing helpers, add/list/remove rendering."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli import tunnel as tunnel_cli
from otto.cli.tunnel import (
    _fmt_age,
    _hosts_completer,
    _l2_reachable,
    _parse_endpoint,
    _parse_hosts,
    _tunnel_id_completer,
    tunnel_app,
)
from otto.tunnel import (
    AddedTunnel,
    DiscoveredTunnel,
    RemovedReport,
    Tunnel,
    TunnelDiscovery,
    TunnelHop,
)

runner = CliRunner()


# ── --hosts parsing helpers (moved verbatim from otto.cli.link) ─────────────


def test_parse_endpoint_plain_and_pinned():
    assert _parse_endpoint("test1") == ("test1", None)
    assert _parse_endpoint("test1@eth1") == ("test1", "eth1")


def test_parse_hosts_splits_comma_list():
    assert _parse_hosts("test1@eth0,test2") == [("test1", "eth0"), ("test2", None)]


def test_parse_hosts_accepts_three_or_more_entries():
    assert _parse_hosts("test1,test2@eth0,test3") == [
        ("test1", None),
        ("test2", "eth0"),
        ("test3", None),
    ]


def test_parse_hosts_rejects_empty():
    with pytest.raises(ValueError, match="at least one host"):
        _parse_hosts("")


# ── _l2_reachable (simple-L2 reachability heuristic, spec §11.3) ────────────


def test_l2_reachable_shares_24_prefix():
    hosts = {"a": "10.0.0.1", "b": "10.0.0.9", "c": "192.168.5.5"}
    assert set(_l2_reachable("a", hosts)) == {"b"}  # same /24, excludes self + far net


def test_l2_reachable_unknown_host_returns_empty():
    assert _l2_reachable("ghost", {"a": "10.0.0.1", "b": "10.0.0.9"}) == []


def test_l2_reachable_malformed_ip_returns_empty():
    assert _l2_reachable("a", {"a": "not-an-ip"}) == []


def test_l2_reachable_sorted_and_stable():
    hosts = {"a": "10.0.0.1", "z": "10.0.0.2", "b": "10.0.0.3"}
    assert _l2_reachable("a", hosts) == ["b", "z"]


# ── _hosts_completer wiring ──────────────────────────────────────────────────


def _repo_with_hosts(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    """A fake Repo whose single lab search path holds *hosts* in lab.json."""
    lab = tmp_path / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "lab.json").write_text(json.dumps({"hosts": hosts}))
    return SimpleNamespace(labs=[lab])


_A = {"ip": "10.0.0.1", "element": "a", "creds": [{"login": "u", "password": "p"}]}
_B = {"ip": "10.0.0.9", "element": "b", "creds": [{"login": "u", "password": "p"}]}
_C = {"ip": "192.168.5.5", "element": "c", "creds": [{"login": "u", "password": "p"}]}


def test_hosts_completer_narrows_to_l2_neighbors_after_comma(tmp_path):
    repo = _repo_with_hosts(tmp_path, [_A, _B, _C])
    with patch("otto.cli.tunnel.get_repos", return_value=[repo]):
        result = _hosts_completer(None, "a,")
    assert result == ["a,b"]  # c is on a different /24; a is already typed


def test_hosts_completer_falls_back_to_full_list_on_narrowing_error(tmp_path):
    repo = _repo_with_hosts(tmp_path, [_A, _B, _C])
    with (
        patch("otto.cli.tunnel.get_repos", return_value=[repo]),
        patch("otto.cli.tunnel._ip_by_host", side_effect=RuntimeError("boom")),
    ):
        result = _hosts_completer(None, "a,")
    assert set(result) == {"a,b", "a,c", "a,local"}  # unnarrowed fallback (incl. builtin `local`)


def test_hosts_completer_no_comma_yet_is_unaffected(tmp_path):
    repo = _repo_with_hosts(tmp_path, [_A, _B, _C])
    with patch("otto.cli.tunnel.get_repos", return_value=[repo]):
        result = _hosts_completer(None, "a")
    assert result == ["a"]  # no comma yet: narrowing never engages


# ── _tunnel_id_completer wiring ──────────────────────────────────────────────


def test_tunnel_id_completer_filters_prefix_and_sorts():
    """Only ids sharing the incomplete prefix survive, and the result is
    sorted even though ``read_tunnel_ids`` hands back an unsorted list."""
    with (
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch(
            "otto.cli.tunnel.read_tunnel_ids",
            return_value=["tun-b-161", "tun-a-2000", "tun-a-161"],
        ),
    ):
        result = _tunnel_id_completer(None, "tun-a")
    assert result == ["tun-a-161", "tun-a-2000"]


def test_tunnel_id_completer_none_result_returns_empty():
    with (
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.read_tunnel_ids", return_value=None),
    ):
        result = _tunnel_id_completer(None, "")
    assert result == []


def test_tunnel_id_completer_raising_returns_empty():
    """Completion never crashes the shell: a lookup failure yields ``[]``."""
    with (
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.read_tunnel_ids", side_effect=RuntimeError("boom")),
    ):
        result = _tunnel_id_completer(None, "")
    assert result == []


# ── _fmt_age ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(42, "42s"), (300, "5m"), (7200, "2h"), (172800, "2d")],
)
def test_fmt_age(seconds, expected):
    assert _fmt_age(seconds) == expected


# ── `add`: ValueError/RuntimeError render red + exit 1, no traceback ────────
# The conflict case in particular is a NORMAL user outcome (spec §7.5), so it
# must render cleanly, not blow up.


async def _boom_value_error(*_a, **_k):
    raise ValueError("a tunnel 'tun-abc-161' already exists on this path+port")


async def _boom_runtime_error(*_a, **_k):
    raise RuntimeError("host 'test1' is missing socat and/or bash (required for tunnels)")


def test_add_command_renders_value_error_and_exits_1_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_cli, "get_lab", object)
    monkeypatch.setattr(tunnel_cli, "add_tunnel", _boom_value_error)
    with pytest.raises(typer.Exit) as exc:
        tunnel_cli.add(hosts="test1,test2", port=161, protocol="udp", dest=None)
    assert exc.value.exit_code == 1
    assert "already exists" in capsys.readouterr().out


def test_add_command_renders_runtime_error_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_cli, "get_lab", object)
    monkeypatch.setattr(tunnel_cli, "add_tunnel", _boom_runtime_error)
    with pytest.raises(typer.Exit) as exc:
        tunnel_cli.add(hosts="test1,test2", port=161, protocol="udp", dest=None)
    assert exc.value.exit_code == 1
    assert "missing socat" in capsys.readouterr().out


# ── `add` happy path (CliRunner, full command surface) ──────────────────────


def test_add_command_happy_path_prints_id_endpoints_and_carriers():
    tunnel = Tunnel(
        protocol="tcp",
        service_port=161,
        path=(TunnelHop(host="test1"), TunnelHop(host="test2")),
    )
    added = AddedTunnel(tunnel=tunnel, carrier_fwd=49200, carrier_rev=49201)
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.add_tunnel", AsyncMock(return_value=added)),
    ):
        result = runner.invoke(tunnel_app, ["add", "--hosts", "test1,test2", "--port", "161"])
    assert result.exit_code == 0, result.output
    assert tunnel.id in result.output
    assert "test1 <-> test2" in result.output
    assert "via -" in result.output
    assert "carriers 49200/49201" in result.output


def test_add_passes_carrier_through():
    tunnel = Tunnel(
        protocol="tcp",
        service_port=161,
        path=(TunnelHop(host="test1"), TunnelHop(host="test2")),
    )
    added = AddedTunnel(tunnel=tunnel, carrier_fwd=49200, carrier_rev=49201)
    fake_add = AsyncMock(return_value=added)
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.add_tunnel", fake_add),
    ):
        result = runner.invoke(
            tunnel_app,
            # A NON-default carrier name: with the default ("socat") this test
            # could not tell a plumbed flag from a hardcoded/ignored one.
            # add_tunnel is mocked, so the name needn't be registered.
            ["add", "--hosts", "test1,test2", "--port", "161", "--carrier", "custom"],
        )
    assert result.exit_code == 0, result.output
    assert fake_add.await_args.kwargs["carrier"] == "custom"


# ── `list` ───────────────────────────────────────────────────────────────────


def _direct_tunnel(**kw):
    defaults = {
        "protocol": "tcp",
        "service_port": 161,
        "path": (TunnelHop(host="test1", interface="eth0"), TunnelHop(host="test2")),
    }
    defaults.update(kw)
    return Tunnel(**defaults)


def _discovered(tunnel, *, age_seconds=42, uncertain=False, missing=frozenset()):
    expected = tunnel.expected_processes()
    present = expected - set(missing)
    return DiscoveredTunnel(
        tunnel=tunnel,
        present=present,
        missing=set(missing),
        age_seconds=age_seconds,
        uncertain=uncertain,
    )


def test_list_renders_rich_table_with_column_headers():
    """Issue #139: `tunnel list` renders a Rich table, not bare log lines."""
    discovery = TunnelDiscovery(tunnels=[_discovered(_direct_tunnel())], unreachable=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    for header in ("ID", "ENDPOINTS", "VIA", "PORT", "PROTO", "AGE", "STATUS"):
        assert header in result.output


def test_list_renders_one_row_per_tunnel_with_all_columns(monkeypatch):
    tunnel = _direct_tunnel()
    discovered = _discovered(tunnel, age_seconds=300)
    discovery = TunnelDiscovery(tunnels=[discovered], unreachable=[])
    recorded = {}

    def _record(repos, ids):
        recorded["repos"] = repos
        recorded["ids"] = ids

    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=["repo-sentinel"]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids", side_effect=_record),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert tunnel.id in out
    assert "test1@eth0 <-> test2@-" in out
    assert "161" in out
    assert "tcp" in out
    assert "5m" in out
    assert "ok" in out
    assert recorded == {"repos": ["repo-sentinel"], "ids": [tunnel.id]}


def test_list_relay_tunnel_shows_via_hosts():
    tunnel = Tunnel(
        protocol="tcp",
        service_port=80,
        path=(
            TunnelHop(host="test1"),
            TunnelHop(host="relay1"),
            TunnelHop(host="test2"),
        ),
    )
    discovery = TunnelDiscovery(tunnels=[_discovered(tunnel)], unreachable=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "relay1" in result.output


def test_list_dest_renders_arrow():
    tunnel = Tunnel(
        protocol="tcp",
        service_port=80,
        path=(TunnelHop(host="test1"), TunnelHop(host="test2")),
        dest="test3",
    )
    discovery = TunnelDiscovery(tunnels=[_discovered(tunnel)], unreachable=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "→ test3" in result.output


def test_list_direct_tunnel_shows_dash_for_via():
    tunnel = _direct_tunnel()
    discovery = TunnelDiscovery(tunnels=[_discovered(tunnel)], unreachable=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    # The VIA cell renders a standalone dash token (the "test2@-" endpoint
    # dash is glued to "@", so token-splitting isolates the VIA cell).
    assert "-" in result.output.split()


def test_list_unreachable_hosts_produce_yellow_partial_scan_line():
    discovery = TunnelDiscovery(tunnels=[], unreachable=["test9", "test8"])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "partial scan" in result.output
    assert "test8, test9" in result.output  # sorted


def test_list_degraded_tunnel_shows_present_over_expected():
    tunnel = _direct_tunnel()
    expected = tunnel.expected_processes()
    one_missing = {next(iter(expected))}
    discovered = _discovered(tunnel, missing=one_missing)
    discovery = TunnelDiscovery(tunnels=[discovered], unreachable=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    n_expected = len(expected)
    n_present = n_expected - 1
    assert f"degraded ({n_present}/{n_expected})" in result.output


def test_list_uncertain_tunnel_appends_question_mark():
    tunnel = _direct_tunnel()
    discovered = _discovered(tunnel, uncertain=True)
    discovery = TunnelDiscovery(tunnels=[discovered], unreachable=["test1"])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
        patch("otto.cli.tunnel.discover_tunnels", AsyncMock(return_value=discovery)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        result = runner.invoke(tunnel_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "ok?" in result.output


# ── `remove` ─────────────────────────────────────────────────────────────────


def test_remove_command_renders_value_error_and_exits_1_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_cli, "get_lab", object)
    monkeypatch.setattr(tunnel_cli, "remove_tunnel", _boom_value_error)
    with pytest.raises(typer.Exit) as exc:
        tunnel_cli.remove(tunnel_id="tun-abc-161", all_=False, yes=False)
    assert exc.value.exit_code == 1
    assert "already exists" in capsys.readouterr().out


def test_remove_prints_removed_ids():
    report = RemovedReport(removed_ids=["tun-abc-161"], killed={}, unreachable=[], survivors=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_tunnel", AsyncMock(return_value=report)),
        patch("otto.cli.tunnel.record_tunnel_ids") as mock_record,
        patch("otto.cli.tunnel.get_repos", return_value=["repo-sentinel"]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "tun-abc-161"])
    assert result.exit_code == 0, result.output
    assert "tun-abc-161" in result.output
    mock_record.assert_called_once_with(["repo-sentinel"], [])


def test_remove_prints_multiple_removed_ids_comma_joined_no_brackets():
    """``removed_ids`` must render comma-joined like the survivors style, not
    as a raw Python list repr (e.g. ``['tun-a-161', 'tun-b-53']``)."""
    report = RemovedReport(
        removed_ids=["tun-a-161", "tun-b-53"], killed={}, unreachable=[], survivors=[]
    )
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_all_tunnels", AsyncMock(return_value=report)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "tun-a-161, tun-b-53" in result.output
    assert "[" not in result.output
    assert "]" not in result.output


def test_remove_unreachable_multiple_renders_comma_joined_no_brackets():
    report = RemovedReport(
        removed_ids=["tun-abc-161"], killed={}, unreachable=["test8", "test9"], survivors=[]
    )
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_tunnel", AsyncMock(return_value=report)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "tun-abc-161"])
    assert result.exit_code == 1, result.output
    assert "test8, test9" in result.output
    assert "[" not in result.output
    assert "]" not in result.output


def test_remove_survivors_render_red_and_exit_1():
    report = RemovedReport(
        removed_ids=["tun-abc-161"],
        killed={"test1": [123]},
        unreachable=[],
        survivors=[("test1", 123)],
    )
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_tunnel", AsyncMock(return_value=report)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "tun-abc-161"])
    assert result.exit_code == 1, result.output
    assert "still running after kill" in result.output
    assert "test1/123" in result.output


def test_remove_unreachable_renders_yellow_and_exits_1():
    report = RemovedReport(
        removed_ids=["tun-abc-161"], killed={}, unreachable=["test9"], survivors=[]
    )
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_tunnel", AsyncMock(return_value=report)),
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "tun-abc-161"])
    assert result.exit_code == 1, result.output
    assert "could not reach" in result.output


def test_remove_all_without_yes_prompts():
    report = RemovedReport(removed_ids=[], killed={}, unreachable=[], survivors=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_all_tunnels", AsyncMock(return_value=report)) as mock_remove,
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "--all"], input="n\n")
    assert result.exit_code == 1, result.output
    mock_remove.assert_not_called()
    # Regression: typer's vendored click fork makes `typer.Exit` a
    # `RuntimeError` subclass, so a naive `except (ValueError, RuntimeError)`
    # would catch the confirmation-decline Exit(1) and re-print it as a
    # spurious "[red]1[/red]" error line.
    assert "1" not in result.output


def test_remove_all_with_yes_skips_prompt():
    report = RemovedReport(removed_ids=["tun-abc-161"], killed={}, unreachable=[], survivors=[])
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.remove_all_tunnels", AsyncMock(return_value=report)) as mock_remove,
        patch("otto.cli.tunnel.record_tunnel_ids"),
        patch("otto.cli.tunnel.get_repos", return_value=[]),
    ):
        result = runner.invoke(tunnel_app, ["remove", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    mock_remove.assert_called_once()


def test_remove_with_neither_id_nor_all_exits_2():
    with patch("otto.cli.tunnel.get_lab", return_value=object()):
        result = runner.invoke(tunnel_app, ["remove"])
    assert result.exit_code == 2, result.output
    assert "give a tunnel id or --all" in result.output
