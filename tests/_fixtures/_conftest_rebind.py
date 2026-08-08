"""Rebind a directory conftest to a DUPLICATE ``Directory`` collector (pytest 9).

The hazard
----------
Since pytest 8.4/9 a conftest's fixtures are bound to the ``Directory``
collector **node object** for its directory, not to a nodeid string:
``FixtureManager._pending_conftests`` maps ``dir path -> conftest module`` and
``pytest_make_collect_report`` pops it the first time that directory is
collected, calling ``parsefactories(holder=conftest, node=<that node>)``.
Visibility is then pure object identity — ``_matchfactories`` keeps a
``FixtureDef`` only when ``fixturedef.node in node.iter_parents()``, and
``_getautousenames`` walks ``_node_autousenames``, a ``dict[Node, ...]``.

``Session.collect`` can build the same directory TWICE. When an initial
argument's remaining parts are exactly one file path, it re-collects that
file's parent with ``handle_dupes=False``, which overwrites the cached
``CollectReport`` with fresh child ``Directory`` nodes — for EVERY level
between the re-collected parent and a later argument's target. Each later
argument descending through that parent gets new nodes the fixture manager
has never seen, and every conftest on the rebuilt levels silently vanishes:
autouse fixtures simply never run.

Concretely, three arguments are enough::

    pytest tests/unit/cli/test_listing.py \\
           tests/unit/test_tuple_return_debt.py \\
           tests/unit/cli/test_cov.py

Argument 2 is a bare file under ``tests/unit`` — the shared parent of
arguments 1 and 3 — so ``tests/unit/cli`` is rebuilt between them and
``tests/unit/cli/conftest.py``'s autouse fixtures never fire for
``test_cov.py``. That dropped ``no_logger_output_dir``, whose stub
``OttoContext`` every ``otto cov get`` validation test depends on, and three
tests failed with a bogus ``TypeError`` instead of the message they assert.
With the bare sibling higher up (e.g. ``noxfile.py`` in the repo root), the
ROOT ``tests/conftest.py`` itself is on a rebuilt level and every root guard
is gone for the third argument's items. Every PAIR of those files passes;
only the TRIPLE fails, and the loadgroup gates never co-schedule that exact
shape — so it is invisible in CI and shows up only when a developer types two
directories' worth of files in one command.

The repair
----------
At ``pytest_collectstart`` — before the directory's children (and therefore
before any item's fixture closure) are built — notice a ``Directory`` node
whose conftest was already consumed by an EARLIER node for the same path, and
re-run ``parsefactories`` for that conftest against the new node. The result
is exactly what pytest would have produced had it created the node once: the
autouse names bind to the live node and the ``FixtureDef``s become visible to
items under it.

Two load-bearing wiring choices, both found the hard way:

* **This module must be registered as a PLUGIN** (``tests/conftest.py`` does
  ``config.pluginmanager.register(...)`` from ``pytest_configure``), never
  re-exported as a conftest hook. ``collect_one_node`` dispatches
  ``pytest_collectstart`` through ``collector.ihook`` — a *path-filtered*
  proxy that strips conftest-module hookimpls for directories the conftest
  system considers unrelated, and an intermediate (non-argument-anchor)
  directory's first collect sees NO conftests at all. A conftest-hosted hook
  therefore never fires for exactly the nodes that need repair (the interim
  reviewer proved root guards vanishing for a whole argument's items).
  Registered plugins are dispatched globally, for every node.
* **Bind-state is read from the fixture manager, not counted locally**: a
  node needs the rebind iff its directory's conftest is loaded, no longer
  pending (an earlier node consumed it), and no FixtureDef is bound to THIS
  node object. That stays correct even for orderings where this hook never
  saw the first node, and it is naturally idempotent — the same-node
  re-collect that ``handle_dupes=False`` performs is a no-op here.

Pinned by ``tests/unit/test_conftest_directory_rebind.py``: a three-level
pytester-subprocess probe (root + intermediate + anchor conftests) whose
no-hook leg is the standing reproduction for BOTH the anchor and the
intermediate/root levels — if it ever goes green, pytest fixed the
re-collect upstream and this module can be deleted.
"""

import pytest


def _directory_conftest(collector: pytest.Directory):
    """The loaded conftest module plugin for *collector*'s directory, or None."""
    return collector.config.pluginmanager.get_plugin(str(collector.path / "conftest.py"))


def pytest_collectstart(collector: pytest.Collector) -> None:
    """Re-bind a directory's conftest when pytest hands out a duplicate node.

    A no-op when the node's conftest is still *pending* (pytest binds the
    first node itself, in its own ``pytest_make_collect_report`` wrapper,
    which runs after this hook), when a ``FixtureDef`` is already bound to
    this very node (the same-node re-collect), and for directories with no
    loaded ``conftest.py``.
    """
    if not isinstance(collector, pytest.Directory):
        return
    conftest = _directory_conftest(collector)
    if conftest is None:
        return

    fixturemanager = collector.session._fixturemanager
    # First collect of this path: the binding is still pending and pytest
    # will perform it itself right after this hook.
    if collector.path in fixturemanager._pending_conftests:
        return
    bound_here = any(
        fixturedef.node is collector
        for fixturedefs in fixturemanager._arg2fixturedefs.values()
        for fixturedef in fixturedefs
    )
    if bound_here:
        return

    # The same call pytest makes for the first node — see
    # ``_pytest.fixtures.FixtureManager.pytest_make_collect_report``. The
    # fixture manager exposes no public rebind seam; the pin proves this
    # keeps working across pytest upgrades.
    fixturemanager.parsefactories(holder=conftest, node=collector)
