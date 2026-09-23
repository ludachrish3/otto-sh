"""The per-host login map --user completion reads, without building a host."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from otto.config import completion_cache as cc
from otto.config.completion_cache import collect_logins_by_host
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import touch_settings

_CREDS = [
    {"login": "u", "password": "hunter2"},
    {"login": "root", "password": "toor", "proxy": "su"},
    {"login": "tel", "password": "tt", "protocols": ["telnet"]},
]


def _repo(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]),
        sut_dir=tmp_path,
        inventory_settings={},
        creds_settings={},
    )


def test_maps_ids_to_login_entries_sorted_by_login(tmp_path):
    repo = _repo(tmp_path, [{"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS}])
    assert collect_logins_by_host([repo]) == {
        "u1": [
            {"login": "root", "protocols": [], "proxy": True},
            {"login": "tel", "protocols": ["telnet"], "proxy": False},
            {"login": "u", "protocols": [], "proxy": False},
        ]
    }


def test_the_map_is_record_driven_whatever_the_family(tmp_path):
    repo = _repo(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"}],
    )
    # A zephyr record still declares creds, so z1 IS present — refusing `--user`
    # on that family is the marker's job (spec §3.3), not the collector's.
    assert set(collect_logins_by_host([repo])) == {"z1"}


def test_a_host_whose_summary_has_no_logins_is_omitted(monkeypatch):
    from otto.labs import HostSummary

    monkeypatch.setattr(cc, "resolve_process_inventory", lambda repos: object())
    monkeypatch.setattr(
        cc,
        "repo_host_summaries",
        lambda repo, resolution: [HostSummary(id="bare"), HostSummary(id="u1", logins=[])],
    )
    assert collect_logins_by_host([SimpleNamespace()]) == {}


def test_empty_without_repos():
    assert collect_logins_by_host([]) == {}


def test_write_and_read_round_trip_and_no_password_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.inventory_settings = {}
    entries = {"u1": [{"login": "u", "protocols": [], "proxy": False}]}
    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=["u1"], logins_by_host=entries)
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["logins_by_host"] == entries
    raw = cc._cache_path().read_text()
    assert "hunter2" not in raw
    assert "toor" not in raw
    assert json.loads(raw)["schema"] == cc.SCHEMA_VERSION == 20


def test_read_cache_rejects_a_malformed_login_map(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.inventory_settings = {}
    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=[])
    data = json.loads(cc._cache_path().read_text())
    data["sections"]["names"]["payload"]["logins_by_host"] = ["not", "a", "dict"]
    cc._cache_path().write_text(json.dumps(data))
    assert cc.read_cache([fake_repo]) is None
