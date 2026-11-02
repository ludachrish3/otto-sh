#!/usr/bin/env python3
"""Fail if docs/installation.md's dependency table drifts from pyproject.toml.

The table under "### Direct runtime dependencies" documents package names and
minimum versions by hand (the Purpose column is prose worth writing by hand);
this gate keeps the hand-written half honest against
``[project] dependencies``. Both directions are drift: a dependency missing
from the table, a table row with no matching dependency, and a stale floor.

Usage: python scripts/check_docs_dependency_table.py [pyproject.toml docs/installation.md]
       python scripts/check_docs_dependency_table.py --floor NAME [pyproject.toml]

``--floor NAME`` prints one dependency's declared minimum version and checks
nothing else; an unknown name is an error on stderr with nothing on stdout.
"""

import re
import sys
from pathlib import Path

import tomli

REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][0-9A-Za-z.]*)")
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|")
FLOOR_USAGE = "usage: check_docs_dependency_table.py --floor NAME [pyproject.toml]"
MAX_FLOOR_ARGS = 2
USAGE_ERROR = 2
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


def print_floor(name: str, pyproject_path: Path) -> int:
    """Print ``name``'s declared minimum version; return 1 when it is not a dependency."""
    floors = parse_pyproject(pyproject_path.read_text())
    if name not in floors:
        print(f"{name}: not a direct dependency in {pyproject_path}", file=sys.stderr)
        return 1
    print(floors[name])
    return 0


def main(argv: list[str]) -> int:
    """Print each drift line; return 1 when the docs table and pyproject disagree."""
    root = Path(__file__).resolve().parent.parent
    if argv[:1] == ["--floor"]:
        args = argv[1:]
        if not 1 <= len(args) <= MAX_FLOOR_ARGS:
            print(FLOOR_USAGE, file=sys.stderr)
            return USAGE_ERROR
        return print_floor(args[0], Path(args[1]) if args[1:] else root / "pyproject.toml")
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
