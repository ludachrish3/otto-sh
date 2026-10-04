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


def test_an_acyclic_graph_says_the_flag_can_be_turned_on(tmp_path) -> None:
    graph = _graph(tmp_path, '[[modules]]\npath = "a"\ndepends_on = ["b"]\n')
    page = rmg.render(graph)
    assert rmg.cycles(graph) == []
    assert "forbid_circular_dependencies = true` can be turned on" in page
    assert "## Modules that import each other" not in page


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
