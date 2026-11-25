"""A netcat listener's readiness is timed from its command being typed.

The host's ``spawn_cmd`` returns the listener's task once the command has been
typed, so the session it opens first is not counted inside the readiness
timeout. Without one (the fallback) the task is spawned at once and the open
is counted, which is what the control cases pin.
"""

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.transfer import NcFileTransfer
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host.test_transfer_nc_shared_port import (
    _Bed,
    _FakeWriter,
    _fixed_scan_start,
    _make_ft,
)

_READY_TIMEOUT = 0.05
_OPEN = 0.3  # how long the fake session takes to open: longer than the readiness timeout


def _opened_later() -> asyncio.Event:
    """An event the loop sets once the fake session has taken `_OPEN` to open."""
    opened = asyncio.Event()
    asyncio.get_running_loop().call_later(_OPEN, opened.set)
    return opened


class _SlowOpenBed(_Bed):
    """A `_Bed` whose every listener command reaches the host only after its session opened."""

    async def exec_side(self, cmd: str, *a: Any, **kw: Any) -> CommandResult:
        if " -l -p " in cmd:
            await _opened_later().wait()
        return await super().exec_side(cmd, *a, **kw)

    async def spawn(self, cmd: str, **kw: Any) -> "asyncio.Task[CommandResult]":
        """What the host's `_exec_spawned` does: the task, once the command is typed."""
        typed = asyncio.Event()

        async def call() -> CommandResult:
            await _opened_later().wait()
            typed.set()
            return await _Bed.exec_side(self, cmd, **kw)

        task = asyncio.create_task(call())
        waiter = asyncio.ensure_future(typed.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        return task


async def _put(
    tmp_path: Path, bed: _SlowOpenBed, *, spawn: bool, made: list[Any] | None = None
) -> Any:
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 5)

    async def fake_connect(host: str, port: int, timeout: float = 2.0):
        bed.serve(port)
        return None, _FakeWriter()

    ft = _make_ft(AsyncMock(side_effect=bed.exec_side))
    if spawn:
        ft._spawn_cmd = bed.spawn
    if made is not None:
        made.append(ft)
    with (
        _fixed_scan_start(9000),
        patch.object(transfer_mod, "_NC_LISTENER_READY_TIMEOUT", _READY_TIMEOUT),
        patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
        patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
    ):
        per_file = await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), 5.0)
    assert not ft._reserved_ports
    return per_file[src]


async def _get(
    tmp_path: Path, bed: _SlowOpenBed, *, spawn: bool, made: list[Any] | None = None
) -> Any:
    src = Path("/remote/data.bin")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    class _Reader:
        async def read(self, _n: int) -> bytes:
            return b"hello"

    async def forward_port(*, dest_port: int) -> int:
        bed.serve(dest_port)
        return 15000

    ft = _make_ft(AsyncMock(side_effect=bed.exec_side), has_tunnel=True)
    ft._connections.forward_port = AsyncMock(side_effect=forward_port)
    if spawn:
        ft._spawn_cmd = bed.spawn
    if made is not None:
        made.append(ft)
    with (
        _fixed_scan_start(9000),
        patch.object(transfer_mod, "_NC_LISTENER_READY_TIMEOUT", _READY_TIMEOUT),
        patch.object(
            transfer_mod, "_connect_with_retry", AsyncMock(return_value=(_Reader(), _FakeWriter()))
        ),
        patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
    ):
        per_file = await asyncio.wait_for(ft._get_files_nc_tunneled([src], dst_dir), 5.0)
    assert not ft._reserved_ports
    return per_file[src]


_DIRECTIONS = pytest.mark.parametrize("transfer", [_put, _get], ids=["put", "tunnelled-get"])


@pytest.mark.asyncio
@_DIRECTIONS
async def test_a_slow_session_open_is_not_counted_in_the_readiness_timeout(
    tmp_path: Path, transfer: Any
) -> None:
    bed = _SlowOpenBed()
    result = await transfer(tmp_path, bed, spawn=True)
    assert result.status is Status.Success, result.msg
    assert len(bed.listen_cmds) == 1, "the first listener was ready: no retry"


@pytest.mark.asyncio
@_DIRECTIONS
async def test_without_a_spawn_entry_the_open_is_counted(tmp_path: Path, transfer: Any) -> None:
    """The control: the same slow open through the fallback times out."""
    result = await transfer(tmp_path, _SlowOpenBed(), spawn=False)
    assert result.status is Status.Error
    assert "not ready within" in (result.msg or "")


@pytest.mark.asyncio
@_DIRECTIONS
async def test_a_cancel_while_the_spawn_is_pending_propagates_and_frees_the_port(
    tmp_path: Path, transfer: Any
) -> None:
    bed = _SlowOpenBed()
    entered = asyncio.Event()
    cancelled: list[str] = []

    async def spawn(cmd: str, **kw: Any) -> "asyncio.Task[CommandResult]":
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(re.search(r"-p (\d+)", cmd).group(1))  # type: ignore[union-attr]
            raise
        raise AssertionError("unreachable")

    bed.spawn = spawn  # type: ignore[method-assign]
    made: list[Any] = []
    running = asyncio.create_task(transfer(tmp_path, bed, spawn=True, made=made))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled == ["9000"]
    assert not made[0]._reserved_ports, "the reserved port is released"
