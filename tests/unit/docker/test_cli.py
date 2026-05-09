"""Unit tests for `otto.cli.docker` helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from otto.configmodule.lab import Lab
from otto.configmodule.repo import Repo


def _make_repo(tmp: Path, *, name: str, default_host: str) -> Repo:
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "Dockerfile").write_text("FROM alpine\n")
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    (sut / ".otto" / "settings.toml").write_text(
        f"name = \"{name}\"\n"
        f"version = \"1.0.0\"\n"
        f"\n[[docker.composes]]\n"
        f"path = \"${{sutDir}}/docker/compose.yml\"\n"
        f"default_host = \"{default_host}\"\n"
        f"services = [\"svc\"]\n"
    )
    return Repo(sutDir=sut)


def test_select_repos_filters_by_lab_applicability(tmp_path):
    """A repo whose default_host isn't in the active lab is silently skipped.

    Reproduces the bug from `otto docker down` against a multi-repo workspace
    where one repo targets a host that lives in a different lab.
    """
    from otto.cli import docker as docker_cli

    repo_in_lab = _make_repo(tmp_path, name="repo1", default_host="pepper_seed")
    repo_out_of_lab = _make_repo(tmp_path, name="repo2", default_host="grape_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()  # only pepper_seed is in the lab

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with patch.object(docker_cli, "getRepos", return_value=[repo_in_lab, repo_out_of_lab]), \
         patch.object(docker_cli, "getConfigModule", return_value=fake_cfg):
        selected = docker_cli._select_repos(repo_name=None)

    names = [r.name for r in selected]
    assert names == ["repo1"], f"repo2 (grape_seed) must be skipped, got {names}"


def test_select_repos_on_does_not_override_lab_filter(tmp_path):
    """--on chooses where to deploy, not which repos belong to the active lab.

    A repo whose declared default_host lives in another lab must still be
    skipped even when --on names an in-lab host — otherwise multi-repo
    workspaces would bring up stacks for unrelated labs (see
    test_e2e_multi_repo_only_active_lab_runs).
    """
    from otto.cli import docker as docker_cli

    repo = _make_repo(tmp_path, name="repo2", default_host="grape_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with patch.object(docker_cli, "getRepos", return_value=[repo]), \
         patch.object(docker_cli, "getConfigModule", return_value=fake_cfg):
        selected = docker_cli._select_repos(repo_name=None, on="pepper_seed")

    assert [r.name for r in selected] == []
