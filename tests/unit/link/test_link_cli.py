"""``otto link`` CLI: the ``--hosts`` parsing helpers (pure, hostless)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import typer

from otto.cli import link as link_cli
from otto.cli.link import _hosts_completer, _l2_reachable, _parse_endpoint, _parse_hosts


def test_parse_endpoint_plain_and_pinned():
    assert _parse_endpoint("test1") == ("test1", None)
    assert _parse_endpoint("test1@eth1") == ("test1", "eth1")


def test_parse_hosts_splits_comma_list():
    assert _parse_hosts("test1@eth0,test2") == [("test1", "eth0"), ("test2", None)]


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
    with patch("otto.cli.link.get_repos", return_value=[repo]):
        result = _hosts_completer(None, "a,")
    assert result == ["a,b"]  # c is on a different /24; a is already typed


def test_hosts_completer_falls_back_to_full_list_on_narrowing_error(tmp_path):
    repo = _repo_with_hosts(tmp_path, [_A, _B, _C])
    with (
        patch("otto.cli.link.get_repos", return_value=[repo]),
        patch("otto.cli.link._ip_by_host", side_effect=RuntimeError("boom")),
    ):
        result = _hosts_completer(None, "a,")
    assert set(result) == {"a,b", "a,c", "a,local"}  # unnarrowed fallback (incl. builtin `local`)


def test_hosts_completer_no_comma_yet_is_unaffected(tmp_path):
    repo = _repo_with_hosts(tmp_path, [_A, _B, _C])
    with patch("otto.cli.link.get_repos", return_value=[repo]):
        result = _hosts_completer(None, "a")
    assert result == ["a"]  # no comma yet: narrowing never engages


# ── T9(1): `add`/`remove` render a ValueError/RuntimeError from the library
# as `[red]...[/red]` + `typer.Exit(1)` instead of an unhandled traceback.
# The conflict case in particular is a NORMAL user outcome (spec §7.5), so it
# must render cleanly, not blow up.


async def _boom_value_error(*_a, **_k):
    raise ValueError("a tunnel 'lnk-abc-161' already exists on this route+port")


async def _boom_runtime_error(*_a, **_k):
    raise RuntimeError("host 'test1' is missing socat and/or bash (required for tunnels)")


def test_add_command_renders_value_error_and_exits_1_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(link_cli, "get_lab", object)
    monkeypatch.setattr(link_cli, "add_link", _boom_value_error)
    with pytest.raises(typer.Exit) as exc:
        link_cli.add(hosts="test1,test2", port=161, protocol="udp", dest=None)
    assert exc.value.exit_code == 1
    assert "already exists" in capsys.readouterr().out


def test_add_command_renders_runtime_error_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(link_cli, "get_lab", object)
    monkeypatch.setattr(link_cli, "add_link", _boom_runtime_error)
    with pytest.raises(typer.Exit) as exc:
        link_cli.add(hosts="test1,test2", port=161, protocol="udp", dest=None)
    assert exc.value.exit_code == 1
    assert "missing socat" in capsys.readouterr().out


def test_remove_command_renders_value_error_and_exits_1_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(link_cli, "get_lab", object)
    monkeypatch.setattr(link_cli, "remove_link", _boom_value_error)
    with pytest.raises(typer.Exit) as exc:
        link_cli.remove(link_id="lnk-abc-161", all_=False, yes=False)
    assert exc.value.exit_code == 1
    assert "already exists" in capsys.readouterr().out
