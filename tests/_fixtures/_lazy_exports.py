"""The lazy-export leak check the root conftest runs after every test.

Every otto package exports its public names lazily (PEP 562): ``__getattr__``
resolves ``pkg.name`` from the module ``_LAZY_ATTRS`` names, and the package's
own ``__dict__`` holds none of them. ``monkeypatch.setattr("otto.pkg.name", fake)``
breaks that on UNDO. It records the lazily resolved real object as the old
value, and the undo ``setattr``s it back, so the real object is now a plain
global in the package ``__dict__``. From then on, every ``from otto.pkg import
name`` in the process binds that cached object without consulting
``__getattr__``, and a later test that patches the DEFINING module
(``otto.pkg.module.name``) silently patches nothing the code under test sees.
The failure lands on that later, innocent test, and only when xdist happens
to put both on one worker. (``unittest.mock.patch`` does not leak: it
``delattr``s on exit.)

The fix at a call site is to patch where the name is defined, which is what
``_LAZY_ATTRS[name]`` names. This module is the guard that keeps it that way.
A module- or session-scoped ``MonkeyPatch`` of a lazily exported name would
trip the guard at the first test's teardown while the patch is still live,
so such patches must target the defining module too.

Scope: the packages that declare ``_LAZY_ATTRS``. The three that declare
``_LAZY_EXPORTS`` instead (``otto``, ``otto.config``, ``otto.logger``) have
the same hazard but are not checked yet: the suite monkeypatches
``otto.config.get_repos`` and its siblings on the package at about a hundred
sites, and moving those is its own change.
"""

import functools
import sys
from pathlib import Path
from types import ModuleType


class LeakedLazyExportError(RuntimeError):
    """A test left a lazily exported name cached in its package's ``__dict__``."""


@functools.cache
def lazy_package_names() -> list[str]:
    """Every otto package whose ``__init__`` declares ``_LAZY_ATTRS``, read once per process.

    Read from the source tree rather than from ``sys.modules``, so a package
    imported for the first time mid-session is still checked.
    """
    import otto

    root = Path(otto.__file__).parent
    return [
        ".".join(["otto", *init.parent.relative_to(root).parts])
        for init in sorted(root.rglob("__init__.py"))
        if "_LAZY_ATTRS" in init.read_text(encoding="utf-8")
    ]


def leaked_lazy_exports() -> list[str]:
    """``pkg.name`` for each lazily exported name now sitting in its package's ``__dict__``.

    A submodule is skipped: importing ``pkg.sub`` binds ``sub`` on the package
    legitimately, should a lazy name ever share a submodule's name. Costs one
    ``sys.modules`` lookup and one set intersection per lazy package.
    """
    leaked = []
    for package in lazy_package_names():
        module = sys.modules.get(package)
        lazy = getattr(module, "_LAZY_ATTRS", None)
        if not lazy:
            continue
        namespace = vars(module)
        leaked.extend(
            f"{package}.{name}"
            for name in sorted(namespace.keys() & lazy.keys())
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
        defining.append(f"{dotted} -> patch {module._LAZY_ATTRS[name]}.{name}")
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
