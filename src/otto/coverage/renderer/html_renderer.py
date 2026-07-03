"""Render a :class:`~otto.coverage.store.model.CoverageStore` to an HTML report directory.

The report layout follows the familiar gcovr information architecture:

- ``index.html`` — project summary (aggregate + per-tier breakdown), a
  legend, a "Captures" provenance table (when the store has any), and a
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

**Row precedence (spec §9).**  Each annotated source line resolves to
exactly one CSS class, in this order: ``state-excluded`` (source-scanned
exclusion markers, spec §8 — always wins, even over a covered/stale/aging
line) → ``tier-<index>`` (the highest-precedence tier in ``tier_order``
with a nonzero hit count on that line) → ``state-aging`` → ``state-stale``
→ ``state-uncovered``.  A line with no :class:`~otto.coverage.store.model.LineRecord`
at all (not excluded, never measured by any tier — e.g. a blank line or a
declaration gcov never emits a ``DA:`` for) gets ``state-uncoverable``, a
sixth, deliberately-unlisted bucket so those lines don't read as bright-red
misses.

Colors are resolved once per report and emitted as CSS custom properties
(``--tier-<index>``, ``--state-<name>``) in an inline ``<style>`` block on
both pages; ``report.css`` consumes them via ``color-mix()`` so the actual
row background never needs a template-side computation.
"""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ...version import get_version
from ..store.model import BranchHits, CoverageStore, FileRecord, LineRecord

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Coverage percentage thresholds for CSS class assignment (pct-high / pct-mid / pct-low).
_PCT_HIGH_THRESHOLD = 75.0
_PCT_MID_THRESHOLD = 50.0

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
    """Render a :class:`~otto.coverage.store.model.CoverageStore` to an HTML report directory.

    Args:
        output_dir: Directory to write the report into (created if needed).
        templates_dir: Override for the default templates directory.
        project_name: Title shown in the HTML report header.
        extra_markers: Extra source exclusion-marker strings (spec §8),
            forwarded from ``[coverage.exclusions].markers`` via the
            reporter.  Scanned alongside the built-in ``LCOV_EXCL_*``
            markers when annotating each file's source.
    """

    def __init__(
        self,
        output_dir: Path,
        templates_dir: Path = TEMPLATES_DIR,
        project_name: str = "Coverage Report",
        *,
        extra_markers: list[str] | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.project_name = project_name
        self.extra_markers: list[str] = list(extra_markers or [])
        # Deferred so importing the renderer module (and thus `otto.coverage`,
        # pulled onto the CLI startup path via cli.cov) does not load jinja2.
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html"]),
        )
        cast("dict[str, Any]", self.env.globals)["pct_class"] = self._pct_class

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def render(self, store: CoverageStore) -> None:
        """Render the full HTML report."""
        # Deferred: otto.coverage.colors is a cheap pure-python module, but
        # keeping it out of this module's top-level imports keeps it out of
        # the `otto cov --help` import surface too (that surface imports
        # this module but never calls render()).
        from ..colors import DEFAULT_TIER_COLORS, STATE_COLORS

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._copy_static()
        tier_order = self._effective_tier_order(store)
        tier_labels = {t: _label_for(t) for t in tier_order}
        tier_colors = self._resolve_tier_colors(store, tier_order, DEFAULT_TIER_COLORS)
        otto_version = get_version()

        files_data = []
        for file_record in store.files():
            excluded_count = self._render_file(
                file_record,
                tier_order,
                tier_labels,
                tier_colors,
                STATE_COLORS,
                otto_version,
            )
            files_data.append(self._build_file_row(file_record, tier_order, excluded_count))

        self._render_index(
            store,
            tier_order,
            tier_labels,
            tier_colors,
            STATE_COLORS,
            files_data,
            otto_version,
        )
        logger.info("Report written to %s", self.output_dir / "index.html")

    @staticmethod
    def _effective_tier_order(store: CoverageStore) -> list[str]:
        if store.tier_order:
            return list(store.tier_order)
        return ["system"]

    @staticmethod
    def _resolve_tier_colors(
        store: CoverageStore,
        tier_order: list[str],
        default_tier_colors: dict[str, str],
    ) -> dict[str, str]:
        """Per-tier CSS colors: ``store.tier_colors`` first, then a best-effort default.

        ``store.tier_colors`` is a name -> color map filled by the reporter
        from declared tier settings.  A tier with no entry there (e.g. the
        git-less ``--tier`` escape hatch, which carries no color info at all)
        falls back to :data:`otto.coverage.colors.DEFAULT_TIER_COLORS` indexed
        by the tier **name**.  That default map is keyed by tier *kind*
        (``e2e``/``unit``/``manual``), so the fallback only lands a real color
        when a tier's name coincides with a kind key (``"unit"``/``"manual"``);
        every other name — including the conventional ``"system"`` (kind
        ``e2e``) — falls through to plain ``"green"``.
        """
        colors: dict[str, str] = {}
        for tier in tier_order:
            colors[tier] = store.tier_colors.get(tier) or default_tier_colors.get(tier, "green")
        return colors

    # ------------------------------------------------------------------
    # Index page
    # ------------------------------------------------------------------

    def _render_index(
        self,
        store: CoverageStore,
        tier_order: list[str],
        tier_labels: dict[str, str],
        tier_colors: dict[str, str],
        state_colors: dict[str, str],
        files_data: list[dict[str, Any]],
        otto_version: str,
    ) -> None:
        template = self.env.get_template("index.html")

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
                tier_colors=tier_colors,
                state_colors=state_colors,
                provenance=store.provenance,
                otto_version=otto_version,
            )
        )

    def _build_file_row(
        self, fr: FileRecord, tier_order: list[str], excluded_count: int
    ) -> dict[str, Any]:
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
            "stale_count": totals["stale_count"],
            "aging_count": totals["aging_count"],
            "excluded_count": excluded_count,
        }

    # ------------------------------------------------------------------
    # Per-file page
    # ------------------------------------------------------------------

    def _render_file(
        self,
        record: FileRecord,
        tier_order: list[str],
        tier_labels: dict[str, str],
        tier_colors: dict[str, str],
        state_colors: dict[str, str],
        otto_version: str,
    ) -> int:
        """Render one annotated-source page; returns its excluded-line count.

        The excluded-line scan (spec §8) runs once here, reusing this same
        source read, and its result both drives each line's row class and
        feeds the file's excluded count — shared with the index's per-file
        column via the return value, so the source is never re-read.
        """
        from ..exclusions import scan_excluded_lines

        template = self.env.get_template("file.html")

        try:
            source_text = record.path.read_text(errors="replace")
        except OSError as e:
            logger.warning(
                "Could not read source %s (%s); its annotated page will be empty.",
                record.path,
                e,
            )
            source_text = ""
        source_lines = source_text.splitlines()
        excluded_linenos = scan_excluded_lines(source_text, self.extra_markers or None)
        # Annotate the store with the source-scanned exclusions (spec §9): the
        # reporter renders before it saves store.json, so this flows through
        # to the serialised store for frontend consumers.
        record.excluded_lines = excluded_linenos

        annotated_lines = [
            self._build_line_row(i, text, record.lines.get(i), tier_order, i in excluded_linenos)
            for i, text in enumerate(source_lines, start=1)
        ]

        file_summary = self._file_totals(record, tier_order)
        file_summary["excluded_count"] = len(excluded_linenos)

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
                tier_colors=tier_colors,
                state_colors=state_colors,
                otto_version=otto_version,
            )
        )
        return len(excluded_linenos)

    def _build_line_row(
        self,
        lineno: int,
        source_text: str,
        lr: LineRecord | None,
        tier_order: list[str],
        excluded: bool,
    ) -> dict[str, Any]:
        """Build the template context for one row of the source table."""
        coverable = lr is not None
        if lr is None:
            tier_hits = dict.fromkeys(tier_order, 0)
            branches: list[dict[str, Any]] = []
        else:
            tier_hits = {t: lr.hits.for_tier(t) for t in tier_order}
            branches = [self._build_branch(b, tier_order) for b in lr.branches]

        return {
            "number": lineno,
            "source": source_text,
            "coverable": coverable,
            "tier_hits": tier_hits,
            "branches": branches,
            "row_class": self._row_class_for(lr, tier_order, excluded),
        }

    @staticmethod
    def _row_class_for(lr: LineRecord | None, tier_order: list[str], excluded: bool) -> str:
        """Resolve one line's CSS class per the module-level precedence order."""
        if excluded:
            return "state-excluded"
        if lr is None:
            return "state-uncoverable"
        for i, tier in enumerate(tier_order):
            if lr.hits.for_tier(tier) > 0:
                return f"tier-{i}"
        if lr.state == "aging":
            return "state-aging"
        if lr.state == "stale":
            return "state-stale"
        return "state-uncovered"

    @staticmethod
    def _build_branch(branch: BranchHits, tier_order: list[str]) -> dict[str, Any]:
        if branch.hits.total() > 0:
            pill_class = "branch-taken"
        elif branch.is_reachable():
            pill_class = "branch-not-taken"
        else:
            pill_class = "branch-unreachable"

        tip_parts = [f"block={branch.block} branch={branch.branch}"]
        tip_parts.extend(
            f"{tier}: hits={branch.hits.for_tier(tier)} reach={branch.is_reachable(tier)}"
            for tier in tier_order
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

    def _file_totals(self, fr: FileRecord, tier_order: list[str]) -> dict[str, Any]:
        """Compute aggregate + per-tier counts and percentages for a file.

        ``stale_count``/``aging_count`` iterate every :class:`LineRecord` in
        the file, not just lines within the current source's line range —
        state-only records past EOF (shrunk-file tolerance, spec §7) still
        count here even though they get no annotated row.
        """
        lines_total = len(fr.lines)
        lines_hit = sum(1 for line_rec in fr.lines.values() if line_rec.hits.is_hit())
        all_branches = [b for lr in fr.lines.values() for b in lr.branches]
        branches_total = sum(1 for b in all_branches if b.is_reachable() is True)
        branches_hit = sum(
            1 for b in all_branches if b.is_reachable() is True and b.hits.total() > 0
        )
        stale_count = sum(1 for line_rec in fr.lines.values() if line_rec.state == "stale")
        aging_count = sum(1 for line_rec in fr.lines.values() if line_rec.state == "aging")

        per_tier = {}
        for tier in tier_order:
            lh = sum(1 for line_rec in fr.lines.values() if line_rec.hits.for_tier(tier) > 0)
            bt = sum(1 for b in all_branches if b.is_reachable(tier) is True)
            bh = sum(
                1
                for b in all_branches
                if b.is_reachable(tier) is True and b.hits.for_tier(tier) > 0
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
            "stale_count": stale_count,
            "aging_count": aging_count,
        }

    def _store_totals(self, store: CoverageStore, tier_order: list[str]) -> dict[str, Any]:
        """Compute aggregate + per-tier counts and percentages for the whole store."""
        lines_total = 0
        lines_hit = 0
        branches_total = 0
        branches_hit = 0
        per_tier_counts = {t: {"lt": 0, "lh": 0, "bt": 0, "bh": 0} for t in tier_order}

        for fr in store.files():
            lines_total += len(fr.lines)
            lines_hit += sum(1 for line_rec in fr.lines.values() if line_rec.hits.is_hit())
            all_branches = [b for lr in fr.lines.values() for b in lr.branches]
            branches_total += sum(1 for b in all_branches if b.is_reachable() is True)
            branches_hit += sum(
                1 for b in all_branches if b.is_reachable() is True and b.hits.total() > 0
            )
            for tier in tier_order:
                bucket = per_tier_counts[tier]
                bucket["lt"] += len(fr.lines)
                bucket["lh"] += sum(
                    1 for line_rec in fr.lines.values() if line_rec.hits.for_tier(tier) > 0
                )
                bucket["bt"] += sum(1 for b in all_branches if b.is_reachable(tier) is True)
                bucket["bh"] += sum(
                    1
                    for b in all_branches
                    if b.is_reachable(tier) is True and b.hits.for_tier(tier) > 0
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
        if pct >= _PCT_HIGH_THRESHOLD:
            return "pct-high"
        if pct >= _PCT_MID_THRESHOLD:
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
