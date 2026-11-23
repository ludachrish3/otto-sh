"""One enumeration of every TAB completion site in otto's serialised completion tree.

The serialised tree is what ``otto.config.completion_tree.serialize_tree`` writes
into the cache's ``shim`` section, and what ``otto._shim_complete`` answers from.
A scoped group (``otto host``) completes its verbs from a VIEW chosen by the
host's class: ``tree["host_classes"][<class>]`` for an id the cache knows, its
own ``commands`` (the union menu) for one it does not. Each view is walked here
and every node keeps the class it was reached through, so a parameter that
exists on one host class only is still enumerated, under that class.

Two callers, one walk: the shim differential (``tests/unit/shim/test_differential.py``)
generates its corpus from :func:`completion_nodes`, and the import-budget coverage
test (``tests/unit/import_budget/test_completion_coverage.py``) maps every
:func:`completion_sites` entry to a measured surface.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompletionNode:
    """One command node of the serialised tree, as a TAB reaches it."""

    path: tuple[str, ...]
    """Command names from the root, the root itself being ``()``. A scoped verb
    sits directly under its group (``("host", "get")``): the host id a TAB types
    between them is not part of the path."""
    host_class: str | None
    """The host class whose view holds this node; ``None`` outside a class view,
    including the scoped group's union menu."""
    scope: tuple[str, ...] | None
    """The scoped group's path when this node is reached through one of its views
    (the union menu included); ``None`` when it is reached by name alone."""
    node: dict[str, Any]


SITE_HANDOVER_CLASSES: dict[str, str] = {
    "live": "live",
    "past_sep": "list fragment past its first separator",
}
"""Each :attr:`CompletionSite.why` the corpus produces -> the shim's hand-over reason class.

The class is the one ``tests/unit/shim/test_differential.py`` buckets a reason
into (``_reason_class``), and ``tests/unit/import_budget/test_completion_coverage.py``
asserts that these and :data:`INPUT_HANDOVER_CLASSES` are exactly that file's
``EXPECTED_HANDOVER_REASONS``: a new hand-over class in the shim fails there
until it is placed here, and a site class is then modelled by :func:`_shape`.
``multi`` has no entry: otto has no ``nargs > 1`` site, so the corpus never
produces its reason (``nargs=N option NAME``)."""

INPUT_HANDOVER_CLASSES: frozenset[str] = frozenset(
    {"value given to flag", "stacked short flags", "unknown option", "unknown command"}
)
"""The hand-over classes the TYPED WORDS cause, on any site: none is a site's shape or kind."""


@dataclass(frozen=True)
class CompletionSite:
    """One value-taking parameter: the place a TAB completes a value."""

    path: tuple[str, ...]
    host_class: str | None
    param: str
    source: dict[str, Any]
    shape: str
    """``"single"``, ``"multi"`` (``nargs > 1``, which the shim hands over) or
    ``"past_sep"`` (a separated list that goes live past its first separator)."""

    @property
    def identity(self) -> tuple[tuple[str, ...], str | None, str]:
        """The canonical site key: command path, host class, parameter name."""
        return (self.path, self.host_class, self.param)

    @property
    def why(self) -> str | None:
        """Why the shim hands this site over (``"live"`` or the shape), else ``None``."""
        if self.shape != "single":
            return self.shape
        if self.source.get("kind") == "live":
            return "live"
        return None

    @property
    def handover(self) -> bool:
        """The shim hands this site over to the real CLI rather than answering it."""
        return self.why is not None


def _shape(param: dict[str, Any]) -> str:
    """The shim's own hand-over rules, read from the serialised fields.

    ``nargs > 1`` on an option or a positional (``_shim_complete._reject_multi_value``
    and ``_target``), and a ``payload`` separated list with ``live_past_sep``
    (``_shim_complete._source_values``, which reads the flag on that kind only).
    """
    if param["nargs"] > 1:
        return "multi"
    source = param["source"]
    if source.get("kind") == "payload" and source.get("sep") and source.get("live_past_sep"):
        return "past_sep"
    return "single"


def _walk(
    node: dict[str, Any],
    path: tuple[str, ...],
    host_class: str | None,
    scope: tuple[str, ...] | None,
    tree: dict[str, Any],
) -> Iterator[CompletionNode]:
    yield CompletionNode(path, host_class, scope, node)
    if node.get("scoped_by") and scope is None:
        # A TAB never reaches these verbs by name: the next word is the host id,
        # and the id picks the view. The union first, then every class.
        for name, child in node["commands"].items():
            yield from _walk(child, (*path, name), None, path, tree)
        for cls, verbs in tree.get("host_classes", {}).items():
            for name, child in verbs.items():
                yield from _walk(child, (*path, name), cls, path, tree)
        return
    for name, child in node["commands"].items():
        yield from _walk(child, (*path, name), host_class, scope, tree)


def completion_nodes(tree: dict[str, Any]) -> list[CompletionNode]:
    """Every command node of *tree*, the scoped group's union menu and class views included."""
    return list(_walk(tree, (), None, None, tree))


def completion_sites(tree: dict[str, Any]) -> list[CompletionSite]:
    """Every value-taking parameter of *tree*, keeping the host class it is reached through."""
    return [
        CompletionSite(
            path=entry.path,
            host_class=entry.host_class,
            param=param["name"],
            source=param["source"],
            shape=_shape(param),
        )
        for entry in completion_nodes(tree)
        for param in entry.node["params"]
        if param["takes_value"]
    ]


def serialised_cli_tree() -> dict[str, Any]:
    """otto's own CLI, serialised as the cache's ``shim`` section holds it (no project)."""
    import typer

    from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
    from otto.config.completion_tree import serialize_tree

    return serialize_tree(typer.main.get_command(app), pending_key=PENDING_SUBCMD_ARGS_KEY).tree
