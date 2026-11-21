"""Expire the completion deadline on cue: once the worker reaches the state a test is about.

The completion cache bounds each project's enumeration with a wall-clock
deadline (``otto.config.completion_cache._bounded``). A test that sets a
short deadline and asserts what the worker had reached by then is betting
that everything before that point fits in the window, and a loaded runner
loses the bet. :func:`expire_when` replaces the wait itself
(``_await_worker``): the deadline passes the moment *reached* is set, so
the worker is past exactly that point, every time. The same idea for the
event loop's deadlines is :mod:`tests._fixtures.clock`.
"""

import threading

import pytest

import otto.config.completion_cache as cc

WAIT = 60.0
"""How long the expiry waits for *reached*; never reached when the code is right."""


def expire_when(monkeypatch: pytest.MonkeyPatch, reached: threading.Event) -> None:
    """Make the completion deadline pass as soon as *reached* is set (at most :data:`WAIT`)."""

    def wait(_thread: threading.Thread, _deadline: float) -> None:
        assert reached.wait(WAIT), "the worker never reached the point the deadline waits for"

    monkeypatch.setattr(cc, "_await_worker", wait)
