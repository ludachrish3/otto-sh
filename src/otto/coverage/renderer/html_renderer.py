"""Render a :class:`CoverageStore` to an HTML report directory.

The report layout follows the familiar gcovr information architecture:

- ``index.html`` — project summary (aggregate + per-tier breakdown) and a
  sortable file table.
- ``files/<mangled_path>.html`` — per-file annotated source with a file
  summary block, a legend, and a code table that shows per-tier hit
  counts and branch status alongside the source.

Tier ordering is read from ``store.tier_order`` (the precedence order
established at load time, first = highest).  When the store has no
tiers — typically because the run produced no coverage data — the
renderer falls back to ``("system",)`` so the templates still have
something to iterate.  All per-tier columns, percentages, and the
winner-take-all row coloring on the annotated source view are driven by
that list, so adding a new tier requires no changes here.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...version import getVersion
from ..store.model import BranchHits, CoverageStore, FileRecord, LineRecord

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Pretty labels for the conventional tier names.  Tiers without an entry
# here render with their raw name title-cased — no code change needed to
# add a new tier, just an optional label tweak.
TIER_LABELS: dict[str, str] = {
    "system": "System",
    "unit": "Unit",
    "manual": "Manual",
}


def _label_for(tier: str) -> str:
    return TIER_LABELS.get(tier, tier.replace("_", " ").title())


class HtmlRenderer:
    """Render a :class:`CoverageStore` to an HTML report directory.

    Args:
        output_dir: Directory to write the report into (created if needed).
        templates_dir: Override for the default templates directory.
        project_name: Title shown in the HTML report header.
    """

    def __init__(
        self,
        output_dir: Path,
        templates_dir: Path = TEMPLATES_DIR,
        project_name: str = "Coverage Report",
    ) -> None:
        self.output_dir = output_dir
        self.project_name = project_name
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html"]),
        )
        cast(dict[str, Any], self.env.globals)["pct_class"] = self._pct_class

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def render(self, store: CoverageStore) -> None:
        """Render the full HTML report."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._copy_static()
        tier_order = self._effective_tier_order(store)
        tier_labels = {t: _label_for(t) for t in tier_order}
        otto_version = getVersion()
        self._render_index(store, tier_order, tier_labels, otto_version)
        for file_record in store.files():
            self._render_file(file_record, tier_order, tier_labels, otto_version)
        logger.info("Report written to %s", self.output_dir / "index.html")

    @staticmethod
    def _effective_tier_order(store: CoverageStore) -> list[str]:
        if store.tier_order:
            return list(store.tier_order)
        return ["system"]

    # ------------------------------------------------------------------
    # Index page
    # ------------------------------------------------------------------

    def _render_index(
        self,
        store: CoverageStore,
        tier_order: list[str],
        tier_labels: dict[str, str],
        otto_version: str,
    ) -> None:
        template = self.env.get_template("index.html")
        files_data = [self._build_file_row(fr, tier_order) for fr in store.files()]

        summary = {
            "file_count": store.file_count(),
            **self._store_totals(store, tier_order),
        }

        out = self.output_dir / "index.html"
        out.write_text(
            template.render(
                project_name=self.project_name,
                generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                files=files_data,
                summary=summary,
                tier_order=tier_order,
                tier_labels=tier_labels,
                otto_version=otto_version,
            )
        )

    def _build_file_row(self, fr: FileRecord, tier_order: list[str]) -> dict:
        """Build the template context for one row of the files table."""
        totals = self._file_totals(fr, tier_order)
        return {
            "path": str(fr.path),
            "display_path": self._display_path(fr),
            "link": self._file_link(fr),
            "line_count": totals["lines_total"],
            "pct_total": totals["pct_total"],
            "branch_pct_total": totals["branch_pct_total"],
            "per_tier": totals["per_tier"],
        }

    # ------------------------------------------------------------------
    # Per-file page
    # ------------------------------------------------------------------

    def _render_file(
        self,
        record: FileRecord,
        tier_order: list[str],
        tier_labels: dict[str, str],
        otto_version: str,
    ) -> None:
        template = self.env.get_template("file.html")

        try:
            source_lines = record.path.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            source_lines = []

        annotated_lines = [
            self._build_line_row(i, text, record.lines.get(i), tier_order)
            for i, text in enumerate(source_lines, start=1)
        ]

        file_summary = self._file_totals(record, tier_order)

        out = self.output_dir / self._file_link(record)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            template.render(
                project_name=self.project_name,
                file_path=str(record.path),
                display_path=self._display_path(record),
                lines=annotated_lines,
                file_summary=file_summary,
                tier_order=tier_order,
                tier_labels=tier_labels,
                otto_version=otto_version,
            )
        )

    def _build_line_row(
        self,
        lineno: int,
        source_text: str,
        lr: LineRecord | None,
        tier_order: list[str],
    ) -> dict:
        """Build the template context for one row of the source table."""
        coverable = lr is not None
        if lr is None:
            tier_hits = {t: 0 for t in tier_order}
            row_class = "line-uncoverable"
            branches: list[dict] = []
        else:
            tier_hits = {t: lr.hits.for_tier(t) for t in tier_order}
            row_class = self._row_class_for(lr, tier_order)
            branches = [self._build_branch(b, tier_order) for b in lr.branches]

        return {
            "number": lineno,
            "source": source_text,
            "coverable": coverable,
            "tier_hits": tier_hits,
            "branches": branches,
            "row_class": row_class,
        }

    @staticmethod
    def _row_class_for(lr: LineRecord, tier_order: list[str]) -> str:
        """Winner-take-all: use the highest-precedence tier that hit the line."""
        for tier in tier_order:
            if lr.hits.for_tier(tier) > 0:
                return f"line-hit-{tier}"
        return "line-missed"

    @staticmethod
    def _build_branch(branch: BranchHits, tier_order: list[str]) -> dict:
        if branch.hits.total() > 0:
            pill_class = "branch-taken"
        elif branch.is_reachable():
            pill_class = "branch-not-taken"
        else:
            pill_class = "branch-unreachable"

        tip_parts = [f"block={branch.block} branch={branch.branch}"]
        for tier in tier_order:
            tip_parts.append(
                f"{tier}: hits={branch.hits.for_tier(tier)} "
                f"reach={branch.is_reachable(tier)}"
            )
        return {
            "block": branch.block,
            "branch": branch.branch,
            "pill_class": pill_class,
            "tooltip": " | ".join(tip_parts),
        }

    # ------------------------------------------------------------------
    # Aggregations (lines / branches → hit/total/%)
    # ------------------------------------------------------------------

    def _file_totals(self, fr: FileRecord, tier_order: list[str]) -> dict:
        """Compute aggregate + per-tier counts and percentages for a file."""
        lines_total = len(fr.lines)
        lines_hit = sum(1 for l in fr.lines.values() if l.hits.is_hit())
        all_branches = [b for lr in fr.lines.values() for b in lr.branches]
        branches_total = sum(1 for b in all_branches if b.is_reachable() is True)
        branches_hit = sum(
            1 for b in all_branches
            if b.is_reachable() is True and b.hits.total() > 0
        )

        per_tier = {}
        for tier in tier_order:
            lh = sum(
                1 for l in fr.lines.values()
                if l.hits.for_tier(tier) > 0
            )
            bt = sum(
                1 for b in all_branches
                if b.is_reachable(tier) is True
            )
            bh = sum(
                1 for b in all_branches
                if b.is_reachable(tier) is True
                and b.hits.for_tier(tier) > 0
            )
            per_tier[tier] = {
                "lines_total": lines_total,
                "lines_hit": lh,
                "line_pct": _pct(lh, lines_total),
                "branches_total": bt,
                "branches_hit": bh,
                "branch_pct": _pct(bh, bt),
            }

        return {
            "lines_total": lines_total,
            "lines_hit": lines_hit,
            "pct_total": _pct(lines_hit, lines_total),
            "branches_total": branches_total,
            "branches_hit": branches_hit,
            "branch_pct_total": _pct(branches_hit, branches_total),
            "per_tier": per_tier,
        }

    def _store_totals(self, store: CoverageStore, tier_order: list[str]) -> dict:
        """Compute aggregate + per-tier counts and percentages for the whole store."""
        lines_total = 0
        lines_hit = 0
        branches_total = 0
        branches_hit = 0
        per_tier_counts = {
            t: {"lt": 0, "lh": 0, "bt": 0, "bh": 0} for t in tier_order
        }

        for fr in store.files():
            lines_total += len(fr.lines)
            lines_hit += sum(1 for l in fr.lines.values() if l.hits.is_hit())
            all_branches = [b for lr in fr.lines.values() for b in lr.branches]
            branches_total += sum(1 for b in all_branches if b.is_reachable() is True)
            branches_hit += sum(
                1 for b in all_branches
                if b.is_reachable() is True and b.hits.total() > 0
            )
            for tier in tier_order:
                bucket = per_tier_counts[tier]
                bucket["lt"] += len(fr.lines)
                bucket["lh"] += sum(
                    1 for l in fr.lines.values()
                    if l.hits.for_tier(tier) > 0
                )
                bucket["bt"] += sum(
                    1 for b in all_branches
                    if b.is_reachable(tier) is True
                )
                bucket["bh"] += sum(
                    1 for b in all_branches
                    if b.is_reachable(tier) is True
                    and b.hits.for_tier(tier) > 0
                )

        per_tier = {
            tier: {
                "lines_total": c["lt"],
                "lines_hit": c["lh"],
                "line_pct": _pct(c["lh"], c["lt"]),
                "branches_total": c["bt"],
                "branches_hit": c["bh"],
                "branch_pct": _pct(c["bh"], c["bt"]),
            }
            for tier, c in per_tier_counts.items()
        }
        return {
            "lines_total": lines_total,
            "lines_hit": lines_hit,
            "pct_total": _pct(lines_hit, lines_total),
            "branches_total": branches_total,
            "branches_hit": branches_hit,
            "branch_pct_total": _pct(branches_hit, branches_total),
            "per_tier": per_tier,
        }

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pct_class(pct: float) -> str:
        """Return a CSS class name based on a coverage percentage."""
        if pct >= 75.0:
            return "pct-high"
        if pct >= 50.0:
            return "pct-mid"
        return "pct-low"

    @staticmethod
    def _display_path(record: FileRecord) -> str:
        return str(record.path)

    @staticmethod
    def _file_link(record: FileRecord) -> str:
        safe = str(record.path).replace("/", "_").replace("\\", "_").lstrip("_")
        return f"files/{safe}.html"

    def _copy_static(self) -> None:
        static_dst = self.output_dir / "static"
        if STATIC_DIR.exists():
            shutil.copytree(str(STATIC_DIR), str(static_dst), dirs_exist_ok=True)


def _pct(hit: int, total: int) -> float:
    if total == 0:
        return 0.0
    return (hit / total) * 100.0
