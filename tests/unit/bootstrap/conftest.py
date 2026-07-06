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

import pytest


@pytest.fixture(autouse=True)
def _isolate_tmp_imports(purge_tmp_imports):
    """Dir-wide: request ``purge_tmp_imports`` so each bootstrap test's tmp-imported
    modules are dropped on teardown (the requested fixture owns the setup/teardown)."""
