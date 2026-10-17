"""tach's layers: every module has its layer, and no file escapes tach.

``tach.toml`` declares otto's ten role layers (spec 2026-10-11 placement-and-phases
§2) and sets ``layers_explicit_depends_on = true``: an import between two modules
passes only when the importer's ``depends_on`` lists it, downward imports included.
``scripts/tach_layer_canary.py`` (a ``make lint-arch`` and ``nox -s lint`` leg)
proves the installed tach behaves that way, and ``scripts/render_module_graph.py``
refuses a module with no ``depends_on``, which tach would read as "may import
anything". What tach cannot check, this file does:

* the switch is on, and the layers are the spec's ten, highest first;
* ``forbid_circular_dependencies`` is on: every file is in a module, so tach
  refuses an import that closes a cycle (spec 2026-10-11 placement-and-phases
  §5.1), and turning it off would let one through;
* every module outside an import cycle has a layer. The declared graph has no
  cycle (tach refuses one), so that is every module;
* no module in an import cycle has a layer. With no cycle this holds trivially;
  it stays so that a module that rejoined a cycle could not keep a layer tach
  would tolerate while its peers in the cycle were unlayered;
* every ``.py`` file under ``src/otto`` is in a tach module: tach sees no import
  made by a file no module owns, so one would escape every rule;
* a facade's module owns its facade file and nothing else (``FACADE_FILES``).
  The root package is a module, so a new top-level file is owned by it, and a
  new file in ``otto/lab/`` or ``otto/session/`` by that facade, unless it is
  declared: without this check it would sit in the entry layer, unnoticed.
"""

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

# Each facade module and the one file it owns (spec 2026-10-11 placement-and-phases
# §3.11): any other file it owns is a file no module was declared for.
FACADE_FILES = {
    "otto": "otto/__init__.py",
    "otto.context": "otto/context.py",
    "otto.lab": "otto/lab/__init__.py",
    "otto.session": "otto/session/__init__.py",
}


@pytest.fixture(scope="module")
def graph() -> rmg.ModuleGraph:
    return rmg.load_graph()


def owners(modules: "list[str]", files: "list[str]") -> "dict[str, str | None]":
    """Each file under ``src/`` in *files* -> the module in *modules* that owns it."""
    return {rel: rmg.declared_owner(rmg.module_of(rel), modules) for rel in files}


def _otto_files() -> "list[str]":
    src = PROJECT_ROOT / "src"
    return [file.relative_to(src).as_posix() for file in sorted((src / "otto").rglob("*.py"))]


def ownership_problems(owned: "dict[str, str | None]", facades: "dict[str, str]") -> "list[str]":
    """A file no module owns, and a file a facade's module owns besides its facade."""
    problems = [
        f"{rel} is in no tach module, so tach checks none of its imports"
        for rel, owner in owned.items()
        if owner is None
    ]
    problems += [
        f"{rel} is owned by the facade module {owner}, which owns {facades[owner]} alone"
        for rel, owner in owned.items()
        if owner in facades and rel != facades[owner]
    ]
    return problems


def test_a_downward_import_must_be_declared_too(graph) -> None:
    assert graph.explicit_layer_dependencies, (
        "tach.toml must set layers_explicit_depends_on = true: without it tach passes "
        "any import into a lower layer undeclared, and --exact reports the declared "
        "ones as unused."
    )


def test_tach_refuses_an_import_cycle(graph) -> None:
    assert graph.forbids_cycles, (
        "tach.toml must set forbid_circular_dependencies = true: every file is in "
        "a module, so tach can refuse an import that closes a cycle (spec "
        "2026-10-11 placement-and-phases §5.1)."
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


def test_every_file_is_in_its_own_module(graph) -> None:
    assert set(FACADE_FILES) <= set(graph.modules), "a facade module is not declared"
    problems = ownership_problems(owners(graph.modules, _otto_files()), FACADE_FILES)
    assert not problems, (
        "Declare a tach module for each file in tach.toml (spec 2026-10-11 "
        "placement-and-phases §5.1):\n  " + "\n  ".join(problems)
    )


def test_an_escaping_file_and_a_facade_s_stray_file_are_named() -> None:
    modules = ["otto", "otto.lab", "otto.fleet"]
    files = ["otto/__init__.py", "otto/lab/__init__.py", "otto/fleet.py", "otto/new.py"]
    facades = {"otto": "otto/__init__.py", "otto.lab": "otto/lab/__init__.py"}
    assert ownership_problems(owners(modules, files), facades) == [
        "otto/new.py is owned by the facade module otto, which owns otto/__init__.py alone"
    ]
    assert ownership_problems(owners(["otto.fleet"], ["otto/new.py"]), facades) == [
        "otto/new.py is in no tach module, so tach checks none of its imports"
    ]
    assert ownership_problems(owners(modules, files[:3]), facades) == []
