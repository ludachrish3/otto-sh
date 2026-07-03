"""Report-time validity for pinned manual captures (spec §7).

Anchor chain per file: blob fast-path → blob diff → pin diff →
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
from .store.model import BranchHits, CoverageStore

logger = logging.getLogger(__name__)


def _anchor_diff(fc: CaptureFileCov, repo_root: Path, relpath: Path, pin: str) -> str | None:
    """-U0 diff pin→current for one file; '' = unchanged; None = unverifiable."""
    current = repo_root / relpath
    if not current.is_file():
        return None
    if fc.blob and gitio.hash_object(repo_root, current) == fc.blob:
        return ""
    base_blob = fc.blob if fc.blob and gitio.blob_exists(repo_root, fc.blob) else None
    if base_blob is None:
        base_blob = gitio.blob_sha(repo_root, relpath, rev=pin)
    if base_blob is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=relpath.suffix) as tmp:
        Path(tmp.name).write_bytes(gitio.cat_blob(repo_root, base_blob))
        return gitio.diff_no_index_u0(Path(tmp.name), current)


def _is_aging(captured_at: str, max_age_days: int | None, today: datetime | None) -> bool:
    if max_age_days is None:
        return False
    captured = datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = today or datetime.now(timezone.utc)
    return (now - captured).days > max_age_days


def apply_manual_capture(
    store: CoverageStore,
    capture: Capture,
    repo_root: Path,
    max_age_days: int | None,
    today: datetime | None = None,
) -> None:
    """Fold one manual capture into *store* with validity states."""
    store.register_tier(capture.tier)
    aging = _is_aging(capture.captured_at, max_age_days, today)

    for rel_str, fc in capture.files.items():
        relpath = Path(rel_str)
        diff = _anchor_diff(fc, repo_root, relpath, capture.pin)
        file_rec = store.get_or_create_file(repo_root / relpath)
        if diff is None:
            logger.warning(
                "Manual capture %s/%s is unverifiable (pin %s and blob missing) — "
                "treating as stale; re-capture to refresh.",
                capture.ticket,
                rel_str,
                capture.pin[:12],
            )
            for lineno in fc.lines:
                lr = file_rec.get_or_create_line(lineno)
                if lr.state is None and not lr.hits.is_hit():
                    lr.state = "stale"
            continue

        remapper = LineRemapper(parse_u0_hunks(diff))
        for lineno, count in fc.lines.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is None:
                # Changed/deleted since the pin: no current position to
                # credit, so mark stale at the pin's own line number
                # (a nearby-enough anchor for a human to find) as long as
                # no tier has already put a hit there.
                if count > 0:
                    lr = file_rec.get_or_create_line(lineno)
                    if not lr.hits.is_hit():
                        lr.state = "stale"
                continue
            lr = file_rec.get_or_create_line(new_line)
            lr.hits.add(capture.tier, count)
            if aging and count > 0 and lr.state is None:
                lr.state = "aging"

        for lineno, triples in fc.branches.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is None:
                continue
            lr = file_rec.get_or_create_line(new_line)
            existing = {(b.block, b.branch): b for b in lr.branches}
            for block, branch, taken in triples:
                key = (block, branch)
                if key not in existing:
                    bh = BranchHits(block=block, branch=branch)
                    lr.branches.append(bh)
                    existing[key] = bh
                existing[key].set_reachable(capture.tier, True)
                if taken > 0:
                    existing[key].hits.add(capture.tier, taken)

    store.provenance.append(
        {
            "tier": capture.tier,
            "board": capture.board,
            "labs": capture.labs,
            "date": capture.captured_at,
            "tester": capture.tester,
            "ticket": capture.ticket,
            "note": capture.note,
            "dirty_remap": capture.dirty_remap,
            "pin": capture.pin,
        }
    )
