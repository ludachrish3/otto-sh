"""Which pytest-asyncio loop a test runs on, and closing each loop's hosts before it ends.

pytest-asyncio runs every async test and fixture on a loop held by one of its
``_<scope>_scoped_runner`` fixtures. A host's connection belongs to the loop
that opened it, so each of those loops must close its own hosts while it still
runs: afterwards nothing can close them gracefully. :class:`~otto.suite.plugin.OttoPlugin`
names each runner's loop, holds a cleanup boundary on it for the runner's
scope, and shuts its host registry down just before the runner closes it, and
otto's per-test work (the ``ensure`` converge and the monitor events) runs on
the runner of the test's own loop through :func:`runner_for`.
"""

import re
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from ..invocation import Boundary

RUNNER_FIXTURE = re.compile(r"_(function|class|module|package|session)_scoped_runner")
"""The names of pytest-asyncio's runner fixtures; group 1 is the loop scope."""


def item_loop_scope(item: pytest.Item) -> str:
    """Return the loop scope *item*'s body runs on, by pytest-asyncio's own precedence.

    An async test runs on its closest ``asyncio`` marker's ``loop_scope``,
    else on ``asyncio_default_test_loop_scope``. A sync test has no loop of
    its own; otto's per-test work for it runs where an unpinned async fixture
    would, on ``asyncio_default_fixture_loop_scope``. Either default falls
    back to ``function``, as pytest-asyncio's does.
    """
    from pytest_asyncio import is_async_test

    if is_async_test(item):
        marker = item.get_closest_marker("asyncio")
        if marker is not None:
            # ``scope=`` is pytest-asyncio's deprecated spelling, still honoured there.
            scope = marker.kwargs.get("loop_scope") or marker.kwargs.get("scope")
            if scope:
                return str(scope)
        return item.config.getini("asyncio_default_test_loop_scope") or "function"
    return item.config.getini("asyncio_default_fixture_loop_scope") or "function"


def runner_for(request: pytest.FixtureRequest) -> Any:
    """Return the pytest-asyncio runner whose loop the requesting test runs on.

    The value is an ``asyncio.Runner`` (the ``backports.asyncio.runner``
    backport on Python 3.10): ``runner.run(coro)`` drives *coro* on that
    loop. Call it from a sync fixture, never from inside a running loop.
    """
    return request.getfixturevalue(f"_{item_loop_scope(request.node)}_scoped_runner")


def runner_label(scope: str, request: pytest.FixtureRequest) -> str:
    """Return a human name for the loop a runner fixture of *scope* serves.

    The name is what otto's loop-end debug line and a
    :class:`~otto.host.loop_owner.HostLoopError` say:

    - ``the session's loop`` for the session;
    - ``TestRouter's loop`` for a class;
    - ``test_router.py's loop`` for a module, named by its file;
    - ``net's loop`` for a package, named by its directory, since a package
      node has no module file;
    - ``test_uplink's loop`` for one test. A class-scoped runner outside a
      class is named this way too: pytest sets up a class-scoped fixture
      per test for a plain function, so that loop serves one test.
    """
    if scope == "session":
        from ..host.loop_owner import SESSION_LOOP_LABEL

        return SESSION_LOOP_LABEL
    if scope == "class":
        cls = getattr(request, "cls", None)
        return f"{cls.__name__ if cls is not None else request.node.name}'s loop"
    if scope == "function":
        return f"{request.node.name}'s loop"
    return f"{request.node.path.name}'s loop"


def sweep_runner_loop(runner: Any, label: str, boundary: "Boundary") -> None:
    """Shut down the runner loop's host registry before the runner closes the loop.

    Closes every host registered on the loop, whatever boundaries are still
    held, then refuses new ones: a later acquisition raises and a later
    release closes nothing. Runs with or without an otto context. A runner
    whose loop is already closed has nothing left to close gracefully.
    Bounded by the deadline the runner's boundary recorded; a failed close is
    a warning.
    """
    try:
        loop = runner.get_loop()
    except RuntimeError:  # the runner is closed already
        return
    if loop.is_closed():
        return
    from ..invocation import shut_down

    runner.run(shut_down(loop, label=label, deadline=boundary.deadline))
