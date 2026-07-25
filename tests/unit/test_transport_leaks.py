"""Tests for the asyncio transport-leak registry (tests/_fixtures/_transport_leaks.py).

The registry replaces the old whole-heap leak scan (per-test ``gc.collect()``
plus a ``gc.get_objects()`` isinstance sweep, measured at 2.3-3.4x suite CPU
under ``OTTO_DETECT_ASYNCIO_LEAKS=1``): transports are recorded at their
``__init__`` chokepoint together with the test that created them, and the
per-test check iterates only the tracked live transports. These tests exercise
the load-bearing behaviors with a *real* selector transport: registration at
creation, attribution to the creating test, the leak predicate (open transport
on a closed loop), and the no-gc guarantee that is the whole point of the
redesign.
"""

import asyncio
import gc
import socket

import pytest

from tests._fixtures import _transport_leaks
from tests._fixtures._transport_leaks import (
    describe_referrers,
    install_transport_tracker,
    scan_leaked_transports,
)

CREATOR = "sentinel-creator-nodeid"


@pytest.fixture
def tagged_tracker(monkeypatch):
    """Install the tracker with the creator tag pinned to ``CREATOR``.

    In a full-suite run the root conftest has already installed the tracker
    with its own nodeid getter; installation is idempotent, so pinning the
    module-level getter is what makes attribution deterministic either way.
    """
    install_transport_tracker(lambda: "(tracker-test fallback)")
    monkeypatch.setattr(_transport_leaks, "_current_test_getter", lambda: CREATOR)


def _own_entries(entries):
    """Only the scan entries created by this test file (other tests' live
    transports may legitimately sit in the shared registry)."""
    return [(transport, desc) for transport, desc in entries if CREATOR in desc]


def _make_selector_transport(loop):
    """Create a real ``_SelectorSocketTransport`` on ``loop``.

    Returns ``(transport, peer_sock)``; the transport owns its own socket,
    the peer end is the caller's to close.
    """
    left, right = socket.socketpair()
    transport, _protocol = loop.run_until_complete(
        loop.create_connection(asyncio.Protocol, sock=left)
    )
    return transport, right


def _drain(loop):
    """Run scheduled callbacks (e.g. ``_call_connection_lost``) to completion."""
    for _ in range(3):
        loop.run_until_complete(asyncio.sleep(0))


class TestScanLeakedTransports:
    def test_open_transport_on_closed_loop_reported_with_creator(self, tagged_tracker):
        loop = asyncio.new_event_loop()
        transport, peer = _make_selector_transport(loop)
        loop.close()  # deliberately without transport.close() — the leak shape
        try:
            leaks = _own_entries(scan_leaked_transports())
            assert len(leaks) == 1
            leaked_transport, desc = leaks[0]
            assert leaked_transport is transport
            assert type(transport).__name__ in desc
        finally:
            # Dispose without a loop so the deliberate leak never escapes this
            # test: closes the sock and clears it, so __del__ won't warn and
            # later scans won't re-report it.
            transport._call_connection_lost(None)
            peer.close()

    def test_cleanly_closed_transport_not_reported(self, tagged_tracker):
        loop = asyncio.new_event_loop()
        transport, peer = _make_selector_transport(loop)
        transport.close()
        _drain(loop)
        loop.close()
        peer.close()
        assert _own_entries(scan_leaked_transports()) == []

    def test_live_transport_on_open_loop_not_reported(self, tagged_tracker):
        loop = asyncio.new_event_loop()
        transport, peer = _make_selector_transport(loop)
        try:
            assert _own_entries(scan_leaked_transports()) == []
        finally:
            transport.close()
            _drain(loop)
            loop.close()
            peer.close()

    def test_scan_never_calls_gc(self, monkeypatch):
        """The perf contract: the per-test path must not touch the gc — the
        old detector's per-test ``gc.collect()``/``gc.get_objects()`` is what
        cost 2.3-3.4x suite CPU."""

        def _boom(*args, **kwargs):
            raise AssertionError("scan_leaked_transports must never call into gc")

        monkeypatch.setattr(gc, "collect", _boom)
        monkeypatch.setattr(gc, "get_objects", _boom)
        scan_leaked_transports()


class TestDescribeReferrers:
    def test_names_the_holders_type(self):
        obj = object()
        holder = [obj]
        try:
            assert "builtins.list" in describe_referrers(obj)
        finally:
            holder.clear()
