"""
Asyncio-aware test timeout fixture.

Enforces ``@pytest.mark.timeout(seconds)`` by scheduling cooperative task
cancellation via ``asyncio.Task.cancel()``.  Unlike signal- or thread-based
approaches (e.g. ``pytest-timeout``), this injects ``CancelledError`` at the
next ``await`` point, so ``finally`` blocks and ``async with`` exit handlers
run normally — critical for cleaning up SSH connections, tunnels, and port
forwards.

The fixture is registered as a pytest plugin by ``OttoPlugin`` (production
runs via ``otto test``) and by the project root ``conftest.py`` (dev runs
via ``pytest``).

Supports two ways to specify a timeout:

1. ``@pytest.mark.timeout(seconds)`` marker on the test or class.
2. A ``timeout`` class attribute on the test class (e.g. ``OttoSuite``
   subclasses).

The marker takes precedence when both are present.
"""

import asyncio

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _otto_timeout(request: pytest.FixtureRequest):
    """Enforce ``@pytest.mark.timeout(seconds)`` via asyncio task cancellation."""
    timeout_marker = request.node.get_closest_marker('timeout')
    timeout_secs: float | None = None
    if timeout_marker is not None and timeout_marker.args:
        timeout_secs = float(timeout_marker.args[0])
    elif request.instance is not None and hasattr(request.instance, 'timeout'):
        val = getattr(request.instance, 'timeout', None)
        if val is not None:
            timeout_secs = float(val)

    handle = None
    if timeout_secs is not None:
        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        if current_task is not None:
            handle = loop.call_later(timeout_secs, current_task.cancel)

    try:
        yield
    except asyncio.CancelledError:
        pytest.fail(f'Test timed out after {timeout_secs}s')
    finally:
        if handle is not None:
            handle.cancel()
