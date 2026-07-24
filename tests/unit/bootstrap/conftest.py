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


# ``_isolate_sys_path`` (the per-test ``sys.path`` snapshot/restore) used to
# live here dir-wide. It moved to the root ``tests/conftest.py``: an audit
# found ``sys.path`` is also mutated outside this tree (direct
# ``add_libs_to_pythonpath()`` calls in ``tests/unit/config``/``tests/unit/suite``,
# in-process ``bootstrap()`` in ``tests/e2e/suite``), and process-global state
# takes a root-level guard — the same #132/#133 rule that moved
# ``_isolate_registries`` there.
