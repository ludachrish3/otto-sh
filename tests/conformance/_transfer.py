"""The one way this tree reaches a host's transfer backend OBJECT.

Two surfaces need the backend itself rather than the transfer NAME --
``transfer-mode`` asks it whether it carries a mode, ``transfer-progress`` asks
it what it promises the progress bar -- and both must reach it the same way,
through the same refusal, or the second one to be written quietly invents a
second answer to "this host has no readable backend".

NOT :mod:`tests.conformance._controls`. That module is about POSITIVE CONTROLS
-- proving an instrument can go red -- and this is not one: it is an accessor
both a contract and a control call.
"""

from otto.host.host import BaseHost
from otto.host.transfer import BaseFileTransfer
from tests._fixtures.profiles import Cell


def transfer_backend_of(host: BaseHost, cell: Cell, *, refusal_tail: str) -> BaseFileTransfer:
    """*host*'s transfer backend, or a loud failure naming *cell* and *refusal_tail*.

    Reached through the private attribute both host families happen to agree
    on (:class:`~otto.host.local_host.LocalHost` and
    :class:`~otto.host.unix_host.UnixHost` each name it ``_file_transfer``),
    because there is no public one: the registry lookup
    ``build_transfer_backend`` would answer for ``sftp`` and ``scp`` but raises
    for the ``local`` cell, whose transfer name deliberately records the
    ABSENCE of a registered backend rather than naming one.

    Deliberately not ``getattr(host, "_file_transfer", None)`` with a lenient
    default. A host this cannot read is a cell whose contract has never been
    measured, and the honest report of that is a named failure, not a quietly
    skipped assertion.

    *refusal_tail* is the CALLER'S half of that failure and is required, with
    no default: the two surfaces ask this backend different questions, and a
    refusal that named neither would tell a reader which host failed but not
    what it was being asked. It completes the sentence
    ``"<cell>: <HostType> exposes no `_file_transfer`, so ..."``.
    """
    backend = getattr(host, "_file_transfer", None)
    if backend is None:
        raise AssertionError(
            f"{cell}: {type(host).__name__} exposes no `_file_transfer`, so {refusal_tail}"
        )
    return backend
