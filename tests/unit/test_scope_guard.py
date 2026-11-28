"""Every reader of the whole-universe accessors is named, with a reason (spec 2026-11-24 §6, Q13).

Project scope bounds every walk, lookup and listing: code reads ``ctx.lab`` /
``ctx.projects`` / ``get_host``. A module that needs the whole universe reads
it through an explicit accessor (``ctx.unscoped``, ``otto.lab_view.whole_lab``,
``otto.declared.unscoped_projects``) or one of the other whole-universe tokens.
This guard reads every module under ``src/otto`` and fails on a token read by a
module that ``_READERS`` does not list with that token, so a new whole-universe
read cannot slip in unnoticed; it also fails on a listed pair that no longer
reads its token. The walk is the shared one in ``tests/_fixtures/import_walk.py``: every import
spelling at any depth, ``TYPE_CHECKING`` bodies skipped.

No test here reads the lab; the guard is a static read of the source.
"""

import ast
import functools

import pytest

from scripts.render_module_graph import module_of
from tests._fixtures.import_walk import SRC, ImportWalk, package_of, resolve_import

pytestmark = pytest.mark.interpreter_agnostic


_UNSCOPED_ATTR = "unscoped"
_ANY_SPELLING = frozenset(
    {"unscoped_projects", "whole_lab", "installed_resolver", "published_result"}
)
_DECLARED_NAMES = frozenset({"get_projects", "get_ordered_projects", "compose"})
_ACCESSOR_HOMES = frozenset({"otto.declared", "otto.bootstrap"})
"""Where ``get_projects`` and ``get_ordered_projects`` are importable from; the
bare ``otto.bootstrap`` import (the initiator side effect) reads nothing."""
_LAB_HOMES = frozenset({"otto.labs.loader.Lab", "otto.lab.Lab"})

_DEFINES = frozenset(
    {
        "otto.bootstrap",
        "otto.declared",
        "otto.run_context",
        "otto.lab_view",
        "otto.invocation",
        "otto.labs.loader",
    }
)
"""The modules that define the accessors; they read their own tokens."""

_PARTICIPATION = "participation input (§3.3)"
_CONFIG = (
    "whole-universe configuration (§3.7: logs, coverage, reservations, monitor, published_result)"
)
_CORE = "orchestration core (§3.7: walk input, driving project, dependency closure, in-play)"
_OPT_OUT = "opt-out command or recorded-file reader"
_LIST_LABS = "--list-labs' per-project lab filter (§4.1)"
_LAB_BUILD = "lab build and reservation configuration (bootstrap())"
_COMPLETION = "completion payload or cache, phase 3"
_HOP = "a hop and class discovery need only lab membership (§3.8, Q29)"
_OTTO_STATE = "state otto created is found lab-wide (Q8)"
_SAFETY = "a safety guard or a lookup diagnostic reads the whole lab (Q12)"
_CANONICAL = "canonical-lab writes and docker facts"
_BUILDS_LAB = "builds a canonical Lab"

_READERS: dict[str, dict[str, str]] = {
    "otto.cli.completers": {
        "get_projects": _COMPLETION,
    },
    "otto.cli.cov": {
        "get_projects": _CONFIG,
    },
    "otto.cli.docker": {
        "get_projects": _COMPLETION,
    },
    "otto.cli.host_lookup": {
        "unscoped": _HOP,
    },
    "otto.cli.init": {
        "get_projects": _OPT_OUT,
    },
    "otto.cli.inventory": {
        "get_projects": _PARTICIPATION,
    },
    # bootstrap: the pre-lab gates; get_projects: a lab-free command's owner,
    # judged over every parsed project before any lab.
    "otto.cli.invoke": {
        "bootstrap": _LAB_BUILD,
        "get_projects": _PARTICIPATION,
    },
    "otto.cli.lab": {
        "get_projects": _OPT_OUT,
    },
    "otto.cli.link": {
        "get_projects": _COMPLETION,
        "unscoped": _OTTO_STATE,
    },
    "otto.cli.listing": {
        "get_projects": _PARTICIPATION,
    },
    "otto.cli.main": {
        "bootstrap": _LAB_BUILD,
        "get_projects": _LIST_LABS,
        "published_result": _CONFIG,
    },
    "otto.cli.monitor": {
        "get_projects": _OPT_OUT,
    },
    "otto.cli.remote_completion": {
        "get_projects": _COMPLETION,
    },
    "otto.cli.reservation": {
        "get_projects": _CONFIG,
    },
    "otto.cli.run": {
        "get_projects": _PARTICIPATION,
    },
    # Mixed: the participation reads (listing_participation) are the ones that
    # stay; its completers and the tree printer also read the whole universe.
    "otto.cli.test": {
        "get_projects": _PARTICIPATION,
    },
    "otto.cli.tunnel": {
        "get_projects": _COMPLETION,
        "unscoped": _OTTO_STATE,
    },
    "otto.config.scope": {
        "unscoped": _CORE,
    },
    "otto.context": {},
    "otto.coverage.collect": {
        "unscoped_projects": _CONFIG,
    },
    "otto.coverage.get": {
        "unscoped_projects": _CONFIG,
    },
    "otto.coverage.instrumentation": {
        "unscoped_projects": _CONFIG,
    },
    "otto.docker.compose": {
        "whole_lab": _CANONICAL,
    },
    # _no_capable_host names the docker-capable hosts the scope leaves out.
    "otto.docker.observe": {
        "whole_lab": _SAFETY,
    },
    "otto.docker.resolve": {
        "whole_lab": _CANONICAL,
    },
    "otto.examples.lab_repository": {
        "Lab": _BUILDS_LAB,
    },
    "otto.examples.reservations_cli": {
        "Lab": _BUILDS_LAB,
    },
    "otto.fleet": {
        "whole_lab": _CORE,
    },
    "otto.host.power": {
        "installed_resolver": _HOP,
    },
    "otto.host.remote_host": {
        "installed_resolver": _HOP,
    },
    "otto.host.userland": {
        "published_result": _CONFIG,
    },
    "otto.labs.json_repository": {
        "Lab": _BUILDS_LAB,
    },
    "otto.link.manage": {
        "whole_lab": _SAFETY,
    },
    "otto.monitor.live": {
        "unscoped": _OTTO_STATE,
        "unscoped_projects": _CONFIG,
    },
    "otto.project.orchestrator": {
        "unscoped": _CORE,
    },
    "otto.project.plan": {
        "unscoped": _CORE,
    },
    # requirement_lab: the one site that picks the lab a reservation
    # requirement reads, for the gate, otto reservation check, otto host's
    # named-host gate and the remote completer alike.
    "otto.reservations.check": {
        "unscoped": _CONFIG,
    },
    # owner_refusal judges participation over every parsed project, once.
    "otto.session.activation": {
        "unscoped": _PARTICIPATION,
    },
    "otto.session.dependencies": {
        "unscoped": _CORE,
    },
    "otto.session.logs": {
        "get_projects": _CONFIG,
    },
    "otto.suite.run": {
        "Lab": _BUILDS_LAB,
        "unscoped": _CORE,
        "unscoped_projects": _CONFIG,
    },
    "otto.testing.conformance": {
        "Lab": _BUILDS_LAB,
    },
    "otto.testing.conformance_host": {
        "Lab": _BUILDS_LAB,
    },
    "otto.tunnel.manage": {
        "whole_lab": _OTTO_STATE,
    },
}
"""Module -> token -> why this module reads the whole universe."""


class _Reads(ImportWalk):
    """The shared import walk, widened to attribute reads and string attributes."""

    def __init__(self, package: str, lines: "list[str]") -> None:
        super().__init__(package, lines)
        self.aliases: dict[str, str] = {}
        self.dotted: set[str] = set()
        self.attrs: set[str] = set()
        self.chains: list[tuple[str, list[str]]] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 — ast.NodeVisitor spelling
        for alias in node.names:
            self.dotted.add(alias.name)
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                root = alias.name.split(".")[0]
                self.aliases[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 — ast.NodeVisitor spelling
        module = resolve_import(self.package, node)
        self.dotted.add(module)
        for alias in node.names:
            full = f"{module}.{alias.name}"
            self.dotted.add(full)
            self.aliases[alias.asname or alias.name] = full

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast.NodeVisitor spelling
        """Read ``getattr(x, "name")`` as the attribute read ``x.name``."""
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self.attrs.add(node.args[1].value)
            target = node.args[0]
            if isinstance(target, ast.Name):
                self.chains.append((target.id, [node.args[1].value]))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 — ast.NodeVisitor spelling
        self.attrs.add(node.attr)
        parts = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            self.chains.append((value.id, parts[::-1]))
        self.generic_visit(node)


def tokens_in(text: str, package: str) -> "set[str]":
    """Return every whole-universe token the source *text* reads at run time."""
    walk = _Reads(package, text.split("\n"))
    walk.visit(ast.parse(text))
    dotted = set(walk.dotted)
    for base, rest in walk.chains:
        if base in walk.aliases:
            dotted.add(".".join([walk.aliases[base], *rest]))
    found: set[str] = set()
    if _UNSCOPED_ATTR in walk.attrs:
        found.add(_UNSCOPED_ATTR)
    for name in dotted:
        module, _, leaf = name.rpartition(".")
        if leaf in _ANY_SPELLING or (module in _ACCESSOR_HOMES and leaf in _DECLARED_NAMES):
            found.add(leaf)
        elif name == "otto.bootstrap.bootstrap":
            found.add("bootstrap")
        elif name in _LAB_HOMES:
            found.add("Lab")
    return found


@functools.cache
def _reads_by_module() -> "dict[str, set[str]]":
    reads: dict[str, set[str]] = {}
    for path in sorted((SRC / "otto").rglob("*.py")):
        module = module_of(path.relative_to(SRC).as_posix())
        if module in _DEFINES:
            continue
        found = tokens_in(path.read_text(encoding="utf-8"), package_of(path))
        if found:
            reads[module] = found
    return reads


def test_every_whole_universe_reader_is_named() -> None:
    unnamed = [
        f"{module}/{token}"
        for module, tokens in sorted(_reads_by_module().items())
        for token in sorted(tokens - set(_READERS.get(module, {})))
    ]
    assert not unnamed, (
        "use ctx.lab / ctx.projects / get_host, or add <module>/<token> to _READERS "
        "with a reason:\n  " + "\n  ".join(unnamed)
    )


def test_reader_list_has_no_stale_entry() -> None:
    reads = _reads_by_module()
    stale = [
        f"{module}/{token}"
        for module, tokens in sorted(_READERS.items())
        for token in sorted(set(tokens) - reads.get(module, set()))
    ]
    assert not stale, "_READERS names a (module, token) that is no longer read:\n  " + "\n  ".join(
        stale
    )


def test_every_reader_has_a_reason() -> None:
    blank = [f"{m}/{t}" for m, ts in _READERS.items() for t, why in ts.items() if not why.strip()]
    assert not blank, blank


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f(ctx):\n    return ctx.unscoped.lab\n", {"unscoped"}),
        ("from otto.declared import unscoped_projects\n", {"unscoped_projects"}),
        ("from ..declared import unscoped_projects as up\n", {"unscoped_projects"}),
        ("from .. import declared\ndeclared.unscoped_projects()\n", {"unscoped_projects"}),
        ("import otto.declared\notto.declared.get_projects()\n", {"get_projects"}),
        ("import otto.declared as d\nd.get_ordered_projects()\n", {"get_ordered_projects"}),
        ("def f():\n    from ..declared import compose\n", {"compose"}),
        ("from otto.docker.compose import compose\n", set()),
        ("from otto.lab_view import whole_lab\n", {"whole_lab"}),
        ("def f():\n    from ..invocation import installed_resolver\n", {"installed_resolver"}),
        ("class C:\n    from otto.declared import published_result\n", {"published_result"}),
        ("from otto import bootstrap\n", set()),
        ("from .. import bootstrap\ndel bootstrap\n", set()),
        ("import otto.bootstrap\n", set()),
        ("from .. import bootstrap\nbootstrap.bootstrap()\n", {"bootstrap"}),
        ("import otto.bootstrap as b\nb.bootstrap()\n", {"bootstrap"}),
        ("from otto.bootstrap import get_projects\n", {"get_projects"}),
        ("from ..bootstrap import get_ordered_projects as g\n", {"get_ordered_projects"}),
        ("from .. import bootstrap\nbootstrap.get_projects()\n", {"get_projects"}),
        ("import otto.bootstrap\notto.bootstrap.get_projects()\n", {"get_projects"}),
        ("def f(ctx):\n    return getattr(ctx, 'unscoped')\n", {"unscoped"}),
        ("from otto.bootstrap import bootstrap\n", {"bootstrap"}),
        ("from otto.labs.loader import Lab\n", {"Lab"}),
        ("from ..labs import loader\nloader.Lab\n", {"Lab"}),
        ("from otto.lab import Lab as L\n", {"Lab"}),
        ("import otto.lab\notto.lab.Lab\n", {"Lab"}),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from otto.lab import Lab\n",
            set(),
        ),
        ("from otto.labs.loader import split_lab_names\n", set()),
    ],
)
def test_guard_sees_every_spelling(source: str, expected: "set[str]") -> None:
    assert tokens_in(source, "otto.probe") == expected, source
