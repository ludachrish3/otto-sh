"""#1220: ``reboot``'s down phase counts a probe as down only when it shows the host gone.

During a held shutdown a guest's sshd is still running but answers slowly: a
probe can connect, receive the banner and then run out of its bound inside
the handshake. That probe shows a host that is still up. Counting it as down
let the up phase reach the old boot and report a reboot that had not
happened. The rule (``connections._open_may_be_up``) spares a failed probe
when the host's server answered (the ssh banner, a byte from a telnet
server), when its ssh connection to the host (a socket, or a jump's
channel; never a proxy pipe) was open when a timeout cut it, or when it
failed at the jump before the host's own open began. Everything else is
down.

The scripted tests drive the patchable dial seams (``ssh_connect``,
``open_telnet_connection``) playing the library's side of the contract. The
loopback tests at the end run the real asyncssh and telnetlib3, against a
local server and an in-process asyncssh jump host, so the coupling to each
library is pinned too. No loopback test races a wall-clock bound: where a
cut is the point, the test cuts the probe itself once the server has seen
the client get that far (``_cut_when``); elsewhere the probe ends on its own
inside :data:`PROBE_TIMEOUT_S`.
"""

import asyncio
import contextlib
import socket
import sys
from collections.abc import Callable
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock

import pytest
from typing_extensions import Self

import otto.host.connections as connections_mod
import otto.host.telnet as telnet_mod
from otto.host._asyncssh import asyncssh
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.host import PROBE_TIMEOUT_S, _ProbeAnswer
from otto.host.login_proxy import Cred
from otto.host.options import SshOptions, TelnetOptions
from otto.host.transport import _SshHopTransport
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import Result
from otto.utils import Status

_BANNER = "SSH-2.0-OpenSSH_9.6p1"
_BOUND = 0.05
"""The scripted tests' probe bound. Their fakes settle what the open saw
before their first await, so the bound only has to fire, never to win a race."""

_DOWN = _ProbeAnswer(reached=False)


def _may_be_up() -> _ProbeAnswer:
    """A failed probe that spares the host: built per test, so the module collects on its own."""
    return _ProbeAnswer(reached=False, may_be_up=True)


_EXPECTED = {"may-be-up": _may_be_up, "down": lambda: _DOWN}


def _unix_host(*, term: str = "ssh", hop: str | None = None) -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        valid_terms=["ssh", "telnet"],
        term=term,
        hop=hop,
    )


def _ssh_conn(*, banner: bool) -> MagicMock:
    """An asyncssh connection stand-in over a socket, its extra info holding the banner, or not."""
    conn = MagicMock()
    extra: dict[str, object] = {"socket": object()}
    if banner:
        extra["server_version"] = _BANNER
    conn.get_extra_info.side_effect = lambda name, default=None: extra.get(name, default)
    conn.wait_closed = AsyncMock()  # a logged-in one is torn down like a real connection
    return conn


_BANNERLESS = ("silent", "eof-before-banner")


class _SshDialer:
    """The ``ssh_connect`` seam, playing asyncssh's side of one dial per script entry.

    The last entry repeats. ``"no-transport"`` never opens a transport (a
    TCP connect or a jump's channel open that hangs); every other entry opens
    one first, which asyncssh signals by handing the connection to the
    client object's ``connection_made``. Then ``"silent"`` hangs before the
    banner, ``"slow-handshake"`` hangs after it, ``"eof-before-banner"`` is
    closed by the server before it, ``"banner-then-eof"`` after it,
    ``"banner-then-refused-login"`` refuses the login, and ``"old-boot"`` and
    ``"new-boot"`` log in. ``"refused"`` is refused outright. Each dial's
    watch (the ``on_open`` observer) is kept, to check what it still holds.
    """

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.dials: list[str] = []
        self.watches: list[object] = []

    async def __call__(self, host: str, *, password: object, **kwargs: object) -> object:
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.dials.append(step)
        factory = kwargs["client_factory"]
        assert isinstance(factory, connections_mod._WatchedClientFactory)
        self.watches.append(factory.on_open.__self__)  # ty: ignore[unresolved-attribute]
        if step == "refused":
            raise ConnectionRefusedError(111, "Connect call failed")
        if step == "no-transport":
            await asyncio.Event().wait()
        client = factory()
        conn = _ssh_conn(banner=step not in _BANNERLESS)
        client.connection_made(conn)
        if step in ("old-boot", "new-boot"):
            return conn
        if step in ("eof-before-banner", "banner-then-eof"):
            raise asyncssh.ConnectionLost("Connection lost")
        if step == "banner-then-refused-login":
            raise asyncssh.PermissionDenied("Permission denied")
        await asyncio.Event().wait()  # "silent" and "slow-handshake": the bound cuts it
        raise AssertionError("unreachable")


def _dialing(monkeypatch: pytest.MonkeyPatch, script: list[str]) -> _SshDialer:
    dialer = _SshDialer(script)
    monkeypatch.setattr(connections_mod, "ssh_connect", dialer)
    return dialer


class _Hop:
    """A hop transport stand-in. ``"up"``: the tunnel is up at once (asyncssh's ``tunnel``).

    ``"hangs"``: the jump's connect or login never finishes; ``"refused"``:
    the jump refuses the connection. Either way no tunnel is ever handed out.
    """

    def __init__(self, state: str = "up") -> None:
        self.state = state

    async def get_tunnel(self) -> object:
        if self.state == "refused":
            raise ConnectionRefusedError(111, "Connect call failed")
        if self.state == "hangs":
            await asyncio.Event().wait()
        return MagicMock()

    async def forward_port(self, dest_host: str, *, dest_port: int) -> int:
        await self.get_tunnel()
        return 40000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


def _hopping(monkeypatch: pytest.MonkeyPatch, *states: str) -> None:
    """Each hop transport a unix host builds is a :class:`_Hop` in the next of *states*.

    The last state repeats; none given is ``"up"``. A host builds one at
    construction, then one per fresh probe.
    """
    left = list(states) or ["up"]

    def build(self: UnixHost, hop_id: str | None = None) -> _Hop:
        return _Hop(left.pop(0) if len(left) > 1 else left[0])

    monkeypatch.setattr(UnixHost, "_build_hop_transport", build)


# -- one down probe ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hop", [None, "h"], ids=["direct", "hopped"])
async def test_a_handshake_cut_after_the_banner_spares_the_host(monkeypatch, hop):
    """The issue's probe: sshd answered, but the handshake ran past the bound."""
    _hopping(monkeypatch)
    host = _unix_host(hop=hop)
    _dialing(monkeypatch, ["slow-handshake"])
    answer = await host._probe_down(_BOUND)
    assert answer == _may_be_up()
    assert answer.went_down is False


@pytest.mark.asyncio
async def test_a_connect_cut_with_its_connection_open_spares_the_host(monkeypatch):
    """The TCP connect succeeded and the bound cut the open before the banner: still up."""
    host = _unix_host()
    _dialing(monkeypatch, ["silent"])
    assert await host._probe_down(_BOUND) == _may_be_up()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["banner-then-eof", "banner-then-refused-login"])
async def test_a_host_that_answered_and_then_failed_the_probe_is_up(monkeypatch, step):
    """No cut here: only the banner spares it (sshd killed mid-handshake, or a refused login)."""
    host = _unix_host()
    _dialing(monkeypatch, [step])
    assert await host._probe_down(PROBE_TIMEOUT_S) == _may_be_up()


@pytest.mark.asyncio
@pytest.mark.parametrize("hop", [None, "h"], ids=["direct", "hopped"])
async def test_a_connect_cut_before_any_transport_opened_is_down(monkeypatch, hop):
    """A TCP connect (or the jump's channel open) that never completes: the host is down."""
    _hopping(monkeypatch)
    host = _unix_host(hop=hop)
    _dialing(monkeypatch, ["no-transport"])
    answer = await host._probe_down(_BOUND)
    assert answer == _DOWN
    assert answer.went_down is True


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["refused", "eof-before-banner"])
async def test_a_refused_connect_or_one_closed_before_the_banner_is_down(monkeypatch, step):
    host = _unix_host()
    _dialing(monkeypatch, [step])
    answer = await host._probe_down(PROBE_TIMEOUT_S)
    assert answer == _DOWN
    assert answer.went_down is True


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["ssh", "telnet"])
@pytest.mark.parametrize("state", ["hangs", "refused"])
async def test_a_probe_that_failed_at_the_jump_learned_nothing_of_the_host(
    monkeypatch, term, state
):
    """The jump's own connect hung or was refused: the host's open never started."""
    _hopping(monkeypatch, state)
    host = _unix_host(term=term, hop="h")
    dialer = _dialing(monkeypatch, ["refused"])
    _telnet_opener(monkeypatch, answers=False)
    answer = await host._probe_down(_BOUND)
    assert answer == _ProbeAnswer(reached=False, may_be_up=True, at_jump=True)
    assert dialer.dials == []  # the host itself was never dialled


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["slow-handshake", "refused", "new-boot"])
async def test_the_watch_holds_no_connection_once_the_connect_ends(monkeypatch, step):
    """The connection's options hold the raw password: the watch drops it on every path."""
    host = _unix_host()
    dialer = _dialing(monkeypatch, [step])
    await host._probe_down(_BOUND)
    assert len(dialer.watches) == 1
    assert dialer.watches[0].conn is None  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_is_reachable_still_reads_a_slow_handshake_as_unreachable(monkeypatch):
    """Only the down phase spares a probe; one that did not finish is never reachable."""
    host = _unix_host()
    _dialing(monkeypatch, ["slow-handshake"])
    assert await host.is_reachable(timeout=_BOUND, fresh=True) is False


class _TelnetOpener:
    """The ``open_telnet_connection`` seam: optionally the server's first bytes, then a hang."""

    def __init__(self, *, answers: bool) -> None:
        self.answers = answers
        self.dials = 0

    async def __call__(
        self, host: str, *, single_client: bool = False, on_answer=None, **kwargs: object
    ) -> object:
        self.dials += 1
        if self.answers and on_answer is not None:
            on_answer()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _telnet_opener(monkeypatch: pytest.MonkeyPatch, *, answers: bool) -> _TelnetOpener:
    opener = _TelnetOpener(answers=answers)
    monkeypatch.setattr(telnet_mod, "open_telnet_connection", opener)
    return opener


@pytest.mark.asyncio
async def test_a_telnet_server_that_answered_spares_the_host(monkeypatch):
    host = _unix_host(term="telnet")
    opener = _telnet_opener(monkeypatch, answers=True)
    assert await host._probe_down(_BOUND) == _may_be_up()
    assert opener.dials == 1


@pytest.mark.asyncio
async def test_a_silent_telnet_open_is_down(monkeypatch):
    host = _unix_host(term="telnet")
    _telnet_opener(monkeypatch, answers=False)
    assert await host._probe_down(_BOUND) == _DOWN


@pytest.mark.asyncio
async def test_a_single_client_telnet_console_answering_says_nothing_of_the_device(monkeypatch):
    """An embedded target's telnet is a bridge to its one console: the bridge answers anyway."""
    host = ZephyrHost(ip="10.0.0.1", element=Element(name="box"), log=LogMode.QUIET)
    _telnet_opener(monkeypatch, answers=True)
    assert await host._probe_down(_BOUND) == _DOWN


# -- the down wait and the reboot -----------------------------------------------


@pytest.mark.asyncio
async def test_the_down_wait_keeps_polling_past_a_slow_handshake(monkeypatch):
    host = _unix_host()
    dialer = _dialing(monkeypatch, ["slow-handshake", "slow-handshake", "refused"])
    monkeypatch.setattr(UnixHost, "probe_bound", lambda self, timeout=PROBE_TIMEOUT_S: _BOUND)
    assert await host.wait_until_down(5.0, interval=0) is True
    assert dialer.dials == ["slow-handshake", "slow-handshake", "refused"]


@pytest.mark.asyncio
async def test_the_down_wait_ends_on_a_connect_that_opened_no_transport(monkeypatch):
    host = _unix_host()
    dialer = _dialing(monkeypatch, ["no-transport"])
    monkeypatch.setattr(UnixHost, "probe_bound", lambda self, timeout=PROBE_TIMEOUT_S: _BOUND)
    assert await host.wait_until_down(5.0, interval=0) is True
    assert dialer.dials == ["no-transport"]


def _rebooting(monkeypatch: pytest.MonkeyPatch, host: UnixHost) -> None:
    monkeypatch.setattr(host, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    monkeypatch.setattr(host, "_confirm_recovered", AsyncMock(return_value=True))
    monkeypatch.setattr(UnixHost, "probe_bound", lambda self, timeout=PROBE_TIMEOUT_S: _BOUND)


@pytest.mark.asyncio
async def test_a_slow_shutdown_is_reported_rebooted_only_after_it_went_down(monkeypatch):
    """The issue end to end: slow, then a login to the old boot, then down, then the new boot.

    The success must come from the new boot. Counting the first slow probe
    as down would send the up phase to the old boot's login instead.
    """
    host = _unix_host()
    _rebooting(monkeypatch, host)
    script = ["slow-handshake", "old-boot", "slow-handshake", "refused", "refused", "new-boot"]
    dialer = _dialing(monkeypatch, script)
    result = await host.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status is Status.Success, result.msg
    assert dialer.dials == script


@pytest.mark.asyncio
async def test_a_host_still_slow_at_the_down_timeout_never_went_down(monkeypatch):
    """A down phase that ends at ``down_timeout`` without seeing down keeps today's verdict."""
    host = _unix_host()
    _rebooting(monkeypatch, host)
    _dialing(monkeypatch, ["slow-handshake"])
    result = await host.reboot(wait=True, timeout=1.0, down_timeout=0.3, poll_interval=0)
    assert result.status is Status.Failed
    assert result.msg == (
        "'box' never went down within 0.3s of the reboot — the reboot likely did not take"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["hangs", "refused"])
async def test_a_down_phase_that_only_ever_failed_at_the_jump_names_the_jump(monkeypatch, state):
    """No poll reached for the host: the reboot's outcome is unknown, and the jump is why."""
    _hopping(monkeypatch, state)
    host = _unix_host(hop="h")
    _rebooting(monkeypatch, host)
    dialer = _dialing(monkeypatch, ["refused"])
    result = await host.reboot(wait=True, timeout=1.0, down_timeout=0.3, poll_interval=0)
    assert result.status is Status.Failed
    assert result.msg == (
        "'box' could not be probed within 0.3s of the reboot: every poll failed at its "
        "jump host 'h' (or a hop before it) — the reboot outcome is unknown"
    )
    assert dialer.dials == []


@pytest.mark.asyncio
async def test_a_down_phase_that_reached_the_host_once_keeps_todays_verdict(monkeypatch):
    """Jump failures mixed with a slow host: the host was seen up, so it did not go down."""
    # Hops built at construction and at the reboot's rebuild, then one per poll:
    # the first poll's jump is up (and finds the host slow), every later one refuses.
    _hopping(monkeypatch, "up", "up", "up", "refused")
    host = _unix_host(hop="h")
    _rebooting(monkeypatch, host)
    dialer = _dialing(monkeypatch, ["slow-handshake"])
    result = await host.reboot(wait=True, timeout=1.0, down_timeout=0.3, poll_interval=0)
    assert result.msg == (
        "'box' never went down within 0.3s of the reboot — the reboot likely did not take"
    )
    assert dialer.dials == ["slow-handshake"]  # only the first poll's jump reached the host


# -- the real libraries, against a loopback server --------------------------------


def _any_connection(_received: bytes) -> bool:
    return True


def _any_bytes(received: bytes) -> bool:
    return bool(received)


def _past_the_first_line(received: bytes) -> bool:
    """The client sent more than its version line: asyncssh sends that only once it read ours."""
    end = received.find(b"\n")
    return end >= 0 and len(received) > end + 1


def _answered_ttype(received: bytes) -> bool:
    """The client answered our ``IAC DO TTYPE``: it has read the bytes we sent."""
    return b"\xff\xfb\x18" in received or b"\xff\xfc\x18" in received


class _Peer:
    """A loopback server: greets each client with *greeting*, then holds or closes.

    ``greeting=None`` is a server that accepts and says nothing. *close*
    is ``"at-once"`` (before any greeting), ``"after-greeting"``, or ``None``
    to hold the connection until the client leaves. :attr:`seen` is set once
    *seen_when* holds for the bytes received from a client (checked at
    accept too). Every handler is cancelled and drained on exit, so no
    socket outlives the test (``filterwarnings = ["error"]``).
    """

    def __init__(
        self,
        greeting: bytes | None,
        *,
        close: str | None = None,
        seen_when: Callable[[bytes], bool] = _any_bytes,
    ) -> None:
        self.greeting = greeting
        self.close = close
        self.seen_when = seen_when
        self.seen = asyncio.Event()
        self.handlers: set[asyncio.Task[object]] = set()
        self.port = 0

    def _check(self, received: bytes) -> None:
        if self.seen_when(received):
            self.seen.set()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        received = b""
        try:
            self._check(received)
            if self.close == "at-once":
                return
            if self.greeting is not None:
                writer.write(self.greeting)
                await writer.drain()
            if self.close == "after-greeting":
                return
            while chunk := await reader.read(4096):
                received += chunk
                self._check(received)
        except OSError:
            pass
        finally:
            writer.close()
            self.handlers.discard(task)

    async def __aenter__(self) -> Self:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.server.close()
        for task in list(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        await self.server.wait_closed()


def _cut_when(monkeypatch: pytest.MonkeyPatch, seen: asyncio.Event) -> None:
    """Cut each probe of a unix host the moment *seen* is set, as its bound would cut it.

    ``wait_for``'s own cut, made on an event instead of a clock: the open is
    cancelled, then the probe fails with ``TimeoutError``. A probe that ends
    on its own first is left alone.
    """
    real = UnixHost._probe_with

    async def probe_with(self: UnixHost, conns: object) -> None:
        probe = asyncio.ensure_future(real(self, conns))  # ty: ignore[invalid-argument-type]
        waiter = asyncio.ensure_future(seen.wait())
        try:
            await asyncio.wait({probe, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if probe.done():
            return probe.result()
        probe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await probe
        raise TimeoutError("cut, as the probe's bound cuts it")

    monkeypatch.setattr(UnixHost, "_probe_with", probe_with)


@pytest.fixture
def _no_home_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep the user's ``~/.ssh`` (keys, config, CA bundle) and ssh agent out of the dials.

    asyncssh loads the default client keys for any ``client_keys`` but
    ``None``, an empty list included, and otto's options cannot pass
    ``None``; so ``HOME`` points at an empty directory instead.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)


def _quiet_ssh_options(port: int, **extra: object) -> SshOptions:
    """Options for a loopback dial that read no ssh config and ask no agent."""
    return SshOptions(port=port, extra={"config": [], "agent_path": None, **extra})


def _loopback_ssh_host(port: int, *, hop: str | None = None, **extra: object) -> UnixHost:
    return UnixHost(
        ip="127.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        ssh_options=_quiet_ssh_options(port, **extra),
        hop=hop,
    )


def _closed_port() -> int:
    """A loopback port nothing listens on: a connect to it is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_BANNER_LINE = (_BANNER + "\r\n").encode()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
@pytest.mark.parametrize(
    ("greeting", "seen_when"),
    [(_BANNER_LINE, _past_the_first_line), (None, _any_bytes)],
    ids=["banner-then-stall", "accepts-and-says-nothing"],
)
async def test_real_asyncssh_spares_a_host_whose_connect_was_cut_once_open(
    monkeypatch, greeting, seen_when
):
    async with _Peer(greeting, seen_when=seen_when) as peer:
        _cut_when(monkeypatch, peer.seen)
        host = _loopback_ssh_host(peer.port)
        assert await host._probe_down(PROBE_TIMEOUT_S) == _may_be_up()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
@pytest.mark.parametrize(
    ("greeting", "close", "expected"),
    [(_BANNER_LINE, "after-greeting", "may-be-up"), (None, "at-once", "down")],
    ids=["closes-after-the-banner", "closes-before-the-banner"],
)
async def test_real_asyncssh_reads_the_banner_after_the_connection_is_gone(
    greeting, close, expected
):
    """No cut: the server's close ends the connect, and only the banner tells the two apart."""
    async with _Peer(greeting, close=close) as peer:
        host = _loopback_ssh_host(peer.port)
        assert await host._probe_down(PROBE_TIMEOUT_S) == _EXPECTED[expected]()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_asyncssh_reads_a_refused_connect_as_down():
    host = _loopback_ssh_host(_closed_port())
    assert await host._probe_down(PROBE_TIMEOUT_S) == _DOWN


_PROXY = (
    "import socket, sys, time\n"
    "line = sys.stdin.buffer.readline()\n"
    "socket.create_connection(('127.0.0.1', int(sys.argv[1]))).sendall(line)\n"
    "time.sleep(60)\n"
)
"""A proxy command that never reaches a host: it reads the client's version
line, reports it to the test's server, and hangs."""


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_asyncssh_through_a_proxy_command_counts_only_the_banner(monkeypatch):
    """The proxy's pipe opens before the proxy reaches anything: an open pipe is not the host."""
    async with _Peer(None) as witness:
        _cut_when(monkeypatch, witness.seen)
        proxy = [sys.executable, "-c", _PROXY, str(witness.port)]
        host = _loopback_ssh_host(_closed_port(), proxy_command=proxy)
        assert await host._probe_down(PROBE_TIMEOUT_S) == _DOWN


class _JumpServer(asyncssh.SSHServer):
    """An in-process jump host: no login, and direct-tcpip opens as *mode* says.

    ``"forward"`` connects to the destination itself, as sshd does;
    ``"refuse"`` refuses the open; ``"hang"`` never confirms it until the
    test releases it (sets :attr:`requested` when asked).
    """

    def __init__(self, mode: str, requested: asyncio.Event, release: asyncio.Event) -> None:
        self.mode = mode
        self.requested = requested
        self.release = release

    def begin_auth(self, username: str) -> bool:
        return False

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> object:
        self.requested.set()
        if self.mode == "forward":
            return True
        if self.mode == "refuse":
            return False
        return self._held()

    async def _held(self) -> object:
        await self.release.wait()
        raise asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, "released")


class _Jump:
    """A loopback jump host every unix host in the test reaches its target through."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        self.monkeypatch = monkeypatch
        self.mode = mode
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    async def _dial(self) -> object:
        return await asyncssh.connect(
            "127.0.0.1",
            port=self.port,
            username="j",
            known_hosts=None,
            config=[],
            agent_path=None,
            client_keys=None,
        )

    async def __aenter__(self) -> Self:
        self.listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=lambda: _JumpServer(self.mode, self.requested, self.release),
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        )
        self.port = self.listener.get_port()
        self.monkeypatch.setattr(
            UnixHost,
            "_build_hop_transport",
            lambda host, hop_id=None: _SshHopTransport(self._dial),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release.set()
        self.listener.close()
        await self.listener.wait_closed()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_asyncssh_through_a_jump_whose_channel_never_opens_is_down(monkeypatch):
    """The bed's real down signature: the jump never confirms its channel to the host."""
    async with _Jump(monkeypatch, "hang") as jump:
        _cut_when(monkeypatch, jump.requested)
        host = _loopback_ssh_host(_closed_port(), hop="j")
        assert await host._probe_down(PROBE_TIMEOUT_S) == _DOWN


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_asyncssh_through_a_jump_that_refuses_the_channel_is_down(monkeypatch):
    async with _Jump(monkeypatch, "refuse"):
        host = _loopback_ssh_host(_closed_port(), hop="j")
        assert await host._probe_down(PROBE_TIMEOUT_S) == _DOWN


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_asyncssh_through_a_jump_spares_a_host_cut_once_its_channel_opened(
    monkeypatch,
):
    """The jump's channel to a silent host opened, then the cut: the host accepted it."""
    async with _Jump(monkeypatch, "forward"), _Peer(None) as peer:
        _cut_when(monkeypatch, peer.seen)
        host = _loopback_ssh_host(peer.port, hop="j")
        assert await host._probe_down(PROBE_TIMEOUT_S) == _may_be_up()


def _loopback_telnet_host(port: int, *, hop: str | None = None) -> UnixHost:
    return UnixHost(
        ip="127.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        valid_terms=["ssh", "telnet"],
        term="telnet",
        telnet_options=TelnetOptions(port=port),
        ssh_options=_quiet_ssh_options(22),
        hop=hop,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("greeting", "seen_when", "expected"),
    [(b"\xff\xfd\x18", _answered_ttype, "may-be-up"), (None, _any_connection, "down")],
    ids=["negotiates-then-stalls", "says-nothing"],
)
async def test_real_telnetlib3_reads_whether_the_server_answered(
    monkeypatch, greeting, seen_when, expected
):
    """A telnet server's first bytes (``IAC DO TTYPE``) prove it; an open socket alone does not."""
    async with _Peer(greeting, seen_when=seen_when) as peer:
        _cut_when(monkeypatch, peer.seen)
        host = _loopback_telnet_host(peer.port)
        assert await host._probe_down(PROBE_TIMEOUT_S) == _EXPECTED[expected]()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_home_ssh")
async def test_real_telnet_through_a_jump_forward_counts_only_a_byte(monkeypatch):
    """The hop's local forward accepted and its channel reached a silent host: still down.

    Why telnet ignores an open transport: the local socket is open (and the
    channel too) while the device has said nothing.
    """
    async with _Jump(monkeypatch, "forward"), _Peer(None, seen_when=_any_connection) as peer:
        _cut_when(monkeypatch, peer.seen)
        host = _loopback_telnet_host(peer.port, hop="j")
        assert await host._probe_down(PROBE_TIMEOUT_S) == _DOWN
