"""UnixHost / EmbeddedHost build their backends through the registry + create (WS#4)."""

import pytest

from otto.host import connections as conn_mod
from otto.host import transfer as xfer_mod
from otto.host.connections import ConnectionManager
from otto.host.transfer import FileTransfer, register_transfer_backend
from otto.host.unix_host import UnixHost


@pytest.fixture(autouse=True)
def _isolate_registries():
    saved_t = dict(conn_mod._TERM_BACKENDS)
    saved_x = dict(xfer_mod._TRANSFER_BACKENDS)
    try:
        yield
    finally:
        conn_mod._TERM_BACKENDS.clear()
        conn_mod._TERM_BACKENDS.update(saved_t)
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved_x)


def test_unix_host_builds_registered_transfer_backend():
    """A custom transfer backend registered at runtime is the one the host builds."""
    built = {}

    class RecordingTransfer(FileTransfer):
        host_families = frozenset({"unix"})

        @classmethod
        def create(cls, ctx):
            built["name"] = ctx.transfer
            return super().create(ctx)

    xfer_mod._TRANSFER_BACKENDS["recording"] = RecordingTransfer

    h = UnixHost(ip="10.0.0.9", creds={"root": "x"}, element="e",
                 transfer="recording")
    assert isinstance(h._file_transfer, RecordingTransfer)
    assert built["name"] == "recording"


def test_connection_factory_override_still_wins():
    """A _connection_factory test double is still used in place of the registry."""
    class FakeConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):
            self._name = kwargs.get("name", "fake")
            self._term = kwargs.get("term", "ssh")
            self._hop = None

    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e",
                 term="ssh", _connection_factory=FakeConnections)
    assert isinstance(h._connections, FakeConnections)


# ---------------------------------------------------------------------------
# set_transfer_type / set_term_type rebuild the backend via create() so a
# *custom* backend swap instantiates the right CLASS, not just a string swap on
# the existing instance (the latent footgun WS#4's custom backends expose).
# ---------------------------------------------------------------------------


class XmodemTransfer(FileTransfer):
    """A distinct unix transfer backend class for the rebuild tests."""

    host_families = frozenset({"unix"})


def test_set_transfer_type_rebuilds_to_custom_backend():
    register_transfer_backend("xmodem", XmodemTransfer)
    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", transfer="scp")
    assert type(h._file_transfer) is FileTransfer  # built-in to start

    h.set_transfer_type("xmodem")

    # Rebuilt to the custom CLASS — not a FileTransfer with a swapped string.
    assert isinstance(h._file_transfer, XmodemTransfer)
    assert h.transfer == "xmodem"


def test_set_transfer_type_switches_among_builtins():
    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", transfer="scp")
    h.set_transfer_type("sftp")
    assert type(h._file_transfer) is FileTransfer
    assert h._file_transfer.transfer == "sftp"
    assert h.transfer == "sftp"


def test_set_transfer_type_preserves_the_connection():
    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", transfer="scp")
    conn_before = h._connections
    h.set_transfer_type("sftp")
    # Transfer-only rebuild: the live connection object is untouched.
    assert h._connections is conn_before


def test_set_term_type_switches_builtin():
    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", term="ssh")
    h.set_term_type("telnet")
    assert h.term == "telnet"
    assert h._connections.term == "telnet"


def test_set_transfer_type_rejects_embedded_only_backend():
    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", transfer="scp")
    with pytest.raises(ValueError, match="not a valid unix transfer backend"):
        h.set_transfer_type("console")
    assert h.transfer == "scp"  # unchanged
