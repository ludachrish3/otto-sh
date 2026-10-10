"""Replacing a built-in term or transfer backend reaches the real host construction path.

Unix and embedded hosts build their connections and file transfer through the
registries, so a backend registered over a built-in name with
``overwrite=True`` is what a host on that name builds, for every family the
built-in serves. The root isolation fixture restores the tables.
"""

import dataclasses
import sys

import pytest

from otto.host import connections as conn_mod
from otto.host import transfer as xfer_mod
from otto.host.connections import TermContext, register_term_backend
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.transfer import NcFileTransfer, register_transfer_backend
from otto.host.unix_host import UnixHost
from otto.registry import Ref
from tests.unit.registry.backend_doubles import (
    _assert_embedded_context,
    _OfflineConnections,
    _record_term_backend,
    _unix_host,
    _zephyr,
)

TERMS = "otto.host.connections:TERM_BACKENDS"
TRANSFERS = "otto.host.transfer.registry:TRANSFER_BACKENDS"

REPLACES = [
    (TERMS, "ssh"),
    (TERMS, "telnet"),
    (TERMS, "console"),
    (TRANSFERS, "console"),
    (TRANSFERS, "ftp"),
    (TRANSFERS, "nc"),
    (TRANSFERS, "scp"),
    (TRANSFERS, "sftp"),
    (TRANSFERS, "shell"),
    (TRANSFERS, "tftp"),
]
"""Every ``(table, built-in name)`` pair this module replaces."""

TERM_CASES = [
    ("ssh", "unix"),
    ("telnet", "unix"),
    ("telnet", "embedded"),
    ("console", "unix"),
    ("console", "embedded"),
]
"""Each built-in term, on each family it serves."""

TRANSFER_CASES = [
    ("console", "embedded"),
    ("ftp", "unix"),
    ("nc", "unix"),
    ("scp", "unix"),
    ("sftp", "unix"),
    ("shell", "unix"),
    ("tftp", "embedded"),
]
"""Each built-in transfer, on each family it serves."""


def test_replaces_names_every_built_in_term_and_transfer():
    builtins = {
        (table_key, name)
        for table_key, table in [
            (TERMS, conn_mod.TERM_BACKENDS),
            (TRANSFERS, xfer_mod.TRANSFER_BACKENDS),
        ]
        for name in table.names()
        if table.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


def test_the_cases_cover_every_family_each_built_in_serves():
    terms = {
        (n, f) for n in ["ssh", "telnet", "console"] for f in _families(conn_mod.TERM_BACKENDS, n)
    }
    transfers = {
        (n, f) for n, _ in TRANSFER_CASES for f in _families(xfer_mod.TRANSFER_BACKENDS, n)
    }
    assert terms == set(TERM_CASES)
    assert transfers == set(TRANSFER_CASES)


def _families(table, name: str) -> "frozenset[str]":
    return table.peek(name).metadata.host_families


def _unix_host_on(term: str) -> UnixHost:
    """A unix host on *term*, built through whatever is registered under it."""
    extra = (
        {"console_options": ConsoleOptions(server="test4", port=2323)} if term == "console" else {}
    )
    return UnixHost(
        ip="10.0.0.9",
        creds=[Cred(login="root", password="x")],
        element=Element("ne"),
        term=term,
        valid_terms=[term],
        **extra,
    )


def _unix_host_over_recording() -> UnixHost:
    """``_unix_host`` without its own term replacement: the recording one stays."""
    return _unix_host_on("ssh")


@pytest.mark.parametrize(("term", "family"), TERM_CASES)
def test_a_replaced_term_is_what_a_host_builds(term, family):
    if family == "unix":
        calls = _record_term_backend(term, base=_OfflineConnections)
        host = _unix_host_on(term)
    else:
        calls = _record_term_backend(term)
        host = _zephyr(term)
    assert len(calls) == 1, f"the {family} host bypassed the registered {term!r} backend"
    ctx, built = calls[0]
    assert isinstance(ctx, TermContext)
    assert ctx.term == term
    assert host._connections is built


def test_a_rebuilt_unix_host_builds_through_the_registry_again():
    calls = _record_term_backend("ssh", base=_OfflineConnections)
    host = _unix_host_over_recording()
    host.rebuild_connections()
    assert len(calls) == 2
    assert all(isinstance(ctx, TermContext) for ctx, _ in calls)
    assert host._connections is calls[-1][1]


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


def _record_transfer_backend(name: str) -> type:
    """Register a recording subclass of *name*'s own class over it, and return it.

    Its ``built`` attribute lists the ``transfer`` of every context it built from.
    """
    base = xfer_mod.TRANSFER_BACKENDS.get(name).cls

    class _Recording(base):
        built: "list[str]" = []  # noqa: RUF012 — one recording class per test

        @classmethod
        def create(cls, ctx):
            cls.built.append(ctx.transfer)
            return super().create(ctx)

    register_transfer_backend(name, _Recording, overwrite=True)
    return _Recording


@pytest.mark.parametrize(("name", "family"), TRANSFER_CASES)
def test_a_replaced_transfer_is_what_a_host_builds(name, family):
    recording = _record_transfer_backend(name)
    if family == "unix":
        host = _unix_host(transfer=name, valid_transfers=[name])
    else:
        host = _zephyr("telnet", transfer=name, valid_transfers=[name])
    assert recording.built == [name], f"the {family} host bypassed the registered {name!r} backend"
    assert isinstance(host._file_transfer, recording)


def test_unix_host_builds_registered_transfer_backend():
    """A custom transfer backend registered at runtime is the one the host builds."""
    built = {}

    class RecordingTransfer(NcFileTransfer):
        host_families = frozenset({"unix"})

        @classmethod
        def create(cls, ctx):
            built["name"] = ctx.transfer
            return super().create(ctx)

    register_transfer_backend("recording", RecordingTransfer)
    h = UnixHost(
        ip="10.0.0.9",
        creds=[Cred(login="root", password="x")],
        element=Element("e"),
        transfer="recording",
        valid_transfers=["recording"],
    )
    assert isinstance(h._file_transfer, RecordingTransfer)
    assert built["name"] == "recording"


def test_eager_invalid_metadata_beside_an_unresolved_ref_is_refused_without_importing():
    with pytest.raises(ValueError, match="host_families"):
        register_term_backend(
            "x", Ref("never_imported_mod:C"), host_families=frozenset(), authenticates=True
        )
    assert "never_imported_mod" not in sys.modules
    assert "x" not in conn_mod.TERM_BACKENDS
