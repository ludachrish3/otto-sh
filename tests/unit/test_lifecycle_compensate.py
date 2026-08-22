"""compensate(): rollback/undo runs to completion even when the caller is cancelled.

Chaos spec (shielded compensating actions): an interrupt mid-compensation
must not tear the rollback; a hung rollback is bounded by a deadline; both
paths re-raise the held cancellation once the compensation resolves.
Deterministic: expiry is driven by ``deadline=0`` (the call_later fires on
the next loop turn), never by wall-clock waits.
"""

import asyncio

import pytest

from otto.lifecycle import compensate


@pytest.mark.asyncio
async def test_result_passthrough_without_cancellation():
    async def rollback() -> str:
        return "undone"

    assert await compensate(rollback(), deadline=60.0, what="test rollback") == "undone"


@pytest.mark.asyncio
async def test_exception_passthrough_without_cancellation():
    async def rollback() -> None:
        raise ValueError("undo failed")

    with pytest.raises(ValueError, match="undo failed"):
        await compensate(rollback(), deadline=60.0, what="test rollback")


@pytest.mark.asyncio
async def test_cancellation_is_held_until_the_rollback_completes():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the cancel land in compensate's shield
    assert not done  # rollback still parked on its event — held, not torn, not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]  # the rollback finished BEFORE the cancellation re-raised


@pytest.mark.asyncio
async def test_second_cancellation_does_not_tear_the_rollback():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # a second cancel is the shield's whole point
    await asyncio.sleep(0)
    assert not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]


@pytest.mark.asyncio
async def test_deadline_abandons_a_hung_rollback(caplog):
    hung = asyncio.Event()  # never set

    async def rollback() -> None:
        await hung.wait()

    task = asyncio.ensure_future(compensate(rollback(), deadline=0.0, what="hung rollback"))
    await asyncio.sleep(0)  # rollback parked
    task.cancel()  # holds the cancel and arms the deadline; 0.0 fires next loop turn
    with (
        caplog.at_level("WARNING", logger="otto.lifecycle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert any("hung rollback" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_wins_over_a_late_rollback_failure(caplog):
    started = asyncio.Event()
    release = asyncio.Event()

    async def rollback() -> None:
        started.set()
        await release.wait()
        raise ValueError("undo failed late")

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="late failure"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with (
        caplog.at_level("WARNING", logger="otto.lifecycle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert any("late failure" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# timeout=: the caller's OWN bound, armed at the call rather than by an interrupt
# ---------------------------------------------------------------------------


class TestCompensateTimeoutBound:
    """``timeout`` bounds the work FROM THE CALL, interrupt or no interrupt.

    ``deadline`` is teardown pressure and stays conditional: nobody
    cancelling means a compensating action runs as long as it needs, because
    a rollback torn off half-run is worse than a slow one. ``timeout`` is the
    opposite promise, made by a caller whose compensation is itself
    best-effort with a known healthy cost (a single remote ``rm``; a listener
    reap) -- so it is OPT-IN, and the first test here is the one that keeps it
    that way.

    Deterministic like the tests above: ``timeout=0`` fires on the next loop
    turn, never a wall-clock wait.
    """

    @pytest.mark.asyncio
    async def test_no_bound_is_armed_at_all_without_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default must stay unbounded-when-uninterrupted, byte for byte.

        docker/compose.py, link/manage.py, tunnel/manage.py, session.py and
        privilege.py all pass neither bound and rely on their rollback being
        allowed to finish. A test that merely watched a slow compensation
        complete could not tell "unbounded" from "bounded at 10s" without
        burning 10s, so this reads the arming directly: with no ``timeout``,
        ``compensate`` schedules NO timer before its work completes.

        Reddens against exactly the mutation it exists for -- a default of
        ``timeout=DEFAULT_TEARDOWN_DEADLINE`` (or any always-on bound)
        records a ``call_later`` here while every other test stays green.
        """
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later
        delays: "list[float]" = []

        def spy(delay: float, callback, *args):  # type: ignore[no-untyped-def]
            delays.append(delay)
            return real_call_later(delay, callback, *args)

        monkeypatch.setattr(loop, "call_later", spy)

        async def rollback() -> str:
            await asyncio.sleep(0)  # a real suspension: the timer would have room to arm
            return "undone"

        assert await compensate(rollback(), what="unbounded rollback") == "undone"
        assert delays == [], f"an uninterrupted compensation was bounded after all: {delays}"

    @pytest.mark.asyncio
    async def test_a_compensation_that_finishes_inside_its_bound_is_untouched(self) -> None:
        """Positive control: the bound truncates nothing that completes in time."""

        async def rollback() -> str:
            await asyncio.sleep(0)
            return "undone"

        assert await compensate(rollback(), timeout=60.0, what="bounded rollback") == "undone"

    @pytest.mark.parametrize("hops", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_work_that_finishes_at_its_bound_is_honored_not_abandoned(
        self, hops: int, caplog
    ) -> None:
        """A compensation that resolves as its bound expires keeps its result.

        THE TIE IS NOT A DETAIL. The bound exists to stop work that CANNOT
        finish; reporting work that DID finish as abandoned is both a lie to
        the operator (a temp that was in fact removed, announced as possibly
        still on the device) and a discarded result.

        Nor is it one interleaving. "The work finished" and "the bound fired"
        reach ``compensate`` down different callback chains, so the tie has an
        offset -- counted here in scheduler turns (``asyncio.sleep(0)``), never
        wall clock:

        * 0 and 1 hops -- the shield wrapper has caught up, and reports the
          result itself;
        * 2 hops -- THE MEASURED WINDOW: the task is done, but the wrapper's
          done-callback is still queued (``Future.set_result`` schedules it
          with ``call_soon``), so a version that consults only the wrapper
          warns "abandoned" and returns ``None`` over a rollback that
          completed. Reproduced identically on 3.10 and 3.14; this row was the
          one RED against the shipped-then-fixed behavior, and it also reds a
          mutant that simply checks the expiry before the work.

        Three hops in is where genuine abandonment starts -- the work really
        has not finished -- which the hung-rollback tests above already cover,
        so this parametrization stops at the last honored offset rather than
        pinning the boundary itself to today's callback arithmetic.
        """

        async def rollback() -> str:
            for _ in range(hops):
                await asyncio.sleep(0)
            return "undone"

        with caplog.at_level("WARNING", logger="otto.lifecycle"):
            outcome = await compensate(rollback(), timeout=0.0, what=f"tie at {hops} hops")

        assert outcome == "undone", (
            f"a compensation that finished was abandoned at {hops} scheduling hops"
        )
        assert not [r for r in caplog.records if "abandoned" in r.message], (
            f"completed work was announced as abandoned: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_expiry_with_no_cancellation_held_returns_none_instead_of_raising(
        self, caplog
    ) -> None:
        """The shape a plain ``asyncio.wait_for`` wrapper had, folded into the helper.

        The caller here is NOT being cancelled -- it is unwinding a
        cancellation it already caught (shell's interrupted-put cleanup is
        the live case). Expiry must therefore not invent a ``CancelledError``
        for it to handle: the call returns ``None``, the abandonment is
        logged, and the caller's own re-raise is what propagates.

        ``cancelled_inner`` and ``all_tasks`` are the direct observations that
        the bound CANCELLED AND JOINED the work rather than merely stopping
        waiting on it: a bound that walked away would leave the rollback
        running and strand it past this call. Both are read AT THE MOMENT
        ``compensate`` returns, from inside the awaiting coroutine -- read
        after the awaiting task itself finishes they would be vacuous, since
        the loop gives an abandoned rollback its cancellation turn either way
        (measured: dropping the join keeps a check made afterwards green).
        """
        cancelled_inner: "list[bool]" = []
        seen: "dict[str, object]" = {}
        # This test's OWN task is pending throughout, so "stray" means new:
        # anything running below that was not already running above.
        before = asyncio.all_tasks()

        async def rollback() -> str:
            try:
                await asyncio.Event().wait()  # never set
            except asyncio.CancelledError:
                cancelled_inner.append(True)
                raise
            return "unreachable"

        async def caller() -> object:
            outcome = await compensate(rollback(), timeout=0.0, what="hung bounded rollback")
            seen["outcome"] = outcome
            seen["inner_cancelled"] = list(cancelled_inner)
            seen["strays"] = [
                t
                for t in asyncio.all_tasks()
                if t not in before and t is not asyncio.current_task()
            ]
            return outcome

        with caplog.at_level("WARNING", logger="otto.lifecycle"):
            task = asyncio.ensure_future(caller())
            # Runaway guard, not a measurement: an unbounded compensation
            # parks forever and would otherwise burn the suite's cap.
            _done, pending = await asyncio.wait({task}, timeout=10.0)

        assert not pending, "the timeout never fired: the hung compensation was never abandoned"
        assert seen["outcome"] is None, "expiry must report 'nothing to give', not raise"
        assert seen["inner_cancelled"] == [True], (
            "compensate returned before the work it abandoned had even been cancelled"
        )
        assert seen["strays"] == [], (
            "the abandoned compensation was still running when the call that gave up on it returned"
        )
        assert any("hung bounded rollback" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_expiry_with_a_cancellation_held_still_re_raises_it(self, caplog) -> None:
        """A held cancellation outranks the bound: an interrupted caller stays interrupted.

        The interrupt is delivered FROM the rollback's own first step, which
        is what makes the ordering deterministic instead of a wall-clock race:
        it lands while the work is genuinely running, in the same loop turn
        the ``timeout=0`` timer is due. Both interleavings of those two events
        reach the same outcome -- the cancellation is held first and re-raised
        after the expiry, or the expiry is observed with the cancellation
        already held -- which is the property, not the ordering.
        """
        holder: "list[asyncio.Task[object]]" = []
        cancelled_inner: "list[bool]" = []

        async def rollback() -> str:
            holder[0].cancel()  # the caller's interrupt, landing mid-compensation
            try:
                await asyncio.Event().wait()  # never set
            except asyncio.CancelledError:
                cancelled_inner.append(True)
                raise
            return "unreachable"

        with caplog.at_level("WARNING", logger="otto.lifecycle"):
            holder.append(
                asyncio.ensure_future(
                    compensate(
                        rollback(), timeout=0.0, deadline=60.0, what="held-then-abandoned rollback"
                    )
                )
            )
            with pytest.raises(asyncio.CancelledError):
                await holder[0]

        assert cancelled_inner == [True], "the abandoned work was left running, not cancelled"
        assert any("held-then-abandoned rollback" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_with_both_bounds_set_the_earlier_expiry_wins(self, caplog) -> None:
        """``timeout`` counts from the call, ``deadline`` from the interrupt.

        With nobody interrupting, ``deadline`` is never armed at all, so a
        ``timeout`` of 0 abandons the work while a 60s ``deadline`` sits
        unused -- the two are not alternatives, and the earlier expiry is what
        the work actually gets.
        """
        cancelled_inner: "list[bool]" = []

        async def rollback() -> str:
            try:
                await asyncio.Event().wait()  # never set
            except asyncio.CancelledError:
                cancelled_inner.append(True)
                raise
            return "unreachable"

        with caplog.at_level("WARNING", logger="otto.lifecycle"):
            task = asyncio.ensure_future(
                compensate(rollback(), timeout=0.0, deadline=60.0, what="both bounds")
            )
            _done, pending = await asyncio.wait({task}, timeout=10.0)  # runaway guard

        assert not pending, "the tighter of the two bounds never fired"
        assert task.result() is None
        assert cancelled_inner == [True]
        assert any("both bounds" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_deadline_still_fires_first_when_it_is_the_tighter_bound(
        self, caplog
    ) -> None:
        """The other direction: an armed ``timeout`` must not disarm ``deadline``.

        A held cancellation arms ``deadline=0``, which fires long before the
        60s ``timeout`` -- and takes the existing path (the work is cancelled
        under the shield), proving the opt-in bound did not replace the
        teardown one.
        """
        started = asyncio.Event()
        cancelled_inner: "list[bool]" = []

        async def rollback() -> str:
            started.set()
            try:
                await asyncio.Event().wait()  # never set
            except asyncio.CancelledError:
                cancelled_inner.append(True)
                raise
            return "unreachable"

        task = asyncio.ensure_future(
            compensate(rollback(), deadline=0.0, timeout=60.0, what="deadline-first rollback")
        )
        await started.wait()
        task.cancel()  # holds the cancel and arms the 0s deadline
        with (
            caplog.at_level("WARNING", logger="otto.lifecycle"),
            pytest.raises(asyncio.CancelledError),
        ):
            await task

        assert cancelled_inner == [True]
        assert any("deadline-first rollback" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_interrupted_bounded_compensation_drops_the_shield_it_walked_away_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hygiene the bound must not cost: the abandoned wrapper takes the failure with it.

        A bare ``await asyncio.shield(task)`` hands its wrapper future the
        cancellation that ends the await, and a CANCELLED wrapper is what
        marks the inner task's eventual exception retrieved. The bounded path
        awaits through :func:`asyncio.wait` instead -- which is what makes
        expiry distinguishable from an interrupt -- and ``asyncio.wait``
        leaves the wrapper untouched, so ``compensate`` has to drop it by
        hand. Without that, a rollback that fails AFTER its caller was
        interrupted parks its ``ValueError`` in a wrapper nobody is waiting
        on, and CPython reports "Future exception was never retrieved" at
        some later GC -- landing on whichever innocent test is running then
        (the unraisable-drift flake shape this suite has been bitten by).

        The wrappers are captured rather than the warning waited for: the
        collection that would print it happens at TEST TEARDOWN (pytest's log
        capture holds the failure's traceback until then, measured), so a
        check made inside the test would pass either way. What is asserted is
        the state that decides it -- the first wrapper, the one the held
        cancellation left behind, must be cancelled.
        """
        wrappers: "list[asyncio.Future[object]]" = []
        real_shield = asyncio.shield

        def spy(arg: object) -> "asyncio.Future[object]":
            wrapper = real_shield(arg)  # type: ignore[arg-type]
            wrappers.append(wrapper)
            return wrapper

        monkeypatch.setattr(asyncio, "shield", spy)
        started = asyncio.Event()
        release = asyncio.Event()

        async def rollback() -> None:
            started.set()
            await release.wait()
            raise ValueError("undo failed after the interrupt")

        task = asyncio.ensure_future(
            compensate(rollback(), timeout=60.0, what="late failure under a bound")
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)  # let the cancel land and be HELD
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(wrappers) > 1, (
            "the held cancellation never re-entered the shielded await: nothing was abandoned"
        )
        assert wrappers[0].cancelled(), (
            "the shield compensate walked away from was left holding the rollback's failure, "
            "with nobody to retrieve it"
        )
