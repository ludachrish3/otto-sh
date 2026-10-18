"""Render ``tach.toml`` into ``docs/architecture/modules.md``.

Run from the repo root::

    uv run python -m scripts.render_module_graph           # write the page
    uv run python -m scripts.render_module_graph --check   # render, write nothing

``tach.toml`` is the single home for otto's module dependency contracts:
``tach check`` (``make lint-arch``) refuses an import between two modules that
the file does not declare. This page draws the declared graph. Drawn whole it
is unreadable, so the page draws it three ways: the cycles condensed to one
node each (a module in no cycle is its own node), the pairs inside a cycle
that import each other, and a table of every module's edges.
``docs/conf.py`` runs this at ``builder-inited`` for every builder; the page is
git-ignored, so the only copy in the tree is ``tach.toml`` itself.
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import tomli

ROOT = Path(__file__).resolve().parent.parent

TACH_TOML = ROOT / "tach.toml"

PAGE_PATH = ROOT / "docs" / "architecture" / "modules.md"

# Where a new module goes: per layer, highest first, what belongs there and the slots it
# holds or fills (spec 2026-10-11 placement-and-phases §2). The page derives the rest
# (what each layer may import, which modules it holds) from tach.toml.
LAYER_GUIDE: "dict[str, list[str]]" = {
    "entry": [
        (
            "The CLI, the conformance harness, the shipped examples, the console shim, "
            "and the public facades over lower layers (`otto`, `otto.context`, `otto.lab`, "
            "`otto.session`)."
        ),
        "—",
    ],
    "root": [
        (
            "The composition root, `otto.bootstrap`, and the library entry points that can "
            "compose the projects with no run in hand: the test suite, coverage, the project-aware "
            "logging install."
        ),
        "Importing `otto.bootstrap` fills `otto.declared`'s initiator.",
    ],
    "workflows": [
        "What a run does: instructions, the project actions, `otto init`, the monitor service.",
        "`otto.project.actions` fills the instruction catalog's first-party slot.",
    ],
    "run": [
        "The live run: the run context, session's decisions, the fleet, docker.",
        "`set_context` fills the active-run slot in `otto.invocation`.",
    ],
    "composition": [
        "Settings, `Project` and discovery, the completion cache, the project environments.",
        "—",
    ],
    "sources": [
        "Where labs, inventory, creds and reservations come from; the lab model and its loader.",
        "—",
    ],
    "topology": [
        "Links, checks, tunnels, and the monitor's lower files.",
        "—",
    ],
    "specs": [
        "The pydantic models of lab, settings and inventory data.",
        "—",
    ],
    "host": [
        "Connections, transfer, the host classes and the host specs.",
        "—",
    ],
    "foundation": [
        "Leaf definitions every layer reads, and the slots filled from above.",
        (
            "Holds the slots: the initiator and the composed projects (`otto.declared`), the "
            "active run and its policy (`otto.invocation`), the first-party actions "
            "(`otto.instruction_catalog`)."
        ),
    ],
}


@dataclass
class ModuleGraph:
    """The declared dependency graph: each module and the modules it may import."""

    depends_on: "dict[str, list[str]]"
    layers: "list[str]" = field(default_factory=list)
    """tach's ``layers``, highest first, as ``tach.toml`` lists them."""
    layer_of: "dict[str, str]" = field(default_factory=dict)
    """Each module that declares a ``layer``, mapped to it; a module with none is exempt."""
    explicit_layer_dependencies: bool = False
    """``layers_explicit_depends_on``: whether a downward import must be declared too."""
    forbids_cycles: bool = False
    """``forbid_circular_dependencies``: whether tach refuses an import cycle."""

    def is_upward(self, module: str, dep: str) -> bool:
        """Whether *module* depending on *dep* points up the layers.

        A module with no layer is exempt, as tach exempts it, so an edge from
        or to one is never upward. Two modules of one layer may import each other.
        """
        if module not in self.layer_of or dep not in self.layer_of:
            return False
        return self.layers.index(self.layer_of[module]) > self.layers.index(self.layer_of[dep])

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

    Every entry must spell its ``depends_on``, ``[]`` for a module that imports
    no other. tach reads a missing list as "may import anything", so drawing it
    as no edges would draw a contract the code is not held to.

    Raises
    ------
    ValueError
        If an entry has no ``path`` or no ``depends_on``, or names a ``layer``
        that ``layers`` does not list.
    """
    doc = tomli.loads(path.read_text(encoding="utf-8"))
    layers = list(doc.get("layers", []))
    graph: dict[str, list[str]] = {}
    layer_of: dict[str, str] = {}
    for number, entry in enumerate(doc.get("modules", []), start=1):
        if "path" not in entry:
            msg = f"{path.name}: [[modules]] entry {number} has no path; name its module."
            raise ValueError(msg)
        if "depends_on" not in entry:
            msg = (
                f"{path.name}: module {entry['path']!r} has no depends_on. tach reads a "
                "missing list as 'may import anything'; spell it, [] for a module that "
                "imports no other."
            )
            raise ValueError(msg)
        if "layer" in entry and entry["layer"] not in layers:
            msg = (
                f"{path.name}: module {entry['path']!r} is in layer {entry['layer']!r}, "
                f"which layers does not list ({layers}); tach refuses an undefined layer."
            )
            raise ValueError(msg)
        deps = [dep if isinstance(dep, str) else dep["path"] for dep in entry["depends_on"]]
        graph[entry["path"]] = sorted(set(deps) - {entry["path"]})
        if "layer" in entry:
            layer_of[entry["path"]] = entry["layer"]
    for deps in list(graph.values()):
        for dep in deps:
            graph.setdefault(dep, [])
    return ModuleGraph(
        graph,
        layers=layers,
        layer_of=layer_of,
        explicit_layer_dependencies=bool(doc.get("layers_explicit_depends_on", False)),
        forbids_cycles=bool(doc.get("forbid_circular_dependencies", False)),
    )


def module_of(path: str) -> str:
    """Return the dotted module a file under ``src/`` defines.

    *path* is relative to ``src/``: ``otto/labs/loader.py`` is ``otto.labs.loader``,
    and a trailing ``__init__`` is dropped, so ``otto/lab/__init__.py`` is ``otto.lab``.
    """
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def declared_owner(module: str, declared: "list[str]") -> "str | None":
    """Return the declared tach module that owns *module*, or ``None`` when none does.

    A declared module owns itself and everything under it; the longest match wins.
    """
    owners = [m for m in declared if module == m or module.startswith(m + ".")]
    return max(owners, key=len) if owners else None


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


def node_edges(graph: ModuleGraph) -> "dict[str, set[str]]":
    """Collapse each cycle to one node and map each node to every node it imports.

    Nothing is dropped yet: this is every arrow between two nodes, before
    :func:`condensed` leaves out the ones another route covers.
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
    return edges


def _reachable(edges: "dict[str, set[str]]", start: str, skip: str) -> "set[str]":
    """Every node *start* reaches without taking its own arrow to *skip*."""
    seen: set[str] = set()
    todo = [n for n in edges[start] if n != skip]
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(edges[node])
    return seen


def condensed(graph: ModuleGraph) -> "dict[str, list[str]]":
    """Collapse each cycle to one node and drop the edges another route covers.

    What is left is acyclic. An edge ``a -> c`` is dropped when ``a`` already
    reaches ``c`` another way, so every arrow drawn is one the layering needs.
    """
    edges = node_edges(graph)
    return {
        node: sorted(dep for dep in targets if dep not in _reachable(edges, node, skip=dep))
        for node, targets in sorted(edges.items())
    }


class OmittedArrow(NamedTuple):
    """One arrow the overview leaves out, and a node that carries it instead."""

    source: str
    target: str
    via: str
    """A node *source* imports directly that reaches *target*."""


def omitted_arrow(graph: ModuleGraph) -> "OmittedArrow | None":
    """Name one arrow the overview leaves out, preferring one from ``otto.cli`` to a single module.

    ``None`` when the overview leaves nothing out.
    """
    edges = node_edges(graph)
    drawn = condensed(graph)
    dropped = [
        (source, target)
        for source, targets in sorted(edges.items())
        for target in sorted(targets)
        if target not in drawn[source]
    ]
    dropped.sort(key=lambda arrow: (arrow[0] != "otto.cli", arrow[1].startswith("cycle ")))
    for source, target in dropped:
        for via in sorted(edges[source] - {target}):
            if via in drawn[source] and target in _reachable(edges, via, skip=""):
                return OmittedArrow(source, target, via)
    return None


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
    if not found and graph.forbids_cycles:
        return [
            "No module is in a cycle: the condensed graph above is the whole graph.",
            "`tach.toml` sets `forbid_circular_dependencies = true`, so `tach check`",
            "refuses an import that would close one.",
            "",
        ]
    if not found:
        return [
            "No module is in a cycle: the condensed graph above is the whole graph.",
            "`tach.toml` does not set `forbid_circular_dependencies = true`, so",
            "`tach check` would not refuse an import that closed one.",
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
        "one cycle.",
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


def _omission_caption(graph: ModuleGraph) -> "list[str]":
    """Say which arrows the overview leaves out, how many, and where to see them all."""
    total = sum(len(targets) for targets in node_edges(graph).values())
    drawn = sum(len(targets) for targets in condensed(graph).values())
    lines = [
        "**Not every import is drawn.** An arrow from A to C is left out when A",
        "already reaches C through arrows that are drawn, so what remains is the",
        f"order the modules build on each other: {drawn} of the {total} arrows between",
        "these nodes are drawn.",
    ]
    example = omitted_arrow(graph)
    if example is not None:
        lines += [
            f"For example, {_short(example.source)} → {_short(example.target)} is not drawn,",
            f"because {_short(example.source)} → {_short(example.via)} is, and",
            f"{_short(example.via)} reaches {_short(example.target)}.",
        ]
    lines.append(
        "To see every dependency a module declares, use [the table of every module](#every-module)."
    )
    return lines


def layer_guide(graph: ModuleGraph) -> "list[str]":
    """Return the table that says where a new module goes, one row per layer, or none.

    Raises
    ------
    ValueError
        If ``LAYER_GUIDE`` does not describe exactly the layers ``tach.toml`` lists.
    """
    if not graph.layers:
        return []
    if list(LAYER_GUIDE) != graph.layers:
        msg = (
            f"LAYER_GUIDE describes {list(LAYER_GUIDE)}, but tach.toml's layers are "
            f"{graph.layers}; describe each layer, highest first."
        )
        raise ValueError(msg)
    lines = [
        "## Where a new module goes",
        "",
        "Each module has a layer, and may import its own layer and the ones below it,",
        "through the edges its `depends_on` lists. A new module goes in the lowest layer",
        "whose modules it needs; a slot is how a lower layer reaches up without an",
        "import: a higher module fills it, and the lower one reads it.",
        "",
        "| layer | what belongs | may import | slots | modules |",
        "|---|---|---|---|---|",
    ]
    for index, layer in enumerate(graph.layers):
        belongs, slots = LAYER_GUIDE[layer]
        below = "itself only" if index == len(graph.layers) - 1 else "itself and every layer below"
        members = sorted(m for m, home in graph.layer_of.items() if home == layer)
        held = ", ".join(_short(m) for m in members) or "—"
        lines.append(f"| `{layer}` | {belongs} | {below} | {slots} | {held} |")
    lines.append("")
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
        "It also refuses a declared edge that no import uses, so every declared edge",
        "is an import in the code. Why each edge exists, and",
        "which ones are known debt, is written beside it in `tach.toml`; how the file",
        "is maintained is in [the quality gates](quality-gates.md).",
        "",
        "## The layering",
        "",
        "An arrow points from a module to a module it imports. Each cycle (a set of",
        "modules that can all reach each other) is drawn as one bold node.",
        f"{in_cycles} of the {len(graph.modules)} modules are in a cycle.",
        "",
        *_omission_caption(graph),
        "",
        "```{graphviz}",
        overview_dot(graph),
        "```",
        "",
        *_cycle_section(graph, found),
        *layer_guide(graph),
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
