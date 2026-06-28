"""Tests for the shared Progress singleton that backs get/put.

Concurrent host transfers previously each opened their own ``rich.Live`` on
the shared module-level ``CONSOLE``, producing overlapping cursor escapes and
ghost rows.  The fix funnels every concurrent transfer through a single
reference-counted Progress/Live managed inside ``otto.host.transfer``.

These tests exercise the ref-count manager directly so callers don't need to
know it exists — they just call ``get``/``put`` concurrently.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import otto.host.transfer.progress as transfer_mod
from otto.host.transfer import BaseFileTransfer, _acquire_shared_progress
from otto.utils import Status


@pytest.fixture(autouse=True)
def _reset_shared_progress_state():
    """Ensure no state leaks between tests if an earlier one crashed mid-entry."""
    transfer_mod._shared_progress = None
    transfer_mod._shared_progress_refs = 0
    yield
    transfer_mod._shared_progress = None
    transfer_mod._shared_progress_refs = 0


class TestSharedProgress:
    @pytest.mark.asyncio
    async def test_single_entry_starts_and_stops(self):
        fake_progress = MagicMock()
        with patch.object(transfer_mod, "make_transfer_progress", return_value=fake_progress):
            async with _acquire_shared_progress() as p:
                assert p is fake_progress
                fake_progress.start.assert_called_once()
                fake_progress.stop.assert_not_called()
            fake_progress.stop.assert_called_once()
        assert transfer_mod._shared_progress is None
        assert transfer_mod._shared_progress_refs == 0

    @pytest.mark.asyncio
    async def test_nested_reuses_one_progress(self):
        """A nested acquire within an outer one must not create a second Live."""
        fake_progress = MagicMock()
        with patch.object(transfer_mod, "make_transfer_progress", return_value=fake_progress):
            async with _acquire_shared_progress() as outer:
                async with _acquire_shared_progress() as inner:
                    assert outer is inner
                    fake_progress.start.assert_called_once()
                fake_progress.stop.assert_not_called()
            fake_progress.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_gather_shares_one_live(self):
        """Mixed concurrent acquirers (e.g. several put calls in one gather) share one Progress."""
        factory = MagicMock(side_effect=lambda: MagicMock())
        with patch.object(transfer_mod, "make_transfer_progress", new=factory):

            async def one():
                async with _acquire_shared_progress() as p:
                    await asyncio.sleep(0)
                    return p

            results = await asyncio.gather(one(), one(), one())

        # Only one Progress was ever constructed.
        assert factory.call_count == 1
        # All acquirers saw the same instance.
        assert results[0] is results[1] is results[2]
        results[0].start.assert_called_once()
        results[0].stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_still_decrements_ref(self):
        """An exception inside the context still releases the ref count cleanly."""
        fake_progress = MagicMock()
        with (
            patch.object(transfer_mod, "make_transfer_progress", return_value=fake_progress),
            pytest.raises(RuntimeError),
        ):
            async with _acquire_shared_progress():
                raise RuntimeError("boom")
        assert transfer_mod._shared_progress_refs == 0
        assert transfer_mod._shared_progress is None
        fake_progress.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_sequential_calls_make_fresh_progress(self):
        """Back-to-back (non-overlapping) calls each get their own Progress."""
        factory = MagicMock(side_effect=lambda: MagicMock())
        with patch.object(transfer_mod, "make_transfer_progress", new=factory):
            async with _acquire_shared_progress():
                pass
            async with _acquire_shared_progress():
                pass
        assert factory.call_count == 2


# ---------------------------------------------------------------------------
# BaseFileTransfer enforces the progress contract at the type level
# ---------------------------------------------------------------------------


class TestBaseFileTransferIsAbstract:
    """``BaseFileTransfer`` makes progress reporting a structural requirement
    of every transfer backend: ``_run_put`` and ``_run_get`` are both
    ``@abstractmethod`` and receive a ``TransferProgressFactory``. A new
    backend cannot be instantiated without implementing both — the type
    system, not a runtime test, is the first line of defense."""

    def test_missing_both_hooks_raises_type_error(self):
        class NoHooks(BaseFileTransfer):
            pass

        with pytest.raises(TypeError, match="abstract"):
            NoHooks(name="x")

    def test_missing_run_get_raises_type_error(self):
        class OnlyPut(BaseFileTransfer):
            async def _run_put(self, src_files, dest_dir, progress_factory):
                return Status.Success, ""

        with pytest.raises(TypeError, match="_run_get"):
            OnlyPut(name="x")

    def test_missing_run_put_raises_type_error(self):
        class OnlyGet(BaseFileTransfer):
            async def _run_get(self, src_files, dest_dir, progress_factory):
                return Status.Success, ""

        with pytest.raises(TypeError, match="_run_put"):
            OnlyGet(name="x")

    def test_both_hooks_present_instantiates(self):
        class Concrete(BaseFileTransfer):
            async def _run_put(self, src_files, dest_dir, progress_factory):
                return Status.Success, ""

            async def _run_get(self, src_files, dest_dir, progress_factory):
                return Status.Success, ""

        # No exception — both abstract methods supplied.
        Concrete(name="x")


class TestBaseFileTransferProgressWiring:
    """The base owns the progress-acquisition plumbing — verify the factory
    actually reaches ``_run_put`` / ``_run_get``, and that
    ``show_progress=False`` short-circuits with ``progress_factory=None``."""

    def _spy_subclass(self):
        captured: dict[str, object] = {}

        class Spy(BaseFileTransfer):
            async def _run_put(self, src_files, dest_dir, progress_factory):
                captured["put_factory"] = progress_factory
                return Status.Success, ""

            async def _run_get(self, src_files, dest_dir, progress_factory):
                captured["get_factory"] = progress_factory
                return Status.Success, ""

        return Spy(name="spy"), captured

    @pytest.mark.asyncio
    async def test_show_progress_true_passes_a_factory(self):
        spy, captured = self._spy_subclass()
        await spy.put_files([Path("/a/foo")], Path("/dest"), show_progress=True)
        assert callable(captured["put_factory"])

    @pytest.mark.asyncio
    async def test_show_progress_false_passes_none(self):
        spy, captured = self._spy_subclass()
        await spy.put_files([Path("/a/foo")], Path("/dest"), show_progress=False)
        assert captured["put_factory"] is None

    @pytest.mark.asyncio
    async def test_filename_validation_short_circuits_before_run_put(self):
        """An over-limit name returns Status.Error before ``_run_put``
        executes — the spy never gets called."""
        spy, captured = self._spy_subclass()
        spy._max_filename_len = 5
        status, err = await spy.put_files(
            [Path("/a/long_filename.bin")],
            Path("/dest"),
        )
        assert status == Status.Error
        assert "put_factory" not in captured
