"""Report-time validity for manual captures anchored to ``base_commit`` (spec §7).

Anchor chain per file: blob fast-path → blob diff → base_commit diff →
unverifiable (whole file stale, loud warning).  Valid lines are loaded
into the store under the capture's tier; stale lines are marked but
carry no hits; aging marks valid-but-old manual evidence.
"""

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .capture import gitio
from .capture.model import Capture, CaptureFileCov
from .capture.remap import LineRemapper, parse_u0_hunks
from .store.model import BranchHits, CoverageStore, FileRecord, LineRecord

logger = logging.getLogger(__name__)


def register_capture_run(store: CoverageStore, capture: Capture) -> int:
    """Register one capture as a run; returns its run id.

    The chip label prefers the host display name, then the board (host
    id) — ``add_run`` itself falls back to the tier name, which only
    synthetic runs use.
    """
    return store.add_run(
        tier=capture.tier,
        label=capture.display_name or capture.board or None,
        board=capture.board,
        labs=capture.labs,
        captured_at=capture.captured_at,
        tester=capture.tester,
        ticket=capture.ticket,
        note=capture.note,
        base_commit=capture.base_commit,
        dirty_remap=capture.dirty_remap,
    )


def _insert_branch_triples(
    line_rec: LineRecord, tier: str, triples: list[tuple[int, int, int | None]]
) -> None:
    """Merge lcov branch triples into one line under *tier* (mirrors LCOVLoader).

    ``taken`` is ``None`` for a never-reached branch, ``0`` for a reached
    but not-taken branch, and a positive count otherwise.
    """
    existing = {(b.block, b.branch): b for b in line_rec.branches}
    for block, branch, taken in triples:
        key = (block, branch)
        if key not in existing:
            bh = BranchHits(block=block, branch=branch)
            line_rec.branches.append(bh)
            existing[key] = bh
        reachable = taken is not None
        existing[key].set_reachable(tier, reachable)
        if reachable and taken > 0:
            existing[key].hits.add(tier, taken)


def _record_stale_run_for_line(line_rec: LineRecord, run_id: int | None) -> None:
    """Record a revoked run on a stale line."""
    if run_id is not None and run_id not in line_rec.stale_runs:
        line_rec.stale_runs.append(run_id)


def _insert_lines(
    file_rec: FileRecord,
    tier: str,
    lines: dict[int, int],
    branches: dict[int, list[tuple[int, int, int | None]]],
    run_id: int | None = None,
) -> None:
    """Fold one file's line hits and branch triples into *file_rec* under *tier*.

    Coordinates are taken verbatim — callers pass current-worktree line
    numbers (either because the capture's ``base_commit`` is HEAD, or
    after the manual anchor-chain remap). No validity states are set
    here; that is the manual-capture pass's job. This is the single
    insertion path shared by :func:`load_capture_into_store` and
    :func:`apply_manual_capture`.

    Only executed lines (count > 0) credit the run — a run that
    instrumented but never hit a line is not listed for it.
    """
    for lineno, count in lines.items():
        line_rec = file_rec.get_or_create_line(lineno)
        line_rec.hits.add(tier, count)
        if run_id is not None and count > 0:
            line_rec.run_hits[run_id] = line_rec.run_hits.get(run_id, 0) + count
    for lineno, triples in branches.items():
        _insert_branch_triples(file_rec.get_or_create_line(lineno), tier, triples)


def load_capture_into_store(
    store: CoverageStore, capture: Capture, repo_root: Path, run_id: int | None = None
) -> None:
    """Fold a base_commit==HEAD (e2e-kind) capture into *store* verbatim.

    The caller has already verified ``capture.base_commit`` equals the
    tree's HEAD, so the capture's coordinates *are* the current-worktree
    coordinates and no anchor chain / remap is needed. Unlike
    :func:`apply_manual_capture` this sets no validity states — an automated
    capture carries no human session to attribute.
    """
    store.register_tier(capture.tier)
    for rel_str, fc in capture.files.items():
        file_rec = store.get_or_create_file(repo_root / Path(rel_str))
        _insert_lines(file_rec, capture.tier, fc.lines, fc.branches, run_id=run_id)


def load_dirty_capture_into_store(
    store: CoverageStore, capture: Capture, repo_root: Path, run_id: int | None = None
) -> None:
    """Fold a base_commit==HEAD e2e capture in, remapping HEAD → dirty working tree.

    An e2e capture carries no anchor chain: its coordinates are the exact
    ``base_commit``'s line numbers. The caller has verified
    ``capture.base_commit`` equals HEAD, but when the working tree is
    *dirty* the renderer reads the edited on-disk source, so a verbatim
    insert (:func:`load_capture_into_store`) would misalign every hit
    past a local edit. Remap each file's line/branch
    numbers from HEAD (OLD) to the working tree (NEW) using the same ``-U0``
    worktree diff + ``LineRemapper`` the manual anchor chain uses; hits
    on locally-modified lines have no NEW counterpart and are dropped.
    """
    store.register_tier(capture.tier)
    for rel_str, fc in capture.files.items():
        relpath = Path(rel_str)
        remapper = LineRemapper(parse_u0_hunks(gitio.diff_worktree_file_u0(repo_root, relpath)))

        mapped_lines: dict[int, int] = {}
        for lineno, count in fc.lines.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is not None:
                mapped_lines[new_line] = mapped_lines.get(new_line, 0) + count

        mapped_branches: dict[int, list[tuple[int, int, int | None]]] = {}
        for lineno, triples in fc.branches.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is not None:
                mapped_branches.setdefault(new_line, []).extend(triples)

        file_rec = store.get_or_create_file(repo_root / relpath)
        _insert_lines(file_rec, capture.tier, mapped_lines, mapped_branches, run_id=run_id)


def _anchor_diff(
    fc: CaptureFileCov, repo_root: Path, relpath: Path, base_commit: str
) -> str | None:
    """-U0 diff base_commit→current for one file; '' = unchanged; None = unverifiable."""
    current = repo_root / relpath
    if not current.is_file():
        return None
    if fc.blob and gitio.hash_object(repo_root, current) == fc.blob:
        return ""
    base_blob = fc.blob if fc.blob and gitio.blob_exists(repo_root, fc.blob) else None
    if base_blob is None:
        base_blob = gitio.blob_sha(repo_root, relpath, rev=base_commit)
    if base_blob is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=relpath.suffix) as tmp:
        Path(tmp.name).write_bytes(gitio.cat_blob(repo_root, base_blob))
        return gitio.diff_no_index_u0(Path(tmp.name), current)


def _is_aging(capture: Capture, max_age_days: int | None, today: datetime | None) -> bool:
    if max_age_days is None:
        return False
    try:
        captured = datetime.strptime(capture.captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        logger.warning(
            "Manual capture %s (board %s) has a blank/unparseable captured_at %r; "
            "treating as not aging.",
            capture.ticket,
            capture.board,
            capture.captured_at,
        )
        return False
    now = today or datetime.now(timezone.utc)
    return (now - captured).days > max_age_days


def _apply_unverifiable_capture(
    file_rec: FileRecord,
    capture: Capture,
    rel_str: str,
    fc: CaptureFileCov,
    run_id: int | None,
) -> None:
    """Mark one unverifiable file's executed lines stale (base_commit+blob both gone).

    Never-executed (count 0) lines are skipped — mirrors the mapped path's
    ``stale_linenos`` count>0 gate below, so a run is never listed STALE on
    a line it never actually ran.
    """
    logger.warning(
        "Manual capture %s/%s is unverifiable (base_commit %s and blob missing) — "
        "treating as stale; re-capture to refresh.",
        capture.ticket,
        rel_str,
        capture.base_commit[:12],
    )
    for lineno, count in fc.lines.items():
        if count == 0:
            continue
        lr = file_rec.get_or_create_line(lineno)
        if lr.state is None and not lr.hits.is_hit():
            lr.state = "stale"
        _record_stale_run_for_line(lr, run_id)


def apply_manual_capture(
    store: CoverageStore,
    capture: Capture,
    repo_root: Path,
    max_age_days: int | None,
    today: datetime | None = None,
    run_id: int | None = None,
) -> None:
    """Fold one manual capture into *store* with validity states."""
    store.register_tier(capture.tier)
    aging = _is_aging(capture, max_age_days, today)
    if aging and run_id is not None:
        store.runs[run_id].aging = True

    for rel_str, fc in capture.files.items():
        relpath = Path(rel_str)
        diff = _anchor_diff(fc, repo_root, relpath, capture.base_commit)
        file_rec = store.get_or_create_file(repo_root / relpath)
        if diff is None:
            _apply_unverifiable_capture(file_rec, capture, rel_str, fc, run_id)
            continue

        remapper = LineRemapper(parse_u0_hunks(diff))

        # Remap base_commit (OLD) coordinates → current-worktree (NEW)
        # coordinates. Lines with no new position (changed/deleted since
        # base_commit) are recorded for stale marking at their own
        # base_commit line number — a nearby-enough anchor for a human to
        # find.
        mapped_lines: dict[int, int] = {}
        stale_linenos: list[int] = []
        for lineno, count in fc.lines.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is None:
                if count > 0:
                    stale_linenos.append(lineno)
                continue
            mapped_lines[new_line] = mapped_lines.get(new_line, 0) + count

        mapped_branches: dict[int, list[tuple[int, int, int | None]]] = {}
        for lineno, triples in fc.branches.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is not None:
                mapped_branches.setdefault(new_line, []).extend(triples)

        _insert_lines(file_rec, capture.tier, mapped_lines, mapped_branches, run_id=run_id)

        # Covered wins over a stale marker from an earlier capture: when this
        # (later) capture validly credits a line that a previous capture left
        # flagged "stale", clear the stale state so the freshly-covered line
        # no longer reads as unverifiable.
        for new_line, count in mapped_lines.items():
            if count > 0:
                lr = file_rec.lines.get(new_line)
                if lr is not None and lr.state == "stale":
                    lr.state = None

        if aging:
            for new_line, count in mapped_lines.items():
                if count > 0:
                    lr = file_rec.get_or_create_line(new_line)
                    if lr.state is None:
                        lr.state = "aging"

        for lineno in stale_linenos:
            lr = file_rec.get_or_create_line(lineno)
            if not lr.hits.is_hit():
                lr.state = "stale"
            _record_stale_run_for_line(lr, run_id)
