#!/usr/bin/env python3
"""Make the primary checkout safe for ``make release`` to build in place.

``make release`` must run in the primary checkout, and it builds and tests
that checkout where it stands. So it reads local state that CI and
``make gate-fresh`` (a pristine worktree of the committed tree) never have.
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

This step runs first, before anything is built. It does three things:

- It REFUSES commits not yet pushed to the branch's upstream
  (:func:`unpushed_commits`). The push gate excuses only the release's own
  commits, so an unpushed commit would need its lanes stamped again on the
  released tree; pushing first costs a maintainer one command (#660).
- It REFUSES untracked files under the paths the release builds and tests
  read (:data:`READ_ROOTS`), naming each one. Whether such a file belongs
  in the repo is the author's call, so nothing here deletes it.
- It CLEARS the gitignored run output the release regenerates anyway
  (:func:`stale_outputs`), and reports each path it removed. Removing these
  costs only a rebuild, so no judgement about what is stale is needed.

Usage, from the repo root, as the Makefile's ``release`` recipe runs it::

    uv run python scripts/release_preflight.py
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def preflight(repo: Path) -> int:
    """Refuse unpushed commits and untracked sources, otherwise clear stale output."""
    unpushed = unpushed_commits(repo)
    if unpushed is None:
        print(
            "release-preflight: refusing to release: the branch has no upstream, so there "
            "is no way to tell whether every commit is pushed. Release from main tracking "
            "origin/main.",
            file=sys.stderr,
        )
        return 1
    if unpushed:
        print(
            "release-preflight: refusing to release: these commits are not pushed, and the "
            "push gate after a release excuses only the release's own commits:",
            file=sys.stderr,
        )
        for line in unpushed:
            print(f"  {line}", file=sys.stderr)
        print("Push them (git push), then run make release again.", file=sys.stderr)
        return 1
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
    args = parser.parse_args(argv)
    return preflight(args.repo.resolve())


if __name__ == "__main__":
    sys.exit(main())
