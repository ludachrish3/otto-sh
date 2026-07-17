"""The collector's tunnel loop: change-detection, failure containment
(spec 2026-07-16 §2). Every guard here is mutation-proven — see Step 5."""

import asyncio
import logging
from typing import Any

import pytest

from otto.models.monitor import TunnelRecord
from otto.monitor.collector import MetricCollector


def _rec(tid: str, status: str = "ok") -> TunnelRecord:
    return TunnelRecord(
        id=tid,
        protocol="udp",
        service_port=15001,
        hops=["a", "b"],
        status=status,  # type: ignore[arg-type]
        carriers_present=4,
        carriers_expected=4,
    )


class _Script:
    """A tunnel_source scripted per call: a list -> that set; an Exception -> raise."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> list[TunnelRecord]:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _SpyDB:
    """Records write ordering; quacks just enough of MetricDB."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def write_tunnels(self, tunnels_json: str) -> None:
        self._log.append(f"db:{tunnels_json}")


def _collector(source: _Script, log: list[str] | None = None) -> MetricCollector:
    c = MetricCollector(hosts=[], tunnel_source=source)
    c.session_id = "s1"
    if log is not None:
        c._db = _SpyDB(log)  # type: ignore[assignment]
        c._publish = lambda frag: log.append(f"sse:{sorted(frag)}")  # type: ignore[method-assign]
    return c


def test_first_pass_with_tunnels_publishes_and_stores() -> None:
    source = _Script([_rec("tun-a-1")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == [_rec("tun-a-1")]
    assert len(log) == 2
    assert log[0].startswith("db:")  # db BEFORE publish
    assert log[1].startswith("sse:")


def test_unchanged_set_does_not_republish() -> None:
    source = _Script([_rec("tun-a-1")], [_rec("tun-a-1")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    asyncio.run(c._tunnel_pass())
    assert len(log) == 2  # one db + one sse total


def test_status_flip_republishes() -> None:
    source = _Script([_rec("tun-a-1")], [_rec("tun-a-1", status="degraded")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    asyncio.run(c._tunnel_pass())
    assert len(log) == 4


def test_successful_empty_scan_publishes_empty_exactly_once() -> None:
    source = _Script([_rec("tun-a-1")], [], [])
    log: list[str] = []
    c = _collector(source, log)
    for _ in range(3):
        asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == []
    assert len(log) == 4  # initial pair + the [] pair; third pass is a no-op


def test_failed_scan_keeps_last_state_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Script(
        [_rec("tun-a-1")], RuntimeError("net down"), RuntimeError("still down"), [_rec("tun-a-1")]
    )
    log: list[str] = []
    c = _collector(source, log)
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.collector"):
        for _ in range(4):
            asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == [_rec("tun-a-1")]  # never blanked
    assert len(log) == 2  # failures wrote/published NOTHING; recovery set is unchanged
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # first failure + recovery, not one per failing tick
    assert "tunnel scan" in warnings[0].message


def test_initial_run_spawns_tunnel_loop() -> None:
    """run() wires the loop when a source is present. Uses a shell-less
    target so the collection side idles; duration=0 -> exactly the initial
    passes."""
    from datetime import timedelta

    from otto.monitor.collector import MonitorTarget

    class _Host:
        name = "h1"
        id = "h1"

        async def run(self, commands: list[str], **kwargs: Any) -> Any:
            raise RuntimeError("no shell in this test")

    source = _Script([], [])
    c = MetricCollector(targets=[MonitorTarget(host=_Host(), parsers={})], tunnel_source=source)  # type: ignore[arg-type]
    asyncio.run(c.run(interval=timedelta(milliseconds=10), duration=timedelta(0)))
    assert source.calls >= 1
