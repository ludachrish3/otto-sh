"""UnixHost / EmbeddedHost build their backends through the registry + create (WS#4).

The seam's replacement differential (a backend registered over each built-in
reaches every family's host) lives in
``tests/unit/registry/test_replacement_term_transfer.py``.
"""

import dataclasses

import pytest

from otto.host import connections as conn_mod
from otto.host.connections import ConnectionManager
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.transfer import (
    NcFileTransfer,
    ScpFileTransfer,
    SftpFileTransfer,
    register_transfer_backend,
)
from otto.host.unix_host import UnixHost
from tests.unit.registry.backend_doubles import _unix_host


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
        assert conn_mod.TERM_BACKENDS.peek(name).cls is ConnectionManager
