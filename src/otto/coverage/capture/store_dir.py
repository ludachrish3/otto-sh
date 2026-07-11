"""The committed in-repo manual-capture store (spec §3)."""

import re
from pathlib import Path

from .model import Capture

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-") or "x"


def manual_store_dir(repo_root: Path) -> Path:
    """Directory storing committed manual capture records."""
    return repo_root / ".otto" / "coverage" / "manual"


def write_manual_capture(capture: Capture, repo_root: Path) -> Path:
    """Write a capture to the manual store and return its path."""
    captured_at_slug = capture.captured_at.replace("-", "").replace(":", "")
    name = f"{captured_at_slug}-{_slug(capture.ticket or 'no-ticket')}-{_slug(capture.board)}.json"
    path = manual_store_dir(repo_root) / name
    capture.save(path)
    return path


def load_manual_captures(repo_root: Path) -> list[Capture]:
    """Load all manual captures from repo_root, sorted by filename."""
    d = manual_store_dir(repo_root)
    if not d.is_dir():
        return []
    captures: list[Capture] = []
    for p in sorted(d.glob("*.json")):
        try:
            captures.append(Capture.load(p))
        except ValueError as e:  # noqa: PERF203
            raise ValueError(f"malformed manual capture {p.name}: {e}") from e
    return captures
