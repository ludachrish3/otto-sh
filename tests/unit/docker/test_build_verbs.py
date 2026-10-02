"""build_on: the image-level verb. Every rule is the library's; the host is required."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from otto.docker import build_verbs as verbs_mod
from otto.docker import deployment as deploy_mod
from otto.docker.build_verbs import DockerBuildError, build_on, compose_build
from otto.docker.reports import BuildReport
from otto.docker.resolve import Displacement, SelectedFragment, Selection, UseCaseResolutionError
from otto.result import CommandNotRunError, CommandResult
from otto.utils import Status

from .test_deploy import _frag, _host, _lab


def _ok(value="") -> CommandResult:
    return CommandResult(Status.Success, value=value, command="docker build", retcode=0)


def _fail(value="boom") -> CommandResult:
    return CommandResult(Status.Failed, value=value, command="docker build", retcode=1)


def _image(name):
    return SimpleNamespace(name=name)


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

    def _install(repos, ordered=None):
        return patch.multiple(
            verbs_mod,
            get_lab=lambda: lab,
            get_repos=lambda: list(repos),
            get_ordered_repos=lambda: list(ordered or repos),
        )

    return _install


@pytest.fixture
def builds():
    """Fake build_images recording (repo name, host id, image_names, rebuild) per call."""
    calls = []

    async def _build_images(repo, parent, *, image_names=None, rebuild=False):
        calls.append(
            (repo.name, parent.id, None if image_names is None else list(image_names), rebuild)
        )
        names = [i.name for i in repo.docker_settings.images]
        wanted = names if image_names is None else [n for n in names if n in set(image_names)]
        return {n: _ok(f"{repo.name}-{n}:abc") for n in wanted}

    with patch.object(verbs_mod, "build_images", AsyncMock(side_effect=_build_images)):
        yield calls


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
        pytest.raises(DockerBuildError, match=r"host is required.*\['alt2', 'test3'\]") as e,
    ):
        await build_on(None)  # type: ignore[arg-type]
    assert e.value.field == "host"


@pytest.mark.asyncio
async def test_a_host_outside_the_lab_or_not_docker_capable_is_refused(install, lab):
    lab.hosts["test3"].docker_capable = False
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerBuildError, match=r"'ghost' is not a docker-capable") as e,
    ):
        await build_on("ghost")
    assert e.value.field == "host"
    with (
        install([_repo("a", images=("api",))]),
        pytest.raises(DockerBuildError, match=r"'test3' is not a docker-capable.*\['alt2'\]"),
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
        pytest.raises(DockerBuildError, match=r"repo 'c' is not a loaded repo.*\['a', 'b'\]") as e,
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
        pytest.raises(DockerBuildError, match=r"'apo'.*declared: \['api'\]") as e,
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
        pytest.raises(DockerBuildError, match=r"nothing to build.*\['a', 'b'\]") as e,
    ):
        await build_on("test3")
    assert e.value.field is None
    assert builds == []


@pytest.mark.asyncio
async def test_a_failed_image_makes_the_report_not_ok(install):
    async def _build_images(repo, parent, *, image_names=None, rebuild=False):
        return {"api": _fail("syntax error")}

    with (
        install([_repo("a", images=("api",))]),
        patch.object(verbs_mod, "build_images", AsyncMock(side_effect=_build_images)),
    ):
        report = await build_on("test3")
    assert not report.ok
    assert [(f.repo, f.image) for f in report.failed] == [("a", "api")]
    assert "syntax error" in report.failed[0].result.value


@pytest.mark.asyncio
async def test_rebuild_is_passed_through(install, builds):
    with install([_repo("a", images=("api",))]):
        await build_on("test3", rebuild=True)
    assert builds[0][3] is True


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
    """Patch deployment._resolve to a fixed placement: {host_id: [SelectedFragment...]}."""
    frags = [sf for sfs in placed.values() for sf in sfs]
    selection = Selection("integration", frags, displaced=list(displaced))
    repos = {sf.repo.name: sf.repo for sf in frags}
    order_map = order or {name: i for i, name in enumerate(repos)}
    return patch.object(deploy_mod, "_resolve", return_value=(lab, selection, placed, order_map))


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
    """The placement call is deployment._resolve with the same on/provide."""
    a = _uc_repo("a", _frag(), images=("api",))
    placed = {"test3": [SelectedFragment(a, a.docker_settings.use_cases[0])]}
    with (
        _resolved(lab, placed) as resolve,
        patch.object(verbs_mod, "build_images", AsyncMock(return_value={"api": _ok()})),
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
        pytest.raises(DockerBuildError, match=r"'mockdb'.*declared: \['api'\]") as e,
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
            deploy_mod, "_resolve", side_effect=UseCaseResolutionError("role 'edge' is ambiguous")
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
