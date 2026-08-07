"""Per-capture anchor resolution: one tree diff, per-file blob fallback (spec §8/§9).

Semantics table lives in AnchorResolver's docstring; keep it in sync
with tests/unit/cov/test_anchor.py.
"""

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .capture import gitio
from .capture.model import CaptureFileCov
from .capture.remap import Hunk, parse_u0_hunks
from .capture.treediff import FileDiff, parse_multifile_u0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnchorResult:
    """Where one file's capture coordinates land in the current tree."""

    new_relpath: Path | None  # None = deleted or unverifiable
    hunks: list[Hunk] = field(default_factory=list)
    verifiable: bool = True


class AnchorResolver:
    """Resolve every file of one capture against the working tree.

    | situation                         | result                                       |
    | base ok, absent from tree diff    | (relpath, [], True) — unchanged/ws-only      |
    | base ok, modified                 | (relpath, hunks, True)                       |
    | base ok, renamed                  | (new_path, hunks, True)                      |
    | base ok, deleted                  | (None, [], False)                            |
    | base missing (GC'd / shallow)     | batched fallback index (or per-file chain)   |
    """

    def __init__(
        self,
        repo_root: Path,
        base_commit: str,
        *,
        files: dict[str, CaptureFileCov] | None = None,
    ) -> None:
        self._root = repo_root
        self._base = base_commit
        self._tree: dict[str, FileDiff] | None = None
        self._fallback_index: dict[str, AnchorResult] | None = None
        try:
            self._tree = parse_multifile_u0(gitio.diff_tree_u0(repo_root, base_commit))
        except (gitio.NotAGitRepoError, gitio.GitMissingError):
            # Neither is "this base_commit is gone", and the fallback below
            # runs the SAME gitio calls: it would die anyway, several frames
            # deeper inside hash_objects, with worse context.
            raise
        except gitio.GitCommandFailedError:
            shallow_hint = (
                " (shallow clone — deepen with `git fetch --unshallow` to recover history)"
                if gitio.is_shallow(repo_root)
                else ""
            )
            logger.warning(
                "base_commit %s is not resolvable%s; falling back to per-file blob anchors.",
                base_commit[:12],
                shallow_hint,
            )
            if files is not None:
                self._fallback_index = self._build_fallback_index(files)

    def resolve(self, relpath: Path, fc: CaptureFileCov) -> AnchorResult:
        """Resolve one capture-relative *relpath* against this resolver's diff/fallback."""
        if self._tree is None:
            if self._fallback_index is not None:
                indexed = self._fallback_index.get(relpath.as_posix())
                if indexed is not None:
                    return indexed
            return self._resolve_by_blob(relpath, fc)
        fd = self._tree.get(relpath.as_posix())
        if fd is None:
            # Not in the diff: unchanged (or whitespace-only) — but only
            # verifiable if the file actually exists here (a capture from
            # a different repo names paths the diff never saw).
            if (self._root / relpath).is_file():
                return AnchorResult(new_relpath=relpath)
            return AnchorResult(new_relpath=None, verifiable=False)
        if fd.new_path is None:
            return AnchorResult(new_relpath=None, verifiable=False)
        return AnchorResult(new_relpath=Path(fd.new_path), hunks=fd.hunks)

    def _resolve_by_blob(self, relpath: Path, fc: CaptureFileCov) -> AnchorResult:
        """Today's per-file chain: blob fast-path → blob diff → unverifiable."""
        current = self._root / relpath
        if not current.is_file():
            return AnchorResult(new_relpath=None, verifiable=False)
        if fc.blob and gitio.hash_object(self._root, current) == fc.blob:
            return AnchorResult(new_relpath=relpath)
        base_blob = fc.blob if fc.blob and gitio.blob_exists(self._root, fc.blob) else None
        if base_blob is None:
            base_blob = gitio.blob_sha(self._root, relpath, rev=self._base)
        if base_blob is None:
            return AnchorResult(new_relpath=None, verifiable=False)
        with tempfile.NamedTemporaryFile(suffix=relpath.suffix) as tmp:
            Path(tmp.name).write_bytes(gitio.cat_blob(self._root, base_blob))
            diff = gitio.diff_no_index_u0(Path(tmp.name), current)
        return AnchorResult(new_relpath=relpath, hunks=parse_u0_hunks(diff))

    def _build_fallback_index(self, files: dict[str, CaptureFileCov]) -> dict[str, AnchorResult]:
        """Resolve every file's blob fallback with O(1) git spawns (spec §9 amendment).

        Same semantics as ``_resolve_by_blob`` (fast-path blob match →
        blob diff → unverifiable) but batched: files with ``fc.blob is
        None`` are left out of the index for the lazy path to handle;
        files whose current path is missing are unverifiable immediately.
        Everything else is hashed in one spawn, split into unchanged
        (fast-pathed verbatim) vs. changed; the changed set's base blobs
        are existence-checked and fetched in one spawn each, then diffed
        against the current content in one spawn over two sibling temp
        trees — the base_commit resolvability is gone here (that's why
        we're in this branch at all), so there is no per-file
        ``blob_sha(rev=base)`` rescue to replicate: it would fail for
        every file the same way it does in ``_resolve_by_blob``.
        """
        index: dict[str, AnchorResult] = {}
        candidates: dict[str, Path] = {}
        base_blob_of: dict[str, str] = {}
        for rel_str, fc in files.items():
            if fc.blob is None:
                continue  # left to the lazy per-file path
            current = self._root / rel_str
            if not current.is_file():
                index[rel_str] = AnchorResult(new_relpath=None, verifiable=False)
                continue
            candidates[rel_str] = current
            base_blob_of[rel_str] = fc.blob
        if not candidates:
            return index

        rel_order = list(candidates)
        hashes = gitio.hash_objects(self._root, [candidates[r] for r in rel_order])
        changed: list[str] = []
        for rel_str, current_hash in zip(rel_order, hashes, strict=True):
            if current_hash == base_blob_of[rel_str]:
                index[rel_str] = AnchorResult(new_relpath=Path(rel_str))
            else:
                changed.append(rel_str)
        if not changed:
            return index

        base_shas = {base_blob_of[r] for r in changed}
        present = gitio.blobs_exist(self._root, list(base_shas))
        resolvable = [r for r in changed if base_blob_of[r] in present]
        for r in changed:
            if base_blob_of[r] not in present:
                index[r] = AnchorResult(new_relpath=None, verifiable=False)
        if not resolvable:
            return index

        blobs = gitio.cat_blobs(self._root, list({base_blob_of[r] for r in resolvable}))
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = Path(tmp) / "base"
            dir_b = Path(tmp) / "current"
            dir_a.mkdir()
            dir_b.mkdir()
            for r in resolvable:
                rel = Path(r)
                base_file = dir_a / rel
                base_file.parent.mkdir(parents=True, exist_ok=True)
                base_file.write_bytes(blobs[base_blob_of[r]])
                current_file = dir_b / rel
                current_file.parent.mkdir(parents=True, exist_ok=True)
                current_file.write_bytes(candidates[r].read_bytes())
            diff_text = gitio.diff_no_index_dir_u0(dir_a, dir_b)

        parsed = parse_multifile_u0(diff_text)
        prefix = f"{dir_a.name}/"
        for r in resolvable:
            fd = parsed.get(prefix + r)
            if fd is None:
                # Absent from the -w diff: whitespace-only, verbatim.
                index[r] = AnchorResult(new_relpath=Path(r))
            else:
                index[r] = AnchorResult(new_relpath=Path(r), hunks=fd.hunks)
        return index
