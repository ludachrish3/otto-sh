"""Tier 1 stability tests for ``SessionManager`` — pure asyncio, no real transports.

Targets the three race hotspots in :mod:`otto.host.session`:
``_oneshot_pool``, ``_named_sessions`` get-or-create, and
``_ensure_session()`` default-session recreation.

Tests are expected to land RED until lock fixes are applied to
``SessionManager``; the failures are the diagnosis.
"""

import asyncio
import re
from types import SimpleNamespace
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from otto.host.connections import ConnectionManager
from otto.host.session import SessionManager, ShellSession
from otto.utils import CommandStatus


# ── Fake session + factory ────────────────────────────────────────────────────

class _StabilityFakeSession(ShellSession):
    """``ShellSession`` that simulates real transport timing.

    ``_open()`` and ``close()`` yield to the event loop so concurrent tasks
    can interleave at realistic points. Every command succeeds with
    retcode 0; this module probes the manager, not command flow.
    """

    def __init__(self, instance_id: int) -> None:
        super().__init__()
        self.instance_id = instance_id
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _open(self) -> None:
        # Yield to mimic real transport setup (TCP+auth in ssh/telnet).
        await asyncio.sleep(0)

    async def _write(self, data: str) -> None:
        if self._ready_marker in data:
            self._read_queue.put_nowait(f"{self._ready_marker}\n")
        elif self._begin_marker in data:
            self._read_queue.put_nowait(f"{self._begin_marker}\n")
            self._read_queue.put_nowait(f"{self._end_marker_prefix}0__\n")

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        buf = ""
        while True:
            chunk = await self._read_queue.get()
            buf += chunk
            if pattern.search(buf):
                return buf

    async def close(self) -> None:
        # Yield to mimic real transport teardown — this is the await point
        # that opens the `_ensure_session()` race window.
        await asyncio.sleep(0)
        self._alive = False
        self._initialized = False


class _Factory:
    """Counting session factory — each call returns a fresh fake."""

    def __init__(self) -> None:
        self.created: list[_StabilityFakeSession] = []

    def __call__(self) -> _StabilityFakeSession:
        session = _StabilityFakeSession(instance_id=len(self.created) + 1)
        self.created.append(session)
        return session

    @property
    def created_count(self) -> int:
        return len(self.created)


def _make_mgr(factory: _Factory, term: str = 'telnet') -> SessionManager:
    """Build a ``SessionManager`` wired to the factory.

    ``term='telnet'`` makes ``oneshot()`` go through ``_oneshot_pool``;
    SSH's ``oneshot`` bypasses the pool (uses asyncssh ``create_process``
    directly) and isn't relevant to these tests.
    """
    return SessionManager(
        # SimpleNamespace duck-types ConnectionManager — we only need `.term`
        # because session_factory short-circuits the connection-based paths.
        connections=cast(ConnectionManager, SimpleNamespace(term=term)),
        session_factory=factory,
    )


class _SlowConnectFakeSession(_StabilityFakeSession):
    """Fake session with a configurable, non-trivial ``_open()`` delay.

    Real telnet ``oneshot()`` pool sessions spend ~1-2 s in the connect
    handshake.  A fake that connects instantly can't tell a manager that
    connects pool sessions *concurrently* from one that connects them
    *serially* — both finish in ~0 s.  This fake makes that distinction
    observable by sleeping for ``connect_delay`` inside ``_open()``.
    """

    connect_delay: float = 0.1

    async def _open(self) -> None:
        await asyncio.sleep(self.connect_delay)


class _SlowConnectFactory(_Factory):
    """``_Factory`` variant that hands out :class:`_SlowConnectFakeSession`."""

    def __call__(self) -> _StabilityFakeSession:
        session = _SlowConnectFakeSession(instance_id=len(self.created) + 1)
        self.created.append(session)
        return session


# ── Targeted concurrency tests ────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_oneshot_pool_connects_concurrently() -> None:
    """Concurrent ``oneshot()`` calls must connect their pool sessions in parallel.

    Regression for the telnet pool serialization bug: every concurrent
    ``oneshot()`` acquires a *uniquely named* ``__oneshot_pool_N__`` session
    via ``open_session()``.  When ``open_session()`` guarded the whole
    get-or-create body (including the slow connect) with a single shared
    lock, those N connects ran one after another — N short oneshots took
    ``N × connect_delay`` instead of ~one ``connect_delay``.  On real telnet
    hosts that turned 10 parallel oneshots into a ~16 s serial chain and
    blew the 15 s budget in ``test_real_long_telnet_oneshot_vs_concurrent``.

    With per-name locks, distinct names connect concurrently.
    """
    factory = _SlowConnectFactory()
    mgr = _make_mgr(factory)

    N = 10
    delay = _SlowConnectFakeSession.connect_delay

    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await asyncio.gather(*(mgr.oneshot(f'echo {i}') for i in range(N)))
    elapsed = loop.time() - start

    assert all(r.status.is_ok for r in results), "some oneshots returned non-ok status"
    # Serial connects would take >= N * delay.  Parallel connects take ~delay;
    # allow generous slack for scheduling/event-loop overhead but stay well
    # below the serial figure.
    assert elapsed < (N * delay) / 2, (
        f"{N} concurrent oneshots took {elapsed:.2f}s with a {delay:.2f}s "
        f"per-connect delay — pool connects serialized instead of running "
        f"in parallel (serial would be ~{N * delay:.2f}s)"
    )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_oneshot_pool_high_fanout() -> None:
    """200 concurrent ``oneshot()`` calls must not corrupt ``_oneshot_pool``.

    Catches: ``IndexError`` from concurrent pop on a draining pool,
    duplicate sessions returned to multiple in-flight callers, dead
    sessions left in the pool after drain.
    """
    factory = _Factory()
    mgr = _make_mgr(factory)

    N = 200
    results = await asyncio.gather(
        *(mgr.oneshot(f'echo {i}') for i in range(N)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"{len(exceptions)} oneshot() calls raised; first: {exceptions[0]!r}"
    statuses = cast(list[CommandStatus], results)
    assert all(r.status.is_ok for r in statuses), "some oneshots returned non-ok status"

    # Every session left in the pool should still be alive.
    dead_in_pool = [s for s in mgr._oneshot_pool if not s.alive]
    assert not dead_in_pool, f"{len(dead_in_pool)} dead session(s) left in pool"

    # No duplicates in the pool — would indicate a session was returned twice.
    pool_ids = [id(s) for s in mgr._oneshot_pool]
    assert len(pool_ids) == len(set(pool_ids)), "duplicate session detected in pool"

    # Pool size must not exceed factory creation count (sanity).
    assert len(mgr._oneshot_pool) <= factory.created_count

    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_named_session_alive_check_race() -> None:
    """Concurrent ``open_session(name)`` after transport death must yield one replacement.

    Catches: multiple replacement sessions created and clobbered in
    ``_named_sessions``, leaving leaked instances that were never closed.
    """
    factory = _Factory()
    mgr = _make_mgr(factory)

    # Open + warm so the alive guard becomes meaningful.
    handle = await mgr.open_session('router1')
    await handle.run('echo init')
    assert handle.alive

    # Simulate transport death without going through close().
    handle._session._alive = False

    # Fan-out: 30 concurrent open_session calls on the same name.
    sessions = await asyncio.gather(
        *(mgr.open_session('router1') for _ in range(30)),
    )

    # All callers should resolve to a single underlying ShellSession.
    underlying_ids = {id(hs._session) for hs in sessions}
    assert len(underlying_ids) == 1, (
        f"{len(underlying_ids)} distinct ShellSession instances handed out "
        f"for one name — replacement was not unique"
    )

    # The dict should hold exactly one entry for the name.
    assert list(mgr._named_sessions.keys()) == ['router1']

    # Factory was called once for the original + once for the replacement = 2.
    # Anything more means the get-or-create race fired and created orphans.
    assert factory.created_count == 2, (
        f"factory.created_count={factory.created_count} (expected 2). "
        f"Surplus instances are orphans never bound to _named_sessions."
    )

    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_ensure_default_session_recreation_race() -> None:
    """Concurrent commands after default-session death must trigger one recreation.

    This is the highest-confidence test in this module: ``_ensure_session()``
    has an unguarded ``await self._session.close()`` between its alive-check
    and the recreate path, and the fake's ``close()`` yields to the event
    loop. Multiple tasks can all pass the guard and all create new sessions.
    """
    factory = _Factory()
    mgr = _make_mgr(factory)

    # Create + warm the default session.
    await mgr.run_cmd('echo init', timeout=5.0)
    assert factory.created_count == 1
    initial = mgr._session
    assert initial is not None and initial.alive
    initial._alive = False

    M = 50
    results = await asyncio.gather(
        *(mgr.run_cmd(f'echo {i}', timeout=5.0) for i in range(M)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"{len(exceptions)} run_cmd calls raised; first: {exceptions[0]!r}"
    statuses = cast(list[CommandStatus], results)
    assert all(r.status.is_ok for r in statuses), "some commands returned non-ok status"

    # Exactly one replacement: 1 initial + 1 = 2.
    extra = factory.created_count - 2
    assert extra == 0, (
        f"factory.created_count={factory.created_count} (expected 2). "
        f"_ensure_session race created {extra} extra default session(s) — "
        f"the await on close() let multiple tasks past the alive guard."
    )

    await mgr.close_all()


# ── Hypothesis property test ──────────────────────────────────────────────────

_OPS = ['open_a', 'open_b', 'oneshot', 'run_default',
        'kill_default', 'kill_a', 'kill_b', 'close_a']


async def _exec_ops(ops: list[str]) -> None:
    factory = _Factory()
    mgr = _make_mgr(factory)
    try:
        for op in ops:
            if op == 'open_a':
                await mgr.open_session('A')
            elif op == 'open_b':
                await mgr.open_session('B')
            elif op == 'oneshot':
                await mgr.oneshot('echo')
            elif op == 'run_default':
                await mgr.run_cmd('echo', timeout=5.0)
            elif op == 'kill_default' and mgr._session is not None:
                mgr._session._alive = False
            elif op == 'kill_a' and 'A' in mgr._named_sessions:
                mgr._named_sessions['A']._session._alive = False
            elif op == 'kill_b' and 'B' in mgr._named_sessions:
                mgr._named_sessions['B']._session._alive = False
            elif op == 'close_a' and 'A' in mgr._named_sessions:
                await mgr._named_sessions['A'].close()

            # Invariant: no *user-named* session lives in the oneshot pool.
            # The pool holds HostSessions registered under `__oneshot_pool_N__`
            # keys (this is by design — oneshot reuses open_session for the
            # creation path), so those don't count as a violation.
            pool_shells = {id(hs._session) for hs in mgr._oneshot_pool}
            user_named_shells = {
                id(hs._session)
                for name, hs in mgr._named_sessions.items()
                if not name.startswith('__oneshot_pool_')
            }
            overlap = pool_shells & user_named_shells
            assert not overlap, (
                f"after {op!r}: user-named ShellSession id(s) {overlap} also "
                f"appear in _oneshot_pool"
            )

            # Invariant: factory creation is bounded by total operations + 1
            # (the +1 covers the initial default session's lazy creation).
            assert factory.created_count <= len(ops) + 2 + len(mgr._oneshot_pool), (
                f"factory.created_count={factory.created_count} after "
                f"{op!r} — unbounded session growth"
            )
    finally:
        await mgr.close_all()


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(ops=st.lists(st.sampled_from(_OPS), min_size=3, max_size=20))
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_session_manager_property(ops: list[str]) -> None:
    """Random sequences of operations must preserve manager invariants."""
    await _exec_ops(ops)


# ── Cancellation during the readiness handshake ───────────────────────────────

class _NeverReadyFakeSession(_StabilityFakeSession):
    """Fake whose marker handshake never completes.

    Swallows the readiness probe instead of echoing the marker, so
    ``_ensure_initialized`` blocks until cancelled — letting a test land a
    cancellation *inside* the handshake. Records whether ``close()`` ran.
    """

    def __init__(self, instance_id: int) -> None:
        super().__init__(instance_id)
        self.closed = False

    async def _write(self, data: str) -> None:
        # Never enqueue the READY marker — the handshake stalls.
        pass

    async def close(self) -> None:
        self.closed = True
        await super().close()


@pytest.mark.asyncio
async def test_open_session_closes_session_when_init_cancelled() -> None:
    """A cancellation inside ``_ensure_initialized`` must not orphan the
    half-built session.

    Regression: with telnet's login drain removed, the ~1 s readiness
    handshake moved out of the cleanup-guarded ``connect()`` window. A
    caller-side ``wait_for`` cancellation landing in the handshake left the
    session — and, for telnet, its owned client socket — unclosed.
    """
    created: list[_NeverReadyFakeSession] = []

    def factory() -> _NeverReadyFakeSession:
        session = _NeverReadyFakeSession(instance_id=len(created) + 1)
        created.append(session)
        return session

    mgr = SessionManager(
        connections=cast(ConnectionManager, SimpleNamespace(term='telnet')),
        session_factory=factory,
    )
    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(mgr.open_session('x'), timeout=0.05)

    assert len(created) == 1
    assert created[0].closed, "open_session leaked the session on cancellation"


# ── Retry once on a failed readiness handshake ────────────────────────────────

class _HandshakeFailsOnceFakeSession(_StabilityFakeSession):
    """Fake whose first ``_ensure_initialized`` raises ``ConnectionError``
    (simulating ``_fail_init``); subsequent instances succeed normally.

    Selected per-instance via ``instance_id`` so a counting factory can
    produce one failing session followed by a healthy one — modeling the
    race the retry path addresses: a fresh telnet socket whose peer EOFs
    the marker handshake, where a second open lands cleanly."""

    fail_until_instance: int = 1

    async def _write(self, data: str) -> None:
        if (
            self._ready_marker in data
            and self.instance_id <= self.fail_until_instance
        ):
            raise ConnectionError(
                "shell never became ready after open — the device is "
                "unresponsive or login failed (e.g. bad credentials)"
            )
        await super()._write(data)


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_ensure_session_retries_once_on_handshake_failure() -> None:
    """A ``ConnectionError`` from the first ``_ensure_initialized`` triggers
    exactly one rebuild + retry; the second attempt's success becomes the
    caller's success. Regression guard for the fan-out race fix in
    ``SessionManager._ensure_session``."""
    factory = _Factory()

    def make_session() -> _HandshakeFailsOnceFakeSession:
        session = _HandshakeFailsOnceFakeSession(
            instance_id=len(factory.created) + 1,
        )
        factory.created.append(session)
        return session

    mgr = SessionManager(
        connections=cast(ConnectionManager, SimpleNamespace(term='telnet')),
        session_factory=make_session,
    )

    # Single run_cmd: first session fails its handshake, retry builds a
    # fresh session that completes. The caller observes a success.
    result = await mgr.run_cmd('echo hello', timeout=2.0)
    assert result.status.is_ok, f"expected success after retry, got {result!r}"
    assert factory.created_count == 2, (
        f"expected exactly 2 session builds (1 failed + 1 retry), got "
        f"{factory.created_count}"
    )

    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_ensure_session_propagates_persistent_handshake_failure() -> None:
    """If both attempts fail, ``_ensure_session`` propagates the
    ``ConnectionError`` rather than looping forever. Genuine "device
    unresponsive / bad credentials" must still surface."""
    factory = _Factory()

    def make_session() -> _HandshakeFailsOnceFakeSession:
        session = _HandshakeFailsOnceFakeSession(
            instance_id=len(factory.created) + 1,
        )
        session.fail_until_instance = 999  # always fail
        factory.created.append(session)
        return session

    mgr = SessionManager(
        connections=cast(ConnectionManager, SimpleNamespace(term='telnet')),
        session_factory=make_session,
    )

    with pytest.raises(ConnectionError):
        await mgr.run_cmd('echo hello', timeout=2.0)
    assert factory.created_count == 2, (
        f"expected exactly 2 attempts before giving up, got "
        f"{factory.created_count}"
    )

    await mgr.close_all()
