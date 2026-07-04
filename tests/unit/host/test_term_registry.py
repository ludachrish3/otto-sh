"""Term backend registry + ConnectionManager.create construction seam (WS#4)."""

import pytest

from otto.host import connections as conn_mod
from otto.host.connections import (
    ConnectionManager,
    TermContext,
    build_term_backend,
    register_term_backend,
)
from otto.host.login_proxy import Cred


@pytest.fixture(autouse=True)
def _isolate_term_registry():
    """Unregister any test-added term backend after each test.

    Built-ins (``ssh``/``telnet``) are never touched by these tests, so there
    is nothing to snapshot/restore — only the names a test itself registers
    need cleanup.
    """
    before = set(conn_mod.TERM_BACKENDS.names())
    try:
        yield
    finally:
        for name in set(conn_mod.TERM_BACKENDS.names()) - before:
            conn_mod.TERM_BACKENDS.unregister(name)


class TestBuiltins:
    def test_ssh_and_telnet_registered_to_connection_manager(self):
        assert build_term_backend("ssh") is ConnectionManager
        assert build_term_backend("telnet") is ConnectionManager

    def test_builtin_term_families(self):
        assert conn_mod.TERM_BACKENDS.get("ssh").host_families == frozenset({"unix"})
        assert conn_mod.TERM_BACKENDS.get("telnet").host_families == frozenset({"unix", "embedded"})


class TestRegistry:
    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown term backend"):
            build_term_backend("nope")
        # known names are listed so a typo is diagnosable
        with pytest.raises(ValueError, match="ssh") as exc_info:
            build_term_backend("nope")
        assert "ssh" in str(exc_info.value)
        assert "telnet" in str(exc_info.value)

    def test_register_and_build_custom(self):
        class CustomTerm(ConnectionManager):
            pass

        register_term_backend("myterm", CustomTerm, host_families=frozenset({"unix"}))
        assert build_term_backend("myterm") is CustomTerm
        assert conn_mod.TERM_BACKENDS.get("myterm").host_families == frozenset({"unix"})

    def test_register_rejects_empty_families(self):
        class CustomTerm(ConnectionManager):
            pass

        with pytest.raises(ValueError, match="host_families is empty"):
            register_term_backend("bad", CustomTerm, host_families=frozenset())


class TestCreate:
    def test_create_constructs_connection_manager(self):
        ctx = TermContext(
            ip="10.0.0.5",
            creds=[Cred(login="root", password="x")],
            user="root",
            term="ssh",
            name="h1",
        )
        cm = ConnectionManager.create(ctx)
        assert isinstance(cm, ConnectionManager)
        assert cm.ip == "10.0.0.5"
        assert cm.term == "ssh"
