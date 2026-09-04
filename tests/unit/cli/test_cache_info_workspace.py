"""``otto cache info``'s closing block: what completion offers HERE, and what it dropped.

The read side of completion's outlet (``otto.labs.drops``). Driven through
``cache_app`` with ``CliRunner`` like the rest of ``test_cache_cli.py``, but
against a REAL parsed repo under ``OTTO_SUT_DIRS`` — the block runs
discovery, reads the workspace's ``names`` entry and describes the inventory
as completion resolves it, so a stand-in would test nothing.
"""

import json
from types import SimpleNamespace

from typer.testing import CliRunner

import otto.config.completion_cache as cc
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

runner = CliRunner()

_CREDS = [{"login": "u", "password": "p"}]

SETTINGS = """\
[[lab.sources]]
name = "local"
backend = "json"
paths = ["lab", "hosts.json"]

[inventory]
backend = "json"
path = "inventory.json"
supplies = ["ip"]
"""


SETTINGS_BROKEN = """\
[[lab.sources]]
name = "local"
backend = "json"
paths = ["lab", "hosts.json"]

[inventory]
backend = "json"
"""


def _workspace(tmp_path, monkeypatch, *, references: list[str], settings: str = SETTINGS):
    """A parsed SUT repo whose ``hosts.json`` supplement references *references*."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    repo = make_sut_repo(tmp_path / "invsut", name="invsut", extra=settings)
    (repo / "inventory.json").write_text(json.dumps({"dut-1": {"ip": "10.0.0.1"}}))
    write_lab_json(
        repo / "lab" / "lab.json",
        [{"ip": "10.0.0.9", "element": "plain", "creds": _CREDS, "labs": ["site"]}],
        declare_labs=True,
    )
    write_lab_json(
        repo / "hosts.json",
        [
            {"inventory": key, "element": f"dut{i}", "creds": _CREDS, "labs": ["site"]}
            for i, key in enumerate(references, start=1)
        ],
        declare_labs=False,
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    return repo


def _seed_cache() -> None:
    """Write the names entry the way the slow path does, from the real collectors."""
    from otto.bootstrap import discover

    repos = discover().repos
    assert repos, "discovery found no repo under OTTO_SUT_DIRS"
    cc.write_cache(
        repos,
        instructions=[],
        suites=[],
        hosts=cc.collect_host_ids(repos),
        host_drops=cc.collect_host_drops(repos),
    )


def test_info_explains_the_workspace_hosts_and_what_was_dropped(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    repo = _workspace(tmp_path, monkeypatch, references=["dut-1", "ghost"])
    _seed_cache()

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "this workspace:" in out
    assert "completion names: fresh" in out
    assert "inventory: json:" in out
    assert "BROKEN" not in out
    assert f"lab files (invsut/local): {repo / 'lab' / 'lab.json'}, {repo / 'hosts.json'}" in out
    assert "hosts offered: 3 — dut1 local plain" in out  # `local` is the builtin host
    assert "dropped: 1 — not offered, and why:" in out
    assert f"[invsut] {repo / 'hosts.json'}: element 'dut2' hosts[0]: " in out
    assert "ghost" in out


def test_info_says_none_dropped_when_every_entry_built(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    _seed_cache()

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "hosts offered: 3 — dut1 local plain" in result.output
    assert "dropped: none" in result.output


def test_info_reports_a_missing_entry_without_guessing_hosts(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "no cached workspaces" in result.output
    assert "completion names: missing" in result.output
    assert "hosts offered: unknown — no entry to read" in result.output
    assert "dropped" not in result.output


def test_info_with_a_broken_inventory_promises_no_write(tmp_path, monkeypatch):
    """The standing line must not say a TAB writes an entry when the BROKEN
    inventory line right under it means nothing can (the digest is ephemeral)."""
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"], settings=SETTINGS_BROKEN)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "inventory: BROKEN" in out
    assert "requires a 'path'" in out
    expected = (
        "completion names: missing — no entry yet; nothing is written while the inventory is broken"
    )
    assert expected in out
    assert "writes one" not in out
    assert "rebuilds it" not in out
    assert "hosts offered: unknown — no entry to read" in out


def test_info_with_an_unfingerprinted_inventory_promises_no_write(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    unfingerprinted = SimpleNamespace(label="probe:live", fingerprint=lambda: None)
    monkeypatch.setattr("otto.inventory.build_inventory", lambda _repos: unfingerprinted)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "inventory: probe:live — cannot report freshness, so completion never caches" in out
    assert "nothing is written while the inventory cannot report freshness" in out
    assert "writes one" not in out


def test_info_stays_home_wide_without_a_workspace(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "this workspace" not in result.output
