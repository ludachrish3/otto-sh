"""The per-(host, product) ``capture.json`` artifact (spec §3).

A capture stores line/branch data in **committed-code coordinates**,
anchored to the ``base_commit`` whose numbering they mean, with
per-file blob SHAs as the rebase-tolerant validity anchor.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import gitio
from .remap import LineRemapper, parse_u0_hunks

logger = logging.getLogger(__name__)

CAPTURE_FORMAT_VERSION = 3
"""``capture.json`` schema version, bumped on breaking on-disk changes.

Version 3 adds the required ``product`` field: a capture is one
(host, product) pair's retrieval, staged under ``cov/<host>/<product>/``.
Version 2 renames the git-anchor field from ``pin`` to ``base_commit``
(JSON key and Python attribute alike) — clearer terminology for the
commit whose line numbering a capture's coordinates mean. There is no
migration shim: a file that does not declare this exact version fails
loud in :meth:`Capture.load` with a message telling the caller to
re-capture, rather than silently mis-reading the renamed key under its
old name (or, worse, tripping pydantic's ``extra="forbid"`` on the
now-unknown ``pin`` key with an unfriendly traceback).
"""


class CaptureFileCov(BaseModel):
    """Coverage for one source file, keyed in ``base_commit`` coordinates."""

    model_config = ConfigDict(extra="forbid")

    blob: str | None = None
    lines: dict[int, int] = Field(default_factory=dict)
    branches: dict[int, list[tuple[int, int, int | None]]] = Field(default_factory=dict)


class Capture(BaseModel):
    """One (board, product) retrieval result — the universal capture artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(default=CAPTURE_FORMAT_VERSION, alias="schema")
    tier: str
    product: str
    base_commit: str
    dirty_remap: bool = False
    captured_at: str = ""
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    labs: list[str] = Field(default_factory=list)
    board: str = ""
    display_name: str | None = None
    files: dict[str, CaptureFileCov] = Field(default_factory=dict)

    def save(self, path: Path) -> None:
        """Serialize this capture to *path* as indented JSON (by alias)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(by_alias=True, indent=2))

    @classmethod
    def load(cls, path: Path) -> "Capture":
        """Deserialize a :class:`Capture` from *path* (unknown keys rejected).

        Raises:
            ValueError: The file's ``"schema"`` key does not match
                :data:`CAPTURE_FORMAT_VERSION` (including files with no
                ``"schema"`` key at all — everything predating this
                capture-format version). There is no migration shim; the
                message tells the caller to re-capture.
        """
        raw_text = path.read_text()
        raw = json.loads(raw_text)
        found_format = raw.get("schema") if isinstance(raw, dict) else None
        if found_format != CAPTURE_FORMAT_VERSION:
            # Distinguish "no schema key at all" from "a schema key was
            # present but isn't the int we expect" (e.g. a hand-edited
            # capture.json with "schema": "2") — see the identical fix in
            # otto.coverage.store.model.CoverageStore.load.
            if found_format is None:
                found_label = "none"
            elif isinstance(found_format, int):
                found_label = f"v{found_format}"
            else:
                found_label = f"{found_format!r} (expected an int)"
            raise ValueError(
                f"capture format v{CAPTURE_FORMAT_VERSION} required; "
                f"found {found_label} — re-capture with otto cov get"
            )
        return cls.model_validate_json(raw_text)


def _parse_block_id(text: str) -> int | None:
    r"""Parse a ``BRDA:`` block field, or ``None`` for an exception-tagged branch.

    The field is ``[e]<digits>``. ``man geninfo`` places the tag before the
    *line number* (``BRDA:<exception tag><line number>,<block
    number>,...``), but that is wrong — lcov 2.0's own writer
    (``lcovutil.pm`` ~5093, ``printf(INFO_HANDLE "BRDA:%u,%s%u,%s,%s\n",
    $line, $br->is_exception() ? 'e' : '', $block_id, ...)``) and its own
    parser (``lcovutil.pm`` ~4725, ``/^BRDA:(\d+),(e?)(\d+),(.+)$/``) agree
    the tag sits between the line number and the block number instead.
    "'exception tag' is 'e' if this is a branch related to exception
    handling" (``man geninfo``). A leading ``e`` marks compiler-synthesised
    exception-handling machinery, not a branch the product's own source
    wrote (observed live: arm64 kernel headers' asm-goto alternatives,
    pulled into a kmod product's translation unit) — otto's store has no
    exception-branch concept, so the caller drops the whole record on
    ``None``. An untagged id is always plain decimal.
    Duplicated from ``otto.coverage.merge.lcov_loader._parse_block_id``
    rather than imported: this module's BRDA parser is deliberately minimal
    and independent of the report-merge pipeline (see the module docstring).
    """
    if text.startswith("e"):
        return None
    return int(text)


def parse_info(
    info_path: Path,
) -> dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int | None]]]]]:
    """Minimal SF/DA/BRDA parser: source path → (line hits, branch triples).

    Branch triples are ``(block, branch, taken)`` where ``taken`` is
    ``None`` for lcov's ``-`` (branch never reached) and an ``int`` count
    otherwise — preserving the never-reached/reached-but-not-taken
    distinction through to the capture file. An exception-tagged ``BRDA:``
    record (see ``_parse_block_id`` above) is dropped entirely: no triple is
    appended for it.
    """
    files: dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int | None]]]]] = {}
    lines: dict[int, int] = {}
    branches: dict[int, list[tuple[int, int, int | None]]] = {}
    current: str | None = None
    with info_path.open() as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("SF:"):
                current = line[3:]
                lines = {}
                branches = {}
            elif line.startswith("DA:") and current is not None:
                parts = line[3:].split(",")
                lines[int(parts[0])] = lines.get(int(parts[0]), 0) + int(parts[1])
            elif line.startswith("BRDA:") and current is not None:
                lineno_s, block_s, branch_s, taken = line[5:].split(",")
                block = _parse_block_id(block_s)
                if block is None:
                    continue  # exception-tagged: compiler machinery, not a product branch
                count = None if taken == "-" else int(taken)
                branches.setdefault(int(lineno_s), []).append((block, int(branch_s), count))
            elif line == "end_of_record" and current is not None:
                files[current] = (lines, branches)
                current = None
    return files


def _remap_file(
    lines: dict[int, int],
    branches: dict[int, list[tuple[int, int, int | None]]],
    remapper: LineRemapper,
) -> tuple[dict[int, int], dict[int, list[tuple[int, int, int | None]]]]:
    """Worktree (NEW) coordinates → base_commit (OLD) coordinates; drop unmappables."""
    out_lines: dict[int, int] = {}
    for lineno, count in lines.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_lines[old] = out_lines.get(old, 0) + count
    out_branches: dict[int, list[tuple[int, int, int | None]]] = {}
    for lineno, triples in branches.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_branches[old] = triples
    return out_lines, out_branches


def build_capture(  # noqa: PLR0913 — wide capture-construction API, one keyword per annotation
    *,
    info_path: Path,
    tier: str,
    repo_root: Path,
    board: str,
    product: str,
    labs: list[str],
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    display_name: str | None = None,
    now: datetime | None = None,
) -> Capture:
    """Build a :class:`Capture` anchored to ``base_commit`` from an lcov ``.info`` file.

    Args:
        board: The staging-dir/host-id name the counters came from.
        product: The product whose counters these are — the ``<product>``
            segment of ``cov/<host>/<product>/``.
        display_name: Host display name to annotate onto the capture;
            ``board`` stays the staging-dir/host-id name.
    """
    base_commit = gitio.head_commit(repo_root)
    dirty = gitio.is_dirty(repo_root)
    repo_root = repo_root.resolve()

    files: dict[str, CaptureFileCov] = {}
    for src, (raw_lines, raw_branches) in parse_info(info_path).items():
        src_path = Path(src).resolve()
        if not src_path.is_relative_to(repo_root):
            logger.warning("Skipping source outside repo: %s", src)
            continue
        rel = src_path.relative_to(repo_root)
        blob = gitio.blob_sha(repo_root, rel)
        if blob is None:
            logger.warning(
                "Skipping source with no committed version at HEAD (untracked or generated): %s",
                rel,
            )
            continue
        if dirty:
            hunks = parse_u0_hunks(gitio.diff_worktree_file_u0(repo_root, rel))
            lines, branches = _remap_file(raw_lines, raw_branches, LineRemapper(hunks))
        else:
            lines, branches = raw_lines, raw_branches
        files[rel.as_posix()] = CaptureFileCov(
            blob=blob,
            lines=lines,
            branches=branches,
        )

    captured_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Capture(
        tier=tier,
        product=product,
        base_commit=base_commit,
        dirty_remap=dirty,
        captured_at=captured_at,
        tester=tester,
        ticket=ticket,
        note=note,
        labs=labs,
        board=board,
        display_name=display_name,
        files=files,
    )
