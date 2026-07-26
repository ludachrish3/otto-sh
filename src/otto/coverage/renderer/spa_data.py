"""Emit the JS data chunks the coverage SPA (Plan C) consumes.

This is the pure-Python half of the SPA report: it turns a
:class:`~otto.coverage.store.model.CoverageStore` into

- ``cov_data/index.js`` — one classic-script assignment
  (``window.__OTTO_COV__ = {...};``) carrying the report-wide
  :data:`IndexPayload`-shaped dict (config, run table, and a directory
  tree of rollup :data:`Stats`), and
- ``cov_data/files/<mangled>.js`` — one classic-script call
  (``window.__OTTO_COV_FILE__({...});``) per file, carrying its
  annotated source and per-line hit/branch/state data.

It deliberately mirrors the mechanics of
:class:`~otto.coverage.renderer.html_renderer.HtmlRenderer` (display-path
prefix stripping, path mangling, tier labels/colors, the source-exclusion
scan) without importing from it — that module is retired in Plan D.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...version import get_version
from ..colors import DEFAULT_TIER_COLORS, STATE_COLORS
from ..exclusions import scan_excluded_lines
from ..store.model import STAT_TYPES, CoverageStore, FileRecord, LineRecord, RunRecord

logger = logging.getLogger(__name__)

OTTO_COV_DATA_FORMAT: int = 1
"""``IndexPayload["format"]`` / ``FileChunk["stamp"]``-adjacent format marker.

Bump alongside the TypeScript ``EXPECTED_DATA_FORMAT`` constant, or never."""

# Pretty labels for the conventional tier names. Copied from
# HtmlRenderer (not imported — that module dies in Plan D). Tiers without
# an entry here render with their raw name title-cased.
TIER_LABELS: dict[str, str] = {
    "system": "System",
    "unit": "Unit",
    "manual": "Manual",
}


def _label_for(tier: str) -> str:
    return TIER_LABELS.get(tier, tier.replace("_", " ").title())


def make_stamp() -> str:
    """Return a fresh report stamp: UTC timestamp + a short random suffix.

    Written once per :func:`emit_chunks` call and carried verbatim on the
    index payload and every file chunk, so the frontend can detect a
    stale/partial chunk cache against a freshly generated index.
    """
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


def mangle_path(path: Path) -> str:
    """Mangle a canonical file path into a filesystem/URL-safe chunk id.

    Same scheme as ``HtmlRenderer._file_link``: full canonical path (never
    the display path), so chunk filenames stay stable across ``--prefix``
    changes.
    """
    return str(path).replace("/", "_").replace("\\", "_").lstrip("_")


def _display_path(record: FileRecord, prefix: Path | None) -> str:
    """Display-only path: strip *prefix* when it matches, else the full path."""
    if prefix is not None:
        try:
            return str(record.path.relative_to(prefix))
        except ValueError:
            return str(record.path)
    return str(record.path)


def _resolve_tier_colors(store: CoverageStore, tier_order: list[str]) -> dict[str, str]:
    """Per-tier colors: ``store.tier_colors`` first, else a name-keyed default.

    Same fallback quirk as ``HtmlRenderer._resolve_tier_colors``.
    """
    return {t: store.tier_colors.get(t) or DEFAULT_TIER_COLORS.get(t, "green") for t in tier_order}


def _line_to_json(lr: LineRecord) -> dict[str, Any]:
    """One line's JSON shape, matching ``FileRecord.to_dict()``'s per-line shape exactly."""
    d: dict[str, Any] = {
        "hits": lr.hits.to_dict(),
        "branches": [b.to_dict() for b in lr.branches],
        "state": lr.state,
    }
    if lr.run_hits:
        d["run"] = {str(rid): n for rid, n in lr.run_hits.items()}
    if lr.stale_runs:
        d["stale_run"] = list(lr.stale_runs)
    if lr.ticket is not None:
        d["ticket"] = lr.ticket
    return d


# ----------------------------------------------------------------------
# Stats aggregation (rolled up bottom-up into the directory tree)
# ----------------------------------------------------------------------


def _empty_stats(tier_order: list[str]) -> dict[str, Any]:
    return {
        "lines": {"total": 0, "hit": 0, "per_tier": dict.fromkeys(tier_order, 0)},
        "branches": {"total": 0, "hit": 0, "per_tier": dict.fromkeys(tier_order, 0)},
        "flags": {"stale": 0, "aging": 0, "excluded": 0},
        "ctx_lines": {},
    }


def _add_stats(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Accumulate *source* into *target* in place."""
    target["lines"]["total"] += source["lines"]["total"]
    target["lines"]["hit"] += source["lines"]["hit"]
    for tier, n in source["lines"]["per_tier"].items():
        target["lines"]["per_tier"][tier] = target["lines"]["per_tier"].get(tier, 0) + n
    target["branches"]["total"] += source["branches"]["total"]
    target["branches"]["hit"] += source["branches"]["hit"]
    for tier, n in source["branches"]["per_tier"].items():
        target["branches"]["per_tier"][tier] = target["branches"]["per_tier"].get(tier, 0) + n
    target["flags"]["stale"] += source["flags"]["stale"]
    target["flags"]["aging"] += source["flags"]["aging"]
    target["flags"]["excluded"] += source["flags"]["excluded"]
    for label, n in source["ctx_lines"].items():
        target["ctx_lines"][label] = target["ctx_lines"].get(label, 0) + n


def _file_stats(
    fr: FileRecord, tier_order: list[str], runs_by_id: dict[int, RunRecord]
) -> dict[str, Any]:
    """Aggregate one file's :data:`Stats`.

    Iterates every :class:`LineRecord` the file has, including ones past
    the current source's EOF (shrunk-file tolerance) — they still count
    here even though they have no source line to annotate.
    """
    stats = _empty_stats(tier_order)
    lines = list(fr.lines.values())

    stats["lines"]["total"] = len(lines)
    stats["lines"]["hit"] = sum(1 for lr in lines if lr.hits.is_hit())
    for tier in tier_order:
        stats["lines"]["per_tier"][tier] = sum(1 for lr in lines if lr.hits.for_tier(tier) > 0)

    all_branches = [b for lr in lines for b in lr.branches]
    stats["branches"]["total"] = len(all_branches)
    stats["branches"]["hit"] = sum(1 for b in all_branches if b.hits.total() > 0)
    for tier in tier_order:
        stats["branches"]["per_tier"][tier] = sum(
            1 for b in all_branches if b.hits.for_tier(tier) > 0
        )

    stats["flags"]["stale"] = sum(1 for lr in lines if lr.state == "stale")
    stats["flags"]["aging"] = sum(1 for lr in lines if lr.state == "aging")
    stats["flags"]["excluded"] = len(fr.excluded_lines)

    ctx: dict[str, int] = {}
    for lr in lines:
        labels: set[str] = set()
        for run_id, count in lr.run_hits.items():
            if count > 0:
                run = runs_by_id.get(run_id)
                if run is not None:
                    labels.add(run.label)
        for label in labels:
            ctx[label] = ctx.get(label, 0) + 1
    stats["ctx_lines"] = ctx

    return stats


# ----------------------------------------------------------------------
# Directory tree
# ----------------------------------------------------------------------


def _new_dir(name: str) -> dict[str, Any]:
    return {"name": name, "dirs": {}, "files": {}, "stats": None}


def _tree_parts(display_path: str) -> tuple[str, ...]:
    r"""Split a display path into tree-grouping parts.

    Drops a leading path anchor (``/`` or ``\\``) so the fallback
    (unstripped absolute path, when ``prefix`` doesn't match) still
    builds a sane nested tree instead of a bogus ``"/"``-named root
    child.
    """
    parts = Path(display_path).parts
    if parts and parts[0] in ("/", "\\"):
        parts = parts[1:]
    return parts


def _insert_file(
    root: dict[str, Any],
    parts: tuple[str, ...],
    file_node: dict[str, Any],
    stats: dict[str, Any],
    tier_order: list[str],
) -> None:
    node = root
    _add_stats(node["stats"], stats)
    for part in parts[:-1]:
        if part not in node["dirs"]:
            node["dirs"][part] = _new_dir(part)
            node["dirs"][part]["stats"] = _empty_stats(tier_order)
        node = node["dirs"][part]
        _add_stats(node["stats"], stats)
    node["files"][parts[-1]] = file_node


def _finalize(node: dict[str, Any]) -> dict[str, Any]:
    """Recursively sort a dir node's children by name (deterministic output)."""
    return {
        "name": node["name"],
        "dirs": [_finalize(node["dirs"][k]) for k in sorted(node["dirs"])],
        "files": [node["files"][k] for k in sorted(node["files"])],
        "stats": node["stats"],
    }


# ----------------------------------------------------------------------
# run_contrib
# ----------------------------------------------------------------------


def _build_run_contrib(store: CoverageStore, prefix: Path | None) -> dict[str, Any]:
    contrib: dict[int, dict[str, Any]] = {
        r.id: {"lines": 0, "revoked": 0, "files": {}} for r in store.runs
    }
    for fr in store.files():
        display_path = _display_path(fr, prefix)
        for lr in fr.lines.values():
            for run_id, count in lr.run_hits.items():
                if count > 0 and run_id in contrib:
                    bucket = contrib[run_id]
                    bucket["lines"] += 1
                    bucket["files"][display_path] = bucket["files"].get(display_path, 0) + 1
            for run_id in lr.stale_runs:
                if run_id in contrib:
                    contrib[run_id]["revoked"] += 1

    result: dict[str, Any] = {}
    for run_id, bucket in contrib.items():
        files_sorted = sorted(bucket["files"].items(), key=lambda kv: (-kv[1], kv[0]))
        result[str(run_id)] = {
            "lines": bucket["lines"],
            "revoked": bucket["revoked"],
            "files": [[path, n] for path, n in files_sorted],
        }
    return result


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def build_index_payload(
    store: CoverageStore,
    *,
    project_name: str,
    prefix: Path | None,
    stamp: str,
) -> dict[str, Any]:
    """Build the ``IndexPayload`` dict written to ``cov_data/index.js``.

    Reads whatever exclusion data is already on each ``FileRecord`` —
    it does not scan source itself; :func:`emit_chunks` runs that scan
    (mirroring ``HtmlRenderer``) and annotates the store before this
    function's per-file stats are computed.
    """
    tier_order = list(store.tier_order)
    tier_colors = _resolve_tier_colors(store, tier_order)
    tier_labels = {t: _label_for(t) for t in tier_order}
    runs_by_id = {r.id: r for r in store.runs}

    root = _new_dir(project_name)
    root["stats"] = _empty_stats(tier_order)
    for fr in store.files():
        display_path = _display_path(fr, prefix)
        stats = _file_stats(fr, tier_order, runs_by_id)
        file_node = {
            "name": Path(display_path).name,
            "path": display_path,
            "chunk": mangle_path(fr.path),
            "stats": stats,
        }
        parts = _tree_parts(display_path)
        _insert_file(root, parts, file_node, stats, tier_order)

    tree = _finalize(root)

    return {
        "format": OTTO_COV_DATA_FORMAT,
        "stamp": stamp,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "otto_version": get_version(),
        "project_name": project_name,
        "tier_order": tier_order,
        "tier_labels": tier_labels,
        "tier_colors": tier_colors,
        "state_colors": dict(STATE_COLORS),
        "thresholds": store.thresholds.to_dict(),
        "stat_types": list(STAT_TYPES),
        "runs": [r.to_dict() for r in store.runs],
        "run_contrib": _build_run_contrib(store, prefix),
        "total_lines": tree["stats"]["lines"]["total"],
        "tree": tree,
    }


def _build_file_chunk(
    fr: FileRecord,
    prefix: Path | None,
    extra_markers: list[str] | None,
    stamp: str,
) -> dict[str, Any]:
    """Build one ``FileChunk`` dict, annotating ``fr.excluded_lines`` as a side effect.

    Mirrors ``HtmlRenderer._render_file``'s source read + exclusion scan.
    """
    try:
        source_text = fr.path.read_text(errors="replace")
    except OSError as e:
        logger.warning(
            "Could not read source %s (%s); its chunk will have an empty source.",
            fr.path,
            e,
        )
        source_text = ""

    excluded_linenos = scan_excluded_lines(source_text, extra_markers or None)
    # Annotate the store (spec §9 frontend contract): the reporter renders
    # before it saves store.json, so this flows through to the serialised
    # store for frontend consumers, exactly like HtmlRenderer does today.
    fr.excluded_lines = excluded_linenos

    lines_json = {str(lineno): _line_to_json(lr) for lineno, lr in fr.lines.items()}

    return {
        "stamp": stamp,
        "chunk": mangle_path(fr.path),
        "path": _display_path(fr, prefix),
        "source": source_text,
        "lines": lines_json,
        "excluded": sorted(excluded_linenos),
    }


def emit_chunks(
    store: CoverageStore,
    output_dir: Path,
    *,
    project_name: str,
    prefix: Path | None,
    extra_markers: list[str] | None,
    stamp: str,
) -> None:
    """Write ``cov_data/index.js`` and one ``cov_data/files/<mangled>.js`` per file.

    Every file's source-exclusion scan runs first (annotating
    ``FileRecord.excluded_lines`` on the store), so the index payload's
    per-node ``flags.excluded`` counts reflect it — the reporter's later
    ``store.save()`` then persists the same annotation, exactly like
    ``HtmlRenderer`` does today.
    """
    cov_data_dir = output_dir / "cov_data"
    files_dir = cov_data_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    for fr in store.files():
        chunk = _build_file_chunk(fr, prefix, extra_markers, stamp)
        out = files_dir / f"{chunk['chunk']}.js"
        out.write_text(f"window.__OTTO_COV_FILE__({json.dumps(chunk)});\n")

    payload = build_index_payload(store, project_name=project_name, prefix=prefix, stamp=stamp)
    (cov_data_dir / "index.js").write_text(f"window.__OTTO_COV__ = {json.dumps(payload)};\n")
