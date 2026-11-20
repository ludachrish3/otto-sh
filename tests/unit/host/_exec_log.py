"""Readers of the records an exec writes, shared by the exec-log tests of several modules."""

import contextlib
import logging
from collections.abc import Iterator

from otto.host.login_proxy import Cred
from tests.conformance._log import _watching_records

SU_LOGIN = [Cred(login="admin", password="secret"), Cred(login="root", password="secret")]
"""A login and root's credential: what ``unix_host_over_scripted`` needs for ``su``."""


@contextlib.contextmanager
def all_records() -> "Iterator[list[logging.LogRecord]]":
    """Every otto record, DEBUG and up, written while the block runs."""
    records: list[logging.LogRecord] = []
    with _watching_records(records.append, level=logging.DEBUG):
        yield records


def records_of(records: "list[logging.LogRecord]", host: object) -> "list[logging.LogRecord]":
    """The records of *records* tagged with *host*."""
    return [r for r in records if getattr(r, "host", None) is host]
