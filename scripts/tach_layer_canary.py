"""Prove the installed tach enforces the layer rules ``tach.toml`` relies on.

Run from the repo root (``make lint-arch`` and nox's ``lint`` session do)::

    uv run --group lint python scripts/tach_layer_canary.py

``tach.toml`` declares otto's role layers and sets
``layers_explicit_depends_on = true``, so that an import between two modules
passes only when the importer's ``depends_on`` lists it, downward imports
included. That behaviour is tach's, and the ``tach>=0.35.0`` floor floats: an
upgrade that changed it would leave ``tach check --exact`` green over a
contract it no longer enforces. This builds a two-module project in a
temporary directory and runs the installed tach on five variants, each with
the outcome the design relies on (spec 2026-10-11 placement-and-phases §1.1):

1. every import declared, switch on: passes, so the fixture itself is valid;
2. an undeclared downward import, switch on: refused;
3. the same, switch off: passes, so case 2's refusal is the switch's doing;
4. a declared upward import: refused as a layer violation;
5. a layered module with no ``depends_on`` key: its undeclared import passes,
   which is why ``scripts/render_module_graph.py`` refuses a module without one.
"""

import importlib.util
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Case:
    """One variant of the throwaway project and the outcome tach must give."""

    name: str
    switch: bool
    """``layers_explicit_depends_on``."""
    high_depends_on: "list[str] | None"
    """``pkg.high``'s ``depends_on``; ``None`` leaves the key out."""
    low_imports_high: bool
    """Whether ``pkg.low`` imports ``pkg.high`` (and declares it)."""
    refused_with: "str | None"
    """Text tach's output must contain, or ``None`` when tach must pass."""


CASES = [
    Case("all declared, switch on", True, ["pkg.low"], False, None),
    Case("undeclared downward, switch on", True, [], False, "cannot depend on 'pkg.low'"),
    Case("undeclared downward, switch off", False, [], False, None),
    Case("declared upward", True, ["pkg.low"], True, "is lower than layer"),
    Case("no depends_on key, switch on", True, None, False, None),
]


def _write_project(root: Path, case: Case) -> None:
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "high.py").write_text("import pkg.low\n")
    (pkg / "low.py").write_text("import pkg.high\n" if case.low_imports_high else "")
    high_deps = (
        "" if case.high_depends_on is None else f"depends_on = {case.high_depends_on!r}\n"
    ).replace("'", '"')
    low_deps = '["pkg.high"]' if case.low_imports_high else "[]"
    (root / "tach.toml").write_text(
        'source_roots = ["src"]\n'
        f"layers_explicit_depends_on = {'true' if case.switch else 'false'}\n"
        'layers = ["top", "bottom"]\n\n'
        f'[[modules]]\npath = "pkg.high"\nlayer = "top"\n{high_deps}\n'
        f'[[modules]]\npath = "pkg.low"\nlayer = "bottom"\ndepends_on = {low_deps}\n'
    )


def run_case(case: Case) -> "str | None":
    """Run tach on *case*'s project; return what went wrong, or ``None``."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_project(root, case)
        done = subprocess.run(
            [sys.executable, "-m", "tach", "check", "--exact"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    output = done.stdout + done.stderr
    if case.refused_with is None and done.returncode != 0:
        return f"tach refused it:\n{output}"
    if case.refused_with is not None:
        if done.returncode == 0:
            return "tach passed it"
        if case.refused_with not in output:
            return f"tach refused it, but without {case.refused_with!r}:\n{output}"
    return None


def main() -> int:
    """Run every case; exit 1 when tach's behaviour is not the one tach.toml relies on."""
    if importlib.util.find_spec("tach") is None:
        print(
            "tach layer canary: tach is not installed in this environment; it is in the "
            "lint dependency group, so run this via uv run --group lint.",
            file=sys.stderr,
        )
        return 1
    failures = [(case, problem) for case in CASES if (problem := run_case(case)) is not None]
    for case, problem in failures:
        print(f"tach layer canary: {case.name}: {problem}", file=sys.stderr)
    if failures:
        print(
            "The installed tach no longer behaves as tach.toml's layers assume; "
            "see scripts/tach_layer_canary.py. Cases 3 and 5 also fail if tach grows "
            "stricter, deliberately: tach.toml and its reader assume the behaviour "
            "they pin, so a stricter tach is reviewed before it is relied on.",
            file=sys.stderr,
        )
        return 1
    print(f"tach layer canary: {len(CASES)} cases behave as tach.toml assumes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
