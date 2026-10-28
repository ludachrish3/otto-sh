"""The per-(host, product) ``capture.json`` artifact (spec §3).

A capture stores line/branch data in **committed-code coordinates**,
anchored to the ``base_commit`` whose numbering they mean, with
per-file blob SHAs as the rebase-tolerant validity anchor.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ConfigDict, Field

from ...models.base import OttoModel
from ..formats import CAPTURE_READ_VERSIONS, CAPTURE_WRITE_VERSIONS, found_version_label
from . import gitio
from .remap import LineRemapper, parse_u0_hunks

logger = logging.getLogger(__name__)

[CAPTURE_FORMAT_VERSION] = CAPTURE_WRITE_VERSIONS
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


class CaptureFileCov(OttoModel):
    """Coverage for one source file, keyed in ``base_commit`` coordinates."""

    model_config = ConfigDict(extra="forbid")

    blob: str | None = None
    lines: dict[int, int] = Field(default_factory=dict)
    branches: dict[int, list[tuple[int, int, int | None]]] = Field(default_factory=dict)


class Capture(OttoModel):
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
                one of ``CAPTURE_READ_VERSIONS`` (including files with no
                ``"schema"`` key at all — everything predating this
                capture-format version). There is no migration shim; the
                message tells the caller to re-capture.
        """
        raw_text = path.read_text()
        raw = json.loads(raw_text)
        found_format = raw.get("schema") if isinstance(raw, dict) else None
        if found_format not in CAPTURE_READ_VERSIONS:
            raise ValueError(
                f"capture format {' or '.join(f'v{v}' for v in CAPTURE_READ_VERSIONS)} required; "
                f"found {found_version_label(found_format)} — re-capture with otto cov get"
            )
        return cls.model_validate_json(raw_text)


def parse_info(
    info_path: Path,
) -> dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int | None]]]]]:
    """Read an ``.info`` into the capture's shape: source path → (line hits, branch triples).

    One reader for lcov's text, :func:`otto.coverage.merge.lcov_loader.read_info`,
    serves the capture and the store loader alike, so a repeated source adds
    up the same way on both. Branch triples are ``(block, branch, taken)``
    where ``taken`` is ``None`` for lcov's ``-`` (branch never reached) and
    an ``int`` count otherwise — preserving the never-reached/reached-but-
    not-taken distinction through to the capture file. An exception-tagged
    ``BRDA:`` record is dropped by the reader: no triple is appended for it.
    """
    # Here, not at the top: the merge pipeline imports the store, which a
    # capture reader on the CLI's cov verbs need not load until it parses.
    from ..merge.lcov_loader import read_info

    files: dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int | None]]]]] = {}
    for path, record in read_info(info_path).items():
        branches: dict[int, list[tuple[int, int, int | None]]] = {}
        for (lineno, block, branch), taken in record.branches.items():
            branches.setdefault(lineno, []).append((block, branch, taken))
        files[path] = (dict(record.lines), branches)
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
    project_root: Path,
    board: str,
    product: str,
    labs: list[str],
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    display_name: str | None = None,
    base_commit: str | None = None,
    now: datetime | None = None,
) -> Capture:
    """Build a :class:`Capture` anchored to ``base_commit`` from an lcov ``.info`` file.

    Args:
        board: The staging-dir/host-id name the counters came from.
        product: The product whose counters these are — the ``<product>``
            segment of ``cov/<host>/<product>/``.
        display_name: Host display name to annotate onto the capture;
            ``board`` stays the staging-dir/host-id name.
        base_commit: The commit the counters were collected at, when known;
            the capture is refused unless the tree is still at it.

    Raises:
        otto.coverage.errors.CaptureCommitError: *base_commit* is given and
            the tree's HEAD is a different commit.
    """
    head = gitio.head_commit(project_root)
    if base_commit is not None and base_commit != head:
        from ..errors import CaptureCommitError

        raise CaptureCommitError(capture=str(info_path.parent), taken_at=base_commit, head=head)
    base_commit = head
    dirty = gitio.is_dirty(project_root)
    project_root = project_root.resolve()

    files: dict[str, CaptureFileCov] = {}
    outside = 0
    for src, (raw_lines, raw_branches) in parse_info(info_path).items():
        src_path = Path(src).resolve()
        if not src_path.is_relative_to(project_root):
            outside += 1
            logger.debug("outside the project root: %s", src)
            continue
        rel = src_path.relative_to(project_root)
        blob = gitio.blob_sha(project_root, rel)
        if blob is None:
            logger.warning(
                "Skipping source with no committed version at HEAD (untracked or generated): %s",
                rel,
            )
            continue
        if dirty:
            hunks = parse_u0_hunks(gitio.diff_worktree_file_u0(project_root, rel))
            lines, branches = _remap_file(raw_lines, raw_branches, LineRemapper(hunks))
        else:
            lines, branches = raw_lines, raw_branches
        files[rel.as_posix()] = CaptureFileCov(
            blob=blob,
            lines=lines,
            branches=branches,
        )

    if outside:
        logger.info(
            "%s/%s: skipped %d source file(s) outside the project root", board, product, outside
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
