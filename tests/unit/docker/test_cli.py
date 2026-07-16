"""Unit tests for `otto.cli.docker` helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from otto.cli import docker as docker_cli
from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.host.unix_host import UnixHost
from otto.utils import Status


def _make_repo(tmp: Path, *, name: str, default_host: str) -> Repo:
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "Dockerfile").write_text("FROM alpine\n")
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    (sut / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\n'
        f'version = "1.0.0"\n'
        f"\n[[docker.composes]]\n"
        f'path = "${{sut_dir}}/docker/compose.yml"\n'
        f'default_host = "{default_host}"\n'
        f'services = ["svc"]\n'
    )
    return Repo(sut_dir=sut)


def _make_repo_with_image(tmp: Path, *, name: str, default_host: str) -> Repo:
    """Like _make_repo but also declares a [[docker.images]] entry so
    _build's ``if not r.docker_settings.images: continue`` guard is passed."""
    sut = tmp / name
    (sut / ".otto").mkdir(parents=True)
    (sut / "docker").mkdir()
    (sut / "docker" / "Dockerfile").write_text("FROM alpine\n")
    (sut / "docker" / "compose.yml").write_text("services: {}\n")
    (sut / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\n'
        f'version = "1.0.0"\n'
        f"\n[[docker.images]]\n"
        f'name = "myimage"\n'
        f'dockerfile = "${{sut_dir}}/docker/Dockerfile"\n'
        f'context = "${{sut_dir}}/docker"\n'
        f"\n[[docker.composes]]\n"
        f'path = "${{sut_dir}}/docker/compose.yml"\n'
        f'default_host = "{default_host}"\n'
        f'services = ["svc"]\n'
    )
    return Repo(sut_dir=sut)


def test_select_repos_filters_by_lab_applicability(tmp_path):
    """A repo whose default_host isn't in the active lab is silently skipped.

    Reproduces the bug from `otto docker down` against a multi-repo workspace
    where one repo targets a host that lives in a different lab.
    """
    repo_in_lab = _make_repo(tmp_path, name="repo1", default_host="pepper_seed")
    repo_out_of_lab = _make_repo(tmp_path, name="repo2", default_host="grape_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()  # only pepper_seed is in the lab

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo_in_lab, repo_out_of_lab]),
        patch.object(docker_cli, "get_lab", return_value=fake_cfg.lab),
    ):
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
    repo = _make_repo(tmp_path, name="repo2", default_host="grape_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=fake_cfg.lab),
    ):
        selected = docker_cli._select_repos(repo_name=None, on="pepper_seed")

    assert [r.name for r in selected] == []


# ---------------------------------------------------------------------------
# _select_repos — additional coverage: name-filter, no-match exit, bad-on exit
# ---------------------------------------------------------------------------


def test_select_repos_filters_by_repo_name(tmp_path):
    """_select_repos(repo_name=…) keeps only the repo whose name matches."""
    repo1 = _make_repo(tmp_path / "r1", name="repo1", default_host="pepper_seed")
    repo2 = _make_repo(tmp_path / "r2", name="repo2", default_host="pepper_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1, repo2]),
        patch.object(docker_cli, "get_lab", return_value=lab),
    ):
        result = docker_cli._select_repos(repo_name="repo2")

    assert [r.name for r in result] == ["repo2"]


def test_select_repos_no_match_exits(tmp_path):
    """_select_repos raises Exit(1) when repo_name matches nothing."""
    repo1 = _make_repo(tmp_path / "r1", name="repo1", default_host="pepper_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "rprint"),
        pytest.raises(typer.Exit) as exc,
    ):
        docker_cli._select_repos(repo_name="nope")

    assert exc.value.exit_code == 1


def test_select_repos_bad_on_exits(tmp_path):
    """_select_repos raises Exit(1) when --on names a host not in the lab."""
    repo1 = _make_repo(tmp_path / "r1", name="repo1", default_host="pepper_seed")

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "rprint"),
        pytest.raises(typer.Exit) as exc,
    ):
        docker_cli._select_repos(repo_name=None, on="ghost")

    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# _build command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_success(tmp_path):
    """_build prints a green 'built' line when build_images returns Success."""
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_rprint = MagicMock()
    mock_build = AsyncMock(return_value={"myimage": (Status.Success, "sha256:abc")})

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    all_calls = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[green]" in all_calls
    assert "built" in all_calls


@pytest.mark.asyncio
async def test_build_skipped(tmp_path):
    """_build prints a dim 'cached' line when build_images returns Skipped."""
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_rprint = MagicMock()
    mock_build = AsyncMock(return_value={"myimage": (Status.Skipped, "already exists")})

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    all_calls = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[dim]" in all_calls
    assert "cached" in all_calls


@pytest.mark.asyncio
async def test_build_failed_exits(tmp_path):
    """_build raises Exit(1) when build_images returns Failed for an image."""
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_build = AsyncMock(return_value={"myimage": (Status.Failed, "build error")})

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
        patch.object(docker_cli, "rprint", MagicMock()),
        pytest.raises(typer.Exit) as exc,
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    assert exc.value.exit_code == 1


@pytest.mark.asyncio
async def test_build_skips_repo_with_no_images(tmp_path):
    """_build silently skips a repo that has no docker.images declared."""
    # _make_repo produces a repo with composes but no images
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")
    mock_build = AsyncMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    # build_images must NOT be called when images is empty
    mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# _up command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_up_registers_containers(tmp_path):
    """_up prints a green 'N container(s) registered' line on success."""
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_container = MagicMock()
    mock_container.id = "ctr1"
    mock_container.container_id = "abcdef123456789"

    mock_compose_up = AsyncMock(return_value={"svc": mock_container})
    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_up", mock_compose_up),
        patch.object(docker_cli, "get_user_compose_project", return_value="myrepo_proj"),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._up(repo=None, on=None, no_build=False)

    all_calls = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[green]" in all_calls
    assert "container(s) registered" in all_calls


@pytest.mark.asyncio
async def test_up_skips_repo_with_no_composes(tmp_path):
    """_up skips a repo with no [[docker.composes]] without crashing.

    Uses a MagicMock repo where docker_settings.composes is falsy so that
    _up's ``if not r.docker_settings.composes: continue`` guard is triggered
    (DockerSettings is frozen, so we can't assign to it after construction).
    """
    # MagicMock repo whose composes tuple is empty → the guard fires
    repo = MagicMock()
    repo.docker_settings.composes = ()

    mock_compose_up = AsyncMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_up", mock_compose_up),
        patch.object(docker_cli, "get_user_compose_project", return_value="proj"),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._up(repo=None, on=None, no_build=False)

    # compose_up should never be called for a repo with no composes
    mock_compose_up.assert_not_called()


@pytest.mark.asyncio
async def test_up_accepts_positional_handle_for_on(tmp_path):
    """--on is a CLI host-id input (like `otto host`'s positional), so it must
    accept a positional handle (e.g. "dut1"), not just a canonical id.

    Two "dut" hosts make the lower element_id host's logical_index 1, so its
    canonical id ("dut47") is reachable via the handle "dut1". compose_up's
    downstream lookup is canonical-id-only, so ``_up`` must resolve the
    handle to "dut47" before calling compose_up — not forward "dut1" raw.
    """
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    lab = Lab(name="veggies")
    dut_a = UnixHost(ip="10.0.0.1", creds=[], element="dut", element_id=47)
    dut_b = UnixHost(ip="10.0.0.2", creds=[], element="dut", element_id=200)
    lab.add_host(dut_a)
    lab.add_host(dut_b)
    lab._assign_logical_indices()
    assert dut_a.id == "dut47"  # sanity: canonical id
    assert dut_a.logical_index == 1  # sanity: "dut1" handle -> dut_a

    mock_compose_up = AsyncMock(return_value={})

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "compose_up", mock_compose_up),
        patch.object(docker_cli, "get_user_compose_project", return_value="myrepo_proj"),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._up(repo=None, on="dut1", no_build=False)

    mock_compose_up.assert_called_once()
    assert mock_compose_up.call_args.kwargs["on"] == "dut47"


# ---------------------------------------------------------------------------
# _down command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_down_skipped(tmp_path):
    """_down prints a dim 'nothing to tear down' line when status is Skipped."""
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_compose_down = AsyncMock(return_value=Status.Skipped)
    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_down", mock_compose_down),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._down(repo=None, on=None)

    all_calls = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[dim]" in all_calls
    assert "nothing to tear down" in all_calls


@pytest.mark.asyncio
async def test_down_success(tmp_path):
    """_down prints a green 'stack down' line on Success."""
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_compose_down = AsyncMock(return_value=Status.Success)
    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_down", mock_compose_down),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._down(repo=None, on=None)

    all_calls = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[green]" in all_calls
    assert "stack down" in all_calls


@pytest.mark.asyncio
async def test_down_failed_exits(tmp_path):
    """_down raises Exit(1) when compose_down returns Failed."""
    repo = _make_repo(tmp_path / "r1", name="myrepo", default_host="pepper_seed")

    mock_compose_down = AsyncMock(return_value=Status.Failed)

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_down", mock_compose_down),
        patch.object(docker_cli, "rprint", MagicMock()),
        pytest.raises(typer.Exit) as exc,
    ):
        await docker_cli._down(repo=None, on=None)

    assert exc.value.exit_code == 1


@pytest.mark.asyncio
async def test_down_skips_repo_with_no_composes(tmp_path):
    """_down silently skips a repo that has no [[docker.composes]]."""
    repo = MagicMock()
    repo.docker_settings.composes = ()
    mock_compose_down = AsyncMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "compose_down", mock_compose_down),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._down(repo=None, on=None)

    mock_compose_down.assert_not_called()


# ---------------------------------------------------------------------------
# _ps command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ps_all_hosts_table(tmp_path):
    """_ps queries compose_ps for a docker-capable host and passes row data to Table."""
    host = MagicMock(spec=UnixHost)
    host.id = "pepper_seed"
    host.docker_capable = True

    lab = Lab(name="veggies")
    lab.hosts["pepper_seed"] = host

    rows = [
        {
            "ID": "abc123def456xyz",
            "Image": "myimg",
            "Status": "Up 2 hours",
            "Names": "ctr1",
        }
    ]
    mock_compose_ps = AsyncMock(return_value=rows)

    mock_table_instance = MagicMock()
    mock_table_cls = MagicMock(return_value=mock_table_instance)

    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "compose_ps", mock_compose_ps),
        patch.object(docker_cli, "Table", mock_table_cls),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._ps(on=None)

    # Table should have been called with column headers
    mock_table_cls.assert_called_once()
    # add_row should have been called with parsed values
    mock_table_instance.add_row.assert_called_once()
    call_args = mock_table_instance.add_row.call_args[0]
    assert call_args[0] == "pepper_seed"  # host id
    assert call_args[1] == "abc123def456"  # first 12 chars of ID
    assert call_args[2] == "myimg"  # Image
    assert call_args[3] == "Up 2 hours"  # Status
    assert call_args[4] == "ctr1"  # Names


@pytest.mark.asyncio
async def test_ps_bad_host_exits():
    """_ps raises Exit(1) when --on names a non-docker-capable host."""
    # Create a host that is NOT docker_capable
    host = MagicMock(spec=UnixHost)
    host.docker_capable = False

    lab = Lab(name="veggies")
    lab.hosts["meh_host"] = host

    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "rprint", MagicMock()),
        pytest.raises(typer.Exit) as exc,
    ):
        await docker_cli._ps(on="ghost")

    assert exc.value.exit_code == 1


@pytest.mark.asyncio
async def test_ps_all_docker_capable_hosts():
    """_ps (no --on) queries all docker-capable hosts in the lab."""
    capable = MagicMock(spec=UnixHost)
    capable.id = "cap_host"
    capable.docker_capable = True

    not_capable = MagicMock(spec=UnixHost)
    not_capable.docker_capable = False

    lab = Lab(name="veggies")
    lab.hosts["cap_host"] = capable
    lab.hosts["nocap_host"] = not_capable

    mock_compose_ps = AsyncMock(return_value=[])
    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "compose_ps", mock_compose_ps),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._ps(on=None)

    # compose_ps must be called for the capable host only
    mock_compose_ps.assert_called_once_with(capable)


@pytest.mark.asyncio
async def test_ps_specific_capable_host():
    """_ps --on <host> queries only the named docker-capable host."""
    capable = MagicMock(spec=UnixHost)
    capable.id = "cap_host"
    capable.docker_capable = True

    lab = Lab(name="veggies")
    lab.hosts["cap_host"] = capable

    mock_compose_ps = AsyncMock(return_value=[])
    mock_rprint = MagicMock()

    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "compose_ps", mock_compose_ps),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._ps(on="cap_host")

    mock_compose_ps.assert_called_once_with(capable)


@pytest.mark.asyncio
async def test_ps_accepts_positional_handle_for_on(tmp_path):
    """_ps --on <handle> resolves a positional handle (e.g. "dut1") to the
    matching docker-capable host, same as a canonical id — --on is a CLI
    host-id input like `otto host`'s positional argument."""
    dut_a = UnixHost(ip="10.0.0.1", creds=[], element="dut", element_id=47, docker_capable=True)
    dut_b = UnixHost(ip="10.0.0.2", creds=[], element="dut", element_id=200, docker_capable=True)

    lab = Lab(name="veggies")
    lab.add_host(dut_a)
    lab.add_host(dut_b)
    lab._assign_logical_indices()
    assert dut_a.id == "dut47"
    assert dut_a.logical_index == 1

    mock_compose_ps = AsyncMock(return_value=[])

    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "compose_ps", mock_compose_ps),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._ps(on="dut1")

    mock_compose_ps.assert_called_once_with(dut_a)


# ---------------------------------------------------------------------------
# _docker_host_completer
# ---------------------------------------------------------------------------


def test_completer_cache_hit():
    """_docker_host_completer returns sorted cached docker_hosts on cache hit."""
    with patch(
        "otto.config.get_completion_names",
        return_value={"docker_hosts": ["h2", "h1"]},
    ):
        result = docker_cli._docker_host_completer(MagicMock(), "")

    assert result == ["h1", "h2"]


def test_completer_cache_miss_falls_back():
    """_docker_host_completer falls back to collect_docker_capable_host_ids on cache miss."""
    with (
        patch("otto.config.get_completion_names", return_value=None),
        patch(
            "otto.config.completion_cache.collect_docker_capable_host_ids",
            return_value=["h2", "h1"],
        ),
        patch("otto.config.get_repos", return_value=[]),
    ):
        result = docker_cli._docker_host_completer(MagicMock(), "")

    assert result == ["h1", "h2"]


def test_completer_prefix_filter():
    """_docker_host_completer filters by incomplete prefix."""
    with patch(
        "otto.config.get_completion_names",
        return_value={"docker_hosts": ["alpha", "beta", "almond"]},
    ):
        result = docker_cli._docker_host_completer(MagicMock(), "al")

    assert result == ["almond", "alpha"]


# ---------------------------------------------------------------------------
# _docker_host_completer lab scoping (issue #138)
# ---------------------------------------------------------------------------


def _ctx_with_labs(lab_names) -> SimpleNamespace:
    """Click-like context chain: ``-l/--lab`` lives on the root ``otto`` ctx."""
    root = SimpleNamespace(info_name="otto", params={"labs": lab_names}, parent=None)
    return SimpleNamespace(info_name="docker", params={}, parent=root)


def test_completer_cache_hit_filters_by_selected_lab():
    """Lab selected: docker-capable suggestions restricted to that lab's hosts."""
    fake_cache = {
        "docker_hosts": ["carrot_seed", "apple_seed"],
        "hosts_by_lab": {"veggies": ["carrot_seed"], "fruits": ["apple_seed"]},
    }
    with patch("otto.config.get_completion_names", return_value=fake_cache):
        result = docker_cli._docker_host_completer(_ctx_with_labs(["veggies"]), "")

    assert result == ["carrot_seed"]


def test_completer_cache_miss_filters_by_selected_lab(tmp_path):
    """Cache miss + lab selected: the live scan is restricted to the lab."""
    import json

    lab = tmp_path / "lab"
    lab.mkdir()
    creds = [{"login": "u", "password": "p"}]
    (lab / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "ip": "1.1.1.1",
                        "element": "carrot",
                        "creds": creds,
                        "docker_capable": True,
                        "labs": ["veggies"],
                    },
                    {
                        "ip": "1.1.1.2",
                        "element": "apple",
                        "creds": creds,
                        "docker_capable": True,
                        "labs": ["fruits"],
                    },
                ]
            }
        )
    )
    repo = SimpleNamespace(labs=[lab], docker_settings=None)
    with (
        patch("otto.config.get_completion_names", return_value=None),
        patch("otto.config.get_repos", return_value=[repo]),
    ):
        result = docker_cli._docker_host_completer(_ctx_with_labs(["veggies"]), "")

    assert result == ["carrot"]


def test_completer_no_lab_selected_keeps_all_docker_hosts():
    fake_cache = {
        "docker_hosts": ["carrot_seed", "apple_seed"],
        "hosts_by_lab": {"veggies": ["carrot_seed"], "fruits": ["apple_seed"]},
    }
    with patch("otto.config.get_completion_names", return_value=fake_cache):
        result = docker_cli._docker_host_completer(_ctx_with_labs(None), "")

    assert result == ["apple_seed", "carrot_seed"]
