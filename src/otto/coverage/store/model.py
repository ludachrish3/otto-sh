"""Coverage data model.

This is the core of the coverage module — everything else feeds into or
reads from these types.  The model is deliberately independent of gcov,
lcov, or coverage.py internals.

Coverage tiers are *names* — free-form strings like ``"system"``,
``"unit"``, ``"manual"``, or anything a user wires up on the command
line.  ``LineHits`` and ``BranchHits`` store per-tier counts in dicts
keyed by tier name, so adding a new tier requires no changes to the
model.  A :class:`CoverageStore` also carries a ``tier_order`` list that
defines the precedence of tiers for presentation purposes (first =
highest precedence).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIER_SYSTEM = "system"
"""Conventional tier name used by the merged .gcda pipeline.

Any string is a valid tier name; this constant spares callers a string
literal when they mean the canonical system-coverage tier.
"""

STORE_FORMAT_VERSION = 3
"""``store.json`` schema version, bumped on breaking on-disk changes.

Version 2 is the first version to carry an explicit ``"format"`` key —
introduced alongside a run-table field/key rename that shortened every
per-record and per-line name (JSON keys and the record class/attribute
names alike) by dropping a now-redundant qualifying word.  Version 3
renames the run table's git-anchor field from ``pin`` to
``base_commit``, matching the same rename on the capture artifact it is
sourced from.  There is no migration shim: a file that does not declare
this exact version fails loud in :meth:`CoverageStore.load` with a
message telling the caller to regenerate it, rather than silently
mis-reading renamed/reshaped keys under old names.
"""


@dataclass
class LineHits:
    """Per-tier hit counts for a single line.

    Counts are a dict keyed by tier name.  Absent keys mean zero hits
    for that tier.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def add(self, tier: str, n: int) -> None:
        """Add *n* hits to *tier*."""
        self.counts[tier] = self.counts.get(tier, 0) + n

    def for_tier(self, tier: str) -> int:
        """Hit count for *tier* (0 if the tier has no entry)."""
        return self.counts.get(tier, 0)

    def total(self) -> int:
        """Sum of hits across all tiers."""
        return sum(self.counts.values())

    def is_hit(self, tier: str | None = None) -> bool:
        """Return True if hit in *tier* (or in any tier when ``tier`` is None)."""
        if tier is None:
            return self.total() > 0
        return self.for_tier(tier) > 0

    def merge(self, other: "LineHits") -> None:
        """Additive merge — sum counts for every tier present in *other*."""
        for tier, count in other.counts.items():
            self.add(tier, count)

    def to_dict(self) -> dict[str, int]:
        """Return a plain dict copy of per-tier hit counts."""
        return dict(self.counts)


@dataclass
class BranchHits:
    """Per-tier hit counts and reachability for one branch.

    Reachability is tri-state per tier:

    - key present, value ``True``  → observed as reachable at least once
    - key present, value ``False`` → observed only as unreachable (lcov ``-``)
    - key absent                  → no data yet for that tier

    Once a tier sees the branch as reachable it stays reachable for that
    tier — merges only flip ``False`` → ``True``, never the other way.
    """

    block: int
    branch: int

    hits: LineHits = field(default_factory=LineHits)
    reachable: dict[str, bool] = field(default_factory=dict)

    @property
    def branch_id(self) -> tuple[int, int]:
        """``(block, branch)`` tuple that uniquely identifies this branch within a line."""
        return (self.block, self.branch)

    def set_reachable(self, tier: str, reachable: bool) -> None:
        """Record a reachability observation for *tier*.

        If the tier was already marked reachable, keep it reachable.
        Otherwise adopt the new value.
        """
        prev = self.reachable.get(tier, False)
        self.reachable[tier] = prev or reachable

    def is_reachable(self, tier: str | None = None) -> bool | None:
        """Reachability for *tier*, or combined across all tiers when None.

        Returns ``None`` if no data has been recorded for the requested
        tier (or for any tier when ``tier`` is None).
        """
        if tier is None:
            if not self.reachable:
                return None
            return any(self.reachable.values())
        return self.reachable.get(tier)

    def is_hit_for(self, tier: str | None = None) -> bool:
        """Return True if this branch was taken at least once in *tier*."""
        if tier is None:
            return self.hits.is_hit()
        return self.hits.for_tier(tier) > 0

    def merge(self, other: "BranchHits") -> None:
        """Merge *other* into this branch, accumulating hits and updating reachability."""
        assert self.block == other.block  # noqa: S101 — internal invariant: callers must only merge matching branch keys
        assert self.branch == other.branch  # noqa: S101 — internal invariant: callers must only merge matching branch keys
        self.hits.merge(other.hits)
        for tier, reachable in other.reachable.items():
            self.set_reachable(tier, reachable)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this branch record."""
        return {
            "block": self.block,
            "branch": self.branch,
            "hits": self.hits.to_dict(),
            "reachable": dict(self.reachable),
        }


@dataclass
class RunRecord:
    """One coverage run in the report — a capture or a synthetic per-tier load.

    The run table (``CoverageStore.runs``) is derived fresh at report
    time from the capture inputs; ``id`` is the record's index into that
    list.  ``label`` is what the drilldown chip shows: the host display
    name when the capture carries one, else the board (host id), else the
    tier name (synthetic runs pass neither).
    """

    id: int
    tier: str
    label: str
    board: str = ""
    labs: list[str] = field(default_factory=list)
    captured_at: str = ""
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    base_commit: str = ""
    dirty_remap: bool = False
    aging: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this run."""
        return {
            "id": self.id,
            "tier": self.tier,
            "label": self.label,
            "board": self.board,
            "labs": list(self.labs),
            "captured_at": self.captured_at,
            "tester": self.tester,
            "ticket": self.ticket,
            "note": self.note,
            "base_commit": self.base_commit,
            "dirty_remap": self.dirty_remap,
            "aging": self.aging,
        }


@dataclass
class LineRecord:
    """Coverage data for a single source line."""

    line_number: int
    hits: LineHits = field(default_factory=LineHits)
    branches: list[BranchHits] = field(default_factory=list)

    # Set by the manual-capture validity pass (spec §7): "stale" (anchored
    # evidence no longer matches the current source), "aging" (valid but
    # older than the configured max age), or None (normal).
    state: str | None = None

    # Per-run traceability (run-contexts spec §5): hits keyed by run id
    # (index into CoverageStore.runs), and the ids of runs whose
    # evidence for this line was revoked by the manual-validity pass.
    run_hits: dict[int, int] = field(default_factory=dict)
    stale_runs: list[int] = field(default_factory=list)

    def merge(self, other: "LineRecord") -> None:
        """Merge *other* into this line record, accumulating hits and branch data."""
        assert self.line_number == other.line_number  # noqa: S101 — internal invariant: callers must only merge matching line numbers
        self.hits.merge(other.hits)

        existing = {(b.block, b.branch): b for b in self.branches}
        for other_branch in other.branches:
            key = (other_branch.block, other_branch.branch)
            if key in existing:
                existing[key].merge(other_branch)
            else:
                clone = BranchHits(
                    block=other_branch.block,
                    branch=other_branch.branch,
                    hits=LineHits(counts=dict(other_branch.hits.counts)),
                    reachable=dict(other_branch.reachable),
                )
                self.branches.append(clone)

        for run_id, count in other.run_hits.items():
            self.run_hits[run_id] = self.run_hits.get(run_id, 0) + count
        for run_id in other.stale_runs:
            if run_id not in self.stale_runs:
                self.stale_runs.append(run_id)


@dataclass
class FileRecord:
    """Coverage data for a single source file."""

    path: Path
    lines: dict[int, LineRecord] = field(default_factory=dict)

    # Source-scanned exclusion markers (spec §8/§9 frontend contract). This
    # is render-time state, not measured coverage: the renderer scans the
    # file's source for ``LCOV_EXCL_*`` (and any extra markers) once and
    # annotates the store here before it is serialised, so ``store.json``
    # consumers can distinguish excluded lines from plain misses.
    excluded_lines: set[int] = field(default_factory=set)

    def get_or_create_line(self, line_number: int) -> LineRecord:
        """Return the :class:`LineRecord` for *line_number*, creating it if absent."""
        if line_number not in self.lines:
            self.lines[line_number] = LineRecord(line_number=line_number)
        return self.lines[line_number]

    def merge(self, other: "FileRecord") -> None:
        """Merge *other* into this file record, combining per-line hits and branches."""
        assert self.path == other.path  # noqa: S101 — internal invariant: callers must only merge records for the same file path
        for lineno, other_line in other.lines.items():
            if lineno in self.lines:
                self.lines[lineno].merge(other_line)
            else:
                # Start empty and let merge copy hits/branches/runs —
                # pre-seeding the clone with copied counts and then merging
                # doubled every hit.
                clone = LineRecord(line_number=lineno)
                clone.merge(other_line)
                self.lines[lineno] = clone

    def _all_branches(self) -> list[BranchHits]:
        return [b for lr in self.lines.values() for b in lr.branches]

    def line_coverage_pct(self, tier: str | None = None) -> float:
        """Line coverage percentage, optionally filtered by tier."""
        if not self.lines:
            return 0.0
        hit = sum(1 for line_rec in self.lines.values() if line_rec.hits.is_hit(tier))
        return (hit / len(self.lines)) * 100

    def branch_coverage_pct(self, tier: str | None = None, conservative: bool = True) -> float:
        """Branch coverage percentage.

        *conservative=True* (default): denominator is only branches where
        ``is_reachable(tier)`` is ``True``.  Matches genhtml behaviour.

        *conservative=False*: denominator is all branches seen in any
        ``.info`` file.
        """
        all_branches = self._all_branches()
        if not all_branches:
            return 0.0
        if conservative:
            candidates = [b for b in all_branches if b.is_reachable(tier) is True]
        else:
            candidates = all_branches
        if not candidates:
            return 0.0
        hit = sum(1 for b in candidates if b.is_hit_for(tier))
        return (hit / len(candidates)) * 100

    def sorted_lines(self) -> Iterator[LineRecord]:
        """Yield :class:`LineRecord` objects in ascending line-number order."""
        yield from (self.lines[k] for k in sorted(self.lines))

    @staticmethod
    def _line_to_dict(rec: "LineRecord") -> dict[str, Any]:
        d: dict[str, Any] = {
            "hits": rec.hits.to_dict(),
            "branches": [b.to_dict() for b in rec.branches],
            "state": rec.state,
        }
        if rec.run_hits:
            d["run"] = {str(rid): n for rid, n in rec.run_hits.items()}
        if rec.stale_runs:
            d["stale_run"] = list(rec.stale_runs)
        return d

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this file record."""
        return {
            "path": str(self.path),
            "lines": {str(lineno): self._line_to_dict(rec) for lineno, rec in self.lines.items()},
            "excluded_lines": sorted(self.excluded_lines),
        }


class CoverageStore:
    """Central store for all coverage data across all files, hosts, and tiers.

    ``tier_order`` holds the user-defined precedence of tiers — first
    entry is highest precedence.  Downstream renderers iterate this list
    to drive column ordering and the winner-take-all row coloring used
    by the annotated source view.  ``runs`` captures the run table —
    each run record corresponds to one coverage run or synthetic per-tier
    aggregate loaded into the store.
    """

    def __init__(self, tier_order: list[str] | None = None) -> None:
        self._files: dict[Path, FileRecord] = {}
        self.tier_order: list[str] = list(tier_order) if tier_order else []
        self.tier_colors: dict[str, str] = {}
        self.runs: list[RunRecord] = []

    def register_tier(self, tier: str) -> None:
        """Append *tier* to the tier order if not already present.

        Loaders call this when they see a new tier so the store's
        ``tier_order`` list stays in sync with the data without requiring
        every call site to pre-declare every tier.
        """
        if tier not in self.tier_order:
            self.tier_order.append(tier)

    def add_run(
        self,
        *,
        tier: str,
        label: str | None = None,
        board: str = "",
        labs: list[str] | None = None,
        captured_at: str = "",
        tester: dict[str, str] | None = None,
        ticket: str | None = None,
        note: str | None = None,
        base_commit: str = "",
        dirty_remap: bool = False,
    ) -> int:
        """Register one run and return its run id (index into ``runs``).

        ``label`` falls back to ``board``, then to ``tier`` — the synthetic
        per-tier runs pass neither.  Also registers *tier* so the run
        table can never reference an unknown tier.
        """
        self.register_tier(tier)
        run_id = len(self.runs)
        self.runs.append(
            RunRecord(
                id=run_id,
                tier=tier,
                label=label or board or tier,
                board=board,
                labs=list(labs or []),
                captured_at=captured_at,
                tester=tester,
                ticket=ticket,
                note=note,
                base_commit=base_commit,
                dirty_remap=dirty_remap,
            )
        )
        return run_id

    def get_or_create_file(self, path: Path) -> FileRecord:
        """Return the :class:`FileRecord` for *path*, creating it if absent.

        Resolves *path* to its canonical form when the file exists on disk,
        so duplicate entries from different relative or symlinked references
        to the same file are collapsed to one record.
        """
        canonical = path.resolve() if path.exists() else path
        if canonical not in self._files:
            self._files[canonical] = FileRecord(path=canonical)
        return self._files[canonical]

    def merge_file(self, record: FileRecord) -> None:
        """Merge *record* into the store, accumulating data for an existing path or inserting it."""
        if record.path in self._files:
            self._files[record.path].merge(record)
        else:
            self._files[record.path] = record

    def files(self) -> Iterator[FileRecord]:
        """Yield all :class:`FileRecord` objects in sorted path order."""
        yield from sorted(self._files.values(), key=lambda f: f.path)

    def file_count(self) -> int:
        """Return the number of source files tracked in the store."""
        return len(self._files)

    def _all_branches(self) -> list[BranchHits]:
        return [b for f in self._files.values() for lr in f.lines.values() for b in lr.branches]

    def overall_pct(self, tier: str | None = None) -> float:
        """Overall line coverage percentage across all files."""
        all_lines = sum(len(f.lines) for f in self._files.values())
        if all_lines == 0:
            return 0.0
        hit = sum(
            sum(1 for line_rec in f.lines.values() if line_rec.hits.is_hit(tier))
            for f in self._files.values()
        )
        return (hit / all_lines) * 100

    def overall_branch_pct(self, tier: str | None = None, conservative: bool = True) -> float:
        """Overall branch coverage percentage across all files."""
        all_branches = self._all_branches()
        if not all_branches:
            return 0.0
        if conservative:
            candidates = [b for b in all_branches if b.is_reachable(tier) is True]
        else:
            candidates = all_branches
        if not candidates:
            return 0.0
        hit = sum(1 for b in candidates if b.is_hit_for(tier))
        return (hit / len(candidates)) * 100

    def save(self, path: Path) -> None:
        """Serialise the store to JSON."""
        data = {
            "format": STORE_FORMAT_VERSION,
            "tier_order": list(self.tier_order),
            "files": [f.to_dict() for f in self.files()],
            "runs": [r.to_dict() for r in self.runs],
            "tier_colors": dict(self.tier_colors),
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> "CoverageStore":
        """Deserialise a store from JSON.

        Raises:
            ValueError: The file's ``"format"`` key does not match
                :data:`STORE_FORMAT_VERSION` (including files with no
                ``"format"`` key at all — everything predating this
                store-format version).  There is no migration shim; the
                message tells the caller to regenerate the store.
        """
        data = json.loads(path.read_text())
        found_format = data.get("format") if isinstance(data, dict) else None
        if found_format != STORE_FORMAT_VERSION:
            found_label = f"v{found_format}" if isinstance(found_format, int) else "none"
            raise ValueError(
                f"coverage store format v{STORE_FORMAT_VERSION} required; "
                f"found {found_label} — regenerate with otto cov get/report"
            )

        tier_order = data.get("tier_order") or []
        files_data = data.get("files", [])
        tier_colors = data.get("tier_colors") or {}
        runs_data = data.get("runs") or []

        store = cls(tier_order=tier_order)
        store.tier_colors = dict(tier_colors)
        for rd in runs_data:
            store.runs.append(
                RunRecord(
                    id=rd["id"],
                    tier=rd["tier"],
                    label=rd["label"],
                    board=rd.get("board", ""),
                    labs=list(rd.get("labs") or []),
                    captured_at=rd.get("captured_at", ""),
                    tester=rd.get("tester"),
                    ticket=rd.get("ticket"),
                    note=rd.get("note"),
                    base_commit=rd.get("base_commit", ""),
                    dirty_remap=rd.get("dirty_remap", False),
                    aging=rd.get("aging", False),
                )
            )
        for fd in files_data:
            record = FileRecord(path=Path(fd["path"]))
            record.excluded_lines = set(fd.get("excluded_lines") or [])
            for lineno_str, ld in fd["lines"].items():
                hits = LineHits(counts=dict(ld["hits"]))
                branches = []
                for bd in ld.get("branches", []):
                    bh = BranchHits(
                        block=bd["block"],
                        branch=bd["branch"],
                        hits=LineHits(counts=dict(bd["hits"])),
                        reachable=dict(bd.get("reachable") or {}),
                    )
                    branches.append(bh)
                lr = LineRecord(
                    line_number=int(lineno_str),
                    hits=hits,
                    branches=branches,
                    state=ld.get("state"),
                )
                lr.run_hits = {int(k): v for k, v in (ld.get("run") or {}).items()}
                lr.stale_runs = list(ld.get("stale_run") or [])
                record.lines[int(lineno_str)] = lr
                for tier in hits.counts:
                    store.register_tier(tier)
                for b in branches:
                    for tier in b.hits.counts:
                        store.register_tier(tier)
                    for tier in b.reachable:
                        store.register_tier(tier)
            store.merge_file(record)
        return store
