"""The ``wait_for`` / ``wait_for_async`` deadline-poll primitive (gate G6).

Every timing assertion here runs on a fake clock: ``time.monotonic`` /
``time.sleep`` (and the async twin's loop clock / ``asyncio.sleep``) are
replaced with a virtual clock that advances only when the helper sleeps, so
probe schedules are asserted exactly — no real waiting, no tolerance windows.
"""

import pytest

import otto.utils as utils_mod
from otto.utils import WaitTimeoutError, wait_for, wait_for_async


class FakeClock:
    """Virtual monotonic clock: ``sleep`` advances ``now``; every sleep is logged."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class FakeAsyncio:
    """Stand-in for utils' ``asyncio`` reference: loop clock + sleep on a FakeClock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def get_running_loop(self) -> "FakeAsyncio":
        return self

    def time(self) -> float:
        return self._clock.now

    async def sleep(self, duration: float) -> None:
        self._clock.sleep(duration)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(utils_mod, "time", fake)
    return fake


@pytest.fixture
def async_clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(utils_mod, "asyncio", FakeAsyncio(fake))
    return fake


def test_immediate_success_probes_once_and_never_sleeps(clock):
    probes: list[float] = []

    def ready() -> bool:
        probes.append(clock.now)
        return True

    wait_for(ready, 5.0, on_timeout="unreachable")
    assert probes == [0.0]
    assert clock.sleeps == []


def test_check_first_probe_schedule(clock):
    probes: list[float] = []

    def ready() -> bool:
        probes.append(clock.now)
        return clock.now >= 0.3

    wait_for(ready, 5.0, interval=0.1, on_timeout="unreachable")
    assert probes == [0.0, 0.1, 0.2, pytest.approx(0.3)]
    assert clock.now == pytest.approx(0.3)


def test_timeout_caps_final_sleep_and_probes_at_the_edge(clock):
    probes: list[float] = []

    def never() -> bool:
        probes.append(clock.now)
        return False

    with pytest.raises(WaitTimeoutError, match=r"^condition never held$"):
        wait_for(never, 0.25, interval=0.1, on_timeout="condition never held")
    # Final sleep is capped to the remaining 0.05 s and the deadline edge is
    # probed once more: total wall time is exactly the timeout, not
    # timeout-rounded-up-to-a-whole-interval.
    assert probes == [0.0, 0.1, pytest.approx(0.2), pytest.approx(0.25)]
    assert clock.sleeps == [0.1, 0.1, pytest.approx(0.05)]
    assert clock.now == pytest.approx(0.25)


def test_edge_probe_success_beats_the_timeout(clock):
    # A predicate that turns true exactly at the deadline is a success, not a
    # TimeoutError — the capped final sleep's probe must be consulted.
    wait_for(lambda: clock.now >= 0.25, 0.25, interval=0.1, on_timeout="unreachable")
    assert clock.now == pytest.approx(0.25)


def test_sleep_first_never_probes_at_t0(clock):
    probes: list[float] = []

    def ready() -> bool:
        probes.append(clock.now)
        return True

    wait_for(ready, 5.0, interval=0.1, probe_first=False, on_timeout="unreachable")
    assert probes == [pytest.approx(0.1)]


def test_sleep_first_with_timeout_below_interval_still_probes_once(clock):
    probes: list[float] = []

    def never() -> bool:
        probes.append(clock.now)
        return False

    with pytest.raises(TimeoutError):
        wait_for(never, 0.04, interval=0.1, probe_first=False, on_timeout="nope")
    assert probes == [pytest.approx(0.04)]


def test_exhausted_budget_with_probe_first_gets_exactly_one_probe(clock):
    # The do-while contract (ported from unix_host's recovery gate): a budget
    # that is already spent when we get here must not fail unprobed.
    probes: list[float] = []
    wait_for(lambda: (probes.append(clock.now), True)[1], 0.0, on_timeout="unreachable")
    assert probes == [0.0]

    with pytest.raises(TimeoutError):
        wait_for(lambda: (probes.append(clock.now), False)[1], 0.0, on_timeout="spent")
    assert probes == [0.0, 0.0]
    assert clock.sleeps == []


def test_exhausted_budget_without_probe_first_raises_unprobed(clock):
    with pytest.raises(TimeoutError):
        wait_for(lambda: pytest.fail("must not probe"), 0.0, probe_first=False, on_timeout="spent")


def test_on_timeout_callable_renders_lazily_with_captured_state(clock):
    renders: list[int] = []
    last_seen = ""

    def probing() -> bool:
        nonlocal last_seen
        last_seen = f"state@{clock.now}"
        return False

    def render() -> str:
        renders.append(1)
        return f"gave up; last: {last_seen}"

    with pytest.raises(TimeoutError, match=r"^gave up; last: state@0\.2"):
        wait_for(probing, 0.2, interval=0.1, on_timeout=render)
    assert renders == [1]

    # And never rendered on the success path.
    wait_for(lambda: True, 1.0, on_timeout=lambda: renders.append(2) or "unused")
    assert renders == [1]


def test_interval_callable_ramps_by_sleep_index(clock):
    with pytest.raises(TimeoutError):
        wait_for(
            lambda: False,
            1.0,
            interval=lambda i: 0.05 if i < 2 else 0.2,
            on_timeout="ramped out",
        )
    # Two fast sleeps, then the slow interval until the capped final sleep.
    assert clock.sleeps[:3] == [0.05, 0.05, 0.2]
    assert clock.sleeps[-1] == pytest.approx(1.0 - 0.05 - 0.05 - 0.2 * 4)
    assert clock.now == pytest.approx(1.0)


def test_predicate_exception_propagates_unchanged(clock):
    def dead_child() -> bool:
        raise RuntimeError("child exited before the marker")

    with pytest.raises(RuntimeError, match=r"^child exited before the marker$"):
        wait_for(dead_child, 5.0, on_timeout="unreachable")


def test_sync_twin_refuses_an_async_predicate(clock):
    # A coroutine object is truthy — without the guard this would "succeed"
    # instantly without ever evaluating the condition.
    async def async_probe() -> bool:
        return False

    with pytest.raises(TypeError, match="use wait_for_async"):
        wait_for(async_probe, 5.0, on_timeout="unreachable")
    assert clock.sleeps == []


def test_expiry_type_is_distinguishable_from_a_predicate_timeout(clock):
    # Expiry raises the dedicated subclass; a TimeoutError raised BY the
    # predicate propagates as-is, so `except WaitTimeoutError` around a wait
    # can never swallow a probe's own timeout.
    with pytest.raises(WaitTimeoutError):
        wait_for(lambda: False, 0.1, on_timeout="expired")

    def probe_times_out() -> bool:
        raise TimeoutError("from the predicate")

    with pytest.raises(TimeoutError, match=r"^from the predicate$") as excinfo:
        wait_for(probe_times_out, 5.0, on_timeout="unreachable")
    assert not isinstance(excinfo.value, WaitTimeoutError)


def test_nan_timeout_is_rejected_not_an_infinite_loop(clock):
    # NaN defeats both the expiry comparison and the sleep cap (min() picks
    # the interval), so an unvalidated NaN would poll forever.
    with pytest.raises(ValueError, match="NaN"):
        wait_for(lambda: True, float("nan"), on_timeout="unreachable")
    assert clock.sleeps == []


def test_negative_and_nan_intervals_rejected_zero_allowed(clock):
    with pytest.raises(ValueError, match="interval must be non-negative"):
        wait_for(lambda: False, 1.0, interval=-0.1, on_timeout="unreachable")
    with pytest.raises(ValueError, match="interval must be non-negative"):
        wait_for(lambda: False, 1.0, interval=float("nan"), on_timeout="unreachable")

    # A ramp callable is validated per returned value, naming the sleep index.
    with pytest.raises(ValueError, match=r"got -1 \(sleep 2\)"):
        wait_for(
            lambda: False,
            10.0,
            interval=lambda i: 0.1 if i < 2 else -1,
            on_timeout="unreachable",
        )

    # Zero is legal — sleep(0) is a yield, the tight-poll spelling that
    # mock-backed callers (poll_interval=0) rely on.
    flips = iter([False, False, True])
    wait_for(lambda: next(flips), 1.0, interval=0, on_timeout="unreachable")
    assert clock.sleeps[-2:] == [0, 0]


@pytest.mark.asyncio
async def test_async_schedule_and_awaitable_predicate(async_clock):
    probes: list[float] = []

    async def ready() -> bool:
        probes.append(async_clock.now)
        return async_clock.now >= 0.2

    await wait_for_async(ready, 5.0, interval=0.1, on_timeout="unreachable")
    assert probes == [0.0, 0.1, pytest.approx(0.2)]


@pytest.mark.asyncio
async def test_async_accepts_plain_bool_predicate_and_times_out(async_clock):
    probes: list[float] = []

    def never() -> bool:
        probes.append(async_clock.now)
        return False

    with pytest.raises(TimeoutError, match=r"^async never held$"):
        await wait_for_async(never, 0.25, interval=0.1, on_timeout="async never held")
    assert probes == [0.0, 0.1, pytest.approx(0.2), pytest.approx(0.25)]
    assert async_clock.now == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_async_sleep_first(async_clock):
    probes: list[float] = []

    async def ready() -> bool:
        probes.append(async_clock.now)
        return True

    await wait_for_async(ready, 5.0, interval=0.1, probe_first=False, on_timeout="unreachable")
    assert probes == [pytest.approx(0.1)]


@pytest.mark.asyncio
async def test_async_interval_callable_ramps_by_sleep_index(async_clock):
    # The ramp's only production user (_wait_for_remote_listener) is async, so
    # the sleep-index plumbing is pinned on this twin too — the loops are
    # duplicated bodies, and the sync test alone would not catch a dropped
    # `sleeps += 1` here.
    with pytest.raises(WaitTimeoutError):
        await wait_for_async(
            lambda: False,
            1.0,
            interval=lambda i: 0.05 if i < 2 else 0.2,
            on_timeout="ramped out",
        )
    assert async_clock.sleeps[:3] == [0.05, 0.05, 0.2]
    assert async_clock.now == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_async_exhausted_budget_probe_first_edges(async_clock):
    # The motivating _confirm_recovered case is async: a spent budget still
    # gets exactly one probe, and its verdict decides.
    probes: list[float] = []

    async def up() -> bool:
        probes.append(async_clock.now)
        return True

    await wait_for_async(up, 0.0, on_timeout="unreachable")
    assert probes == [0.0]

    async def down() -> bool:
        probes.append(async_clock.now)
        return False

    with pytest.raises(WaitTimeoutError):
        await wait_for_async(down, 0.0, on_timeout="spent")
    assert probes == [0.0, 0.0]
    assert async_clock.sleeps == []


@pytest.mark.asyncio
async def test_async_predicate_exception_and_lazy_on_timeout(async_clock):
    async def dead() -> bool:
        raise RuntimeError("async child died")

    with pytest.raises(RuntimeError, match=r"^async child died$"):
        await wait_for_async(dead, 5.0, on_timeout="unreachable")

    renders: list[int] = []
    with pytest.raises(WaitTimeoutError, match=r"^async gave up at 0\.2"):
        await wait_for_async(
            lambda: False,
            0.2,
            interval=0.1,
            on_timeout=lambda: renders.append(1) or f"async gave up at {async_clock.now}",
        )
    assert renders == [1]


@pytest.mark.asyncio
async def test_async_nan_timeout_and_bad_interval_rejected(async_clock):
    with pytest.raises(ValueError, match="NaN"):
        await wait_for_async(lambda: True, float("nan"), on_timeout="unreachable")

    # asyncio.sleep would silently accept a negative and busy-spin; the shared
    # validation makes the async twin as loud as the sync one.
    with pytest.raises(ValueError, match="interval must be non-negative"):
        await wait_for_async(lambda: False, 1.0, interval=-0.5, on_timeout="unreachable")


@pytest.mark.asyncio
async def test_async_real_loop_smoke():
    # One un-mocked round trip on the real loop: a condition that turns true
    # after the first real sleep, with a real (tiny) interval.
    flips = iter([False, True])
    await wait_for_async(lambda: next(flips), 1.0, interval=0.001, on_timeout="unreachable")

    with pytest.raises(TimeoutError):
        await wait_for_async(lambda: False, 0.01, interval=0.002, on_timeout="real-loop timeout")
