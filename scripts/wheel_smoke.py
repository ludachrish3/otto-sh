"""Smoke an INSTALLED otto: the one a user gets, not the dev checkout.

Run by ``nox -s wheel_smoke`` and ``nox -s wheel_smoke_floors`` (``make
wheel-smoke``) with the interpreter of a venv that holds the built wheel and
otto's runtime dependencies only (locked, or at their floors): no dev
group, no editable install, no source tree on ``sys.path``. Every other lane
runs otto from the dev venv, where a development dependency (pytest-cov, #593)
or a plugin pytest auto-loads hides what an installed otto lacks.

It checks, running otto in a scratch directory outside the checkout:

1. ``otto`` is imported from the installed wheel, not ``src/`` (an editable
   install, or ``src/`` on ``sys.path``);
2. every ``otto`` module imports, and every third-party import statement in
   otto's source runs, a function-local one too (#1181);
3. the hostless CLI lists, runs (seeded, unseeded, in file order, and
   failing, with its exit code) and honors an async test and a
   ``@pytest.mark.timeout`` — with pytest's plugin autoload on, and off
   (``PYTEST_DISABLE_PLUGIN_AUTOLOAD``), which otto must not depend on.

The SUT is generated under a temporary directory; its lab names one host that
nothing contacts. Exits non-zero naming every check that failed.
"""

import ast
import importlib
import importlib.util
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

SRC = Path(__file__).resolve().parent.parent / "src"

SUITE = """\
import asyncio
import time

import pytest


def test_sync():
    assert True


async def test_async():
    await asyncio.sleep(0)


@pytest.mark.timeout(1)
def test_times_out():
    time.sleep(30)


def test_fails():
    assert False
"""

# (argv, expected exit code, text the output must contain)
CHECKS: list[tuple[list[str], int, str]] = [
    (["--help"], 0, "Usage"),
    (["test", "--list-tests"], 0, "test_async"),
    (["-l", "smoke", "test", "test_sync", "test_async", "--no-cov"], 0, "2 passed"),
    (["-l", "smoke", "test", "test_sync", "--no-cov", "--seed", "7"], 0, "1 passed"),
    (["-l", "smoke", "test", "test_async", "--no-cov", "--no-random"], 0, "1 passed"),
    (["-l", "smoke", "test", "test_times_out", "--no-cov", "--no-random"], 1, "Timeout"),
    (["-l", "smoke", "test", "test_fails", "--no-cov"], 1, "1 failed"),
]


def _import_all() -> list[str]:
    """Import every otto module; return a failure line per one that does not import."""
    import otto

    if SRC in Path(otto.__file__).resolve().parents:
        return [f"otto imported from {SRC} ({otto.__file__}), not an installed wheel"]
    failures = []
    for info in pkgutil.walk_packages(otto.__path__, "otto."):
        if info.name == "otto.__main__":  # importing it runs the CLI
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001 — every failure is reported, none stops the walk
            failures.append(f"import {info.name}: {type(e).__name__}: {e}")
    return failures


_OPTIONAL = {"ImportError", "ModuleNotFoundError"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
"""Bodies that run later, outside a ``try`` around their definition (a class body runs in place)."""
_NOT_DEPENDENCIES = {"otto", "__future__"} | set(sys.stdlib_module_names)


def _gate(test: ast.expr) -> "bool | None":
    """Evaluate a ``sys.version_info`` gate on this interpreter; ``None`` for any other test."""
    text = ast.unparse(test)
    if "sys.version_info" not in text:
        return None
    try:
        return bool(eval(text, {"sys": sys}))  # noqa: S307 -- a version comparison from otto's source
    except Exception:  # noqa: BLE001 -- a gate that names anything else is not judged
        return None


class ImportLine(NamedTuple):
    """One third-party import statement otto's source runs."""

    line: int
    code: str
    module: str
    """The top-level module it imports."""
    in_try: bool
    """In the body of a ``try`` that handles ImportError: otto copes with it missing."""


def _handles_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(ast.unparse(t) in _OPTIONAL for t in types)


def _skipped(node: ast.AST, parents: "dict[ast.AST, ast.AST]") -> bool:
    """Say whether otto never runs import *node* here.

    That is: under ``if TYPE_CHECKING:``, or on the branch of a
    ``sys.version_info`` gate this interpreter does not take (``if`` body or
    ``else``).
    """
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.If):
            type_checking = ast.unparse(parent.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")
            if type_checking and child in parent.body:
                return True
            taken = _gate(parent.test)
            if taken is not None and (child in parent.body) != taken:
                return True
        child, parent = parent, parents.get(parent)
    return False


def _in_try(node: ast.AST, parents: "dict[ast.AST, ast.AST]") -> bool:
    """Say whether *node* runs inside a ``try`` body that handles ImportError.

    A ``try`` around a function definition does not count: the imports inside
    run when it is called, outside it. A class body runs where it stands, so
    a ``try`` around a class does.
    """
    child, parent = node, parents.get(node)
    while parent is not None and not isinstance(child, _SCOPES):
        if (
            isinstance(parent, ast.Try)
            and child in parent.body
            and any(_handles_import_error(h) for h in parent.handlers)
        ):
            return True
        child, parent = parent, parents.get(parent)
    return False


def third_party_statements(source: str) -> "list[ImportLine]":
    """Every third-party import statement in *source* that otto runs here.

    Module-level and function-local alike; one statement per imported module,
    so ``import os, requests`` yields ``import requests``. The standard
    library, otto itself, ``TYPE_CHECKING`` blocks and a version gate's
    untaken branch are left out.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or _skipped(node, parents):
            continue
        in_try = _in_try(node, parents)
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            module = node.module.split(".")[0]
            if module not in _NOT_DEPENDENCIES:
                found.append(ImportLine(node.lineno, ast.unparse(node), module, in_try))
            continue
        for alias in node.names:
            module = alias.name.split(".")[0]
            if module not in _NOT_DEPENDENCIES:
                code = ast.unparse(ast.Import(names=[alias]))
                found.append(ImportLine(node.lineno, code, module, in_try))
    return found


def _third_party_imports() -> "list[str]":
    """Run every third-party import statement in installed otto, deferred ones included.

    Importing every module (:func:`_import_all`) runs only the module-level
    imports. A function-local import of a runtime dependency (otto defers
    pysnmp, pynetbox and others to keep startup light) runs only on the code
    path that needs it, so a dependency floor that lacks a name otto imports
    there (pysnmp 7.1.0 has no ``get_cmd``, #1181) is invisible to it. This
    reads otto's own source and runs each statement
    :func:`third_party_statements` finds. One inside a ``try`` that handles
    ImportError is optional only when its module is not present at all
    (``importlib.util.find_spec``): an extra otto copes without (zstandard)
    is skipped, while a present module (uv, a declared dependency whose
    absence is a fatal install error) must import whatever its handler does.
    ``find_spec``, not distribution metadata: Python 3.10's
    ``packages_distributions`` reads only ``top_level.txt``, which the uv
    wheel lacks.
    """
    import otto

    failures, seen = [], set()
    root = Path(otto.__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        for found in third_party_statements(path.read_text(encoding="utf-8")):
            if found.code in seen or (
                found.in_try and importlib.util.find_spec(found.module) is None
            ):
                continue
            seen.add(found.code)
            try:
                exec(compile(found.code, str(path), "exec"), {})  # noqa: S102 -- otto's own import lines
            except Exception as e:  # noqa: BLE001 -- every failure is reported, none stops the walk
                where = f"{path.relative_to(root.parent)}:{found.line}"
                failures.append(f"{where}: {found.code}: {type(e).__name__}: {e}")
    return failures


def _sut(root: Path) -> Path:
    project = root / "sut"
    for d in (".otto", "tests", "labs"):
        (project / d).mkdir(parents=True)
    (project / ".otto" / "settings.toml").write_text(
        'name = "smoke"\nversion = "1.0.0"\ntests = ["tests"]\n'
        '[[lab.sources]]\nbackend = "json"\npaths = ["labs"]\n'
    )
    host = {
        "ip": "127.0.0.1",
        "os_type": "unix",
        "valid_terms": ["ssh"],
        "creds": [{"login": "nobody", "password": "unused"}],
    }
    lab = {"labs": {"smoke": {}}, "elements": [{"name": "h", "labs": ["smoke"], "hosts": [host]}]}
    (project / "labs" / "lab.json").write_text(json.dumps(lab))
    (project / "tests" / "test_smoke.py").write_text(SUITE)
    return project


def _cli(root: Path, project: Path) -> list[str]:
    """Run every CLI check with plugin autoload on and off; return a line per failure."""
    otto_bin = Path(sys.executable).parent / "otto"
    base = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith(("OTTO_", "PYTEST_", "COV")) or k == "OTTOPATH")
    }
    base.update(OTTO_HOME=str(root / "home"), OTTOPATH=str(project), NO_COLOR="1")
    failures = []
    for autoload, extra in (("on", {}), ("off", {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})):
        for argv, want, text in CHECKS:
            env = {**base, **extra, "OTTO_XDIR": str(root / "x")}
            r = subprocess.run(  # noqa: S603 — the installed otto, with fixed arguments
                [str(otto_bin), *argv],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            out = r.stdout + r.stderr
            ok = r.returncode == want and text in out
            label = f"autoload {autoload}: otto {' '.join(argv)}"
            print(f"{'ok  ' if ok else 'FAIL'} {label} (exit {r.returncode})")
            if not ok:
                failures.append(f"{label}: exit {r.returncode}, wanted {want} and {text!r}\n{out}")
    return failures


def main() -> int:
    """Run every check; return the exit code (1 when any failed)."""
    failures = _import_all()
    failures += _third_party_imports()
    with tempfile.TemporaryDirectory(prefix="otto-wheel-smoke-") as d:
        root = Path(d)
        failures += _cli(root, _sut(root))
    for failure in failures:
        print(f"\nFAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
