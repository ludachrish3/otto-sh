"""Shared fixtures for the ``tests/unit/bootstrap`` package.

Every test here drives ``bootstrap()`` discovery against a SUT repo written into
``tmp_path``, which imports that repo's test/init modules into ``sys.modules``.
Those imports outlive the test, so under the nightly
``--count=N --repeat-scope=session`` repeat the second pass re-imports the same
module *name* and gets pass 1's cached copy — a stale valid module shadows a
freshly-written broken one and ``bootstrap()`` reports 0 discovery errors instead
of 1 (issue #108). ``bs._reset()`` (already autouse in ``test_bootstrap.py``)
clears bootstrap's own caches but not ``sys.modules``.

Requesting the unit-tree ``purge_tmp_imports`` fixture dir-wide removes each
test's tmp-imported modules on teardown, keeping discovery independent across
repeat iterations.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_tmp_imports(purge_tmp_imports):
    """Dir-wide: request ``purge_tmp_imports`` so each bootstrap test's tmp-imported
    modules are dropped on teardown (the requested fixture owns the setup/teardown)."""


@pytest.fixture(autouse=True)
def _isolate_sys_path():
    """Dir-wide: restore ``sys.path`` after each bootstrap test.

    ``Repo.add_libs_to_pythonpath()`` appends each repo's ``libs`` dirs (per-test
    ``tmp_path`` subdirs here) to ``sys.path`` with no cleanup of its own — it's
    a one-shot, startup-time call in production, so this is fine there. But
    combined with tests that reuse generic repo/init-module names (e.g. two
    different tests both writing a repo named ``b`` with ``init = ["b_init"]``),
    an earlier test's still-on-disk ``tmp_path`` entry sits ahead of the current
    test's own entry in ``sys.path``. ``purge_tmp_imports`` drops the stale name
    from ``sys.modules``, so the next ``import_module("b_init")`` re-imports
    fresh — but the import machinery resolves it against *whichever* matching
    directory comes first in ``sys.path``, which can be the earlier test's
    (working) module shadowing the current test's (deliberately broken) one.
    This guard lives here, not in the root or unit-tree conftest, because
    bootstrap tests are the only place that mutates ``sys.path`` this way.
    """
    snapshot = list(sys.path)
    yield
    sys.path[:] = snapshot
