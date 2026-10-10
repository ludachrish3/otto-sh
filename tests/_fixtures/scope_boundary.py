"""Hold a wider pytest-asyncio loop's cleanup for a fixture that shares hosts across tests.

The root conftest fails a test that leaves hosts registered on an open event
loop no cleanup boundary holds, and a live class, module, package or session
runner loop is not exempt (``_orphan_registrations`` in ``tests/conftest.py``).
Under ``otto test`` the suite plugin holds a boundary on every runner loop;
under bare ``pytest`` nothing does. So an async fixture with a wider
``loop_scope`` whose hosts outlive the test that first used them holds one
itself, for its whole scope, with :func:`held_for_scope`; otherwise the first
test to use it errors at teardown with ``LeakedRegistrationError``.
``tests/unit/test_wider_loop_fixtures_hold_a_boundary.py`` requires it of
every such fixture in the repo.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator


@contextlib.asynccontextmanager
async def held_for_scope(label: str) -> AsyncIterator[None]:
    """Hold the running loop's cleanup boundary until the block exits.

    Wrap the whole fixture body, its ``yield`` included. The release at exit
    is the loop's last, so it closes every host the fixture's own teardown
    left registered, within the run policy's teardown deadline. *label* names
    the loop in that sweep's log line, e.g. ``"test_docker_build.py's loop"``.
    """
    from otto import invocation

    boundary = invocation.acquire_boundary(
        asyncio.get_running_loop(), deadline=invocation.current_policy().teardown_deadline
    )
    try:
        yield
    finally:
        await boundary.release(label=label)
