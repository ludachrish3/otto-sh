"""otto's own code imports a name from the module that defines it, never through a package facade.

A package facade serves its public names from a lazy table (``_LAZY_ATTRS`` or
``_LAZY_EXPORTS``): ``from otto.inventory import build_inventory`` asks the
package's ``__getattr__``, which imports ``otto.inventory.config``. That
spelling is for users. Inside otto it hides the real dependency: tach maps an
import to the module it names, so ``from otto.host import CommandResult`` reads
as an edge to ``otto.host`` while the class lives in ``otto.result``, and the
declared graph, the layers and the cycle ratchet would all judge the wrong
edge. Spec 2026-10-04 public-api-manifest §9 set the rule, and spec 2026-10-11
placement-and-phases §3.10 carried it out across ``src/otto``.

``scripts/facade_sweep.py`` finds the sites (its docstring says which imports
count and which pass) and re-points them; this test fails on any it finds. A
site is a ``from <facade> import <name>``, or the same name read as an
attribute of an imported facade package (``import otto.inventory as
inventory`` then ``inventory.build_inventory``), which the script lists but
leaves to be re-pointed by hand. ``tests/unit/scripts/test_facade_sweep.py``
tests the finder and the rewrite on toy trees.

For the ``otto.lab`` facade, ``tests/unit/test_moved_name_spellings.py`` holds
a broader clause: otto's own code may not import it at all, nor name it in a
lazy table, so a ``from otto.lab import`` fails both tests.
"""

import pytest

from scripts.facade_sweep import FIX_COMMANDS, find_sites, lazy_bindings, owner
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SRC_ROOT = PROJECT_ROOT / "src"

FIX = " && ".join(FIX_COMMANDS)


def test_the_scan_sees_the_facades() -> None:
    facades = lazy_bindings(SRC_ROOT)
    assert len(facades) >= 25, sorted(facades)
    assert facades["otto.inventory"]["build_inventory"] == [
        "otto.inventory.config",
        "build_inventory",
    ]
    assert facades["otto.logger"]["management"] is None
    assert owner(facades, "otto", "app") == ["otto.cli.main", "app"]


def test_otto_imports_no_name_through_a_facade() -> None:
    found = [site.describe() for site in find_sites(SRC_ROOT)]
    assert not found, (
        "otto's own code imports these names through a package facade; import each from "
        "the module that defines it. To re-point every from-import, run the line below; "
        f"re-point a read marked 'by hand' yourself:\n  {FIX}\n  " + "\n  ".join(found)
    )
