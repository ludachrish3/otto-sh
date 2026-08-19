#!/usr/bin/env python3
"""Fail if a hardcoded otto version number appears in docs source.

Otto version numbers in docs are build-time substitutions: pages write the
%OTTO_VERSION% token and docs/conf.py's source-read hook replaces it with the
release version. A hand-written literal (``otto-sh==0.5.4``) is exactly how the
docs rotted before this gate existed, so each spelling that can carry one is
banned. A near-miss token (``%OTTO_VERSN%``) would render literally in built
HTML, so anything %OTTO_-shaped that is not exactly the token is banned too.

Prose mentions of historical versions ("shipped in v0.8.1") are deliberately
NOT matched: the patterns are scoped to install/download spellings.

Usage: python scripts/lint_docs_versions.py docs/
"""

import re
import sys
from pathlib import Path

TOKEN = "%OTTO_VERSION%"  # noqa: S105 — a text placeholder, not a credential
OFFENSES = [
    (re.compile(r"otto-sh==\d"), "hardcoded otto version pin (use %OTTO_VERSION%)"),
    (re.compile(r"otto_sh-\d"), "hardcoded otto wheel filename (use %OTTO_VERSION%)"),
    (re.compile(r"releases/download/v\d"), "hardcoded release URL (use %OTTO_VERSION%)"),
    # Lookbehind keeps prefixed names out: release_process.md documents
    # `make release NEW_VERSION=0.4.0rc1`, a deliberate interface example.
    (
        re.compile(r"(?<![A-Z_])VERSION=\d+\.\d+"),
        "hardcoded VERSION assignment (use %OTTO_VERSION%)",
    ),
]
NEAR_TOKEN = re.compile(r"%OTTO_[A-Z_]*%")
SKIP_PARTS = ("_build", "superpowers", "_inventories")


def lint_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, reason)`` offenses found in one docs source file."""
    offenses: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for pattern, reason in OFFENSES:
            if pattern.search(line):
                offenses.append((lineno, reason))
        offenses.extend(
            (lineno, f"malformed version token {match.group(0)!r}")
            for match in NEAR_TOKEN.finditer(line)
            if match.group(0) != TOKEN
        )
    return offenses


def main(argv: list[str]) -> int:
    """Lint the docs tree at ``argv[0]``; print offenses and return the exit status."""
    if len(argv) != 1:
        print("usage: lint_docs_versions.py <docs-dir>", file=sys.stderr)
        return 2
    failed = False
    for path in sorted(Path(argv[0]).rglob("*")):
        if path.suffix not in {".md", ".rst"}:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        for lineno, reason in lint_file(path):
            print(f"{path}:{lineno}: {reason}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
