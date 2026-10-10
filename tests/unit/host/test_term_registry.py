"""Term backend registry + ConnectionManager.create construction seam (WS#4)."""

import sys

import pytest

from otto.host import connections as conn_mod
from otto.host.connections import (
    ConnectionManager,
    TermConstructionError,
    TermContext,
    build_term_backend,
    register_term_backend,
)
from otto.host.login_proxy import Cred
from otto.registry import Ref


def _term_ctx() -> TermContext:
    return TermContext(
        ip="10.0.0.5", creds=[Cred(login="root", password="x")], term="ssh", name="h1"
    )


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
        assert conn_mod.TERM_BACKENDS.peek("ssh").cls is ConnectionManager
        assert conn_mod.TERM_BACKENDS.peek("telnet").cls is ConnectionManager

    def test_builtin_term_families(self):
        assert conn_mod.TERM_BACKENDS.peek("ssh").metadata.host_families == frozenset({"unix"})
        assert conn_mod.TERM_BACKENDS.peek("telnet").metadata.host_families == frozenset(
            {"unix", "embedded"}
        )


class TestRegistry:
    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown term backend"):
            build_term_backend("nope", _term_ctx())
        # known names are listed so a typo is diagnosable
        with pytest.raises(ValueError, match="ssh") as exc_info:
            build_term_backend("nope", _term_ctx())
        assert "ssh" in str(exc_info.value)
        assert "telnet" in str(exc_info.value)

    def test_register_and_build_custom(self):
        class CustomTerm(ConnectionManager):
            pass

        register_term_backend(
            "myterm", CustomTerm, host_families=frozenset({"unix"}), authenticates=True
        )
        assert conn_mod.TERM_BACKENDS.peek("myterm").cls is CustomTerm
        assert conn_mod.TERM_BACKENDS.peek("myterm").metadata.host_families == frozenset({"unix"})

    def test_register_rejects_empty_families(self):
        class CustomTerm(ConnectionManager):
            pass

        with pytest.raises(ValueError, match="host_families is empty"):
            register_term_backend("bad", CustomTerm, host_families=frozenset(), authenticates=True)

    def test_built_in_terms_authenticate(self):
        assert conn_mod.TERM_BACKENDS.peek("ssh").metadata.authenticates is True
        assert conn_mod.TERM_BACKENDS.peek("telnet").metadata.authenticates is True

    def test_register_records_authenticates(self):
        class Quiet(ConnectionManager):
            pass

        register_term_backend(
            "quiet", Quiet, host_families=frozenset({"unix"}), authenticates=False
        )
        assert conn_mod.TERM_BACKENDS.peek("quiet").metadata.authenticates is False

    def test_register_rejects_a_non_bool_authenticates(self):
        class Bad(ConnectionManager):
            pass

        with pytest.raises(ValueError, match="authenticates must be a bool"):
            register_term_backend(
                "bad-auth", Bad, host_families=frozenset({"unix"}), authenticates="yes"
            )

    def test_register_requires_authenticates(self):
        class Bad(ConnectionManager):
            pass

        with pytest.raises(TypeError, match="authenticates"):
            register_term_backend("bad-missing", Bad, host_families=frozenset({"unix"}))


class TestCreate:
    def test_create_constructs_connection_manager(self):
        ctx = TermContext(
            ip="10.0.0.5",
            creds=[Cred(login="root", password="x")],
            term="ssh",
            name="h1",
        )
        cm = ConnectionManager.create(ctx)
        assert isinstance(cm, ConnectionManager)
        assert cm.ip == "10.0.0.5"
        assert cm.term == "ssh"


# ── built through the registry: a context in, a built backend out ────────────


class TestBuildsThroughTheRegistry:
    def test_build_term_backend_returns_a_built_backend(self):
        cm = build_term_backend("ssh", _term_ctx())
        assert isinstance(cm, ConnectionManager)
        assert (cm.ip, cm.term) == ("10.0.0.5", "ssh")

    def test_an_unknown_term_names_the_stage_and_lists_names(self):
        with pytest.raises(TermConstructionError, match="lookup failed") as exc_info:
            build_term_backend("sshh", _term_ctx())
        assert isinstance(exc_info.value, ValueError)
        message = str(exc_info.value)
        assert "'ssh'" in message  # the near miss
        assert "telnet" in message  # and the registered names

    def test_a_create_that_raises_is_a_construction_error_with_the_cause_chained(self):
        class Boom(ConnectionManager):
            @classmethod
            def create(cls, ctx):
                raise RuntimeError("boom")

        register_term_backend("boom", Boom, host_families=frozenset({"unix"}), authenticates=True)
        with pytest.raises(TermConstructionError, match="construction failed") as exc_info:
            build_term_backend("boom", _term_ctx())
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_a_create_returning_a_non_manager_is_a_result_error(self):
        class Wrong(ConnectionManager):
            @classmethod
            def create(cls, ctx):
                return object()

        register_term_backend("wrong", Wrong, host_families=frozenset({"unix"}), authenticates=True)
        with pytest.raises(TermConstructionError, match="result failed"):
            build_term_backend("wrong", _term_ctx())

    def test_a_reference_to_a_non_manager_is_a_resolution_error(self):
        register_term_backend(
            "notamanager",
            Ref("otto.host.options:ConsoleOptions"),
            host_families=frozenset({"unix"}),
            authenticates=False,
        )
        with pytest.raises(TermConstructionError, match="resolution failed") as exc_info:
            build_term_backend("notamanager", _term_ctx())
        assert isinstance(exc_info.value.__cause__, TypeError)
        assert "not a ConnectionManager subclass" in str(exc_info.value.__cause__)

    def test_term_metadata_is_read_without_importing(self):
        register_term_backend(
            "lazy",
            Ref("never_imported_term_mod:Term"),
            host_families=frozenset({"unix"}),
            authenticates=False,
        )
        metadata = conn_mod.TERM_BACKENDS.peek("lazy").metadata
        assert (metadata.host_families, metadata.authenticates, metadata.dials_host) == (
            frozenset({"unix"}),
            False,
            True,
        )
        assert "never_imported_term_mod" not in sys.modules
