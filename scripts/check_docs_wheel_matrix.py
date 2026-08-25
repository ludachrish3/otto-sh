#!/usr/bin/env python3
"""Fail if docs/installation.md's air-gap wheel claims drift from uv.lock.

``check_docs_dependency_table.py`` keeps the *direct runtime dependency* table
honest against ``pyproject.toml``. This gate answers the harder, air-gap
question that table cannot: for every non-pure runtime dependency, does a
wheel actually exist for every Python otto claims to support?

It reads three committed sources of truth and cross-checks four claims:

1. The download recipe's Python list covers exactly the minors declared in
   ``[project] classifiers`` -- a recipe that skips 3.12 builds a bundle that
   cannot install on 3.12.
2. Every package in the runtime closure that ships binary wheels appears in
   the "Native-extension dependencies" table, and nothing else does.
3. Each such row's "Wheel matrix" cell matches what the lock's wheel filenames
   actually say (``abi3`` spans versions; ``per-version`` does not).
4. Each such package really has an installable wheel for every supported minor
   on the documented Linux target.

Claim 4 is the one that matters inside the gap: a missing wheel there does not
surface until ``pip install --no-index`` runs on the isolated host, long after
the media has crossed. Dev dependencies are deliberately out of scope -- they
never enter the wheel bundle.

Usage: python scripts/check_docs_wheel_matrix.py [uv.lock pyproject.toml docs/installation.md]
"""

import re
import sys
from pathlib import Path

import tomli

ROOT_PACKAGE = "otto-sh"
SECTION_HEADING = "### Native-extension dependencies"

#: The air-gap recipe's documented default target. The gate checks this one
#: matrix rather than every platform: it is the target the docs actually tell
#: operators to download for, and a gap here is the gap that strands a lab.
TARGET_ARCH = "x86_64"

PURE = "pure"
ABI3 = "abi3"
PER_VERSION = "per-version"
PER_VERSION_PURE_FALLBACK = "per-version + pure fallback"

# `| `pkg` | pulled in by | wheel matrix | notes |` -- the backticked first
# cell is what distinguishes a data row from the header and separator rows.
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
RECIPE_LOOP = re.compile(r"for\s+PYVER\s+in\s+([0-9.\s]+);")
CLASSIFIER = re.compile(r"^Programming Language :: Python :: (\d+)\.(\d+)$")
CPYTHON_TAG = re.compile(r"^cp(\d)(\d+)$")


class Tag:
    """One expanded wheel tag triple (interpreter, ABI, platform)."""

    __slots__ = ("abi", "interpreter", "platform")

    def __init__(self, interpreter: str, abi: str, platform: str) -> None:
        self.interpreter = interpreter
        self.abi = abi
        self.platform = platform

    @property
    def is_pure(self) -> bool:
        """True when this tag names a platform-independent, ABI-free wheel."""
        return self.abi == "none" and self.platform == "any"


def wheel_filename(wheel: dict) -> str:
    """Return the bare ``.whl`` filename for one uv.lock wheel entry."""
    return (wheel.get("url") or wheel.get("path") or "").rsplit("/", maxsplit=1)[-1]


def wheel_tags(filename: str) -> list[Tag]:
    """Expand a wheel filename's compressed tag set into individual tags.

    Parsed by hand rather than via ``packaging.utils.parse_wheel_filename``:
    the last three hyphen-separated fields are the tag sets regardless of how
    the project name or an optional build number is spelled, so splitting from
    the right cannot be tripped by an exotic name, and the gate keeps working
    if the resolver ever locks a distribution packaging refuses to parse.
    """
    if not filename.endswith(".whl"):
        return []
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:  # noqa: PLR2004 -- name, version, and three tag fields
        return []
    interpreters, abis, platforms = parts[-3], parts[-2], parts[-1]
    return [
        Tag(interpreter, abi, platform)
        for interpreter in interpreters.split(".")
        for abi in abis.split(".")
        for platform in platforms.split(".")
    ]


def classify(wheels: list[dict]) -> str:
    """Describe the wheel matrix a package ships: pure, abi3, or per-version."""
    tags = [tag for wheel in wheels for tag in wheel_tags(wheel_filename(wheel))]
    binary = [tag for tag in tags if not tag.is_pure]
    if not binary:
        return PURE
    if any(tag.abi == "abi3" for tag in binary):
        return ABI3
    return PER_VERSION_PURE_FALLBACK if len(binary) != len(tags) else PER_VERSION


def runtime_closure(lock: dict) -> dict[str, list[dict]]:
    """Map every transitively-reachable runtime package to its lock entries.

    Walks ``dependencies`` only. ``dev-dependencies`` are excluded by
    construction: they are not installed by ``pip install otto-sh`` and never
    belong in an air-gap bundle, so letting them reach the docs table would
    make the gate demand rows for packages operators never download.
    """
    entries: dict[str, list[dict]] = {}
    for package in lock["package"]:
        entries.setdefault(package["name"], []).append(package)

    reached: dict[str, list[dict]] = {}
    queue = [dep["name"] for dep in entries.get(ROOT_PACKAGE, [{}])[0].get("dependencies", [])]
    while queue:
        name = queue.pop()
        if name in reached or name not in entries:
            continue
        reached[name] = entries[name]
        for entry in entries[name]:
            queue.extend(dep["name"] for dep in entry.get("dependencies", []))
    return reached


def supported_minors(pyproject_text: str) -> list[tuple[int, int]]:
    """Return the Python minors declared in ``[project] classifiers``."""
    minors = []
    for classifier in tomli.loads(pyproject_text)["project"].get("classifiers", []):
        match = CLASSIFIER.match(classifier)
        if match:
            minors.append((int(match.group(1)), int(match.group(2))))
    return sorted(minors)


def parse_native_table(docs_text: str) -> dict[str, str]:
    """Map package -> declared wheel matrix from the native-extension table."""
    rows: dict[str, str] = {}
    in_section = False
    for line in docs_text.splitlines():
        if line.startswith("#"):
            in_section = line.strip() == SECTION_HEADING
            continue
        if not in_section:
            continue
        match = ROW.match(line)
        if match:
            rows[match.group(1)] = match.group(3)
    return rows


def parse_download_minors(docs_text: str) -> list[tuple[int, int]]:
    """Return the Python minors the air-gap download recipe loops over."""
    match = RECIPE_LOOP.search(docs_text)
    if match is None:
        return []
    minors = []
    for token in match.group(1).split():
        major, _, minor = token.partition(".")
        minors.append((int(major), int(minor)))
    return sorted(minors)


def _tag_supports(tag: Tag, minor: tuple[int, int], arch: str) -> bool:
    """Return True when one wheel tag installs on CPython ``minor`` for ``arch``."""
    if tag.is_pure:
        return True
    if arch not in tag.platform or "linux" not in tag.platform:
        return False
    match = CPYTHON_TAG.match(tag.interpreter)
    if match is None:  # pypy / graalpy / py3-tagged binary wheels
        return False
    wheel_version = (int(match.group(1)), int(match.group(2)))
    if tag.abi == "abi3":
        # Stable ABI is forward-compatible only: cp39-abi3 runs on 3.9+.
        return wheel_version <= minor
    # An exact-ABI wheel matches its own minor. `cp314t` (free-threaded) is a
    # distinct ABI and deliberately does NOT satisfy a stock interpreter.
    return tag.abi == tag.interpreter and wheel_version == minor


def wheel_gaps(
    name: str, wheels: list[dict], minors: list[tuple[int, int]], arch: str
) -> list[str]:
    """Report each supported minor with no installable wheel for ``arch``."""
    tags = [tag for wheel in wheels for tag in wheel_tags(wheel_filename(wheel))]
    return [
        f"{name}: no wheel for Python {major}.{minor} on {arch} "
        f"-- an air-gap bundle built for that interpreter would be incomplete"
        for major, minor in minors
        if not any(_tag_supports(tag, (major, minor), arch) for tag in tags)
    ]


def audit(lock: dict, pyproject_text: str, docs_text: str) -> list[str]:
    """Return every drift between the docs' air-gap claims and the lock."""
    problems: list[str] = []
    minors = supported_minors(pyproject_text)
    recipe = parse_download_minors(docs_text)
    table = parse_native_table(docs_text)

    problems.extend(
        f"Python {major}.{minor} is a supported classifier but the air-gap "
        f"download recipe never downloads for it"
        for major, minor in minors
        if (major, minor) not in recipe
    )
    problems.extend(
        f"the air-gap download recipe downloads for Python {major}.{minor}, "
        f"which pyproject.toml does not declare as supported"
        for major, minor in recipe
        if (major, minor) not in minors
    )

    closure = runtime_closure(lock)
    actual = {}
    for name, entries in closure.items():
        wheels = [wheel for entry in entries for wheel in entry.get("wheels", [])]
        matrix = classify(wheels)
        if matrix != PURE:
            actual[name] = matrix

    problems.extend(
        f"{name}: ships binary wheels but is missing from {SECTION_HEADING!r}"
        for name in sorted(actual.keys() - table.keys())
    )
    problems.extend(
        f"{name}: listed under {SECTION_HEADING!r} but is not a binary-wheel runtime dependency"
        for name in sorted(table.keys() - actual.keys())
    )
    problems.extend(
        f"{name}: docs table says {table[name]}, uv.lock says {actual[name]}"
        for name in sorted(actual.keys() & table.keys())
        if table[name] != actual[name]
    )

    for name in sorted(actual):
        wheels = [wheel for entry in closure[name] for wheel in entry.get("wheels", [])]
        problems.extend(wheel_gaps(name, wheels, minors, TARGET_ARCH))
    return problems


def main(argv: list[str]) -> int:
    """Print each drift line; return 1 when the docs and the lock disagree."""
    root = Path(__file__).resolve().parent.parent
    lock_path = Path(argv[0]) if argv else root / "uv.lock"
    pyproject_path = Path(argv[1]) if len(argv) > 1 else root / "pyproject.toml"
    docs_path = Path(argv[2]) if len(argv) > 2 else root / "docs" / "installation.md"  # noqa: PLR2004

    docs_text = docs_path.read_text()
    problems = audit(tomli.loads(lock_path.read_text()), pyproject_path.read_text(), docs_text)
    if not parse_native_table(docs_text):
        problems.append(f"no rows found under {SECTION_HEADING!r} in {docs_path}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
