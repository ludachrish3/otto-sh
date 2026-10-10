"""Each event loop's host registry and its cleanup boundaries (spec 2 §3.3), one test per rule.

Duck hosts only (tests/_fixtures/registry.py): the leaf sees records, never
BaseHost. The real wiring is tests/unit/test_cleanup_boundaries.py.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable

import pytest

from otto import invocation as inv
from tests._fixtures.registry import DuckHost, register_duck, registered_ids

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _forget_registries():
    yield
    inv.forget_closed_loops()


def _loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


async def _until(ready: Callable[[], bool]) -> None:
    """The handshake: yield to the loop until *ready()* holds, bounded so a defect cannot hang."""

    async def poll() -> None:
        # The waiter queue is a plain list with no event to wait on: poll it once per step.
        while not ready():  # noqa: ASYNC110 — no event exists for the state polled here
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 5)


async def test_registering_prunes_closed_loops_registries():  # P1
    dead = asyncio.new_event_loop()
    DuckHost("old").claim(dead)
    dead.close()
    DuckHost("new").claim(_loop())
    assert [s.loop for s in inv.open_registrations()] == [_loop()]
    assert dead not in inv._REGISTRIES  # the pruning is the rule under test


async def test_the_last_release_forgets_the_idle_registry():  # P6
    boundary = inv.acquire_boundary(_loop(), deadline=5)
    DuckHost("h").claim(_loop())
    assert await boundary.release(label="t") == ["h"]
    assert _loop() not in inv._REGISTRIES  # nothing held, registered or waiting: no entry kept


async def test_a_release_that_leaves_a_holder_keeps_the_registry():  # P6, the other side
    outer = inv.acquire_boundary(_loop(), deadline=5)
    inner = inv.acquire_boundary(_loop(), deadline=5)
    assert await inner.release(label="t") == []  # empty, but the outer one still holds it
    assert inv._REGISTRIES[_loop()].held == 1
    DuckHost("h").claim(_loop())
    assert await outer.release(label="t") == ["h"]
    assert _loop() not in inv._REGISTRIES


async def test_a_shut_registry_is_kept_so_the_loop_stays_shut():  # P7
    inv.acquire_boundary(_loop(), deadline=5)  # a runner's boundary, never released
    DuckHost("h").claim(_loop())
    assert await inv.shut_down(_loop(), label="t", deadline=5) == ["h"]
    registry = inv._REGISTRIES[_loop()]  # kept: the shut state lives in it, nowhere else
    assert registry.state == "shut"
    with pytest.raises(RuntimeError, match="shut its host registry down"):
        inv.acquire_boundary(_loop(), deadline=5)
    with pytest.raises(RuntimeError, match="shut that loop's host registry down"):
        await inv.sweep_unheld(_loop(), label="t", deadline=5)
    assert await inv.shut_down(_loop(), label="t", deadline=5) == []
    assert inv._REGISTRIES[_loop()] is registry


async def test_a_registry_forgotten_with_records_is_reported_first(monkeypatch):  # P4
    seen: list[inv.RegistrySnapshot] = []
    monkeypatch.setattr(inv, "_FORGET_OBSERVER", seen.append)
    for forget in (
        lambda: DuckHost("trigger").claim(_loop()),  # registration pruning
        inv.forget_closed_loops,
        inv.abandon_closed_loops,
    ):
        dead = asyncio.new_event_loop()
        DuckHost("left").claim(dead)
        dead.close()
        seen.clear()
        forget()
        assert [(s.loop, s.closed, s.display_ids) for s in seen] == [(dead, True, ["left"])]

    class _Bare:
        id = "gone"

        async def close(self):
            return None

    empty, bare = asyncio.new_event_loop(), _Bare()
    register_duck(bare, empty)  # a record, then none
    inv.unregister(empty, id(bare), 0)
    empty.close()
    seen.clear()
    inv.forget_closed_loops()
    assert seen == []  # an empty registry is forgotten silently


async def test_only_forget_registry_removes_a_registry():  # P5
    # async only because the module carries pytestmark = pytest.mark.asyncio, and
    # pytest-asyncio warns (an error under the repo's filterwarnings) on a marked sync test.
    import ast

    from tests._fixtures.paths import PROJECT_ROOT

    tree = ast.parse((PROJECT_ROOT / "src" / "otto" / "invocation.py").read_text())
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            deletes = isinstance(node, ast.Delete) and any(
                isinstance(t, ast.Subscript) and ast.unparse(t.value) == "_REGISTRIES"
                for t in node.targets
            )
            pops = isinstance(node, ast.Call) and ast.unparse(node.func) in {
                "_REGISTRIES.pop",
                "_REGISTRIES.clear",
                "_REGISTRIES.popitem",
            }
            if (deletes or pops) and fn.name != "_forget_registry":
                offenders.append(f"{fn.name}:{node.lineno}")
    assert offenders == []


async def test_a_re_registration_replaces_the_hosts_record():  # P2
    host = DuckHost("h")
    host.claim(_loop())
    host.claim(_loop())
    assert registered_ids(_loop()) == ["h"]
    assert inv._REGISTRIES[_loop()].records[id(host)].generation == 2


async def test_a_completed_close_removes_only_its_own_generation():  # P3
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await host.entered.wait()
    host.claim(_loop())  # reconnected while its old close is in flight
    host.block = False
    host.gate.set()
    assert await sweep == ["h", "h"]  # the old close finished, then the sweep closed the new record
    assert registered_ids(_loop()) == []


async def test_a_dependent_closes_before_what_it_depends_on():  # S1
    order: list[str] = []
    parent = DuckHost("parent")
    child = DuckHost("child", parent=parent)

    async def note(name):
        order.append(name)

    parent.on_close = lambda: note("parent")
    child.on_close = lambda: note("child")
    parent.claim(_loop())
    child.claim(_loop())
    await inv.sweep_unheld(_loop(), label="t", deadline=5)
    assert order == ["child", "parent"]


async def test_a_failed_close_is_logged_and_the_rest_still_close(caplog):  # S2
    bad, good = DuckHost("bad", fail=OSError("boom")), DuckHost("good")
    bad.claim(_loop())
    good.claim(_loop())
    with caplog.at_level(logging.WARNING, logger="otto.invocation"):
        closed = await inv.sweep_unheld(_loop(), label="t", deadline=5)
    assert closed == ["good"]
    assert "closing host 'bad' failed" in caplog.text
    assert registered_ids(_loop()) == []


async def test_expiry_cancels_twice_then_leaves_a_refusing_close_to_finish():  # S3
    host = DuckHost("stuck", block=True, refuse_cancel=True)
    host.claim(_loop())
    try:
        # generous outer bound: the sweep returns after its deadline and two graces, not never
        await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert host.cancels_refused == 2
    finally:
        host.gate.set()  # the refusing close finishes on its own later; a red never hangs
    await asyncio.wait_for(host.finished.wait(), 1)
    assert host.closes_finished == 1


async def test_the_closed_ids_are_returned_in_completion_order_and_logged(caplog):  # S4
    first, second = DuckHost("first"), DuckHost("second")  # independent: both start at once
    second_done = asyncio.Event()

    async def wait_for_second():
        await second_done.wait()  # started first, finishes last

    async def mark_second():
        second_done.set()

    first.on_close = wait_for_second
    second.on_close = mark_second
    first.claim(_loop())
    second.claim(_loop())
    with caplog.at_level(logging.DEBUG, logger="otto.invocation"):
        closed = await inv.sweep_unheld(_loop(), label="the probe's loop", deadline=5)
    assert closed == ["second", "first"]
    assert "closed 2 hosts at end of the probe's loop: second, first" in caplog.text


async def test_expiry_abandons_every_outstanding_record_and_leaves_none(caplog):  # S5
    host = DuckHost("h", block=True, refuse_cancel=True)
    late = DuckHost("late", block=True)

    async def reconnect_and_register_a_peer():
        host.claim(_loop())  # re-registered at a new generation while its old close is in flight
        late.claim(_loop())  # registered during the sweep

    host.on_close = reconnect_and_register_a_peer
    host.claim(_loop())
    try:
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert registered_ids(_loop()) == []
        assert host.abandoned == [2]
        assert late.abandoned == [1]
        assert "ran past its deadline" in caplog.text
        # h's cut-short close at generation 1 is not named again: its newer record was.
        assert re.search(r"dropped the connections of: h, late$", caplog.text, re.MULTILINE)
    finally:
        host.gate.set()  # lets the refusing close end, so a red never hangs the loop's teardown


async def test_a_close_cut_short_that_removed_its_own_record_is_still_abandoned(caplog):  # S16
    host = DuckHost("h", block=True, release_on_cancel=True)
    host.claim(_loop())
    try:
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert host.abandoned == [1]  # the half-closed connections are dropped
        assert "dropped the connections of: h" in caplog.text
        assert registered_ids(_loop()) == []
    finally:
        host.gate.set()


async def test_a_close_cut_short_after_its_host_reconnected_elsewhere_is_not_abandoned(
    caplog,
):  # S17
    other = asyncio.new_event_loop()
    host = DuckHost("h", block=True, release_on_cancel=True)

    def reconnect_elsewhere_and_close_there() -> None:
        host.claim(other)
        host._release(other, host.generation)  # unowned again, at generation 2

    host.after_cancel = reconnect_elsewhere_and_close_there
    host.claim(_loop())
    try:
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert host.abandon_calls == [1]  # the cut record reached its abandon, at generation 1
        assert host.abandoned == []  # a no-op: the host is at 2
        assert host.generation == 2
        assert registered_ids(_loop()) == []
        # The warning names only what was dropped, never a stale no-op.
        assert re.search(r"dropped the connections of: none$", caplog.text, re.MULTILINE)
    finally:
        host.gate.set()
        other.close()


async def test_a_close_that_finishes_despite_the_cancel_is_not_abandoned():  # S18
    class _Finishing(DuckHost):
        async def close(self) -> None:
            loop, generation = self.owner, self.generation
            with contextlib.suppress(asyncio.CancelledError):  # finishes its close anyway
                await self.gate.wait()
            self.closes_finished += 1
            self._release(loop, generation)

    host = _Finishing("h")
    host.claim(_loop())
    closed = await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
    assert closed == ["h"]
    assert host.abandoned == []  # it was not cut short: it closed
    assert registered_ids(_loop()) == []


async def test_abandoning_a_cut_close_leaves_no_record_even_if_the_abandon_registers():  # S19
    class _Reregistering(DuckHost):
        def _abandon(self, loop, generation):
            dropped = super()._abandon(loop, generation)
            self.claim(loop)  # a defective host that registers again while dropped
            return dropped

    host = _Reregistering("h", block=True, release_on_cancel=True)
    host.claim(_loop())
    try:
        await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert host.abandoned == [1]
        assert registered_ids(_loop()) == []  # no record outlives the expired sweep
    finally:
        host.gate.set()


async def test_a_queued_close_whose_host_was_rebuilt_before_it_started_closes_the_new_record(
    caplog,
):  # S20
    rebuilt = DuckHost("rebuilt", block=True, release_on_cancel=True)

    async def rebuild_the_other() -> None:
        rebuilt.claim(_loop())  # same loop, new generation: what rebuild_connections registers

    rebuilder = DuckHost("rebuilder", on_close=rebuild_the_other)
    rebuilder.claim(_loop())
    rebuilt.claim(_loop())  # independent of the rebuilder: both closes are queued in one step
    try:
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        # The close queued for generation 1 never ran; the sweep closed generation 2,
        # and the expiry that interrupted that close abandoned it at generation 2.
        assert rebuilt.closes_started == 1
        assert rebuilt.abandoned == [2]
        assert registered_ids(_loop()) == []
        assert re.search(r"dropped the connections of: rebuilt$", caplog.text, re.MULTILINE)
    finally:
        rebuilt.gate.set()


async def test_a_cut_close_whose_cancellation_cleanup_raises_is_still_abandoned(caplog):  # S21
    def fail_while_cleaning_up() -> None:
        raise OSError("connection reset while closing")

    host = DuckHost("h", block=True, release_on_cancel=True, after_cancel=fail_while_cleaning_up)
    host.claim(_loop())
    try:
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            closed = await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=0.1), 5)
        assert closed == []
        assert "closing host 'h' failed during the loop's sweep: OSError" in caplog.text
        assert host.abandoned == [1]  # the expiry interrupted it, and it never finished
        assert re.search(r"dropped the connections of: h$", caplog.text, re.MULTILINE)
        assert registered_ids(_loop()) == []
    finally:
        host.gate.set()


async def test_entering_draining_happens_before_the_first_await():  # S6
    host = DuckHost("h", block=True)
    host.claim(_loop())
    # Launched in the same step: the second sweep starts only once the first has
    # suspended, so it must already find the loop draining.
    first = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    second = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    try:
        with pytest.raises(RuntimeError, match="already closing its hosts"):
            await asyncio.wait_for(second, 1)
    finally:
        host.gate.set()  # lets the close end, so a red never hangs the loop's teardown
    assert await asyncio.wait_for(first, 5) == ["h"]


async def test_one_sweep_per_loop():  # S7
    host = DuckHost("h", block=True)
    host.claim(_loop())
    first = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await host.entered.wait()
    with pytest.raises(RuntimeError, match="already closing its hosts"):
        await inv.sweep_unheld(_loop(), label="t", deadline=5)
    with pytest.raises(RuntimeError, match="closing its hosts"):
        await inv.shut_down(_loop(), label="t", deadline=5)
    host.gate.set()
    await first


async def test_a_registration_alone_wakes_the_sweep_while_a_close_still_blocks():  # S8
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await host.entered.wait()
    peer = DuckHost("peer")
    peer.claim(_loop())  # registered from outside the sweep; no close has finished
    await asyncio.wait_for(peer.finished.wait(), 1)  # swept while h still blocks
    assert host.closes_finished == 0
    host.gate.set()
    assert sorted(await sweep) == ["h", "peer"]


async def test_a_child_registered_mid_sweep_closes_before_its_waiting_parent():  # S9
    parent = DuckHost("parent")
    holder = DuckHost("holder", parent=parent, block=True)  # keeps the parent from starting
    child = DuckHost("child", parent=parent, block=True)
    seen_by_parent: list[int] = []

    async def parent_close():
        seen_by_parent.append(child.closes_finished)

    parent.on_close = parent_close
    parent.claim(_loop())
    holder.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await holder.entered.wait()
    child.claim(_loop())  # registers while the parent's close has not started
    await asyncio.wait_for(child.entered.wait(), 1)  # the registration alone started it
    holder.gate.set()
    await asyncio.wait_for(holder.finished.wait(), 1)
    parent_started = asyncio.ensure_future(parent.entered.wait())
    done, _ = await asyncio.wait({parent_started}, timeout=0.2)  # bounded absence check
    assert not done  # the child is still closing, so the parent must not start
    child.gate.set()
    await asyncio.wait_for(sweep, 5)
    assert seen_by_parent == [1]


async def test_a_dependency_cycle_closes_everything_rather_than_spinning():  # S10
    a, b = DuckHost("a"), DuckHost("b")
    a.parent, b.parent = b, a
    a.claim(_loop())
    b.claim(_loop())
    assert sorted(await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=5), 5)) == [
        "a",
        "b",
    ]


async def test_a_drain_that_ends_reopens_the_registry():  # S11
    DuckHost("h").claim(_loop())
    await inv.sweep_unheld(_loop(), label="t", deadline=5)
    inv.acquire_boundary(_loop(), deadline=5)  # open again: no raise
    assert inv._REGISTRIES[_loop()].state == "open"


async def test_a_close_raising_cancelled_by_itself_is_a_failed_close(caplog):  # S12
    host = DuckHost("h", fail=asyncio.CancelledError())
    host.claim(_loop())
    with caplog.at_level(logging.WARNING, logger="otto.invocation"):
        closed = await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=5), 5)
    assert closed == []
    assert host.closes_started == 1
    assert "closing host 'h' failed" in caplog.text


async def test_the_synchronous_acquisition_refuses_draining_shut_and_closed_loops():  # B1
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await host.entered.wait()
    with pytest.raises(RuntimeError, match="closing its hosts"):
        inv.acquire_boundary(_loop(), deadline=5)
    host.gate.set()
    await sweep
    await inv.shut_down(_loop(), label="t", deadline=5)
    with pytest.raises(RuntimeError, match="shut"):
        inv.acquire_boundary(_loop(), deadline=5)
    dead = asyncio.new_event_loop()
    dead.close()
    with pytest.raises(RuntimeError, match="closed"):
        inv.acquire_boundary(dead, deadline=5)


async def test_the_last_release_is_bounded_by_the_deadline_recorded_at_acquisition():  # B2
    boundary = inv.acquire_boundary(_loop(), deadline=0.1)
    assert boundary.deadline == 0.1
    host = DuckHost("stuck", block=True, refuse_cancel=True)
    host.claim(_loop())
    release = asyncio.ensure_future(boundary.release(label="t"))
    try:
        # asyncio.wait never cancels: an unbounded drain cannot absorb it and hang the test
        done, _ = await asyncio.wait({release}, timeout=0.1 + 2 * 0.25 + 1.0)
        assert release in done, "the release outlived the deadline its boundary recorded"
        assert host.abandoned == [1]
    finally:
        host.gate.set()  # always: lets a drain with no deadline (the mutation) finish
        await asyncio.wait({release}, timeout=1.0)
        if not release.done():
            release.cancel()


async def test_a_release_that_is_not_the_last_sweeps_nothing():  # B3
    outer, inner = (
        inv.acquire_boundary(_loop(), deadline=5),
        inv.acquire_boundary(_loop(), deadline=5),
    )
    host = DuckHost("h")
    host.claim(_loop())
    assert await inner.release(label="inner") == []
    assert registered_ids(_loop()) == ["h"]
    assert await outer.release(label="outer") == ["h"]


async def test_release_is_exactly_once_even_when_cancelled_mid_sweep():  # B4
    boundary = inv.acquire_boundary(_loop(), deadline=5)
    registry = inv._REGISTRIES[_loop()]
    host = DuckHost("h", block=True)
    host.claim(_loop())
    release = asyncio.ensure_future(boundary.release(label="t"))
    await host.entered.wait()
    release.cancel()
    host.gate.set()  # a cancelled drain finishes its closes first (R-C1), then re-raises
    with pytest.raises(asyncio.CancelledError):
        await release
    assert registry.held == 0  # the count is the rule
    assert _loop() not in inv._REGISTRIES  # and the emptied registry was forgotten
    with pytest.raises(RuntimeError, match="already released"):
        await boundary.release(label="t")


async def test_shutdown_sweeps_whatever_the_count_then_refuses_and_sweeps_nothing():  # B5
    held = inv.acquire_boundary(_loop(), deadline=5)
    host = DuckHost("h")
    host.claim(_loop())
    assert await inv.shut_down(_loop(), label="runner", deadline=5) == ["h"]
    with pytest.raises(RuntimeError, match="shut"):
        inv.acquire_boundary(_loop(), deadline=5)
    with pytest.raises(RuntimeError, match="shut"):
        await inv.acquire_boundary_waiting(_loop(), deadline=5)
    DuckHost("later").claim(_loop())
    assert await held.release(label="t") == []
    assert registered_ids(_loop()) == ["later"]


async def test_an_acquisition_from_inside_the_sweep_is_refused_and_the_sweep_finishes():  # B6
    refusals: list[str] = []

    async def enter_a_context():
        try:
            inv.acquire_boundary(_loop(), deadline=5)
        except RuntimeError as exc:
            refusals.append(str(exc))
        try:
            await inv.acquire_boundary_waiting(_loop(), deadline=5)
        except RuntimeError as exc:
            refusals.append(str(exc))

    DuckHost("h", on_close=enter_a_context).claim(_loop())
    assert await asyncio.wait_for(inv.sweep_unheld(_loop(), label="t", deadline=1), 3) == ["h"]
    assert len(refusals) == 2
    assert all("inside a host sweep" in r for r in refusals)


async def test_sweep_unheld_refuses_a_held_draining_or_shut_loop_and_sweeps_otherwise():  # B7
    boundary = inv.acquire_boundary(_loop(), deadline=5)
    DuckHost("h").claim(_loop())
    with pytest.raises(RuntimeError, match="cleanup boundary holds it"):
        await inv.sweep_unheld(_loop(), label="t", deadline=5)
    inv._REGISTRIES[_loop()].held = 0  # drop the hold without the release's sweep
    del boundary
    assert await inv.sweep_unheld(_loop(), label="t", deadline=5) == ["h"]
    await inv.shut_down(_loop(), label="t", deadline=5)
    with pytest.raises(RuntimeError, match="shut"):
        await inv.sweep_unheld(_loop(), label="t", deadline=5)


async def test_a_waiter_is_admitted_at_the_end_of_the_drain_it_waited_on_despite_churn():  # W1
    loop = _loop()
    registry_of = lambda: inv._REGISTRIES[loop]  # noqa: E731 — the queue is the rule
    closes: list[str] = []
    blocker = DuckHost("blocker", block=True)

    async def note_blocker():
        closes.append("blocker")

    blocker.on_close = note_blocker
    blocker.claim(loop)
    sweep = asyncio.ensure_future(inv.sweep_unheld(loop, label="t", deadline=5))
    await blocker.entered.wait()

    async def churn():  # competing short boundaries, each leaving a host for a drain to close
        for i in range(5):
            boundary = await inv.acquire_boundary_waiting(loop, deadline=5)
            duck = DuckHost(f"churn{i}")

            async def note(i=i):
                closes.append(f"churn{i}")

            duck.on_close = note
            duck.claim(loop)
            await boundary.release(label="churn")

    churner = asyncio.ensure_future(churn())
    await _until(lambda: len(registry_of().waiters) == 1)  # the churn's first wait queues first
    closes_at_admission: list[list[str]] = []

    async def long_wait():
        boundary = await inv.acquire_boundary_waiting(loop, deadline=5)
        closes_at_admission.append(list(closes))
        return boundary

    waiter = asyncio.ensure_future(long_wait())
    await _until(lambda: len(registry_of().waiters) == 2)  # queued behind the churn
    blocker.gate.set()
    await sweep
    boundary = await asyncio.wait_for(waiter, 1)
    assert closes_at_admission == [["blocker"]]  # admitted when the FIRST drain ended
    await asyncio.wait_for(churner, 5)
    assert registry_of().held == 1  # the waiter's hold: no churn release was the last
    assert sorted(await boundary.release(label="t")) == [f"churn{i}" for i in range(5)]


async def test_a_waiter_cancelled_after_its_grant_releases_it_and_one_cancelled_before_leaves():
    # W2
    # The drain is simulated (state set by hand, ended by hand), so the test can cancel
    # a waiter in the same step as its grant: with a real sweep, the granted waiter
    # resumes before the test does.
    registry = inv._REGISTRIES.setdefault(_loop(), inv._Registry())
    registry.state = "draining"
    before = asyncio.ensure_future(inv.acquire_boundary_waiting(_loop(), deadline=5))
    after = asyncio.ensure_future(inv.acquire_boundary_waiting(_loop(), deadline=5))
    await _until(lambda: len(registry.waiters) == 2)
    before.cancel()
    await _until(lambda: len(registry.waiters) == 1)  # cancelled before its grant: it left
    inv._end_drain(registry, shut=False)  # grants `after`, counted held
    assert registry.held == 1
    after.cancel()  # cancelled after its grant, before it resumed
    with pytest.raises(asyncio.CancelledError):
        await after
    assert registry.held == 0  # given back as an ordinary release


async def test_the_wait_is_bounded_by_the_running_sweeps_deadline():  # W3
    host = DuckHost("stuck", block=True, refuse_cancel=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=0.1))
    try:
        await host.entered.wait()
        boundary = await asyncio.wait_for(
            inv.acquire_boundary_waiting(_loop(), deadline=5), 0.1 + 2 * 0.25 + 1.0
        )
        await sweep
        await boundary.release(label="t")
    finally:
        host.gate.set()  # lets the refusing close end, so a red never hangs the loop's teardown


async def test_abandon_closed_loops_abandons_what_a_closed_loop_left_and_forgets_it():  # T1
    dead = asyncio.new_event_loop()
    stays, moved, stale = DuckHost("stays"), DuckHost("moved"), DuckHost("stale")
    stays.claim(dead)
    moved.claim(dead)
    stale.claim(dead)
    moved.owner = _loop()  # since moved to another loop: not this registry's to abandon
    stale.generation += 1  # reconnected since, unregistered: its abandon is a no-op
    dead.close()
    assert inv.abandon_closed_loops() == ["stays"]  # a stale no-op is not named
    assert stays.abandoned == [1]
    assert moved.abandoned == []
    assert stale.abandon_calls == [1]
    assert stale.abandoned == []
    assert dead not in inv._REGISTRIES


async def _queue_a_waiter() -> "asyncio.Future":
    """Queue an acquisition on the draining loop; return it once the queue shows it."""
    registry = inv._REGISTRIES[_loop()]  # the queue is the handshake
    before = len(registry.waiters)
    waiter = asyncio.ensure_future(inv.acquire_boundary_waiting(_loop(), deadline=5))
    await _until(lambda: len(registry.waiters) > before)
    return waiter


async def test_a_cancelled_drain_finishes_before_it_admits_anyone():  # C1, Review Focus 3
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    await host.entered.wait()
    waiter = await _queue_a_waiter()
    sweep.cancel()  # Ctrl-C during open_context's exit
    done, _ = await asyncio.wait({waiter}, timeout=0.2)  # bounded absence check
    assert not done  # the old close still has authority: nobody enters
    assert not sweep.done()
    assert inv._REGISTRIES[_loop()].state == "draining"
    host.gate.set()  # the old close finishes under the closed admission
    with pytest.raises(asyncio.CancelledError):
        await sweep  # only now does the cancellation propagate
    boundary = await asyncio.wait_for(waiter, 1)
    assert host.closes_finished == 1
    assert registered_ids(_loop()) == []
    assert inv._REGISTRIES[_loop()].state == "open"
    await boundary.release(label="t")


async def test_a_second_cancel_cuts_the_drain_short_and_abandons_before_admitting(caplog):  # C2
    host = DuckHost("stuck", block=True, refuse_cancel=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=None))
    await host.entered.wait()
    abandoned_at_admission: list[list[int]] = []

    async def wait_and_look():
        boundary = await inv.acquire_boundary_waiting(_loop(), deadline=5)
        abandoned_at_admission.append(list(host.abandoned))
        return boundary

    registry = inv._REGISTRIES[_loop()]
    waiter = asyncio.ensure_future(wait_and_look())
    try:
        await _until(lambda: bool(registry.waiters))
        sweep.cancel()
        done, _ = await asyncio.wait({sweep}, timeout=0.2)  # bounded absence check
        assert not done  # absorbed: the drain keeps going, admission closed
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            sweep.cancel()  # a second Ctrl-C
            # bounded: two graces, not the unbounded deadline
            done, _ = await asyncio.wait({sweep}, timeout=2)
        assert sweep in done
        assert sweep.cancelled()
        boundary = await asyncio.wait_for(waiter, 1)
        assert abandoned_at_admission == [[1]]  # abandoned BEFORE anyone was admitted
        assert registered_ids(_loop()) == []
        assert "closing hosts at end of t was cut short; gave up after" in caplog.text
        assert "ran past its deadline" not in caplog.text  # it had no deadline
        await boundary.release(label="t")
    finally:
        host.gate.set()  # lets the refusing close end, so a red never hangs the loop's teardown


async def test_a_cancelled_drain_that_expires_abandons_before_admitting(caplog):  # C3
    host = DuckHost("stuck", block=True, refuse_cancel=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=0.2))
    await host.entered.wait()
    abandoned_at_admission: list[list[int]] = []

    async def wait_and_look():
        boundary = await inv.acquire_boundary_waiting(_loop(), deadline=5)
        abandoned_at_admission.append(list(host.abandoned))
        return boundary

    registry = inv._REGISTRIES[_loop()]
    waiter = asyncio.ensure_future(wait_and_look())
    try:
        await _until(lambda: bool(registry.waiters))
        sweep.cancel()  # once
        with (
            caplog.at_level(logging.WARNING, logger="otto.invocation"),
            pytest.raises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(sweep, 5)
        boundary = await asyncio.wait_for(waiter, 1)
        assert abandoned_at_admission == [[1]]
        assert "closing hosts at end of t ran past its deadline; gave up after" in caplog.text
        await boundary.release(label="t")
    finally:
        host.gate.set()  # lets the refusing close end, so a red never hangs the loop's teardown


async def test_a_raising_ownership_check_is_a_warning_and_the_drain_goes_on(caplog):  # S13
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    bad = DuckHost("bad", owned_by_fails=ValueError("a defective ownership check"))
    try:
        await host.entered.wait()
        waiter = await _queue_a_waiter()
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            bad.claim(_loop())  # wakes the sweep, whose recompute asks bad's ownership
            await _until(lambda: bad.closes_started == 1)  # counted as owned: closed
            done, _ = await asyncio.wait({waiter}, timeout=0.2)  # bounded absence check
            assert not done  # h's close still has authority: nobody enters
    finally:
        host.gate.set()  # lets the close end, so a red never hangs the loop's teardown
    assert sorted(await asyncio.wait_for(sweep, 5)) == ["bad", "h"]
    assert "ownership check of host 'bad' failed" in caplog.text
    boundary = await asyncio.wait_for(waiter, 1)
    await boundary.release(label="t")


async def test_a_sweep_that_fails_is_cut_short_and_abandons_before_admitting(
    monkeypatch, caplog
):  # S14
    host = DuckHost("h", block=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    abandoned_at_admission: list[list[int]] = []

    async def wait_and_look():
        boundary = await inv.acquire_boundary_waiting(_loop(), deadline=5)
        abandoned_at_admission.append(list(host.abandoned))
        return boundary

    def defect(loop, record, *_):
        raise RuntimeError("a defect in the sweep")

    try:
        await host.entered.wait()
        registry = inv._REGISTRIES[_loop()]
        waiter = asyncio.ensure_future(wait_and_look())
        await _until(lambda: bool(registry.waiters))
        monkeypatch.setattr(inv, "_owned", defect)
        with caplog.at_level(logging.WARNING, logger="otto.invocation"):
            DuckHost("late").claim(_loop())  # wakes the sweep into the defect
            with pytest.raises(RuntimeError, match="a defect in the sweep"):
                await asyncio.wait_for(sweep, 5)
        monkeypatch.undo()
        boundary = await asyncio.wait_for(waiter, 1)
        assert abandoned_at_admission == [[1]]  # cut short and abandoned BEFORE admission
        assert registered_ids(_loop()) == []  # no record outlives the failed drain
        assert registry.state == "open"
        assert "closing hosts at end of t failed; gave up" in caplog.text
        await boundary.release(label="t")
    finally:
        host.gate.set()  # lets the close end, so a red never hangs the loop's teardown


async def test_a_sweep_that_fails_with_no_record_left_still_cuts_short_before_admitting(
    monkeypatch,
):  # S14, the failure alone
    host = DuckHost("h", block=True, refuse_cancel=True)
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    refused_at_admission: list[int] = []

    async def wait_and_look():
        boundary = await inv.acquire_boundary_waiting(_loop(), deadline=5)
        refused_at_admission.append(host.cancels_refused)
        return boundary

    try:
        await host.entered.wait()
        registry = inv._REGISTRIES[_loop()]

        def defect(loop, record, *_):
            registry.records.clear()  # the in-flight close's record is gone too
            raise RuntimeError("a defect in the sweep")

        waiter = asyncio.ensure_future(wait_and_look())
        await _until(lambda: bool(registry.waiters))
        monkeypatch.setattr(inv, "_owned", defect)
        DuckHost("late").claim(_loop())  # wakes the sweep into the defect
        with pytest.raises(RuntimeError, match="a defect in the sweep"):
            await asyncio.wait_for(sweep, 5)
        monkeypatch.undo()
        boundary = await asyncio.wait_for(waiter, 1)
        # No record was left, so only the failure made the drain cut the close short.
        assert refused_at_admission == [2]
        assert registered_ids(_loop()) == []
        assert registry.state == "open"
        await boundary.release(label="t")
    finally:
        host.gate.set()  # lets the refusing close end, so a red never hangs the loop's teardown


async def test_a_deadline_that_runs_out_in_the_end_of_body_window_abandons_the_late_record(
    caplog,
):  # S15, at the deadline
    host, late = DuckHost("h", block=True), DuckHost("late")
    host.claim(_loop())
    with caplog.at_level(logging.WARNING, logger="otto.invocation"):
        sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=1.0))
        try:
            await host.entered.wait()
            body = next(
                t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__name__", "") == "_sweep"
            )

            def in_window(_):
                late.claim(_loop())
                # Blocks the loop past the deadline, so the drain resumes with none left:
                # the deadline has passed, not raced.
                time.sleep(1.1)

            body.add_done_callback(in_window)
        finally:
            host.gate.set()  # lets the close end, so a red never hangs the loop's teardown
        assert await asyncio.wait_for(sweep, 5) == ["h"]
    assert late.abandoned == [1]
    assert late.closes_started == 0
    assert registered_ids(_loop()) == []
    assert "ran past its deadline" in caplog.text


async def test_a_raising_ownership_check_warns_once_however_often_the_sweep_recomputes(
    caplog,
):  # S13, once
    parent = DuckHost("bad", owned_by_fails=ValueError("a defective ownership check"))
    child = DuckHost("child", parent=parent, block=True)
    parent.claim(_loop())
    child.claim(_loop())
    with caplog.at_level(logging.WARNING, logger="otto.invocation"):
        sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
        try:
            await child.entered.wait()  # bad waits on its dependent, asked on each recompute
            for name in ("x1", "x2", "x3"):
                extra = DuckHost(name)
                extra.claim(_loop())  # each registration wakes the sweep into a recompute
                await asyncio.wait_for(extra.finished.wait(), 5)
        finally:
            child.gate.set()
        assert sorted(await asyncio.wait_for(sweep, 5)) == ["bad", "child", "x1", "x2", "x3"]
    assert caplog.text.count("ownership check of host 'bad' failed") == 1


async def test_a_registration_after_the_sweeps_last_close_is_closed_by_the_same_drain():  # S15
    host, late = DuckHost("h", block=True), DuckHost("late")
    host.claim(_loop())
    sweep = asyncio.ensure_future(inv.sweep_unheld(_loop(), label="t", deadline=5))
    try:
        await host.entered.wait()
        body = next(
            t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__name__", "") == "_sweep"
        )
        # A done callback runs after the sweep's body returns and before the drain
        # resumes: the window between "nothing left" and the drain's end.
        body.add_done_callback(lambda _: late.claim(_loop()))
    finally:
        host.gate.set()  # lets the close end, so a red never hangs the loop's teardown
    assert await asyncio.wait_for(sweep, 5) == ["h", "late"]
    assert late.closes_finished == 1
    assert registered_ids(_loop()) == []


async def test_abandon_closed_loops_isolates_a_raising_host(caplog):  # T2
    dead = asyncio.new_event_loop()
    bad = DuckHost("bad", abandon_fails=ValueError("a defective abandon"))
    good = DuckHost("good")
    odd = DuckHost("odd", owned_by_fails=ValueError("a defective ownership check"))
    for host in (bad, good, odd):
        host.claim(dead)
    dead.close()
    with caplog.at_level(logging.WARNING, logger="otto.invocation"):
        assert inv.abandon_closed_loops() == ["good", "odd"]  # odd counted as owned
    assert good.abandoned == [1]
    assert odd.abandoned == [1]
    assert dead not in inv._REGISTRIES
    assert "abandoning host 'bad' failed" in caplog.text
    assert "ownership check of host 'odd' failed" in caplog.text


@pytest.mark.parametrize(
    ("where", "interrupt"),
    [("continuing", SystemExit(3)), ("graces", KeyboardInterrupt())],
)
async def test_an_interrupt_during_a_cancelled_drain_is_what_propagates(
    monkeypatch, caplog, where, interrupt
):  # C4
    host = DuckHost("h", block=True)
    host.claim(_loop())
    real_wait = asyncio.wait
    # The drain's own waits, in order: the caller's cancel lands in the first; the
    # drain continues in the second (a further cancel cuts it short there); cutting
    # short waits out a grace in the third.
    script: list[BaseException] = {
        "continuing": [asyncio.CancelledError(), interrupt],
        "graces": [asyncio.CancelledError(), asyncio.CancelledError(), interrupt],
    }[where]

    async def scripted_wait(*args, **kwargs):
        if script:
            raise script.pop(0)
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", scripted_wait)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="otto.invocation"),
            pytest.raises(type(interrupt)) as info,
        ):
            await inv.sweep_unheld(_loop(), label="t", deadline=5)
    finally:
        monkeypatch.undo()
        host.gate.set()
    assert info.value is interrupt  # SystemExit keeps its code
    assert host.abandoned == [1]  # cut short, then abandoned
    assert "closing hosts at end of t was cut short; gave up after" in caplog.text
    assert registered_ids(_loop()) == []
    # The drain ended (a registry left draining is never forgotten) and its
    # emptied registry was forgotten.
    assert _loop() not in inv._REGISTRIES
