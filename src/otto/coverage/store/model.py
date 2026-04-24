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
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Conventional tier name used by the merged .gcda pipeline.  Any string
# is a valid tier name — this constant just spares callers a string
# literal when they mean the canonical system-coverage tier.
TIER_SYSTEM = "system"


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
        """True if hit in *tier* (or in any tier when ``tier`` is None)."""
        if tier is None:
            return self.total() > 0
        return self.for_tier(tier) > 0

    def merge(self, other: LineHits) -> None:
        """Additive merge — sum counts for every tier present in *other*."""
        for tier, count in other.counts.items():
            self.add(tier, count)

    def to_dict(self) -> dict[str, int]:
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
        """True if this branch was taken at least once in *tier*."""
        if tier is None:
            return self.hits.is_hit()
        return self.hits.for_tier(tier) > 0

    def merge(self, other: BranchHits) -> None:
        assert self.block == other.block and self.branch == other.branch
        self.hits.merge(other.hits)
        for tier, reachable in other.reachable.items():
            self.set_reachable(tier, reachable)

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "branch": self.branch,
            "hits": self.hits.to_dict(),
            "reachable": dict(self.reachable),
        }


@dataclass
class LineRecord:
    """Coverage data for a single source line."""

    line_number: int
    hits: LineHits = field(default_factory=LineHits)
    branches: list[BranchHits] = field(default_factory=list)

    # Populated by the blame correlator (not rendered by the HTML report).
    commit_hash: str | None = None
    commit_author: str | None = None
    commit_summary: str | None = None

    def merge(self, other: LineRecord) -> None:
        assert self.line_number == other.line_number
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


@dataclass
class FileRecord:
    """Coverage data for a single source file."""

    path: Path
    lines: dict[int, LineRecord] = field(default_factory=dict)

    def get_or_create_line(self, line_number: int) -> LineRecord:
        if line_number not in self.lines:
            self.lines[line_number] = LineRecord(line_number=line_number)
        return self.lines[line_number]

    def merge(self, other: FileRecord) -> None:
        assert self.path == other.path
        for lineno, other_line in other.lines.items():
            if lineno in self.lines:
                self.lines[lineno].merge(other_line)
            else:
                clone = LineRecord(
                    line_number=lineno,
                    hits=LineHits(counts=dict(other_line.hits.counts)),
                )
                clone.merge(other_line)  # picks up branches cleanly
                self.lines[lineno] = clone

    def _all_branches(self) -> list[BranchHits]:
        return [b for lr in self.lines.values() for b in lr.branches]

    def line_coverage_pct(self, tier: str | None = None) -> float:
        """Line coverage percentage, optionally filtered by tier."""
        if not self.lines:
            return 0.0
        hit = sum(1 for l in self.lines.values() if l.hits.is_hit(tier))
        return (hit / len(self.lines)) * 100

    def branch_coverage_pct(
        self, tier: str | None = None, conservative: bool = True
    ) -> float:
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
        yield from (self.lines[k] for k in sorted(self.lines))

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "lines": {
                str(lineno): {
                    "hits": rec.hits.to_dict(),
                    "branches": [b.to_dict() for b in rec.branches],
                    "commit": rec.commit_hash,
                    "author": rec.commit_author,
                    "summary": rec.commit_summary,
                }
                for lineno, rec in self.lines.items()
            },
        }


class CoverageStore:
    """Central store for all coverage data across all files, hosts, and tiers.

    ``tier_order`` holds the user-defined precedence of tiers — first
    entry is highest precedence.  Downstream renderers iterate this list
    to drive column ordering and the winner-take-all row coloring used
    by the annotated source view.
    """

    def __init__(self, tier_order: list[str] | None = None) -> None:
        self._files: dict[Path, FileRecord] = {}
        self.tier_order: list[str] = list(tier_order) if tier_order else []

    def register_tier(self, tier: str) -> None:
        """Append *tier* to the tier order if not already present.

        Loaders call this when they see a new tier so the store's
        ``tier_order`` list stays in sync with the data without requiring
        every call site to pre-declare every tier.
        """
        if tier not in self.tier_order:
            self.tier_order.append(tier)

    def get_or_create_file(self, path: Path) -> FileRecord:
        canonical = path.resolve() if path.exists() else path
        if canonical not in self._files:
            self._files[canonical] = FileRecord(path=canonical)
        return self._files[canonical]

    def merge_file(self, record: FileRecord) -> None:
        if record.path in self._files:
            self._files[record.path].merge(record)
        else:
            self._files[record.path] = record

    def files(self) -> Iterator[FileRecord]:
        yield from sorted(self._files.values(), key=lambda f: f.path)

    def file_count(self) -> int:
        return len(self._files)

    def _all_branches(self) -> list[BranchHits]:
        return [b for f in self._files.values() for lr in f.lines.values() for b in lr.branches]

    def overall_pct(self, tier: str | None = None) -> float:
        """Overall line coverage percentage across all files."""
        all_lines = sum(len(f.lines) for f in self._files.values())
        if all_lines == 0:
            return 0.0
        hit = sum(
            sum(1 for l in f.lines.values() if l.hits.is_hit(tier))
            for f in self._files.values()
        )
        return (hit / all_lines) * 100

    def overall_branch_pct(
        self, tier: str | None = None, conservative: bool = True
    ) -> float:
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
            "tier_order": list(self.tier_order),
            "files": [f.to_dict() for f in self.files()],
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> CoverageStore:
        """Deserialise a store from JSON."""
        data = json.loads(path.read_text())
        # Tolerate both the new envelope shape and the legacy list-only
        # shape (older test fixtures).
        if isinstance(data, dict):
            tier_order = data.get("tier_order") or []
            files_data = data.get("files", [])
        else:
            tier_order = []
            files_data = data

        store = cls(tier_order=tier_order)
        for fd in files_data:
            record = FileRecord(path=Path(fd["path"]))
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
                    commit_hash=ld.get("commit"),
                    commit_author=ld.get("author"),
                    commit_summary=ld.get("summary"),
                )
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
