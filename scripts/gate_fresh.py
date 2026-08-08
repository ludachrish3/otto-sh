#!/usr/bin/env python3
"""Run CI's assets-absent Python lanes in a throwaway pristine git worktree.

Local gates run in a tree carrying gitignored build outputs (notably
``src/otto/_webassets/*/``, built only by ``make web``) that a clean CI
checkout never has. The dev tree is therefore a strict superset of CI's
environment, and a superset certifies nothing about a subset — which is how
issue #196 reached ``main`` green. A fresh worktree is free of every
gitignored artifact by construction, and also catches an unsynced ``uv.lock``
and a test that only passes because of a file nobody ``git add``ed.

Usage::

    scripts/gate_fresh.py
    scripts/gate_fresh.py --ref <sha-or-branch>
    scripts/gate_fresh.py --pre-push          # reads git's pre-push stdin
"""

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GATED_TARGETS = ["lint-python", "lint-arch", "typecheck-python", "coverage-hostless"]

ZERO_SHA = "0" * 40
_PRE_PUSH_LINE_FIELDS = 4


class GateFreshError(RuntimeError):
    """Something about *running* the gate failed — never a pass/fail verdict.

    Covers both ends of a run: an unmet precondition before it starts (not a
    git repository, an unresolvable ref, an underlying git command failing)
    and a disposal failure after it ends (``git worktree remove`` itself
    failing to clean up a completed run).
    """


def ref_to_gate(stdin_text: str, *, protected: str = "refs/heads/main") -> str | None:
    """Which sha, if any, this push would put on the protected ref.

    git feeds pre-push one ``<local ref> <local sha> <remote ref> <remote sha>``
    line per ref. The REMOTE ref decides: ``git push origin mybranch:main``
    updates main from a differently-named local branch, and keying off the
    local name would wave it through. An all-zero local sha is a delete.
    """
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) != _PRE_PUSH_LINE_FIELDS:
            continue
        _local_ref, local_sha, remote_ref, _remote_sha = fields
        if remote_ref == protected and local_sha != ZERO_SHA:
            return local_sha
    return None


@dataclass(frozen=True)
class TreeState:
    """What the invoking repo's working tree holds beyond its HEAD commit."""

    tracked_dirty: list[str]
    untracked: list[str]


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise GateFreshError(f"git failed in {repo}: {e}") from e
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
    tracked_dirty: list[str] = []
    untracked: list[str] = []
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


def resolve_ref(repo: Path, ref: str) -> str:
    """Resolve ``ref`` to a full sha, refusing by name if it does not exist."""
    try:
        return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except GateFreshError as exc:
        raise GateFreshError(f"cannot resolve ref {ref!r}: {exc}") from exc


@dataclass(frozen=True)
class GateResult:
    """Outcome of one gate run, and what it left on disk."""

    passed: bool
    worktree: Path
    kept: bool


def _run_targets(worktree: Path, targets: list[str]) -> bool:
    """Sync a fresh venv, then run the gated make targets in the worktree."""
    sync = subprocess.run(["uv", "sync"], cwd=worktree, check=False)  # noqa: S607
    if sync.returncode != 0:
        return False
    return (
        subprocess.run(  # noqa: S603
            ["make", *targets],  # noqa: S607
            cwd=worktree,
            check=False,
        ).returncode
        == 0
    )


def _reconcile_after_cleanup_failure(repo: Path, holder: Path) -> None:
    """Best-effort recovery when ``git worktree remove`` itself fails.

    Deletes ``holder`` from disk regardless (so the corrupted worktree
    doesn't linger), which would otherwise orphan git's own admin metadata
    pointing at a now-missing directory — the exact ``git worktree list``
    pollution ``gate()``'s removal paths exist to prevent, reintroduced
    through a different route. ``git worktree prune`` reconciles that
    metadata after the fact; if even that fails, this stays silent and
    lets the caller's own error handling (a printed diagnostic, or letting
    the cleanup exception itself propagate) surface the situation instead.
    """
    shutil.rmtree(holder, ignore_errors=True)
    with contextlib.suppress(GateFreshError):
        _git(repo, "worktree", "prune")


def gate(
    repo: Path,
    sha: str,
    *,
    targets: list[str] | None = None,
    runner: Callable[[Path, list[str]], bool] | None = None,
) -> GateResult:
    """Run ``targets`` against ``sha`` in a throwaway worktree.

    Three-way disposal, the whole subtlety of this function: a **pass**
    removes the worktree (nothing to debug); a **clean failure** (``run()``
    returns ``False``) keeps it, so a red gate hands back a live tree with its
    reports still on disk; an **exception** (interrupt, harness error) is not
    a gate verdict, so it removes the worktree and re-raises rather than
    leaving a registration that would pollute ``git worktree list`` for every
    later run.

    Every removal path also deletes ``holder``, the ``tempfile.mkdtemp``
    directory ``worktree`` lives in: ``git worktree remove`` only unregisters
    and deletes its own target (the ``holder/"tree"`` leaf), never its parent,
    so leaving ``holder`` alone would leak an empty directory into system temp
    on every single passing run — including one where ``git worktree add``
    itself fails before ``worktree`` ever exists.

    Cleanup failure (``git worktree remove`` itself raising, e.g. because the
    worktree was already corrupted) is handled the same way regardless of
    which branch hit it, via ``_reconcile_after_cleanup_failure()`` — but what
    happens to the *original* control flow differs, because the two branches
    disagree about whether a cleanup failure is the most important thing
    the caller learns about. After an **exception**, that failure is
    swallowed rather than allowed to replace the original exception as what
    the caller sees, so this function prints a one-line diagnostic to stderr
    itself (never raised) before re-raising the original exception unchanged
    — otherwise a stale registration would be reconciled but never reported.
    After a **pass**, there is no other exception in flight to protect, so
    the cleanup failure is simply left to propagate as its own
    ``GateFreshError``: a cleanup failure must never present itself as a
    silent, clean PASS.
    """
    targets = list(GATED_TARGETS if targets is None else targets)
    run = _run_targets if runner is None else runner
    holder = Path(tempfile.mkdtemp(prefix="otto-gate-fresh-"))
    worktree = holder / "tree"
    try:
        _git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), sha)
    except BaseException:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    try:
        passed = run(worktree, targets)
    except BaseException:
        # best-effort: a cleanup failure here must not mask the exception
        # being handled
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree))
        except GateFreshError as cleanup_exc:
            _reconcile_after_cleanup_failure(repo, holder)
            print(
                f"gate-fresh: could not clean up worktree at {worktree}: {cleanup_exc}",
                file=sys.stderr,
            )
        else:
            shutil.rmtree(holder, ignore_errors=True)
        raise
    if passed:
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree))
        except GateFreshError:
            # Unlike the exception branch above, there is no more important
            # exception in flight to protect here — a PASS whose cleanup
            # failed must not report itself as a clean PASS, so this
            # propagates as its own GateFreshError instead of being
            # swallowed.
            _reconcile_after_cleanup_failure(repo, holder)
            raise
        shutil.rmtree(holder, ignore_errors=True)
    return GateResult(passed=passed, worktree=worktree, kept=not passed)


def main(argv: list[str] | None = None) -> int:
    """Refuse on tracked dirt, report untracked dirt, then gate ``--ref``.

    Stream split (matches ``gate()``'s own convention of putting failure
    diagnostics on stderr): stdout carries progress a run that is still
    going is expected to produce — the untracked-file report, the pre-gate
    status line, ``PASS``. stderr carries everything that means this run
    did NOT succeed — the tracked-dirty refusal, a caught
    ``GateFreshError``, and the FAIL/kept-worktree report. A consumer that
    can only watch file descriptors (e.g. a pre-push hook) can then tell
    "still running" from "it failed" without waiting on the exit code.
    Pre-gate stdout writes are flushed explicitly: ``gate()``'s child
    processes (``uv sync``, ``make``) write straight to the inherited fd,
    bypassing Python's stdout buffer, so an unflushed status line would
    otherwise sit behind up to ~3 minutes of that child output when stdout
    isn't a tty.

    Under ``--pre-push`` the tracked-dirty refusal does not apply: ``--ref``
    is already pinned to the sha git is about to push, which is committed
    content the invoking checkout's dirt cannot alter, so proceeding gates
    exactly what the push would put on ``main`` regardless of what is
    lying around locally. Refusing anyway would block a routine push (e.g.
    hand-over work staged in the same checkout) for a reason that does not
    hold in this mode, and the predictable response — ``git push
    --no-verify`` — trains the habit this hook exists to prevent. A brief
    stdout note is emitted rather than staying silent, so a pushing
    developer isn't left wondering why local edits aren't reflected in the
    result.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ref", default="HEAD", help="commit-ish to gate (default: HEAD)")
    parser.add_argument("--repo", default=".", help="repository to gate from (default: cwd)")
    parser.add_argument(
        "--pre-push",
        action="store_true",
        help="read git's pre-push stdin and gate only a push to main",
    )
    args = parser.parse_args(argv)

    if args.pre_push:
        sha = ref_to_gate(sys.stdin.read())
        if sha is None:
            return 0
        args.ref = sha

    repo = Path(args.repo).resolve()
    try:
        state = tree_state(repo)
        if state.tracked_dirty:
            if args.pre_push:
                print(
                    "gate-fresh: local tracked-file changes exist but are not part of "
                    "this gate — a pre-push run gates the sha being pushed, which is "
                    "already committed, so working-tree dirt cannot affect it.",
                    flush=True,
                )
            else:
                print(
                    "gate-fresh: refusing — tracked files are modified or staged, and a "
                    "worktree cannot see them, so this would gate different content than "
                    "you are looking at. Commit first, then gate:",
                    file=sys.stderr,
                )
                for name in state.tracked_dirty:
                    print(f"  {name}", file=sys.stderr)
                return 1
        if state.untracked:
            print("gate-fresh: these untracked files are NOT in the gated tree:", flush=True)
            for name in state.untracked:
                print(f"  {name}", flush=True)
        sha = resolve_ref(repo, args.ref)
        print(
            f"gate-fresh: gating {sha[:12]} in a pristine worktree ({', '.join(GATED_TARGETS)})",
            flush=True,
        )
        result = gate(repo, sha)
    except GateFreshError as exc:
        print(f"gate-fresh: {exc}", file=sys.stderr)
        return 1

    if result.passed:
        print("gate-fresh: PASS")
        return 0
    print(f"gate-fresh: FAIL — worktree kept for debugging at {result.worktree}", file=sys.stderr)
    print(
        "gate-fresh: remove it with `git worktree remove --force <path>` when done",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
