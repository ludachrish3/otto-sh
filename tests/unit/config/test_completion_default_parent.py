"""The cache carries the parent each lab defaults to, by the one rule."""

import json
from unittest.mock import MagicMock

import pytest

from otto.config import completion_cache as cc
from otto.labs.protocol import HostSummary
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project, touch_settings

CREDS = [{"login": "u", "password": "p"}]
HOSTS = [
    {
        "ip": "10.0.0.1",
        "element": "test3",
        "labs": ["east"],
        "creds": CREDS,
        "docker_capable": True,
        "docker_priority": 10,
    },
    {
        "ip": "10.0.0.2",
        "element": "alt2",
        "labs": ["east", "west"],
        "creds": CREDS,
        "docker_capable": True,
    },
    {
        "ip": "10.0.0.3",
        "element": "box",
        "labs": ["west"],
        "creds": CREDS,
        "docker_capable": True,
    },
]
SETTINGS = """\
[[lab.sources]]
backend = "json"
paths = ["lab"]

[[docker.images]]
name = "api"
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.composes]]
name = "core"
path = "docker/compose.yml"
services = ["api", "db"]

[[docker.use_cases]]
name = "integration"
composes = ["core"]
"""


def _s(hid, labs, cap=True, pri=0):
    return HostSummary(id=hid, labs=labs, docker_capable=cap, docker_priority=pri)


@pytest.fixture
def projects_two_labs(tmp_path, monkeypatch):
    from otto.config.project import discover

    sut = make_project(
        tmp_path / "sut",
        name="sut",
        extra=SETTINGS,
        files={"docker/Dockerfile": "FROM scratch\n", "docker/compose.yml": "services: {}\n"},
    )
    (sut / "lab").mkdir()
    write_lab_json(sut / "lab" / "lab.json", HOSTS)
    monkeypatch.setenv("OTTOPATH", str(sut))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return discover().projects


def test_the_rule_over_summaries_matches_the_lab_rule():
    assert cc.default_parent_from_summaries([_s("a", ["east"])]) == "a"
    assert cc.default_parent_from_summaries([_s("a", ["east"]), _s("b", ["east"], pri=2)]) == "b"
    assert cc.default_parent_from_summaries([_s("a", ["east"]), _s("b", ["east"])]) is None
    assert cc.default_parent_from_summaries([_s("a", ["east"], cap=False)]) is None


def test_the_rule_ignores_hosts_that_cannot_run_docker():
    # a lone capable host wins however highly an incapable one is ranked
    hosts = [_s("a", ["east"]), _s("b", ["east"], cap=False, pri=99)]
    assert cc.default_parent_from_summaries(hosts) == "a"


def test_the_rule_ranks_a_negative_priority_below_the_default():
    assert cc.default_parent_from_summaries([_s("a", ["east"], pri=-1), _s("b", ["east"])]) == "b"


def test_the_rule_refuses_a_tie_at_the_top_even_with_a_lower_host_below():
    hosts = [_s("a", ["east"], pri=5), _s("b", ["east"], pri=5), _s("c", ["east"], pri=1)]
    assert cc.default_parent_from_summaries(hosts) is None


def test_collect_default_parent_by_lab_skips_labs_the_rule_refuses(projects_two_labs):
    # east = {test3 cap pri 10, alt2 cap 0}; west = {alt2 cap 0, box cap 0}: a tie
    assert cc.collect_docker_default_parent_by_lab(projects_two_labs) == {"east": "test3"}


def test_container_ids_are_synthesized_under_the_default_parent_only(projects_two_labs):
    ids = cc.collect_host_ids_by_lab(projects_two_labs)
    assert "test3.integration.api" in ids["east"]
    assert not any(i.startswith("alt2.") for i in ids["east"] + ids["west"])
    assert not any(".integration." in i for i in ids["west"])


def test_the_flat_collector_agrees_with_the_per_lab_one(projects_two_labs):
    flat = cc.collect_host_ids(projects_two_labs)
    assert "test3.integration.api" in flat
    assert not any(i.startswith(("alt2.", "box.")) for i in flat if ".integration." in i)
    assert not any(".integration." in i for i in cc.collect_host_ids(projects_two_labs, ["west"]))


def test_the_key_rides_the_names_section_and_read_cache_serves_it(projects_two_labs):
    from otto.cli.main import _completion_collectors
    from otto.config.cache_sections import _collect_names

    payload = _collect_names(projects_two_labs, _completion_collectors())
    assert payload["docker_default_parent_by_lab"] == {"east": "test3"}
    cc.write_cache(
        projects_two_labs,
        payload["instructions"],
        payload["hosts"],
        docker_default_parent_by_lab=payload["docker_default_parent_by_lab"],
    )
    view = cc.read_cache(projects_two_labs)
    assert view is not None
    assert view["docker_default_parent_by_lab"] == {"east": "test3"}


def test_read_cache_rejects_a_malformed_default_parent_map(tmp_path, monkeypatch):
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
    data["sections"]["names"]["payload"]["docker_default_parent_by_lab"] = ["not", "a", "dict"]
    cc._cache_path().write_text(json.dumps(data))
    assert cc.read_cache([mock_project]) is None


def test_schema_is_28():
    from otto import _shim_complete as sc

    assert cc.SCHEMA_VERSION == 28 == sc.SCHEMA
