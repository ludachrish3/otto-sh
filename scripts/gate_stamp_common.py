"""What the coverage stamp and the lane stamps share: the tree, its dirt, the store.

A stamp is a COMMITTED-tree contract (see ``scripts/coverage_stamp.py`` and
``scripts/gate_tip.py``): the tree hash names what is committed, the dirt
decides whether a run can speak for that tree, and the store lives under the
clone's common git dir so every worktree sees the same evidence. This module
imports nothing from the other scripts; they import it.
"""

import contextlib
import fcntl
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import tomli

ZERO_SHA = "0" * 40
"""The all-zero sha a pre-push hook is handed for a ref that does not exist."""


class GateError(RuntimeError):
    """Something about RUNNING a gate failed — never a pass/fail verdict.

    Covers both ends of a run: an unmet precondition before it starts (not a
    git repository, an unresolvable ref, an underlying git command failing)
    and a disposal failure after it ends (``git worktree remove`` itself
    failing to clean up a completed run).
    """

    stderr: str = ""
    """The failed git command's stderr, when the error wraps one (:func:`_git`)."""


@dataclass(frozen=True)
class TreeState:
    """What the invoking repo's working tree holds beyond its HEAD commit."""

    tracked_dirty: "list[str]"
    untracked: "list[str]"
    ignored_sources: "list[str]"
    """Ignored ``.py`` files under ``src/`` or a test root: imported live, not committed."""


def run_git(
    repo: Path, *args: str, stdin: "str | None" = None, env: "dict[str, str] | None" = None
) -> "subprocess.CompletedProcess[str]":
    """Run git in *repo* and return the completed process, whatever its exit status.

    The one git spawn of ``gate_tip``, ``gate_scope`` and this module: *stdin* is fed
    when given, *env* replaces the environment when given (``None`` inherits it).
    Output is text, undecodable bytes replaced. A caller that reads the status itself
    (an exit 1 that means "no") calls this; one for which any non-zero exit is an error
    calls :func:`_git`. Only a failure to spawn git at all raises (:class:`GateError`).
    """
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            check=False,
        )
    except OSError as exc:  # FileNotFoundError included: no git on PATH
        raise GateError(f"git is not available: {exc}") from exc


def _git(repo: Path, *args: str, stdin: "str | None" = None) -> str:
    """Run git in *repo* (feeding *stdin* when given) and return its stdout."""
    result = run_git(repo, *args, stdin=stdin)
    if result.returncode != 0:
        error = GateError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
        error.stderr = result.stderr.strip()
        raise error
    return result.stdout


def tree_state(repo: Path) -> TreeState:
    """Split the working tree's dirt into blocking and informational halves.

    Three kinds of dirt come back, because a stamp speaks for the COMMITTED tree and the live
    checkout can differ from it in three ways. ``tracked_dirty``: modified or staged tracked
    files, which the commit does not hold. ``untracked``: files git does not know. And
    ``ignored_sources``: ignored ``.py`` files under ``src/`` or a test root, which
    ``git status`` never lists yet the checkout imports. Tracked dirt always blocks; untracked
    files and ignored sources block only under ``src/`` or a test root (a stray note or build
    output elsewhere is harmless), which :func:`blocking_untracked` decides and
    :func:`dirt_refusals` renders.

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
        top = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    except GateError as e:
        raise GateError(f"not a git repository: {repo}") from e
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
    ignored = _git(
        top, "ls-files", "-o", "-i", "--exclude-standard", "-z", "--", "src/", *test_roots(top)
    )
    ignored_sources = sorted(path for path in ignored.split("\0") if path.endswith(".py"))
    return TreeState(
        tracked_dirty=sorted(tracked_dirty),
        untracked=sorted(untracked),
        ignored_sources=ignored_sources,
    )


def tree_hash(repo: Path, rev: str = "HEAD") -> str:
    """Return ``<rev>^{tree}``: the committed content, whatever the commit or branch."""
    return _git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


def test_roots(repo: Path) -> "list[str]":
    """Return pyproject's ``testpaths``: the roots the live checkout collects from."""
    try:
        data = tomli.loads((repo / "pyproject.toml").read_text())
        return list(data["tool"]["pytest"]["ini_options"]["testpaths"])
    except (OSError, KeyError, tomli.TOMLDecodeError) as exc:
        raise GateError(f"no pytest testpaths in {repo / 'pyproject.toml'}: {exc!r}") from exc


def blocking_untracked(state: TreeState, roots: "list[str]") -> "list[str]":
    """Select the files the live checkout would import that the commit lacks.

    The untracked paths under ``src/`` or a test root, plus every ignored source
    (:attr:`TreeState.ignored_sources`): the one decision :func:`dirt_refusals` and the
    runner's post-lane check both make.
    """
    prefixes = tuple(f"{root.rstrip('/')}/" for root in ("src", *roots))
    under = [path for path in state.untracked if path.startswith(prefixes)]
    return sorted({*under, *state.ignored_sources})


def dirt_refusals(state: TreeState, roots: "list[str]") -> "list[str]":
    """Return why a run over this working tree cannot speak for its commit; empty when it can."""
    why: "list[str]" = []
    if state.tracked_dirty:
        why.append(
            "tracked files are modified or staged (the committed tree does not hold them): "
            + ", ".join(state.tracked_dirty)
        )
    blocking = blocking_untracked(state, roots)
    # Deliberate: blocking_untracked is the one decision; split its result back only to word it.
    ignored = set(state.ignored_sources)
    untracked = [path for path in blocking if path not in ignored]
    if untracked:
        why.append(
            "untracked files under src/ or a test root (the live checkout imports them; "
            "the committed tree has no such file): " + ", ".join(untracked)
        )
    why.extend(
        f"ignored source the check would import: {path} "
        "(git ls-files -o -i names it; move it out or un-ignore and commit it)"
        for path in blocking
        if path in ignored
    )
    return why


def fetch_main(repo: Path, remote: str) -> str:
    """Fetch *remote*'s ``main`` and return the commit it names: the one base resolver."""
    try:
        _git(repo, "fetch", remote, "main")
    except GateError as exc:
        raise GateError(
            f"{remote} has no main (git fetch {remote} main failed): {exc.stderr}"
        ) from exc
    return _git(repo, "rev-parse", "FETCH_HEAD").strip()


def at_main_tip(repo: Path, remote: str) -> bool:
    """Whether HEAD is exactly *remote*'s freshly fetched ``main``; a failed fetch raises."""
    return fetch_main(repo, remote) == _git(repo, "rev-parse", "HEAD").strip()


_GITHUB_URL = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?"
)


def remote_slug(repo: Path, remote: str) -> str:
    """Return ``owner/repo`` for *remote*'s GitHub URL; anything else is refused."""
    url = _git(repo, "remote", "get-url", remote).strip()
    match = _GITHUB_URL.fullmatch(url)
    if match is None:
        raise GateError(f"{remote} ({url}) is not a GitHub remote; the status needs owner/repo")
    return match["slug"]


LOCK_FILE = ".lock"
"""The flock file at a store's root (:func:`store_lock`)."""


@contextlib.contextmanager
def store_lock(
    root: Path, *, shared: bool = False, dir_mode: int | None = None, file_mode: int | None = None
) -> Iterator[None]:
    """Hold the store's lock (flock on ``<root>/.lock``): exclusive, or *shared* for readers.

    A writer (exclusive) creates the store and the lock file if missing, and waits for every
    other holder. A reader (shared) waits only for a writer; with no store there is nothing
    to read, so it takes no lock and creates nothing. Only for short store steps, never
    while a lane runs, and never nested: two flocks of one process on two open files of the
    same lock can deadlock. *dir_mode* and *file_mode* are set on the root and the lock
    file when THIS call creates them (a store shared across users, such as the bed
    lease's), never on one
    that already exists: another user's file is not ours to chmod, and need not be.
    """
    if shared and not root.is_dir():
        yield
        return
    # Created exclusively, so two users racing to make the store each learn
    # whether THEY made it: the loser must not chmod the winner's object (EPERM).
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        pass
    else:
        if dir_mode is not None:
            root.chmod(dir_mode)
    lock = root / LOCK_FILE
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND)
    except FileExistsError:
        fd = os.open(lock, os.O_WRONLY | os.O_APPEND)
    else:
        if file_mode is not None:
            os.fchmod(fd, file_mode)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_atomically(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write *text* to *path* through a temp file and a rename, creating the parents.

    *mode* is set on the file before the rename (``mkstemp`` creates it ``0o600``
    whatever the umask), for a file other users must be able to replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stamps_root(repo: Path, name: str) -> Path:
    """Return ``<git common dir>/<name>``: one store per clone, shared by its worktrees."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common) / name


JUNIT_DIR = Path("reports") / "junit"
"""Where every lane writes its JUnit XML.

The Makefile's ``JUNIT_DIR`` and noxfile's ``JUNIT_DIR`` spell the same path; the census in
tests/unit/scripts/test_gate_tip.py pins the three equal.
"""


def utc_iso(when: datetime) -> str:
    """Render *when* as the stamps and the bed lease write times (ISO 8601, seconds)."""
    return when.isoformat(timespec="seconds")


def utc_now() -> str:
    """Return the current UTC time as the stamps write it (ISO 8601, seconds)."""
    return utc_iso(datetime.now(timezone.utc))


def nproc() -> int:
    """Return the CPUs this process may run on (what ``nproc(1)`` prints; 1 when unknown).

    The affinity mask, not the machine's count: a cpuset or ``taskset`` narrows what a
    lane can spread over, and the Makefile's worker counts read the same number.
    """
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def junit_snapshot(repo: Path) -> "dict[str, list[int]]":
    """Return every JUnit report under *repo* with its mtime (ns) and size.

    Taken before a lane runs and again after it: what is new, or whose mtime or size
    moved, is what the lane wrote. No clock reference is involved, so a report an earlier
    run left in the same tick as the lane's start is never the lane's, and a report the
    lane rewrote in place is.
    """
    base = repo / JUNIT_DIR
    if not base.is_dir():
        return {}
    found: "dict[str, list[int]]" = {}
    for path in base.rglob("*.xml"):
        try:
            info = path.stat()
        except OSError:  # vanished between the glob and the stat: not a report of ours
            continue
        found[path.relative_to(repo).as_posix()] = [info.st_mtime_ns, info.st_size]
    return found


def junit_snapshot_or_none(repo: Path) -> "dict[str, list[int]] | None":
    """Return ``junit_snapshot(repo)``, or None when the directory cannot be read.

    The one answer to "what if the before-snapshot cannot be taken": nothing is
    attributed (``junit_written`` of None is empty), never everything. A report.
    """
    try:
        return junit_snapshot(repo)
    except OSError:
        return None


def junit_written(
    before: "dict[str, list[int]] | None", after: "dict[str, list[int]] | None"
) -> "list[str]":
    """Return the reports *after* holds that *before* did not, or holds differently; sorted.

    Either snapshot missing (None: it could not be taken) attributes nothing.
    """
    if before is None or after is None:
        return []
    return sorted(path for path, mark in after.items() if before.get(path) != mark)
