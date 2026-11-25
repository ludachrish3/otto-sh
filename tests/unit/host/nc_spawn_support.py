"""A fake of the host's spawn entry, shared by the netcat transfer tests."""

import asyncio
from collections.abc import Awaitable, Callable

from otto.result import CommandResult


async def spawn_once_typed(
    run: Callable[[], Awaitable[CommandResult]],
    before_typed: Callable[[], Awaitable[None]] | None = None,
) -> "asyncio.Task[CommandResult]":
    """What the host's `_exec_spawned` does: the task, once the command is typed.

    *before_typed* is the session open that precedes typing; if it raises, the
    task is returned already done with that error, as a failed open leaves it.
    """
    typed = asyncio.Event()

    async def call() -> CommandResult:
        if before_typed is not None:
            await before_typed()
        typed.set()
        return await run()

    task = asyncio.create_task(call())
    waiter = asyncio.ensure_future(typed.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    return task
