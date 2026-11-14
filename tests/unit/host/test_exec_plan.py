"""One exec plan per call: each family states its route, and one method dispatches it.

``BaseHost._exec_one`` plans (``_plan_exec``) and runs the plan
(``_run_plan``); no family decides a route anywhere else. These tests pin each
family's answer, that no family re-decides inline, and that the transfer line
budget reads the same plan the dispatch runs.
"""

import ast
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import DockerContainerHost, EmbeddedHost, LocalHost, UnixHost
from otto.host.element import Element
from otto.host.exec_route import ExecRoute
from otto.host.login_proxy import Cred
from otto.host.session import ExecPlan, typed_line_budget
from otto.logger.mode import LogMode
from otto.result import CommandResult, NotRunResult
from otto.utils import Status
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.conftest import active_context
from tests.unit.host.test_docker_host import _make_container
from tests.unit.host.test_embedded_host import _embedded_console
from tests.unit.host.test_unix_host import _unix_host

_SRC = CHECKOUT_ROOT / "src" / "otto" / "host"


def _unix_with_users(*, term: str = "ssh") -> UnixHost:
    """``admin`` logs in; ``root`` has a direct cred; ``app`` is reachable only via ``su``."""
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[
            Cred(login="admin", password="secret"),
            Cred(login="root", password="rootpw"),
            Cred(login="app", proxy="su", via="admin"),
        ],
        log=LogMode.QUIET,
        term=term,
    )


def _unix_console() -> UnixHost:
    from otto.host.options import ConsoleOptions

    return UnixHost(
        ip="10.0.0.2",
        element=Element(name="test2"),
        creds=[Cred(login="test", password="Password1")],
        log=LogMode.QUIET,
        valid_terms=["console"],
        term="console",
        console_options=ConsoleOptions(server="test1", port=4001),
    )


def _embedded_telnet():
    from otto.host import ZephyrHost

    return ZephyrHost(ip="192.0.2.1", element=Element(name="zephyr37_fat"), log=LogMode.QUIET)


@pytest.mark.parametrize(
    ("build", "user", "needs_shell", "expected"),
    [
        pytest.param(
            lambda: _unix_host(term="ssh"),
            None,
            False,
            ExecPlan(route=ExecRoute.SSH_CHANNEL),
            id="unix-ssh",
        ),
        pytest.param(
            lambda: _unix_host(term="ssh"),
            None,
            True,
            ExecPlan(route=ExecRoute.POOLED_SHELL),
            id="unix-ssh-needs-shell",
        ),
        pytest.param(
            _unix_with_users,
            "root",
            False,
            ExecPlan(route=ExecRoute.SSH_CHANNEL, as_user="root"),
            id="unix-ssh-direct-cred-user",
        ),
        pytest.param(
            _unix_with_users,
            "root",
            True,
            ExecPlan(route=ExecRoute.POOLED_SHELL),
            id="unix-ssh-direct-cred-user-needs-shell",
        ),
        pytest.param(
            _unix_with_users,
            "app",
            False,
            ExecPlan(route=ExecRoute.POOLED_SHELL),
            id="unix-ssh-user-without-direct-cred",
        ),
        pytest.param(
            lambda: _unix_host(term="telnet"),
            None,
            False,
            ExecPlan(route=ExecRoute.POOLED_SHELL),
            id="unix-telnet",
        ),
        pytest.param(
            lambda: _unix_with_users(term="telnet"),
            "root",
            False,
            ExecPlan(route=ExecRoute.POOLED_SHELL),
            id="unix-telnet-user",
        ),
        pytest.param(
            _unix_console,
            None,
            False,
            ExecPlan(route=ExecRoute.DEFAULT_SESSION),
            id="unix-console",
        ),
        pytest.param(
            _embedded_console,
            None,
            False,
            ExecPlan(route=ExecRoute.DEFAULT_SESSION),
            id="embedded-console",
        ),
        pytest.param(
            _embedded_telnet,
            None,
            False,
            ExecPlan(route=ExecRoute.DEFAULT_SESSION),
            id="embedded-telnet",
        ),
        pytest.param(
            LocalHost, None, False, ExecPlan(route=ExecRoute.LOCAL_SUBPROCESS), id="local"
        ),
        pytest.param(
            _make_container, None, False, ExecPlan(route=ExecRoute.CONTAINER), id="container"
        ),
        pytest.param(
            _make_container, "root", True, ExecPlan(route=ExecRoute.CONTAINER), id="container-user"
        ),
    ],
)
def test_each_family_plans_its_route(build, user, needs_shell, expected):
    assert build()._plan_exec(user=user, needs_shell=needs_shell) == expected


def test_no_product_family_overrides_exec_one():
    for cls in [UnixHost, EmbeddedHost, LocalHost, DockerContainerHost]:
        assert "_exec_one" not in vars(cls), cls.__name__


_PRIMITIVES = {"exec_on", "exec_on_route", "_exec_subprocess", "_exec_via_parent"}
_ALLOWED_CALLERS = {"_run_plan", "_exec_on_console"}


def test_only_run_plan_calls_an_exec_primitive():
    offenders: list[str] = []
    allowed_hits: dict[str, int] = {}
    for name in ["unix_host.py", "embedded_host.py", "local_host.py", "docker_host.py"]:
        tree = ast.parse((_SRC / name).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if fn.name in _ALLOWED_CALLERS:
                allowed_hits[name] = allowed_hits.get(name, 0) + sum(
                    1
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _PRIMITIVES
                )
                continue
            offenders.extend(
                f"{name}:{node.lineno} {fn.name} -> {node.func.attr}"
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _PRIMITIVES
            )
    assert offenders == []
    # Positive control: a renamed primitive or a wrong path would leave the scan
    # empty and the test green. The embedded family dispatches through the
    # shared default session rather than a primitive, so it is scanned for
    # offenders but is not expected to hit.
    for name in ["unix_host.py", "local_host.py", "docker_host.py"]:
        assert allowed_hits.get(name, 0) > 0, f"{name}: no primitive call in {_ALLOWED_CALLERS}"


def test_a_per_user_transfer_budget_reads_that_users_plan(monkeypatch):
    """A backend built for ``app`` sizes its lines to ``app``'s route, the pooled shell.

    The ambient-user half of "budget and dispatch read one plan" is
    ``test_inside_as_user_the_transfer_budget_reads_the_plan_exec_dispatches``.
    """
    h = _unix_with_users(term="ssh")
    captured = {}

    def _capture(*, name, ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("otto.host.unix_host.build_transfer_backend", _capture)
    h._build_file_transfer(user="app")

    assert h._plan_exec(user="app", needs_shell=False).route is ExecRoute.POOLED_SHELL
    budget = captured["ctx"].exec_line_budget()
    assert budget is not None
    assert budget == h._session_mgr.line_budget(route=ExecRoute.POOLED_SHELL)
    assert budget == typed_line_budget(h._session_mgr._command_frame, echoes=True)
    # The host's own plain exec still rides the bare channel, unbounded.
    plain = h._exec_plan(user=None, needs_shell=False).route
    assert h._session_mgr.line_budget(route=plain) is None


@pytest.mark.asyncio
async def test_exec_stamps_the_planned_route():
    assert (await LocalHost().exec("true")).route == "local-subprocess"


@pytest.mark.asyncio
async def test_run_stamps_the_default_session():
    host = LocalHost()
    try:
        result = await host.run("true")
    finally:
        await host.close()
    assert result.only.route == "default-session"


@pytest.mark.asyncio
async def test_a_container_reports_its_parents_route():
    parent = MagicMock()
    parent.id = parent.name = "test3"
    parent.term = "ssh"
    parent.exec = AsyncMock(
        return_value=CommandResult(
            status=Status.Success, value="", command="x", retcode=0, route="pooled-shell"
        )
    )
    container = _make_container(parent)
    container._docker_exec = AsyncMock(return_value="docker exec x true")
    assert (await container.exec("true")).route == "container/pooled-shell"


@pytest.mark.parametrize("build", [LocalHost, _make_container], ids=["local", "container"])
@pytest.mark.asyncio
async def test_a_not_run_result_is_returned_unstamped(build):
    host = build()
    with active_context(dry_run=True):
        result = await host.exec("true")
    assert isinstance(result, NotRunResult)
    assert result.route == ""


@pytest.mark.asyncio
async def test_a_run_plan_that_dispatches_another_primitive_is_caught(monkeypatch):
    """The route is the primitive's record, so a dispatch that disagrees with its plan raises.

    The plan says ``ssh-channel``; ``LocalHost._run_plan`` runs a subprocess
    whatever the plan says. Copying the plan's route onto the result would
    report the channel that never opened.
    """
    monkeypatch.setattr(
        LocalHost,
        "_plan_exec",
        lambda self, *, user, needs_shell: ExecPlan(route=ExecRoute.SSH_CHANNEL),
    )
    with pytest.raises(AssertionError, match=r"planned the 'ssh-channel' route but ran on "):
        await LocalHost().exec("true")


@pytest.mark.asyncio
async def test_the_local_subprocess_primitive_records_its_route():
    result = await LocalHost()._exec_subprocess("true", 10.0)
    assert result.route == "local-subprocess"


@pytest.mark.asyncio
async def test_the_default_session_primitive_records_its_route():
    host = LocalHost()
    try:
        result = await host._live_session_mgr().run_cmd(cmd="true", timeout=10.0)
    finally:
        await host.close()
    assert result.route == "default-session"


@pytest.mark.asyncio
async def test_the_ssh_channel_primitive_records_its_route():
    from types import SimpleNamespace

    from pydantic import SecretStr

    from otto.host.session import SessionManager
    from tests.unit.host.test_session import _FakeSshProcess, _StubExecSession

    conn = SimpleNamespace(
        credentials=("alice", SecretStr("pw")), login_target="alice", proxy_hops=[], term="ssh"
    )
    mgr = SessionManager(connections=conn, session_factory=_StubExecSession, host_id="h")
    channel = SimpleNamespace(create_process=AsyncMock(return_value=_FakeSshProcess(["out\n"])))
    result = await mgr.exec_on(conn=channel, cmd="id", timeout=10.0)
    assert result.route == "ssh-channel"


@pytest.mark.asyncio
async def test_the_pooled_shell_primitive_records_its_route():
    from tests.unit.host.test_session_exec_pooled import _exec, _mgr

    result = await _exec(_mgr(term="telnet"), ExecRoute.POOLED_SHELL, cmd="id")
    assert result.route == "pooled-shell"


@pytest.mark.asyncio
async def test_a_parent_result_with_no_route_is_a_bug_in_otto():
    """Every primitive records its route, so a parent answer naming none is not a lab condition."""
    parent = MagicMock()
    parent.id = parent.name = "test3"
    parent.term = "ssh"
    parent.exec = AsyncMock(
        return_value=CommandResult(status=Status.Success, value="", command="x", retcode=0)
    )
    container = _make_container(parent)
    container._docker_exec = AsyncMock(return_value="docker exec x true")
    with pytest.raises(AssertionError, match="names no route"):
        await container.exec("true")


@pytest.mark.asyncio
async def test_inside_as_user_the_transfer_budget_reads_the_plan_exec_dispatches(monkeypatch):
    """The shared backend's budget and its ``exec`` see one effective user.

    Inside ``as_user("app")`` on an ssh host the shared backend's ``exec``
    inherits ``app``, who has no direct cred, so it rides the pooled shell;
    the budget must be that shell's, not the bare channel's ``None``.
    """
    h = _unix_with_users(term="ssh")
    monkeypatch.setattr(type(h._session_mgr), "ambient_user", property(lambda self: "app"))
    captured: dict = {}

    def _capture(*, name, ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("otto.host.unix_host.build_transfer_backend", _capture)
    h._build_file_transfer(user=None)

    dispatched: list[ExecPlan] = []

    async def _record(self, plan, **_kw):
        dispatched.append(plan)
        return CommandResult(
            status=Status.Success, value="", command="x", retcode=0, route=plan.route.value
        )

    monkeypatch.setattr(UnixHost, "_run_plan", _record)
    await captured["ctx"].exec_cmd("true")

    assert [p.route for p in dispatched] == [ExecRoute.POOLED_SHELL]
    assert captured["ctx"].exec_line_budget() == h._session_mgr.line_budget(
        route=dispatched[0].route
    )
