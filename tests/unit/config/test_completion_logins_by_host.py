"""The per-host login map --user completion reads, without building a host."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from otto.config import completion_cache as cc
from otto.config.completion_cache import collect_logins_by_host
from otto.config.project import Project
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.project_scaffold import touch_settings

_CREDS = [
    {"login": "u", "password": "hunter2"},
    {"login": "root", "password": "toor", "proxy": "su"},
    {"login": "tel", "password": "tt", "protocols": ["telnet"]},
]


def _project(tmp_path: Path, hosts: list[dict]) -> Project:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return fake_project(lab_sources=json_lab_sources(tmp_path, [lab]), root=tmp_path)


def test_maps_ids_to_login_entries_sorted_by_login(tmp_path):
    project = _project(
        tmp_path, [{"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS}]
    )
    assert collect_logins_by_host([project]) == {
        "u1": [
            {"login": "root", "protocols": [], "proxy": True},
            {"login": "tel", "protocols": ["telnet"], "proxy": False},
            {"login": "u", "protocols": [], "proxy": False},
        ]
    }


def test_the_map_is_record_driven_whatever_the_family(tmp_path):
    project = _project(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"}],
    )
    # A zephyr record still declares creds, so z1 IS present — refusing `--user`
    # on that family is the marker's job (spec §3.3), not the collector's.
    assert set(collect_logins_by_host([project])) == {"z1"}


def test_a_host_whose_summary_has_no_logins_is_omitted(monkeypatch):
    from otto.labs import HostSummary

    monkeypatch.setattr(cc, "resolve_process_inventory", lambda projects: object())
    monkeypatch.setattr(
        cc,
        "project_host_summaries",
        lambda project, resolution, *, profiles: [
            HostSummary(id="bare"),
            HostSummary(id="u1", logins=[]),
        ],
    )
    assert collect_logins_by_host([SimpleNamespace(os_profiles={})]) == {}


def test_empty_without_projects():
    assert collect_logins_by_host([]) == {}


def test_write_and_read_round_trip_and_no_password_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    mock_project = MagicMock()
    mock_project.root = tmp_path / "sut"
    mock_project.root.mkdir()
    touch_settings(mock_project.root)
    mock_project.init = []
    mock_project.libs = []
    mock_project.tests = []
    mock_project.inventory_settings = {}
    entries = {"u1": [{"login": "u", "protocols": [], "proxy": False}]}
    cc.write_cache([mock_project], instructions=[], hosts=["u1"], logins_by_host=entries)
    out = cc.read_cache([mock_project])
    assert out is not None
    assert out["logins_by_host"] == entries
    raw = cc._cache_path().read_text()
    assert "hunter2" not in raw
    assert "toor" not in raw
    assert json.loads(raw)["schema"] == cc.SCHEMA_VERSION


def test_read_cache_rejects_a_malformed_login_map(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    mock_project = MagicMock()
    mock_project.root = tmp_path / "sut"
    mock_project.root.mkdir()
    touch_settings(mock_project.root)
    mock_project.init = []
    mock_project.libs = []
    mock_project.tests = []
    mock_project.inventory_settings = {}
    cc.write_cache([mock_project], instructions=[], hosts=[])
    data = json.loads(cc._cache_path().read_text())
    data["sections"]["names"]["payload"]["logins_by_host"] = ["not", "a", "dict"]
    cc._cache_path().write_text(json.dumps(data))
    assert cc.read_cache([mock_project]) is None
