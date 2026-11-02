#!/usr/bin/env python3
"""Make the checkout safe for ``make release`` to build in place, on a release branch.

``make release`` runs on a release branch at ``main``'s tip, pushed to its
upstream, and lands through a pull request: ``main`` takes no direct push.
It builds and tests that checkout where it stands. So it reads local state that CI (a pristine
checkout of the committed tree) never has.
The 0.17.0 release (2026-10-18) stopped three times on such state, and no
push gate had seen any of it (#661):

- a gitignored build left from before a rename: ``tests/project5/build/lib``
  held a build of the kernel coverage library under its old name, which
  ``otto_kmodcov/build.sh`` refuses to replace, and the package directory
  the rename left under ``src/otto/`` held only a ``__pycache__``, which made
  it importable as a namespace package;
- gitignored run output older than the committed verdicts it is checked
  against: ``reports/conformance-observations/``;
- an untracked source the clean Sphinx build reads: a generated docs page
  whose ignore line the rename had re-pointed.

This step runs first, before anything is built. It does five things:

- It REFUSES to run anywhere but a release branch: on ``main`` (which takes
  no direct push, so the release could not land from it) and on a detached
  HEAD (no branch to push and open the pull request from).
- It REFUSES commits not yet pushed to the branch's upstream, and a branch
  with no upstream (:func:`unpushed_commits`): the release is gated and
  landed as a pull request of that branch, so what it certifies must be
  what the remote holds (#660).
- It REFUSES a branch that is not ``main``'s tip
  (``gate_stamp_common.at_main_tip``, the one predicate): the release
  certifies the tree ``main`` will carry once its pull request merges, so
  it starts from ``main`` exactly, neither ahead nor behind.
- It REFUSES untracked files under the paths the release builds and tests
  read (:data:`READ_ROOTS`), naming each one. Whether such a file belongs
  in the repo is the author's call, so nothing here deletes it.
- It CLEARS the gitignored run output the release regenerates anyway
  (:func:`stale_outputs`), and reports each path it removed. Removing these
  costs only a rebuild, so no judgement about what is stale is needed.

Usage, from the repo root, as the Makefile's ``release`` recipe runs it::

    uv run python scripts/release_preflight.py --remote origin
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run by path, only scripts/ is on sys.path
from scripts.gate_stamp_common import GateError, _git, at_main_tip  # noqa: E402

_START_BRANCH = "git checkout -b release/next && git push -u origin release/next"
"""How a maintainer starts a release branch, named in the refusals that need one."""

READ_ROOTS = ["docs/", "src/", "tests/", "schemas/"]
"""Untracked files under these roots are refused: the release's builds and suites read them."""

OBSERVATIONS_GLOB = "reports/*-observations"
"""Run output a matrix test compares with the committed verdicts; the release re-measures it."""

BUILD_DIRS = ["tests/project5/build", "tests/project_ctr/build"]
"""Build output the e2e fixtures rebuild on demand, refusing one another tool made."""

PACKAGE_ROOT = "src/otto"
"""Where a directory holding nothing but ``__pycache__`` would import as a namespace package."""


def untracked_in_read_roots(repo: Path) -> list[str]:
    """Return the untracked, unignored files under :data:`READ_ROOTS`, sorted."""
    out = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["git", "status", "--porcelain", "--untracked-files=all", "-z", "--", *READ_ROOTS],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(entry[3:] for entry in out.split("\0") if entry.startswith("?? "))


def unpushed_commits(repo: Path) -> list[str] | None:
    """Return the one-line summaries of commits on HEAD not on its upstream, oldest first.

    Returns ``None`` when the branch has no upstream to compare with. The
    remote-tracking ref is read as it stands, with no fetch: a stale one can
    only list too much, never hide an unpushed commit.
    """
    run = subprocess.run(
        ["git", "log", "--reverse", "--format=%h %s", "@{upstream}..HEAD"],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )
    if run.returncode != 0:
        return None
    return run.stdout.splitlines()


def _holds_only_pycache(directory: Path) -> bool:
    """Return whether *directory* holds no file outside a ``__pycache__`` directory."""
    for child in directory.iterdir():
        if child.name == "__pycache__" and child.is_dir() and not child.is_symlink():
            continue
        if child.is_dir() and not child.is_symlink() and _holds_only_pycache(child):
            continue
        return False
    return True


def orphan_packages(repo: Path) -> list[Path]:
    """Return the outermost directories under :data:`PACKAGE_ROOT` that hold only ``__pycache__``.

    A package whose sources moved away leaves its bytecode directory
    behind, and Python imports the empty shell as a namespace package.
    """
    root = repo / PACKAGE_ROOT
    if not root.is_dir():
        return []
    found: list[Path] = []
    pending = [root]
    while pending:
        for child in sorted(pending.pop().iterdir()):
            if not child.is_dir() or child.is_symlink() or child.name == "__pycache__":
                continue
            if _holds_only_pycache(child):
                found.append(child)
            else:
                pending.append(child)
    return found


def stale_outputs(repo: Path) -> list[Path]:
    """Return the regenerable run and build output present in *repo*, in a stable order."""
    paths = sorted(p for p in repo.glob(OBSERVATIONS_GLOB) if p.is_dir())
    paths += [repo / d for d in BUILD_DIRS if (repo / d).exists()]
    return paths + orphan_packages(repo)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _refuse(message: str) -> int:
    print(f"release-preflight: {message}", file=sys.stderr)
    return 1


def preflight(repo: Path, *, remote: str = "origin") -> int:
    """Refuse anything but a pushed release branch at main's tip, and untracked sources.

    Otherwise clear the stale output the release regenerates. A git failure on
    the way is a refusal, not a traceback.
    """
    try:
        return _preflight(repo, remote)
    except GateError as exc:
        return _refuse(f"refusing to release: {exc}")


def _preflight(repo: Path, remote: str) -> int:
    branch = _git(repo, "branch", "--show-current").strip()
    if not branch:
        return _refuse(
            "refusing to release from a detached HEAD: make release runs on a release "
            f"branch ({_START_BRANCH})"
        )
    if branch == "main":
        return _refuse(
            "refusing to release from main: main takes no direct push; run make release "
            f"on a release branch ({_START_BRANCH})"
        )
    unpushed = unpushed_commits(repo)
    if unpushed is None:
        return _refuse(
            "refusing to release: there is no way to tell whether every commit is pushed, "
            f"because the branch has no upstream: push -u first (git push -u {remote} {branch})"
        )
    if unpushed:
        print(
            "release-preflight: refusing to release: these commits are not pushed, and the "
            "release certifies and lands what the remote branch holds:",
            file=sys.stderr,
        )
        for line in unpushed:
            print(f"  {line}", file=sys.stderr)
        print("Push them (git push), then run make release again.", file=sys.stderr)
        return 1
    if not at_main_tip(repo, remote):
        head = _git(repo, "rev-parse", "HEAD").strip()
        main_sha = _git(repo, "rev-parse", "FETCH_HEAD").strip()
        return _refuse(
            f"refusing to release: HEAD ({head[:12]}) is not {remote}/main's tip "
            f"({main_sha[:12]}); a release branch starts at main's tip "
            f"(git fetch {remote} main && git checkout -B {branch} FETCH_HEAD)"
        )
    untracked = untracked_in_read_roots(repo)
    if untracked:
        print(
            "release-preflight: refusing to release: untracked files under "
            f"{', '.join(READ_ROOTS)} would be read by the release's builds and suites, "
            "though no commit carries them:",
            file=sys.stderr,
        )
        for path in untracked:
            print(f"  {path}", file=sys.stderr)
        print(
            "Commit each one, ignore it in .gitignore, or move it out of the checkout, "
            "then run make release again.",
            file=sys.stderr,
        )
        return 1
    for path in stale_outputs(repo):
        _remove(path)
        print(f"release-preflight: removed {path.relative_to(repo)} (regenerated by the release)")
    print("release-preflight: the checkout is ready to release")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the checkout to prepare")
    parser.add_argument("--remote", default="origin", help="the remote whose main is the base")
    args = parser.parse_args(argv)
    return preflight(args.repo.resolve(), remote=args.remote)


if __name__ == "__main__":
    sys.exit(main())
