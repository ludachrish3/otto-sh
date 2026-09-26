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

import textwrap

import pytest

from tests._fixtures.sutrepo import make_sut_repo


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


def write_repo_with_test_body(tmp_path, stem: str, body: str) -> str:
    """A repo whose one top-level test file is named ``test_<stem>.py`` and runs *body*.

    *stem* must be unique per case. ``Repo.import_test_file`` keys ``sys.modules``
    on the file STEM alone and early-returns when the name is already present, so
    parametrized cases sharing a filename would silently skip the import after the
    first and pass vacuously — a guard that cannot fail.
    """
    repo = make_sut_repo(
        tmp_path / stem,
        name=stem,
        tests=["tests"],
        files={f"tests/test_{stem}.py": textwrap.dedent(body)},
    )
    return str(repo)
