#!/usr/bin/env python3
"""Fail if docs/installation.md's dependency table drifts from pyproject.toml.

The table under "### Direct runtime dependencies" documents package names,
minimum versions and upper bounds by hand (the Purpose column is prose worth
writing by hand); this gate keeps the hand-written half honest against
``[project] dependencies``. Both directions are drift: a dependency missing
from the table, a table row with no matching dependency, a stale floor and a
stale bound.

The same invocation checks every dependency's upper bound against ``uv.lock``
(#1287): the bound is the next version that may break, counted from the locked
version (below the next major from 1.0, below the next minor in 0.x), written
as ``>=floor,<bound``, and a bound may be tighter than that, never looser.
docs/contributing.md, "Dependency updates", states the rule.

Every entry is read once, by ``parse_dependencies`` with ``packaging``, and
every mode below reads from that. Names are compared canonically
(``packaging.utils.canonicalize_name``), so ``Foo_Bar`` is ``foo-bar``.

Usage: python scripts/check_docs_dependency_table.py [pyproject.toml docs/installation.md]
       python scripts/check_docs_dependency_table.py --floor NAME [pyproject.toml]
       python scripts/check_docs_dependency_table.py --constraints [pyproject.toml]

``--floor NAME`` prints one dependency's declared minimum version and checks
nothing else; an unknown name is an error on stderr with nothing on stdout.

``--constraints`` prints one ``name==floor`` line per direct dependency: the
constraints ``nox -s wheel_smoke_floors`` resolves otto's runtime under, so
every direct dependency is installed at exactly its declared floor, and a
floor no resolution can install (another dependency needs more) fails the
resolution by name (#1181).
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import tomli
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

FLOOR_USAGE = "usage: check_docs_dependency_table.py --floor NAME [pyproject.toml]"
CONSTRAINTS_USAGE = "usage: check_docs_dependency_table.py --constraints [pyproject.toml]"
MAX_FLOOR_ARGS = 2
USAGE_ERROR = 2
SECTION_HEADING = "### Direct runtime dependencies"
BOUND_COLUMN = "Below"
BOUND_CELL = 3  # name, floor, bound: the table's first three cells
OTHER_UPPER_OPERATORS = frozenset({"~=", "==", "<=", "==="})


@dataclass(frozen=True)
class Dependency:
    """One ``[project] dependencies`` entry: its name, floor and upper bound as written."""

    name: str
    floor: str
    bound: str | None
    other_upper: str | None
    requirement: Requirement


@dataclass(frozen=True)
class TableRow:
    """One row of the docs table; ``bound`` is None when the table has no bound column."""

    name: str
    floor: str
    bound: str | None


def parse_dependencies(text: str) -> dict[str, Dependency]:
    """Map each direct runtime dependency (canonical name) to its parsed entry."""
    dependencies: dict[str, Dependency] = {}
    for spec in tomli.loads(text)["project"]["dependencies"]:
        requirement = Requirement(spec)
        floors = [s.version for s in requirement.specifier if s.operator == ">="]
        if not floors:
            raise SystemExit(f"cannot parse dependency floor from {spec!r}")
        bounds = [s.version for s in requirement.specifier if s.operator == "<"]
        others = [str(s) for s in requirement.specifier if s.operator in OTHER_UPPER_OPERATORS]
        dependencies[canonicalize_name(requirement.name)] = Dependency(
            name=requirement.name,
            floor=floors[0],
            bound=min(bounds, key=Version) if bounds else None,
            other_upper=others[0] if others else None,
            requirement=requirement,
        )
    return dependencies


def parse_locked(text: str) -> dict[str, list[Version]]:
    """Map each package (canonical name) in ``uv.lock`` to every version locked for it."""
    locked: dict[str, list[Version]] = {}
    for package in tomli.loads(text).get("package", []):
        if "version" in package:
            name = canonicalize_name(package["name"])
            locked.setdefault(name, []).append(Version(package["version"]))
    return locked


def bound_limit(version: Version) -> Version:
    """Return the loosest allowed upper bound: the next major from 1.0, else the next minor."""
    if version.major >= 1:
        return Version(str(version.major + 1))
    return Version(f"0.{version.minor + 1}")


def bound_problems(
    dependencies: dict[str, Dependency], locked: dict[str, list[Version]]
) -> list[str]:
    """One line per dependency whose upper bound is missing, loose, or excludes its lock."""
    problems: list[str] = []
    for key, dep in sorted(dependencies.items()):
        versions = locked.get(key)
        if not versions:
            problems.append(f"{dep.name}: not in uv.lock, so its upper bound cannot be checked")
            continue
        if dep.bound is None:
            limit = bound_limit(max(versions))
            if dep.other_upper is not None:
                problems.append(
                    f"{dep.name}: upper bound written as {dep.other_upper}; "
                    f"write the upper bound as `<X` (here `<{limit}`)"
                )
            else:
                problems.append(
                    f"{dep.name}: no upper bound ({dep.requirement.specifier}); add <{limit}"
                )
            continue
        for version in sorted(versions):
            limit = bound_limit(version)
            if Version(dep.bound) > limit:
                problems.append(
                    f"{dep.name}: upper bound <{dep.bound} is looser than the rule allows "
                    f"for locked {version} (<{limit})"
                )
            if not dep.requirement.specifier.contains(version, prereleases=True):
                problems.append(
                    f"{dep.name}: locked {version} is outside the declared range "
                    f"{dep.requirement.specifier}"
                )
    return problems


def _cells(line: str) -> list[str]:
    """Split a markdown table line into its stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_table(text: str) -> dict[str, TableRow]:
    """Map package (canonical name) -> row from the direct-dependencies docs table."""
    rows: dict[str, TableRow] = {}
    has_bound = False
    in_section = False
    for line in text.splitlines():
        if line.startswith("### "):
            in_section = line.strip() == SECTION_HEADING
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = _cells(line)
        if cells[0] == "Package":
            has_bound = BOUND_COLUMN in cells
            continue
        if len(cells) < BOUND_CELL or not (cells[0].startswith("`") and cells[0].endswith("`")):
            continue
        name = cells[0].strip("`")
        bound = cells[BOUND_CELL - 1] if has_bound and len(cells) >= BOUND_CELL else None
        rows[canonicalize_name(name)] = TableRow(name, cells[1], bound)
    return rows


def diff(dependencies: dict[str, Dependency], table: dict[str, TableRow]) -> list[str]:
    """Human-readable drift list; empty when the table matches pyproject."""
    problems = [
        f"{dependencies[key].name}: in pyproject.toml but missing from the docs table"
        for key in sorted(dependencies.keys() - table.keys())
    ]
    problems.extend(
        f"{table[key].name}: in the docs table but not a pyproject dependency"
        for key in sorted(table.keys() - dependencies.keys())
    )
    for key in sorted(dependencies.keys() & table.keys()):
        dep, row = dependencies[key], table[key]
        if dep.floor != row.floor:
            problems.append(
                f"{dep.name}: docs table says {row.floor}, pyproject.toml says {dep.floor}"
            )
        if row.bound is None:
            problems.append(
                f"{dep.name}: the docs table has no {BOUND_COLUMN!r} column for the upper bound"
            )
        elif dep.bound is not None and row.bound != dep.bound:
            problems.append(
                f"{dep.name}: docs table says bound {row.bound}, pyproject.toml says {dep.bound}"
            )
    return problems


def print_floor(name: str, pyproject_path: Path) -> int:
    """Print ``name``'s declared minimum version; return 1 when it is not a dependency."""
    dependencies = parse_dependencies(pyproject_path.read_text())
    dep = dependencies.get(canonicalize_name(name))
    if dep is None:
        print(f"{name}: not a direct dependency in {pyproject_path}", file=sys.stderr)
        return 1
    print(dep.floor)
    return 0


def floor_constraints(dependencies: dict[str, Dependency]) -> str:
    """Pin each direct dependency to its declared floor, one ``name==floor`` line each."""
    return "".join(f"{dep.name}=={dep.floor}\n" for dep in dependencies.values())


def main(argv: list[str]) -> int:
    """Print each drift line; return 1 when the docs table and pyproject disagree."""
    root = Path(__file__).resolve().parent.parent
    if argv[:1] == ["--floor"]:
        args = argv[1:]
        if not 1 <= len(args) <= MAX_FLOOR_ARGS:
            print(FLOOR_USAGE, file=sys.stderr)
            return USAGE_ERROR
        return print_floor(args[0], Path(args[1]) if args[1:] else root / "pyproject.toml")
    if argv[:1] == ["--constraints"]:
        args = argv[1:]
        if len(args) > 1:
            print(CONSTRAINTS_USAGE, file=sys.stderr)
            return USAGE_ERROR
        pyproject = Path(args[0]) if args else root / "pyproject.toml"
        print(floor_constraints(parse_dependencies(pyproject.read_text())), end="")
        return 0
    pyproject_path = Path(argv[0]) if argv else root / "pyproject.toml"
    docs_path = Path(argv[1]) if len(argv) > 1 else root / "docs" / "installation.md"
    dependencies = parse_dependencies(pyproject_path.read_text())
    table = parse_table(docs_path.read_text())
    problems = diff(dependencies, table)
    if not table:
        problems.append(f"no rows found under {SECTION_HEADING!r} in {docs_path}")
    lock_path = pyproject_path.parent / "uv.lock"
    if lock_path.is_file():
        problems.extend(bound_problems(dependencies, parse_locked(lock_path.read_text())))
    else:
        problems.append(f"{lock_path} not found, so upper bounds cannot be checked")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
