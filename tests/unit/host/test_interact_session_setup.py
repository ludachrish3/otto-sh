"""The login bridge runs the session-setup hook before the pumps start."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import interact
from otto.host.command_frame import BashFrame, RawFrame, ZephyrFrame
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.session_setup import SESSION_SETUPS, SessionSetup, register_session_setup
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests._fixtures.dialect_shell import DialectShell
from tests.unit.host.test_interact import _make_fake_asyncssh


class _Console:
    """write_remote/read_remote closures over a DialectShell.

    ``DialectShell.reply()`` answers only the LAST write, so a read issued
    when the console has nothing to say for that write is a SILENT console,
    not a closed one. It is spelled here as ``asyncio.TimeoutError`` (the
    fixture's own idiom, see ``DialectSession._read_until_pattern``) rather
    than as an empty chunk: an empty chunk is EOF, and the product treats
    EOF as the transport dying — which would kill the session under the
    login proxy's credential-prompt watch instead of letting it time out.
    Raising the timeout the read would have hit anyway is behaviourally the
    same for every caller and costs no wall clock.
    """

    def __init__(self, shell: DialectShell, *, trailer: str = "", trailer_after: str = "") -> None:
        self.shell = shell
        self.sent: list[bytes] = []
        # A real console keeps talking after the framed reply — it prints the
        # prompt the human is about to type at. *trailer* appends that to the
        # answer to any write containing *trailer_after*, which is how the
        # residual gets something specific to be.
        self.trailer = trailer
        self.trailer_after = trailer_after

    async def write_remote(self, data: bytes) -> None:
        self.sent.append(data)
        self.shell.wrote(data.decode())

    async def read_remote(self) -> bytes:
        reply = self.shell.reply()
        if reply is None:
            raise asyncio.TimeoutError("the console has nothing to say")
        if self.trailer and self.trailer_after in self.shell.writes[-1]:
            reply += self.trailer
        return reply.encode()


def _bridge_kwargs():
    """Patch _run_bridge and capture its kwargs; return (patch cm, holder)."""
    holder: dict = {}

    async def fake_bridge(**kw):
        holder.update(kw)

    return patch.object(interact, "_run_bridge", new=fake_bridge), holder


def _ssh_process(console: _Console) -> MagicMock:
    """An asyncssh process whose stdin and stdout are *console*'s two ends."""
    proc = MagicMock()

    def write(data: bytes) -> None:
        console.sent.append(data)
        console.shell.wrote(data.decode())

    proc.stdin.write = write
    proc.stdout.read = lambda _n: console.read_remote()
    proc.close = MagicMock()
    return proc


def _hooked_host(creds: list[Cred], setup_name: str) -> UnixHost:
    """A lab-shaped Unix host carrying a hook and the DEFAULT ``shell_history``."""
    return UnixHost(
        ip="10.0.0.1",
        element=Element("box"),
        creds=creds,
        log=LogMode.QUIET,
        session_setup=SessionSetup(name=setup_name),
    )


async def _login_over_a_console(host: UnixHost, console: _Console, *, login_target: str):
    """Drive ``UnixHost._login``'s ssh arm against *console*; return every write.

    The bridge is entered through the HOST, not through
    ``_run_session_setup_on_bridge`` directly, because the host's own
    ``shell_history`` field is the subject: a test that called the bridge
    helper could only make a claim about a parameter, and the absence of that
    parameter is the whole point.
    """
    conn = MagicMock()
    conn.create_process = AsyncMock(return_value=_ssh_process(console))

    async def ssh():
        return conn

    host._connections = SimpleNamespace(
        credentials=("admin", "pw"), login_target=login_target, ssh=ssh
    )
    fake_asyncssh = _make_fake_asyncssh()
    # The bridge session reaches session.py, which names the TOP-LEVEL
    # `asyncssh.ConnectionLost` in its except clauses; a MagicMock attribute
    # there is a TypeError, not a caught error.
    fake_asyncssh.ConnectionLost = fake_asyncssh.misc.ConnectionLost
    with (
        patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
        patch.object(interact, "_run_bridge", new=AsyncMock()),
        patch.object(interact, "_setup_raw_mode", return_value=None),
        patch.object(interact, "_restore_terminal"),
        patch.object(interact.sys, "stdin"),
    ):
        interact.sys.stdin.isatty = lambda: False
        interact.sys.stdin.fileno = lambda: 0
        await host._login()
    return [w.decode() for w in console.sent]


@pytest.fixture
def hook():
    calls = []

    async def fn(session, ctx):
        calls.append(ctx.kind)
        await session.run("export APP_ENV=lab")

    register_session_setup("t7-export", fn, overwrite=True)
    yield calls
    SESSION_SETUPS.unregister("t7-export")


@pytest.mark.asyncio
async def test_bridge_sequence_for_a_hook_host(hook):
    console = _Console(DialectShell())
    await interact._run_session_setup_on_bridge(
        write_remote=console.write_remote,
        read_remote=console.read_remote,
        newline=b"\n",
        host_name="h",
        host_id="h",
        setup=SessionSetup(name="t7-export"),
        landing_frame=None,
        target_frame=BashFrame(),
        creds=[Cred(login="admin", password="pw")],
        proxy_hops=[],
        via_login="admin",
        log_line=lambda _l: None,
    )
    writes = [w.decode() for w in console.sent]
    # -1 defaults on purpose: a step that never happened has to surface as a
    # failed comparison naming it, not as a StopIteration from the generator.
    i_ready1 = next((i for i, w in enumerate(writes) if "_READY__" in w), -1)
    i_restore_landing = next((i for i, w in enumerate(writes) if "stty echo" in w), -1)
    i_hook = next((i for i, w in enumerate(writes) if "export APP_ENV" in w), -1)
    i_ready2 = next((i for i, w in enumerate(writes) if "_READY__" in w and i > i_hook), -1)
    i_restore_target = next(
        (i for i, w in enumerate(writes) if "stty echo" in w and i > i_ready2), -1
    )
    assert i_ready1 < i_restore_landing
    assert i_restore_landing < i_hook
    assert i_hook < i_ready2
    assert i_ready2 < i_restore_target
    assert hook == ["bridge"]


@pytest.mark.asyncio
async def test_bridge_replays_hops_over_the_framed_session_before_the_hook(hook, capsys):
    console = _Console(DialectShell())
    hop = Cred(login="mysql", proxy="su", via="admin")
    await interact._run_session_setup_on_bridge(
        write_remote=console.write_remote,
        read_remote=console.read_remote,
        newline=b"\n",
        host_name="h",
        host_id="h",
        setup=SessionSetup(name="t7-export"),
        landing_frame=None,
        target_frame=BashFrame(),
        creds=[Cred(login="admin", password="pw"), hop],
        proxy_hops=[hop],
        via_login="admin",
        log_line=lambda _l: None,
    )
    writes = [w.decode() for w in console.sent]
    i_hook = next((i for i, w in enumerate(writes) if "export APP_ENV" in w), -1)
    assert writes.index("su - mysql\n") < i_hook
    # The hook path announces the hops the same way _replay_proxy_hops does —
    # the human is watching a terminal that goes quiet while su runs.
    assert "[otto] proxying to mysql..." in capsys.readouterr().err


@pytest.mark.asyncio
async def test_raw_landing_on_the_bridge_sends_nothing_before_the_hook():
    async def fn(session, ctx):
        await session.send("python3\n")

    register_session_setup("t7-raw", fn, overwrite=True)
    try:
        console = _Console(DialectShell())
        await interact._run_session_setup_on_bridge(
            write_remote=console.write_remote,
            read_remote=console.read_remote,
            newline=b"\n",
            host_name="h",
            host_id="h",
            setup=SessionSetup(name="t7-raw"),
            landing_frame=RawFrame(),
            target_frame=ZephyrFrame(),
            creds=[],
            proxy_hops=[],
            via_login="",
            log_line=lambda _l: None,
        )
        assert console.sent[0] == b"python3\n"
    finally:
        SESSION_SETUPS.unregister("t7-raw")


@pytest.mark.asyncio
async def test_telnet_bridge_translates_every_newline_of_a_multiline_payload():
    sent: list[bytes] = []

    async def write_remote(data: bytes) -> None:
        sent.append(data)

    async def read_remote() -> bytes:
        return b""

    s = interact._BridgeShellSession(
        write_remote, read_remote, newline=b"\r", command_frame=BashFrame()
    )
    await s._write("a\nb\r\nc\n")
    assert sent == [b"a\rb\rc\r"]


@pytest.mark.asyncio
async def test_run_ssh_login_without_a_hook_takes_todays_path(hook):
    """No session_setup -> _replay_proxy_hops runs and no bridge session is built."""
    proc = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.close = MagicMock()
    conn = MagicMock()
    conn.create_process = AsyncMock(return_value=proc)
    with (
        patch.dict(sys.modules, {"asyncssh": _make_fake_asyncssh()}),
        patch.object(interact, "_run_bridge", new=AsyncMock()),
        patch.object(interact, "_replay_proxy_hops", new=AsyncMock()) as replay,
        patch.object(interact, "_run_session_setup_on_bridge", new=AsyncMock()) as setup,
        patch.object(interact, "_setup_raw_mode", return_value=None),
        patch.object(interact, "_restore_terminal"),
        patch.object(interact.sys, "stdin"),
    ):
        interact.sys.stdin.isatty = lambda: False
        interact.sys.stdin.fileno = lambda: 0
        await interact.run_ssh_login(conn=conn, host_name="h")
    replay.assert_awaited_once()
    setup.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_ssh_login_with_a_hook_uses_the_bridge_session(hook):
    proc = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.close = MagicMock()
    conn = MagicMock()
    conn.create_process = AsyncMock(return_value=proc)
    residual = b"admin@lab:~$ "
    bridge_cm, bridge_kw = _bridge_kwargs()
    with (
        patch.dict(sys.modules, {"asyncssh": _make_fake_asyncssh()}),
        bridge_cm,
        patch.object(interact, "_replay_proxy_hops", new=AsyncMock()) as replay,
        patch.object(
            interact, "_run_session_setup_on_bridge", new=AsyncMock(return_value=residual)
        ) as setup,
        patch.object(interact, "_setup_raw_mode", return_value=None),
        patch.object(interact, "_restore_terminal"),
        patch.object(interact.sys, "stdin"),
    ):
        interact.sys.stdin.isatty = lambda: False
        interact.sys.stdin.fileno = lambda: 0
        await interact.run_ssh_login(
            conn=conn,
            host_name="h",
            session_setup=SessionSetup(name="t7-export"),
            target_frame=BashFrame(),
            creds=[],
        )
    replay.assert_not_awaited()
    setup.assert_awaited_once()
    # The residual is not merely produced: it reaches the bridge as the prelude,
    # which is the only way the human ever sees the prompt they are sitting at.
    assert bridge_kw["prelude"] == residual


@pytest.mark.asyncio
async def test_the_prelude_reaches_both_the_terminal_and_the_session_log():
    """Residual bytes the pumps never saw are still shown and still logged."""
    lines: list[str] = []

    async def write_remote(data: bytes) -> None:
        return None

    async def read_remote() -> bytes:
        return b""

    with (
        patch.object(interact, "_setup_raw_mode", return_value=None),
        patch.object(interact, "_restore_terminal"),
        patch.object(interact.sys, "stdin"),
        patch.object(interact.os, "write") as os_write,
    ):
        interact.sys.stdin.isatty = lambda: False
        interact.sys.stdin.fileno = lambda: 0
        await interact._run_bridge(
            write_remote=write_remote,
            read_remote=read_remote,
            install_sigwinch=lambda: lambda: None,
            on_output_line=lines.append,
            prelude=b"admin@lab:~$ ",
        )
    assert lines == ["admin@lab:~$ "]
    assert os_write.call_args_list[0].args == (1, b"admin@lab:~$ ")


@pytest.mark.asyncio
async def test_the_hook_sees_the_identity_the_hops_left_the_bridge_in():
    """current_user is seeded and stamped, so as_user() does not re-hop to via."""
    seen: list[str] = []

    async def fn(session, ctx):
        seen.append(session.current_user)

    register_session_setup("t7-identity", fn, overwrite=True)
    try:
        console = _Console(DialectShell())
        hop = Cred(login="mysql", proxy="su", via="admin")
        await interact._run_session_setup_on_bridge(
            write_remote=console.write_remote,
            read_remote=console.read_remote,
            newline=b"\n",
            host_name="h",
            host_id="h",
            setup=SessionSetup(name="t7-identity"),
            landing_frame=None,
            target_frame=BashFrame(),
            creds=[Cred(login="admin", password="pw"), hop],
            proxy_hops=[hop],
            via_login="admin",
            log_line=lambda _l: None,
        )
        assert seen == ["mysql"]
    finally:
        SESSION_SETUPS.unregister("t7-identity")


@pytest.mark.asyncio
async def test_the_bridge_never_quiets_history_whatever_the_host_field_says(hook):
    """``otto login`` hands a human a shell whose up-arrow recall still works.

    The host here carries the DEFAULT ``shell_history`` (suppression on), a
    login-proxy hop and a hook, so every stage that could carry the payload
    runs: the landing handshake, the hop's resync probe, the hook's own framed
    command and the target handshake. None of them may carry a byte of it —
    ``set +o history`` would cost the human the recall of everything they type
    afterwards, which is the trade the shell-history docs refuse to make for
    ``login``.
    """
    console = _Console(DialectShell())
    hop = Cred(login="mysql", proxy="su", via="admin")
    host = _hooked_host([Cred(login="admin", password="pw"), hop], "t7-export")
    assert host.shell_history is False  # the default, and the field under test
    writes = await _login_over_a_console(host, console, login_target="mysql")

    readies = [w for w in writes if "_READY__" in w]
    assert len(readies) == 2, writes  # landing handshake + frame entry
    assert "su - mysql\n" in writes
    probes = [w for w in writes if "$(id -un)" in w]
    assert probes, writes
    assert any("export APP_ENV" in w for w in writes), writes

    stream = "".join(writes)
    assert "HISTFILE" not in stream
    assert "set +o history" not in stream


@pytest.mark.asyncio
async def test_the_default_bridge_leaves_shell_history_alone(hook):
    console = _Console(DialectShell())
    await interact._run_session_setup_on_bridge(
        write_remote=console.write_remote,
        read_remote=console.read_remote,
        newline=b"\n",
        host_name="h",
        host_id="h",
        setup=SessionSetup(name="t7-export"),
        landing_frame=None,
        target_frame=BashFrame(),
        creds=[],
        proxy_hops=[],
        via_login="admin",
        log_line=lambda _l: None,
    )
    assert BashFrame().quiet_history() not in console.sent[0].decode()


@pytest.mark.asyncio
async def test_the_residual_is_what_the_console_said_after_the_last_framed_match(hook):
    """The bytes past the final END sentinel are returned, not dropped."""
    console = _Console(DialectShell(), trailer="admin@lab:~$ ", trailer_after="stty echo")
    residual = await interact._run_session_setup_on_bridge(
        write_remote=console.write_remote,
        read_remote=console.read_remote,
        newline=b"\n",
        host_name="h",
        host_id="h",
        setup=SessionSetup(name="t7-export"),
        landing_frame=None,
        target_frame=BashFrame(),
        creds=[],
        proxy_hops=[],
        via_login="admin",
        log_line=lambda _l: None,
    )
    # The newline is the one that terminated the END sentinel's line — the human
    # is on a fresh line at the prompt, which is exactly what they should see.
    assert residual == b"\nadmin@lab:~$ "


@pytest.mark.asyncio
async def test_a_hooks_own_switch_user_is_not_quieted_on_the_bridge():
    """The handle carries no prefix, so an su the HOOK performs is unquieted too.

    The same promise one layer in: the hook's session handle is built with an
    empty history prefix, so a ``switch_user`` the hook performs leaves the
    elevated shell's history alone as well.
    """

    async def fn(session, ctx):
        await session.switch_user("mysql")

    register_session_setup("t7-switch", fn, overwrite=True)
    try:
        console = _Console(DialectShell())
        host = _hooked_host(
            [Cred(login="admin", password="pw"), Cred(login="mysql", proxy="su", via="admin")],
            "t7-switch",
        )
        assert host.shell_history is False
        writes = await _login_over_a_console(host, console, login_target="admin")
        probes = [w for w in writes if "$(id -un)" in w]
        assert probes, writes
        assert not any("HISTFILE" in p for p in probes), probes
    finally:
        SESSION_SETUPS.unregister("t7-switch")
