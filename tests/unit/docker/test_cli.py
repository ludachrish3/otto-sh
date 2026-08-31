"""Unit tests for `otto.cli.docker` helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from otto.cli import docker as docker_cli
from otto.config.lab import Lab
from otto.config.repo import DockerUseCase, Repo
from otto.docker.deployment import UseCaseStack
from otto.docker.resolve import Displacement, SelectedFragment, Selection
from otto.host.unix_host import UnixHost
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import make_sut_repo
from tests.conftest import active_context

_DOCKER_FILES = {"docker/Dockerfile": "FROM alpine\n", "docker/compose.yml": "services: {}\n"}


def _use_case_block(name: str, host: str) -> str:
    """A [[docker.use_cases]] fragment pinning its stack to *host* — the same
    "declared exact host" semantics ``default_host`` used to carry, now
    expressed as a committed placement pin (spec §14)."""
    return (
        f"\n[[docker.use_cases]]\n"
        f'name = "{name}"\n'
        f'composes = ["core"]\n'
        f'role = "docker"\n'
        f'placement = {{ docker = "{host}" }}\n'
    )


def _make_repo(tmp: Path, *, name: str, host: str) -> Repo:
    sut = make_sut_repo(
        tmp / name,
        name=name,
        extra=(
            "[[docker.composes]]\n"
            'name = "core"\n'
            'path = "docker/compose.yml"\n'
            'services = ["svc"]\n' + _use_case_block(name, host)
        ),
        files=_DOCKER_FILES,
    )
    return Repo(sut_dir=sut)


def _make_repo_with_image(tmp: Path, *, name: str, host: str) -> Repo:
    """Like _make_repo but also declares a [[docker.images]] entry so
    _build's ``if not r.docker_settings.images: continue`` guard is passed."""
    sut = make_sut_repo(
        tmp / name,
        name=name,
        extra=(
            "[[docker.images]]\n"
            'name = "myimage"\n'
            'dockerfile = "docker/Dockerfile"\n'
            'context = "docker"\n'
            "\n[[docker.composes]]\n"
            'name = "core"\n'
            'path = "docker/compose.yml"\n'
            'services = ["svc"]\n' + _use_case_block(name, host)
        ),
        files=_DOCKER_FILES,
    )
    return Repo(sut_dir=sut)


def _make_repo_images_only(tmp: Path, *, name: str) -> Repo:
    """A repo declaring [[docker.images]] but no [[docker.composes]] at all —
    the mirror image of ``_make_repo``. Used to prove ``_up``/``_down`` now
    print a loud notice (and fail if it's all they were given) instead of
    silently doing nothing for a build-only repo."""
    sut = make_sut_repo(
        tmp / name,
        name=name,
        extra=(
            "[[docker.images]]\n"
            'name = "myimage"\n'
            'dockerfile = "docker/Dockerfile"\n'
            'context = "docker"\n'
        ),
        files=_DOCKER_FILES,
    )
    return Repo(sut_dir=sut)


def test_select_repos_filters_by_lab_applicability(tmp_path):
    """A repo whose committed placement pin isn't in the active lab is silently skipped.

    Reproduces the bug from `otto docker down` against a multi-repo workspace
    where one repo targets a host that lives in a different lab.
    """
    repo_in_lab = _make_repo(tmp_path, name="repo1", host="test3")
    repo_out_of_lab = _make_repo(tmp_path, name="repo2", host="alt3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()  # only test3 is in the lab

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo_in_lab, repo_out_of_lab]),
        patch.object(docker_cli, "get_lab", return_value=fake_cfg.lab),
    ):
        selected = docker_cli._select_repos(repo_name=None)

    names = [r.name for r in selected]
    assert names == ["repo1"], f"repo2 (alt3) must be skipped, got {names}"


def test_select_repos_on_does_not_override_lab_filter(tmp_path, capsys):
    """--on chooses where to deploy, not which repos belong to the active lab.

    A repo whose declared placement pin lives in another lab must still be
    skipped even when --on names an in-lab host — otherwise multi-repo
    workspaces would bring up stacks for unrelated labs (see
    test_e2e_multi_repo_only_active_lab_runs). With no other repo to fall
    back on, the exclusion empties the selection entirely — which is now a
    loud exit(1), not the silent empty list this used to return (the exact
    demo failure this task pins: a declared host outside the active lab used
    to exit 0 having printed nothing).
    """
    repo = _make_repo(tmp_path, name="repo2", host="alt3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

    fake_cfg = MagicMock()
    fake_cfg.lab = lab

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=fake_cfg.lab),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._select_repos(repo_name=None, on="test3")

    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "repo2" in out
    assert "alt3" in out


def test_select_repos_skip_notice_survives_rich_markup(tmp_path, capsys):
    """The exclusion notice must reach the terminal whole, brackets included.

    Same class as the ``use-cases`` table's fragment cell: this line is the
    only thing that tells a user WHY their repo was skipped, and rich deletes
    any bracketed span that parses as a style tag. The hostile condition is
    INJECTED (a repo whose name contains ``[bold]``) rather than inherited —
    the notice's own ``['alt3']`` candidate list happens to be tag-safe,
    because the quotes inside a Python list repr keep rich's tag pattern from
    matching, so inheriting it would prove nothing. A repo name is user data
    and reaches the message through ``!r``, where its brackets are bare.
    """
    repo = _make_repo(tmp_path, name="[bold]repo2", host="alt3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._select_repos(repo_name=None)

    assert excinfo.value.exit_code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "[bold]repo2" in out, (
        "rich ate the bracketed span — the skipped repo is misnamed in the one "
        "message that explains the skip"
    )


# ---------------------------------------------------------------------------
# _select_repos — additional coverage: name-filter, no-match exit, bad-on exit
# ---------------------------------------------------------------------------


def test_select_repos_filters_by_repo_name(tmp_path):
    """_select_repos(repo_name=…) keeps only the repo whose name matches."""
    repo1 = _make_repo(tmp_path / "r1", name="repo1", host="test3")
    repo2 = _make_repo(tmp_path / "r2", name="repo2", host="test3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1, repo2]),
        patch.object(docker_cli, "get_lab", return_value=lab),
    ):
        result = docker_cli._select_repos(repo_name="repo2")

    assert [r.name for r in result] == ["repo2"]


def test_select_repos_no_match_exits(tmp_path):
    """_select_repos raises Exit(1) when repo_name matches nothing."""
    repo1 = _make_repo(tmp_path / "r1", name="repo1", host="test3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

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
    repo1 = _make_repo(tmp_path / "r1", name="repo1", host="test3")

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "rprint"),
        pytest.raises(typer.Exit) as exc,
    ):
        docker_cli._select_repos(repo_name=None, on="ghost")

    assert exc.value.exit_code == 1


def test_select_repos_is_blind_to_debris_deploy_time_still_refuses_it(tmp_path):
    """_select_repos harvests a pin's VALUE blindly, without validating that
    its KEY even addresses the fragment's own role (T14 review I1): a
    fragment declaring ``role = "docker"`` but ``placement = { web = ... }``
    is config debris ``_place_fragment`` refuses outright, but this coarse
    lab-applicability filter has no way to see that -- it only reads
    ``uc.placement.values()``.

    This is not a hole: the case selection no longer surfaces is caught
    later, once and specifically, at RESOLVE time -- the same
    ``_resolve_parent`` a bare ``otto docker build``/``up`` without --on
    would call next. Proving both halves is the point: selection stays
    silent about the debris (it neither excludes for the "right" reason nor
    raises), and resolution refuses it with the exact, actionable phrase.
    """
    from otto.docker.compose import _resolve_parent

    sut = make_sut_repo(
        tmp_path / "r1",
        name="repo1",
        extra=(
            "[[docker.composes]]\n"
            'name = "core"\n'
            'path = "docker/compose.yml"\n'
            'services = ["svc"]\n'
            "\n[[docker.use_cases]]\n"
            'name = "repo1"\n'
            'composes = ["core"]\n'
            'role = "docker"\n'
            'placement = { web = "test3" }\n'  # mis-keyed: role is "docker", not "web"
        ),
        files=_DOCKER_FILES,
    )
    repo1 = Repo(sut_dir=sut)

    lab = Lab(name="unix")
    lab.hosts["test3"] = MagicMock()

    # Half 1: selection harvests the pin's value blindly and keeps the repo
    # (test3 IS in the active lab) -- it never even looks at the key.
    with (
        patch.object(docker_cli, "get_repos", return_value=[repo1]),
        patch.object(docker_cli, "get_lab", return_value=lab),
    ):
        selected = docker_cli._select_repos(repo_name=None)
    assert [r.name for r in selected] == ["repo1"]

    # Half 2: the SAME repo, resolved for real, refuses -- with the specific
    # config-debris phrase, not a generic "not in lab" message.
    with pytest.raises(ValueError, match="the pin can never apply to this fragment in any lab"):
        _resolve_parent(repo1, lab, on=None)


# ---------------------------------------------------------------------------
# _build command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_success(tmp_path):
    """_build prints a green 'built' line when build_images returns Success."""
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", host="test3")

    mock_rprint = MagicMock()
    mock_build = AsyncMock(
        return_value={
            "myimage": CommandResult(
                Status.Success,
                value="sha256:abc",
                msg="sha256:abc",
                command="docker build",
                retcode=0,
            )
        }
    )

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
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", host="test3")

    mock_rprint = MagicMock()
    mock_build = AsyncMock(
        return_value={
            "myimage": CommandResult(
                Status.Skipped, value="already exists", msg="already exists", command="", retcode=-1
            )
        }
    )

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
    repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", host="test3")

    mock_build = AsyncMock(
        return_value={
            "myimage": CommandResult(
                Status.Failed, value="build error", command="docker build", retcode=1
            )
        }
    )

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
    """_build prints a notice (not silence) for a repo with no docker.images,
    and — being the ONLY selected repo — acted on nothing, so it fails loud."""
    # _make_repo produces a repo with composes but no images
    repo = _make_repo(tmp_path / "r1", name="myrepo", host="test3")
    mock_build = AsyncMock()

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    # build_images must NOT be called when images is empty
    mock_build.assert_not_called()
    assert excinfo.value.exit_code == 1


@pytest.mark.asyncio
async def test_build_composes_only_workspace_fails_loud(tmp_path, capsys):
    """A composes-only workspace running `otto docker build` must not exit 0
    having silently built nothing — the same failure class `_select_repos`
    was fixed for, one layer down (spec §13)."""
    repo = _make_repo(tmp_path / "r1", name="repo1", host="test3")

    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", AsyncMock()),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "repo1" in out
    assert "declares no" in out


@pytest.mark.asyncio
async def test_build_mixed_workspace_acts_on_one_prints_notice_for_other(tmp_path, capsys):
    """When at least one selected repo declares images, `_build` exits
    normally even though a composes-only sibling is skipped — but the skip
    must still be printed, not silently dropped."""
    acted = _make_repo_with_image(tmp_path / "r1", name="acted", host="test3")
    skipped = _make_repo(tmp_path / "r2", name="skipped", host="test3")
    mock_build = AsyncMock(
        return_value={
            "myimage": CommandResult(
                Status.Success,
                value="sha256:abc",
                msg="sha256:abc",
                command="docker build",
                retcode=0,
            )
        }
    )

    with (
        patch.object(docker_cli, "_select_repos", return_value=[acted, skipped]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
        patch.object(docker_cli, "build_images", mock_build),
    ):
        await docker_cli._build(repo=None, on=None, rebuild=False, image=None)  # must not raise

    out = capsys.readouterr().out
    assert "skipped" in out
    assert "declares no" in out


# ---------------------------------------------------------------------------
# _up command
# ---------------------------------------------------------------------------


class TestGenuineSkipsStayedSkipped:
    """The dry-run contract's ``Status.NotRun`` sweep must not have touched these.

    ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` section 4
    introduced ``Status.NotRun`` for "a dry run declined this" and left
    ``Status.Skipped`` (whose ``is_ok`` is True) for genuine skips. Two
    genuine skips live in this package -- a build-cache hit and a repo with no
    ``[[docker.composes]]`` -- and ``otto/cli/docker.py`` branches on
    ``Status.Skipped`` to print ``cached`` and ``nothing to tear down``. Flip
    either producer and those two lines print the wrong thing.

    The mocked ``test_build_skipped`` / ``test_down_skipped`` above cannot
    catch that: they HARDCODE ``Status.Skipped`` in the double, so they stay
    green against a producer that stopped emitting it. These run the REAL
    producer and feed its own result to the real CLI branch, so the two ends
    are pinned together.
    """

    @pytest.mark.asyncio
    async def test_a_cache_hit_is_a_genuine_skip_and_still_prints_cached(self, tmp_path):
        from otto.docker.build import _build_one

        repo = _make_repo_with_image(tmp_path / "r1", name="myrepo", host="test3")
        settings = repo.docker_settings

        class _CachedParent:
            """A parent on which ``docker image inspect`` and ``docker tag`` succeed.

            That IS the cache hit -- the condition is injected here rather than
            inherited from a canned Result, so the Skipped under assertion is
            ``_build_one``'s own.
            """

            async def exec(self, cmd, **_kw):
                return CommandResult(Status.Success, value="", command=cmd, retcode=0)

        cached = await _build_one(
            _CachedParent(), "myrepo", settings, settings.images[0], rebuild=False
        )
        assert cached.status is Status.Skipped, (
            "a build-cache hit is a genuine skip -- nothing was declined, the "
            "image is already there"
        )
        assert cached.is_ok is True

        mock_rprint = MagicMock()
        with (
            patch.object(docker_cli, "_select_repos", return_value=[repo]),
            patch.object(docker_cli, "get_lab", return_value=MagicMock()),
            patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
            patch.object(docker_cli, "build_images", AsyncMock(return_value={"myimage": cached})),
            patch.object(docker_cli, "rprint", mock_rprint),
        ):
            await docker_cli._build(repo=None, on=None, rebuild=False, image=None)

        printed = " ".join(str(c) for c in mock_rprint.call_args_list)
        assert "cached" in printed, f"the cache-hit line stopped saying 'cached': {printed}"
        assert "FAILED" not in printed


# ---------------------------------------------------------------------------
# use-case plumbing: _default_use_case, _parse_provide, _parse_env
# ---------------------------------------------------------------------------


def _uc(
    name: str = "integration",
    *,
    composes=("core",),
    role=None,
    provides=None,
    priority=0,
    env=None,
    pass_env=(),
) -> DockerUseCase:
    """One `[[docker.use_cases]]` fragment, as settings parsing would build it."""
    return DockerUseCase(
        name=name,
        composes=tuple(composes),
        role=role,
        provides=provides,
        priority=priority,
        env=dict(env or {}),
        pass_env=tuple(pass_env),
    )


def _uc_repo(name: str, *fragments: DockerUseCase) -> SimpleNamespace:
    """A repo table in the shape `declared_use_cases`/`select_fragments` read."""
    return SimpleNamespace(
        name=name,
        docker_settings=SimpleNamespace(use_cases=tuple(fragments), composes=(), images=()),
    )


def test_default_use_case_returns_the_only_one_declared():
    with patch.object(docker_cli, "get_repos", return_value=[_uc_repo("a", _uc())]):
        assert docker_cli._default_use_case(None) == "integration"


def test_default_use_case_passes_an_explicit_name_through_unresolved():
    """An explicit name is NOT checked here — `select_fragments` owns that refusal.

    Checking it twice would give one mistake two different messages depending
    on which verb the user reached it through.
    """
    with patch.object(docker_cli, "get_repos", return_value=[]):
        assert docker_cli._default_use_case("whatever") == "whatever"


def test_default_use_case_refuses_when_two_are_declared(capsys):
    repos = [_uc_repo("a", _uc("integration")), _uc_repo("b", _uc("soak"))]
    with (
        patch.object(docker_cli, "get_repos", return_value=repos),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._default_use_case(None)
    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "integration" in out
    assert "soak" in out


def test_default_use_case_refuses_when_none_are_declared(capsys):
    with (
        patch.object(docker_cli, "get_repos", return_value=[_uc_repo("a")]),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._default_use_case(None)
    assert excinfo.value.exit_code == 1
    assert "[[docker.use_cases]]" in capsys.readouterr().out


def test_parse_provide_splits_on_the_first_equals():
    assert docker_cli._parse_provide(["edge=repo1"]) == {"edge": "repo1"}


def test_parse_provide_refuses_a_malformed_pair_naming_the_form():
    with pytest.raises(typer.BadParameter) as excinfo:
        docker_cli._parse_provide(["edge"])
    assert "CAPABILITY=REPO" in str(excinfo.value)


def test_parse_env_keeps_every_equals_after_the_first_in_the_value():
    assert docker_cli._parse_env(["URL=a=b"]) == {"URL": "a=b"}


def test_parse_env_refuses_a_value_with_no_equals_naming_the_form():
    with pytest.raises(typer.BadParameter) as excinfo:
        docker_cli._parse_env(["JUSTAKEY"])
    assert "KEY=VALUE" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _up / _down — the use-case verbs
# ---------------------------------------------------------------------------


def _stub_stack(*, use_case="integration", displaced=()) -> UseCaseStack:
    """A `deploy` return value with one registered container on one host."""
    container = MagicMock()
    container.id = "test3.integration.api"
    container.container_id = "abcdef123456789"
    return UseCaseStack(
        use_case=use_case,
        selection=Selection(use_case=use_case, fragments=[], displaced=list(displaced)),
        projects={"test3": "unix-integration-u"},
        hosts={"api": container},
        by_host={"test3": {"api": container}},
    )


@pytest.mark.asyncio
async def test_up_deploys_the_named_use_case_and_reports_it(tmp_path):
    deploy = AsyncMock(return_value=_stub_stack())
    mock_rprint = MagicMock()
    with (
        patch("otto.docker.deployment.deploy", deploy),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._up(use_case="integration", service=None)

    assert deploy.call_args.args == ("integration",)
    assert deploy.call_args.kwargs["services"] is None
    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[green]" in printed
    assert "container(s) registered" in printed
    assert "unix-integration-u" in printed
    assert "abcdef123456" in printed


@pytest.mark.asyncio
async def test_up_narrows_to_the_named_services():
    """MUTATION PROBE: service narrowing reaches the library through the CLI."""
    deploy = AsyncMock(return_value=_stub_stack())
    with patch("otto.docker.deployment.deploy", deploy), patch.object(docker_cli, "rprint"):
        await docker_cli._up(use_case="integration", service=["api", "db"])

    assert deploy.call_args.kwargs["services"] == ["api", "db"]


@pytest.mark.asyncio
async def test_up_deploys_the_only_declared_use_case_when_bare():
    deploy = AsyncMock(return_value=_stub_stack())
    with (
        patch("otto.docker.deployment.deploy", deploy),
        patch.object(docker_cli, "get_repos", return_value=[_uc_repo("a", _uc())]),
        patch.object(docker_cli, "rprint"),
    ):
        await docker_cli._up()

    assert deploy.call_args.args == ("integration",)


@pytest.mark.asyncio
async def test_up_bare_refuses_when_two_use_cases_are_declared(capsys):
    deploy = AsyncMock()
    repos = [_uc_repo("a", _uc("integration")), _uc_repo("b", _uc("soak"))]
    with (
        patch("otto.docker.deployment.deploy", deploy),
        patch.object(docker_cli, "get_repos", return_value=repos),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._up()

    deploy.assert_not_called()
    assert excinfo.value.exit_code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "integration" in out
    assert "soak" in out
    # The DISTINGUISHING phrase, not just the names: "many declared" and
    # "none declared" are different refusals and must be pinned apart.
    assert "name the one you mean" in out


@pytest.mark.asyncio
async def test_up_forwards_provide_env_and_env_files(tmp_path):
    """MUTATION PROBE: --provide reaches the selection, --env/--env-file the merge."""
    env_file = tmp_path / "extra.env"
    env_file.write_text("K=V\n")
    deploy = AsyncMock(return_value=_stub_stack())
    with patch("otto.docker.deployment.deploy", deploy), patch.object(docker_cli, "rprint"):
        await docker_cli._up(
            use_case="integration",
            service=None,
            on="test3",
            no_build=True,
            provide=["edge=repo1"],
            env=["A=1", "B=2"],
            env_file=[env_file],
        )

    kwargs = deploy.call_args.kwargs
    assert kwargs["provide"] == {"edge": "repo1"}
    assert kwargs["env"] == {"A": "1", "B": "2"}
    assert kwargs["env_files"] == [env_file]
    assert kwargs["on"] == "test3"
    assert kwargs["build"] is False


@pytest.mark.asyncio
async def test_up_prints_displacements_as_they_are():
    """A --provide override can leave the WINNER carrying the lower priority,
    and the loser can be the winner's own repo. The line must survive both."""
    displaced = [
        Displacement(
            capability="edge",
            loser_repo="repo1",
            loser_priority=10,
            winner_repo="repo1",
            winner_priority=5,
        )
    ]
    deploy = AsyncMock(return_value=_stub_stack(displaced=displaced))
    mock_rprint = MagicMock()
    with (
        patch("otto.docker.deployment.deploy", deploy),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._up(use_case="integration", service=None)

    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "edge" in printed
    assert "priority 5" in printed
    assert "priority 10" in printed
    assert "lower" not in printed


@pytest.mark.asyncio
async def test_up_surfaces_the_librarys_on_refusal_verbatim(capsys):
    """T7 review I3, CLI side: `--on` naming no lab host refuses LOUDLY here.

    The REAL `deploy` runs — only its lab is stubbed — so the sentence under
    assertion is the one `_canonical_on` actually produces, not one this test
    wrote and then found in its own output. A library rewording turns this red
    where a hand-written copy would have stayed green while asserting a
    sentence nobody emits any more (review M5).
    """
    lab = Lab(name="unix")
    lab.add_host(UnixHost(ip="10.0.0.1", creds=[], element="test", element_id=3))

    with (
        patch("otto.docker.deployment.get_lab", return_value=lab),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._up(use_case="integration", service=None, on="ghost")

    assert excinfo.value.exit_code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "on='ghost' matches no host in lab 'unix'" in out
    assert "test3" in out, "the refusal must name what IS available"


@pytest.mark.asyncio
async def test_up_renders_a_dry_run_decline_and_exits_zero():
    """The decline is the ANSWER a dry run asked for, not a failure.

    ``active_context(dry_run=True)`` INJECTS the condition rather than letting
    the raised decline stand in for it: exiting 0 here is only correct because
    this really is a dry run, and `_run_use_case` checks (review M8) instead of
    inferring it from the exception class.
    """
    from otto.result import CommandNotRunError

    decline = CommandNotRunError("deploy(integration)", "test3", "Resolved plan: test3 <- a[core].")
    mock_rprint = MagicMock()
    with (
        active_context(dry_run=True),
        patch("otto.docker.deployment.deploy", AsyncMock(side_effect=decline)),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._up(use_case="integration", service=None)  # no typer.Exit

    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "Resolved plan: test3" in printed
    assert "container(s) registered" not in printed


@pytest.mark.asyncio
async def test_a_decline_outside_a_dry_run_is_not_swallowed_into_exit_zero():
    """M8: the exit-0 arm is CHECKED, not inferred from the exception class.

    Every raise site of ``CommandNotRunError`` is a dry-run arm today. If one
    ever escapes a real deployment, printing it in magenta and exiting 0 would
    report success for a stack nobody brought up — so the guard reads the
    context, and without a dry run the error propagates untouched.

    ``active_context(dry_run=False)`` INJECTS the negative condition rather
    than inheriting whatever the ambient context happens to be (T14 review
    I2/C8 fold-in): without it, this test's "not a dry run" premise is only
    true by accident of test order, and a leaked ``dry_run=True`` from a
    neighbour would turn a real defect into a false red instead of the
    guard proving anything.
    """
    from otto.result import CommandNotRunError

    decline = CommandNotRunError("deploy(integration)", "test3", "Resolved plan: test3 <- a[core].")
    with (
        active_context(dry_run=False),
        patch("otto.docker.deployment.deploy", AsyncMock(side_effect=decline)),
        patch.object(docker_cli, "rprint", MagicMock()),
        pytest.raises(CommandNotRunError),
    ):
        await docker_cli._up(use_case="integration", service=None)


def test_a_stack_that_registered_nothing_says_so_rather_than_printing_nothing():
    """M6: the documented-unreachable arm, exercised rather than ledgered.

    Not reachable from a resolvable selection (a winner always participates,
    and a `services=` narrowing matching nothing is refused in the library), so
    it is driven directly. It must stay a loud yellow line and must NOT invent
    an exit code for a state nobody can produce.
    """
    empty = UseCaseStack(
        use_case="integration",
        selection=Selection(use_case="integration", fragments=[]),
    )
    mock_rprint = MagicMock()
    with patch.object(docker_cli, "rprint", mock_rprint):
        docker_cli._print_stack_report(empty)  # no typer.Exit

    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[yellow]" in printed
    assert "registered no container on any host" in printed


@pytest.mark.asyncio
async def test_down_tears_down_the_named_use_case_and_services():
    teardown = AsyncMock(return_value=None)
    mock_rprint = MagicMock()
    with (
        patch("otto.docker.deployment.teardown", teardown),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._down(use_case="integration", service=["api"])

    assert teardown.call_args.args == ("integration",)
    assert teardown.call_args.kwargs["services"] == ["api"]
    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "[green]" in printed
    assert "integration" in printed


@pytest.mark.asyncio
async def test_down_renders_a_dry_run_decline_without_claiming_a_teardown():
    from otto.result import CommandNotRunError

    decline = CommandNotRunError("teardown(integration)", "test3", "Resolved plan: test3 <- a.")
    mock_rprint = MagicMock()
    with (
        active_context(dry_run=True),
        patch("otto.docker.deployment.teardown", AsyncMock(side_effect=decline)),
        patch.object(docker_cli, "rprint", mock_rprint),
    ):
        await docker_cli._down(use_case="integration", service=None)

    printed = " ".join(str(c) for c in mock_rprint.call_args_list)
    assert "Resolved plan: test3" in printed
    assert "[green]" not in printed


@pytest.mark.asyncio
async def test_down_surfaces_a_library_refusal(capsys):
    from otto.docker.resolve import UseCaseResolutionError

    with (
        patch(
            "otto.docker.deployment.teardown",
            AsyncMock(side_effect=UseCaseResolutionError("no active repo declares use-case 'x'")),
        ),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._down(use_case="x", service=None)

    assert excinfo.value.exit_code == 1
    assert "no active repo declares use-case 'x'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `otto docker build <USE_CASE>` — narrow to the winners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_narrows_to_the_use_case_winners(tmp_path):
    """`build integration` builds only the repos whose fragments won."""
    winner = _make_repo_with_image(tmp_path / "r1", name="repo1", host="test3")
    loser = _make_repo_with_image(tmp_path / "r2", name="repo2", host="test3")
    selection = Selection(
        use_case="integration",
        fragments=[SelectedFragment(repo=winner, fragment=_uc())],
    )
    built: list[str] = []

    async def _build_images(repo, _parent, **_kw):
        built.append(repo.name)
        return {}

    with (
        patch.object(docker_cli, "_select_repos", return_value=[winner, loser]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "get_repos", return_value=[winner, loser]),
        patch.object(docker_cli, "_resolve_parent_for_repo", return_value=MagicMock()),
        patch("otto.docker.resolve.select_fragments", return_value=selection),
        patch.object(docker_cli, "build_images", AsyncMock(side_effect=_build_images)),
        patch.object(docker_cli, "rprint", MagicMock()),
    ):
        await docker_cli._build(use_case="integration")

    assert built == ["repo1"], f"the losing repo's images were built too: {built}"


@pytest.mark.asyncio
async def test_build_use_case_narrowing_to_nothing_fails_loud(tmp_path, capsys):
    other = _make_repo_with_image(tmp_path / "r1", name="repo1", host="test3")
    selection = Selection(
        use_case="integration",
        fragments=[SelectedFragment(repo=_uc_repo("elsewhere", _uc()), fragment=_uc())],
    )
    with (
        patch.object(docker_cli, "_select_repos", return_value=[other]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "get_repos", return_value=[other]),
        patch("otto.docker.resolve.select_fragments", return_value=selection),
        patch.object(docker_cli, "build_images", AsyncMock()),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._build(use_case="integration")

    assert excinfo.value.exit_code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "integration" in out
    assert "there is nothing to build for it" in out


@pytest.mark.asyncio
async def test_build_surfaces_a_use_case_resolution_refusal(tmp_path, capsys):
    from otto.docker.resolve import UseCaseResolutionError

    repo = _make_repo_with_image(tmp_path / "r1", name="repo1", host="test3")
    with (
        patch.object(docker_cli, "_select_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=MagicMock()),
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch(
            "otto.docker.resolve.select_fragments",
            side_effect=UseCaseResolutionError("capability 'edge' is tied at priority 5"),
        ),
        patch.object(docker_cli, "build_images", AsyncMock()),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await docker_cli._build(use_case="integration")

    assert excinfo.value.exit_code == 1
    assert "tied at priority 5" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `otto docker use-cases` — the inventory verb
# ---------------------------------------------------------------------------


def test_use_cases_lists_fragments_hosts_env_keys_and_displacements(capsys):
    """Fragment rows, the resolved host, env KEY names, displacement notes."""
    winner = _uc_repo("repo1", _uc(provides="edge", priority=10, env={"EDGE_ADDR": "SECRETVALUE"}))
    loser = _uc_repo("repo2", _uc(provides="edge", priority=5, pass_env=("TOKEN",)))
    lab = Lab(name="unix")
    host = UnixHost(ip="10.0.0.1", creds=[], element="test", element_id=3, docker_capable=True)
    lab.add_host(host)

    with (
        patch.object(docker_cli, "get_repos", return_value=[winner, loser]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
    ):
        docker_cli._use_cases()

    out = " ".join(capsys.readouterr().out.split())
    assert "integration" in out
    assert "repo1" in out
    assert "repo2" in out
    assert "edge" in out
    assert "EDGE_ADDR" in out, "the env KEY name must be listed"
    assert "SECRET" not in out, "the env VALUE must never be printed"
    assert "TOKEN" in out
    assert "displaced" in out
    assert "goes to repo1 (priority 10)" in out, "who won, and at what, in full"
    assert "test3" in out, "the resolved host must be shown"


def test_use_cases_renders_the_compose_names_literally_not_as_rich_markup(capsys):
    """The `repo[compose,...]` cell must survive rich's markup parser intact.

    `[core]` is valid rich style syntax: unescaped, the console swallows it and
    prints a bare `repo2` — so a repo contributing TWO fragments with different
    compose stacks rendered two identical rows (found on the live bed, T15).
    The brackets are the only thing distinguishing them, so they are pinned.
    """
    repo = _uc_repo(
        "repo2",
        _uc(name="soak", composes=("core",), role="edge"),
        _uc(name="soak", composes=("mock",), role="dut"),
    )
    lab = Lab(name="unix")

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
    ):
        docker_cli._use_cases()

    out = " ".join(capsys.readouterr().out.split())
    assert "repo2[core]" in out, "the literal brackets must reach the terminal"
    assert "repo2[mock]" in out, "the second fragment must render DIFFERENTLY"


def test_use_cases_prints_the_resolution_error_instead_of_a_host(capsys):
    """An unresolvable placement is REPORTED, not raised — this verb is a listing."""
    repo = _uc_repo("repo1", _uc(role="nosuchrole"))
    lab = Lab(name="unix")
    lab.add_host(
        UnixHost(ip="10.0.0.1", creds=[], element="test", element_id=3, docker_capable=True)
    )

    with (
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
    ):
        docker_cli._use_cases()  # exit 0

    out = " ".join(capsys.readouterr().out.split())
    assert "nosuchrole" in out


def test_use_cases_prints_a_provider_tie_refusal_instead_of_a_table_row(capsys):
    """I4 / spec §10: the SELECTION refusal, not just the placement one.

    The tie is INJECTED — two repos really do declare the same capability at
    the same priority, so the sentence under assertion is `_highest_priority`'s
    own, reached through the real `select_fragments`. It is the refusal a user
    is most likely to hit, and a listing must render it rather than raise: the
    other use-cases still have answers.
    """
    a = _uc_repo("repo1", _uc(provides="edge", priority=5))
    b = _uc_repo("repo2", _uc(provides="edge", priority=5))
    healthy = _uc_repo("repo3", _uc("soak"))
    lab = Lab(name="unix")
    lab.add_host(
        UnixHost(ip="10.0.0.1", creds=[], element="test", element_id=3, docker_capable=True)
    )

    with (
        patch.object(docker_cli, "get_repos", return_value=[a, b, healthy]),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
    ):
        docker_cli._use_cases()  # exit 0 — a listing reports, it does not raise

    out = " ".join(capsys.readouterr().out.split())
    assert "capability 'edge' is tied at priority 5 between repos ['repo1', 'repo2']" in out
    # The tied use-case does not suppress the healthy one.
    assert "use-case soak" in out


def test_use_cases_filters_to_the_named_use_case(capsys):
    """Spec §10: `otto docker use-cases [USE_CASE]`."""
    repos = [_uc_repo("repo1", _uc("integration")), _uc_repo("repo2", _uc("soak"))]
    lab = Lab(name="unix")
    lab.add_host(
        UnixHost(ip="10.0.0.1", creds=[], element="test", element_id=3, docker_capable=True)
    )

    with (
        patch.object(docker_cli, "get_repos", return_value=repos),
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch("otto.docker.resolve.scope_for_repo", return_value=None),
    ):
        docker_cli._use_cases(use_case="soak")

    out = " ".join(capsys.readouterr().out.split())
    assert "use-case soak" in out
    assert "use-case integration" not in out, "the filter listed a use-case nobody asked for"


def test_use_cases_filter_naming_nothing_refuses_loudly(capsys):
    """A filter naming no declared use-case is a USER error about the argument.

    Loud (exit 1) even though this verb only REPORTS a placement that cannot
    resolve: the difference is that a typo'd argument has no answer to print,
    and exiting 0 on an empty listing is how "you spelled it wrong" reads as
    "you have no use-cases".
    """
    with (
        patch.object(docker_cli, "get_repos", return_value=[_uc_repo("repo1", _uc())]),
        patch.object(docker_cli, "get_lab", return_value=Lab(name="unix")),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._use_cases(use_case="integraton")

    assert excinfo.value.exit_code == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "no active repo declares use-case 'integraton'" in out
    assert "declared: integration" in out, "the refusal must name what IS declared"


def test_use_cases_says_so_when_nothing_is_declared(capsys):
    with (
        patch.object(docker_cli, "get_repos", return_value=[_uc_repo("repo1")]),
        patch.object(docker_cli, "get_lab", return_value=Lab(name="unix")),
    ):
        docker_cli._use_cases()  # exit 0 — an empty inventory is an answer

    assert "[[docker.use_cases]]" in capsys.readouterr().out


def test_a_malformed_provide_is_a_usage_error_through_the_dispatch():
    """`BadParameter` from a BODY must still reach click's usage-error exit 2.

    Raised after the leaf started, not during parsing, so nothing but click's
    standalone handling turns it into exit 2 — and the parse happens before
    the lab or the library is touched, which is what keeps the message about
    the flag rather than about a deployment.
    """
    from otto.cli.docker import docker_app
    from tests._fixtures.dispatch import DispatchRunner

    deploy = AsyncMock()
    with patch("otto.docker.deployment.deploy", deploy):
        result = DispatchRunner().invoke(
            docker_app, ["up", "integration", "--provide", "edge"], spec_name="docker"
        )

    deploy.assert_not_called()
    assert result.exit_code == 2, result.output
    assert "CAPABILITY=REPO" in " ".join(result.output.split()), result.output


def test_use_cases_runs_through_the_production_dispatch():
    """The group's one SYNC leaf — it must survive the leaf-invoke wrapper.

    Every other `otto docker` leaf is `async def` and reaches its body through
    the wrapper's coroutine bridge. A sync leaf takes the other branch, and no
    other test in this module drives one: the rest call the bodies directly.
    Registered here with the SHIPPED spec name so the dispatch composed is the
    one production composes.
    """
    from otto.cli.docker import docker_app
    from tests._fixtures.dispatch import DispatchRunner

    with (
        patch.object(docker_cli, "get_repos", return_value=[_uc_repo("repo1", _uc())]),
        patch.object(docker_cli, "get_lab", return_value=Lab(name="unix")),
    ):
        result = DispatchRunner().invoke(docker_app, ["use-cases"], spec_name="docker")

    assert result.exit_code == 0, result.output
    assert "integration" in result.output


def test_use_case_completer_cache_hit():
    with patch(
        "otto.config.get_completion_names",
        return_value={"docker_use_cases": ["soak", "integration"]},
    ):
        assert docker_cli._use_case_completer(MagicMock(), "") == ["integration", "soak"]


def test_use_case_completer_cache_miss_falls_back_to_the_repos():
    with (
        patch("otto.config.get_completion_names", return_value=None),
        patch("otto.config.get_repos", return_value=[_uc_repo("a", _uc()), _uc_repo("b", _uc())]),
    ):
        assert docker_cli._use_case_completer(MagicMock(), "") == ["integration"]


def test_use_case_completer_filters_by_prefix():
    with patch(
        "otto.config.get_completion_names",
        return_value={"docker_use_cases": ["soak", "integration", "install"]},
    ):
        assert docker_cli._use_case_completer(MagicMock(), "ins") == ["install"]


# ---------------------------------------------------------------------------
# _ps command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ps_all_hosts_table(tmp_path):
    """_ps queries compose_ps for a docker-capable host and passes row data to Table."""
    host = MagicMock(spec=UnixHost)
    host.id = "test3"
    host.docker_capable = True

    lab = Lab(name="unix")
    lab.hosts["test3"] = host

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
    assert call_args[0] == "test3"  # host id
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

    lab = Lab(name="unix")
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

    lab = Lab(name="unix")
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

    lab = Lab(name="unix")
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

    lab = Lab(name="unix")
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
        "docker_hosts": ["test1", "alt2"],
        "hosts_by_lab": {"unix": ["test1"], "unix_alt": ["alt2"]},
    }
    with patch("otto.config.get_completion_names", return_value=fake_cache):
        result = docker_cli._docker_host_completer(_ctx_with_labs(["unix"]), "")

    assert result == ["test1"]


def test_completer_cache_miss_filters_by_selected_lab(tmp_path):
    """Cache miss + lab selected: the live scan is restricted to the lab."""
    lab = tmp_path / "lab"
    lab.mkdir()
    creds = [{"login": "u", "password": "p"}]
    write_lab_json(
        lab / "lab.json",
        [
            {
                "ip": "1.1.1.1",
                "element": "test1",
                "creds": creds,
                "docker_capable": True,
                "labs": ["unix"],
            },
            {
                "ip": "1.1.1.2",
                "element": "alt2",
                "creds": creds,
                "docker_capable": True,
                "labs": ["unix_alt"],
            },
        ],
    )
    repo = SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]),
        docker_settings=None,
        sut_dir=tmp_path,
        # `build_inventory` reads it on the enumeration path; a stand-in that
        # omits it enumerates no hosts at all.
        inventory_settings={},
    )
    with (
        patch("otto.config.get_completion_names", return_value=None),
        patch("otto.config.get_repos", return_value=[repo]),
    ):
        result = docker_cli._docker_host_completer(_ctx_with_labs(["unix"]), "")

    assert result == ["test1"]


def test_completer_no_lab_selected_keeps_all_docker_hosts():
    fake_cache = {
        "docker_hosts": ["test1", "alt2"],
        "hosts_by_lab": {"unix": ["test1"], "unix_alt": ["alt2"]},
    }
    with patch("otto.config.get_completion_names", return_value=fake_cache):
        result = docker_cli._docker_host_completer(_ctx_with_labs(None), "")

    assert result == ["alt2", "test1"]


def test_select_repos_empty_selection_fails_loud(tmp_path, capsys):
    """Regression for the reported demo failure: a declared host outside the
    active lab used to be skipped at DEBUG and the command exited 0 silently."""
    repo = _make_repo(tmp_path, name="repo1", host="not-in-lab")
    lab = MagicMock(spec=Lab)
    lab.name = "unix"
    lab.hosts = {"test3": MagicMock(spec=UnixHost)}
    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "get_repos", return_value=[repo]),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._select_repos(None)
    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "repo1" in out  # the repo is NAMED
    assert "not-in-lab" in out  # and so is WHY it was excluded


def test_select_repos_prints_exclusions_even_when_others_selected(tmp_path, capsys):
    kept = _make_repo(tmp_path, name="kept", host="test3")
    skipped = _make_repo(tmp_path, name="skipped", host="elsewhere")
    lab = MagicMock(spec=Lab)
    lab.name = "unix"
    lab.hosts = {"test3": MagicMock(spec=UnixHost)}
    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "get_repos", return_value=[kept, skipped]),
    ):
        selected = docker_cli._select_repos(None)
    assert [r.name for r in selected] == ["kept"]
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "elsewhere" in out


def test_select_repos_no_docker_repos_fails_loud(tmp_path, capsys):
    """A workspace with zero [docker] repos must say so, not exit 0 silently."""
    lab = MagicMock(spec=Lab)
    lab.name = "unix"
    lab.hosts = {"test3": MagicMock(spec=UnixHost)}
    from otto.config.repo import DockerSettings

    plain = MagicMock(spec=Repo)
    plain.name = "plain"
    plain.docker_settings = DockerSettings()  # real empty settings — Mock attrs are
    # truthy and would defeat the `if not ... composes` guards
    with (
        patch.object(docker_cli, "get_lab", return_value=lab),
        patch.object(docker_cli, "get_repos", return_value=[plain]),
        pytest.raises(typer.Exit) as excinfo,
    ):
        docker_cli._select_repos(None)
    assert excinfo.value.exit_code == 1
    assert "no active repo declares a [docker] section" in capsys.readouterr().out.lower()
