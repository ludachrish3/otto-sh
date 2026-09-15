"""Dial tier: banners decide; direct and hop-side arms share one outcome type."""

import asyncio
import socket
import struct

import pytest

from otto.host.survey.dial import (
    HOP_DIAL_BASH,
    HOP_DIAL_NC,
    DialOutcome,
    _reader_timeout,
    classify_banner,
    dial_direct,
    dial_via_hop,
    hop_dial_script,
)
from otto.result import CommandResult
from otto.utils import Status


@pytest.mark.parametrize(
    ("data", "service"),
    [
        (b"SSH-2.0-OpenSSH_9.6\r\n", "ssh"),
        (b"220 (vsFTPd 3.0.5)\r\n", "ftp"),
        (b"\xff\xfd\x18\xff\xfd ", "telnet"),
        (b"HTTP/1.1 400\r\n", None),
        (b"", None),
    ],
)
def test_classify_banner(data, service):
    assert classify_banner(data) == service


async def _serve(banner: bytes | None, *, hold: bool = False):
    """A loopback server. With a banner: writes it, then hangs around briefly before closing.

    With ``banner=None``: holds the connection open FOREVER (blocks on an
    ``asyncio.Event`` that is never set) — a genuinely silent port, not one
    that merely closes fast. ``_close`` cancels the handler task at
    teardown; nothing here ever times out on its own.

    ``hold=True`` with a banner is the shape a REAL service has: it sends its
    greeting and then waits for the client, forever. That combination is what
    the hop script has to survive — a reader that only ever sees the bytes
    arrive, never EOF.
    """
    handlers: set[asyncio.Task] = set()

    async def handler(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            if banner is not None:
                writer.write(banner)
                await writer.drain()
                if hold:
                    await asyncio.Event().wait()
                else:
                    await asyncio.sleep(0.2)
            else:
                await asyncio.Event().wait()
        finally:
            writer.close()
            handlers.discard(task)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    server._test_handlers = handlers
    return server, server.sockets[0].getsockname()[1]


async def _close(server) -> None:
    """Close the server AND drain its still-sleeping handler task.

    A handler that is still mid ``asyncio.sleep(0.2)`` when the test's own
    connect+read already finished is a pending task pytest-asyncio's
    per-test loop would otherwise tear down uncancelled — leaking its
    socket to a bare ``__del__`` that turns into a hard error under this
    repo's ``filterwarnings = ["error"]``. Same shape as
    ``tests/unit/host/test_console_quiesce.py``'s fake server.
    """
    server.close()
    for task in list(server._test_handlers):
        task.cancel()
    if server._test_handlers:
        await asyncio.gather(*server._test_handlers, return_exceptions=True)
    await server.wait_closed()


@pytest.mark.asyncio
async def test_direct_dial_reads_and_classifies_the_banner():
    server, port = await _serve(b"SSH-2.0-test\r\n")
    try:
        out = await dial_direct("127.0.0.1", port, timeout=2.0)
    finally:
        await _close(server)
    assert out.state == "open"
    assert out.service == "ssh"
    assert out.banner.startswith("SSH-2.0-test")


@pytest.mark.asyncio
async def test_direct_dial_open_without_banner_is_open_and_unclassified():
    """Mutation: fold a silent open port into `timeout` and telnetd-without-IAC servers vanish.

    The server holds the connection open forever (see ``_serve``), so the
    read genuinely times out here — it does not hit EOF, which would leave
    the ``except (TimeoutError, ...)`` arm this test exists to guard
    unexercised (deleting that arm would still leave every test green).
    """
    server, port = await _serve(None)
    try:
        out = await dial_direct("127.0.0.1", port, timeout=0.1)
    finally:
        await _close(server)
    assert out == DialOutcome(
        state="open", service=None, banner="", detail="open, no banner within 0.1s"
    )


@pytest.mark.asyncio
async def test_direct_dial_eof_before_a_banner_says_the_peer_closed_it():
    """A clean EOF is not a timeout, and the detail must not claim one.

    accept-then-close is a real shape — tcpwrappers, a single-client console
    refusing the second client. Reporting ``no banner within 2s`` about a
    connection the peer dropped in a millisecond states an observation that
    was never made (mutation: drop the ``eof`` arm and this reads the timeout
    wording while the 2 s never elapsed).
    """

    async def handler(_reader, writer):
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        out = await dial_direct("127.0.0.1", port, timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
    assert out == DialOutcome(
        state="open", service=None, banner="", detail="open, closed by peer before any banner"
    )


@pytest.mark.asyncio
async def test_direct_dial_refused_is_closed():
    server, port = await _serve(None)
    server.close()
    await server.wait_closed()
    out = await dial_direct("127.0.0.1", port, timeout=2.0)
    assert out.state == "closed"


@pytest.mark.asyncio
async def test_direct_dial_timeout_is_timeout_not_closed(monkeypatch):
    """Mutation: catch TimeoutError as closed and a filtered port reads as dead."""

    async def never(*_a, **_k):
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio, "open_connection", never)
    out = await dial_direct("192.0.2.1", 23, timeout=0.05)
    assert out.state == "timeout"


@pytest.mark.asyncio
async def test_direct_dial_reset_mid_read_is_open_not_an_escaping_exception():
    """Mutation: catching only TimeoutError around the read lets a mid-read reset escape.

    accept-then-RST would otherwise raise out of ``dial_direct`` instead of
    returning a ``DialOutcome`` — and skip ``writer.wait_closed()`` on the
    way out, leaking the transport under ``filterwarnings=error``.
    """

    async def handler(reader, writer):
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        out = await dial_direct("127.0.0.1", port, timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
    assert out.state == "open"
    assert "reset" in out.detail


class _Userland:
    def __init__(self, dialect="bash", nc=True, timeout_style="coreutils"):
        self.shell_dialect = dialect
        self.timeout_style = timeout_style
        self._nc = nc

    def has_applet(self, name):
        assert name == "nc"
        return "present" if self._nc else "absent"


def _bash_script(t: int, ip: str, port: int) -> str:
    """The bash arm as :func:`hop_dial_script` renders it, for the expectation side."""
    return HOP_DIAL_BASH.format(t=t, r=_reader_timeout(t), n=256, ip=ip, port=port)


def test_hop_script_prefers_bash_dev_tcp_then_nc_z_then_none():
    """Mutation: pick nc before bash and every hop loses its banner."""
    bash_script = _bash_script(2, "192.0.2.1", 23)
    assert hop_dial_script("192.0.2.1", 23, timeout=2.0, userland=_Userland()) == bash_script
    ash = _Userland(dialect="ash", nc=True, timeout_style="dash-t")
    nc_script = HOP_DIAL_NC.format(prefix="timeout -t 2 ", t=2, ip="192.0.2.1", port=23)
    assert hop_dial_script("192.0.2.1", 23, timeout=2.0, userland=ash) == nc_script
    no_nc = _Userland(dialect="ash", nc=False)
    assert hop_dial_script("192.0.2.1", 23, timeout=2.0, userland=no_nc) is None
    assert hop_dial_script("192.0.2.1", 23, timeout=2.0, userland=None) == bash_script


class _HopRun:
    def __init__(self, text, retcode=0):
        self.text, self.retcode, self.cmds = text, retcode, []

    async def __call__(self, cmd):
        self.cmds.append(cmd)
        return CommandResult(
            status=Status.Success, value=self.text, command=cmd, retcode=self.retcode
        )


@pytest.mark.asyncio
async def test_hop_dial_decodes_the_hex_banner():
    hex_banner = " 53 53 48 2d 32 2e 30 2d 78 0d 0a\n"
    run = _HopRun("OTTO_STATE=open\n" + hex_banner + "OTTO_RC=0\n")
    out = await dial_via_hop(run, "test4", "192.0.2.1", 22, timeout=2.0, userland=_Userland())
    assert out.state == "open"
    assert out.service == "ssh"
    assert out.banner == "SSH-2.0-x\r\n"
    assert run.cmds == [_bash_script(2, "192.0.2.1", 22)]


@pytest.mark.asyncio
async def test_hop_dial_rc_124_is_timeout_and_refused_is_closed():
    """Mutation: treat every non-zero rc as closed and a Zephyr 3.7 dead port lies."""
    out = await dial_via_hop(
        _HopRun("OTTO_RC=124\n"), "test4", "192.0.2.1", 2323, timeout=0.5, userland=_Userland()
    )
    assert out.state == "timeout"
    refused = _HopRun("bash: connect: Connection refused\nOTTO_RC=1\n")
    out = await dial_via_hop(refused, "test4", "192.0.2.29", 22, timeout=0.5, userland=_Userland())
    assert out.state == "closed"


@pytest.mark.asyncio
async def test_hop_rc_marker_survives_a_cr_terminated_line():
    """Mutation: a `$`-anchored regex under MULTILINE misses a CRLF marker line.

    A CR-terminated hop reply would otherwise silently degrade to
    not-checkable instead of reading its real rc.
    """
    out = await dial_via_hop(
        _HopRun("OTTO_RC=124\r\n"), "test4", "192.0.2.1", 23, timeout=0.5, userland=_Userland()
    )
    assert out.state == "timeout"
    ash = _Userland(dialect="ash", nc=True)
    run = _HopRun("OTTO_RC=0\r\n")
    out = await dial_via_hop(run, "carrot", "198.51.100.1", 23, timeout=2.0, userland=ash)
    assert out.state == "open"


@pytest.mark.asyncio
async def test_hop_echoed_command_does_not_false_positive_as_open():
    """Mutation: a whole-text substring match on OTTO_STATE=open matches the echoed command too.

    The script itself contains the literal ``echo OTTO_STATE=open``; a
    vantage that echoes its command back would turn a refused dial into a
    false open — the worst error this tier can make.
    """
    echoed_cmd = _bash_script(2, "192.0.2.1", 23)
    text = echoed_cmd + "\nbash: connect: Connection refused\nOTTO_RC=1\n"
    run = _HopRun(text)
    out = await dial_via_hop(run, "test4", "192.0.2.1", 23, timeout=2.0, userland=_Userland())
    assert out.state == "closed"


@pytest.mark.asyncio
async def test_hop_hex_decode_ignores_stray_hex_words_in_unrelated_stderr():
    """Mutation: scavenging any 2-char hex word out of the merged stream fabricates banner bytes.

    A stray stderr token that happens to look like a hex pair must not
    contribute to the decoded banner.
    """
    text = "OTTO_STATE=open\nbash: ab cd: command not found\nOTTO_RC=0\n"
    run = _HopRun(text)
    out = await dial_via_hop(run, "test4", "192.0.2.1", 22, timeout=2.0, userland=_Userland())
    assert out.state == "open"
    assert out.banner == ""
    assert out.service is None


@pytest.mark.asyncio
async def test_hop_without_a_dial_tool_is_not_checkable_naming_the_hop():
    run = _HopRun("")
    no_nc = _Userland(dialect="ash", nc=False)
    out = await dial_via_hop(run, "carrot", "198.51.100.1", 23, timeout=2.0, userland=no_nc)
    assert out == DialOutcome(state="not-checkable", detail="hop carrot offers no dial tool")
    assert run.cmds == []


@pytest.mark.asyncio
async def test_hop_nc_z_open_has_no_banner_but_is_open():
    has_nc = _Userland(dialect="ash", nc=True)
    run = _HopRun("OTTO_RC=0\n")
    out = await dial_via_hop(run, "carrot", "198.51.100.1", 23, timeout=2.0, userland=has_nc)
    assert out.state == "open"
    assert out.service is None


@pytest.mark.asyncio
async def test_hop_wire_failure_is_not_checkable_with_the_reason():
    async def boom(_cmd):
        raise ConnectionError("hop session died")

    out = await dial_via_hop(boom, "test4", "192.0.2.1", 23, timeout=2.0, userland=_Userland())
    assert out.state == "not-checkable"
    assert "hop session died" in out.detail


_HELD_BANNER = b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.14\r\n"
"""43 bytes — the length a real sshd greeting has, and far short of ``head -c 256``."""

_PRE_FIX_HOP_DIAL_BASH = (
    "timeout {t} bash -c 'exec 3<>/dev/tcp/{ip}/{port} && echo OTTO_STATE=open && "
    "head -c 256 <&3 | od -An -v -tx1' 2>&1; echo OTTO_RC=$?"
)
"""The shape that shipped before the reader was given its own timeout.

Kept as the RED control of the test below, not as history: it is the only way
to demonstrate that the observation this tier makes about a live service
actually changed. ``head -c 256`` writes nothing until it has 256 bytes or
sees EOF, and coreutils ``timeout`` kills its whole process group — bash,
head AND od — so a service that sent 43 bytes and waited produced no hex at
all and read as ``open, no banner``.
"""


async def _bash_hop_run(cmd: str):
    """Run the hop script HERE, with bash, exactly as a hop session would run it.

    The point of the test is the script text itself, which no scripted
    ``run`` double can exercise: the defect it fixes lived entirely in how
    ``timeout``, ``head`` and ``od`` compose. Nothing is contacted but this
    process's own loopback server.
    """
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return CommandResult(
        status=Status.Success,
        value=out.decode("utf-8", "replace"),
        command=cmd,
        retcode=proc.returncode or 0,
    )


@pytest.mark.asyncio
async def test_the_real_hop_script_reads_a_banner_from_a_service_that_holds_the_port():
    """A held-open service's banner must reach the decoder — the whole of C2.

    ssh, ftp and telnet all send a short greeting and then WAIT for the
    client, so the reader never sees EOF on its own. The pre-fix control
    below is the red: same server, same bash, no banner.
    """
    server, port = await _serve(_HELD_BANNER, hold=True)
    assert len(_HELD_BANNER) == 43
    try:
        out = await dial_via_hop(
            _bash_hop_run, "local", "127.0.0.1", port, timeout=1.0, userland=_Userland()
        )
        pre_fix = await _bash_hop_run(_PRE_FIX_HOP_DIAL_BASH.format(t=1, ip="127.0.0.1", port=port))
    finally:
        await _close(server)
    assert out.state == "open", out
    assert out.service == "ssh", out
    assert out.banner.startswith("SSH-2.0-OpenSSH_9.6p1"), out
    # The red, reproduced in the same process against the same server: the
    # pre-fix script reported the port open and then died at its own timeout
    # with not one hex byte of the banner it had been holding since
    # millisecond one.
    assert "OTTO_STATE=open" in str(pre_fix.value), pre_fix.value
    assert "OTTO_RC=124" in str(pre_fix.value), pre_fix.value
    assert "53 53 48" not in str(pre_fix.value), (
        "the pre-fix script emitted hex; the red this test controls for is gone"
    )


@pytest.mark.asyncio
async def test_the_real_hop_script_calls_a_silent_held_open_port_open_with_no_banner():
    """Mutation: fold the empty read into `timeout` and a silent listener disappears."""
    server, port = await _serve(None)
    try:
        out = await dial_via_hop(
            _bash_hop_run, "local", "127.0.0.1", port, timeout=1.0, userland=_Userland()
        )
    finally:
        await _close(server)
    assert out.state == "open", out
    assert out.service is None, out
    assert out.banner == "", out
    assert out.detail == "open, no banner within 1s", out


@pytest.mark.asyncio
async def test_the_real_hop_script_reports_a_refused_port_as_closed():
    """The rc semantics the restructure had to preserve: no OTTO_STATE, and bash's own rc."""
    server, port = await _serve(None)
    server.close()
    await server.wait_closed()
    out = await dial_via_hop(
        _bash_hop_run, "local", "127.0.0.1", port, timeout=1.0, userland=_Userland()
    )
    assert out.state == "closed", out
