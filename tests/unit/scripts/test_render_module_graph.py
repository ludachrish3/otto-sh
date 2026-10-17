"""The module dependencies page: rendered from the ``[[modules]]`` entries of ``tach.toml``."""

import re
from pathlib import Path

import pytest

from scripts import render_module_graph as rmg
from tests._fixtures.docs_conf import assert_conf_renders
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


def _graph(tmp_path: Path, body: str) -> rmg.ModuleGraph:
    path = tmp_path / "tach.toml"
    path.write_text(body)
    return rmg.load_graph(path)


# a <-> b is a pair; c joins them into a three-module cycle; d hangs below
# the cycle by two routes, one of them redundant; e depends on nothing.
CYCLE = """
[[modules]]
path = "a"
depends_on = ["b", { path = "d" }]

[[modules]]
path = "b"
depends_on = ["a", "c"]

[[modules]]
path = "c"
depends_on = ["a", "d"]

[[modules]]
path = "d"
depends_on = ["e"]

[[modules]]
path = "top"
depends_on = ["a", "e"]
"""


def test_both_dependency_spellings_are_edges_and_a_target_without_an_entry_is_a_node(
    tmp_path,
) -> None:
    graph = _graph(tmp_path, CYCLE)
    assert graph.depends_on["a"] == ["b", "d"]
    assert graph.depends_on["e"] == [], "e has no entry of its own but is still drawn"
    assert graph.used_by("a") == ["b", "c", "top"]
    assert graph.edge_count() == 9


def test_the_cycle_is_found_whole_and_the_pair_inside_it_separately(tmp_path) -> None:
    graph = _graph(tmp_path, CYCLE)
    assert rmg.cycles(graph) == [["a", "b", "c"]]
    assert rmg.mutual_pairs(graph) == [("a", "b")], "c -> a, but a never declares c"


def test_cycle_edges_keep_only_the_edges_that_stay_inside_one_cycle(tmp_path) -> None:
    # A second cycle, x <-> y, that also reaches down into the first: x -> a
    # joins two cycles, so it is neither cycle's internal edge.
    graph = _graph(
        tmp_path,
        CYCLE
        + '[[modules]]\npath = "x"\ndepends_on = ["y", "a"]\n'
        + '[[modules]]\npath = "y"\ndepends_on = ["x", "e"]\n',
    )
    assert rmg.cycles(graph) == [["a", "b", "c"], ["x", "y"]]
    assert rmg.cycle_edges(graph) == {
        "a": ["b"],
        "b": ["a", "c"],
        "c": ["a"],
        "x": ["y"],
        "y": ["x"],
    }


def test_the_condensed_graph_drops_the_edges_another_route_already_covers(tmp_path) -> None:
    graph = _graph(tmp_path, CYCLE)
    cycle = rmg.cycle_label(1, ["a", "b", "c"])
    assert rmg.condensed(graph) == {
        cycle: ["d"],
        "d": ["e"],
        "e": [],
        # top -> e is implied by top -> cycle -> d -> e.
        "top": [cycle],
    }


def test_the_overview_caption_counts_the_left_out_arrows_and_names_one(tmp_path) -> None:
    graph = _graph(tmp_path, CYCLE)
    cycle = rmg.cycle_label(1, ["a", "b", "c"])
    assert rmg.omitted_arrow(graph) == rmg.OmittedArrow("top", "e", cycle)
    page = rmg.render(graph)
    assert "**Not every import is drawn.**" in page
    # cycle -> d, d -> e, top -> cycle drawn; top -> e left out.
    assert "3 of the 4 arrows between" in page
    assert f"`top` → `e` is not drawn,\nbecause `top` → `{cycle}` is" in page
    assert "[the table of every module](#every-module)" in page


def test_a_graph_with_nothing_left_out_names_no_example(tmp_path) -> None:
    graph = _graph(tmp_path, '[[modules]]\npath = "a"\ndepends_on = ["b"]\n')
    assert rmg.omitted_arrow(graph) is None
    page = rmg.render(graph)
    assert "1 of the 1 arrows between" in page
    assert "For example" not in page


def test_an_acyclic_graph_says_whether_tach_forbids_a_cycle(tmp_path) -> None:
    body = '[[modules]]\npath = "a"\ndepends_on = ["b"]\n'
    graph = _graph(tmp_path, body)
    page = rmg.render(graph)
    assert rmg.cycles(graph) == []
    assert not graph.forbids_cycles
    assert "does not set `forbid_circular_dependencies = true`, so" in page
    assert "would not refuse an import that closed one" in page
    assert "## Modules that import each other" not in page
    forbidding = _graph(tmp_path, "forbid_circular_dependencies = true\n" + body)
    assert forbidding.forbids_cycles
    page = rmg.render(forbidding)
    assert "sets `forbid_circular_dependencies = true`, so `tach check`" in page
    assert "does not set" not in page


def test_the_page_draws_both_graphs_and_tables_every_module(tmp_path) -> None:
    graph = _graph(tmp_path, CYCLE)
    page = rmg.render(graph)
    assert page.startswith("# Module dependencies")
    assert page.count("```{graphviz}") == 2
    assert '"cycle 1 (3 modules)" [style="bold,rounded"];' in page
    assert '"a" -- "b";' in page
    # a->b, b->a, b->c, c->a; a->d and c->d leave the cycle.
    assert "4 of the 9 dependencies run between two modules of" in page
    assert "| `a` | `b`, `d` | `b`, `c`, `top` |" in page
    assert "| `top` | `a`, `e` | — |" in page


def test_the_docs_build_renders_the_page_and_fails_when_the_renderer_does() -> None:
    assert_conf_renders("scripts.render_module_graph", "docs/architecture/modules.md")


def test_the_page_is_build_output_listed_in_the_overview_and_rebuilt_on_a_tach_edit() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docs/architecture/modules.md" in [line.strip() for line in ignored]
    assert rmg.PAGE_PATH.relative_to(PROJECT_ROOT).as_posix() == "docs/architecture/modules.md"
    index = (PROJECT_ROOT / "docs" / "architecture" / "index.rst").read_text(encoding="utf-8")
    entries = [line.strip() for line in index.splitlines()]
    overview = entries[
        entries.index(":caption: Overview") : entries.index(":caption: Design by area")
    ]
    assert "modules" in overview
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    assert "tach.toml" in srcs.group(0)
    assert "scripts/render_module_graph.py" in srcs.group(0)


def test_the_real_tach_toml_renders() -> None:
    page = rmg.render()
    assert f"{len(rmg.load_graph().modules)} modules" in page
    assert "`otto.errors`" in page


LAYERED = """
layers_explicit_depends_on = true
layers = ["top", "bottom"]

[[modules]]
path = "high"
layer = "top"
depends_on = ["low"]

[[modules]]
path = "low"
layer = "bottom"
depends_on = []

[[modules]]
path = "peer"
layer = "bottom"
depends_on = ["low"]

[[modules]]
path = "free"
depends_on = ["high"]
"""


def test_a_module_without_depends_on_is_refused(tmp_path) -> None:
    # tach reads a missing list as "may import anything"; drawing it as a leaf would lie.
    with pytest.raises(ValueError, match="'b' has no depends_on"):
        _graph(tmp_path, '[[modules]]\npath = "a"\ndepends_on = ["b"]\n\n[[modules]]\npath = "b"\n')


def test_a_module_without_a_path_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="entry 2 has no path"):
        _graph(tmp_path, '[[modules]]\npath = "a"\ndepends_on = []\n\n[[modules]]\ndepends_on = []')


def test_a_module_in_an_undefined_layer_is_refused(tmp_path) -> None:
    # tach refuses an undefined layer name as a configuration error; so does the reader.
    body = LAYERED.replace('path = "free"\n', 'path = "free"\nlayer = "middle"\n')
    with pytest.raises(ValueError, match=r"tach\.toml: module 'free' is in layer 'middle'"):
        _graph(tmp_path, body)


def test_the_layers_and_the_switch_are_read(tmp_path) -> None:
    graph = _graph(tmp_path, LAYERED)
    assert graph.layers == ["top", "bottom"]
    assert graph.layer_of == {"high": "top", "low": "bottom", "peer": "bottom"}
    assert graph.explicit_layer_dependencies


def test_only_an_edge_into_a_higher_layer_is_upward(tmp_path) -> None:
    graph = _graph(tmp_path, LAYERED)
    assert graph.is_upward("low", "high")
    assert not graph.is_upward("high", "low")
    assert not graph.is_upward("peer", "low"), "two modules of one layer may import each other"
    assert not graph.is_upward("low", "peer"), "two modules of one layer may import each other"
    assert not graph.is_upward("free", "high"), "a module with no layer is exempt"
    assert not graph.is_upward("low", "free"), "a module with no layer is exempt"


def test_a_config_without_layers_has_none(tmp_path) -> None:
    graph = _graph(tmp_path, CYCLE)
    assert graph.layers == []
    assert graph.layer_of == {}
    assert not graph.explicit_layer_dependencies
    assert rmg.layer_guide(graph) == []
    assert "## Where a new module goes" not in rmg.render(graph)


def test_the_layer_guide_describes_exactly_the_declared_layers(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"LAYER_GUIDE describes .* but tach\.toml's layers"):
        rmg.layer_guide(_graph(tmp_path, LAYERED))


def test_the_real_page_says_where_a_new_module_goes() -> None:
    graph = rmg.load_graph()
    guide = rmg.layer_guide(graph)
    rows = [line for line in guide if line.startswith("| `")]
    assert [row.split(" | ")[0] for row in rows] == [f"| `{layer}`" for layer in graph.layers]
    root = next(row for row in rows if row.startswith("| `root`"))
    assert "`otto.bootstrap`" in root.split(" | ")[-1]
    foundation = rows[-1].split(" | ")
    assert foundation[2] == "itself only", "the lowest layer may import only itself"
    assert rows[0].split(" | ")[2] == "itself and every layer below"
    assert "## Where a new module goes" in rmg.render(graph)


def test_module_of_names_the_module_a_file_defines() -> None:
    assert rmg.module_of("otto/labs/loader.py") == "otto.labs.loader"
    assert rmg.module_of("otto/lab/__init__.py") == "otto.lab"
    assert rmg.module_of("otto/__init__.py") == "otto"


def test_declared_owner_is_the_longest_declared_prefix() -> None:
    declared = ["otto.config", "otto.config.scope", "otto.utils"]
    assert rmg.declared_owner("otto.config.scope.x", declared) == "otto.config.scope"
    # The longer prefix listed first as well, so neither a first-match nor a last-match passes.
    longer_first = ["otto.config.scope", "otto.config"]
    assert rmg.declared_owner("otto.config.scope.x", longer_first) == "otto.config.scope"
    assert rmg.declared_owner("otto.config.settings", declared) == "otto.config"
    assert rmg.declared_owner("otto.utils", declared) == "otto.utils"
    assert rmg.declared_owner("otto.utilsx", declared) is None, "a prefix must end at a dot"
    assert rmg.declared_owner("otto.declared", declared) is None
