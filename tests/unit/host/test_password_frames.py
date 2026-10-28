"""No otto frame holds a raw password when the library or transport fails.

Traceback locals stay on in the CLI and in the logs, so any otto frame on a
failing stack renders its locals. A password must therefore stay a
``SecretStr`` in every otto frame (local, parameter, kwargs dict, attribute)
down to the final library or transport call, which unwraps it inside the
call expression. Each case plants a password, makes the library entry raise
right after receiving it, and scans every otto frame on the resulting
traceback (and its chained causes) for the raw value.

The scan walks only plain containers and otto dataclasses: calling ``repr``
on arbitrary objects would both run foreign code and miss the point, which
is what a frame's locals hold, not what they choose to print.
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import FrameType, SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aioftp
import asyncssh
import pytest
from pydantic import SecretBytes, SecretStr

import otto
from otto.host import interact
from otto.host import telnet as telnet_mod
from otto.host.connections import ConnectionManager, TelnetTarget
from otto.host.console import ConsoleClient
from otto.host.element import Element
from otto.host.login_proxy import LoginProxyError, run_proxy
from otto.host.options import ConsoleOptions
from otto.host.session import LocalSession, _SessionProxyIO
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.models.base import Cred
from tests.unit.host.test_console_client import ScriptedReader, ScriptedWriter

PLANTED = "PLANTED-SECRET-7f3a"
_OTTO_SRC = str(Path(otto.__file__).resolve().parent)
_MAX_DEPTH = 8


class _BoomError(RuntimeError):
    """What every fake library entry raises once it has received the password."""


def _holds(value: object, secret: str, seen: set[int], depth: int = 0) -> bool:
    """Whether *value* contains *secret*, walking containers and otto dataclasses only."""
    if isinstance(value, str):
        return secret in value
    if isinstance(value, (bytes, bytearray)):
        return secret.encode() in value
    if depth > _MAX_DEPTH or id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, dict):
        return any(
            _holds(k, secret, seen, depth + 1) or _holds(v, secret, seen, depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds(v, secret, seen, depth + 1) for v in value)
    if not isinstance(value, type) and type(value).__module__.startswith("otto."):
        return any(_holds(v, secret, seen, depth + 1) for v in _attributes(value))
    return False


def _attributes(obj: object) -> list[object]:
    """Every instance attribute of an otto object: ``__dict__`` and ``__slots__``, no repr.

    Read through ``object.__getattribute__``, so no property, ``__getattr__``
    or lazy export of the object's own runs.
    """
    values: list[object] = []
    with contextlib.suppress(AttributeError):
        values.extend(object.__getattribute__(obj, "__dict__").values())
    for klass in type(obj).__mro__:
        slots = klass.__dict__.get("__slots__", ())
        for name in [slots] if isinstance(slots, str) else slots:
            value = _slot(obj, name)
            if value is not _UNSET:
                values.append(value)
    return values


_UNSET = object()


def _slot(obj: object, name: str) -> object:
    """The value of slot *name* on *obj*, or ``_UNSET`` when the slot was never set."""
    try:
        return object.__getattribute__(obj, name)
    except AttributeError:
        return _UNSET


def _chain(exc: BaseException | None) -> list[BaseException]:
    """*exc* and every exception chained to it (cause or context), each once."""
    out: list[BaseException] = []
    while exc is not None and exc not in out:
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


def _frames(tb: TracebackType | None) -> list[FrameType]:
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    return frames


def _frames_holding(secret: str, exc: BaseException) -> list[str]:
    """``function:local`` for every otto frame on *exc*'s chain whose locals hold *secret*."""
    found: list[str] = []
    for e in _chain(exc):
        for frame in _frames(e.__traceback__):
            if not str(Path(frame.f_code.co_filename).resolve()).startswith(_OTTO_SRC):
                continue
            for name, value in dict(frame.f_locals).items():
                if _holds(value, secret, set()):
                    found.append(f"{frame.f_code.co_name}:{name}")
    return found


# ---------------------------------------------------------------------------
# Fake library and transport entries
# ---------------------------------------------------------------------------


class _Received:
    """What the fake library entry was handed, so a case cannot pass vacuously."""

    def __init__(self) -> None:
        self.values: list[object] = []

    def take(self, value: object) -> None:
        self.values.append(value)
        if _holds(value, PLANTED, set()):
            raise _BoomError("boom")

    @property
    def got_secret(self) -> bool:
        return any(_holds(v, PLANTED, set()) for v in self.values)


class _FakeStdin:
    """A subprocess stdin: records each write, raises on the one carrying the secret."""

    def __init__(self, received: _Received) -> None:
        self._received = received

    def write(self, data: bytes) -> None:
        self._received.take(data)

    async def drain(self) -> None:
        return None


def _local_session(received: _Received, output: bytes) -> LocalSession:
    """A ready LocalSession over a fake process whose stdout has *output* waiting."""
    stdout = asyncio.StreamReader()
    stdout.feed_data(output)
    session = LocalSession()
    session._process = SimpleNamespace(stdin=_FakeStdin(received), stdout=stdout)  # type: ignore[assignment]
    session._initialized = True
    session._alive = True
    return session


class _BoomErrorWriter(ScriptedWriter):
    def __init__(self, reader: ScriptedReader, received: _Received) -> None:
        super().__init__(reader)
        self._received = received

    def write(self, data: bytes) -> None:
        self._received.take(data)
        super().write(data)


class _TelnetWriter:
    def __init__(self, received: _Received) -> None:
        self._received = received
        self.transport = None

    def write(self, data: bytes) -> None:
        self._received.take(data)

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Drivers: one per path a password takes out of otto
# ---------------------------------------------------------------------------


def _mgr(creds: list[Cred], term: str, **kw: Any) -> ConnectionManager:
    return ConnectionManager(ip="10.0.0.1", creds=creds, term=term, name="h1", **kw)


async def _ssh(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    async def connect(*args: object, **kwargs: object) -> None:
        received.take(kwargs)

    monkeypatch.setattr(asyncssh, "connect", connect)
    await _mgr([Cred(login="admin", password=PLANTED)], "ssh").ssh()


async def _ssh_as(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    async def connect(*args: object, **kwargs: object) -> None:
        received.take(kwargs)

    monkeypatch.setattr(asyncssh, "connect", connect)
    creds = [Cred(login="admin", password="other"), Cred(login="postgres", password=PLANTED)]
    await _mgr(creds, "ssh").ssh_as("postgres")


async def _hop_tunnel(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    from otto.labs.loader import Lab

    async def connect(*args: object, **kwargs: object) -> None:
        received.take(kwargs)

    monkeypatch.setattr(asyncssh, "connect", connect)
    jumpbox = UnixHost(
        ip="10.10.0.1",
        element=Element("jumpbox"),
        creds=[Cred(login="admin", password=PLANTED)],
        log=LogMode.QUIET,
    )
    target = UnixHost(
        ip="10.10.0.2",
        element=Element("target"),
        creds=[Cred(login="user", password="other")],
        hop="jumpbox",
        log=LogMode.QUIET,
    )
    lab = Lab(name="password_frames")
    lab.add_host(jumpbox)
    lab.add_host(target)
    await target._connections._hop.get_tunnel()


async def _ftp_login(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    async def login(self: object, user: str, password: str, *a: object, **k: object) -> None:
        received.take(password)

    monkeypatch.setattr(aioftp.Client, "connect", AsyncMock(return_value=[]))
    monkeypatch.setattr(aioftp.Client, "login", login)
    await _mgr([Cred(login="admin", password=PLANTED)], "ftp").ftp()


async def _telnet_login(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"login:Password:")

    async def open_connection(*args: object, **kwargs: object) -> tuple[object, object]:
        return reader, _TelnetWriter(received)

    monkeypatch.setattr(telnet_mod, "open_telnet_connection", open_connection)
    monkeypatch.setattr(telnet_mod, "negotiate_no_echo", AsyncMock())
    await _mgr([Cred(login="admin", password=PLANTED)], "telnet").telnet()


async def _console_login(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    async def open_(self: ConsoleClient, interactive: bool = False) -> None:
        reader = ScriptedReader([(b"\r", b"box login: "), (b"admin\r", b"Password: ")])
        self.reader = reader
        self.writer = _BoomErrorWriter(reader, received)

    monkeypatch.setattr(ConsoleClient, "open", open_)
    opts = ConsoleOptions(
        server="srv",
        port=4001,
        login_prompt=r"login: ?$",
        password_prompt=r"[Pp]assword: ?$",
        login_timeout=0.3,
        settle=0.0,
        logout=False,
    )
    mgr = _mgr([Cred(login="admin", password=PLANTED)], "console", console_options=opts)
    monkeypatch.setattr(mgr, "console_target", AsyncMock(return_value=TelnetTarget("h", 1)))
    await mgr.console()


def _proxy_io(session: LocalSession) -> _SessionProxyIO:
    return _SessionProxyIO(session, lambda *_: None, lambda *_: None)


async def _su_proxy(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    session = _local_session(received, b"Password: ")
    hop = Cred(login="root", password=PLANTED, proxy="su")
    await run_proxy(_proxy_io(session), hop, via=Cred(login="admin"), host_id="h1")


async def _sudo_answer(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    class _Host(UnixHost):
        def _userland(self) -> None:  # no resolver: sudo, as on a fresh host
            return None

    host = _Host(
        ip="10.0.0.1",
        element=Element("box"),
        creds=[Cred(login="admin", password=PLANTED)],
        log=LogMode.QUIET,
    )
    cmd, expects = host._elevate("id")
    session = _local_session(received, b"otto-sudo:")
    await session.run_cmd(cmd, expects=expects, timeout=5)


async def _interact_write(monkeypatch: pytest.MonkeyPatch, received: _Received) -> None:
    proc = MagicMock()
    proc.stdin.write = received.take
    proc.stdout.read = AsyncMock(side_effect=[b"Password: ", b""])
    proc.returncode = None
    conn = MagicMock()
    conn.create_process = AsyncMock(return_value=proc)
    with patch.object(interact, "_print_stderr"):
        await interact.run_ssh_login(
            conn=conn,
            host_name="h1",
            proxy_hops=[Cred(login="root", password=PLANTED, proxy="su")],
            via_login="admin",
            host_id="h1",
        )


DRIVERS = {
    "ssh": _ssh,
    "ssh_as": _ssh_as,
    "hop_tunnel": _hop_tunnel,
    "ftp_login": _ftp_login,
    "telnet_login": _telnet_login,
    "console_login": _console_login,
    "su_proxy": _su_proxy,
    "sudo_answer": _sudo_answer,
    "interact_write": _interact_write,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(DRIVERS))
async def test_no_otto_frame_holds_the_raw_password(path, monkeypatch):
    received = _Received()
    with pytest.raises(BaseException) as ei:  # noqa: PT011 — the boom may arrive wrapped
        await DRIVERS[path](monkeypatch, received)
    assert received.got_secret, f"{path}: the password never reached the library entry"
    assert any(isinstance(e, _BoomError) for e in _chain(ei.value)), ei.value
    held = _frames_holding(PLANTED, ei.value)
    assert held == [], f"{path}: otto frames holding the raw password: {held}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["su_proxy", "sudo_answer"])
async def test_the_elevation_answer_is_the_password_and_one_newline(path, monkeypatch):
    """The answer is the cred's own value plus a newline, unwrapped at the write.

    Guards the mistake of wrapping a formatted string: ``SecretStr(f"{pw}\\n")``
    would wrap the MASK, and the device would be sent ``**********``.
    """
    received = _Received()
    with pytest.raises((_BoomError, LoginProxyError)):
        await DRIVERS[path](monkeypatch, received)
    assert received.values[-1] == b"PLANTED-SECRET-7f3a\n"


def test_the_probe_finds_a_planted_password():
    """The scan is only worth its green if it can go red."""

    def holder(password: str) -> None:
        kwargs = {"password": password}  # noqa: F841 — the local the scan must find
        raise _BoomError("boom")

    # A frame under src/otto is required; fake one by scanning the test frame directly.
    try:
        holder(PLANTED)
    except _BoomError as exc:
        frame = _frames(exc.__traceback__)[-1]
        assert _holds(dict(frame.f_locals), PLANTED, set())
        assert not _holds({"p": object()}, PLANTED, set())


def test_the_probe_finds_a_planted_password_in_a_real_otto_frame():
    """The same filter and scan the main test uses, through frames under ``src/otto``.

    ``SshOptions`` refuses a password in ``extra`` from inside otto, so the
    refusal's traceback runs through otto frames that hold the planted value.
    If the path filter ever dropped every frame, this goes red and the main
    test cannot pass vacuously.
    """
    from otto.host.options import SshOptions

    with pytest.raises(ValueError, match="password") as ei:
        SshOptions(extra={"password": PLANTED})
    held = _frames_holding(PLANTED, ei.value)
    assert "_refuse_shadowing:extra" in held, held


class _SlotHolder:
    """An otto-module object whose state lives only in a real ``__slots__`` slot."""

    __module__ = "otto._probe_fixture"
    __slots__ = ("held",)

    def __init__(self, held: str) -> None:
        self.held = held


class _DictHolder:
    """An otto-module object whose state lives only in its ``__dict__``."""

    __module__ = "otto._probe_fixture"

    def __init__(self, held: str) -> None:
        self.held = held


def _dict_only(obj: object) -> list[object]:
    """``_attributes`` with the slot walk removed: what a regression would look like."""
    try:
        return list(object.__getattribute__(obj, "__dict__").values())
    except AttributeError:
        return []


def test_the_probe_walks_slots_and_dict_of_otto_objects(monkeypatch):
    slotted, plain = _SlotHolder(PLANTED), _DictHolder(PLANTED)
    assert not hasattr(slotted, "__dict__"), "the slot case must have no __dict__ to fall back on"
    assert _holds(slotted, PLANTED, set())
    assert _holds(plain, PLANTED, set())
    # The slot half is load-bearing: a walk that skips slots misses the value.
    monkeypatch.setattr(sys.modules[__name__], "_attributes", _dict_only)
    assert not _holds(slotted, PLANTED, set())
    assert _holds(plain, PLANTED, set())


def test_the_probe_walks_otto_dataclass_attributes():
    from otto.host.telnet import TelnetClient

    client = TelnetClient(host="h", user="u", password="x")
    object.__setattr__(client, "password", PLANTED)  # what a raw attribute would look like
    assert _holds(client, PLANTED, set())


# ---------------------------------------------------------------------------
# Each transport writes a wrapped answer as its value and its own line end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ssh_session_writes_a_secret_as_its_value_and_a_newline():
    from otto.host.session import SshSession

    session = SshSession(conn=MagicMock())
    session._process = MagicMock()
    await session.write_transport(SecretStr(PLANTED))
    session._process.stdin.write.assert_called_once_with(PLANTED + "\n")


@pytest.mark.asyncio
async def test_a_telnet_session_writes_a_secret_once_with_a_cr_even_when_chunking():
    from otto.host.session import TelnetSession

    writer = MagicMock()
    session = TelnetSession(reader=MagicMock(), writer=writer, write_chunk_size=4)
    await session.write_transport(SecretStr(PLANTED))
    writer.write.assert_called_once_with(PLANTED.encode() + b"\r")


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r"])
async def test_a_bridge_hands_a_secret_to_write_remote_still_wrapped(newline):
    """The bridge's own write is the transport call; it receives ``SecretBytes``."""
    sent: list[object] = []

    async def write_remote(data: object) -> None:
        sent.append(data)

    bridge_io = interact._BridgeProxyIO(write_remote, AsyncMock(), newline)
    session = interact._BridgeShellSession(
        write_remote, AsyncMock(), newline=newline, command_frame=None
    )
    await bridge_io.send(SecretStr(PLANTED))
    await session.write_transport(SecretStr(PLANTED))
    assert all(isinstance(s, SecretBytes) for s in sent)
    assert [s.get_secret_value() for s in sent] == [PLANTED.encode() + newline] * 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_dry_run_refusal_names_the_mask_not_the_password():
    from otto.host.session import _NoTransport

    with pytest.raises(Exception) as ei:  # noqa: PT011 — the refusal type is the dry run's own
        await _NoTransport("h1").write_transport(SecretStr(PLANTED))
    assert PLANTED not in str(ei.value)


@pytest.mark.asyncio
async def test_the_key_passphrase_reaches_asyncssh_and_no_otto_frame(monkeypatch):
    """``SshOptions.passphrase`` is the one route for an encrypted key's passphrase.

    It reaches asyncssh's own ``passphrase`` kwarg, unwrapped only in the
    connect call, and no otto frame on the failing connect holds it.
    """
    from otto.host.options import SshOptions

    received = _Received()
    seen: dict[str, object] = {}

    async def connect(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        received.take(kwargs)

    monkeypatch.setattr(asyncssh, "connect", connect)
    mgr = _mgr(
        [Cred(login="admin")],
        "ssh",
        ssh_options=SshOptions(client_keys=["/k"], passphrase=PLANTED),
    )
    with pytest.raises(_BoomError) as ei:
        await mgr.ssh()
    assert seen["passphrase"] == PLANTED
    held = _frames_holding(PLANTED, ei.value)
    assert held == [], f"otto frames holding the raw passphrase: {held}"


def test_the_passphrase_never_shows_in_a_repr():
    from otto.host.options import SshOptions

    assert PLANTED not in repr(SshOptions(passphrase=PLANTED))
