"""Each docker verb records what the daemon answered, after its own work, best-effort."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.cli import docker as docker_cli
from otto.docker.observe import (
    DockerVerbError,
    HostOutput,
    ObservedContainers,
    ObservedImages,
    ObserveReport,
)
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner


@pytest.fixture
def recorders():
    """The two cache writers, patched where `_record_observed` looks them up."""
    with (
        patch("otto.config.completion_cache.record_docker_images") as images,
        patch("otto.config.completion_cache.record_docker_containers") as containers,
        patch("otto.config.bootstrapped.get_repos", return_value=["repo"]),
    ):
        yield images, containers


@pytest.fixture
def probes():
    with (
        patch(
            "otto.docker.observe.observed_images",
            AsyncMock(return_value=ObservedImages(["a:1"], ["i1"], answered=True)),
        ) as images,
        patch(
            "otto.docker.observe.observed_containers",
            AsyncMock(return_value=ObservedContainers(["c1"], ["x1"], answered=True)),
        ) as containers,
    ):
        yield images, containers


@pytest.mark.asyncio
async def test_the_helper_records_each_kind_asked_for_per_host(recorders, probes):
    images, containers = recorders
    await docker_cli._record_observed(["test3", "alt2"], images=True, containers=True)
    assert [c.args[1] for c in images.call_args_list] == ["test3", "alt2"]
    assert images.call_args_list[0].kwargs == {"refs": ["a:1"], "ids": ["i1"]}
    assert [c.args[1] for c in containers.call_args_list] == ["test3", "alt2"]
    assert containers.call_args_list[0].kwargs == {"names": ["c1"], "ids": ["x1"]}


@pytest.mark.asyncio
async def test_an_unanswered_probe_records_nothing(recorders, probes):
    images, containers = recorders
    probes[0].return_value = ObservedImages([], [], answered=False)
    probes[1].return_value = ObservedContainers([], [], answered=False)
    await docker_cli._record_observed(["test3"], images=True, containers=True)
    images.assert_not_called()
    containers.assert_not_called()


@pytest.mark.asyncio
async def test_an_answered_empty_probe_records_the_empty_lists(recorders, probes):
    images, containers = recorders
    probes[0].return_value = ObservedImages([], [], answered=True)
    probes[1].return_value = ObservedContainers([], [], answered=True)
    await docker_cli._record_observed(["test3"], images=True, containers=True)
    assert images.call_args.kwargs == {"refs": [], "ids": []}
    assert containers.call_args.kwargs == {"names": [], "ids": []}


@pytest.mark.asyncio
async def test_a_dry_run_records_nothing_and_asks_nothing(recorders, probes):
    with patch("otto.host.host.is_dry_run", return_value=True):
        await docker_cli._record_observed(["test3"], images=True, containers=True)
    probes[0].assert_not_awaited()
    probes[1].assert_not_awaited()
    recorders[0].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("gone"),
        OSError("gone"),
        DockerVerbError("not a docker host", field="parent"),
    ],
    ids=["connection", "os", "docker-verb"],
)
async def test_a_probe_that_raises_is_swallowed(recorders, probes, error):
    probes[1].side_effect = error
    await docker_cli._record_observed(["test3"], containers=True)  # no raise
    recorders[1].assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_host_does_not_stop_the_next_host_recording(recorders, probes):
    images, _ = recorders
    probes[0].side_effect = [OSError("gone"), ObservedImages(["a:1"], ["i1"], answered=True)]
    await docker_cli._record_observed(["test3", "alt2"], images=True)
    assert [c.args[1] for c in images.call_args_list] == ["alt2"]


@pytest.mark.asyncio
async def test_a_cache_write_error_is_swallowed(recorders, probes):
    recorders[0].side_effect = OSError("read-only cache")
    await docker_cli._record_observed(["test3"], images=True)  # no raise


def _observe_report(host_ids):
    return ObserveReport(
        hosts=[
            HostOutput(
                host_id=h,
                command="docker x",
                result=CommandResult(Status.Success, value="ok\n", command="docker x", retcode=0),
            )
            for h in host_ids
        ]
    )


@pytest.mark.parametrize(
    ("argv", "seam", "value", "kind", "hosts"),
    [
        (
            ["ps"],
            "otto.docker.observe.list_containers",
            _observe_report(["test3", "alt2"]),
            "containers",
            ["test3", "alt2"],
        ),
        (
            ["images"],
            "otto.docker.observe.list_images",
            _observe_report(["test3"]),
            "images",
            ["test3"],
        ),
        (
            ["compose", "ps", "integration"],
            "otto.docker.observe.compose_ps",
            _observe_report(["alt2"]),
            "containers",
            ["alt2"],
        ),
    ],
    ids=["ps", "images", "compose-ps"],
)
def test_the_observe_leaves_record_for_the_reports_hosts(argv, seam, value, kind, hosts):
    with (
        patch(seam, AsyncMock(return_value=value)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    record.assert_awaited_once_with(hosts, **{kind: True})


def test_a_probe_that_raises_leaves_the_verbs_output_and_exit_alone():
    with (
        patch(
            "otto.docker.observe.list_containers",
            AsyncMock(return_value=_observe_report(["test3"])),
        ),
        patch(
            "otto.docker.observe.observed_containers", AsyncMock(side_effect=OSError("gone"))
        ) as observed,
        patch("otto.config.bootstrapped.get_repos", return_value=["repo"]),
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, ["ps"], spec_name="docker")
    observed.assert_awaited_once_with("test3")
    assert result.exit_code == 0, result.output
    assert "== test3 ==\nok\n" in result.output


def _report_with_a_failed_host(ok_host, failed_host):
    report = _observe_report([ok_host])
    report.hosts.append(
        HostOutput(
            host_id=failed_host,
            command="docker x",
            result=CommandResult(
                Status.Failed, value="unreachable", command="docker x", retcode=255
            ),
        )
    )
    return report


_OBSERVE_LEAVES = [
    (["ps"], "otto.docker.observe.list_containers", "containers"),
    (["images"], "otto.docker.observe.list_images", "images"),
    (["compose", "ps", "integration"], "otto.docker.observe.compose_ps", "containers"),
]
_OBSERVE_IDS = ["ps", "images", "compose-ps"]


@pytest.mark.parametrize(("argv", "seam", "kind"), _OBSERVE_LEAVES, ids=_OBSERVE_IDS)
def test_a_host_the_verb_could_not_ask_is_not_asked_again(argv, seam, kind):
    # The verb already paid that host's connect timeout once; the probe must not
    # pay it a second time. The failure still exits 1, with docker's lines shown.
    report = _report_with_a_failed_host("test3", "alt2")
    with (
        patch(seam, AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 1, result.output
    assert "== test3 ==\nok\n" in result.output
    record.assert_awaited_once_with(["test3"], **{kind: True})


@pytest.mark.parametrize(("argv", "seam", "kind"), _OBSERVE_LEAVES, ids=_OBSERVE_IDS)
def test_the_observe_leaves_render_before_they_probe(argv, seam, kind):
    order: list[str] = []
    with (
        patch(seam, AsyncMock(return_value=_observe_report(["test3"]))),
        patch.object(docker_cli, "_render_observe", lambda *_a, **_k: order.append("render")),
        patch.object(
            docker_cli,
            "_record_observed",
            AsyncMock(side_effect=lambda *_a, **_k: order.append("record")),
        ) as record,
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 0, result.output
    assert order == ["render", "record"]
    record.assert_awaited_once_with(["test3"], **{kind: True})


@pytest.mark.parametrize(("argv", "seam", "kind"), _OBSERVE_LEAVES, ids=_OBSERVE_IDS)
def test_a_failed_render_still_records_and_still_exits_1(argv, seam, kind):
    import typer

    def _render(*_a, **_k):
        raise typer.Exit(1)

    with (
        patch(seam, AsyncMock(return_value=_observe_report(["test3"]))),
        patch.object(docker_cli, "_render_observe", _render),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 1, result.output
    record.assert_awaited_once_with(["test3"], **{kind: True})


def test_compose_down_does_not_probe_a_host_whose_teardown_failed():
    from otto.docker.reports import TeardownReport

    ok = CommandResult(Status.Success, value="", command="docker compose down", retcode=0)
    bad = CommandResult(Status.Failed, value="gone", command="docker compose down", retcode=255)
    report = TeardownReport(hosts={"test3": [ok], "alt2": [bad]}, use_case="integration")
    with (
        patch("otto.docker.deployment.teardown", AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app, ["compose", "down", "integration"], spec_name="docker"
        )
    assert result.exit_code == 1, result.output
    record.assert_awaited_once_with(["test3"], containers=True)


def _two_host_build_report():
    from otto.docker.reports import BuildReport, ImageBuild, RepoBuild

    ok = CommandResult(Status.Success, value="", command="docker build", retcode=0)
    bad = CommandResult(Status.Failed, value="boom", command="docker build", retcode=1)
    return BuildReport(
        repos=[
            RepoBuild(
                repo="one",
                host="test3",
                kind="built",
                images={"api": ImageBuild("api", ["api:latest"], "sha", ok)},
            ),
            RepoBuild(
                repo="two",
                host="alt2",
                kind="built",
                images={"db": ImageBuild("db", [], None, bad)},
            ),
            RepoBuild(
                repo="three",
                host="test3",
                kind="built",
                images={"web": ImageBuild("web", ["web:latest"], "sha2", ok)},
            ),
        ]
    )


def test_build_records_images_for_the_hosts_the_report_names_even_when_one_failed():
    with (
        patch("otto.docker.build_verbs.build_on", AsyncMock(return_value=_two_host_build_report())),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app, ["build", "--parent", "test3"], spec_name="docker"
        )
    assert result.exit_code == 1  # the failed image still exits 1
    record.assert_awaited_once_with(["test3", "alt2"], images=True)  # in order, each host once


def test_compose_build_records_images_for_the_hosts_the_report_names():
    with (
        patch(
            "otto.docker.build_verbs.compose_build",
            AsyncMock(return_value=_two_host_build_report()),
        ),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app, ["compose", "build", "integration"], spec_name="docker"
        )
    assert result.exit_code == 1
    record.assert_awaited_once_with(["test3", "alt2"], images=True)


def test_compose_up_records_containers_for_the_stacks_hosts():
    from otto.docker.deployment import UseCaseStack

    stack = UseCaseStack(
        use_case="integration", selection=MagicMock(), by_host={"test3": {}, "alt2": {}}
    )
    with (
        patch("otto.docker.deployment.deploy", AsyncMock(return_value=stack)),
        patch.object(docker_cli, "_print_stack_report", lambda *_a, **_k: None),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app, ["compose", "up", "integration"], spec_name="docker"
        )
    assert result.exit_code == 0, result.output
    record.assert_awaited_once_with(["test3", "alt2"], containers=True)


def test_compose_down_records_containers_for_the_teardown_reports_hosts():
    from otto.docker.reports import TeardownReport

    report = TeardownReport(hosts={"test3": []}, use_case="integration")
    with (
        patch("otto.docker.deployment.teardown", AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app, ["compose", "down", "integration"], spec_name="docker"
        )
    assert result.exit_code == 0, result.output
    record.assert_awaited_once_with(["test3"], containers=True)


def test_a_declined_verb_records_nothing():
    from otto.result import CommandNotRunError

    with (
        patch(
            "otto.docker.observe.list_images",
            AsyncMock(side_effect=CommandNotRunError("list_images", "test3", "docker images")),
        ),
        patch("otto.host.host.is_dry_run", return_value=True),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, ["images"], spec_name="docker")
    assert result.exit_code == 0, result.output
    record.assert_not_awaited()
