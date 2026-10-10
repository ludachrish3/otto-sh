"""Shared host and backend doubles for the term and transfer construction tests.

A plain module, not a test file: the host construction tests and the seam's
replacement differential both build hosts over replaced backends.
"""

from otto.host import connections as conn_mod
from otto.host.connections import ConnectionManager, TermContext, register_term_backend
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode


def _replace_term_backend(name: str, cls: type[ConnectionManager]) -> None:
    """Register *cls* over the term *name*, keeping that entry's declarations.

    Overwrites a built-in. The root conftest's ``_isolate_registries`` restores
    the original after the test.
    """
    current = conn_mod.TERM_BACKENDS.peek(name).metadata
    register_term_backend(
        name,
        cls,
        host_families=current.host_families,
        authenticates=current.authenticates,
        dials_host=current.dials_host,
        overwrite=True,
    )


def _record_term_backend(
    name: str, base: type[ConnectionManager] = ConnectionManager
) -> "list[tuple[TermContext, ConnectionManager]]":
    """Register a recording subclass of *base* over *name*; return its ``(ctx, built)`` log."""
    calls: list[tuple[TermContext, ConnectionManager]] = []

    class _Recording(base):
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
        self._console_conn = None
        self._name = kwargs.get("name", "fake")
        self._term = kwargs.get("term", "ssh")
        self._hop = None

    async def ssh(self):
        return self._ssh_conn


def _unix_host(**kwargs):
    """A unix host whose ``ssh`` term is the offline double."""
    _replace_term_backend("ssh", _OfflineConnections)
    return UnixHost(
        ip="10.0.0.9",
        creds=[Cred(login="root", password="x")],
        element=Element("ne"),
        term="ssh",
        **kwargs,
    )


def _zephyr(term: str, **kwargs) -> ZephyrHost:
    """An embedded host on *term* (``telnet`` or ``console``); *kwargs* add fields."""
    if term == "console":
        return ZephyrHost(
            ip="192.0.2.1",
            creds=[Cred(login="root", password="x")],
            element=Element("z"),
            valid_terms=["console"],
            term="console",
            console_options=ConsoleOptions(server="test4", port=2323, login=True),
            log=LogMode.QUIET,
            **kwargs,
        )
    return ZephyrHost(
        ip="192.0.2.1",
        creds=[Cred(login="root", password="x")],
        element=Element("z"),
        telnet_options=TelnetOptions(port=2323, login=True),
        log=LogMode.QUIET,
        **kwargs,
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
