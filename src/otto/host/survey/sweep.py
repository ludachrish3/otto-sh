"""The embedded family's inventory: a bounded external sweep from the connect path's vantage.

Never a range by default. At most two dials in flight, because a Zephyr
target has a tiny socket pool and its 3.7 stack answers a SYN to a dead
port badly; the per-dial timeout comes from the engine, injected.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .dial import DialOutcome

_MAX_RANGE = 1024
_PORT_MAX = 65535


@dataclass(frozen=True, slots=True)
class SweepRow:
    """One dialed port from the sweep: the port number and what the dial found there."""

    port: int
    outcome: DialOutcome


def _port(token: str) -> int:
    try:
        value = int(token)
    except ValueError:
        msg = f"--scan-ports: {token!r} is not a port"
        raise ValueError(msg) from None
    if not 1 <= value <= _PORT_MAX:
        msg = f"--scan-ports: {value} is outside 1..{_PORT_MAX}"
        raise ValueError(msg)
    return value


def _parse_range(token: str) -> list[int]:
    """``"a-b"`` → the ports it spans; refuses a reversed or over-wide range by name."""
    lo_s, _, hi_s = token.partition("-")
    lo_s, hi_s = lo_s.strip(), hi_s.strip()
    if not lo_s or not hi_s:
        msg = f"--scan-ports: {token!r} is not a port range"
        raise ValueError(msg)
    lo, hi = _port(lo_s), _port(hi_s)
    if hi < lo:
        msg = f"--scan-ports: range {token} is reversed"
        raise ValueError(msg)
    if hi - lo + 1 > _MAX_RANGE:
        msg = f"--scan-ports: range {token} is wider than {_MAX_RANGE} ports; pass a narrower range"
        raise ValueError(msg)
    return list(range(lo, hi + 1))


def parse_scan_ports(text: str | None) -> list[int]:
    """``"2000-2010,8080"`` → the ports, deduplicated, first-seen order."""
    if text is None or not text.strip():
        return []
    seen: dict[int, None] = {}
    for raw in text.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            for p in _parse_range(token):
                seen[p] = None
        else:
            seen[_port(token)] = None
    return list(seen)


def sweep_port_set(
    declared: list[int], defaults: list[int], alternates: list[int], extra: list[int]
) -> list[int]:
    """Build the bounded set: declared ports, then defaults, alternates, extra; no repeats."""
    seen: dict[int, None] = {}
    for group in (declared, defaults, alternates, extra):
        for p in group:
            seen[p] = None
    return list(seen)


async def sweep_ports(
    ports: list[int],
    dial: "Callable[[int], Awaitable[DialOutcome]]",
    *,
    concurrency: int,
) -> list[SweepRow]:
    """Dial every port with at most *concurrency* in flight; rows in *ports* order."""
    if concurrency < 1:
        msg = "concurrency must be at least 1"
        raise ValueError(msg)
    gate = asyncio.Semaphore(concurrency)

    async def one(port: int) -> SweepRow:
        async with gate:
            try:
                outcome = await dial(port)
            except Exception as exc:  # noqa: BLE001 — one bad dial is one not-checkable row, never a failed survey
                outcome = DialOutcome(state="not-checkable", detail=f"{type(exc).__name__}: {exc}")
        return SweepRow(port=port, outcome=outcome)

    return list(await asyncio.gather(*(one(p) for p in ports)))
