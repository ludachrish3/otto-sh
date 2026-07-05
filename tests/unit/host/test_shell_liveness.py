"""Unit tests for the shared shell-liveness confirmation loop."""

import asyncio
import re

import pytest

from otto.host import shell_liveness
from otto.host.command_frame import SessionMarkers
from otto.host.shell_liveness import confirm_live

_FIXED = SessionMarkers.for_session("cafef00d")


def _render(m: SessionMarkers) -> str:
    return f"probe {m.end_prefix}\n"


def _pattern(m: SessionMarkers) -> re.Pattern[str]:
    return re.compile(re.escape(m.end_prefix) + r"(\d+)__")


class _FakeIO:
    """send/expect fake: expect times out `fail_times` times, then matches."""

    def __init__(self, fail_times: int, sleep_on_fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self.calls = 0
        self._sleep_on_fail = sleep_on_fail

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def expect(self, pattern: re.Pattern[str], timeout: float) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            if self._sleep_on_fail:
                await asyncio.sleep(timeout)
            raise asyncio.TimeoutError
        return "matched"


def _fixed_markers() -> SessionMarkers:
    return _FIXED


@pytest.mark.asyncio
async def test_confirms_on_first_probe():
    io = _FakeIO(fail_times=0)
    ok = await confirm_live(
        io.send,
        io.expect,
        _render,
        _pattern,
        _fixed_markers,
        settle=0.0,
        probe_timeout=0.5,
        deadline=5.0,
    )
    assert ok is True
    assert len(io.sent) == 1


@pytest.mark.asyncio
async def test_resends_past_timeouts_then_confirms():
    io = _FakeIO(fail_times=2)
    ok = await confirm_live(
        io.send,
        io.expect,
        _render,
        _pattern,
        _fixed_markers,
        settle=0.0,
        probe_timeout=0.5,
        deadline=5.0,
    )
    assert ok is True
    assert len(io.sent) == 3  # two resends + the one that landed


@pytest.mark.asyncio
async def test_returns_false_when_deadline_elapses():
    io = _FakeIO(fail_times=999, sleep_on_fail=True)
    ok = await confirm_live(
        io.send,
        io.expect,
        _render,
        _pattern,
        _fixed_markers,
        settle=0.0,
        probe_timeout=0.02,
        deadline=0.08,
    )
    assert ok is False
    assert io.calls >= 1


@pytest.mark.asyncio
async def test_settles_before_first_probe(monkeypatch):
    io = _FakeIO(fail_times=0)
    events: list[str] = []

    async def _record_sleep(duration: float) -> None:
        events.append(f"sleep:{duration}")

    monkeypatch.setattr(shell_liveness.asyncio, "sleep", _record_sleep)
    orig_send = io.send

    async def _tracked_send(text: str) -> None:
        events.append("send")
        await orig_send(text)

    await confirm_live(
        _tracked_send,
        io.expect,
        _render,
        _pattern,
        _fixed_markers,
        settle=0.3,
        probe_timeout=0.5,
        deadline=5.0,
    )
    assert events[0] == "sleep:0.3"  # settle happens BEFORE the first send
    assert "send" in events


@pytest.mark.asyncio
async def test_fresh_markers_used_per_probe():
    io = _FakeIO(fail_times=1)  # one timeout, then match -> two probes
    seen: list[str] = []

    def _counting_markers() -> SessionMarkers:
        return SessionMarkers.for_session(f"id{len(seen)}")

    def _render_track(m: SessionMarkers) -> str:
        seen.append(m.end_prefix)
        return f"probe {m.end_prefix}\n"

    await confirm_live(
        io.send,
        io.expect,
        _render_track,
        _pattern,
        _counting_markers,
        settle=0.0,
        probe_timeout=0.5,
        deadline=5.0,
    )
    assert seen == ["__OTTO_id0_END__", "__OTTO_id1_END__"]  # distinct per probe
