"""Keep otto's own modules through pytester's ``sys.modules`` restores.

pytester snapshots ``sys.modules`` when the fixture is built and before every
in-process run (``runpytest_inprocess``/``inline_run``), and puts the snapshot
back afterwards: every module imported in between is dropped. That exists to
forget the test files a pytester test writes and imports. It also drops any
``otto.*`` module that happened to be imported for the FIRST time inside the
inner run, and that is where it goes wrong.

Dropping a submodule from ``sys.modules`` does not unbind it from its parent
package. The import system bound ``otto.monitor.factory`` as an attribute of
``otto.monitor`` when it loaded it, and that attribute survives the restore. The
process is left with one module in two states: the package attribute holds the
old module object, and ``sys.modules`` has no entry. The next
``from otto.monitor.factory import build_monitor_collector`` imports a SECOND
copy of the module. Any ``mock.patch("otto.monitor.factory.X")`` made in between
lands on the first copy. On Python 3.10 ``mock.patch`` resolves its target by
``getattr`` from the package down, so it finds the orphan attribute. Code under
test that imports the name at call time then gets the real function from the
fresh copy, and the patch never takes effect.

Lazy package exports (PEP 562 ``__getattr__`` tables) are what make this
likely. When a package imported every submodule eagerly, those submodules were
loaded long before any pytester snapshot and the restore always kept them. With
lazy exports, the first import of a submodule can happen anywhere, including
inside an inner pytester session.

The fix preserves every ``otto`` and ``otto.*`` module through the restore.
That matches what a real process does: a product module is imported once and
stays. It also matches the root conftest's registry restore, which never evicts
an ``otto.*`` origin. The generated test modules pytester exists to forget are
never named ``otto.*``, so they are still dropped.

``_pytest.pytester.SysModulesSnapshot`` is private. This module subclasses it
at import, so if pytest renames the class, the root conftest fails to import
instead of the fix going quiet.
``tests/unit/test_pytester_module_snapshot.py`` pins the behaviour.
"""

from collections.abc import Callable

import _pytest.pytester as _pytester


def is_product_module(name: str) -> bool:
    """True for ``otto`` itself and every module below it."""
    return name == "otto" or name.startswith("otto.")


class ProductPreservingSnapshot(_pytester.SysModulesSnapshot):
    """pytester's snapshot, with every otto module added to what it preserves."""

    def __init__(self, preserve: Callable[[str], bool] | None = None) -> None:
        def _keep(name: str) -> bool:
            return is_product_module(name) or (preserve is not None and preserve(name))

        super().__init__(preserve=_keep)


def install() -> None:
    """Make every pytester snapshot in this process preserve otto's modules. Idempotent."""
    _pytester.SysModulesSnapshot = ProductPreservingSnapshot
