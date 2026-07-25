"""Same-context re-capture supersedes (spec §8.5): newest wins, never accumulate.

Accumulating two captures of the same (tier, label, host) context would
double-count that context's coverage; the superseded capture drops out
of the runs table entirely. Plan B's explicit ``host`` field will
replace the board component of the key.
"""

import logging

from .model import Capture

logger = logging.getLogger(__name__)


def _key(cap: Capture) -> tuple[str, str, str]:
    return (cap.tier, cap.display_name or cap.board, cap.board)


def select_manual_captures(captures: list[Capture]) -> list[Capture]:
    """Return winners (input order), newest ``captured_at`` per context key."""
    winners: dict[tuple[str, str, str], Capture] = {}
    for cap in captures:
        key = _key(cap)
        prev = winners.get(key)
        if prev is not None and prev.captured_at > cap.captured_at:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                cap.ticket or "no-ticket",
                cap.captured_at or "undated",
                key[1],
                prev.captured_at,
            )
            continue
        if prev is not None:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                prev.ticket or "no-ticket",
                prev.captured_at or "undated",
                key[1],
                cap.captured_at,
            )
        winners[key] = cap
    keep = set(map(id, winners.values()))
    return [c for c in captures if id(c) in keep]
