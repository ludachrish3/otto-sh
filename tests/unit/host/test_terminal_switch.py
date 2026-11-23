"""``terminal=`` on ``run()``, ``exec()`` and ``open_session()``: the public switch.

Unset, every call runs where it ran before the switch existed and nothing
refuses. ``False`` on an ssh host is a second session without a pty, with
its own shell state; a mode a host cannot give is refused by name before
anything is opened, sent or logged. The no-terminal session itself (its
open, its stop) is ``test_session_terminal_mode.py``'s; these drive it
through the host's own entries, over the same real shell behind an asyncssh
surface.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from otto.host.command_frame import ZephyrFrame
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.errors import UnsupportedOptionError
from otto.host.exec_route import ExecRoute
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.conftest import active_context
from tests.unit.host.test_session_terminal_mode import _PipeConnection


def _unix(term: str = "ssh", **fields: object) -> UnixHost:
    creds = fields.pop("creds", [Cred(login="user", password="pass")])
    return UnixHost(  # ty: ignore[invalid-argument-type]
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=creds,
        term=term,
        log=LogMode.QUIET,
        **fields,
    )


class _EitherConnection(_PipeConnection):
    """A connection that also opens the channel's own shell, as a terminal session asks.

    That shell reads a pipe here too; what tells the two kinds of session
    apart is what each asked of the connection, which is recorded.
    """

    async def create_process(self, *args: Any, **kwargs: Any) -> Any:
        process = await super().create_process(*(args or ('exec "$SHELL"',)), **kwargs)
        self.opened[-1] = args
        return process


def _proxied() -> UnixHost:
    """An ssh host whose login is reached through another user's."""
    host = _unix()
    host._session_mgr._connections = SimpleNamespace(term="ssh", proxy_hops=[object()])  # ty: ignore[invalid-assignment]
    return host


@pytest_asyncio.fixture
async def piped(tmp_path):
    """An ssh host whose connection opens real local shells, and that connection."""
    host = _unix()
    conn = _EitherConnection(tmp_path)
    host._connections.ssh = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
    yield host, conn
    await host.close()
    await conn.reap()


def _refuses_opening(host: UnixHost) -> None:
    """Make any session open on *host* fail the test: a refusal must come first."""
    host._session_mgr._ensure_session = AsyncMock(  # ty: ignore[invalid-assignment]
        side_effect=AssertionError("a session was opened")
    )
    host._session_mgr._open_transport_session = AsyncMock(  # ty: ignore[invalid-assignment]
        side_effect=AssertionError("a session was opened")
    )


# ---------------------------------------------------------------------------
# The run entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_without_a_terminal_opens_a_session_with_no_pty(piped):
    host, conn = piped
    result = (await host.run("echo hi", terminal=False)).only
    assert (result.status, result.value) == (Status.Success, "hi")
    assert [asked.get("request_pty") for asked in conn.asked] == [False]


@pytest.mark.asyncio
async def test_the_session_without_a_terminal_keeps_its_own_state(piped):
    """State set without a terminal is there for the next such call."""
    host, conn = piped
    await host.run("cd /tmp && V=kept", terminal=False)
    result = (await host.run("echo $PWD $V", terminal=False)).only
    assert result.value == "/tmp kept"
    assert len(conn.processes) == 1


@pytest.mark.asyncio
async def test_a_sequence_without_a_terminal_runs_every_command_there(piped):
    host, conn = piped
    results = await host.run(["V=seq", "echo $V"], terminal=False)
    assert [r.value for r in results] == ["", "seq"]
    assert [asked.get("request_pty") for asked in conn.asked] == [False]


@pytest.mark.asyncio
async def test_a_dead_session_without_a_terminal_is_replaced_in_its_own_place(piped):
    """The terminal session and its recorded user are left alone."""
    host, conn = piped
    await host.run("V=term")
    await host.run("V=plain", terminal=False)
    mgr = host._session_mgr
    default, baseline, dead = mgr._session, mgr._baseline_user, mgr._plain_session
    assert dead is not None
    await dead.close()
    assert (await host.run("echo ${V-fresh}", terminal=False)).only.value == "fresh"
    assert mgr._plain_session is not dead
    assert (mgr._session, mgr._baseline_user) == (default, baseline)
    assert (await host.run("echo $V")).only.value == "term"
    assert len(conn.processes) == 3


@pytest.mark.asyncio
async def test_the_two_modes_are_two_sessions_and_close_closes_both(piped):
    """A terminal run and a no-terminal run never share a shell; neither closes the other."""
    host, conn = piped
    await host.run("V=plain", terminal=False)
    await host.run("V=term")
    assert [("term_type" in asked, asked.get("request_pty")) for asked in conn.asked] == [
        (False, False),
        (True, None),
    ]
    assert (await host.run("echo $V", terminal=False)).only.value == "plain"
    assert (await host.run("echo $V")).only.value == "term"
    assert (await host.run("echo $V", terminal=True)).only.value == "term"
    mgr = host._session_mgr
    plain, default = mgr._plain_session, mgr._session
    assert plain is not None
    assert default is not None
    assert plain is not default
    await host.close()
    assert (plain.alive, default.alive) == (False, False)
    assert (mgr._plain_session, mgr._session) == (None, None)


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        (lambda: _unix("telnet"), "needs an ssh transport; this host's is telnet"),
        (_proxied, "cannot reach a login that is proxied through another user"),
    ],
    ids=["telnet", "proxied-login"],
)
@pytest.mark.parametrize("verb", ["run", "exec", "open_session"])
@pytest.mark.asyncio
async def test_no_terminal_is_refused_by_name_where_the_transport_cannot_leave_it_out(
    host, reason, verb
):
    h = host()
    _refuses_opening(h)
    with pytest.raises(UnsupportedOptionError) as refused:
        await getattr(h, verb)("true" if verb != "open_session" else "aux", terminal=False)
    assert refused.value.option == "terminal"
    assert str(refused.value) == (
        f"box: terminal= is not supported on this host: terminal=False {reason}"
    )


@pytest.mark.asyncio
async def test_no_terminal_is_refused_on_a_host_with_a_session_setup_hook():
    from otto.host.session_setup import SessionSetup

    h = _unix()
    h._session_mgr._session_setup = SessionSetup(name="enter-cli")
    _refuses_opening(h)
    with pytest.raises(UnsupportedOptionError, match="cannot run the session setup 'enter-cli'"):
        await h.run("true", terminal=False)


@pytest.mark.parametrize("switch", [{"user": "root"}, {"sudo": True}], ids=["user", "sudo"])
@pytest.mark.asyncio
async def test_run_without_a_terminal_refuses_a_user_switch(switch):
    h = _unix(creds=[Cred(login="user", password="pass"), Cred(login="root", password="pw")])
    _refuses_opening(h)
    with pytest.raises(UnsupportedOptionError, match=r"terminal=False cannot switch users"):
        await h.run("id", terminal=False, **switch)


@pytest.mark.asyncio
async def test_run_without_a_terminal_is_refused_inside_as_user():
    """The switch lives on the terminal session; a no-terminal run there would run as another."""
    h = _unix()
    h._ambient_user = lambda: "root"  # ty: ignore[invalid-assignment]
    _refuses_opening(h)
    with pytest.raises(UnsupportedOptionError, match=r"terminal=False cannot switch users"):
        await h.run("id", terminal=False)


@pytest.mark.parametrize("verb", ["run", "exec", "open_session"])
@pytest.mark.asyncio
async def test_the_local_host_refuses_a_terminal_and_takes_none(verb):
    host = LocalHost()
    try:
        with pytest.raises(UnsupportedOptionError) as refused:
            await getattr(host, verb)("true" if verb != "open_session" else "aux", terminal=True)
        assert str(refused.value) == (
            f"{host.name}: terminal= is not supported on this host: terminal=True is not "
            f"available on the local host, whose sessions read a pipe"
        )
        if verb == "open_session":
            session = await host.open_session("aux", terminal=False)
            assert (await session.run("echo hi")).only.value == "hi"
        else:
            result = await getattr(host, verb)("echo hi", terminal=False)
            assert getattr(result, "only", result).value == "hi"
    finally:
        await host.close()


@pytest.mark.parametrize("asks", [{"sudo": True}, {}], ids=["sudo", "inside-as_user"])
@pytest.mark.asyncio
async def test_no_terminal_on_the_local_host_refuses_no_switch_it_took_before(asks):
    """Its one session reads a pipe either way, so ``terminal=False`` changes nothing there."""
    with active_context(dry_run=True):
        host = LocalHost()
        if not asks:
            host._ambient_user = lambda: "root"  # ty: ignore[invalid-assignment]
        assert (await host.run("true", terminal=False, **asks)).only.status is Status.NotRun
        assert (await host.exec("true", terminal=False, **asks)).status is Status.NotRun


@pytest.mark.parametrize("entry", ["run_cmd", "open_session"])
@pytest.mark.asyncio
async def test_the_session_manager_s_own_entries_refuse_what_the_host_refuses(entry):
    """A caller of the manager gets the refusal too, never a session of the other mode."""
    h = _unix("telnet")
    _refuses_opening(h)
    call = (
        h._session_mgr.run_cmd(cmd="true", terminal=False)
        if entry == "run_cmd"
        else h._session_mgr.open_session(name="aux", terminal=False)
    )
    with pytest.raises(UnsupportedOptionError, match="terminal=False needs an ssh transport"):
        await call


@pytest.mark.asyncio
async def test_a_console_refuses_no_terminal(console_unix_host):
    with pytest.raises(UnsupportedOptionError, match="this host's is console"):
        await console_unix_host.run("true", terminal=False)


@pytest.mark.asyncio
async def test_a_session_without_a_terminal_says_nothing_about_the_line_budget(piped):
    """Its echo fact is a pipe's, not a terminal's: the budget stays the unmeasured one."""
    host, _conn = piped
    before = host._session_mgr._measured_echoes()
    await host.open_session("aux", terminal=False)
    assert host._session_mgr._measured_echoes() is before is True


@pytest.mark.parametrize("verb", ["run", "exec", "open_session"])
@pytest.mark.asyncio
async def test_an_embedded_host_refuses_no_terminal(verb):
    host = EmbeddedHost(
        ip="10.0.0.9",
        element=Element(name="mcu"),
        term="telnet",
        command_frame=ZephyrFrame(),
        log=LogMode.QUIET,
    )
    with pytest.raises(UnsupportedOptionError, match="terminal=False is not available on an"):
        await getattr(host, verb)("true" if verb != "open_session" else "aux", terminal=False)


@pytest.mark.asyncio
async def test_an_embedded_host_refuses_no_terminal_whatever_its_transport_is_called():
    """The family decides: a console is a terminal even under a term named ssh."""
    host = EmbeddedHost(
        ip="10.0.0.9",
        element=Element(name="mcu"),
        term="ssh",
        valid_terms=["ssh"],
        command_frame=ZephyrFrame(),
        log=LogMode.QUIET,
    )
    with pytest.raises(UnsupportedOptionError, match="terminal=False is not available on an"):
        await host.run("true", terminal=False)


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.asyncio
async def test_a_dry_run_takes_the_switch_and_declines(terminal):
    with active_context(dry_run=True):
        h = _unix()
        assert (await h.run("true", terminal=terminal)).only.status is Status.NotRun
        assert (await h.exec("true", terminal=terminal)).status is Status.NotRun


@pytest.mark.asyncio
async def test_a_dry_run_still_refuses_a_mode_the_host_cannot_give():
    with active_context(dry_run=True):
        h = _unix("telnet")
        for call in (h.run("true", terminal=False), h.exec("true", terminal=False)):
            with pytest.raises(UnsupportedOptionError):
                await call
        with pytest.raises(UnsupportedOptionError):
            await h.open_session("aux", terminal=False)


# ---------------------------------------------------------------------------
# The exec entry
# ---------------------------------------------------------------------------


def _routes(host: UnixHost) -> AsyncMock:
    """Record the route each ``exec`` on *host* is dispatched to, running nothing."""

    async def ran(*, route: ExecRoute, **_kw: object) -> CommandResult:
        return CommandResult(
            status=Status.Success, value="", command="true", retcode=0, route=route.value
        )

    host._session_mgr.exec_on_route = AsyncMock(side_effect=ran)  # ty: ignore[invalid-assignment]
    return host._session_mgr.exec_on_route  # ty: ignore[invalid-return-type]


@pytest.mark.parametrize(
    ("terminal", "route"),
    [
        (None, ExecRoute.SSH_CHANNEL),
        (False, ExecRoute.SSH_CHANNEL),
        (True, ExecRoute.POOLED_SHELL),
    ],
)
@pytest.mark.asyncio
async def test_exec_on_ssh_takes_the_channel_unless_a_terminal_is_asked_for(terminal, route):
    h = _unix()
    ran = _routes(h)
    result = await h.exec("true", terminal=terminal)
    assert ran.await_args.kwargs["route"] is route
    assert result.route == route.value


@pytest.mark.asyncio
async def test_exec_with_a_terminal_as_a_direct_login_user_still_takes_a_shell():
    """The direct-credential shortcut is a bare channel; a terminal is a shell as that user."""
    h = _unix(creds=[Cred(login="user", password="pass"), Cred(login="root", password="pw")])
    ran = _routes(h)
    h._connections.ssh_as = AsyncMock()  # ty: ignore[invalid-assignment]
    await h.exec("id", user="root", terminal=True)
    assert ran.await_args.kwargs["route"] is ExecRoute.POOLED_SHELL
    assert ran.await_args.kwargs["user"] == "root"
    h._connections.ssh_as.assert_not_awaited()


@pytest.mark.parametrize("terminal", [None, True])
@pytest.mark.asyncio
async def test_exec_on_telnet_is_a_shell_with_a_terminal_set_or_not(terminal):
    h = _unix("telnet")
    ran = _routes(h)
    await h.exec("true", terminal=terminal)
    assert ran.await_args.kwargs["route"] is ExecRoute.POOLED_SHELL


@pytest.mark.parametrize(
    ("asks", "reason"),
    [
        ({"sudo": True}, "cannot elevate (sudo=True)"),
        ({"expects": ("name: ", "x\n")}, "cannot answer prompts (expects=)"),
        ({"user": "app"}, "cannot run as 'app', who is reached by switching users"),
    ],
    ids=["sudo", "expects", "switched-user"],
)
@pytest.mark.asyncio
async def test_exec_without_a_terminal_refuses_what_only_a_terminal_session_does(
    asks, reason, monkeypatch
):
    h = _unix(
        creds=[
            Cred(login="user", password="pass"),
            Cred(login="app", password="pw", via="user"),
        ]
    )
    ran = _routes(h)
    monkeypatch.setattr(h._connections, "has_direct_cred", lambda *, user: False)
    monkeypatch.setattr(h, "_prepare_exec_elevation", AsyncMock())
    with pytest.raises(UnsupportedOptionError) as refused:
        await h.exec("true", terminal=False, **asks)
    assert f"terminal=False {reason}" in str(refused.value)
    ran.assert_not_awaited()
    h._prepare_exec_elevation.assert_not_awaited()  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_exec_without_a_terminal_as_a_direct_login_user_takes_their_channel():
    h = _unix(creds=[Cred(login="user", password="pass"), Cred(login="root", password="pw")])
    conn = SimpleNamespace()
    h._connections.ssh_as = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
    h._session_mgr.exec_on = AsyncMock(  # ty: ignore[invalid-assignment]
        return_value=CommandResult(
            status=Status.Success, value="root", command="id", retcode=0, route="ssh-channel"
        )
    )
    assert (await h.exec("id", user="root", terminal=False)).value == "root"
    assert h._session_mgr.exec_on.await_args.kwargs["conn"] is conn


@pytest.mark.asyncio
async def test_exec_without_a_terminal_on_the_local_host_still_answers_prompts():
    """The local host's shell session reads a pipe, so nothing about it needs refusing."""
    host = LocalHost()
    try:
        result = await host.exec(
            "printf 'name: '; read -r name; echo got $name",
            expects=("name: ", "otto\n"),
            terminal=False,
        )
        assert result.value.endswith("got otto")
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# The open_session entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_named_session_without_a_terminal_keeps_its_mode(piped):
    host, conn = piped
    plain = await host.open_session("aux", terminal=False)
    assert plain.reads_a_terminal is False
    assert conn.asked[0].get("request_pty") is False
    assert (await plain.run("echo hi")).only.value == "hi"
    assert await host.open_session("aux") is plain
    assert await host.open_session("aux", terminal=False) is plain
    with pytest.raises(ValueError, match="session 'aux' is open without a terminal") as refused:
        await host.open_session("aux", terminal=True)
    assert "open it under another name to have it with one" in str(refused.value)
    assert plain.alive


@pytest.mark.asyncio
async def test_a_named_session_with_a_terminal_refuses_the_other_mode_by_name(piped):
    host, _conn = piped
    session = await host.open_session("mon")
    with pytest.raises(ValueError, match="session 'mon' is open with a terminal") as refused:
        await host.open_session("mon", terminal=False)
    assert "to have it without one" in str(refused.value)
    assert session.alive


async def _enter_as_user(session: Any) -> None:
    async with session.as_user(user="root"):
        raise AssertionError("the switch was entered")


@pytest.mark.parametrize("switch", ["as_user", "switch_user"])
@pytest.mark.asyncio
async def test_a_named_session_without_a_terminal_refuses_a_user_switch(piped, switch):
    host, conn = piped
    plain = await host.open_session("aux", terminal=False)
    written = len(conn.processes[0].written)
    attempt = _enter_as_user(plain) if switch == "as_user" else plain.switch_user(user="root")
    with pytest.raises(UnsupportedOptionError, match=r"terminal=False cannot switch users"):
        await attempt
    assert len(conn.processes[0].written) == written
