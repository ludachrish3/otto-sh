"""A refused ftp STOR or RETR closes the data connection it had already opened.

aioftp's ``Client.get_stream`` opens the passive data connection BEFORE it sends
STOR or RETR; when the server refuses the command the connection is dropped
unreferenced and only the garbage collector closes it, which Python reports as a
``ResourceWarning`` that pytest's unraisable plugin turns into a failure of
whichever test happens to be running then. These tests drive otto's real ftp
backend against a REAL in-process ``aioftp.Server`` over a temporary directory on
loopback, so the refusal is the server's own and the sockets are real.
"""

import gc
import warnings
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aioftp
import pytest
import pytest_asyncio

from otto.host.transfer.ftp import _FtpFileTransfer
from otto.utils import Status
from tests._fixtures.ftp_server import served_ftp

show_progress_modes = pytest.mark.parametrize("show_progress", [False, True], ids=["bare", "bar"])


class _Connections:
    def __init__(self, client: aioftp.Client) -> None:
        self._client = client

    async def ftp(self) -> aioftp.Client:
        return self._client


@pytest_asyncio.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[aioftp.Client, Path]]:
    """A connected real client and the directory the real server serves."""
    root = tmp_path / "served"
    root.mkdir()
    async with served_ftp(root) as client:
        yield client, root


def _backend(client: aioftp.Client) -> _FtpFileTransfer:
    from unittest.mock import AsyncMock

    return _FtpFileTransfer(
        connections=_Connections(client),  # ty: ignore[invalid-argument-type]
        name="dut",
        exec_cmd=AsyncMock(),
    )


def _progress(show_progress: bool) -> Any:
    return (lambda: lambda *_args: None) if show_progress else None


def _leaked_sockets(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, ResourceWarning)
        and ("socket" in str(w.message).lower() or "transport" in str(w.message).lower())
    ]


def _collect() -> None:
    for _ in range(3):
        gc.collect()


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_put_closes_its_data_socket(
    served: tuple[aioftp.Client, Path], tmp_path: Path, show_progress: bool
) -> None:
    client, _root = served
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 20)
    good = tmp_path / "good.bin"
    good.write_bytes(b"g" * 20)
    backend = _backend(client)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        refused = await backend.run_put(
            [src], Path("/absent"), progress_factory=_progress(show_progress)
        )
        _collect()
        landed = await backend.run_put([good], Path("/"), progress_factory=_progress(show_progress))
        _collect()

    assert refused[src].status is Status.Error, refused[src].msg
    assert landed[good].status is Status.Success, landed[good].msg
    assert _leaked_sockets(caught) == []


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_get_closes_its_data_socket(
    served: tuple[aioftp.Client, Path], tmp_path: Path, show_progress: bool
) -> None:
    client, root = served
    (root / "tree").mkdir()
    (root / "present.bin").write_bytes(b"p" * 20)
    dest = tmp_path / "local"
    dest.mkdir()
    backend = _backend(client)
    directory, missing, present = Path("/tree"), Path("/absent.bin"), Path("/present.bin")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        refused = await backend.run_get(
            [directory, missing], dest, progress_factory=_progress(show_progress), concurrent=False
        )
        _collect()
        landed = await backend.run_get([present], dest, progress_factory=_progress(show_progress))
        _collect()

    assert refused[directory].status is Status.Error, refused[directory].msg
    assert refused[missing].status is Status.Error, refused[missing].msg
    assert landed[present].status is Status.Success, landed[present].msg
    assert (dest / "present.bin").read_bytes() == b"p" * 20
    assert list(dest.iterdir()) == [dest / "present.bin"]
    assert _leaked_sockets(caught) == []
