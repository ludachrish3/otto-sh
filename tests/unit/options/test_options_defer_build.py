"""Every ``@options`` class defers its pydantic build, and every one still builds.

Mirrors ``tests/unit/models/test_defer_build.py``: that file walks
``OttoModel`` subclasses via ``__subclasses__()``; a pydantic *dataclass* has
no such common base to recurse from, so this walks the AST of every module
under ``src/otto`` (and the in-tree repo fixtures' importable ``pylib``
packages) looking for a class decorated with ``@options`` or
``@options(...)``, by tracking the local name(s) a module's imports bind to
``options`` — the same alias-tracking approach
``tests/unit/test_error_base.py`` uses for exception bases.

``otto.params.options`` passes ``defer_build=True`` into pydantic's config by
default, so a class's schema and validator build on first use rather than at
import — the same laziness ``OttoModel`` gives ``BaseModel`` subclasses.
"""

import ast
import importlib
import sys
from pathlib import Path

import pydantic.dataclasses

from tests._fixtures.paths import PROJECT_ROOT

_SRC_OTTO = PROJECT_ROOT / "src" / "otto"

# In-tree repo fixtures whose pylib/ otto imports cleanly (no lab hardware
# needed at import time). A repo whose pylib turns out NOT importable in a
# plain unit test process is skipped entirely -- "if they're importable in
# unit tests" -- rather than failing this sweep for an unrelated reason.
_REPO_PYLIBS = [PROJECT_ROOT / "tests" / f"repo{n}" / "pylib" for n in (1, 2, 3, 4)]


def _options_aliases(tree: ast.Module) -> set[str]:
    """Local names this module's imports bind to something named ``options``.

    Matched by imported NAME, not by source module: otto's ``options`` is
    re-exported from several places (``otto``, ``otto.params``, a relative
    ``from .. import options``), and this sweep does not need to resolve
    which -- only one callable in this tree is ever imported under that name.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "options":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _dotted_module_name(rel: Path, *, package_prefix: str) -> str:
    parts = rel.parts[:-1] if rel.name == "__init__.py" else (*rel.parts[:-1], rel.stem)
    if package_prefix:
        return ".".join((package_prefix, *parts)) if parts else package_prefix
    return ".".join(parts)


def _find_options_classes(root: Path, *, package_prefix: str) -> list[tuple[str, str]]:
    """``[(dotted module name, class name)]`` for every ``@options`` class under *root*."""
    found: list[tuple[str, str]] = []
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root)
        if "_webassets" in rel.parts or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        aliases = _options_aliases(tree)
        if not aliases:
            continue
        dotted = _dotted_module_name(rel, package_prefix=package_prefix)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id in aliases:
                    found.append((dotted, node.name))
                    break
    return found


def _load_options_classes() -> dict[tuple[str, str], type]:
    """Import every ``@options`` class found in ``src/otto`` and importable repo pylibs."""
    classes: dict[tuple[str, str], type] = {}
    for dotted, name in _find_options_classes(_SRC_OTTO, package_prefix="otto"):
        module = importlib.import_module(dotted)
        classes[(dotted, name)] = getattr(module, name)

    for pylib in _REPO_PYLIBS:
        if not pylib.is_dir():
            continue
        found = _find_options_classes(pylib, package_prefix="")
        if not found:
            continue
        added = str(pylib) not in sys.path
        if added:
            sys.path.insert(0, str(pylib))
        try:
            for dotted, name in found:
                try:
                    module = importlib.import_module(dotted)
                except ImportError:
                    # Not importable in a plain unit-test process (e.g. a
                    # lab-hardware dependency); the repo's classes are skipped,
                    # not the whole sweep.
                    continue
                classes[(dotted, name)] = getattr(module, name)
        finally:
            if added:
                sys.path.remove(str(pylib))
    return classes


def test_every_options_class_defers_its_build():
    classes = _load_options_classes()
    assert classes, "the AST sweep found no @options classes -- it may be broken"
    assert ("otto.project.options", "InstallOptions") in classes
    assert ("otto.examples.options", "RepoOptions") in classes
    for (dotted, name), cls in classes.items():
        assert pydantic.dataclasses.is_pydantic_dataclass(cls), f"{dotted}.{name}"
        assert cls.__pydantic_config__.get("defer_build") is True, f"{dotted}.{name}"


def test_every_options_class_builds():
    classes = _load_options_classes()
    for (dotted, name), cls in classes.items():
        pydantic.dataclasses.rebuild_dataclass(cls, force=True)
        assert cls.__pydantic_complete__, f"{dotted}.{name}"
