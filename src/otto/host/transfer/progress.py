from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from ...console import CONSOLE

if TYPE_CHECKING:
    from .base import TransferProgressFactory, TransferProgressHandler


def make_rich_progress_handler(progress: Progress, host_name: str) -> "TransferProgressHandler":
    """Return a ``TransferProgressHandler`` that drives the given Rich Progress bar.

    One task is created per source file, detected by a change in *src_path*.
    The caller is responsible for the Progress context (entering and exiting it).

    Example::

        with make_transfer_progress() as progress:
            handler = make_rich_progress_handler(progress, host_name=host.hostname)
            status, err = await host.get(files, dest, progress_handler=handler)
    """
    current_src: str | None = None
    task_id: TaskID | None = None

    def handler(src: str, dst: str, bytes_done: int, bytes_total: int) -> None:
        nonlocal current_src, task_id
        if src != current_src:
            current_src = src
            description = f"[green]{host_name}[/] {Path(src).name}"
            task_id = progress.add_task(description, total=bytes_total)
        assert task_id is not None
        progress.update(task_id, completed=bytes_done)

    return handler


def make_rich_progress_factory(progress: Progress, host_name: str) -> "TransferProgressFactory":
    """Return a factory that creates a fresh ``TransferProgressHandler`` per file.

    Each call to the returned factory produces an independent handler with its
    own closure state, so concurrent transfers don't share progress tracking.

    Example::

        with make_transfer_progress() as progress:
            factory = make_rich_progress_factory(progress, host_name=host.name)
            status, err = await host.put(files, dest)
    """

    def factory() -> "TransferProgressHandler":
        return make_rich_progress_handler(progress, host_name)

    return factory


def make_transfer_progress() -> Progress:
    """Return a pre-configured Rich Progress suited for file transfers."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(elapsed_when_finished=True),
        console=CONSOLE,
    )


# Rich's Live isn't meant to run multiple instances on the same console — two
# Lives rendering simultaneously produce overlapping cursor escapes and ghost
# rows. Concurrent host transfers (e.g. `asyncio.gather(host_a.put(...),
# host_b.get(...))`) used to hit exactly that. Instead we share one
# Progress across every in-flight transfer: the first caller to enter starts
# the Live, subsequent callers just attach a task, and the last caller to
# leave stops the Live and drops the singleton. Single-threaded asyncio makes
# the naive ref-count safe without a lock.
_shared_progress: Progress | None = None
_shared_progress_refs: int = 0


@asynccontextmanager
async def _acquire_shared_progress() -> AsyncIterator[Progress]:
    """Yield a process-wide Progress, creating/destroying the Live on demand."""
    global _shared_progress, _shared_progress_refs
    if _shared_progress is None:
        _shared_progress = make_transfer_progress()
        _shared_progress.start()
    progress = _shared_progress
    _shared_progress_refs += 1
    try:
        yield progress
    finally:
        _shared_progress_refs -= 1
        if _shared_progress_refs == 0:
            progress.stop()
            _shared_progress = None


def _make_sftp_progress(
    handler: "TransferProgressHandler",
) -> Callable[[bytes, bytes, int, int], None]:
    """Wrap Otto's str-path TransferProgressHandler into asyncssh's bytes-path type."""

    def adapted(src: bytes, dst: bytes, done: int, total: int) -> None:
        handler(src.decode(), dst.decode(), done, total)

    return adapted
