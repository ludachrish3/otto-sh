"""Click-shaped context CHAINS for the tests that drive the leaf preamble directly.

``command_preamble`` and the pieces it calls read a leaf ``click.Context``, and
one of them — :func:`~otto.cli.invoke.refuse_inactive_instruction` — walks
``ctx.parent`` up to the group that dispatched it. A fake that stops at the leaf
therefore models the wrong object: every real leaf context has a parent, right
up to the root, and a walk written against the real shape crashes on a fake
without one.

Sharing the builder rather than re-rolling it per module is what keeps the three
preamble test modules agreeing about that shape. Each node carries exactly the
three attributes the production walk reads — ``command.name``, ``info_name`` and
``parent`` — so a walk that started reading a fourth would fail here rather than
pass against a fake nobody remembered to extend.
"""

from types import SimpleNamespace
from typing import Any


def chain(*names: str) -> Any:
    """Build a ctx chain from the root name down, and return the LEAF node.

    ``chain("otto", "run", "flash-b")`` is what click builds for
    ``otto run flash-b``: the root context, the ``run`` group's context, and
    the leaf, each pointing at its parent.
    """
    node = None
    for name in names:
        node = SimpleNamespace(command=SimpleNamespace(name=name), info_name=name, parent=node)
    return node
