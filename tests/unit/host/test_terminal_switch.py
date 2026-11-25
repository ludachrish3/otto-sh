"""``terminal=`` on ``run()``, ``exec()`` and ``open_session()``: the public switch.

Unset, every call runs where it ran before the switch existed and nothing
refuses. ``False`` on an ssh host is a second session without a pty, with
its own shell state; a mode a host cannot give is refused by name before
anything is opened, sent or logged. The no-terminal session itself (its
open, its stop) is ``test_session_terminal_mode.py``'s; these drive it
through the host's own entries, over the same real shell behind an asyncssh
surface.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from otto.host.command_frame import ZephyrFrame
from otto.host.daemon import detached_command, launch_command
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.errors import UnsupportedOptionError
from otto.host.exec_route import ExecRoute
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.process_stop import SESSION_MARK
from otto.host.session import SshSession
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status, wait_for_async
from scripts.process_group import process_state
from tests.conftest import active_context
from tests.unit.host.test_session_terminal_mode import _PipeConnection


def _unix(term: str = "ssh", **fields: object) -> UnixHost:
    creds = fields.pop("creds", [Cred(login="user", password="pass")])
    ip = fields.pop("ip", "10.0.0.1")
    return UnixHost(  # ty: ignore[invalid-argument-type]
        ip=ip,
        element=Element(name="box"),
        creds=creds,
        term=term,
        log=LogMode.QUIET,
        **fields,
    )


class _EitherConnection(_PipeConnection):
    """A connection that also opens the channel's own shell, as a terminal session asks.

    That shell reads a pipe here too; what tells the two kinds of session
    apart is what each asked of the connection, which is recorded. With
    *login*, that shell is a login shell, as a server's pty shell is: it
    reads the profile.
    """

    def __init__(self, home: Path, *, login: bool = False, **kwargs: Any) -> None:
        super().__init__(home, **kwargs)
        self._terminal_shell = 'exec "$SHELL" -l' if login else 'exec "$SHELL"'

    async def create_process(self, *args: Any, **kwargs: Any) -> Any:
        process = await super().create_process(*(args or (self._terminal_shell,)), **kwargs)
        self.opened[-1] = args
        return process


def _proxied() -> UnixHost:
    """An ssh host whose login is reached through another user's."""
    host = _unix()
    host._session_mgr._connections = SimpleNamespace(term="ssh", proxy_hops=[object()])  # ty: ignore[invalid-assignment]
    return host


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


@pytest.mark.parametrize(
    ("command", "value"), [("echo err >&2; echo out", "err\nout"), ("echo err >&2", "err")]
)
@pytest.mark.asyncio
async def test_an_exec_without_a_terminal_keeps_stderr_where_it_was_written(piped, command, value):
    """The connection forwards two pipes in its own order; the value keeps the written one."""
    host, _ = piped
    result = await host.exec(command, terminal=False)
    assert (result.status, result.value) == (Status.Success, value)


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
        (None, ExecRoute.POOLED_SHELL),
        (False, ExecRoute.POOLED_SHELL),
        (True, ExecRoute.POOLED_SHELL),
    ],
)
@pytest.mark.asyncio
async def test_exec_on_ssh_takes_a_pooled_session_set_or_not(terminal, route):
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


# ---------------------------------------------------------------------------
# An unset exec runs on a session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unset_exec_on_ssh_runs_on_a_session_without_a_terminal(piped):
    host, conn = piped
    result = await host.exec('test -t 1 && echo tty || echo pipe; echo "${OTTO_SESSION:+set}"')
    assert (result.status, result.value, result.route) == (
        Status.Success,
        "pipe\nset",
        "pooled-shell",
    )
    assert [asked.get("request_pty") for asked in conn.asked] == [False]


@pytest.mark.asyncio
async def test_eight_held_execs_are_on_eight_sessions_and_one_stays_idle(piped, tmp_path):
    """Concurrency is a session per call; afterwards ssh keeps one idle session."""
    host, conn = piped
    from otto.utils import wait_for_async

    gate = tmp_path / "gate"
    gate.mkdir()
    calls = [
        asyncio.create_task(
            host.exec(
                f"touch {gate}/started.{i}; "
                f"while [ ! -e {gate}/go ]; do sleep 0.05; done; echo $OTTO_SESSION"
            )
        )
        for i in range(8)
    ]
    try:
        await wait_for_async(
            lambda: len(list(gate.glob("started.*"))) == 8,
            30,
            on_timeout="eight execs never ran at once",
        )
        live = [s for s in host._session_mgr._named_sessions.values() if s.alive]
        assert len(live) == 8
    finally:
        (gate / "go").touch()
        results = await asyncio.gather(*calls)
    assert len({r.value for r in results}) == 8
    (idle,) = host._session_mgr._exec_pools[("user", True)]
    assert idle.alive
    assert [s for s in host._session_mgr._named_sessions.values() if s.alive] == [idle]
    assert len(conn.processes) == 8


@pytest.mark.parametrize("frame", [None, "ash"], ids=["bash", "ash"])
@pytest.mark.asyncio
async def test_a_long_line_arrives_byte_identical_on_an_unset_exec(piped, frame):
    host, _conn = piped
    if frame is not None:
        from otto.host.command_frame import build_command_frame

        host._session_mgr._command_frame = build_command_frame(type_name=frame)
    payload = "".join(chr(ord("a") + i % 26) for i in range(100_000))
    cksum = await asyncio.create_subprocess_exec(
        "cksum", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    expected = (await cksum.communicate(payload.encode()))[0].decode().strip()
    result = await host.exec(f"printf %s '{payload}' | cksum", timeout=60)
    assert (result.status, result.value) == (Status.Success, expected)


@pytest.mark.asyncio
async def test_a_printing_profile_sets_its_value_and_prints_into_no_output(piped, tmp_path, caplog):
    host, _conn = piped
    (tmp_path / ".profile").write_text("echo PROFILE-BANNER\nexport FROM_PROFILE=yes\n")
    caplog.set_level("DEBUG")
    cold = await host.exec("echo $FROM_PROFILE", log=LogMode.NORMAL)
    warm = await host.exec("echo $FROM_PROFILE", log=LogMode.NORMAL)
    assert (cold.value, warm.value) == ("yes", "yes")
    assert host._session_mgr._exec_pool_count == 1, "the warm call reused the session"
    assert "PROFILE-BANNER" not in caplog.text


async def _hook_exports(session: Any, _ctx: Any) -> None:
    await session.run("export APP_ENV=lab")


@pytest.mark.asyncio
async def test_an_unset_exec_on_a_hooked_host_runs_on_a_terminal_session(piped):
    from otto.host.session_setup import SessionSetup
    from tests._fixtures.session_setup_hooks import temporary_session_setup

    host, conn = piped
    with temporary_session_setup("exec-hook", _hook_exports) as name:
        host.session_setup = SessionSetup(name=name)
        host._session_mgr._session_setup = host.session_setup
        result = await host.exec("echo $APP_ENV")
    assert (result.status, result.value, result.route) == (Status.Success, "lab", "pooled-shell")
    assert [asked.get("request_pty") for asked in conn.asked] == [None]


@pytest.mark.asyncio
async def test_exec_without_a_terminal_on_a_hooked_host_is_refused_before_any_open():
    from otto.host.session_setup import SessionSetup

    h = _unix()
    h._session_mgr._session_setup = SessionSetup(name="enter-cli")
    _refuses_opening(h)
    with pytest.raises(UnsupportedOptionError, match="cannot run the session setup 'enter-cli'"):
        await h.exec("true", terminal=False)


@pytest.mark.parametrize("terminal", [None, False])
@pytest.mark.asyncio
async def test_a_direct_login_user_s_exec_is_a_session_over_their_own_login(tmp_path, terminal):
    h = _unix(creds=[Cred(login="user", password="pass"), Cred(login="root", password="pw")])
    own = _EitherConnection(tmp_path)
    h._connections.ssh = AsyncMock(side_effect=AssertionError("the host's own login was used"))  # ty: ignore[invalid-assignment]
    h._connections.ssh_as = AsyncMock(return_value=own)  # ty: ignore[invalid-assignment]
    try:
        result = await h.exec("echo hi", user="root", terminal=terminal)
        assert (result.value, result.route) == ("hi", "pooled-shell")
        assert h._connections.ssh_as.await_args.kwargs["user"] == "root"
        assert [asked.get("request_pty") for asked in own.asked] == [False]
    finally:
        await h.close()
        await own.reap()


# ---------------------------------------------------------------------------
# A stopped exec without a terminal: everything the session started is stopped
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(params=["openssh", "dropbear"])
async def piped_either_server(request, tmp_path):
    """:func:`piped`, on each kind of server: a signal reaches the group, or the shell alone."""
    host = _unix()
    conn = _EitherConnection(tmp_path, signals_group=request.param == "openssh")
    host._connections.ssh = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
    yield host
    await host.close()
    await conn.reap()


def _running(pid: int) -> bool:
    return process_state(pid) not in (None, "Z")


def _sleeping(seconds: int) -> list[int]:
    """The pids of every process whose command line is ``sleep <seconds>``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"sleep", str(seconds).encode()] and _running(int(entry.name)):
            found.append(int(entry.name))
    return found


async def _pid_of(path: Path) -> int:
    await wait_for_async(
        lambda: path.exists() and path.read_text().endswith("\n"),
        10.0,
        interval=0.01,
        on_timeout=f"{path.name} was never written",
    )
    return int(path.read_text())


@pytest.mark.parametrize("how", ["timeout", "cancel"])
@pytest.mark.asyncio
async def test_a_stopped_exec_without_a_terminal_stops_everything_its_session_started(
    piped_either_server, tmp_path, how
):
    """The stopped command and its tail, and an earlier exec's background process too.

    A process an earlier exec left in the background carries the session's
    token and is stopped with it, detached into a session of its own
    (``setsid``) or not. Only one started without the token survives.
    The stopped session is closed, and the next exec runs on another.
    """
    host = piped_either_server
    unmarked, marked, plain, started = (tmp_path / n for n in ("um", "mk", "pl", "st"))
    tail = tmp_path / "tail"
    duration = 10**6 + secrets.randbelow(10**6)
    try:
        first = await host.exec("echo $OTTO_SESSION")
        await host.exec(
            f"env -u OTTO_SESSION setsid sh -c 'echo $$ > {unmarked}; exec sleep 600' "
            f">/dev/null 2>&1 </dev/null &"
        )
        survivor = await _pid_of(unmarked)
        await host.exec(
            f"setsid sh -c 'echo $$ > {marked}; exec sleep 600' >/dev/null 2>&1 </dev/null &"
        )
        await host.exec(f"sh -c 'echo $$ > {plain}; exec sleep 600' >/dev/null 2>&1 </dev/null &")
        earlier = [await _pid_of(marked), await _pid_of(plain)]
        cmd = f"sh -c 'echo $$ > {started}; exec sleep {duration}'; touch {tail}"
        if how == "timeout":
            result = await host.exec(cmd, timeout=5)
            assert result.timed_out
            assert started.exists(), "the command never started within its budget"
        else:
            running = asyncio.ensure_future(host.exec(cmd, timeout=60))
            await _pid_of(started)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        stopped = int(started.read_text())
        for pid in (stopped, *earlier):
            await wait_for_async(
                lambda pid=pid: not _running(pid), 10.0, interval=0.01, on_timeout=f"{pid} runs"
            )
        assert _sleeping(duration) == []
        assert _running(survivor)
        nxt = await host.exec("echo $OTTO_SESSION")
        assert nxt.value != first.value
        mgr = host._session_mgr
        alive = [s for idle in mgr._exec_pools.values() for s in idle if s.alive]
        assert len(alive) == 1
        assert not tail.exists()
    finally:
        for path in (unmarked, marked, plain, started):
            with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
                os.kill(int(path.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_a_detached_launch_outlives_a_later_stop_of_its_session(
    piped_either_server, tmp_path
):
    """Otto's own detached launch (a daemon's fallback, a link check's listener) is not stopped.

    It clears the session mark before ``setsid``, so a later timed-out exec
    on the session that typed it, which stops everything the session
    started, does not reach it.
    """
    host = piped_either_server
    launched, started = tmp_path / "launched", tmp_path / "started"
    try:
        first = await host.exec(
            "echo $OTTO_SESSION; "
            + detached_command(f"sh -c 'echo $$ > {launched}; exec sleep 600'")
        )
        survivor = await _pid_of(launched)
        result = await host.exec(f"echo $OTTO_SESSION; echo $$ > {started}; sleep 600", timeout=2)
        assert result.timed_out
        # The stopped exec ran on the session that launched the survivor.
        assert result.value.splitlines()[1] == first.value.splitlines()[0]
        await wait_for_async(
            lambda: not _running(int(started.read_text())),
            10.0,
            interval=0.01,
            on_timeout="the timed-out exec's shell was not stopped",
        )
        assert _running(survivor)
    finally:
        with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
            os.kill(int(launched.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("nth", [1, 2, 3], ids=["handshake", "second-read", "third-read"])
@pytest.mark.parametrize("entry", ["exec", "run"])
@pytest.mark.asyncio
async def test_a_cancel_landing_as_a_session_read_completes_still_cancels_the_command(
    piped, monkeypatch, entry, nth
):
    """The cancel is requested in the loop turn the session's *nth* read completes.

    Only the ``handshake`` case is the lost-cancel turn: the opening
    handshake's read is itself the inner await of a bounded wait, where
    Python 3.10's ``wait_for`` returned the read and dropped the cancel, and
    the command was then typed after its caller had cancelled it. The
    ``second-read`` and ``third-read`` cases are reads inside the command's
    own task; they pin an ordinary cancel in the middle of a command.
    """
    host, _conn = piped
    duration = 10**6 + secrets.randbelow(10**6)
    reads = 0
    calling: "list[asyncio.Future[Any]]" = []
    real = SshSession.read_transport_until

    async def cancelling(self: SshSession, *, pattern: Any) -> str:
        nonlocal reads
        data = await real(self, pattern=pattern)
        reads += 1
        if reads == nth:
            calling[0].cancel()
        return data

    monkeypatch.setattr(SshSession, "read_transport_until", cancelling)
    cmd = f"echo started; sleep {duration} < /dev/null"
    never = float("inf")
    call = (
        host.exec(cmd, timeout=never)
        if entry == "exec"
        else host.run(cmd, timeout=never, terminal=False)
    )
    calling.append(asyncio.ensure_future(call))
    done, _ = await asyncio.wait(calling, timeout=10.0)
    if not done:
        calling[0].cancel()
        await asyncio.wait(calling, timeout=10.0)
    assert reads >= nth, f"only {reads} read(s) completed: the cancel was never requested"
    assert done, "the cancelled call went on waiting for its command"
    assert calling[0].cancelled()
    await wait_for_async(
        lambda: _sleeping(duration) == [],
        10.0,
        interval=0.01,
        on_timeout="the cancelled command is still running",
    )


@pytest.mark.asyncio
async def test_a_daemon_otto_launches_outlives_a_later_timed_out_exec_on_its_session(
    piped_either_server, tmp_path
):
    """The ``setsid`` fallback of :func:`launch_command` starts its daemon without the session mark.

    A later exec on the same pooled session times out, and its stop reaches
    every process started with the session's token; the daemon is not one of
    them. ``systemd-run`` is kept off the launching exec's ``PATH``, so the
    launch takes the fallback, which inherits the session shell's environment.
    """
    host = piped_either_server
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "sh", "setsid", "sleep"):
        found = shutil.which(tool)
        assert found, f"the fallback launch needs {tool} on this machine's PATH"
        (bin_dir / tool).symlink_to(found)
    daemon, started = tmp_path / "daemon", tmp_path / "started"
    launch = launch_command(
        "otto-test:v1:keep", ["sh", "-c", f"echo $$ > {daemon}; exec sleep 600"]
    )
    try:
        session_token = (await host.exec("echo $OTTO_SESSION")).value
        assert session_token
        await host.exec(f"PATH={bin_dir} {launch}")
        pid = await _pid_of(daemon)
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        assert not [e for e in environ if e.startswith(f"{SESSION_MARK}=".encode())]
        result = await host.exec(f"sh -c 'echo $$ > {started}; exec sleep 600'", timeout=5)
        assert result.timed_out
        stopped = await _pid_of(started)
        await wait_for_async(
            lambda: not _running(stopped), 10.0, interval=0.01, on_timeout=f"{stopped} runs"
        )
        assert _running(pid), "the timed-out exec's stop killed the daemon"
    finally:
        for path in (daemon, started):
            with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
                os.kill(int(path.read_text()), signal.SIGKILL)
