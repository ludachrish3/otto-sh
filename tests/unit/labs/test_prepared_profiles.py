"""A lab source is prepared for the repos selected with it, not once per repo (R §4.3)."""

from pathlib import Path

from otto.config.repo import Repo
from otto.labs import build_lab_sources
from otto.labs.registry import LAB_REPOSITORIES
from otto.models.settings import OsProfileSpec
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_CREDS = [{"login": "u", "password": "p"}]


def _profile_repo(tmp_path: Path) -> Repo:
    """Repo A: declares the ``alpha`` data profile and no lab source."""
    return fake_repo(
        "repo-a",
        sut_dir=tmp_path / "a",
        os_profiles={"alpha": OsProfileSpec.model_validate({"base": "unix", "has_bash": False})},
    )


def _host_repo(tmp_path: Path) -> Repo:
    """Repo B: one json lab whose host selects ``os_type: alpha``."""
    lab = tmp_path / "b" / "lab"
    write_lab_json(
        lab / "lab.json",
        [{"ip": "10.0.0.7", "element": "b1", "labs": ["e"], "creds": _CREDS, "os_type": "alpha"}],
    )
    return fake_repo(
        "repo-b", lab_sources=json_lab_sources(tmp_path / "b", [lab]), sut_dir=tmp_path / "b"
    )


def test_preparation_follows_the_selected_repos_without_a_revision_change(tmp_path):
    a, b = _profile_repo(tmp_path), _host_repo(tmp_path)
    revision = LAB_REPOSITORIES.revision
    build_lab_sources([b])  # prepared for B alone: no alpha profile in its env
    assert LAB_REPOSITORIES.revision == revision
    lab = build_lab_sources([a, b]).load_lab("e")
    assert LAB_REPOSITORIES.revision == revision
    host = lab.hosts["b1"]
    assert host.os_type == "alpha"
    assert host.has_bash is False


def test_a_build_for_the_repo_alone_does_not_displace_the_selection_s_preparation(tmp_path):
    """The lab panel builds a repo alone; the selection's prepared sources must survive it."""
    from otto.host.os_profile import ProfileContext
    from otto.labs.sources import prepared_lab_sources

    a, b = _profile_repo(tmp_path), _host_repo(tmp_path)
    selection = ProfileContext.from_repos([a, b])
    (first,) = prepared_lab_sources(b, profiles=selection)
    prepared_lab_sources(b)  # the repo alone, as Repo.get_lab_panel builds it
    (again,) = prepared_lab_sources(b, profiles=selection)
    assert again is first
