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


def _run_raw_input(
    args: list[str], cwd: Path | None, stdin: bytes, ok_codes: tuple[int, ...] = (0,)
) -> bytes:
    """Run git with *stdin* piped in; mirrors ``_run_raw``'s error translation.

    A separate helper (not an optional param on ``_run_raw``) so that
    ``_run_raw``'s signature — and the positional-forwarding monkeypatches
    committed spawn-count tests rely on — never changes.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            input=stdin,
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


def hash_objects(repo_root: Path, paths: list[Path]) -> list[str]:
    """Hash many files in one spawn via ``git hash-object --stdin-paths``.

    Output order matches *paths* order. Reads paths from stdin instead of
    argv so a whole capture's worth of files costs one process instead of
    one per file (spec §9 batched fallback); applies the same
    attribute-driven filters as :func:`hash_object`, so results are
    identical file-for-file.
    """
    stdin = "".join(f"{p}\n" for p in paths).encode()
    out = _run_raw_input(["hash-object", "--stdin-paths"], repo_root, stdin)
    return out.decode().splitlines()


def blob_exists(repo_root: Path, sha: str) -> bool:
    """Return True if a blob exists in the repository."""
    try:
        _run(["cat-file", "-e", sha], repo_root)
    except GitUnavailableError as e:
        if "not a git repository" in str(e):
            raise
        return False
    return True


def blobs_exist(repo_root: Path, shas: list[str]) -> set[str]:
    """Return the subset of *shas* present in the object database, in one spawn.

    ``git cat-file --batch-check`` answers one line per input sha: ``<sha>
    <type> <size>`` when present, ``<sha> missing`` when absent.
    """
    stdin = "".join(f"{sha}\n" for sha in shas).encode()
    out = _run_raw_input(["cat-file", "--batch-check"], repo_root, stdin).decode()
    present: set[str] = set()
    for line in out.splitlines():
        if not line.endswith("missing"):
            present.add(line.split()[0])
    return present


def cat_blob(repo_root: Path, sha: str) -> bytes:
    """Return the contents of a blob."""
    return _run_raw(["cat-file", "blob", sha], repo_root)


_BATCH_HEADER_FIELDS = 3  # "<sha> <type> <size>"; a miss is "<sha> missing" (2 fields)


def cat_blobs(repo_root: Path, shas: list[str]) -> dict[str, bytes]:
    r"""Fetch many blobs' contents in one spawn via ``git cat-file --batch``.

    The batch stream is ``<sha> <type> <size>\n`` followed by exactly
    *size* content bytes and a trailing ``\n``, repeated per input sha
    (a sha git can't find instead emits ``<sha> missing\n``, skipped
    here). Duplicate shas in *shas* are fine — later entries just
    overwrite earlier ones in the returned dict.
    """
    stdin = "".join(f"{sha}\n" for sha in shas).encode()
    out = _run_raw_input(["cat-file", "--batch"], repo_root, stdin)
    result: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode()
        pos = nl + 1
        parts = header.split()
        sha = parts[0]
        if len(parts) != _BATCH_HEADER_FIELDS:  # "<sha> missing"
            continue
        size = int(parts[2])
        result[sha] = out[pos : pos + size]
        pos += size + 1  # skip the trailing newline after the content
    return result


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


def diff_no_index_dir_u0(dir_a: Path, dir_b: Path) -> str:
    """Return a unified diff (U0, whitespace-insensitive) between two sibling dirs.

    One spawn answers every changed file's diff for the batched blob-
    fallback index (spec §9 amendment) instead of one ``--no-index`` spawn
    per file, mirroring :func:`diff_no_index_u0`'s ``-w``/exit-code
    contract. *dir_a* and *dir_b* must be siblings (share a parent) —
    passing their bare names with ``cwd=dir_a.parent`` makes git report
    paths as ``<dir_a.name>/<rel>`` / ``<dir_b.name>/<rel>`` (after its own
    standard ``a/``/``b/`` prefix, which
    :func:`~otto.coverage.capture.treediff.parse_multifile_u0` already
    strips); the caller strips the remaining ``<dir_a.name>/`` prefix to
    recover capture-relative paths.
    """
    return _run(
        ["diff", "--no-index", "-w", "-U0", dir_a.name, dir_b.name],
        cwd=dir_a.parent,
        ok_codes=(0, 1),
    )


def diff_tree_u0(repo_root: Path, base: str) -> str:
    """Tree-wide ``-U0`` diff of *base* vs the working tree, rename-detecting.

    One subprocess per capture replaces the per-file anchor diffs (spec
    §9). ``-M`` reports renames (spec §8.2: git's tracking is the whole
    policy); ``-w`` matches :func:`diff_worktree_file_u0`'s whitespace
    immunity (spec §8.1); ``--relative -- .`` scopes and re-roots paths
    when ``repo_root`` is nested inside a larger repository.

    Raises:
        GitUnavailableError: *base* is not a resolvable commit here
            (GC'd after a squash-merge, or absent from a shallow clone).
            Callers fall back to the per-file blob chain.
    """
    return _run(
        ["diff", "-M", "-w", "-U0", "--relative", base, "--", "."],
        repo_root,
    )


def is_shallow(repo_root: Path) -> bool:
    """Return whether the repo is a shallow clone (affects anchor degradation hints)."""
    return _run(["rev-parse", "--is-shallow-repository"], repo_root).strip() == "true"
