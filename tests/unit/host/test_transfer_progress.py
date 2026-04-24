"""Tests for the shared Progress singleton that backs get/put.

Concurrent host transfers previously each opened their own ``rich.Live`` on
the shared module-level ``CONSOLE``, producing overlapping cursor escapes and
ghost rows.  The fix funnels every concurrent transfer through a single
reference-counted Progress/Live managed inside ``otto.host.transfer``.

These tests exercise the ref-count manager directly so callers don't need to
know it exists — they just call ``get``/``put`` concurrently.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import otto.host.transfer as transfer_mod
from otto.host.transfer import _acquire_shared_progress


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
        with patch.object(transfer_mod, 'make_transfer_progress', return_value=fake_progress):
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
        with patch.object(transfer_mod, 'make_transfer_progress', return_value=fake_progress):
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
        with patch.object(transfer_mod, 'make_transfer_progress', new=factory):

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
        with patch.object(transfer_mod, 'make_transfer_progress', return_value=fake_progress):
            with pytest.raises(RuntimeError):
                async with _acquire_shared_progress():
                    raise RuntimeError("boom")
        assert transfer_mod._shared_progress_refs == 0
        assert transfer_mod._shared_progress is None
        fake_progress.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_sequential_calls_make_fresh_progress(self):
        """Back-to-back (non-overlapping) calls each get their own Progress."""
        factory = MagicMock(side_effect=lambda: MagicMock())
        with patch.object(transfer_mod, 'make_transfer_progress', new=factory):
            async with _acquire_shared_progress():
                pass
            async with _acquire_shared_progress():
                pass
        assert factory.call_count == 2
