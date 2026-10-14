"""tach's layers: every module takes its layer as it leaves the cycle, and no file escapes tach.

``tach.toml`` declares otto's ten role layers (spec 2026-10-11 placement-and-phases
§2) and sets ``layers_explicit_depends_on = true``: an import between two modules
passes only when the importer's ``depends_on`` lists it, downward imports included.
``scripts/tach_layer_canary.py`` (a ``make lint-arch`` and ``nox -s lint`` leg)
proves the installed tach behaves that way, and ``scripts/render_module_graph.py``
refuses a module with no ``depends_on``, which tach would read as "may import
anything". What tach cannot check, this file does:

* the switch is on, and the layers are the spec's ten, highest first;
* every module outside an import cycle has a layer. A module in the cycle has
  none yet, which exempts it from the layer check; it takes its layer in the
  commit that frees it (spec §5.2), and one left without would be exempt for good;
* no module in an import cycle has a layer yet. tach tolerates one while its
  peers in the cycle are unlayered, and nothing else would notice it;
* no ``.py`` file under ``src/otto`` is outside every tach module except the ones
  ``UNOWNED`` lists, and that list only shrinks: ``UNOWNED_WHEN_LANDED`` pins it
  as it landed, and an entry added past it fails. tach sees no import made by a
  file no module owns, so a new one would escape every rule.
"""

import collections

import pytest

from scripts import render_module_graph as rmg
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

# Highest first, the order tach reads them (spec 2026-10-11 placement-and-phases §2).
LAYERS = [
    "entry",
    "root",
    "workflows",
    "run",
    "composition",
    "sources",
    "topology",
    "specs",
    "host",
    "foundation",
]

# Files no tach module owns yet. Each is declared by the landing that frees it
# (spec 2026-10-11 placement-and-phases §5.1). Shrink-only.
UNOWNED = [
    "otto/__init__.py",
    "otto/__main__.py",
    "otto/_shim.py",
    "otto/env/__init__.py",
    "otto/env/backends.py",
    "otto/env/manage.py",
    "otto/env/preflight.py",
    "otto/lab/__init__.py",
]

# UNOWNED as this test landed it. UNOWNED may lose entries but never gain one,
# so a new file declares its tach module instead of joining the list.
UNOWNED_WHEN_LANDED = [
    "otto/__init__.py",
    "otto/__main__.py",
    "otto/_shim.py",
    "otto/declared.py",
    "otto/env/__init__.py",
    "otto/env/backends.py",
    "otto/env/manage.py",
    "otto/env/preflight.py",
    "otto/lab/__init__.py",
]


@pytest.fixture(scope="module")
def graph() -> rmg.ModuleGraph:
    return rmg.load_graph()


def unowned_files(modules: "list[str]") -> "list[str]":
    """Every ``.py`` file under ``src/otto`` that no module in *modules* owns."""
    src = PROJECT_ROOT / "src"
    found = []
    for file in sorted((src / "otto").rglob("*.py")):
        rel = file.relative_to(src).as_posix()
        if rmg.declared_owner(rmg.module_of(rel), modules) is None:
            found.append(rel)
    return found


def test_a_downward_import_must_be_declared_too(graph) -> None:
    assert graph.explicit_layer_dependencies, (
        "tach.toml must set layers_explicit_depends_on = true: without it tach passes "
        "any import into a lower layer undeclared, and --exact reports the declared "
        "ones as unused."
    )


def test_the_layers_are_the_specs_ten_highest_first(graph) -> None:
    assert graph.layers == LAYERS


def test_every_module_outside_the_cycle_has_a_layer(graph) -> None:
    in_cycle = {module for members in rmg.cycles(graph) for module in members}
    missing = sorted(m for m in graph.modules if m not in in_cycle and m not in graph.layer_of)
    assert not missing, (
        f"{missing} are in no import cycle but have no layer, so tach checks no layer "
        "rule for them. Give each its layer in tach.toml in this commit (spec "
        "2026-10-11 placement-and-phases §2.2 names it)."
    )


def layered_cycle_members(graph: rmg.ModuleGraph) -> "list[str]":
    """The modules in an import cycle that already declare a layer."""
    return sorted(m for members in rmg.cycles(graph) for m in members if m in graph.layer_of)


def test_no_module_in_the_cycle_has_a_layer(graph) -> None:
    early = layered_cycle_members(graph)
    assert not early, (
        f"{early} are in an import cycle but have a layer. A module takes its layer "
        "in the commit that frees it (spec 2026-10-11 placement-and-phases §5.2); "
        "remove the layer until then."
    )


def test_a_layer_inside_the_cycle_is_named() -> None:
    ring = {"a": ["b"], "b": ["a"], "c": ["a"]}
    assert layered_cycle_members(rmg.ModuleGraph(ring, ["top"], {"c": "top"})) == []
    assert layered_cycle_members(rmg.ModuleGraph(ring, ["top"], {"a": "top"})) == ["a"]


def test_no_file_escapes_tach_beyond_the_unowned_list(graph) -> None:
    found = unowned_files(graph.modules)
    new = sorted(set(found) - set(UNOWNED))
    assert not new, (
        f"{new} are in no tach module, so tach checks none of their imports. "
        "Declare a module for them in tach.toml."
    )
    owned = sorted(set(UNOWNED) - set(found))
    assert not owned, f"{owned} are owned now — good. Delete them from UNOWNED in this commit."


def unowned_additions(current: "list[str]", landed: "list[str]") -> "list[str]":
    """The entries of *current* beyond *landed*, counting repeats."""
    return sorted((collections.Counter(current) - collections.Counter(landed)).elements())


def test_the_unowned_list_only_shrinks() -> None:
    added = unowned_additions(UNOWNED, UNOWNED_WHEN_LANDED)
    assert not added, (
        f"{added} joined UNOWNED, which only shrinks: declare a tach module that owns "
        "them in tach.toml instead (spec 2026-10-11 placement-and-phases §5.1)."
    )


def test_an_unowned_entry_past_the_pin_is_named() -> None:
    landed = ["otto/a.py", "otto/b.py"]
    assert unowned_additions(["otto/a.py"], landed) == [], "an entry may leave"
    assert unowned_additions([*landed, "otto/c.py"], landed) == ["otto/c.py"]
    assert unowned_additions([*landed, "otto/a.py"], landed) == ["otto/a.py"]
