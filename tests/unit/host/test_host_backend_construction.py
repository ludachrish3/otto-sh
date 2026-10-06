"""UnixHost / EmbeddedHost build their backends through the registry + create (WS#4)."""

import dataclasses

import pytest

from otto.host import connections as conn_mod
from otto.host import transfer as xfer_mod
from otto.host.connections import ConnectionManager, TermContext, register_term_backend
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.transfer import (
    NcFileTransfer,
    ScpFileTransfer,
    SftpFileTransfer,
    register_transfer_backend,
)
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode


def _replace_term_backend(name: str, cls: type[ConnectionManager]) -> None:
    """Register *cls* over the term *name*, keeping that entry's declarations.

    Overwrites a built-in. The root conftest's ``_isolate_registries`` restores
    the original after the test (this module defines no fixture of that name, so the
    root one applies).
    """
    current = conn_mod.TERM_BACKENDS.get(name)
    register_term_backend(
        name,
        cls,
        host_families=current.host_families,
        authenticates=current.authenticates,
        dials_host=current.dials_host,
        overwrite=True,
    )


def _record_term_backend(name: str) -> "list[tuple[TermContext, ConnectionManager]]":
    """Register a recording replacement over *name*; return its ``(ctx, built)`` log."""
    calls: list[tuple[TermContext, ConnectionManager]] = []

    class _Recording(ConnectionManager):
        @classmethod
        def create(cls, ctx: TermContext) -> ConnectionManager:
            built = super().create(ctx)
            calls.append((ctx, built))
            return built

    _replace_term_backend(name, _Recording)
    return calls


class _OfflineConnections(ConnectionManager):
    """ConnectionManager double that yields a session without touching the network."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._ssh_conn = object()
        self._sftp_conn = None
        self._ftp_conn = None
        self._telnet_conn = None
        self._name = kwargs.get("name", "fake")
        self._term = kwargs.get("term", "ssh")
        self._hop = None

    async def ssh(self):
        return self._ssh_conn


def _unix_host(**kwargs):
    _replace_term_backend("ssh", _OfflineConnections)
    return UnixHost(
        ip="10.0.0.9",
        creds=[Cred(login="root", password="x")],
        element=Element("ne"),
        term="ssh",
        **kwargs,
    )


class TestShellHistoryReachesTheShell:
    """UnixHost.shell_history must survive all the way to the bytes written."""

    @staticmethod
    async def _first_line(**kwargs) -> str:
        host = _unix_host(**kwargs)
        session = await host._session_mgr._build_session()
        return session._handshake_payload(session._markers)

    @pytest.mark.asyncio
    async def test_default_unix_host_suppresses_history(self):
        # The product decision: otto should not bury a human's own history.
        assert "HISTFILE=/dev/null" in await self._first_line()

    @pytest.mark.asyncio
    async def test_opting_in_leaves_the_shell_untouched(self):
        assert "HISTFILE" not in await self._first_line(shell_history=True)

    @pytest.mark.asyncio
    async def test_opting_in_still_silences_echo(self):
        # Opting into history must not cost the readiness handshake anything.
        assert "stty -echo" in await self._first_line(shell_history=True)

    def test_field_defaults_to_suppressed(self):
        assert _unix_host().shell_history is False


def test_unix_host_builds_registered_transfer_backend():
    """A custom transfer backend registered at runtime is the one the host builds."""
    built = {}

    class RecordingTransfer(NcFileTransfer):
        host_families = frozenset({"unix"})

        @classmethod
        def create(cls, ctx):
            built["name"] = ctx.transfer
            return super().create(ctx)

    xfer_mod.TRANSFER_BACKENDS.register("recording", RecordingTransfer)

    h = UnixHost(
        ip="10.0.0.9",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        transfer="recording",
        valid_transfers=["recording"],
    )
    assert isinstance(h._file_transfer, RecordingTransfer)
    assert built["name"] == "recording"


# ---------------------------------------------------------------------------
# #601: an embedded host builds its connections through the term registry,
# as a unix host does, so a replacement registered over `telnet` or `console`
# reaches it.
# ---------------------------------------------------------------------------


def _zephyr(term: str) -> ZephyrHost:
    if term == "console":
        return ZephyrHost(
            ip="192.0.2.1",
            creds=[Cred(login="root", password="x")],
            element=Element("z"),
            valid_terms=["console"],
            term="console",
            console_options=ConsoleOptions(server="test4", port=2323, login=True),
            log=LogMode.QUIET,
        )
    return ZephyrHost(
        ip="192.0.2.1",
        creds=[Cred(login="root", password="x")],
        element=Element("z"),
        telnet_options=TelnetOptions(port=2323, login=True),
        log=LogMode.QUIET,
    )


def _assert_embedded_context(ctx: TermContext, host: ZephyrHost) -> None:
    """The context an embedded host hands its term backend: identity plus forced options."""
    assert isinstance(ctx, TermContext)
    assert (ctx.ip, ctx.term, ctx.name) == (host.ip, host.term, host.name)
    assert ctx.creds == host.creds
    assert ctx.hop is None  # no hop in these hosts
    # The unix-only transports stay unset on an embedded host.
    assert (ctx.ssh_options, ctx.sftp_options, ctx.ftp_options) == (None, None, None)
    # An RTOS shell has no login step, and its one console serves one client.
    assert ctx.telnet_options is not None
    assert ctx.telnet_options.port == host.telnet_options.port
    assert ctx.telnet_options.login is False
    assert ctx.telnet_options.single_client_console is True
    assert ctx.console_options is not None
    assert ctx.console_options.server == host.console_options.server
    assert ctx.console_options.login is False
    if host.term == "console":
        # The bound method, uncalled: the server is looked up on the first dial.
        assert callable(ctx.console_endpoint)
        assert ctx.console_endpoint == host.console_endpoint
    else:
        assert ctx.console_endpoint is None


@pytest.mark.parametrize("term", ["telnet", "console"])
def test_embedded_host_builds_through_the_registered_term_backend(term):
    """#601: a replacement registered over a built-in term reaches embedded hosts.

    Its ``create`` runs once per construction -- the build and each rebuild --
    with a ``TermContext`` carrying the embedded options, and what it returns
    becomes the host's connections (and its session manager's).
    """
    calls = _record_term_backend(term)
    host = _zephyr(term)
    assert len(calls) == 1, f"the embedded host bypassed the registered {term!r} backend"
    ctx, built = calls[0]
    _assert_embedded_context(ctx, host)
    assert host._connections is built
    assert host._session_mgr._connections is built

    host.rebuild_connections()
    assert len(calls) == 2
    ctx, rebuilt = calls[1]
    _assert_embedded_context(ctx, host)
    assert rebuilt is not built
    assert host._connections is rebuilt
    assert host._session_mgr._connections is rebuilt


def test_an_override_copy_of_an_embedded_host_builds_through_the_registry():
    """``dataclasses.replace`` (the fleet-override and survey seam) re-runs
    ``__post_init__``; the copy must build through the registered backend too."""
    calls = _record_term_backend("telnet")
    host = _zephyr("telnet")
    copy = dataclasses.replace(host, telnet_options=TelnetOptions(port=24, login=True))
    assert len(calls) == 2
    ctx, built = calls[1]
    assert ctx.telnet_options.port == 24
    assert ctx.telnet_options.login is False
    assert copy._connections is built


# ---------------------------------------------------------------------------
# Switching a host's active protocol goes through the override-copy seam
# (dataclasses.replace -> __post_init__), which rebuilds the backend via the
# registry create() seam so a *custom* backend swap instantiates the right
# CLASS. The menu (valid_transfers/valid_terms) is enforced: the target must
# be listed in the host's menu, and the copy is insulated from the original.
# ---------------------------------------------------------------------------


class XmodemTransfer(NcFileTransfer):
    """A distinct unix transfer backend class for the rebuild tests."""

    host_families = frozenset({"unix"})


def test_transfer_override_rebuilds_to_custom_backend():
    register_transfer_backend("xmodem", XmodemTransfer)
    # xmodem must be in the menu to be selectable
    h = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        valid_transfers=["scp", "xmodem"],
        transfer="scp",
    )
    assert type(h._file_transfer) is ScpFileTransfer  # built-in to start

    switched = dataclasses.replace(h, transfer="xmodem")

    # Rebuilt to the custom CLASS on the copy — not a string swap.
    assert isinstance(switched._file_transfer, XmodemTransfer)
    assert switched.transfer == "xmodem"
    # original is untouched (insulation)  # noqa: ERA001 — prose assertion label, not code
    assert h.transfer == "scp"
    assert type(h._file_transfer) is ScpFileTransfer


def test_transfer_override_switches_among_builtins():
    h = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        transfer="scp",
    )
    switched = dataclasses.replace(h, transfer="sftp")
    assert type(switched._file_transfer) is SftpFileTransfer
    assert switched.transfer == "sftp"


def test_override_copy_has_its_own_connection():
    h = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        transfer="scp",
    )
    switched = dataclasses.replace(h, transfer="sftp")
    # The override copy is insulated: it builds its own connection rather than
    # sharing the original's live one.
    assert switched._connections is not h._connections


def test_term_override_switches_builtin():
    h = UnixHost(
        ip="10.0.0.1", creds=[Cred(login="root", password="x")], element=Element("e"), term="ssh"
    )
    switched = dataclasses.replace(h, term="telnet")
    assert switched.term == "telnet"
    assert switched._connections.term == "telnet"
    assert h.term == "ssh"  # original untouched


def test_transfer_override_rejects_out_of_menu_backend():
    h = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        transfer="scp",
    )
    # console is not in the unix default menu -> validate_choice fails loud
    with pytest.raises(ValueError, match="transfer menu"):
        dataclasses.replace(h, transfer="console")
    assert h.transfer == "scp"  # original unchanged


def test_term_builtins_are_restored_between_tests():
    """The tests above overwrite built-ins; the root conftest restores them."""
    for name in ["ssh", "telnet", "console"]:
        assert conn_mod.build_term_backend(name) is ConnectionManager
