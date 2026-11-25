"""Which session one ``exec`` takes: a host decides two things, each in one place.

Whether ``run`` and ``exec`` share the host's one line (``_shares_one_line``)
picks the default session or a pooled one (``BaseHost._exec_on_manager``);
whether a pooled session has a terminal is ``BaseHost._exec_plain``'s answer.
These tests drive each case the ``exec`` contract page lists through the real
dispatch, and pin that the transfer line budget reads the same answers.
"""

import ast
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host import DockerContainerHost, EmbeddedHost, LocalHost, UnixHost
from otto.host.command_frame import ExecInput, ExecRestore
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.session import SessionManager, typed_line_budget
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.host._sessions import session_manager
from tests.unit.host.test_docker_host import _make_container, _mock_parent
from tests.unit.host.test_embedded_host import _embedded_console
from tests.unit.host.test_unix_host import _unix_host


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


class _Entries:
    """The two session manager entries of one host, spied: which ran, and with which ``plain``."""

    def __init__(self, mgr: SessionManager, monkeypatch) -> None:
        ok = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        self.pooled = AsyncMock(return_value=ok)
        self.default = AsyncMock(return_value=ok)
        monkeypatch.setattr(mgr, "exec_pooled", self.pooled)
        monkeypatch.setattr(mgr, "exec_on_default_session", self.default)

    def ran(self) -> "tuple[str, bool | None]":
        """``("pooled", plain)`` or ``("shared", None)``: the one entry awaited, exactly once."""
        calls = self.pooled.await_count + self.default.await_count
        assert calls == 1, f"{calls} manager entries awaited"
        if self.default.await_count:
            return ("shared", None)
        return ("pooled", self.pooled.await_args.kwargs["plain"])

    def none_ran(self) -> bool:
        return self.pooled.await_count == 0 and self.default.await_count == 0


def _spy_entries(host, monkeypatch) -> _Entries:
    return _Entries(host._live_session_mgr(), monkeypatch)


async def _exec_once(host, monkeypatch, *, user=None, needs_shell=False) -> _Entries:
    """Dispatch one ``exec`` through ``BaseHost._exec_one``, with the manager's entries spied.

    A console's switch to another user is the session's own work, pinned in
    ``test_unix_host.py``; here it is a no-op so the session it ends on is seen.
    """
    entries = _spy_entries(host, monkeypatch)

    @contextlib.asynccontextmanager
    async def _no_switch(*, user):
        yield

    monkeypatch.setattr(host, "as_user", _no_switch)
    await host._exec_one("true", 10.0, user=user, needs_shell=needs_shell)
    return entries


def _hooked(stack) -> UnixHost:
    from otto.host.session_setup import SessionSetup
    from tests._fixtures.session_setup_hooks import noop_session_setup

    host = _unix_host(term="ssh")
    _declare_hook(host, SessionSetup(name=stack.enter_context(noop_session_setup("case-hook"))))
    return host


def _proxied(stack) -> UnixHost:
    from tests.unit.host.test_session_manager_setup import _conn

    hop = Cred(login="app", proxy="su", via="admin")
    host = _unix_host(term="ssh")
    host._session_mgr = session_manager(
        connections=_conn(hops=[hop], login_target="app"), name="h", host_id="h"
    )
    return host


def _own_user_hooked(stack) -> UnixHost:
    from otto.host.session_setup import SessionSetup
    from tests._fixtures.session_setup_hooks import noop_session_setup

    host = _unix_with_users()
    _declare_hook(host, SessionSetup(name=stack.enter_context(noop_session_setup("case-own"))))
    return host


#: Per case the ``exec`` page lists: ``(build, user, needs_shell)`` for every host and
#: call it names. ``build`` takes the test's exit stack.
_CASE_HOSTS = {
    "shared": [
        pytest.param(lambda _s: _unix_console(), None, False, id="console"),
        # A console takes the default session whatever the call needs.
        pytest.param(lambda _s: _unix_console(), None, True, id="console-needs-shell"),
        pytest.param(lambda _s: _unix_console(), "root", True, id="console-user"),
        pytest.param(lambda _s: _embedded_console(), None, False, id="embedded-console"),
        pytest.param(lambda _s: _embedded_telnet(), None, False, id="embedded-telnet"),
    ],
    "pooled-terminal": [
        pytest.param(lambda _s: _unix_host(term="telnet"), None, False, id="telnet"),
        pytest.param(lambda _s: _unix_with_users(term="telnet"), "root", False, id="telnet-user"),
        pytest.param(lambda _s: _unix_host(term="ssh"), None, True, id="ssh-needs-shell"),
        pytest.param(lambda _s: _unix_with_users(), "root", True, id="ssh-own-user-needs-shell"),
        pytest.param(lambda _s: _unix_with_users(), "app", False, id="ssh-switched-user"),
        pytest.param(_proxied, None, False, id="proxied"),
        pytest.param(_hooked, None, False, id="hook"),
        pytest.param(_own_user_hooked, "root", False, id="ssh-own-user-hook"),
    ],
    "pooled-no-terminal": [
        pytest.param(lambda _s: _unix_host(term="ssh"), None, False, id="ssh"),
        pytest.param(lambda _s: _unix_with_users(), "root", False, id="ssh-own-user"),
        pytest.param(lambda _s: LocalHost(), None, False, id="local"),
        pytest.param(lambda _s: LocalHost(), None, True, id="local-needs-shell"),
    ],
}


def _page_cases():
    from scripts.render_support_matrix import EXEC_SESSION_CASES

    return [
        pytest.param(case.id, *host.values, id=f"{case.id}-{host.id}")
        for case in EXEC_SESSION_CASES
        if case.id != "container"
        for host in _CASE_HOSTS[case.id]
    ]


def test_the_exec_page_lists_the_cases_this_module_drives():
    from scripts.render_support_matrix import COMMAND_PAGES, EXEC_SESSION_CASES

    assert COMMAND_PAGES["exec"].session_cases == EXEC_SESSION_CASES
    assert [case.id for case in EXEC_SESSION_CASES] == [*_CASE_HOSTS, "container"]


@pytest.mark.parametrize(("case", "build", "user", "needs_shell"), _page_cases())
@pytest.mark.asyncio
async def test_every_case_the_exec_page_lists_is_what_otto_does(
    case, build, user, needs_shell, monkeypatch
):
    """``EXEC_SESSION_CASES`` (rendered on ``docs/command-contracts/exec.md``) against the dispatch.

    ``pooled-no-terminal`` is a session opened without a terminal (``plain``),
    or one whose transport has none (the local host's pipe).
    """
    with contextlib.ExitStack() as stack:
        host = build(stack)
        entries = await _exec_once(host, monkeypatch, user=user, needs_shell=needs_shell)
        ran, plain = entries.ran()
        types_into_terminal = host._live_session_mgr()._types_into_terminal
    if case == "shared":
        assert ran == "shared"
    elif case == "pooled-terminal":
        assert (ran, plain, types_into_terminal) == ("pooled", False, True)
    else:
        assert ran == "pooled"
        assert plain or not types_into_terminal


@pytest.mark.parametrize("term", ["ssh", "telnet"])
@pytest.mark.asyncio
async def test_the_container_case_the_exec_page_lists_is_what_otto_does(term, monkeypatch):
    """A container's ``exec`` is its parent's ``docker exec``; its own manager runs nothing."""
    parent = _mock_parent(term=term)
    container = _make_container(parent)
    container._docker_exec = AsyncMock(return_value="docker exec abc123def456 true")
    entries = _spy_entries(container, monkeypatch)
    await container._exec_one("true", 10.0)
    parent.exec.assert_awaited_once()
    assert parent.exec.await_args.args[0] == "docker exec abc123def456 true"
    assert entries.none_ran()


def test_no_family_overrides_exec_or_exec_one():
    """``exec`` and ``_exec_one`` are the base host's; a family overrides the two hooks under them.

    The container is the one family that overrides ``exec``: it rewrites its
    arguments and delegates.
    """
    for cls in [UnixHost, EmbeddedHost, LocalHost]:
        assert "exec" not in vars(cls), cls.__name__
    for cls in [UnixHost, EmbeddedHost, LocalHost, DockerContainerHost]:
        assert "_exec_one" not in vars(cls), cls.__name__


_MANAGER_EXEC_ENTRIES = {"exec_pooled", "exec_on_default_session"}
_THE_DISPATCH = "BaseHost._exec_on_manager"


def test_only_the_one_dispatch_calls_a_manager_exec_entry():
    """``BaseHost._exec_on_manager`` is the one caller in ``src/otto`` of either manager entry.

    A second caller would pick a session, forward the user or fold the log
    apart from the one dispatch, and still pass every case above.
    """
    src = CHECKOUT_ROOT / "src" / "otto"
    callers: list[str] = []

    class _Calls(ast.NodeVisitor):
        def __init__(self, path: Path) -> None:
            self.path, self.scope = path, []

        def _scoped(self, node: ast.AST, name: str) -> None:
            self.scope.append(name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scoped(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._scoped(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MANAGER_EXEC_ENTRIES:
                callers.append(f"{self.path.relative_to(src)}:{node.lineno} {'.'.join(self.scope)}")
            self.generic_visit(node)

    for path in sorted(src.rglob("*.py")):
        _Calls(path).visit(ast.parse(path.read_text(encoding="utf-8")))
    offenders = [c for c in callers if not c.endswith(f" {_THE_DISPATCH}")]
    assert offenders == [], f"only {_THE_DISPATCH} may call {sorted(_MANAGER_EXEC_ENTRIES)}"
    # Positive control: a renamed entry or a wrong path would leave the scan empty.
    assert len(callers) == 2, callers


@pytest.mark.asyncio
async def test_an_embedded_exec_runs_on_the_default_session_with_its_restore(monkeypatch):
    """``EmbeddedHost.exec`` reaches ``exec_on_default_session`` once, and the START restore."""
    from types import SimpleNamespace
    from typing import cast

    from otto.host.command_frame import build_command_frame
    from otto.host.session import ShellSession

    seen: list[ExecInput | None] = []

    class _Recording(ShellSession):
        async def open_transport(self) -> None: ...
        async def write_transport(self, data: str) -> None: ...
        async def read_transport_until(self, pattern):  # pragma: no cover - never reads
            raise AssertionError("never reads")

        async def _take_unread(self) -> str:
            return ""

        async def close(self) -> None:
            self._alive = False

        async def _ensure_initialized(self) -> None:
            self._initialized = True
            self._alive = True

        async def run_cmd(self, cmd, *args, exec_input=None, **kwargs) -> CommandResult:
            seen.append(exec_input)
            return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    frame = build_command_frame(type_name="ash")
    mgr = session_manager(
        connections=cast("object", SimpleNamespace(term="console", proxy_hops=[])),  # type: ignore[arg-type]
        name="z",
        command_frame=frame,
        session_factory=lambda: _Recording(command_frame=frame),
    )
    entry = AsyncMock(wraps=mgr.exec_on_default_session)
    monkeypatch.setattr(mgr, "exec_on_default_session", entry)
    host = EmbeddedHost(
        ip="192.0.2.1", element=Element(name="z"), command_frame=frame, log=LogMode.QUIET
    )
    host._session_mgr = mgr
    try:
        await host.exec("echo hi")
    finally:
        host._connections = None  # type: ignore[assignment]
    entry.assert_awaited_once()
    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].restore is ExecRestore.START


@pytest.mark.parametrize("term", ["ssh", "telnet"])
@pytest.mark.parametrize(
    "kwargs",
    [{"expects": [("Password:", "pw")]}, {"user": "app"}],
    ids=["expects", "user"],
)
@pytest.mark.asyncio
async def test_a_container_exec_without_a_terminal_is_never_refused(term, kwargs):
    """``docker exec -i`` has no terminal, whatever the call needs, over any parent."""
    parent = _mock_parent(term=term)
    container = _make_container(parent)
    container._docker_exec = AsyncMock(return_value="docker exec abc123def456 cat")
    result = await container.exec("cat", terminal=False, **kwargs)
    assert result.status is Status.Success
    parent.exec.assert_awaited_once()


def test_a_per_user_transfer_line_room_reads_that_users_session_mode(monkeypatch):
    """A backend built for ``app`` sizes its lines to ``app``'s session, one with a terminal.

    The ambient-user half is
    ``test_inside_as_user_the_transfer_line_room_reads_the_mode_exec_takes``.
    """
    h = _unix_with_users(term="ssh")
    captured = {}

    def _capture(*, name, ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("otto.host.unix_host.build_transfer_backend", _capture)
    h._build_file_transfer(user="app")

    assert h._exec_plain(user="app", needs_shell=False) is False
    budget = captured["ctx"].exec_line_room("")
    assert budget is not None
    assert budget == h._session_mgr.line_budget(shared=False)
    typed = ExecInput(stdin_eof=True, restore=SessionManager._exec_restore_for(False))
    assert budget == typed_line_budget(h._session_mgr._command_frame, echoes=True, exec_input=typed)
    # The host's own exec rides a session without a terminal, unbounded.
    assert h._exec_plain(user=None, needs_shell=False) is True
    assert h._session_mgr.line_budget(shared=False, plain=True) is None


@pytest.mark.asyncio
async def test_inside_as_user_the_transfer_line_room_reads_the_mode_exec_takes(monkeypatch):
    """The shared backend's budget and its ``exec`` see one effective user.

    Inside ``as_user("app")`` on an ssh host the shared backend's ``exec``
    inherits ``app``, who has no direct cred, so it takes a session with a
    terminal; the budget must be that session's, not the unbounded one.
    """
    h = _unix_with_users(term="ssh")
    monkeypatch.setattr(type(h._session_mgr), "ambient_user", property(lambda self: "app"))
    captured: dict = {}

    def _capture(*, name, ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("otto.host.unix_host.build_transfer_backend", _capture)
    h._build_file_transfer(user=None)
    entries = _spy_entries(h, monkeypatch)
    await captured["ctx"].exec_cmd("true")

    ran, plain = entries.ran()
    assert (ran, plain) == ("pooled", False)
    room = captured["ctx"].exec_line_room("")
    assert room is not None
    assert room == h._session_mgr.line_budget(shared=False, plain=plain)


def _declare_hook(host: UnixHost, setup) -> None:
    """Give a built *host* the ``session_setup`` *setup*, where its session manager reads it.

    The manager takes the hook at construction, so setting the host's field
    afterwards alone would leave the session mode decided without it.
    """
    host.session_setup = setup
    host._live_session_mgr()._session_setup = setup


def _hooked_or_proxied(host: UnixHost, reason, *, direct_cred_user: str, monkeypatch, stack):
    """*host* with a ``session_setup`` hook, a proxied login, or neither (*reason* ``None``).

    *direct_cred_user* is the one user its connections say has a direct credential.
    """
    from otto.host.connections import ConnectionManager
    from otto.host.session_setup import SessionSetup
    from tests._fixtures.session_setup_hooks import noop_session_setup

    conns = host._live_connections()
    if reason == "session_setup":
        _declare_hook(host, SessionSetup(name=stack.enter_context(noop_session_setup("plan-hook"))))
    elif reason == "proxy_hops":
        hop = Cred(login="app", proxy="su", via="user")
        monkeypatch.setattr(ConnectionManager, "proxy_hops", property(lambda _self: [hop]))
    monkeypatch.setattr(conns, "has_direct_cred", lambda *, user: user == direct_cred_user)
    return host


@pytest.mark.parametrize("reason", ["session_setup", "proxy_hops"])
def test_a_direct_cred_user_on_a_hooked_or_proxied_host_takes_a_session_with_a_terminal(
    reason, unix_host_ssh, monkeypatch
):
    with contextlib.ExitStack() as stack:
        host = _hooked_or_proxied(
            unix_host_ssh, reason, direct_cred_user="alice", monkeypatch=monkeypatch, stack=stack
        )
        assert not host._shares_one_line()
        assert host._exec_plain(user="alice", needs_shell=False) is False
        assert host._exec_plain(user=None, needs_shell=False) is False


def test_a_direct_cred_user_on_a_plain_ssh_host_gets_a_session_without_a_terminal(
    unix_host_ssh, monkeypatch
):
    """Their own login carries a session without a terminal."""
    with contextlib.ExitStack() as stack:
        host = _hooked_or_proxied(
            unix_host_ssh, None, direct_cred_user="alice", monkeypatch=monkeypatch, stack=stack
        )
        assert host._exec_plain(user="alice", needs_shell=False) is True


@pytest.mark.parametrize(
    ("terminal", "plain"), [(None, True), (False, True), (True, False)], ids=str
)
@pytest.mark.asyncio
async def test_terminal_decides_the_mode_of_an_ssh_exec(terminal, plain, monkeypatch):
    """``terminal=True`` is a reason to want a shell; unset and ``False`` are answered alike."""
    host = _unix_host(term="ssh")
    entries = _spy_entries(host, monkeypatch)
    await host.exec("true", terminal=terminal)
    assert entries.ran() == ("pooled", plain)


def _transfer_ctx(host: UnixHost, monkeypatch):
    captured: dict = {}

    def _capture(*, name, ctx):
        captured["ctx"] = ctx
        return object()

    monkeypatch.setattr("otto.host.unix_host.build_transfer_backend", _capture)
    host._build_file_transfer(user=None)
    return captured["ctx"]


@pytest.mark.parametrize("frame", [None, "ash"], ids=["bash", "ash"])
def test_the_transfer_line_room_is_unbounded_for_an_exec_without_a_terminal(frame, monkeypatch):
    host = _unix_host(term="ssh")
    if frame is not None:
        from otto.host.command_frame import build_command_frame

        host._session_mgr._command_frame = build_command_frame(type_name=frame)
    assert _transfer_ctx(host, monkeypatch).exec_line_room("x" * 100) is None


@pytest.mark.parametrize("reason", ["session_setup", "telnet"])
def test_the_transfer_line_room_is_bounded_where_exec_has_a_terminal(reason, monkeypatch):
    with contextlib.ExitStack() as stack:
        if reason == "telnet":
            host = _unix_host(term="telnet")
        else:
            from otto.host.session_setup import SessionSetup
            from tests._fixtures.session_setup_hooks import noop_session_setup

            host = _unix_host(term="ssh")
            _declare_hook(host, SessionSetup(name=stack.enter_context(noop_session_setup("lr"))))
        room = _transfer_ctx(host, monkeypatch).exec_line_room("")
        assert room is not None
        assert room == host._session_mgr.line_budget(shared=False)
