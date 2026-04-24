"""Timeout wrapper for SSH transfer integration tests.

Wraps an async transfer op in ``asyncio.wait_for``. Without this bound, a
stalled asyncssh session or unresponsive SSH daemon can hang the suite
indefinitely — the kernel TCP keepalive on an ESTAB SSH socket won't fire
for ~2 hours, so an ``await`` with no asyncio-level deadline sits forever.

No retry: the nc silent-empty-file race is handled internally by
``FileTransfer._put_one`` (which retries once on verify failure). Any
``TimeoutError`` that reaches this layer is a real hang and should surface
as a failure, not be retried.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from otto.utils import Status

DEFAULT_TRANSFER_TIMEOUT = 30.0


async def transfer_with_retry(
    op_factory: Callable[[], Awaitable[tuple[Status, str]]],
    *,
    timeout: float = DEFAULT_TRANSFER_TIMEOUT,
) -> tuple[Status, str]:
    """Run ``await op_factory()`` with a ``timeout`` bound."""
    return await asyncio.wait_for(op_factory(), timeout=timeout)
