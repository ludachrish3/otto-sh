"""Same-context re-capture supersedes (spec §8.5): newest wins, never accumulate.

Accumulating two captures of the same (tier, label, host, product)
context would double-count that context's coverage; the superseded
capture drops out of the runs table entirely. The third key component is
the capture's ``board`` field — the host id — the same value carried
into ``RunRecord.host`` at report time (store v4); the fourth is its
``product``, mirroring the report-time run key's ordering. Product is
part of the context because one host can carry several instrumented
products: without it, a host's second product supersedes its first and
that product's coverage disappears from the report — silently, since the
reporter also seeds its cross-source dedupe set from every committed
manual capture, so the loser's cov-dir copy is skipped as well.
"""

import logging

from .model import Capture

logger = logging.getLogger(__name__)


def _key(cap: Capture) -> tuple[str, str, str, str]:
    host = cap.board
    return (cap.tier, cap.display_name or host, host, cap.product)


def select_manual_captures(captures: list[Capture]) -> list[Capture]:
    """Return winners (input order), newest ``captured_at`` per context key."""
    winners: dict[tuple[str, str, str, str], Capture] = {}
    for cap in captures:
        key = _key(cap)
        context = f"{key[1]}/{key[3]}"
        prev = winners.get(key)
        if prev is not None and prev.captured_at > cap.captured_at:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                cap.ticket or "no-ticket",
                cap.captured_at or "undated",
                context,
                prev.captured_at,
            )
            continue
        if prev is not None:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                prev.ticket or "no-ticket",
                prev.captured_at or "undated",
                context,
                cap.captured_at,
            )
        winners[key] = cap
    keep = set(map(id, winners.values()))
    return [c for c in captures if id(c) in keep]
