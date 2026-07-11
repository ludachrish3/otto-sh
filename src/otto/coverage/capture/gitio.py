"""Thin subprocess wrappers around the git plumbing coverage needs.

Everything is synchronous and side-effect-free on the repo (read-only
commands only).  Callers pass the sut repo root; a non-repo raises
:class:`GitUnavailableError` with a clean message.
"""

import subprocess
from pathlib import Path


class GitUnavailableError(RuntimeError):
    """Raised when git cannot answer (not a repo / git missing)."""


def _run_raw(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> bytes:
    """Run git and return raw stdout bytes; translate subprocess errors uniformly."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=False,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitUnavailableError("git executable not found") from e
    if proc.returncode not in ok_codes:
        stderr = proc.stderr.decode(errors="replace")
        raise GitUnavailableError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.strip()}"
        )
    return proc.stdout


def _run(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> str:
    return _run_raw(args, cwd, ok_codes).decode()


def head_commit(repo_root: Path) -> str:
    """Return the current HEAD commit SHA."""
    return _run(["rev-parse", "HEAD"], repo_root).strip()


def is_dirty(repo_root: Path) -> bool:
    """Return True if there are uncommitted changes."""
    return bool(_run(["status", "--porcelain"], repo_root).strip())


def blob_sha(repo_root: Path, relpath: Path, rev: str = "HEAD") -> str | None:
    """Return the SHA of a blob at a path/revision, or None if not found.

    The ``./`` prefix makes git resolve the path against ``repo_root``
    (our cwd) — a bare ``REV:<path>`` resolves against the repo toplevel,
    which is wrong whenever ``repo_root`` is a subdirectory of a larger
    repo (e.g. a sut checked out inside another project).
    """
    try:
        return _run(["rev-parse", f"{rev}:./{relpath.as_posix()}"], repo_root).strip()
    except GitUnavailableError as e:
        if "not a git repository" in str(e):
            raise
        return None


def hash_object(repo_root: Path, path: Path) -> str:
    """Return the SHA1 hash of a file object."""
    return _run(["hash-object", str(path)], repo_root).strip()


def blob_exists(repo_root: Path, sha: str) -> bool:
    """Return True if a blob exists in the repository."""
    try:
        _run(["cat-file", "-e", sha], repo_root)
    except GitUnavailableError as e:
        if "not a git repository" in str(e):
            raise
        return False
    return True


def cat_blob(repo_root: Path, sha: str) -> bytes:
    """Return the contents of a blob."""
    return _run_raw(["cat-file", "blob", sha], repo_root)


def diff_worktree_file_u0(repo_root: Path, relpath: Path) -> str:
    """Return unified diff (U0, whitespace-insensitive) of HEAD vs worktree file.

    ``-w`` (``--ignore-all-space``) suppresses whitespace-only line
    modifications so a reformat/reindent does not invalidate manual
    coverage anchored to the untouched code. Safe for the line remapper:
    a whitespace-only modification is 1 line -> 1 line and shifts no
    counts, so hiding it loses no hunk-offset information (unlike
    ``--ignore-blank-lines``, which would hide count-changing hunks). The
    SUTs are C/C++, where intra-string whitespace is not coverage-
    relevant, so the one behavioural case ``-w`` also equates (spacing
    inside a string literal) is an accepted, immaterial false-valid.
    """
    return _run(["diff", "-w", "-U0", "HEAD", "--", relpath.as_posix()], repo_root)


def diff_no_index_u0(path_a: Path, path_b: Path) -> str:
    """Return unified diff (U0, whitespace-insensitive) between two files outside a repo.

    ``-w`` matches :func:`diff_worktree_file_u0` so the report-time anchor
    chain (base_commit blob vs current file) ignores whitespace-only edits the
    same way the dirty-tree remap does. ``git diff --no-index`` exits 1
    when the files differ — that is success here; with ``-w`` a
    whitespace-only difference exits 0 with empty output (hunkless), which
    the remapper treats as verbatim.
    """
    return _run(
        ["diff", "--no-index", "-w", "-U0", str(path_a), str(path_b)], cwd=None, ok_codes=(0, 1)
    )
