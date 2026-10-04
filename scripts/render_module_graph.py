"""Render ``tach.toml`` into ``docs/architecture/modules.md``.

Run from the repo root::

    uv run python -m scripts.render_module_graph           # write the page
    uv run python -m scripts.render_module_graph --check   # render, write nothing

``tach.toml`` is the single home for otto's module dependency contracts:
``tach check`` (``make lint-arch``) refuses an import between two modules that
the file does not declare. This page draws the declared graph. Drawn whole it
is unreadable, because most of the modules sit in one cycle, so the page draws
it three ways: the cycles condensed to one node each, the pairs inside a cycle
that import each other, and a table of every module's edges.
``docs/conf.py`` runs this at ``builder-inited`` for every builder; the page is
git-ignored, so the only copy in the tree is ``tach.toml`` itself.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import tomli

ROOT = Path(__file__).resolve().parent.parent

TACH_TOML = ROOT / "tach.toml"

PAGE_PATH = ROOT / "docs" / "architecture" / "modules.md"


@dataclass
class ModuleGraph:
    """The declared dependency graph: each module and the modules it may import."""

    depends_on: "dict[str, list[str]]"

    @property
    def modules(self) -> "list[str]":
        """Every module, in name order."""
        return sorted(self.depends_on)

    def used_by(self, module: str) -> "list[str]":
        """List the modules that declare *module* as a dependency, in name order."""
        return sorted(m for m, deps in self.depends_on.items() if module in deps)

    def edge_count(self) -> int:
        """How many declared edges the graph holds."""
        return sum(len(deps) for deps in self.depends_on.values())


def load_graph(path: Path = TACH_TOML) -> ModuleGraph:
    """Read the ``[[modules]]`` entries of the tach config at *path*.

    A dependency is spelled either as a bare module path or as a table with a
    ``path`` key; both name the same edge. A dependency on a module that has no
    entry of its own still becomes a node, so no declared edge is dropped.
    """
    doc = tomli.loads(path.read_text(encoding="utf-8"))
    graph: dict[str, list[str]] = {}
    for entry in doc.get("modules", []):
        deps = [dep if isinstance(dep, str) else dep["path"] for dep in entry.get("depends_on", [])]
        graph[entry["path"]] = sorted(set(deps) - {entry["path"]})
    for deps in list(graph.values()):
        for dep in deps:
            graph.setdefault(dep, [])
    return ModuleGraph(graph)


def cycles(graph: ModuleGraph) -> "list[list[str]]":
    """Find the strongly connected components of two or more modules, largest first.

    Every module in a component can reach every other through declared edges,
    which is exactly what ``forbid_circular_dependencies`` refuses.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def visit(root: str) -> None:
        # Iterative Tarjan: the graph is small, but recursion depth is not ours to spend.
        work = [(root, iter(graph.depends_on[root]))]
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is None:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
            elif child not in index:
                index[child] = low[child] = len(index)
                stack.append(child)
                on_stack.add(child)
                work.append((child, iter(graph.depends_on[child])))
            elif child in on_stack:
                low[node] = min(low[node], index[child])

    for module in graph.modules:
        if module not in index:
            visit(module)
    return sorted(found, key=lambda c: (-len(c), c))


def cycle_edges(graph: ModuleGraph) -> "dict[str, list[str]]":
    """Map each module in a cycle to the modules it depends on inside that same cycle.

    An edge from one cycle to another, or out to a module in no cycle, is left
    out: it points down the layering, which is where an edge should point.
    """
    owner = {member: number for number, members in enumerate(cycles(graph)) for member in members}
    return {
        module: [dep for dep in graph.depends_on[module] if owner.get(dep) == number]
        for module, number in sorted(owner.items())
    }


def cycle_label(number: int, members: "list[str]") -> str:
    """Name the node a condensed cycle is drawn as."""
    return f"cycle {number} ({len(members)} modules)"


def condensed(graph: ModuleGraph) -> "dict[str, list[str]]":
    """Collapse each cycle to one node and drop the edges another route covers.

    What is left is acyclic. An edge ``a -> c`` is dropped when ``a`` already
    reaches ``c`` another way, so every arrow drawn is one the layering needs.
    """
    owner = {m: m for m in graph.modules}
    for number, members in enumerate(cycles(graph), start=1):
        for member in members:
            owner[member] = cycle_label(number, members)
    edges: dict[str, set[str]] = {node: set() for node in owner.values()}
    for module, deps in graph.depends_on.items():
        for dep in deps:
            if owner[module] != owner[dep]:
                edges[owner[module]].add(owner[dep])

    def reachable(start: str, skip: str) -> "set[str]":
        seen: set[str] = set()
        todo = [n for n in edges[start] if n != skip]
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(edges[node])
        return seen

    return {
        node: sorted(dep for dep in targets if dep not in reachable(node, skip=dep))
        for node, targets in sorted(edges.items())
    }


def mutual_pairs(graph: ModuleGraph) -> "list[tuple[str, str]]":
    """Every pair of modules that each declare the other, in name order."""
    return [
        (a, b)
        for a in graph.modules
        for b in graph.depends_on[a]
        if a < b and a in graph.depends_on[b]
    ]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '\\"') + '"'


def overview_dot(graph: ModuleGraph) -> str:
    """DOT for the condensed graph: arrows point from a module to what it imports."""
    reduced = condensed(graph)
    lines = ["digraph modules {", "    rankdir=TB;", "    node [shape=box];"]
    for node in reduced:
        style = ' [style="bold,rounded"]' if node.startswith("cycle ") else ""
        lines.append(f"    {_quote(node)}{style};")
    for node, deps in reduced.items():
        lines += [f"    {_quote(node)} -> {_quote(dep)};" for dep in deps]
    lines.append("}")
    return "\n".join(lines)


def pairs_dot(graph: ModuleGraph) -> str:
    """DOT for the mutually-importing pairs: one double-headed edge per pair."""
    pairs = mutual_pairs(graph)
    lines = [
        "graph pairs {",
        "    layout=neato;",
        "    overlap=false;",
        "    splines=true;",
        "    node [shape=box];",
    ]
    lines += [f"    {_quote(node)};" for node in sorted({m for pair in pairs for m in pair})]
    lines += [f"    {_quote(a)} -- {_quote(b)};" for a, b in pairs]
    lines.append("}")
    return "\n".join(lines)


def _short(module: str) -> str:
    return f"`{module}`"


def _cycle_section(graph: ModuleGraph, found: "list[list[str]]") -> "list[str]":
    if not found:
        return [
            "No module is in a cycle: the condensed graph above is the whole graph,",
            "and `forbid_circular_dependencies = true` can be turned on in `tach.toml`.",
            "",
        ]
    lines = []
    for number, members in enumerate(found, start=1):
        lines += [
            f"**{cycle_label(number, members)}:** " + ", ".join(_short(m) for m in members) + ".",
            "",
        ]
    inside = sum(len(deps) for deps in cycle_edges(graph).values())
    lines += [
        f"{inside} of the {graph.edge_count()} dependencies run between two modules of",
        "one cycle. `tests/unit/test_import_cycle_ratchet.py` (run by `make lint-arch`)",
        "lists the modules in a cycle and the edges inside one, and neither list may",
        "grow: a module that joins a cycle, or a new edge inside one, fails the gate.",
        "",
    ]
    pairs = mutual_pairs(graph)
    lines += [
        "## Modules that import each other",
        "",
        f"{len(pairs)} pairs of modules each declare the other. A pair is the shortest",
        "cycle there is, so these edges are the first ones to question when untangling",
        "a cycle: removing one direction of a pair is often a matter of moving a",
        "function to the module that owns its data. A line here joins the two modules",
        "of a pair.",
        "",
    ]
    if pairs:
        lines += ["```{graphviz}", pairs_dot(graph), "```", ""]
    return lines


def render(graph: "ModuleGraph | None" = None) -> str:
    """Compose the whole page, as Markdown."""
    graph = graph if graph is not None else load_graph()
    found = cycles(graph)
    in_cycles = sum(len(members) for members in found)
    lines = [
        "# Module dependencies",
        "",
        "<!-- GENERATED by scripts/render_module_graph.py from tach.toml; do not edit -->",
        "",
        f"otto's source is split into {len(graph.modules)} modules, and `tach.toml` declares",
        f"{graph.edge_count()} dependencies between them. `tach check --exact` (run by",
        "`make lint-arch`) refuses an import that crosses from one module into another",
        "unless `tach.toml` declares that edge, so this page shows the contract the",
        "code is held to. It is redrawn from `tach.toml` on every docs build.",
        "",
        "It also refuses a declared edge that no import uses, so every edge drawn",
        "here is an import in the code. Why each edge exists, and",
        "which ones are known debt, is written beside it in `tach.toml`; how the file",
        "is maintained is in [the quality gates](quality-gates.md).",
        "",
        "## The layering",
        "",
        "An arrow points from a module to a module it imports. Each cycle (a set of",
        "modules that can all reach each other) is drawn as one bold node, and an",
        "arrow is left out when the modules it joins are already connected another",
        "way, so what remains is the order the modules build on each other.",
        f"{in_cycles} of the {len(graph.modules)} modules are in a cycle.",
        "",
        "```{graphviz}",
        overview_dot(graph),
        "```",
        "",
        *_cycle_section(graph, found),
        "## Every module",
        "",
        "| module | depends on | used by |",
        "|---|---|---|",
    ]
    for module in graph.modules:
        deps = ", ".join(_short(d) for d in graph.depends_on[module]) or "—"
        users = ", ".join(_short(u) for u in graph.used_by(module)) or "—"
        lines.append(f"| {_short(module)} | {deps} | {users} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: "list[str]") -> int:
    """Render the page, or render it and write nothing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=PAGE_PATH)
    parser.add_argument("--check", action="store_true", help="render only; write nothing")
    args = parser.parse_args(argv)
    page = render()
    if args.check:
        print(f"module graph: renders ({len(page.splitlines())} lines)")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"module graph: wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
