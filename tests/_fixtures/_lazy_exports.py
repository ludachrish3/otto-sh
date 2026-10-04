"""The lazy-export leak check the root conftest runs after every test.

Every otto package exports its public names lazily (PEP 562): ``__getattr__``
resolves ``pkg.name`` from the package's lazy table (``_LAZY_ATTRS`` or
``_LAZY_EXPORTS``), and the package's own ``__dict__`` holds none of them.
``monkeypatch.setattr("otto.pkg.name", fake)`` breaks that on UNDO. It records
the lazily resolved real object as the old value, and the undo ``setattr``s it
back, so the real object is now a plain global in the package ``__dict__``.
From then on, every ``from otto.pkg import name`` in the process binds that
cached object without consulting ``__getattr__``, and a later test that
patches the DEFINING module (``otto.pkg.module.name``) silently patches
nothing the code under test sees. The failure lands on that later, innocent
test, and only when xdist happens to put both on one worker.
(``unittest.mock.patch`` does not leak: it ``delattr``s on exit.)

The fix at a call site is to patch where the name is defined, which is what
the package's lazy table names, following hops through other lazy packages.
This module is the guard that keeps it that way. A module- or session-scoped
``MonkeyPatch`` of a lazily exported name would trip the guard at the first
test's teardown while the patch is still live, so such patches must target
the defining module too. The rule covers ``unittest.mock.patch`` as well:
it does not leak, but inside its block it can shadow a defining-module
monkeypatch, so the suite patches where a name is defined everywhere.

Scope: every lazy package, whichever table it declares. ``_LAZY_ATTRS`` maps
a name to the module that defines it. ``_LAZY_EXPORTS`` (``otto``,
``otto.config``, ``otto.logger``) maps it to either a ``(module, attr)`` pair
or, in ``otto.logger``, to the submodule that the name is. A package's own
eager globals (``otto.config``'s ``load_otto_env``) are in no table, so they
are never leaks.

See also: "Patching lazily exported names" in ``docs/contributing.md``, the
contributor rules this guard and ``tests/unit/test_patch_targets.py`` enforce.
"""

import functools
import importlib
import sys
from pathlib import Path
from types import ModuleType

_TABLES = ("_LAZY_ATTRS", "_LAZY_EXPORTS")


class LeakedLazyExportError(RuntimeError):
    """A test left a lazily exported name cached in its package's ``__dict__``."""


@functools.cache
def lazy_package_names() -> list[str]:
    """Every otto package whose ``__init__`` declares a lazy table, read once per process.

    Read from the source tree rather than from ``sys.modules``, so a package
    imported for the first time mid-session is still checked.
    """
    import otto

    root = Path(otto.__file__).parent
    return [
        ".".join(["otto", *init.parent.relative_to(root).parts])
        for init in sorted(root.rglob("__init__.py"))
        if any(table in init.read_text(encoding="utf-8") for table in _TABLES)
    ]


def _lazy_target(module: ModuleType, name: str) -> tuple[str, str] | None:
    """``(module, attr)`` *module*'s own lazy tables resolve *name* to, else ``None``.

    ``_LAZY_ATTRS`` values are the defining module (the attribute keeps its
    name). ``_LAZY_EXPORTS`` values are a ``(module, attr)`` pair or, in
    ``otto.logger``, a bare submodule name.
    """
    for table in _TABLES:
        target = (getattr(module, table, None) or {}).get(name)
        if target is not None:
            return (target, name) if isinstance(target, str) else (target[0], target[1])
    return None


def _defining_module(package: ModuleType, name: str) -> str:
    """The module that really defines ``package.name``, following lazy hops.

    ``otto.get_lab`` is declared as ``otto.config.get_lab``, which is itself
    lazy and defined in ``otto.config.fleet``; patching the intermediate
    package would trip the guard again, so the chain is followed until the
    target module no longer declares the attribute lazily.
    """
    target = _lazy_target(package, name)
    assert target is not None, f"{package.__name__}.{name} is not lazy"
    module_name, attr = target
    seen = {module_name}
    packages = set(lazy_package_names())
    while module_name in packages:
        hop = _lazy_target(importlib.import_module(module_name), attr)
        if hop is None or hop[0] in seen:
            break
        module_name, attr = hop
        seen.add(module_name)
    return f"{module_name}.{attr}"


@functools.cache
def lazy_export_map() -> dict[str, str]:
    """``pkg.name -> defining.module.attr`` for every lazily exported name, chains followed.

    A name that is itself a submodule (``otto.logger.management``) is not an
    export to patch and is left out. Only lazy packages are imported to read
    their tables; the defining modules are never imported.
    """
    exports = {}
    for package in lazy_package_names():
        module = importlib.import_module(package)
        for table in _TABLES:
            for name in getattr(module, table, None) or {}:
                defining = _defining_module(module, name)
                if defining.rpartition(".")[0] != f"{package}.{name}":
                    exports[f"{package}.{name}"] = defining
    return exports


def leaked_lazy_exports() -> list[str]:
    """``pkg.name`` for each lazily exported name now sitting in its package's ``__dict__``.

    A submodule is skipped: importing ``pkg.sub`` binds ``sub`` on the package
    legitimately, should a lazy name ever share a submodule's name. Costs one
    ``sys.modules`` lookup and one set intersection per lazy package.
    """
    leaked = []
    for package in lazy_package_names():
        module = sys.modules.get(package)
        if module is None:
            continue
        lazy = {name for table in _TABLES for name in getattr(module, table, None) or {}}
        namespace = vars(module)
        leaked.extend(
            f"{package}.{name}"
            for name in sorted(namespace.keys() & lazy)
            if not isinstance(namespace[name], ModuleType)
        )
    return leaked


def evict_leaked_lazy_exports() -> list[str]:
    """Take every leaked lazy export back out of its package; return ``pkg.name -> patch ...``.

    Afterwards every package's ``__getattr__`` is back in charge, so the leak
    cannot cascade onto the next test in this worker. Each returned line names
    the module the leaking patch should have targeted.
    """
    defining = []
    for dotted in leaked_lazy_exports():
        package, _, name = dotted.rpartition(".")
        module = sys.modules[package]
        defining.append(f"{dotted} -> patch {_defining_module(module, name)}")
        vars(module).pop(name, None)
    return defining


def raise_on_leaked_lazy_exports(nodeid: str) -> None:
    """Fail *nodeid*'s teardown if it left a lazy export cached, after evicting it."""
    defining = evict_leaked_lazy_exports()
    if not defining:
        return
    raise LeakedLazyExportError(
        f"{nodeid}: left lazily exported name(s) cached in the package __dict__, which "
        "shadows the package's __getattr__ for every later test in this worker. "
        "monkeypatch.setattr on a lazy package does this on undo; patch the defining "
        "module instead:\n  " + "\n  ".join(defining)
    )
