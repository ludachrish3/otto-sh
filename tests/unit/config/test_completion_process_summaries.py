"""One process-wide view of the host summaries, which every by-lab reader reads.

Each project's summaries are resolved against the labs that project declares,
so a host a project's pattern reaches only in another project's lab, or one
host two projects both list, would otherwise be offered under fewer labs than
``--lab`` loads it in. The view unions the labs across projects and resolves
every pattern against every declared name.
"""

from types import SimpleNamespace

import pytest

from otto.config import completion_cache as cc
from otto.labs.protocol import HostSummary


def _summary(host_id, labs=(), patterns=(), *, ip="10.0.0.1", cap=False, pri=0):
    return HostSummary(
        id=host_id,
        labs=list(labs),
        lab_patterns=list(patterns),
        ip=ip,
        docker_capable=cap,
        docker_priority=pri,
    )


@pytest.fixture
def two_projects(monkeypatch):
    """Two projects whose summaries and declared lab names the test sets."""
    a, b = SimpleNamespace(root="a"), SimpleNamespace(root="b")
    state = {"summaries": {"a": [], "b": []}, "names": {"a": [], "b": []}}
    monkeypatch.setattr(cc, "_profiles", lambda projects: None)
    monkeypatch.setattr(
        cc,
        "project_host_summaries",
        lambda project, resolution, *, profiles: state["summaries"][project.root],
    )
    monkeypatch.setattr(
        cc,
        "_project_lab_names",
        lambda project, resolution, profiles, *, view=None: state["names"][project.root],
    )
    monkeypatch.setattr(cc, "resolve_process_inventory", lambda projects: cc.InventoryResolution())
    monkeypatch.setattr(cc, "_declared_container_ids", lambda project, parents: [])
    return SimpleNamespace(projects=[a, b], state=state)


def _view(env):
    return cc._process_host_summaries(env.projects, cc.InventoryResolution(), None)


def test_a_host_two_projects_list_carries_the_labs_of_both(two_projects):
    two_projects.state["summaries"]["a"] = [_summary("dut", ["bench"], ip="10.0.0.1")]
    two_projects.state["summaries"]["b"] = [_summary("dut", ["spare"], ip="10.0.0.2")]
    two_projects.state["names"] = {"a": ["bench"], "b": ["spare"]}

    (summary,) = _view(two_projects)

    assert summary.labs == ["bench", "spare"]
    assert summary.ip == "10.0.0.1", "every other field is the first project's"


def test_a_pattern_reaches_a_lab_only_another_project_declares(two_projects):
    two_projects.state["summaries"]["a"] = [_summary("dut", ["bench"], ["bench", "lab-.*"])]
    two_projects.state["summaries"]["b"] = [_summary("other", ["lab-b"], ["lab-b"])]
    two_projects.state["names"] = {"a": ["bench"], "b": ["lab-b"]}

    by_id = {s.id: s for s in _view(two_projects)}

    assert by_id["dut"].labs == ["bench", "lab-b"]
    assert by_id["other"].labs == ["lab-b"]


def test_a_pattern_never_adds_a_lab_name_nobody_declares(two_projects):
    two_projects.state["summaries"]["a"] = [_summary("dut", ["bench"], ["bench", "ghost.*"])]
    two_projects.state["names"] = {"a": ["bench"], "b": []}

    (summary,) = _view(two_projects)

    assert summary.labs == ["bench"]
    assert cc.collect_lab_names(two_projects.projects) == ["bench"]


def test_a_memberless_host_stays_memberless(two_projects):
    two_projects.state["summaries"]["a"] = [_summary("helper")]
    two_projects.state["names"] = {"a": ["bench"], "b": []}

    (summary,) = _view(two_projects)

    assert summary.labs == []
    assert "helper" in cc.collect_host_ids(two_projects.projects)
    assert "helper" not in cc.collect_host_ids(two_projects.projects, ["bench"])


def test_an_unresolved_inventory_gives_nothing_and_reads_no_names(two_projects, monkeypatch):
    two_projects.state["summaries"]["a"] = [_summary("dut", ["bench"])]

    def fail(*args, **kwargs):
        raise AssertionError("names were read")

    monkeypatch.setattr(cc, "_project_lab_names", fail)

    assert (
        cc._process_host_summaries(two_projects.projects, cc.InventoryResolution(error="x"), None)
        == []
    )


def test_the_view_leaves_each_projects_own_summary_unchanged(two_projects):
    mine = _summary("dut", ["bench"], ["bench", "sp.*"])
    two_projects.state["summaries"]["a"] = [mine]
    two_projects.state["summaries"]["b"] = [_summary("dut", ["x"], ["x"])]
    two_projects.state["names"] = {"a": ["bench"], "b": ["spare", "x"]}

    _view(two_projects)

    assert mine.labs == ["bench"]
    assert mine.lab_patterns == ["bench", "sp.*"]


def test_the_three_readers_agree(two_projects):
    two_projects.state["summaries"]["a"] = [
        _summary("dut", ["bench"], ["bench", "sp.*"]),
        _summary("solo", ["bench"], ["bench"]),
    ]
    two_projects.state["summaries"]["b"] = [_summary("dut", ["x"], ["x"], ip="10.0.0.2")]
    two_projects.state["names"] = {"a": ["bench"], "b": ["spare", "x"]}
    projects = two_projects.projects

    by_lab = cc.collect_host_ids_by_lab(projects)
    grouped = cc._summaries_by_lab(projects, cc.InventoryResolution(), None)

    assert by_lab["spare"] == ["dut"], "the pattern reaches a lab only project b declares"
    for lab in ("bench", "spare", "x"):
        assert sorted(s.id for s in grouped[lab]) == by_lab[lab]
        assert set(cc.collect_host_ids(projects, [lab])) - {"local"} == set(by_lab[lab])


def test_two_equally_ranked_capable_hosts_in_one_lab_give_no_default_parent(two_projects):
    # Alone, each project's lab holds one capable host, so each has a default
    # parent; once the union puts both into ``bench`` the rule refuses, as
    # dispatch's does for the merged lab.
    two_projects.state["summaries"]["a"] = [_summary("h1", ["bench"], cap=True)]
    two_projects.state["summaries"]["b"] = [_summary("h2", ["only-b"], ["bench"], cap=True)]
    two_projects.state["names"] = {"a": ["bench"], "b": ["only-b"]}

    parents = cc._default_parents_by_lab(two_projects.projects, cc.InventoryResolution(), None)

    assert "bench" not in parents
    assert parents == {"only-b": "h2"}
