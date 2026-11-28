"""Click context CHAINS for the tests that drive the leaf preamble or a completer directly.

``command_preamble`` and the pieces it calls read a leaf ``click.Context``, and
one of them — :func:`~otto.cli.invoke.refuse_inactive_owner` — walks
``ctx.parent`` up to the group that dispatched it, while
:func:`~otto.cli.invoke.scope_selection` reads the root's parsed parameters
through ``ctx.find_root()``. A fake that stops at the leaf therefore models
the wrong object: every real leaf context has a parent, right up to the root.

So the chain is made of real ``click.Context`` objects, not look-alikes: a walk
or a reader that starts using another part of the context finds it here as it
finds it on the CLI. Sharing the builder rather than re-rolling it per module is
what keeps the test modules agreeing about that shape.
"""

from typing import Any

import click


def chain(*names: str, root_params: "dict[str, Any] | None" = None) -> click.Context:
    """Build a ctx chain from the root name down, and return the LEAF context.

    ``chain("otto", "run", "flash-b")`` is what click builds for
    ``otto run flash-b``: the root context, the ``run`` group's context, and
    the leaf, each pointing at its parent.

    With *root_params*, the root context is otto's (its command carries
    :data:`~otto.cli.invoke.OTTO_ROOT_ATTR`) and holds those parsed
    parameters, as the root's own parse leaves them (``{"labs": ["a", "b"]}``
    for ``-l a+b``). Without it the root is not otto's, and
    :func:`~otto.cli.invoke.scope_selection` reads no selection from it.
    """
    from otto.cli.invoke import OTTO_ROOT_ATTR

    node: "click.Context | None" = None
    for index, name in enumerate(names):
        is_leaf = index == len(names) - 1
        command: click.Command = click.Command(name) if is_leaf else click.Group(name)
        if node is None and root_params is not None:
            setattr(command, OTTO_ROOT_ATTR, True)
        node = click.Context(command, parent=node, info_name=name)
        if node.parent is None and root_params is not None:
            node.params = dict(root_params)
    if node is None:
        raise TypeError("chain() needs at least one command name")
    return node


class ChainLeaf:
    """Base for a hand-built leaf stand-in hung on a :func:`chain` (``self.parent``).

    A test that fakes the leaf itself (its ``command``, ``meta``) still reaches
    the root the way a real leaf does, so the selection's reader finds it.
    """

    parent: click.Context
    resilient_parsing: bool = False
    """A real parse, as a leaf being invoked has."""

    def find_root(self) -> click.Context:
        """The root of the parent chain, where the selection's reader looks."""
        return self.parent.find_root()
