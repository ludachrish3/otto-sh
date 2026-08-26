"""Mutation tests of ``tests/_fixtures/progress.py``: each clause reds on exactly its violation."""

import pytest

from tests._fixtures.progress import (
    ProgressEvent,
    assert_progress_invariants,
    capture_progress,
    events_for,
)

SRC = "/local/a.bin"


def _ev(done: int, total: int = 100, src: str = SRC) -> ProgressEvent:
    return ProgressEvent(src=src, dst="/remote/a.bin", done=done, total=total)


def test_a_compliant_stride_stream_passes():
    events = events_for(src=SRC, total=100, granularity=32)
    assert_progress_invariants(events, src=SRC, total=100, granularity=32)


def test_a_compliant_whole_file_stream_passes():
    events = events_for(src=SRC, total=100, granularity=None, dst="/remote/a.bin")
    assert_progress_invariants(events, src=SRC, total=100, granularity=None)
    assert events == [_ev(100)]


@pytest.mark.parametrize(
    ("events", "clause"),
    [
        ([], "no progress events"),
        ([_ev(50, src="/other")], "names a different source"),
        ([_ev(32, total=99), _ev(100)], "total changed between events"),
        ([_ev(32), _ev(101)], "outside"),
        ([_ev(-1), _ev(100)], "outside"),
        ([_ev(64), _ev(32), _ev(100)], "not strictly increasing"),
        ([_ev(32), _ev(64), _ev(96)], "final event is 96 of 100"),
        ([_ev(64), _ev(100)], "first event advanced 64 bytes; the stride is 32"),
        ([_ev(32), _ev(100)], "advanced 68 bytes between events; the stride is 32"),
    ],
)
def test_each_stride_clause_reds_on_its_own_violation(events, clause):
    with pytest.raises(AssertionError, match=clause):
        assert_progress_invariants(events, src=SRC, total=100, granularity=32)


def test_a_whole_file_promise_refuses_an_intermediate_event():
    with pytest.raises(AssertionError, match="promised ONE event; saw 2"):
        assert_progress_invariants([_ev(50), _ev(100)], src=SRC, total=100, granularity=None)


def test_an_empty_payload_is_outside_the_helpers_domain():
    with pytest.raises(AssertionError, match="total must be positive"):
        assert_progress_invariants([_ev(0, total=0)], src=SRC, total=0, granularity=32)


def test_events_for_refuses_an_empty_payload():
    with pytest.raises(AssertionError, match="total must be positive"):
        events_for(src=SRC, total=0, granularity=32)


def test_events_for_refuses_a_non_positive_stride():
    with pytest.raises(AssertionError, match="stride must be positive"):
        events_for(src=SRC, total=100, granularity=0)


def test_capture_progress_is_one_handler_per_factory_call():
    events, spy = capture_progress()
    factory = spy(progress=None, host_name="h")
    assert factory() is not factory()
    factory()("/a", "/ra", 1, 2)
    factory()("/b", "/rb", 2, 2)
    assert [e.src for e in events] == ["/a", "/b"]
