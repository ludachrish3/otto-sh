"""Fixtures for the console e2e tests: the test2 lease and the leave-it-at-login check."""

import asyncio
from collections.abc import Iterator

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data_console, make_console_host
from tests.e2e.console._raw import console_answer, ends_at_login

TEST2_CONSOLE = host_data_console("test2")


@pytest.fixture
def test2_lease(tmp_path_factory) -> Iterator[str]:
    """Hold the unix-pool lease on test2 (its console is one serial line)."""
    with lease_unix_host(tmp_path_factory.getbasetemp().parent, ("test2",)) as element:
        yield element


async def _restore_or_explain() -> str | None:
    """``None`` when test2's console answers a CR with ``login:``; else reset it and say why."""
    answer = await console_answer(TEST2_CONSOLE.server, TEST2_CONSOLE.port)
    if ends_at_login(answer):
        return None
    host = make_console_host("test2")
    try:
        restored = await host.logout()
    finally:
        await host.close()
    # Re-probe rather than trust the reset's own word for it.
    after = await console_answer(TEST2_CONSOLE.server, TEST2_CONSOLE.port)
    return (
        f"the test left test2's console ({TEST2_CONSOLE.server}:{TEST2_CONSOLE.port}) off its "
        f"login prompt; it answered {answer!r}; `logout` then said {restored.value!r}, and "
        f"the line then answered {after!r}"
        + ("" if ends_at_login(after) else " (STILL NOT at login: reset it by hand)")
    )


@pytest.fixture
def test2_console(test2_lease) -> Iterator[None]:
    """Lease test2 and, after the test, prove its console is back at ``login:``.

    A test that leaves the line logged in (or anywhere but its login
    prompt) is FAILED here, after the line is reset with ``logout`` so the
    next test starts clean. Sync on purpose, so sync (pty-driven) and async
    tests share it: the teardown runs its own event loop.
    """
    yield
    problem = asyncio.run(_restore_or_explain())
    if problem is not None:
        pytest.fail(problem)
