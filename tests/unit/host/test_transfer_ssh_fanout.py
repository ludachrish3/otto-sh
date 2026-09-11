"""scp and sftp fan out on the shared dispatcher, bounded by their own options.

Both backends used to open every file with an unbounded ``asyncio.gather``,
capped only by whatever ``sshd``'s ``MaxSessions`` refused mid-flight. They now
hand one call per file to :meth:`~otto.host.transfer.base.BaseFileTransfer._dispatch_per_file`,
sized from ``scp_options.max_concurrent_transfers`` /
``sftp_options.max_concurrent_transfers`` (or the derived
:data:`~otto.host.transfer.base.DEFAULT_SESSION_TRANSFER_LIMIT` when unset).

Parametrized over both backends and both directions so the same claim is
proven on all four call sites rather than once and assumed for the rest.
"""

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest

from otto.host.options import ScpOptions, SftpOptions
from otto.host.transfer.base import DEFAULT_SESSION_TRANSFER_LIMIT
from otto.host.transfer.scp import ScpFileTransfer
from otto.host.transfer.sftp import SftpFileTransfer
from otto.utils import Status

BACKENDS_AND_DIRECTIONS = [
    ("scp", "put"),
    ("scp", "get"),
    ("sftp", "put"),
    ("sftp", "get"),
]


class _FanoutRecorder:
    """Stands in for ``asyncssh.scp`` / an sftp client's ``put``/``get``.

    Counts in-flight calls (and their peak) so a test can assert the bound the
    dispatcher's semaphore actually enforced, rather than trusting that it did.
    Sleeps 10ms per call so overlapping calls have a window to be observed
    overlapping at all.
    """

    def __init__(self, *, fail_name: str | None = None) -> None:
        self.in_flight = 0
        self.peak = 0
        self.fail_name = fail_name
        self.order: list[str] = []

    @staticmethod
    def _name(args: tuple[Any, ...]) -> str:
        # scp's GET passes `(ssh_conn, str(src))` as the first positional arg;
        # every other call (scp PUT, sftp PUT, sftp GET) passes `str(src)`
        # directly.
        first = args[0]
        path_str = first[1] if isinstance(first, tuple) else first
        return Path(path_str).name

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        name = self._name(args)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if name == self.fail_name:
                raise RuntimeError(f"boom: {name}")
        finally:
            self.in_flight -= 1
            self.order.append(name)


class _ScpConnections:
    """A connection manager whose ``ssh()`` answers one opaque connection object.

    Nothing on the fake path reads through it -- ``asyncssh.scp`` itself is
    patched out -- so an opaque sentinel is all ``_get_files_scp``/
    ``_put_files_scp`` need.
    """

    def __init__(self) -> None:
        self._conn = object()

    async def ssh(self) -> object:
        return self._conn


class _SftpConnections:
    """A connection manager whose ``sftp()`` answers the given fake client."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def sftp(self) -> object:
        return self._client


def _make_backend(
    backend_name: str,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _FanoutRecorder,
    *,
    scp_options: "ScpOptions | None" = None,
    sftp_options: "SftpOptions | None" = None,
) -> "ScpFileTransfer | SftpFileTransfer":
    if backend_name == "scp":
        monkeypatch.setattr(asyncssh, "scp", recorder)
        return ScpFileTransfer(
            connections=_ScpConnections(),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            scp_options=scp_options if scp_options is not None else ScpOptions(),
            userland=None,
        )
    client = MagicMock(name="SFTPClient")
    client.put = recorder
    client.get = recorder
    return SftpFileTransfer(
        connections=_SftpConnections(client),  # type: ignore[arg-type]
        name="dut",
        exec_cmd=AsyncMock(),
        sftp_options=sftp_options,
    )


async def _run(backend: Any, direction: str, src_files: list[Path], dest_dir: Path, **kwargs: Any):
    if direction == "put":
        return await backend.put_files(src_files, dest_dir, show_progress=False, **kwargs)
    return await backend.get_files(src_files, dest_dir, show_progress=False, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend_name", "direction"), BACKENDS_AND_DIRECTIONS)
async def test_the_fan_out_is_bounded_by_the_derived_default(
    backend_name: str, direction: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _FanoutRecorder()
    backend = _make_backend(backend_name, monkeypatch, recorder)
    files = [tmp_path / f"f{i}.bin" for i in range(12)]
    result = await _run(backend, direction, files, tmp_path)
    assert recorder.peak == DEFAULT_SESSION_TRANSFER_LIMIT, (
        f"peak in-flight was {recorder.peak}, expected the derived default "
        f"{DEFAULT_SESSION_TRANSFER_LIMIT}"
    )
    assert all(r.status is Status.Success for r in result.value.values())
    assert len(result.value) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend_name", "direction"), BACKENDS_AND_DIRECTIONS)
async def test_a_configured_bound_is_obeyed_exactly(
    backend_name: str, direction: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _FanoutRecorder()
    options_kwargs = (
        {"scp_options": ScpOptions(max_concurrent_transfers=2)}
        if backend_name == "scp"
        else {"sftp_options": SftpOptions(max_concurrent_transfers=2)}
    )
    backend = _make_backend(backend_name, monkeypatch, recorder, **options_kwargs)
    files = [tmp_path / f"f{i}.bin" for i in range(8)]
    await _run(backend, direction, files, tmp_path)
    assert recorder.peak == 2, f"peak in-flight was {recorder.peak}, expected the configured 2"


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend_name", "direction"), BACKENDS_AND_DIRECTIONS)
async def test_no_concurrent_moves_one_file_at_a_time(
    backend_name: str, direction: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _FanoutRecorder()
    backend = _make_backend(backend_name, monkeypatch, recorder)
    files = [tmp_path / f"f{i}.bin" for i in range(6)]
    await _run(backend, direction, files, tmp_path, concurrent=False)
    assert recorder.peak == 1, f"peak in-flight was {recorder.peak}, expected 1 (concurrent=False)"
    assert recorder.order == [f.name for f in files], (
        f"completion order was {recorder.order}, expected source order preserved "
        f"under concurrent=False"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend_name", "direction"), BACKENDS_AND_DIRECTIONS)
async def test_a_failing_file_leaves_its_siblings_landing(
    backend_name: str, direction: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = [tmp_path / f"f{i}.bin" for i in range(5)]
    fail_name = files[2].name
    recorder = _FanoutRecorder(fail_name=fail_name)
    backend = _make_backend(backend_name, monkeypatch, recorder)
    result = await _run(backend, direction, files, tmp_path)
    per_file = result.value
    for f in files:
        r = per_file[f]
        if f.name == fail_name:
            assert r.status is Status.Error, f"{f}: expected Error, got {r.status}"
            assert "boom" in r.msg, f"{f}: expected the recorder's failure message, got {r.msg!r}"
        else:
            assert r.status is Status.Success, f"{f}: expected Success, got {r.status}"
        assert r.status is not Status.Skipped, f"{f}: a sibling of a failing file was skipped"


def test_a_bound_below_one_is_refused_at_construction() -> None:
    with pytest.raises(
        ValueError, match=re.escape("scp_options.max_concurrent_transfers must be at least 1")
    ):
        ScpFileTransfer(
            connections=_ScpConnections(),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            scp_options=ScpOptions(max_concurrent_transfers=0),
            userland=None,
        )
    with pytest.raises(
        ValueError, match=re.escape("sftp_options.max_concurrent_transfers must be at least 1")
    ):
        SftpFileTransfer(
            connections=_SftpConnections(MagicMock()),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            sftp_options=SftpOptions(max_concurrent_transfers=0),
        )


@pytest.mark.parametrize("backend_name", ["scp", "sftp"])
def test_the_limit_property_reports_the_resolved_bound(backend_name: str) -> None:
    if backend_name == "scp":
        default_backend: Any = ScpFileTransfer(
            connections=_ScpConnections(),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            scp_options=ScpOptions(),
            userland=None,
        )
        configured_backend: Any = ScpFileTransfer(
            connections=_ScpConnections(),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            scp_options=ScpOptions(max_concurrent_transfers=7),
            userland=None,
        )
    else:
        default_backend = SftpFileTransfer(
            connections=_SftpConnections(MagicMock()),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
        )
        configured_backend = SftpFileTransfer(
            connections=_SftpConnections(MagicMock()),  # type: ignore[arg-type]
            name="dut",
            exec_cmd=AsyncMock(),
            sftp_options=SftpOptions(max_concurrent_transfers=7),
        )
    assert default_backend.concurrency_limit == DEFAULT_SESSION_TRANSFER_LIMIT
    assert configured_backend.concurrency_limit == 7
