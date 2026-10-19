"""A data profile declared by one project serves a lab host of another (R §3.4).

Project A's settings declare ``[os_profiles.alpha]``; project B's lab names
``os_type: alpha``. With both selected, every reader that resolves B's host
resolves it on A's table: the host summaries, the completion collector and
the loaded lab.
"""

import textwrap
from pathlib import Path

from otto.config import completion_cache as cc
from otto.config.project import Project
from otto.labs import build_lab_sources, host_summaries
from otto.session.lab import build_lab
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project

_CREDS = [{"login": "u", "password": "p"}]


def _projects(tmp_path: Path) -> "list[Project]":
    a = make_project(
        tmp_path / "a",
        name="proj-a",
        extra=textwrap.dedent("""
            [os_profiles.alpha]
            base = "unix"
            has_bash = false
        """),
    )
    b = make_project(
        tmp_path / "b",
        name="proj-b",
        extra=textwrap.dedent("""
            [[lab.sources]]
            backend = "json"
            paths = ["lab"]
        """),
    )
    write_lab_json(
        b / "lab" / "lab.json",
        [{"ip": "10.0.0.8", "element": "b1", "labs": ["e"], "creds": _CREDS, "os_type": "alpha"}],
    )
    return [Project(root=a), Project(root=b)]


def test_a_host_on_another_projects_data_profile_is_in_every_reader(tmp_path):
    projects = _projects(tmp_path)

    summaries = {s.id: s for s in host_summaries(build_lab_sources(projects))}
    assert summaries["b1"].os_type == "alpha"

    assert "b1" in cc.collect_host_ids(projects)

    lab = build_lab(projects, ["e"])
    assert lab.hosts["b1"].os_type == "alpha"
    assert lab.hosts["b1"].has_bash is False


def test_completion_for_project_b_alone_sees_project_a_profiles_when_both_are_selected(tmp_path):
    projects = _projects(tmp_path)
    assert cc.collect_host_classes_by_id(projects).get("b1") == "unix"
    assert "b1" in cc.collect_host_ids(projects)
