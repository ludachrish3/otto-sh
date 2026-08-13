"""Tier 1 stability tests for ``SessionManager`` — pure asyncio, no real transports.

Targets the three race hotspots in :mod:`otto.host.session`:
``_exec_pool``, ``_named_sessions`` get-or-create, and
``_ensure_session()`` default-session recreation.

The lock fixes these tests were written to diagnose have since landed, so
the module runs green; each test now stands as a regression guard for the
behaviour its own docstring describes.

No assertion here reads elapsed wall-clock time. Each one observes the
structural fact it is actually about — session identity, factory creation
counts, peak connect overlap, the backoff duration the retry really slept —
so a loaded gate cannot counterfeit a failure. Two of them used to infer
those facts from a clock, and one of those,
``test_exec_pool_connects_concurrently``, was actually counterfeited by load
in the nightly (issue #229); the other's bound was converted alongside it,
before it ever had a sighting.

One assertion's truth is still produced by a timer, and that timer is a
*stimulus*, not a discriminator:
``test_open_session_closes_session_when_init_cancelled`` expects the raise
from a 0.05 s ``wait_for``. Its fake never answers the readiness probe, so
the handshake cannot complete and the timeout is certain to fire; load
pushes that in the safe direction, never toward a false red.
"""

import asyncio
import re
from types import SimpleNamespace
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from otto.host.connections import ConnectionManager
from otto.host.session import _HANDSHAKE_RETRY_BACKOFF, SessionManager, ShellSession
from otto.result import CommandResult

pytestmark = pytest.mark.concurrency

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


def _make_mgr(factory: _Factory, term: str = "telnet") -> SessionManager:
    """Build a ``SessionManager`` wired to the factory.

    ``term='telnet'`` makes ``exec()`` go through ``_exec_pool``;
    SSH's ``exec`` bypasses the pool (uses asyncssh ``create_process``
    directly) and isn't relevant to these tests.
    """
    return SessionManager(
        # SimpleNamespace duck-types ConnectionManager — we only need `.term`
        # because session_factory short-circuits the connection-based paths.
        connections=cast("ConnectionManager", SimpleNamespace(term=term)),
        session_factory=factory,
    )


class _ConnectOverlapTracker:
    """Records the peak number of ``_open()`` calls in flight simultaneously.

    One tracker is shared by every session a :class:`_SlowConnectFactory`
    hands out, so ``peak`` is the largest number of pool connects that were
    ever running at the same moment across the whole fan-out. That count *is*
    the structural property the per-name-lock fix is about, so tests read it
    directly rather than inferring it from elapsed wall-clock time.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)

    def leave(self) -> None:
        self.in_flight -= 1


class _SlowConnectFakeSession(_StabilityFakeSession):
    """Fake session with a configurable, non-trivial ``_open()`` delay.

    Real telnet ``exec()`` pool sessions spend ~1-2 s in the connect
    handshake, and sleeping for ``connect_delay`` inside ``_open()`` keeps the
    fake faithful to that: each concurrent connect is parked on a real timer
    for the width of a handshake, the way the real ones are.

    The delay is *not* what makes the overlap visible, which is worth stating
    because the timing-era docstring here used to claim it was.  Measured 15
    runs each way, a ``connect_delay`` of 0.1 s and of 0 both report a peak
    overlap of 10 under per-name locks and 1 under a single shared lock — the
    tracker reads a structural fact, and structural facts do not need a
    stopwatch-sized window to be legible.  The delay stays because it is the
    realistic stimulus, not because the assertion depends on it.

    Entry and exit are reported to a shared :class:`_ConnectOverlapTracker`;
    the exit is in a ``finally`` so a failed connect cannot leave the in-flight
    count permanently inflated.
    """

    connect_delay: float = 0.1

    def __init__(self, instance_id: int, overlap: _ConnectOverlapTracker) -> None:
        super().__init__(instance_id)
        self.overlap = overlap

    async def _open(self) -> None:
        self.overlap.enter()
        try:
            await asyncio.sleep(self.connect_delay)
        finally:
            self.overlap.leave()


class _SlowConnectFactory(_Factory):
    """``_Factory`` variant that hands out :class:`_SlowConnectFakeSession`.

    Owns the one :class:`_ConnectOverlapTracker` every session it creates
    reports to, so ``factory.overlap.peak`` is the peak connect overlap over
    all the sessions this factory produced.
    """

    def __init__(self) -> None:
        super().__init__()
        self.overlap = _ConnectOverlapTracker()

    def __call__(self) -> _StabilityFakeSession:
        session = _SlowConnectFakeSession(
            instance_id=len(self.created) + 1,
            overlap=self.overlap,
        )
        self.created.append(session)
        return session


# ── Targeted concurrency tests ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_exec_pool_connects_concurrently() -> None:
    """Concurrent ``exec()`` calls must connect their pool sessions in parallel.

    Regression for the telnet pool serialization bug: every concurrent
    ``exec()`` acquires a *uniquely named* ``__exec_pool_N__`` session
    via ``open_session()``.  When ``open_session()`` guarded the whole
    get-or-create body (including the slow connect) with a single shared
    lock, those N connects ran one after another — N short execs took
    ``N x connect_delay`` instead of ~one ``connect_delay``.  On real telnet
    hosts that turned 10 parallel execs into a ~16 s serial chain and
    blew the 15 s budget in ``test_real_long_telnet_exec_vs_concurrent``.

    With per-name locks, distinct names connect concurrently.

    The assertion observes that overlap *directly* — the peak number of
    ``_open()`` calls in flight at once — rather than inferring it from how
    long the fan-out took.  That is load-immune, and on the axis this test
    exists to guard, connect serialization, it strictly dominates the
    ``elapsed < 0.5 s`` bound it replaced (issue #229).  Measured by handing
    the manager k locks shared across the ten pool names, the old bound only
    fired once concurrency had collapsed to two-way or worse (k=2: 0.531 s,
    caught) — partial regressions sailed through it (k=5: 0.211 s, k=4:
    0.319 s, k=3: 0.428 s, all under 0.5 s).  Every one of those fails
    ``peak == N`` immediately.  So the clock was not merely flaky, it was
    also blind to most of the shapes it was there to catch.

    Dominance is claimed on that axis only, not in general: ``peak`` sees
    only the connect phase, while ``elapsed`` bounded the whole fan-out.  A
    regression that added wall-clock cost *outside* ``_open()`` — a
    re-introduced per-exec settle, say — would have tripped the old bound and
    passes this one.  Nothing here guards that; it is not what this test is
    for.
    """
    factory = _SlowConnectFactory()
    mgr = _make_mgr(factory)

    N = 10  # noqa: N806 — single-letter math dimension

    results = await asyncio.gather(*(mgr.exec(f"echo {i}") for i in range(N)))

    assert all(r.status.is_ok for r in results), "some execs returned non-ok status"
    # A peak of N already implies at least N sessions existed (one _open()
    # in flight each). This pins the other side: exactly one pool session per
    # exec, no surplus builds.
    assert factory.created_count == N, (
        f"expected {N} pool sessions to be built, got {factory.created_count}"
    )
    assert factory.overlap.peak == N, (
        f"peak concurrent _open() was {factory.overlap.peak}, expected {N} — "
        f"pool connects serialized instead of running in parallel (a single "
        f"shared lock around the get-or-create body pins the peak at 1)"
    )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_exec_pool_high_fanout() -> None:
    """200 concurrent ``exec()`` calls must not corrupt ``_exec_pool``.

    Catches: ``IndexError`` from concurrent pop on a draining pool,
    duplicate sessions returned to multiple in-flight callers, dead
    sessions left in the pool after drain.
    """
    factory = _Factory()
    mgr = _make_mgr(factory)

    N = 200  # noqa: N806 — single-letter math dimension
    results = await asyncio.gather(
        *(mgr.exec(f"echo {i}") for i in range(N)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"{len(exceptions)} exec() calls raised; first: {exceptions[0]!r}"
    statuses = cast("list[CommandResult]", results)
    assert all(r.status.is_ok for r in statuses), "some execs returned non-ok status"

    # Every session left in the pool should still be alive.
    dead_in_pool = [s for s in mgr._exec_pool if not s.alive]
    assert not dead_in_pool, f"{len(dead_in_pool)} dead session(s) left in pool"

    # No duplicates in the pool — would indicate a session was returned twice.
    pool_ids = [id(s) for s in mgr._exec_pool]
    assert len(pool_ids) == len(set(pool_ids)), "duplicate session detected in pool"

    # Pool size must not exceed factory creation count (sanity).
    assert len(mgr._exec_pool) <= factory.created_count

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
    handle = await mgr.open_session("router1")
    await handle.run("echo init")
    assert handle.alive

    # Simulate transport death without going through close().
    handle._session._alive = False

    # Fan-out: 30 concurrent open_session calls on the same name.
    sessions = await asyncio.gather(
        *(mgr.open_session("router1") for _ in range(30)),
    )

    # All callers should resolve to a single underlying ShellSession.
    underlying_ids = {id(hs._session) for hs in sessions}
    assert len(underlying_ids) == 1, (
        f"{len(underlying_ids)} distinct ShellSession instances handed out "
        f"for one name — replacement was not unique"
    )

    # The dict should hold exactly one entry for the name.
    assert list(mgr._named_sessions.keys()) == ["router1"]

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

    The race this was written to diagnose: ``_ensure_session()`` used to run
    an unguarded ``await self._session.close()`` between its alive-check and
    the recreate path, and the fake's ``close()`` yields to the event loop, so
    every task could pass the guard and every task could build a new session.

    That is fixed — the whole recreate body now runs under
    ``self._ensure_session_lock``, which re-checks liveness after acquiring
    and only then closes the dead session. This test stands as the regression
    guard: it asserts the creation count, not a timing, so it fails if the
    lock or the re-check is ever removed.
    """
    factory = _Factory()
    mgr = _make_mgr(factory)

    # Create + warm the default session.
    await mgr.run_cmd("echo init", timeout=5.0)
    assert factory.created_count == 1
    initial = mgr._session
    assert initial is not None
    assert initial.alive
    initial._alive = False

    M = 50  # noqa: N806 — single-letter math dimension
    results = await asyncio.gather(
        *(mgr.run_cmd(f"echo {i}", timeout=5.0) for i in range(M)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"{len(exceptions)} run_cmd calls raised; first: {exceptions[0]!r}"
    statuses = cast("list[CommandResult]", results)
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

_OPS = ["open_a", "open_b", "exec", "run_default", "kill_default", "kill_a", "kill_b", "close_a"]


async def _exec_ops(ops: list[str]) -> None:
    factory = _Factory()
    mgr = _make_mgr(factory)
    try:
        for op in ops:
            if op == "open_a":
                await mgr.open_session("A")
            elif op == "open_b":
                await mgr.open_session("B")
            elif op == "exec":
                await mgr.exec("echo")
            elif op == "run_default":
                await mgr.run_cmd("echo", timeout=5.0)
            elif op == "kill_default" and mgr._session is not None:
                mgr._session._alive = False
            elif op == "kill_a" and "A" in mgr._named_sessions:
                mgr._named_sessions["A"]._session._alive = False
            elif op == "kill_b" and "B" in mgr._named_sessions:
                mgr._named_sessions["B"]._session._alive = False
            elif op == "close_a" and "A" in mgr._named_sessions:
                await mgr._named_sessions["A"].close()

            # Invariant: no *user-named* session lives in the exec pool.
            # The pool holds HostSessions registered under `__exec_pool_N__`
            # keys (this is by design — exec reuses open_session for the
            # creation path), so those don't count as a violation.
            pool_shells = {id(hs._session) for hs in mgr._exec_pool}
            user_named_shells = {
                id(hs._session)
                for name, hs in mgr._named_sessions.items()
                if not name.startswith("__exec_pool_")
            }
            overlap = pool_shells & user_named_shells
            assert not overlap, (
                f"after {op!r}: user-named ShellSession id(s) {overlap} also appear in _exec_pool"
            )

            # Invariant: factory creation is bounded by total operations + 1
            # (the +1 covers the initial default session's lazy creation).
            assert factory.created_count <= len(ops) + 2 + len(mgr._exec_pool), (
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
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=factory,
    )
    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(mgr.open_session("x"), timeout=0.05)

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
        if self._ready_marker in data and self.instance_id <= self.fail_until_instance:
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
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=make_session,
        # Skip the real ~2 s peer-release backoff — these fakes have no peer to
        # wait on, and paying it burns 40% of the tight timeout(5) budget,
        # making the test flake under CI teardown load. See
        # test_ensure_session_retry_backoff_is_configurable.
        retry_backoff=0.0,
    )

    # Single run_cmd: first session fails its handshake, retry builds a
    # fresh session that completes. The caller observes a success.
    result = await mgr.run_cmd("echo hello", timeout=2.0)
    assert result.status.is_ok, f"expected success after retry, got {result!r}"
    assert factory.created_count == 2, (
        f"expected exactly 2 session builds (1 failed + 1 retry), got {factory.created_count}"
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
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=make_session,
        # Skip the real ~2 s peer-release backoff (no peer here) — it otherwise
        # eats most of the timeout(5) budget and flakes under CI teardown load.
        retry_backoff=0.0,
    )

    with pytest.raises(ConnectionError):
        await mgr.run_cmd("echo hello", timeout=2.0)
    assert factory.created_count == 2, (
        f"expected exactly 2 attempts before giving up, got {factory.created_count}"
    )

    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_ensure_session_retry_backoff_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inter-attempt retry backoff is injectable, so tests don't pay the
    production ``_HANDSHAKE_RETRY_BACKOFF`` peer-release wait. The sibling
    tests above pass ``retry_backoff=0.0`` on the strength of that; this test
    is what makes that safe to rely on.

    It asserts the *granted value* rather than the race outcome: every
    duration handed to ``asyncio.sleep`` while the retry runs is recorded, and
    the injected backoff must appear exactly once (the single retry) with the
    production default never slept at all. An elapsed-time bound would only
    say "this finished quickly", which load can counterfeit, and a check of
    ``mgr._retry_backoff`` would pass against a manager that stored the value
    and then ignored it.

    The injected value is deliberately non-zero and distinctive so it cannot
    be confused with the ``asyncio.sleep(0)`` yields the fake transports make
    on every open and close. The default-never-slept assertion reads that same
    shared list, so it rests on ``_HANDSHAKE_RETRY_BACKOFF`` colliding with
    neither; that precondition is asserted rather than assumed.
    """
    injected_backoff = 0.037
    # Precondition, not a product claim. The two assertions below that read
    # ``slept`` share one list, which also carries the fakes' sleep(0) yields,
    # so if the production default were ever tuned to 0.0 — or to the sentinel
    # — the last of them would become a guaranteed red carrying a false
    # explanation. Fail here instead, saying what actually needs fixing.
    assert _HANDSHAKE_RETRY_BACKOFF not in (0.0, injected_backoff), (
        f"test precondition broken: _HANDSHAKE_RETRY_BACKOFF is now "
        f"{_HANDSHAKE_RETRY_BACKOFF}, which collides with the fakes' sleep(0) "
        f"yields or with the injected {injected_backoff}s sentinel. If it "
        f"collided with the sentinel, choose a fresh injected_backoff so the "
        f"two stay distinguishable; if the default is now 0.0, no sentinel "
        f"helps — the default-never-slept assertion cannot tell a 0.0 default "
        f"from the ambient yields and must be restructured or dropped."
    )
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float, *args: object, **kwargs: object) -> object:
        # Delegate to the real sleep so the retry path's behaviour — and the
        # ordering it depends on — is entirely unchanged; only observed.
        slept.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)

    factory = _Factory()

    def make_session() -> _HandshakeFailsOnceFakeSession:
        session = _HandshakeFailsOnceFakeSession(
            instance_id=len(factory.created) + 1,
        )
        session.fail_until_instance = 999  # always fail
        factory.created.append(session)
        return session

    mgr = SessionManager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=make_session,
        retry_backoff=injected_backoff,
    )

    with pytest.raises(ConnectionError):
        await mgr.run_cmd("echo hello", timeout=2.0)

    assert factory.created_count == 2, f"expected exactly 2 attempts, got {factory.created_count}"
    assert slept.count(injected_backoff) == 1, (
        f"the single retry did not sleep the injected {injected_backoff}s backoff "
        f"exactly once — durations passed to asyncio.sleep: {slept}"
    )
    assert _HANDSHAKE_RETRY_BACKOFF not in slept, (
        f"the retry slept the {_HANDSHAKE_RETRY_BACKOFF}s production default "
        f"instead of the injected {injected_backoff}s — the parameter is stored "
        f"but not used; durations passed to asyncio.sleep: {slept}"
    )

    await mgr.close_all()
