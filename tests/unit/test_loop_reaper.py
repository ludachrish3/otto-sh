"""Tests for the orphaned-event-loop reaper (tests/_loop_reaper.py).

The reaper closes leaked *pytest-asyncio* (harness) event loops at the test
boundary so their ``ResourceWarning`` never gets gc-finalized inside an
unrelated later test and escalated by ``filterwarnings=["error"]`` into a
flaky, misattributed failure. Crucially it must NOT silently swallow a loop
leaked by *product* (``otto/``) code — that is a real regression and must be
surfaced, not masked.
"""
import asyncio

import pytest

from tests._loop_reaper import (
    LeakedProductLoopError,
    classify_loop_origin,
    reap_or_raise,
    reap_orphan_loops,
)


class TestClassifyLoopOrigin:
    def test_otto_frame_in_stack_is_product(self):
        stack = [
            "/x/_pytest/runner.py",
            "/home/u/otto-sh/src/otto/host/unixHost.py",
            "/usr/lib/python3.12/asyncio/runners.py",
        ]
        assert classify_loop_origin(stack) == "product"

    def test_no_otto_frame_is_harness(self):
        stack = [
            "/x/_pytest/runner.py",
            "/x/site-packages/pytest_asyncio/plugin.py",
            "/usr/lib/python3.12/asyncio/base_events.py",
        ]
        assert classify_loop_origin(stack) == "harness"


class TestReapOrphanLoops:
    def test_harness_orphan_is_closed(self):
        lp = asyncio.new_event_loop()
        try:
            closed, leaked = reap_orphan_loops([lp], origin_of=lambda _l: "harness")
            assert lp.is_closed()
            assert closed == [lp]
            assert leaked == []
        finally:
            if not lp.is_closed():
                lp.close()

    def test_product_orphan_is_reported_not_closed(self):
        lp = asyncio.new_event_loop()
        try:
            closed, leaked = reap_orphan_loops([lp], origin_of=lambda _l: "product")
            assert not lp.is_closed(), "a product-leaked loop must not be silently closed"
            assert leaked == [lp]
            assert closed == []
        finally:
            lp.close()

    def test_already_closed_loop_ignored(self):
        lp = asyncio.new_event_loop()
        lp.close()
        closed, leaked = reap_orphan_loops([lp], origin_of=lambda _l: "harness")
        assert closed == []
        assert leaked == []

    @pytest.mark.asyncio
    async def test_running_loop_never_touched(self):
        running = asyncio.get_running_loop()
        closed, leaked = reap_orphan_loops([running], origin_of=lambda _l: "harness")
        assert not running.is_closed()
        assert closed == []
        assert leaked == []


class TestReapOrRaise:
    def test_harness_orphans_closed_and_counted(self):
        lps = [asyncio.new_event_loop(), asyncio.new_event_loop()]
        try:
            reaped = reap_or_raise(lps, origin_of=lambda _l: "harness")
            assert reaped == 2
            assert all(lp.is_closed() for lp in lps)
        finally:
            for lp in lps:
                if not lp.is_closed():
                    lp.close()

    def test_product_leak_raises_and_does_not_close(self):
        lp = asyncio.new_event_loop()
        try:
            with pytest.raises(LeakedProductLoopError):
                reap_or_raise([lp], origin_of=lambda _l: "product")
            assert not lp.is_closed(), "must not close — the leak has to stay visible"
        finally:
            lp.close()
