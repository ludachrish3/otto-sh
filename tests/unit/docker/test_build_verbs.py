"""build_on: the image-level verb. Every rule is the library's; the host is required."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from otto.docker import build_verbs as verbs_mod
from otto.docker import deployment as deploy_mod
from otto.docker.build import BuildOptions
from otto.docker.build_verbs import build_on, compose_build
from otto.docker.deployment import UseCaseResolution
from otto.docker.observe import DockerVerbError
from otto.docker.reports import BuildReport, ImageBuild
from otto.docker.resolve import Displacement, SelectedFragment, Selection, UseCaseResolutionError
from otto.result import CommandNotRunError, CommandResult
from otto.utils import Status

from .test_deploy import _frag, _host, _lab


def _ok(value="") -> CommandResult:
    return CommandResult(Status.Success, value=value, command="docker build", retcode=0)


def _fail(value="boom") -> CommandResult:
    return CommandResult(Status.Failed, value=value, command="docker build", retcode=1)


def _image(name, *, is_archive=False, context=None):
    return SimpleNamespace(
        name=name,
        dockerfile=Path("/ctx/Dockerfile"),
        context=context if context is not None else Path("/ctx"),
        target=None,
        build_args=(),
        is_archive=is_archive,
        dockerfile_in_archive="",
    )


def _repo(name, *, images=(), composes=("core",)):
    return SimpleNamespace(
        name=name,
        docker_settings=SimpleNamespace(
            images=tuple(_image(n) for n in images), composes=tuple(composes), use_cases=()
        ),
    )


@pytest.fixture
def lab():
    return _lab(_host("test3", "10.10.200.13"), _host("alt2", "10.10.200.22"))


@pytest.fixture
def install(lab):
    """Patch the config seams; returns a function taking the repo list (dependency order)."""

    @contextmanager
    def _install(repos, ordered=None):
        with (
            patch("otto.config.fleet.get_lab", lambda: lab),
            patch.multiple(
                "otto.config.bootstrapped",
                get_repos=lambda: list(repos),
                get_ordered_repos=lambda: list(ordered or repos),
            ),
        ):
            yield

    return _install


@pytest.fixture
def builds():
    """Fake build_images recording (repo name, host id, image_names, options) per call."""
    calls = []

    async def _build_images(repo, parent, *, image_names=None, options=None):
        calls.append(
            (repo.name, parent.id, None if image_names is None else list(image_names), options)
        )
        names = [i.name for i in repo.docker_settings.images]
        wanted = names if image_names is None else [n for n in names if n in set(image_names)]
        return {n: ImageBuild(n, [f"{n}:latest"], "abc123abc123", _ok()) for n in wanted}

    with patch.object(verbs_mod, "build_images", AsyncMock(side_effect=_build_images)):
        yield calls


@pytest.mark.asyncio
async def test_two_selected_repos_declaring_one_image_name_are_refused(install, builds):
    repos = [_repo("a", images=("api",)), _repo("b", images=("api", "db"))]
    with install(repos), pytest.raises(DockerVerbError) as excinfo:
        await build_on("test3")
    message = str(excinfo.value)
    assert excinfo.value.field == "images"
    assert "'api'" in message
    assert "a" in message
    assert "b" in message
    assert "--repo" not in message, "the library names the parameter, the CLI spells the flag"
    assert builds == []


@pytest.mark.asyncio
async def test_narrowing_to_one_repo_lifts_the_refusal(install, builds):
    repos = [_repo("a", images=("api",)), _repo("b", images=("api",))]
    with install(repos):
        await build_on("test3", repo="a")
    assert [call[0] for call in builds] == ["a"]


@pytest.mark.asyncio
async def test_a_clash_outside_the_selected_images_is_not_this_builds_problem(install, builds):
    repos = [_repo("a", images=("api", "x")), _repo("b", images=("api", "y"))]
    with install(repos):
        await build_on("test3", images=["x", "y"])
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_two_images_of_one_repo_sharing_a_staging_key_are_refused(install, builds):
    repos = [_repo("a", images=("team/api", "team_api"))]
    with install(repos), pytest.raises(DockerVerbError) as excinfo:
        await build_on("test3")
    message = str(excinfo.value)
    assert excinfo.value.field == "images"
    assert "'team/api'" in message
    assert "'team_api'" in message
    assert "of repo 'a'" in message
    assert "staging directory key 'team_api'" in message
    assert "--repo" not in message
    assert builds == []


@pytest.mark.asyncio
async def test_narrowing_the_selection_lifts_a_staging_key_refusal(install, builds):
    repos = [_repo("a", images=("team/api", "team_api"))]
    with install(repos):
        await build_on("test3", images=["team/api"])
    assert [call[0] for call in builds] == ["a"]


@pytest.mark.asyncio
async def test_two_repos_may_declare_names_that_share_a_staging_key(install, builds):
    repos = [_repo("a", images=("team/api",)), _repo("b", images=("team_api",))]
    with install(repos):
        await build_on("test3")
    assert [call[0] for call in builds] == ["a", "b"]


@pytest.mark.asyncio
async def test_a_dry_run_names_the_exact_docker_build_per_image(install, builds, tmp_path):
    from otto.config.repo import DockerImage
    from otto.docker.build import build_command
    from tests.conftest import active_context

    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    image = DockerImage(name="api", dockerfile=df, context=tmp_path)
    repo = SimpleNamespace(
        name="a",
        docker_settings=SimpleNamespace(images=(image,), composes=("core",), use_cases=()),
    )
    with (
        install([repo]),
        active_context(dry_run=True),
        pytest.raises(CommandNotRunError) as excinfo,
    ):
        await build_on("test3", no_cache=True)

    text = str(excinfo.value)
    assert build_command("a", image, BuildOptions(no_cache=True)) in text
    assert "No context was staged and no image was built." in text
    assert builds == []


@pytest.mark.asyncio
async def test_builds_every_docker_repo_on_the_named_host_in_dependency_order(install, builds):
    a, b = _repo("a", images=("api",)), _repo("b", images=("db",))
    with install([a, b], ordered=[b, a]):
        report = await build_on("test3")
    assert isinstance(report, BuildReport)
    assert [(rb.repo, rb.host, rb.kind) for rb in report.repos] == [
        ("b", "test3", "built"),
        ("a", "test3", "built"),
    ]
    assert [c[:2] for c in builds] == [("b", "test3"), ("a", "test3")]
    assert report.ok
    assert report.displaced == []


@pytest.mark.asyncio
async def test_host_is_required(install):
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerVerbError, match=r"host is required.*\['alt2', 'test3'\]") as e,
    ):
        await build_on(None)  # type: ignore[arg-type]
    assert e.value.field == "host"


@pytest.mark.asyncio
async def test_a_host_outside_the_lab_or_not_docker_capable_is_refused(install, lab):
    lab.hosts["test3"].docker_capable = False
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerVerbError, match=r"'ghost' is not a docker-capable") as e,
    ):
        await build_on("ghost")
    assert e.value.field == "host"
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerVerbError, match=r"'test3' is not a docker-capable.*\['alt2'\]"),
    ):
        await build_on("test3")


@pytest.mark.asyncio
async def test_repo_narrows_and_an_unknown_repo_is_refused(install, builds):
    a, b = _repo("a", images=("api",)), _repo("b", images=("db",))
    with install([a, b]):
        report = await build_on("test3", repo="b")
    assert [rb.repo for rb in report.repos] == ["b"]
    with (
        install([a, b]),
        pytest.raises(DockerVerbError, match=r"repo 'c' is not a loaded repo.*\['a', 'b'\]") as e,
    ):
        await build_on("test3", repo="c")
    assert e.value.field == "repo"


@pytest.mark.asyncio
async def test_each_repo_builds_the_subset_of_images_it_declares(install, builds):
    a, b = _repo("a", images=("api", "worker")), _repo("b", images=("db",))
    with install([a, b]):
        report = await build_on("test3", images=["api", "db"])
    assert [(c[0], c[2]) for c in builds] == [("a", ["api"]), ("b", ["db"])]
    assert [sorted(rb.images) for rb in report.repos] == [["api"], ["db"]]


@pytest.mark.asyncio
async def test_a_repo_with_none_of_the_requested_images_is_left_out(install, builds):
    """b declares db, not api: narrowing to api alone must not add an empty b entry."""
    a, b = _repo("a", images=("api",)), _repo("b", images=("db",))
    with install([a, b]):
        report = await build_on("test3", images=["api"])
    assert [rb.repo for rb in report.repos] == ["a"]

    with (
        install([a, b]),
        patch.object(verbs_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as e,
    ):
        await build_on("test3", images=["api"])
    text = str(e.value)
    assert "a[api]" in text
    assert "b[db]" not in text
    assert "; b" not in text


@pytest.mark.asyncio
async def test_a_duplicated_image_name_builds_once(install, builds):
    with install([_repo("a", images=("api",))]):
        await build_on("test3", images=["api", "api"])
    assert builds[0][2] == ["api"]


@pytest.mark.asyncio
async def test_an_image_no_selected_repo_declares_is_refused(install):
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerVerbError, match=r"'apo'.*declared: \['api'\]") as e,
    ):
        await build_on("test3", images=["apo"])
    assert e.value.field == "images"


@pytest.mark.asyncio
async def test_a_repo_declaring_no_images_is_a_no_images_entry(install, builds):
    a, b = _repo("a", images=("api",)), _repo("b")
    with install([a, b]):
        report = await build_on("test3")
    assert [(rb.repo, rb.kind) for rb in report.repos] == [("a", "built"), ("b", "no_images")]
    assert [c[0] for c in builds] == ["a"]
    assert report.ok


@pytest.mark.asyncio
async def test_nothing_to_build_is_refused_before_any_host_is_touched(install, builds):
    with (
        install([_repo("a"), _repo("b")]),
        pytest.raises(DockerVerbError, match=r"nothing to build.*\['a', 'b'\]") as e,
    ):
        await build_on("test3")
    assert e.value.field is None
    assert builds == []


@pytest.mark.asyncio
async def test_a_failed_image_makes_the_report_not_ok(install):
    async def _build_images(repo, parent, *, image_names=None, options=None):
        return {"api": ImageBuild("api", [], None, _fail("syntax error"))}

    with (
        install([_repo("a", images=("api",))]),
        patch.object(verbs_mod, "build_images", AsyncMock(side_effect=_build_images)),
    ):
        report = await build_on("test3")
    assert not report.ok
    assert [(f.repo, f.image) for f in report.failed] == [("a", "api")]
    assert "syntax error" in report.failed[0].result.value


@pytest.mark.asyncio
async def test_dry_run_declines_with_the_whole_plan_and_builds_nothing(install, builds):
    a, b = _repo("a", images=("api", "worker")), _repo("b", images=("db",))
    with (
        install([a, b]),
        patch.object(verbs_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as e,
    ):
        await build_on("test3", images=["api", "db"])
    text = str(e.value)
    assert "test3 <- a[api]; b[db]" in text
    assert "No context was staged" in text
    assert builds == []


def _uc_repo(name, *fragments, images=()):
    return SimpleNamespace(
        name=name,
        docker_settings=SimpleNamespace(
            images=tuple(_image(n) for n in images), composes=("core",), use_cases=tuple(fragments)
        ),
    )


def _resolved(lab, placed, *, displaced=(), order=None):
    """Patch deployment.resolve_use_case to a fixed placement: {host_id: [SelectedFragment...]}."""
    frags = [sf for sfs in placed.values() for sf in sfs]
    selection = Selection("integration", frags, displaced=list(displaced))
    repos = {sf.repo.name: sf.repo for sf in frags}
    order_map = order or {name: i for i, name in enumerate(repos)}
    return patch.object(
        deploy_mod,
        "resolve_use_case",
        return_value=UseCaseResolution(lab, selection, placed, order_map),
    )


@pytest.mark.asyncio
async def test_compose_build_builds_the_winners_per_host_in_dependency_order(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    b = _uc_repo("b", _frag(), images=("db",))
    placed = {
        "test3": [SelectedFragment(a, a.docker_settings.use_cases[0])],
        "alt2": [SelectedFragment(b, b.docker_settings.use_cases[0])],
    }
    with _resolved(lab, placed):
        report = await compose_build("integration")
    assert [(rb.repo, rb.host) for rb in report.repos] == [
        ("b", "alt2"),
        ("a", "test3"),
    ]  # hosts sorted
    assert report.ok


@pytest.mark.asyncio
async def test_compose_build_shares_resolve_with_deploy(lab):
    """The placement call is deployment.resolve_use_case with the same on/provide."""
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    with (
        _resolved(lab, placed) as resolve,
        patch.object(
            verbs_mod,
            "build_images",
            AsyncMock(
                return_value={"api": ImageBuild("api", ["api:latest"], "abc123abc123", _ok())}
            ),
        ),
    ):
        await compose_build("integration", on="test3", provide={"db": "b"})
    resolve.assert_called_once_with("integration", on="test3", provide={"db": "b"})


@pytest.mark.asyncio
async def test_compose_build_carries_displacements_on_the_report(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    d = Displacement("db", "mock", 0, "real", 1)
    with _resolved(lab, placed, displaced=[d]):
        report = await compose_build("integration")
    assert report.displaced == [d]


@pytest.mark.asyncio
async def test_an_image_only_a_displaced_repo_declares_is_refused(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    with (
        _resolved(lab, placed),
        pytest.raises(DockerVerbError, match=r"'mockdb'.*declared: \['api'\]") as e,
    ):
        await compose_build("integration", images=["mockdb"])
    assert e.value.field == "images"
    assert builds == []


@pytest.mark.asyncio
async def test_a_winner_without_images_is_a_no_images_entry_and_the_other_host_builds(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    b = _uc_repo("b", _frag())
    placed = {
        "test3": [SelectedFragment(a, a.docker_settings.use_cases[0])],
        "alt2": [SelectedFragment(b, b.docker_settings.use_cases[0])],
    }
    with _resolved(lab, placed):
        report = await compose_build("integration")
    assert [(rb.repo, rb.host, rb.kind) for rb in report.repos] == [
        ("b", "alt2", "no_images"),
        ("a", "test3", "built"),
    ]
    assert report.ok


@pytest.mark.asyncio
async def test_compose_build_placement_refusal_is_the_engines(lab):
    with (
        patch.object(
            deploy_mod,
            "resolve_use_case",
            side_effect=UseCaseResolutionError("role 'edge' is ambiguous"),
        ),
        pytest.raises(UseCaseResolutionError, match="ambiguous"),
    ):
        await compose_build("integration")


@pytest.mark.asyncio
async def test_compose_build_dry_run_declines_with_every_host(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    b = _uc_repo("b", _frag(), images=("db",))
    placed = {
        "test3": [SelectedFragment(a, a.docker_settings.use_cases[0])],
        "alt2": [SelectedFragment(b, b.docker_settings.use_cases[0])],
    }
    with (
        _resolved(lab, placed),
        patch.object(verbs_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as e,
    ):
        await compose_build("integration")
    assert "alt2 <- b[db]; test3 <- a[api]" in str(e.value)
    assert builds == []


@pytest.mark.asyncio
async def test_compose_build_dry_run_names_the_displacements(lab, builds):
    """Spec §6: the dry run's plan carries the provider competition's losers."""
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    d = Displacement("db", "mock", 0, "real", 1)
    with (
        _resolved(lab, placed, displaced=[d]),
        patch.object(verbs_mod, "is_dry_run", return_value=True),
        pytest.raises(CommandNotRunError) as e,
    ):
        await compose_build("integration")
    assert "Displaced: db goes to real (priority 1); mock (priority 0) stands down." in str(e.value)
    assert builds == []


@pytest.mark.asyncio
async def test_build_on_hands_the_flags_to_every_build(install, builds):
    repos = [_repo("a", images=("api",))]
    with install(repos):
        await build_on(
            "test3",
            tags=["api:1.4"],
            no_cache=True,
            pull=True,
            build_args={"K": "v"},
            target="prod",
        )
    options = builds[0][3]
    assert options == BuildOptions(
        tags=["api:1.4"], no_cache=True, pull=True, build_args={"K": "v"}, target="prod"
    )


@pytest.mark.asyncio
async def test_a_tag_with_two_selected_images_is_refused_before_anything_runs(install, builds):
    repos = [_repo("a", images=("api", "db"))]
    with install(repos), pytest.raises(DockerVerbError) as excinfo:
        await build_on("test3", tags=["x:1"])
    assert excinfo.value.field == "tag"
    assert "api" in str(excinfo.value)
    assert "db" in str(excinfo.value)
    assert builds == []


@pytest.mark.asyncio
async def test_a_tag_with_one_image_named_is_fine(install, builds):
    repos = [_repo("a", images=("api", "db"))]
    with install(repos):
        await build_on("test3", images=["api"], tags=["x:1"])
    assert builds[0][2] == ["api"]


@pytest.mark.asyncio
async def test_compose_build_hands_the_flags_to_every_build(lab, builds):
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    with _resolved(lab, placed):
        await compose_build("integration", no_cache=True, pull=True, build_args={"K": "v"})
    assert builds[0][3] == BuildOptions(no_cache=True, pull=True, build_args={"K": "v"})


@pytest.mark.asyncio
async def test_a_missing_archive_context_is_refused_before_any_host_is_touched(install, builds):
    missing = SimpleNamespace(name="api", is_archive=True, context=Path("/nonexistent/ctx.tar"))
    repo = SimpleNamespace(
        name="a",
        docker_settings=SimpleNamespace(images=(missing,), composes=(), use_cases=()),
    )
    with install([repo]), pytest.raises(DockerVerbError, match=r"/nonexistent/ctx\.tar") as e:
        await build_on("test3")
    assert "is not a file" in str(e.value)
    assert e.value.field == "images"
    assert builds == [], "no build was started"
