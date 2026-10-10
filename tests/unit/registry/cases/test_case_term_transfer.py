"""Conformance cases for the term and transfer backends (two context-only class seams)."""

import functools
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from otto.host.connections import (
    TERM_BACKENDS,
    ConnectionManager,
    TermContext,
    TermMetadata,
    register_term_backend,
)
from otto.host.login_proxy import Cred
from otto.host.options import NcOptions, UserlandOptions
from otto.host.transfer import NcFileTransfer, TransferContext
from otto.host.transfer.registry import (
    TRANSFER_BACKENDS,
    TransferMetadata,
    register_transfer_backend,
)
from otto.host.userland import Userland
from otto.registry import class_backend
from tests.unit.registry import conformance

COVERS = [
    "otto.host.connections:TERM_BACKENDS",
    "otto.host.transfer.registry:TRANSFER_BACKENDS",
]

_TERM_METADATA = TermMetadata(frozenset({"unix"}), authenticates=True)
_NC_METADATA = TransferMetadata(
    frozenset({"unix"}), NcFileTransfer.progress_granularity, authenticates=False
)


@dataclass
class _Spy:
    parses: int = 0
    builds: int = 0


def _term_ctx() -> TermContext:
    return TermContext(
        ip="10.0.0.5", creds=[Cred(login="root", password="x")], term="ssh", name="h1"
    )


def _transfer_ctx() -> TransferContext:
    return TransferContext(
        transfer="nc",
        host_name="h1",
        connections=MagicMock(),
        nc_options=NcOptions(),
        get_local_ip=lambda: "1.2.3.4",
        exec_cmd=AsyncMock(),
        userland=Userland(UserlandOptions(), AsyncMock()),
    )


def _term_spy_entry():
    spy = _Spy()

    class _Spied(ConnectionManager):
        @classmethod
        def create(cls, ctx: TermContext) -> ConnectionManager:
            spy.builds += 1
            return super().create(ctx)

    return class_backend(cls=_Spied, metadata=_TERM_METADATA), spy


def _transfer_spy_entry():
    spy = _Spy()

    class _Spied(NcFileTransfer):
        @classmethod
        def create(cls, ctx):
            spy.builds += 1
            return super().create(ctx)

    return class_backend(cls=_Spied, metadata=_NC_METADATA), spy


def test_term_backend_case():
    conformance.assert_backend_registry(
        TERM_BACKENDS,
        env=_term_ctx(),
        make_spy_entry=_term_spy_entry,
        good_raw={},
        bad_raw={"x": 1},
    )


def test_transfer_backend_case():
    conformance.assert_backend_registry(
        TRANSFER_BACKENDS,
        env=_transfer_ctx(),
        make_spy_entry=_transfer_spy_entry,
        good_raw={},
        bad_raw={"x": 1},
    )


class _CaseTerm(ConnectionManager):
    """A term class of the case's own."""


class _CaseTransfer(NcFileTransfer):
    """A transfer class of the case's own; it declares what nc declares."""


def test_term_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        TERM_BACKENDS,
        via_wrapper=functools.partial(
            register_term_backend,
            "case-term",
            _CaseTerm,
            host_families=frozenset({"unix"}),
            authenticates=True,
        ),
        record_for=lambda: class_backend(cls=_CaseTerm, metadata=_TERM_METADATA),
    )


def test_transfer_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        TRANSFER_BACKENDS,
        via_wrapper=functools.partial(register_transfer_backend, "case-transfer", _CaseTransfer),
        record_for=lambda: class_backend(cls=_CaseTransfer, metadata=_NC_METADATA),
    )
