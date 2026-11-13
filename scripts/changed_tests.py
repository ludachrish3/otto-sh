#!/usr/bin/env python3
"""The test files a diff adds or modifies, under the roots it is asked about.

A pull request's CI repeats these files so that a test that fails only
sometimes is caught on the pull request that introduces it (the
``tests_changed_repeat_*`` nox sessions; docs/contributing.md, "The
changed-test repeat"). Selection is by FILE: a test module the diff touches is repeated
whole, and the lane's marker expression decides which of its tests run.

The diff is the gate-scope oracle's (``gate_scope.changes_between``): a rename
is a delete plus an add, so a renamed file is listed under its new path and a
deleted one is never listed.

Usage::

    uv run python scripts/changed_tests.py --range HEAD^1..HEAD tests/unit

prints one path per line, and nothing when the diff holds no test file under
the roots. A range git cannot read is an error, never an empty answer: a
checkout too shallow to hold the parent must not pass for "nothing to repeat".
"""

import argparse
import fnmatch
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.gate_scope import Change, GateScopeError, changes_between  # noqa: E402 -- path set up above

TEST_MODULE = "test_*.py"
"""The name every test module in this repo has (pytest also collects ``*_test.py``; none exist)."""

DELETED = "D"


def changed_test_files(changes: "list[Change]", roots: "list[str]") -> "list[str]":
    """Return the sorted test modules *changes* add or modify under any of *roots*."""
    root_paths = [PurePosixPath(root) for root in roots]
    selected = {
        change.path
        for change in changes
        if change.status != DELETED
        and fnmatch.fnmatchcase(PurePosixPath(change.path).name, TEST_MODULE)
        and any(root in PurePosixPath(change.path).parents for root in root_paths)
    }
    return sorted(selected)


def main(argv: "list[str] | None" = None) -> int:
    """Print the changed test files under the given roots; 2 when the range cannot be read."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the checkout to read")
    parser.add_argument("--range", dest="rev_range", required=True, help="e.g. HEAD^1..HEAD")
    parser.add_argument("roots", nargs="+", help="directories to look under, e.g. tests/unit")
    args = parser.parse_args(argv)
    try:
        changes = changes_between(args.repo, args.rev_range)
    except GateScopeError as exc:
        print(f"changed-tests: cannot read {args.rev_range}: {exc}", file=sys.stderr)
        return 2
    for path in changed_test_files(changes, args.roots):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
