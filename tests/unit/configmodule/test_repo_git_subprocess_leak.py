"""Regression tests for subprocess transport leaks in ``Repo.runGitCommand``.

``Repo.commit`` and ``Repo.description`` invoke :meth:`Repo.runGitCommand`
via synchronous property access wrapped in :func:`asyncio.run`.  The
coroutine creates a temporary ``LocalHost`` that opens a persistent
``bash`` session.  If the ``LocalHost`` is not closed, its
:class:`asyncio.subprocess.Process` (and the underlying
``BaseSubprocessTransport``) outlives the event loop created by
``asyncio.run``.  When Python later garbage-collects the transport at
interpreter shutdown, its ``__del__`` calls ``loop.call_soon(...)`` on
the already-closed loop and prints ``RuntimeError: Event loop is closed``;
the same teardown also trips ``ThreadedChildWatcher`` with ``Loop ...
that handles pid NNN is closed`` warnings when the bash children are
reaped.

The interpreter-shutdown warnings are timing-dependent (they depend on
reference-cycle collection order and module teardown), so we don't test
them directly.  Instead we test the invariant that actually causes them:
after the ``Repo.commit`` / ``Repo.description`` properties return, there
must be no live :class:`asyncio.base_subprocess.BaseSubprocessTransport`
instances tied to a closed loop.
"""

import asyncio
import gc
import sys
from pathlib import Path

import pytest

from otto.configmodule.repo import Repo

# Python stores the base transport here on 3.10:
from asyncio.base_subprocess import BaseSubprocessTransport


def _live_subprocess_transports() -> list[BaseSubprocessTransport]:
    """Return every BaseSubprocessTransport currently alive in this process."""
    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, BaseSubprocessTransport)]


def _transport_loop(transport: BaseSubprocessTransport) -> asyncio.AbstractEventLoop | None:
    """Return the event loop a transport is bound to, if any."""
    return getattr(transport, '_loop', None)


def _describe_transport(t: BaseSubprocessTransport) -> str:
    loop = _transport_loop(t)
    closed = loop.is_closed() if loop is not None else None
    pid = getattr(getattr(t, '_proc', None), 'pid', None)
    return f'<transport pid={pid} loop_closed={closed} transport={t!r}>'


@pytest.fixture(autouse=True)
def _no_pre_existing_transport_leaks():
    """Fail loudly if another test left a transport alive before this one runs."""
    pre_existing = _live_subprocess_transports()
    # Only bark on transports tied to closed loops — active ones may belong
    # to the running test's asyncio loop.
    stale = [t for t in pre_existing if (l := _transport_loop(t)) is not None and l.is_closed()]
    if stale:
        pytest.fail(
            'Pre-existing leaked subprocess transports detected before test '
            f'started: {[_describe_transport(t) for t in stale]}'
        )
    yield


class TestRepoGitSubprocessLeak:
    """The properties on ``Repo`` that call git synchronously must not leak
    a bash subprocess transport tied to the transient ``asyncio.run`` loop.
    """

    def test_repo_commit_does_not_leak_transport(self):
        """``repo.commit`` must not leave a BaseSubprocessTransport alive
        whose loop has already been closed.

        This is the exact path ``otto monitor`` triggers at startup via
        :meth:`ConfigModule.logRepoCommits`.  A leaked transport here is
        what produces the ``RuntimeError: Event loop is closed`` traceback
        the user saw on Ctrl+C.
        """
        repo_path = Path(__file__).parent.parent.parent / 'repo1'
        repo = Repo(sutDir=repo_path)

        _ = repo.commit  # triggers asyncio.run(setCommitHash())

        # The asyncio.run loop is now closed.  Any BaseSubprocessTransport
        # still alive and bound to it is a leak that will fire
        # "RuntimeError: Event loop is closed" from __del__ at interpreter
        # shutdown.
        leaked = [
            t for t in _live_subprocess_transports()
            if (loop := _transport_loop(t)) is not None and loop.is_closed()
        ]
        assert not leaked, (
            'repo.commit leaked a subprocess transport bound to a closed '
            f'event loop: {[_describe_transport(t) for t in leaked]}'
        )

    def test_repo_description_does_not_leak_transport(self):
        """Same invariant for ``repo.description`` (the second ``asyncio.run``
        path in ``Repo``).
        """
        repo_path = Path(__file__).parent.parent.parent / 'repo1'
        repo = Repo(sutDir=repo_path)

        _ = repo.description

        leaked = [
            t for t in _live_subprocess_transports()
            if (loop := _transport_loop(t)) is not None and loop.is_closed()
        ]
        assert not leaked, (
            'repo.description leaked a subprocess transport bound to a closed '
            f'event loop: {[_describe_transport(t) for t in leaked]}'
        )

    def test_repeated_commit_access_does_not_accumulate_transports(self):
        """Guards against a partial fix that only closes the first call's
        LocalHost but keeps leaking on subsequent creations.
        """
        repo_path = Path(__file__).parent.parent.parent / 'repo1'

        for _ in range(3):
            repo = Repo(sutDir=repo_path)
            _ = repo.commit
            _ = repo.description

        leaked = [
            t for t in _live_subprocess_transports()
            if (loop := _transport_loop(t)) is not None and loop.is_closed()
        ]
        assert not leaked, (
            'Repeated Repo commit/description accesses leaked subprocess '
            f'transports: {[_describe_transport(t) for t in leaked]}'
        )
