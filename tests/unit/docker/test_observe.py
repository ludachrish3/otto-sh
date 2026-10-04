"""otto.docker.observe: otto resolves, docker prints."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from otto.config.repo import DockerUseCase
from otto.docker import observe as observe_mod
from otto.docker.observe import (
    CONTAINERS_PROBE,
    IMAGES_PROBE,
    DockerVerbError,
    LogsTarget,
    ObservedContainers,
    ObservedImages,
    capable_ids,
    compose_logs,
    compose_ps,
    container_logs,
    docker_parent,
    docker_parents,
    follow_logs,
    list_containers,
    list_images,
    logs_flags,
    observed_containers,
    observed_images,
    resolve_compose_logs,
    resolve_logs,
    run_on,
)
from otto.docker.resolve import SelectedFragment, UseCaseResolutionError
from otto.errors import FieldError
from otto.host.docker_host import DockerContainerHost
from otto.logger.mode import LogMode
from otto.result import CommandNotRunError, CommandResult, NotRunResult
from otto.utils import Status
from tests.conftest import active_context

from .test_build_verbs import _resolved
from .test_deploy import _compose_file, _host, _lab, _repo


def _plain(host_id: str, ip: str):
    host = _host(host_id, ip)
    host.docker_capable = False
    return host


@pytest.fixture
def lab():
    return _lab(
        _host("test3", "10.10.200.13"),
        _plain("test2", "10.10.200.12"),
        _host("alt2", "10.10.200.22"),
    )


def test_docker_verb_error_is_a_field_error():
    exc = DockerVerbError("bad", field="host")
    assert isinstance(exc, FieldError)
    assert isinstance(exc, ValueError)
    assert exc.field == "host"


def test_capable_ids_lists_the_docker_capable_unix_hosts_sorted(lab):
    assert capable_ids(lab) == ["alt2", "test3"]


def test_docker_parent_requires_a_host_and_names_the_capable_ones(lab):
    with pytest.raises(DockerVerbError, match=r"host is required.*\['alt2', 'test3'\]") as exc:
        docker_parent(lab, None)
    assert exc.value.field == "host"


def test_docker_parent_refuses_a_host_that_is_not_docker_capable(lab):
    with pytest.raises(DockerVerbError, match=r"host 'test2' is not a docker-capable") as exc:
        docker_parent(lab, "test2")
    assert exc.value.field == "host"


def test_docker_parents_is_every_capable_host_in_lab_order_or_the_one_named(lab):
    assert [h.id for h in docker_parents(lab, None)] == ["test3", "alt2"]
    assert [h.id for h in docker_parents(lab, "alt2")] == ["alt2"]
    with pytest.raises(DockerVerbError) as exc:
        docker_parents(lab, "test2")
    assert exc.value.field == "host"


def test_docker_parents_refuses_a_lab_with_no_docker_capable_host_rather_than_answering_silence():
    lab = _lab(_plain("test2", "10.10.200.12"))
    with pytest.raises(DockerVerbError, match=r"lab '.*' has no docker-capable unix host") as exc:
        docker_parents(lab, None)
    assert exc.value.field == "host"


def _ok(value: str, command: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=value, command=command, retcode=0)


def _fail(value: str, command: str = "") -> CommandResult:
    return CommandResult(Status.Failed, value=value, command=command, retcode=1)


def _record(host, *, fail: bool = False):
    """Make *host*'s exec record its commands and answer the command text back."""
    commands: list[str] = []

    async def _exec(cmd, *_a, **_kw):
        commands.append(cmd)
        return _fail(f"err from {host.id}", cmd) if fail else _ok(f"out of {host.id}\n", cmd)

    host.exec = AsyncMock(side_effect=_exec)
    host.commands = commands
    return host


@pytest.fixture
def two_hosts(lab):
    with patch("otto.config.fleet.get_lab", return_value=lab):
        yield _record(lab.hosts["test3"]), _record(lab.hosts["alt2"])


@pytest.mark.asyncio
async def test_run_on_keeps_dockers_text_whole_per_host_in_order(two_hosts):
    report = await run_on(list(two_hosts), "docker images", asked="list_images")
    assert [h.host_id for h in report.hosts] == ["test3", "alt2"]
    assert [h.command for h in report.hosts] == ["docker images", "docker images"]
    assert report.hosts[0].result.value == "out of test3\n"
    assert report.ok


@pytest.mark.asyncio
async def test_run_on_asks_the_daemon_quietly_so_the_console_does_not_print_it_twice(two_hosts):
    await run_on(list(two_hosts), "docker ps", asked="list_containers")
    for host in two_hosts:
        assert host.exec.await_args.kwargs["log"] is LogMode.QUIET


@pytest.mark.asyncio
async def test_run_on_goes_on_past_a_failing_host_and_reports_not_ok(two_hosts):
    t3, alt2 = two_hosts
    _record(t3, fail=True)
    report = await run_on([t3, alt2], "docker ps", asked="list_containers")
    assert [h.result.is_ok for h in report.hosts] == [False, True]
    assert report.hosts[0].result.value == "err from test3"
    assert not report.ok
    assert alt2.commands == ["docker ps"]


@pytest.mark.asyncio
async def test_run_on_reports_an_unreachable_host_in_ottos_words_and_goes_on(two_hosts):
    t3, alt2 = two_hosts
    # An empty-message error (asyncssh's disconnects often are) still names its kind.
    t3.exec = AsyncMock(side_effect=ConnectionError())
    report = await run_on([t3, alt2], "docker ps", asked="list_containers")
    assert [h.host_id for h in report.hosts] == ["test3", "alt2"]
    assert not report.hosts[0].result.is_ok
    assert report.hosts[0].result.value == "otto: host 'test3' unreachable: ConnectionError()"
    assert report.hosts[0].command == "docker ps"
    assert report.hosts[1].result.value == "out of alt2\n"
    assert not report.ok
    assert alt2.commands == ["docker ps"]


@pytest.mark.asyncio
async def test_run_on_refuses_a_dry_runs_declined_answer(lab):
    host = lab.hosts["test3"]
    # The decline's real shape: a NotRunResult whose `value` raises on read.
    declined = NotRunResult(Status.NotRun, command="docker ps")
    with pytest.raises(CommandNotRunError):
        _ = declined.value
    host.exec = AsyncMock(return_value=declined)
    with pytest.raises(CommandNotRunError, match="list_containers"):
        await run_on([host], "docker ps", asked="list_containers")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "kwargs", "command"),
    [
        (list_containers, {}, "docker ps"),
        (list_containers, {"all": True}, "docker ps -a"),
        (list_images, {}, "docker images"),
    ],
)
async def test_the_fan_out_verbs_run_dockers_own_command_on_every_capable_host(
    two_hosts, verb, kwargs, command
):
    report = await verb(**kwargs)
    assert [h.host_id for h in report.hosts] == ["test3", "alt2"]
    assert {h.command for h in report.hosts} == {command}
    # The device call itself, not only what the report says it was.
    assert all(host.commands == [command] for host in two_hosts)


@pytest.mark.asyncio
async def test_on_narrows_a_fan_out_to_one_host_and_a_bad_on_refuses_by_field(two_hosts):
    report = await list_images(on="alt2")
    assert [h.host_id for h in report.hosts] == ["alt2"]
    with pytest.raises(DockerVerbError) as exc:
        await list_containers(on="test2")
    assert exc.value.field == "host"


def _placed(lab, tmp_path, *host_ids, services=("api", "db"), per_host=None):
    """One fragment per host, each declaring *services* (or its *per_host* share)."""
    placed = {}
    for hid in host_ids:
        declared = per_host[hid] if per_host else services
        repo = _repo("r1", composes=(_compose_file(tmp_path, "core", services=declared),))
        frag = DockerUseCase(name="integration", composes=("core",))
        placed[hid] = [SelectedFragment(repo=repo, fragment=frag)]
    return placed


def test_logs_flags_spells_dockers_flags_in_dockers_order():
    assert logs_flags(tail=None, since=None, timestamps=False) == ""
    assert logs_flags(tail="50", since="10m", timestamps=True) == " --tail 50 --since 10m -t"


@pytest.mark.asyncio
async def test_compose_ps_runs_docker_compose_ps_with_the_project_on_every_acting_host(
    lab, tmp_path, two_hosts
):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3", "alt2")),
        patch.object(observe_mod, "use_case_project", return_value="unix-integration-x") as project,
    ):
        report = await compose_ps("integration", all=True)
    assert [h.host_id for h in report.hosts] == ["test3", "alt2"]
    assert {h.command for h in report.hosts} == {"docker compose -p unix-integration-x ps -a"}
    # Named exactly as teardown names it: the parent's source lab and the use-case.
    assert project.call_args_list[0].args == (lab.hosts["test3"].source_lab, "integration")


@pytest.mark.asyncio
async def test_compose_ps_without_all_asks_for_the_running_containers_only(
    lab, tmp_path, two_hosts
):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3")),
        patch.object(observe_mod, "use_case_project", return_value="p"),
    ):
        report = await compose_ps("integration")
    assert [h.command for h in report.hosts] == ["docker compose -p p ps"]


@pytest.mark.asyncio
async def test_compose_ps_hands_on_and_provide_to_the_shared_resolution(lab, tmp_path, two_hosts):
    with (
        _resolved(lab, _placed(lab, tmp_path, "alt2")) as resolve,
        patch.object(observe_mod, "use_case_project", return_value="p"),
    ):
        await compose_ps("integration", on="alt2", provide={"cap": "r1"})
    assert resolve.call_args == call("integration", on="alt2", provide={"cap": "r1"})


def test_resolve_compose_logs_names_the_services_and_carries_the_flags(lab, tmp_path, two_hosts):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3")),
        patch.object(observe_mod, "use_case_project", return_value="p") as project,
    ):
        targets = resolve_compose_logs("integration", ["api"], tail="5", timestamps=True)
    # The live form puts `-f` right after the verb, before docker's flags.
    assert targets == [
        LogsTarget(
            lab.hosts["test3"],
            "docker compose -p p logs --tail 5 -t api",
            "docker compose -p p logs -f --tail 5 -t api",
        )
    ]
    project.assert_called_once_with(lab.hosts["test3"].source_lab, "integration")


def test_resolve_compose_logs_never_mistakes_a_project_named_logs_for_the_verb(
    lab, tmp_path, two_hosts
):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3")),
        patch.object(observe_mod, "use_case_project", return_value="logs-integration-x"),
    ):
        (target,) = resolve_compose_logs("integration", ["api"])
    assert target.command == "docker compose -p logs-integration-x logs api"
    assert target.follow_command == "docker compose -p logs-integration-x logs -f api"


def test_resolve_compose_logs_names_each_hosts_own_share_of_the_requested_services(
    lab, tmp_path, two_hosts
):
    placed = _placed(lab, tmp_path, "test3", "alt2", per_host={"test3": ("api",), "alt2": ("db",)})
    with (
        _resolved(lab, placed),
        patch.object(observe_mod, "use_case_project", return_value="p"),
    ):
        targets = resolve_compose_logs("integration", ["api", "db"])
    assert [(t.parent.id, t.command) for t in targets] == [
        ("test3", "docker compose -p p logs api"),
        ("alt2", "docker compose -p p logs db"),
    ]


def test_resolve_compose_logs_refuses_an_undeclared_service_naming_the_declared_ones(
    lab, tmp_path, two_hosts
):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3")),
        pytest.raises(UseCaseResolutionError, match=r"\['web'\].*\['api', 'db'\]"),
    ):
        resolve_compose_logs("integration", ["web"])


@pytest.mark.asyncio
async def test_compose_logs_execs_each_target_and_keeps_dockers_text(lab, tmp_path, two_hosts):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3", "alt2")),
        patch.object(observe_mod, "use_case_project", return_value="p"),
    ):
        report = await compose_logs("integration", since="1h")
    assert [(h.host_id, h.command) for h in report.hosts] == [
        ("test3", "docker compose -p p logs --since 1h"),
        ("alt2", "docker compose -p p logs --since 1h"),
    ]
    assert report.hosts[1].result.value == "out of alt2\n"


@pytest.mark.asyncio
async def test_compose_verbs_ask_the_daemon_quietly(lab, tmp_path, two_hosts):
    with (
        _resolved(lab, _placed(lab, tmp_path, "test3")),
        patch.object(observe_mod, "use_case_project", return_value="p"),
    ):
        await compose_ps("integration")
        await compose_logs("integration")
    assert [c.kwargs["log"] for c in two_hosts[0].exec.await_args_list] == [
        LogMode.QUIET,
        LogMode.QUIET,
    ]


def _container(lab, parent_id="test3", project="integration", service="web", cid=""):
    parent = lab.hosts[parent_id]
    ch = DockerContainerHost(parent, cid, project, service, f"unix-{project}-x")
    lab.add_host(ch)
    return ch


@pytest.mark.asyncio
async def test_a_container_host_id_resolves_through_its_parent_with_the_compose_labels_and_minus_a(
    lab, two_hosts
):
    ch = _container(lab)
    parent = lab.hosts["test3"]
    parent.exec = AsyncMock(return_value=_ok("deadbeef1234\n", "docker ps"))
    target = await resolve_logs(ch.id, tail="3")
    assert target.parent is parent
    assert target.command == "docker logs --tail 3 deadbeef1234"
    assert target.follow_command == "docker logs -f --tail 3 deadbeef1234"
    asked = parent.exec.await_args.args[0]
    assert asked.startswith("docker ps -aq ")
    assert "label=com.docker.compose.project=unix-integration-x" in asked
    assert "label=com.docker.compose.service=web" in asked
    assert "label=com.docker.compose.oneoff=False" in asked
    assert parent.exec.await_args.kwargs["log"] is LogMode.QUIET


@pytest.mark.asyncio
async def test_a_container_host_id_with_no_container_on_the_parent_refuses_naming_both(
    lab, two_hosts
):
    ch = _container(lab)
    lab.hosts["test3"].exec = AsyncMock(return_value=_ok("", "docker ps"))
    with pytest.raises(DockerVerbError, match=rf"{ch.id}.*test3") as exc:
        await resolve_logs(ch.id)
    assert exc.value.field == "container"


@pytest.mark.asyncio
async def test_a_failed_probe_relays_dockers_error_not_a_fabricated_absence(lab, two_hosts):
    ch = _container(lab)
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_fail("Cannot connect to the Docker daemon", "docker ps")
    )
    with pytest.raises(DockerVerbError) as exc:
        await resolve_logs(ch.id)
    assert "Cannot connect to the Docker daemon" in str(exc.value)
    assert "never brought up" not in str(exc.value)
    assert exc.value.field == "container"


@pytest.mark.asyncio
async def test_resolve_logs_refuses_a_dry_runs_declined_probe(lab, two_hosts):
    ch = _container(lab)
    lab.hosts["test3"].exec = AsyncMock(
        return_value=NotRunResult(Status.NotRun, command="docker ps")
    )
    with pytest.raises(CommandNotRunError, match="resolve_logs"):
        await resolve_logs(ch.id)


@pytest.mark.asyncio
async def test_with_on_the_name_goes_to_docker_verbatim(lab, two_hosts):
    target = await resolve_logs("web-1", on="alt2", since="2h", timestamps=True)
    assert target.parent is lab.hosts["alt2"]
    assert target.command == "docker logs --since 2h -t web-1"
    assert target.follow_command == "docker logs -f --since 2h -t web-1"
    lab.hosts["alt2"].exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_neither_a_host_id_nor_on_refuses_listing_the_container_host_ids(lab, two_hosts):
    ch = _container(lab)
    with pytest.raises(DockerVerbError, match=rf"'web-1'.*{ch.id}") as exc:
        await resolve_logs("web-1")
    assert exc.value.field == "container"


@pytest.mark.asyncio
async def test_container_logs_runs_the_resolved_command_and_keeps_dockers_text(lab, two_hosts):
    report = await container_logs("web-1", on="alt2")
    assert [(h.host_id, h.command) for h in report.hosts] == [("alt2", "docker logs web-1")]
    assert report.hosts[0].result.value == "out of alt2\n"


@pytest.fixture
def bridge():
    with patch("otto.host.interact.run_ssh_login", AsyncMock(return_value=None)) as fake:
        yield fake


def _live(parent, tail=""):
    """A resolved ``docker logs`` target on *parent*, with its live form."""
    return LogsTarget(parent, f"docker logs{tail} abc", f"docker logs -f{tail} abc")


@pytest.mark.asyncio
async def test_follow_bridges_the_targets_live_form_on_the_parents_ssh_connection(
    lab, two_hosts, bridge
):
    parent = lab.hosts["test3"]
    parent.term = "ssh"
    conn = object()
    with patch.object(
        type(parent),
        "_live_connections",
        lambda self: SimpleNamespace(ssh=AsyncMock(return_value=conn)),
    ):
        await follow_logs([_live(parent, " --tail 5")])
    bridge.assert_awaited_once_with(
        conn=conn, host_name=parent.name, command="docker logs -f --tail 5 abc"
    )


@pytest.mark.asyncio
async def test_follow_refuses_a_target_with_no_live_form(lab, two_hosts, bridge):
    parent = lab.hosts["test3"]
    parent.term = "ssh"
    with pytest.raises(DockerVerbError, match="'docker ps' has no live form") as exc:
        await follow_logs([LogsTarget(parent, "docker ps")])
    assert exc.value.field == "follow"
    bridge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 1, None])
async def test_follow_returns_what_the_bridge_returned(lab, two_hosts, bridge, status):
    parent = lab.hosts["test3"]
    parent.term = "ssh"
    bridge.return_value = status
    with patch.object(
        type(parent),
        "_live_connections",
        lambda self: SimpleNamespace(ssh=AsyncMock(return_value=object())),
    ):
        assert await follow_logs([_live(parent)]) == status


@pytest.mark.asyncio
async def test_follow_refuses_a_telnet_parent_by_field_before_any_connection(
    lab, two_hosts, bridge
):
    parent = lab.hosts["test3"]
    parent.term = "telnet"
    with pytest.raises(
        DockerVerbError, match="needs an SSH parent; test3 is reached by telnet"
    ) as exc:
        await follow_logs([_live(parent)])
    assert exc.value.field == "follow"
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_refuses_more_than_one_host_naming_them(lab, two_hosts, bridge):
    t3, alt2 = lab.hosts["test3"], lab.hosts["alt2"]
    with pytest.raises(
        DockerVerbError, match=r"one terminal follows one host.*test3.*alt2.*`on`"
    ) as exc:
        await follow_logs([LogsTarget(t3, "x"), LogsTarget(alt2, "x")])
    assert exc.value.field == "follow"
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_refuses_no_target_without_printing_an_empty_host_list_as_a_placement(
    bridge,
):
    with pytest.raises(DockerVerbError, match="is placed on no host") as exc:
        await follow_logs([])
    assert exc.value.field == "follow"
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_declines_a_dry_run_at_the_top(lab, two_hosts, bridge):
    parent = lab.hosts["test3"]
    parent.term = "ssh"
    with (
        active_context(lab=lab, dry_run=True),
        pytest.raises(CommandNotRunError, match="follow_logs"),
    ):
        await follow_logs([_live(parent)])
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_follow_on_a_telnet_parent_is_refused_before_the_probe(lab, two_hosts):
    ch = _container(lab)
    parent = lab.hosts["test3"]
    parent.term = "telnet"
    parent.exec = AsyncMock()
    with pytest.raises(DockerVerbError, match="follow needs an SSH parent") as exc:
        await resolve_logs(ch.id, follow=True)
    assert exc.value.field == "follow"
    parent.exec.assert_not_awaited()
    with pytest.raises(DockerVerbError, match="follow needs an SSH parent"):
        await resolve_logs("web-1", on="test3", follow=True)


def test_the_probes_are_dockers_format_flags_verbatim():
    assert IMAGES_PROBE == r"docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}'"
    assert CONTAINERS_PROBE == r"docker ps -a --format '{{.Names}}\t{{.ID}}'"
    # a literal tab is eaten as completion by a PTY shell's line editor
    assert "\t" not in IMAGES_PROBE
    assert "\t" not in CONTAINERS_PROBE


@pytest.mark.asyncio
async def test_observed_images_parses_the_format_lines_and_drops_dangling_rows(lab, two_hosts):
    parent = lab.hosts["test3"]
    parent.exec = AsyncMock(
        return_value=_ok(
            "repo1-api:latest\tsha256:aa\n<none>:<none>\tsha256:bb\nalpine:3.19\tcc\n", IMAGES_PROBE
        )
    )
    seen = await observed_images("test3")
    assert seen == ObservedImages(
        refs=["repo1-api:latest", "alpine:3.19"], ids=["sha256:aa", "cc"], answered=True
    )
    assert parent.exec.await_args.args[0] == IMAGES_PROBE
    assert parent.exec.await_args.kwargs["log"] is LogMode.QUIET


@pytest.mark.asyncio
async def test_observed_containers_parses_the_format_lines(lab, two_hosts):
    parent = lab.hosts["test3"]
    parent.exec = AsyncMock(
        return_value=_ok("unix-x-api-1\t3f9a\nunix-x-db-1\t77ee\n", CONTAINERS_PROBE)
    )
    seen = await observed_containers("test3")
    assert seen == ObservedContainers(
        names=["unix-x-api-1", "unix-x-db-1"], ids=["3f9a", "77ee"], answered=True
    )
    assert parent.exec.await_args.args[0] == CONTAINERS_PROBE
    assert parent.exec.await_args.kwargs["log"] is LogMode.QUIET


@pytest.mark.asyncio
async def test_a_failed_probe_is_unanswered_not_an_error(lab, two_hosts):
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_fail("Cannot connect to the Docker daemon", IMAGES_PROBE)
    )
    assert await observed_images("test3") == ObservedImages([], [], answered=False)


@pytest.mark.asyncio
async def test_a_declined_probe_is_unanswered_not_an_error(lab, two_hosts):
    lab.hosts["test3"].exec = AsyncMock(
        return_value=NotRunResult(Status.NotRun, command=CONTAINERS_PROBE)
    )
    assert await observed_containers("test3") == ObservedContainers([], [], answered=False)


@pytest.mark.asyncio
async def test_an_empty_successful_probe_is_an_answer_of_nothing(lab, two_hosts):
    lab.hosts["test3"].exec = AsyncMock(return_value=_ok("", IMAGES_PROBE))
    assert await observed_images("test3") == ObservedImages([], [], answered=True)
    lab.hosts["test3"].exec = AsyncMock(return_value=_ok("", CONTAINERS_PROBE))
    assert await observed_containers("test3") == ObservedContainers([], [], answered=True)


@pytest.mark.asyncio
async def test_a_malformed_line_is_skipped_not_fatal(lab, two_hosts):
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_ok("good:1\tid1\nno-tab-here\n\n", IMAGES_PROBE)
    )
    assert await observed_images("test3") == ObservedImages(["good:1"], ["id1"], answered=True)


@pytest.mark.asyncio
async def test_an_answer_with_no_parsable_row_is_not_an_answer(lab, two_hosts):
    # A PTY expanding the tab to spaces, or a --format regression: the daemon said
    # something otto cannot read, which must not replace good hints with none.
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_ok("repo1-api:latest    sha256:aa\n", IMAGES_PROBE)
    )
    assert await observed_images("test3") == ObservedImages([], [], answered=False)
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_ok("unix-x-api-1    3f9a\n", CONTAINERS_PROBE)
    )
    assert await observed_containers("test3") == ObservedContainers([], [], answered=False)


@pytest.mark.asyncio
async def test_only_dangling_rows_are_still_an_answer(lab, two_hosts):
    lab.hosts["test3"].exec = AsyncMock(
        return_value=_ok("<none>:<none>\tsha256:bb\n", IMAGES_PROBE)
    )
    assert await observed_images("test3") == ObservedImages([], [], answered=True)


@pytest.mark.asyncio
async def test_a_probe_on_a_non_docker_host_refuses_by_field(lab, two_hosts):
    with pytest.raises(DockerVerbError) as exc:
        await observed_images("nowhere")
    assert exc.value.field == "host"
