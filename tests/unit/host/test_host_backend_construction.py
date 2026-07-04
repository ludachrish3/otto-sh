"""UnixHost / EmbeddedHost build their backends through the registry + create (WS#4)."""

import dataclasses

import pytest

from otto.host import connections as conn_mod
from otto.host import transfer as xfer_mod
from otto.host.connections import ConnectionManager
from otto.host.login_proxy import Cred
from otto.host.transfer import (
    NcFileTransfer,
    ScpFileTransfer,
    SftpFileTransfer,
    register_transfer_backend,
)
from otto.host.unix_host import UnixHost


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Unregister any test-added term/transfer backend after each test."""
    before_t = set(conn_mod.TERM_BACKENDS.names())
    before_x = set(xfer_mod.TRANSFER_BACKENDS.names())
    try:
        yield
    finally:
        for name in set(conn_mod.TERM_BACKENDS.names()) - before_t:
            conn_mod.TERM_BACKENDS.unregister(name)
        for name in set(xfer_mod.TRANSFER_BACKENDS.names()) - before_x:
            xfer_mod.TRANSFER_BACKENDS.unregister(name)


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
        element="e",
        transfer="recording",
        valid_transfers=["recording"],
    )
    assert isinstance(h._file_transfer, RecordingTransfer)
    assert built["name"] == "recording"


def test_connection_factory_override_still_wins():
    """A _connection_factory test double is still used in place of the registry."""

    class FakeConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):
            self._name = kwargs.get("name", "fake")
            self._term = kwargs.get("term", "ssh")
            self._hop = None

    h = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element="e",
        term="ssh",
        _connection_factory=FakeConnections,
    )
    assert isinstance(h._connections, FakeConnections)


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
        element="e",
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
        ip="10.0.0.1", creds=[Cred(login="root", password="x")], element="e", transfer="scp"
    )
    switched = dataclasses.replace(h, transfer="sftp")
    assert type(switched._file_transfer) is SftpFileTransfer
    assert switched.transfer == "sftp"


def test_override_copy_has_its_own_connection():
    h = UnixHost(
        ip="10.0.0.1", creds=[Cred(login="root", password="x")], element="e", transfer="scp"
    )
    switched = dataclasses.replace(h, transfer="sftp")
    # The override copy is insulated: it builds its own connection rather than
    # sharing the original's live one.
    assert switched._connections is not h._connections


def test_term_override_switches_builtin():
    h = UnixHost(ip="10.0.0.1", creds=[Cred(login="root", password="x")], element="e", term="ssh")
    switched = dataclasses.replace(h, term="telnet")
    assert switched.term == "telnet"
    assert switched._connections.term == "telnet"
    assert h.term == "ssh"  # original untouched


def test_transfer_override_rejects_out_of_menu_backend():
    h = UnixHost(
        ip="10.0.0.1", creds=[Cred(login="root", password="x")], element="e", transfer="scp"
    )
    # console is not in the unix default menu -> validate_choice fails loud
    with pytest.raises(ValueError, match="transfer menu"):
        dataclasses.replace(h, transfer="console")
    assert h.transfer == "scp"  # original unchanged
