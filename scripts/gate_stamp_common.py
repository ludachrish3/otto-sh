"""What the coverage stamp and the lane stamps share: the tree, its dirt, the store.

A stamp is a COMMITTED-tree contract (see ``scripts/coverage_stamp.py`` and
``scripts/gate_tip.py``): the tree hash names what is committed, the dirt
decides whether a run can speak for that tree, and the store lives under the
clone's common git dir so every worktree sees the same evidence. This module
imports nothing from the other scripts; they import it.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomli

ZERO_SHA = "0" * 40
"""The all-zero sha a pre-push hook is handed for a ref that does not exist."""


class GateFreshError(RuntimeError):
    """Something about RUNNING a gate failed — never a pass/fail verdict.

    Covers both ends of a run: an unmet precondition before it starts (not a
    git repository, an unresolvable ref, an underlying git command failing)
    and a disposal failure after it ends (``git worktree remove`` itself
    failing to clean up a completed run).
    """


@dataclass(frozen=True)
class TreeState:
    """What the invoking repo's working tree holds beyond its HEAD commit."""

    tracked_dirty: "list[str]"
    untracked: "list[str]"


def _git(repo: Path, *args: str, stdin: "str | None" = None) -> str:
    """Run git in *repo* (feeding *stdin* when given) and return its stdout."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # FileNotFoundError included: no git on PATH
        raise GateFreshError(f"git is not available: {exc}") from exc
    if result.returncode != 0:
        raise GateFreshError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def tree_state(repo: Path) -> TreeState:
    """Split the working tree's dirt into blocking and informational halves.

    Tracked modifications block: the worktree is built from a commit and cannot
    see them, so proceeding would gate different content than the developer is
    looking at. Untracked files do not block — the gate failing because one was
    needed is precisely the forgotten-``git add`` case being caught.

    Parsed from ``git status --porcelain -z`` rather than the human-readable
    (newline-delimited) form. Three prior attempts at parsing the newline form
    each enumerated the two-character status code and each enumeration missed
    a case (a bare ``"R "``/``"C "`` check misses ``"RM"``; a first-character
    check misses ``" R"``, reachable when a tracked file is deleted from the
    worktree only and a similar new path is staged with ``git add -N``). The
    ``-z`` form sidesteps status-code enumeration entirely: paths are
    NUL-terminated instead of newline-terminated (so a filename containing a
    space is not ambiguous with the human form's separators), and for a
    rename or copy — whichever column (index-relative or worktree-relative)
    carries the R/C — the origin path arrives as its own trailing
    NUL-terminated field instead of being joined into one field with a
    literal ``" -> "``. The entry's own path is already the destination (the
    file that exists in the tree now); the origin field only needs skipping.
    """
    try:
        _git(repo, "rev-parse", "--show-toplevel")
    except GateFreshError as e:
        raise GateFreshError(f"not a git repository: {repo}") from e
    tracked_dirty: "list[str]" = []
    untracked: "list[str]" = []
    raw = _git(repo, "status", "--porcelain", "--untracked-files=normal", "-z")
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC" or code[1] in "RC":
            i += 1  # skip the origin-path field emitted for renames/copies
        if code == "??":
            untracked.append(path)
        else:
            tracked_dirty.append(path)
    return TreeState(tracked_dirty=sorted(tracked_dirty), untracked=sorted(untracked))


def tree_hash(repo: Path, rev: str = "HEAD") -> str:
    """Return ``<rev>^{tree}``: the committed content, whatever the commit or branch."""
    return _git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


def blob_sha(repo: Path, path: str) -> str:
    """Return the blob id of the working-tree file at *path* (what a commit of it would record)."""
    return _git(repo, "hash-object", "--", path).strip()


def test_roots(repo: Path) -> "list[str]":
    """Return pyproject's ``testpaths``: the roots the live checkout collects from."""
    data = tomli.loads((repo / "pyproject.toml").read_text())
    return list(data["tool"]["pytest"]["ini_options"]["testpaths"])


def blocking_untracked(untracked: "list[str]", roots: "list[str]") -> "list[str]":
    """Select the untracked paths the live checkout would import: under ``src/`` or a test root."""
    prefixes = tuple(f"{root.rstrip('/')}/" for root in ("src", *roots))
    return [path for path in untracked if path.startswith(prefixes)]


def dirt_refusals(state: TreeState, roots: "list[str]") -> "list[str]":
    """Return why a run over this working tree cannot speak for its commit; empty when it can."""
    why: "list[str]" = []
    if state.tracked_dirty:
        why.append(
            "tracked files are modified or staged (the committed tree does not hold them): "
            + ", ".join(state.tracked_dirty)
        )
    blocking = blocking_untracked(state.untracked, roots)
    if blocking:
        why.append(
            "untracked files under src/ or a test root (the live checkout imports them; "
            "the committed tree has no such file): " + ", ".join(blocking)
        )
    return why


def write_atomically(path: Path, text: str) -> None:
    """Write *text* to *path* through a temp file and a rename, creating the parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stamps_root(repo: Path, name: str) -> Path:
    """Return ``<git common dir>/<name>``: one store per clone, shared by its worktrees."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common) / name
