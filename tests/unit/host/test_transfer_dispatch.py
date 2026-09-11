"""The one dispatcher every backend hands its per-file coroutine to.

Bounded by the instance's ``concurrency_limit`` in BOTH modes, in order when
``concurrent=False``, and every file attempted whatever its siblings do.
"""

import asyncio
from pathlib import Path

import pytest

from otto.host.transfer.base import (
    DEFAULT_SESSION_TRANSFER_LIMIT,
    SSH_CHANNEL_HEADROOM,
    SSHD_DEFAULT_MAX_SESSIONS,
    BaseFileTransfer,
    ProgressGranularity,
    derive_concurrency_limit,
    resolve_concurrency_limit,
)
from otto.result import Result
from otto.utils import Status


class _Probe(BaseFileTransfer):
    """A backend whose only behaviour is to record how many files are in flight."""

    host_families = frozenset({"unix"})
    progress_granularity = ProgressGranularity(put=1, get=1)

    def __init__(
        self,
        limit: int,
        *,
        fail: set[str] | None = None,
        raise_for: set[str] | None = None,
        cancel_for: set[str] | None = None,
    ):
        super().__init__(name="probe")
        self._limit = limit
        self._fail = fail or set()
        self._raise = raise_for or set()
        self._cancel = cancel_for or set()
        self.in_flight = 0
        self.peak = 0
        self.order: list[str] = []
        self.cancelled: list[str] = []

    @property
    def concurrency_limit(self) -> int:
        return self._limit

    async def _one(self, src: Path) -> Result:
        # Split so the sentinel raise below lives in its own try/finally
        # (no sibling except -- TRY301) while THIS function's try/except
        # only ever observes a cancellation coming from the outside.
        try:
            return await self._one_inner(src)
        except asyncio.CancelledError:
            # Only a SIBLING's cancellation is recorded here -- a file in
            # `_cancel` raises this itself, deliberately, not because
            # something else cancelled it.
            if src.name not in self._cancel:
                self.cancelled.append(src.name)
            raise

    async def _one_inner(self, src: Path) -> Result:
        self.order.append(src.name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # A `cancel_for` file raises BEFORE sleeping, deterministically
            # ahead of its siblings (still asleep below) -- so a test can
            # observe whether THEY get cancelled rather than run to
            # completion, independent of scheduling order.
            if src.name in self._cancel:
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
            if src.name in self._raise:
                raise OSError(f"disk on fire for {src.name}")
            if src.name in self._fail:
                return Result(Status.Error, msg=f"{src.name}: refused")
            return Result(Status.Success, value=Path("/dest") / src.name)
        finally:
            self.in_flight -= 1

    async def _run_put(self, src_files, dest_dir, progress_factory, *, concurrent):
        return await self._dispatch_per_file(src_files, self._one, concurrent=concurrent)

    async def _run_get(self, src_files, dest_dir, progress_factory, *, concurrent):
        return await self._dispatch_per_file(src_files, self._one, concurrent=concurrent)


def _files(n: int) -> list[Path]:
    return [Path(f"/src/f{i:02d}") for i in range(n)]


@pytest.mark.asyncio
async def test_concurrent_runs_exactly_the_limit_at_once():
    probe = _Probe(limit=3)
    per_file = await probe._dispatch_per_file(_files(9), probe._one, concurrent=True)
    assert probe.peak == 3, f"peak {probe.peak}: the bound must be USED, not merely respected"
    assert all(r.is_ok for r in per_file.values())
    assert list(per_file) == _files(9), "keyed by the sources exactly as passed, in order"


@pytest.mark.asyncio
async def test_sequential_runs_one_at_a_time_in_order():
    probe = _Probe(limit=3)
    await probe._dispatch_per_file(_files(6), probe._one, concurrent=False)
    assert probe.peak == 1
    assert probe.order == [f.name for f in _files(6)]


@pytest.mark.asyncio
async def test_a_limit_of_one_is_sequential_even_when_concurrent():
    probe = _Probe(limit=1)
    await probe._dispatch_per_file(_files(5), probe._one, concurrent=True)
    assert probe.peak == 1
    assert probe.order == [f.name for f in _files(5)]


def test_one_object_survives_a_second_event_loop():
    """The instance semaphore is rebuilt for the loop that is actually running.

    ``asyncio.Semaphore`` binds to a loop the first time the cap is contended
    -- with more files than permits, that is the second file of the first
    batch. A backend reused after its loop is gone (a fresh
    ``asyncio.run``, which every hostless caller may do) would otherwise
    acquire against the dead loop and fold a ``RuntimeError`` into every
    queued file's entry.
    """
    probe = _Probe(limit=2)
    first = asyncio.run(probe._dispatch_per_file(_files(5), probe._one, concurrent=True))
    second = asyncio.run(probe._dispatch_per_file(_files(5), probe._one, concurrent=True))

    assert all(r.is_ok for r in first.values())
    assert [r.status for r in second.values()] == [Status.Success] * 5, (
        f"second loop: {[r.msg for r in second.values() if not r.is_ok]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [True, False])
async def test_every_file_is_attempted_after_a_failure(concurrent: bool):
    probe = _Probe(limit=2, fail={"f01"})
    per_file = await probe._dispatch_per_file(_files(4), probe._one, concurrent=concurrent)
    assert probe.order
    assert sorted(probe.order) == sorted(f.name for f in _files(4))
    assert per_file[Path("/src/f01")].status is Status.Error
    assert all(per_file[f].is_ok for f in _files(4) if f.name != "f01")
    assert not any(r.status is Status.Skipped for r in per_file.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [True, False])
async def test_an_exception_folds_into_that_files_entry_only(concurrent: bool):
    probe = _Probe(limit=2, raise_for={"f02"})
    per_file = await probe._dispatch_per_file(_files(4), probe._one, concurrent=concurrent)
    entry = per_file[Path("/src/f02")]
    assert entry.status is Status.Error
    assert "disk on fire" in entry.msg
    assert all(per_file[f].is_ok for f in _files(4) if f.name != "f02")


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [True, False])
async def test_overlapping_calls_share_one_instance_budget(concurrent: bool):
    probe = _Probe(limit=2)
    await asyncio.gather(
        *(probe._dispatch_per_file([f], probe._one, concurrent=concurrent) for f in _files(8))
    )
    assert probe.peak == 2, "eight one-file calls must still share the object's two permits"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_fabricates_nothing():
    probe = _Probe(limit=2)
    task = asyncio.ensure_future(probe._dispatch_per_file(_files(6), probe._one, concurrent=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_put_files_and_get_files_thread_concurrent_to_run_methods():
    seen: list[bool] = []

    class Spy(_Probe):
        async def _run_put(self, src_files, dest_dir, progress_factory, *, concurrent):
            seen.append(concurrent)
            return {s: Result(Status.Success, value=dest_dir / s.name) for s in src_files}

        async def _run_get(self, src_files, dest_dir, progress_factory, *, concurrent):
            seen.append(concurrent)
            return {s: Result(Status.Success, value=dest_dir / s.name) for s in src_files}

    spy = Spy(limit=1)
    await spy.put_files([Path("/a")], Path("/d"), show_progress=False, concurrent=False)
    await spy.get_files([Path("/a")], Path("/d"), show_progress=False)
    assert seen == [
        False,
        True,
    ], "put_files must forward the keyword; get_files must default it True"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [True, False])
async def test_a_cancelled_file_is_never_fabricated_into_an_entry(concurrent: bool):
    """The failure mode a gathering ``return_exceptions=True`` hides.

    A cancelled child comes back from such a gather as a VALUE, and writing it
    into the mapping invents an ``Error`` with an empty diagnostic for a file
    nobody ever decided had failed. The cancellation must come out instead.
    """
    probe = _Probe(limit=2, cancel_for={"f02"})
    with pytest.raises(asyncio.CancelledError):
        await probe._dispatch_per_file(_files(4), probe._one, concurrent=concurrent)


@pytest.mark.asyncio
async def test_a_raise_cancels_and_drains_the_sibling_tasks():
    """The concurrent arm must not leave siblings running detached on a raise.

    Plain ``asyncio.gather`` starts every file as a bare Task and, on the
    first raise, propagates while the others keep running unobserved -- a
    later raise from one of them would surface only as an "exception was
    never retrieved" warning, and (for a real backend) its semaphore permit
    would never come back. `_dispatch_per_file` must instead cancel and
    drain every other in-flight file before propagating.
    """
    probe = _Probe(limit=4, cancel_for={"f02"})

    with pytest.raises(asyncio.CancelledError):
        await probe._dispatch_per_file(_files(4), probe._one, concurrent=True)

    assert sorted(probe.cancelled) == ["f00", "f01", "f03"]


def test_the_base_limit_is_one():
    """A backend with no fan-out story inherits the bound, it does not opt in."""

    class Minimal(BaseFileTransfer):
        host_families = frozenset({"unix"})
        progress_granularity = ProgressGranularity(put=1, get=1)

        async def _run_put(self, src_files, dest_dir, progress_factory, *, concurrent):
            return {}

        async def _run_get(self, src_files, dest_dir, progress_factory, *, concurrent):
            return {}

    assert Minimal(name="minimal").concurrency_limit == 1


@pytest.mark.parametrize(("channels", "sharing"), [(0, 1), (1, 0)])
def test_a_derivation_from_a_non_positive_count_is_refused(channels: int, sharing: int):
    with pytest.raises(ValueError, match="must be at least 1"):
        derive_concurrency_limit(channels, sharing_objects=sharing)


def test_derivation_matches_the_documented_arithmetic():
    usable = SSHD_DEFAULT_MAX_SESSIONS - SSH_CHANNEL_HEADROOM
    assert derive_concurrency_limit(2) == usable // 2 == 4
    assert derive_concurrency_limit(1, sharing_objects=2) == usable // 2 == 4
    assert DEFAULT_SESSION_TRANSFER_LIMIT == 4
    assert derive_concurrency_limit(usable * 3) == 1, "never derives below one"


def test_resolution_prefers_a_configured_value_and_refuses_a_useless_one():
    assert resolve_concurrency_limit(None, derived=4, option="x.max") == 4
    assert resolve_concurrency_limit(7, derived=4, option="x.max") == 7
    refusal = r"scp_options\.max_concurrent_transfers must be at least 1, got 0"
    with pytest.raises(ValueError, match=refusal):
        resolve_concurrency_limit(0, derived=4, option="scp_options.max_concurrent_transfers")
