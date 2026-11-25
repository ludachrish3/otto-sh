"""Readers of the records an exec writes, shared by the exec-log tests of several modules."""

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.logger.mode import LogMode
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


def ssh_parent_over_shells(
    cls: type,
    home: Path,
    scripts: "dict[str, str] | None" = None,
    *,
    creds: "list[Cred] | None" = None,
    profile: str = "",
    login: bool = False,
) -> "tuple[Any, AsyncMock]":
    """A *cls* ssh host standing at NORMAL over real local shells, and its dialler.

    The connection is ``test_terminal_switch``'s ``_EitherConnection`` over
    *home*. Each of *scripts* (a name and a ``sh`` body) is a command on
    those shells' ``PATH``, so a wrapper line a test fixes is a command they
    run. *profile* is written into the shells' ``.profile`` ahead of the
    line that puts those commands on ``PATH``; *login* makes the terminal
    shell a login shell, which reads it. Close the host with :func:`close_ssh_parent`.
    """
    from tests.unit.host.test_terminal_switch import _EitherConnection

    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (scripts or {}).items():
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    (home / ".profile").write_text(f'{profile}export PATH="{bin_dir}:$PATH"\n')
    host = cls(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=creds or [Cred(login="u", password="p")],
        term="ssh",
        log=LogMode.NORMAL,
    )
    dial = AsyncMock(return_value=_EitherConnection(home, login=login))
    host._connections.ssh = dial
    return host, dial


async def close_ssh_parent(host: Any, dial: AsyncMock) -> None:
    """Close *host* from :func:`ssh_parent_over_shells`, then reap its connection's shells."""
    await host.close()
    await dial.return_value.reap()
