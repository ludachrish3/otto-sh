"""``authenticating_protocols``: the scope vocabulary a cred may name.

Spec 2026-09-13 cred-scope §2.1.
"""

from otto.host import connections as conn_mod
from otto.host.capability import authenticating_protocols
from otto.host.connections import ConnectionManager, register_term_backend
from otto.host.transfer import NcFileTransfer, register_transfer_backend


def test_built_ins_are_console_ftp_ssh_telnet_sorted():
    assert authenticating_protocols() == ["console", "ftp", "ssh", "telnet"]


def test_a_custom_authenticating_backend_joins_the_vocabulary():
    class Handmade(NcFileTransfer):
        host_families = frozenset({"unix"})
        authenticates = True

    register_transfer_backend("handmade", Handmade)
    assert "handmade" in authenticating_protocols()


def test_a_custom_term_that_does_not_authenticate_stays_out():
    class Serial(ConnectionManager):
        pass

    register_term_backend(
        "serialish", Serial, host_families=frozenset({"unix"}), authenticates=False
    )
    assert "serialish" not in authenticating_protocols()
    assert "serialish" in conn_mod.TERM_BACKENDS
