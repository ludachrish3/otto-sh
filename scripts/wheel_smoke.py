"""Smoke an INSTALLED otto: the one a user gets, not the dev checkout.

Run by ``nox -s wheel_smoke`` and ``nox -s wheel_smoke_floors`` (``make
wheel-smoke``) with the interpreter of a venv that holds the built wheel and
otto's runtime dependencies only (locked, or at their floors): no dev
group, no editable install, no source tree on ``sys.path``. Every other lane
runs otto from the dev venv, where a development dependency (pytest-cov, #593)
or a plugin pytest auto-loads hides what an installed otto lacks.

It checks, running otto in a scratch directory outside the checkout:

1. the installed otto is the built wheel (:func:`check_identity`): its
   distribution reports the wheel's version, ``import otto`` loads it, and
   every file under ``otto/`` hashes as the wheel's own ``RECORD`` says;
2. the installed ``otto.libsonnet`` renders (:func:`check_library`) under the
   Jsonnet binding the platform's marker selects;
3. every ``otto`` module imports, and every third-party import statement in
   otto's source runs, a function-local one too (#1181);
4. the hostless CLI lists, runs (seeded, unseeded, in file order, and
   failing, with its exit code) and honors an async test and a
   ``@pytest.mark.timeout`` — with pytest's plugin autoload on, and off
   (``PYTEST_DISABLE_PLUGIN_AUTOLOAD``), which otto must not depend on.

``--installed-only`` runs the first two alone: ``nox -s packaging_offline``
asks them of an otto installed from an offline wheel directory.

The SUT is generated under a temporary directory; its lab names one host that
nothing contacts. Exits non-zero naming every check that failed.

Usage: python scripts/wheel_smoke.py WHEEL --binding {jsonnet,gojsonnet} [--installed-only]
"""

import argparse
import ast
import base64
import csv
import email.parser
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import os
import pkgutil
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
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


DISTRIBUTION = "otto-sh"
BINDING_CHOICES = ("jsonnet", "gojsonnet")

# Two lines that import the installed library and call into it. The name is
# one re.escape changes, so the render proves the library's code ran.
LIBRARY_PROBE_NAME = "smoke.lab-1"
LIBRARY_TEMPLATE = (
    "local o = import 'otto.libsonnet';\n{ escaped: o.escapeName('" + LIBRARY_PROBE_NAME + "') }\n"
)


@dataclass(frozen=True)
class BuiltWheel:
    """What a wheel says of itself: its version, and each ``otto/`` file's RECORD hash."""

    version: str
    hashes: "Mapping[str, str]"
    """``sha256=<digest>`` by RECORD path, for every file under ``otto/``."""


def read_wheel(wheel: Path) -> BuiltWheel:
    """Read *wheel*'s ``METADATA`` version and its ``RECORD`` entries under ``otto/``."""
    with zipfile.ZipFile(wheel) as archive:
        info = {
            name.split("/", 1)[1]: name
            for name in archive.namelist()
            if name.count("/") == 1 and name.split("/", 1)[0].endswith(".dist-info")
        }
        metadata = email.parser.Parser().parsestr(archive.read(info["METADATA"]).decode("utf-8"))
        record = archive.read(info["RECORD"]).decode("utf-8")
    hashes = {
        row[0]: row[1]
        for row in csv.reader(io.StringIO(record))
        if row and row[0].startswith("otto/")
    }
    return BuiltWheel(version=str(metadata["Version"]), hashes=hashes)


def record_hash(path: Path) -> str:
    """Spell *path*'s sha256 as a RECORD does: ``sha256=`` and the unpadded urlsafe base64."""
    digest = hashlib.sha256(path.read_bytes()).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def check_identity(wheel: Path) -> list[str]:
    """Return a failure line per way the installed otto is not *wheel*; empty when it is.

    The one installed-artifact identity check, for every session that installs
    a built wheel. The ``otto-sh`` distribution must report the wheel's
    version, ``import otto`` must load that distribution's files, and every
    file its RECORD lists under ``otto/`` must be in the wheel's RECORD with
    the same hash, and hash so on disk. The only entries the wheel's RECORD
    does not hold are the ``__pycache__`` files pip compiles at install, which
    it records with no hash; every wheel entry must be installed.
    """
    import otto

    built = read_wheel(wheel)
    dist = importlib.metadata.distribution(DISTRIBUTION)
    failures = [] if built.hashes else [f"{wheel.name}'s RECORD lists no file under otto/"]
    if dist.version != built.version:
        failures.append(
            f"{DISTRIBUTION} {dist.version} is installed, not the wheel's {built.version}"
        )
    installed_init = Path(str(dist.locate_file("otto/__init__.py"))).resolve()
    if Path(otto.__file__).resolve() != installed_init:
        failures.append(
            f"otto imported from {otto.__file__}, not {DISTRIBUTION}'s {installed_init}"
        )
    installed = {str(f): f for f in dist.files or () if str(f).startswith("otto/")}
    failures.extend(
        f"{path}: in the wheel's RECORD, not the installed one"
        for path in sorted(built.hashes.keys() - installed.keys())
    )
    for path, file in sorted(installed.items()):
        if file.hash is None:
            if not ("/__pycache__/" in path and path.endswith(".pyc")):
                failures.append(f"{path}: the installed RECORD lists it with no hash")
            continue
        want = built.hashes.get(path)
        if want is None:
            failures.append(f"{path}: installed, but not in the wheel's RECORD")
            continue
        recorded = f"{file.hash.mode}={file.hash.value}"
        try:
            on_disk = record_hash(Path(str(dist.locate_file(file))))
        except OSError as e:
            failures.append(f"{path}: {e}")
            continue
        if recorded != want or on_disk != want:
            failures.append(
                f"{path}: the wheel records {want}, the install {recorded}, the file is {on_disk}"
            )
    if not failures:
        hashed = sum(1 for file in installed.values() if file.hash is not None)
        print(
            f"ok   the installed {DISTRIBUTION} is {wheel.name}: {hashed} files under otto/ match"
        )
    return failures


def check_library(binding: str) -> list[str]:
    """Render :data:`LIBRARY_TEMPLATE` with the installed otto; a failure line per problem.

    The template, which imports the installed ``otto.libsonnet``, goes
    through otto's renderer, and must render under *binding* (``jsonnet`` or
    ``gojsonnet``), the binding the platform's marker installs, as
    :func:`~otto.templating.binding.load_binding` names it.
    """
    from otto.inventory.config import ProtectedPaths
    from otto.templating import TemplatingError, load_binding
    from otto.templating.imports import ImportPolicy
    from otto.templating.render import RenderRequest, render

    # A real catalog that protects nothing (the template imports only the
    # library, which the policy always serves); never NO_CATALOG, which renders nothing.
    policy = ImportPolicy(roots=(), protected=ProtectedPaths(files=frozenset()))
    with tempfile.TemporaryDirectory(prefix="otto-wheel-smoke-library-") as d:
        entry = Path(d) / "library.jsonnet"
        entry.write_text(LIBRARY_TEMPLATE, encoding="utf-8")
        try:
            text = render(RenderRequest(entry=entry, tla=None, policy=policy)).text
        except TemplatingError as e:
            return [f"rendering the installed otto.libsonnet under the {binding} binding: {e}"]
    found = load_binding().name
    if found != binding:
        return [f"expected the {binding} binding, but the render ran under {found}"]
    print(f"ok   load_binding().name == {found!r}")
    want = {"escaped": re.escape(LIBRARY_PROBE_NAME)}
    if json.loads(text) != want:
        return [f"the installed otto.libsonnet rendered {text.strip()}, not {json.dumps(want)}"]
    print(f"ok   the installed otto.libsonnet renders under {found}")
    return []


def installed_checks(wheel: Path, binding: str) -> list[str]:
    """Return the failures of :func:`check_identity`, then of :func:`check_library`."""
    return check_identity(wheel) + check_library(binding)


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke an installed otto.")
    parser.add_argument("wheel", type=Path, help="the built wheel the installed otto must be")
    parser.add_argument(
        "--binding",
        required=True,
        choices=BINDING_CHOICES,
        help="the Jsonnet binding this platform's marker installs",
    )
    parser.add_argument(
        "--installed-only",
        action="store_true",
        help="check only the install's identity and the library render",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    """Run every check; return the exit code (1 when any failed)."""
    args = _parser().parse_args(argv)
    failures = installed_checks(args.wheel, args.binding)
    if not args.installed_only:
        failures += _import_all()
        failures += _third_party_imports()
        with tempfile.TemporaryDirectory(prefix="otto-wheel-smoke-") as d:
            root = Path(d)
            failures += _cli(root, _sut(root))
    for failure in failures:
        print(f"\nFAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
