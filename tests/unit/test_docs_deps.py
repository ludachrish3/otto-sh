"""Consistency check: the dependency table in the docs must match pyproject.toml.

``docs/getting-started.md`` carries a human-readable table of otto's direct
runtime dependencies and their minimum versions. That table is hand-written,
so it silently drifts whenever a dependency is added, removed, or has its
version floor raised in ``pyproject.toml``. This test fails the moment the two
diverge, with a message telling the developer exactly what to fix.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

REPO_ROOT = Path(__file__).parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS_TABLE = REPO_ROOT / "docs" / "getting-started.md"

# A dependency spec like ``aioftp>=0.27.2``: capture name and the >= floor.
_SPEC_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*>=\s*([0-9][0-9A-Za-z.+!-]*)\s*$")

# A docs table row like ``| `aioftp` | 0.27.2 | Async FTP... |``.
_ROW_RE = re.compile(r"^\|\s*`([A-Za-z0-9._-]+)`\s*\|\s*([0-9][0-9A-Za-z.+!-]*)\s*\|")


def _pyproject_min_versions() -> dict[str, str]:
    """Map each direct runtime dependency to its ``>=`` version floor."""
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]
    versions: dict[str, str] = {}
    for spec in deps:
        match = _SPEC_RE.match(spec)
        assert match is not None, (
            f"Dependency {spec!r} in pyproject.toml is not a simple "
            f"``name>=version`` pin. Update test_docs_deps.py if the "
            f"project intentionally adopts a different constraint style."
        )
        versions[match.group(1).lower()] = match.group(2)
    return versions


def _docs_table_versions() -> dict[str, str]:
    """Map each package in the getting-started.md dependency table to its version."""
    versions: dict[str, str] = {}
    for line in DOCS_TABLE.read_text().splitlines():
        match = _ROW_RE.match(line)
        if match is not None:
            versions[match.group(1).lower()] = match.group(2)
    return versions


def test_docs_dependency_table_matches_pyproject() -> None:
    """The docs dependency table must list exactly the runtime deps in pyproject.toml."""
    expected = _pyproject_min_versions()
    documented = _docs_table_versions()

    missing = sorted(expected.keys() - documented.keys())
    extra = sorted(documented.keys() - expected.keys())
    wrong = sorted(
        f"{name}: pyproject={expected[name]} docs={documented[name]}"
        for name in expected.keys() & documented.keys()
        if expected[name] != documented[name]
    )

    problems: list[str] = []
    if missing:
        problems.append(f"missing from docs table: {missing}")
    if extra:
        problems.append(f"in docs table but not a runtime dependency: {extra}")
    if wrong:
        problems.append(f"version mismatch: {wrong}")

    assert not problems, (
        "The dependency table in docs/getting-started.md is out of sync with "
        "[project].dependencies in pyproject.toml.\n  "
        + "\n  ".join(problems)
        + "\nUpdate the table so it lists every runtime dependency with its "
        "current >= version floor."
    )
