"""``tickets.json`` — otto's first public coverage export.

Versioned independently of ``store.json``: the store may be reshaped freely
for the renderer's benefit, while this file has consumers otto does not
control. Output is deterministic (tickets sorted by id, files by path,
ranges ascending, apart from the wall-clock ``generated`` stamp) so it
diffs cleanly in CI, and every path is repo-relative posix (never the
store's internal absolute path) so two machines with different workspace
roots emit identical ``path`` values for identical coverage, and an
external consumer can map a path onto its own checkout.
"""

import json
from pathlib import Path
from typing import Any

from .store.model import CoverageStore

TICKET_EXPORT_FORMAT = 1
"""``tickets.json`` schema version. Independent of ``STORE_FORMAT_VERSION``."""


def make_generated_stamp() -> str:
    """Return the current UTC time as ``tickets.json``'s ``generated`` field value.

    ``YYYY-MM-DDTHH:MM:SSZ``. Both call sites (``otto cov report
    --tickets-json`` and ``otto test --cov-tickets-json``) use this helper
    rather than each constructing the timestamp independently, so the format
    can't drift between the two entry points.
    """
    from datetime import datetime, timezone

    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def group_ranges(lines: list[int]) -> list[list[int]]:
    """Collapse a list of line numbers into sorted, inclusive ``[start, end]`` ranges.

    Consecutive integers collapse into one range; a line with no adjacent
    neighbor becomes a singleton ``[n, n]`` range. **Callers must pass
    already-deduplicated input** — a repeated line number (e.g. ``[3, 3,
    4]``) is never detected or merged, producing a non-canonical overlapping
    result (``[[3, 3], [3, 4]]``) rather than collapsing cleanly. A later
    task renders the same ranges in the UI from this helper, so it must stay
    importable and stable.
    """
    out: list[list[int]] = []
    for line in sorted(lines):
        if out and line == out[-1][1] + 1:
            out[-1][1] = line
        else:
            out.append([line, line])
    return out


def build_ticket_export(
    store: CoverageStore, *, repo_root: Path, project: str, otto_version: str, generated: str
) -> dict[str, Any]:
    """Build the ``tickets.json`` payload from an attributed store.

    *store* is a :class:`~otto.coverage.store.model.CoverageStore` that
    ticket attribution has already run over. *repo_root* is the git
    repository root every emitted ``path`` is made relative to (posix
    separators). Commit-message ticket attribution
    (``otto.coverage.reporter.CoverageReporter._annotate_tickets``)
    only ever sets ``LineRecord.ticket`` for files under *repo_root* — every
    other file is skipped there — so every file this function encounters
    with any ticket data is guaranteed relative to *repo_root*; a file
    outside it is skipped entirely (it can carry no ticket data to report).

    Raises:
        ValueError: *store* carries no ticket data at all (``store.tickets``
            is empty) — attribution never ran, because ``[coverage.tickets]``
            was never configured for this report, or the store held no
            files under *repo_root* to attribute. A git log walk that ran
            but matched no ticket pattern no longer lands here: those
            lines are attributed to the synthetic ``(no ticket)`` row (and
            an uncommitted line to ``(uncommitted)``), which populates
            ``store.tickets`` like any other id — so this only fires when
            attribution itself never ran. Writing an empty file in this
            case would read as "this project has no uncovered ticket
            work", so this fails loud instead.
    """
    if not store.tickets:
        raise ValueError(
            "no ticket data in this report — [coverage.tickets] must be configured "
            "for --tickets-json"
        )
    # The store keys files by resolved absolute path (get_or_create_file
    # resolves whenever the file exists on disk); resolve repo_root the same
    # way before every is_relative_to/relative_to comparison below —
    # mirrors CoverageReporter._annotate_tickets's identical repo_root.resolve()
    # ahead of its own is_relative_to check, for the same reason (a repo_root
    # reached through a symlink component would otherwise misclassify every
    # file in it as "outside the repo").
    resolved_root = repo_root.resolve()
    per_ticket: dict[str, dict[str, list[int]]] = {}
    covered_of: dict[str, dict[str, list[int]]] = {}
    # Per-tier counts accumulate in this same single pass over the store.
    # Recomputing them inside the per-ticket loop below would re-walk every
    # line once per ticket per tier — O(tickets x tiers x lines), tens of
    # millions of iterations on a store with hundreds of tickets.
    per_tier_of: dict[str, dict[str, int]] = {}
    # `totals` (below) is the DEDUPED repo-truth over every attributed line —
    # mirrors spa_data.py's `_build_ticket_summaries` exactly (same gate,
    # same single pass): a line owned by two tickets counts once here,
    # never once per ticket, unlike the per-ticket `owned`/`covered` below
    # it (those deliberately DO attribute a shared line to every ticket
    # that names it — see the guide's "Overlapping tickets" section).
    total_owned = 0
    total_covered = 0
    for rec in store.files():
        if not rec.path.is_relative_to(resolved_root):
            # Can carry no ticket data (attribution never sets `.ticket` for
            # files outside repo_root) — skip rather than let
            # `.relative_to()` below raise for an unrelated file.
            continue
        # Repo-relative, forward-slash normalized: this export has no
        # --prefix option (unlike the SPA's display path) and must be
        # portable and machine-independent — an absolute path (even
        # `.as_posix()`-normalized) would still bake in this machine's
        # workspace root, so two CI runners with different checkout
        # locations would emit different bytes for identical coverage.
        display = rec.path.relative_to(resolved_root).as_posix()
        for lineno, line in rec.lines.items():
            if line.ticket:
                # Deduped totals: this line counts ONCE here regardless of
                # how many tickets in `line.ticket` claim it.
                total_owned += 1
                if line.hits.is_hit():
                    total_covered += 1
            for ticket_id in line.ticket:
                per_ticket.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                if line.hits.is_hit():
                    covered_of.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                tiers = per_tier_of.setdefault(ticket_id, {})
                for tier in store.tier_order:
                    if line.hits.is_hit(tier):
                        tiers[tier] = tiers.get(tier, 0) + 1

    tickets: list[dict[str, Any]] = []
    for ticket_id in sorted(per_ticket):
        files: list[dict[str, Any]] = []
        owned = covered = 0
        for display in sorted(per_ticket[ticket_id]):
            owned_lines = sorted(per_ticket[ticket_id][display])
            hit_lines = set(covered_of.get(ticket_id, {}).get(display, []))
            missing = [n for n in owned_lines if n not in hit_lines]
            owned += len(owned_lines)
            covered += len(hit_lines)
            files.append(
                {
                    "path": display,
                    "owned": len(owned_lines),
                    "covered": len(hit_lines),
                    "missing": group_ranges(missing),
                }
            )
        record = store.tickets.get(ticket_id)
        per_tier = {tier: per_tier_of.get(ticket_id, {}).get(tier, 0) for tier in store.tier_order}
        tickets.append(
            {
                "id": ticket_id,
                "url": record.url if record else None,
                "commits": sorted(record.commits) if record else [],
                "lines": {"owned": owned, "covered": covered, "uncovered": owned - covered},
                "per_tier": per_tier,
                "files": files,
            }
        )

    return {
        "format": TICKET_EXPORT_FORMAT,
        "generated": generated,
        "otto_version": otto_version,
        "project": project,
        "traversal": "first-parent",
        "thresholds": store.thresholds.to_dict(),
        "tiers": list(store.tier_order),
        "totals": {
            "owned": total_owned,
            "covered": total_covered,
            "uncovered": total_owned - total_covered,
        },
        "tickets": tickets,
    }


def write_ticket_export(
    store: CoverageStore,
    path: Path,
    *,
    repo_root: Path,
    project: str,
    otto_version: str,
    generated: str,
) -> None:
    """Write the ``tickets.json`` payload built from *store* to *path*.

    See :func:`build_ticket_export` for *repo_root*'s role (every emitted
    path is made relative to it).

    Raises:
        ValueError: propagated from :func:`build_ticket_export` when *store*
            carries no ticket data.
    """
    payload = build_ticket_export(
        store, repo_root=repo_root, project=project, otto_version=otto_version, generated=generated
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
