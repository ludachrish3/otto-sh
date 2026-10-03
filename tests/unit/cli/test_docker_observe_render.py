"""The observe verbs print docker's text; otto adds a header per host and nothing else."""

from unittest.mock import AsyncMock, patch

import pytest
import typer

from otto.cli import docker as docker_cli
from otto.docker.observe import HostOutput, LogsTarget, ObserveReport
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner


def _out(host: str, text: str, *, ok: bool = True, command: str = "docker ps") -> HostOutput:
    status = Status.Success if ok else Status.Failed
    return HostOutput(
        host, command, CommandResult(status, value=text, command=command, retcode=0 if ok else 1)
    )


_PS = "CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS   NAMES\n"


def test_fan_out_prints_a_header_per_host_and_dockers_text_untouched(capsys):
    docker_cli._render_observe(
        ObserveReport([_out("test3", _PS + "abc  img\n"), _out("alt2", _PS)]), header=True
    )
    assert capsys.readouterr().out == f"== test3 ==\n{_PS}abc  img\n\n== alt2 ==\n{_PS}"


def test_logs_prints_dockers_text_alone(capsys):
    docker_cli._render_observe(
        ObserveReport([_out("test3", "line 1\nline 2\n", command="docker logs x")]), header=False
    )
    assert capsys.readouterr().out == "line 1\nline 2\n"


def test_a_failing_host_prints_dockers_error_and_the_verb_exits_1(capsys):
    report = ObserveReport(
        [_out("test3", "Cannot connect to the Docker daemon\n", ok=False), _out("alt2", _PS)]
    )
    with pytest.raises(typer.Exit) as exc:
        docker_cli._render_observe(report, header=True)
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "== test3 ==\nCannot connect to the Docker daemon\n" in out
    assert "== alt2 ==\n" in out


def test_a_failed_logs_exits_with_dockers_own_code_and_a_fan_out_still_exits_1(capsys):
    failed = HostOutput(
        "test3",
        "docker logs x",
        CommandResult(
            Status.Failed, value="Error: No such container: x\n", command="x", retcode=125
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        docker_cli._render_observe(ObserveReport([failed]), header=False)
    assert exc.value.exit_code == 125
    assert capsys.readouterr().out == "Error: No such container: x\n"
    with pytest.raises(typer.Exit) as fan:
        docker_cli._render_observe(ObserveReport([failed]), header=True)
    assert fan.value.exit_code == 1


def test_a_host_with_no_output_prints_its_header_and_nothing_else(capsys):
    docker_cli._render_observe(ObserveReport([_out("test3", "")]), header=True)
    assert capsys.readouterr().out == "== test3 ==\n"


def test_a_header_is_printed_even_for_one_host(capsys):
    docker_cli._render_observe(ObserveReport([_out("test3", _PS)]), header=True)
    assert capsys.readouterr().out.startswith("== test3 ==\n")


@pytest.mark.parametrize(
    ("argv", "seam", "args", "kwargs"),
    [
        (["ps"], "otto.docker.observe.list_containers", (), {"on": None, "all": False}),
        (
            ["ps", "-a", "--on", "test3"],
            "otto.docker.observe.list_containers",
            (),
            {"on": "test3", "all": True},
        ),
        (["ps", "--all"], "otto.docker.observe.list_containers", (), {"on": None, "all": True}),
        (["images"], "otto.docker.observe.list_images", (), {"on": None}),
        (["images", "--on", "test3"], "otto.docker.observe.list_images", (), {"on": "test3"}),
        (
            ["compose", "ps", "integration"],
            "otto.docker.observe.compose_ps",
            ("integration",),
            {"all": False, "on": None, "provide": {}},
        ),
        (
            ["compose", "ps", "integration", "-a", "--on", "test3"],
            "otto.docker.observe.compose_ps",
            ("integration",),
            {"all": True, "on": "test3", "provide": {}},
        ),
        (
            ["compose", "logs", "integration"],
            "otto.docker.observe.compose_logs",
            ("integration", []),
            {"on": None, "provide": {}, "tail": None, "since": None, "timestamps": False},
        ),
        (
            ["compose", "logs", "integration", "api", "db", "--tail", "20", "--since", "5m", "-t"],
            "otto.docker.observe.compose_logs",
            ("integration", ["api", "db"]),
            {"on": None, "provide": {}, "tail": "20", "since": "5m", "timestamps": True},
        ),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
def test_the_observe_verbs_hand_the_library_their_flags_and_print_its_report(
    argv, seam, args, kwargs
):
    fake = AsyncMock(return_value=ObserveReport([_out("test3", _PS)]))
    with patch(seam, fake):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 0, result.output
    assert fake.await_args.args == args
    assert fake.await_args.kwargs == kwargs
    assert "== test3 ==" in result.output
    assert "CONTAINER ID" in result.output


@pytest.mark.parametrize(
    ("argv", "args", "kwargs"),
    [
        (
            ["logs", "test3.integration.web"],
            ("test3.integration.web",),
            {"on": None, "tail": None, "since": None, "timestamps": False},
        ),
        (
            ["logs", "web-1", "--on", "test3", "--tail", "9", "-t"],
            ("web-1",),
            {"on": "test3", "tail": "9", "since": None, "timestamps": True},
        ),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
def test_logs_hands_the_library_its_flags_and_prints_dockers_lines_with_no_host_header(
    argv, args, kwargs
):
    fake = AsyncMock(return_value=ObserveReport([_out("test3", _PS, command="docker logs x")]))
    with patch("otto.docker.observe.container_logs", fake):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == 0, result.output
    assert fake.await_args.args == args
    assert fake.await_args.kwargs == kwargs
    assert "==" not in result.output
    assert "CONTAINER ID" in result.output


def test_logs_follow_resolves_then_follows_and_never_prints_a_one_shot_report():
    target = LogsTarget(object(), "docker logs x")
    resolve = AsyncMock(return_value=target)
    follow = AsyncMock()
    once = AsyncMock()
    with (
        patch("otto.docker.observe.resolve_logs", resolve),
        patch("otto.docker.observe.follow_logs", follow),
        patch("otto.docker.observe.container_logs", once),
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app,
            ["logs", "x", "--on", "test3", "--tail", "9", "-f"],
            spec_name="docker",
        )
    assert result.exit_code == 0, result.output
    assert resolve.await_args.args == ("x",)
    assert resolve.await_args.kwargs == {
        "on": "test3",
        "tail": "9",
        "since": None,
        "timestamps": False,
        "follow": True,
    }
    follow.assert_awaited_once_with([target])
    once.assert_not_awaited()


def test_compose_logs_follow_resolves_then_follows_and_never_prints_a_one_shot_report():
    targets = [LogsTarget(object(), "docker compose -p p logs")]
    follow = AsyncMock()
    once = AsyncMock()
    with (
        patch("otto.docker.observe.resolve_compose_logs", return_value=targets) as resolve,
        patch("otto.docker.observe.follow_logs", follow),
        patch("otto.docker.observe.compose_logs", once),
    ):
        result = DispatchRunner().invoke(
            docker_cli.docker_app,
            ["compose", "logs", "integration", "api", "--on", "test3", "--tail", "5", "--follow"],
            spec_name="docker",
        )
    assert result.exit_code == 0, result.output
    assert resolve.call_args.args == ("integration", ["api"])
    assert resolve.call_args.kwargs == {
        "on": "test3",
        "provide": {},
        "tail": "5",
        "since": None,
        "timestamps": False,
    }
    follow.assert_awaited_once_with(targets)
    once.assert_not_awaited()


@pytest.mark.parametrize(
    ("argv", "seam"),
    [
        (["logs", "x", "--on", "test3", "-f"], "otto.docker.observe.resolve_logs"),
        (["compose", "logs", "integration", "-f"], "otto.docker.observe.resolve_compose_logs"),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
@pytest.mark.parametrize(("status", "exit_code"), [(1, 1), (125, 125), (0, 0), (None, 0)], ids=str)
def test_a_follow_exits_with_dockers_status_and_none_is_a_normal_return(
    argv, seam, status, exit_code
):
    target = LogsTarget(object(), "docker logs x")
    resolved = AsyncMock(return_value=target) if seam.endswith("resolve_logs") else None
    follow = AsyncMock(return_value=status)
    with (
        patch(seam, resolved) if resolved else patch(seam, return_value=[target]),
        patch("otto.docker.observe.follow_logs", follow),
    ):
        result = DispatchRunner().invoke(docker_cli.docker_app, argv, spec_name="docker")
    assert result.exit_code == exit_code, result.output
    follow.assert_awaited_once()
