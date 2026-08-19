#!/usr/bin/env python3
"""Fail if docs/installation.md's dependency table drifts from pyproject.toml.

The table under "### Direct runtime dependencies" documents package names and
minimum versions by hand (the Purpose column is prose worth writing by hand);
this gate keeps the hand-written half honest against
``[project] dependencies``. Both directions are drift: a dependency missing
from the table, a table row with no matching dependency, and a stale floor.

Usage: python scripts/check_docs_dependency_table.py [pyproject.toml docs/installation.md]
"""

import re
import sys
from pathlib import Path

import tomli

REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][0-9A-Za-z.]*)")
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|")
SECTION_HEADING = "### Direct runtime dependencies"


def parse_pyproject(text: str) -> dict[str, str]:
    """Map each direct runtime dependency to its declared minimum version."""
    floors: dict[str, str] = {}
    for spec in tomli.loads(text)["project"]["dependencies"]:
        match = REQ.match(spec)
        if match is None:
            raise SystemExit(f"cannot parse dependency floor from {spec!r}")
        floors[match.group(1)] = match.group(2)
    return floors


def parse_table(text: str) -> dict[str, str]:
    """Map package -> min version from the direct-dependencies docs table."""
    rows: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if line.startswith("### "):
            in_section = line.strip() == SECTION_HEADING
            continue
        if not in_section:
            continue
        match = ROW.match(line)
        if match and match.group(2) not in {"Min version", "-----------"}:
            rows[match.group(1)] = match.group(2)
    return rows


def diff(pyproject: dict[str, str], table: dict[str, str]) -> list[str]:
    """Human-readable drift list; empty when the table matches pyproject."""
    problems = [
        f"{name}: in pyproject.toml but missing from the docs table"
        for name in sorted(pyproject.keys() - table.keys())
    ]
    problems.extend(
        f"{name}: in the docs table but not a pyproject dependency"
        for name in sorted(table.keys() - pyproject.keys())
    )
    problems.extend(
        f"{name}: docs table says {table[name]}, pyproject.toml says {pyproject[name]}"
        for name in sorted(pyproject.keys() & table.keys())
        if pyproject[name] != table[name]
    )
    return problems


def main(argv: list[str]) -> int:
    """Print each drift line; return 1 when the docs table and pyproject disagree."""
    root = Path(__file__).resolve().parent.parent
    pyproject_path = Path(argv[0]) if argv else root / "pyproject.toml"
    docs_path = Path(argv[1]) if len(argv) > 1 else root / "docs" / "installation.md"
    problems = diff(parse_pyproject(pyproject_path.read_text()), parse_table(docs_path.read_text()))
    if not parse_table(docs_path.read_text()):
        problems.append(f"no rows found under {SECTION_HEADING!r} in {docs_path}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
