"""The per-board ``capture.json`` artifact (spec §3).

A capture stores line/branch data in **committed-code coordinates**,
pinned to the commit whose numbering they mean, with per-file blob SHAs
as the rebase-tolerant validity anchor.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import gitio
from .remap import LineRemapper, parse_u0_hunks

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class CaptureFileCov(BaseModel):
    """Coverage for one source file, keyed in pin coordinates."""

    model_config = ConfigDict(extra="forbid")

    blob: str | None = None
    lines: dict[int, int] = Field(default_factory=dict)
    branches: dict[int, list[tuple[int, int, int]]] = Field(default_factory=dict)


class Capture(BaseModel):
    """One board's retrieval result — the universal capture artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    tier: str
    pin: str
    dirty_remap: bool = False
    captured_at: str = ""
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    labs: list[str] = Field(default_factory=list)
    board: str = ""
    files: dict[str, CaptureFileCov] = Field(default_factory=dict)

    def save(self, path: Path) -> None:
        """Serialize this capture to *path* as indented JSON (by alias)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(by_alias=True, indent=2))

    @classmethod
    def load(cls, path: Path) -> "Capture":
        """Deserialize a :class:`Capture` from *path* (unknown keys rejected)."""
        return cls.model_validate_json(path.read_text())


def parse_info(
    info_path: Path,
) -> dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]]:
    """Minimal SF/DA/BRDA parser: source path → (line hits, branch triples)."""
    files: dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]] = {}
    lines: dict[int, int] = {}
    branches: dict[int, list[tuple[int, int, int]]] = {}
    current: str | None = None
    with info_path.open() as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("SF:"):
                current = line[3:]
                lines, branches = {}, {}
            elif line.startswith("DA:") and current is not None:
                parts = line[3:].split(",")
                lines[int(parts[0])] = lines.get(int(parts[0]), 0) + int(parts[1])
            elif line.startswith("BRDA:") and current is not None:
                lineno_s, block_s, branch_s, taken = line[5:].split(",")
                count = 0 if taken == "-" else int(taken)
                branches.setdefault(int(lineno_s), []).append((int(block_s), int(branch_s), count))
            elif line == "end_of_record" and current is not None:
                files[current] = (lines, branches)
                current = None
    return files


def _remap_file(
    lines: dict[int, int],
    branches: dict[int, list[tuple[int, int, int]]],
    remapper: LineRemapper,
) -> tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]:
    """Worktree (NEW) coordinates → pin (OLD) coordinates; drop unmappables."""
    out_lines: dict[int, int] = {}
    for lineno, count in lines.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_lines[old] = out_lines.get(old, 0) + count
    out_branches: dict[int, list[tuple[int, int, int]]] = {}
    for lineno, triples in branches.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_branches[old] = triples
    return out_lines, out_branches


def build_capture(
    *,
    info_path: Path,
    tier: str,
    repo_root: Path,
    board: str,
    labs: list[str],
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Capture:
    """Build a pinned :class:`Capture` from an lcov ``.info`` file."""
    pin = gitio.head_commit(repo_root)
    dirty = gitio.is_dirty(repo_root)
    repo_root = repo_root.resolve()

    files: dict[str, CaptureFileCov] = {}
    for src, (raw_lines, raw_branches) in parse_info(info_path).items():
        src_path = Path(src).resolve()
        if not src_path.is_relative_to(repo_root):
            logger.warning("Skipping source outside repo: %s", src)
            continue
        rel = src_path.relative_to(repo_root)
        if dirty:
            hunks = parse_u0_hunks(gitio.diff_worktree_file_u0(repo_root, rel))
            lines, branches = _remap_file(raw_lines, raw_branches, LineRemapper(hunks))
        else:
            lines, branches = raw_lines, raw_branches
        files[rel.as_posix()] = CaptureFileCov(
            blob=gitio.blob_sha(repo_root, rel),
            lines=lines,
            branches=branches,
        )

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Capture(
        tier=tier,
        pin=pin,
        dirty_remap=dirty,
        captured_at=stamp,
        tester=tester,
        ticket=ticket,
        note=note,
        labs=labs,
        board=board,
        files=files,
    )
