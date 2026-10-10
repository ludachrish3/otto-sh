"""A data profile declared by one repo serves a lab host of another (R §3.4).

Repo A's settings declare ``[os_profiles.alpha]``; repo B's lab names
``os_type: alpha``. With both selected, every reader that resolves B's host
resolves it on A's table: the host summaries, the completion collector and
the loaded lab.
"""

import textwrap
from pathlib import Path

from otto.config import completion_cache as cc
from otto.config.repo import Repo
from otto.labs import build_lab_sources, host_summaries
from otto.session.lab import build_lab
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

_CREDS = [{"login": "u", "password": "p"}]


def _repos(tmp_path: Path) -> "list[Repo]":
    a = make_sut_repo(
        tmp_path / "a",
        name="repo-a",
        extra=textwrap.dedent("""
            [os_profiles.alpha]
            base = "unix"
            has_bash = false
        """),
    )
    b = make_sut_repo(
        tmp_path / "b",
        name="repo-b",
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
    return [Repo(sut_dir=a), Repo(sut_dir=b)]


def test_a_host_on_another_repos_data_profile_is_in_every_reader(tmp_path):
    repos = _repos(tmp_path)

    summaries = {s.id: s for s in host_summaries(build_lab_sources(repos))}
    assert summaries["b1"].os_type == "alpha"

    assert "b1" in cc.collect_host_ids(repos)

    lab = build_lab(repos, ["e"])
    assert lab.hosts["b1"].os_type == "alpha"
    assert lab.hosts["b1"].has_bash is False


def test_completion_for_repo_b_alone_sees_repo_a_profiles_when_both_are_selected(tmp_path):
    repos = _repos(tmp_path)
    assert cc.collect_host_classes_by_id(repos).get("b1") == "unix"
    assert "b1" in cc.collect_host_ids(repos)
