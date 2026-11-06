"""Same-context re-capture supersedes (spec §8.5): newest wins, never accumulate.

Accumulating two captures of the same context would double-count that
context's coverage; the superseded capture drops out of the runs table
entirely. The context is :class:`SupersedeKey`, ``(tier, host, product)``:
the host is the capture's ``board`` field — the host id, the same value
carried into ``RunRecord.host`` at report time — and the product is its
``product``. Product is part of the context because one host can carry
several instrumented products: without it, a host's second product
supersedes its first and that product's coverage disappears from the
report — silently, since the reporter also seeds its cross-source dedupe
set from every committed manual capture, so the loser's cov-dir copy is
skipped as well. A capture's ``display_name`` is not part of it: two
captures of one host and product that differ only in display name are one
context, and the newer supersedes the older.
"""

import logging
from typing import NamedTuple

from ...layout import product_label
from .model import Capture

logger = logging.getLogger(__name__)


class SupersedeKey(NamedTuple):
    """The context a manual capture supersedes within: one tier's one host's one product."""

    tier: str
    host: str
    product: str


def supersede_key(cap: Capture) -> SupersedeKey:
    """Return *cap*'s context: the one place a capture's ``board`` is read as its host.

    The report's run dedupe key (``reporter._capture_run_key``) extends this
    key, and ``validity.register_capture_run`` takes a run's tier, host and
    product from it, rather than restating it.
    """
    return SupersedeKey(tier=cap.tier, host=cap.board, product=cap.product)


def select_manual_captures(captures: list[Capture]) -> list[Capture]:
    """Return winners (input order), newest ``captured_at`` per context key."""
    winners: dict[SupersedeKey, Capture] = {}
    for cap in captures:
        key = supersede_key(cap)
        context = product_label(key.host, key.product)
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
