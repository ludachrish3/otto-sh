"""A duplicate ``Directory`` collector must not drop that directory's conftest.

pytest 9 binds a conftest's fixtures to the ``Directory`` collector *node
object* for its directory (``FixtureManager._pending_conftests`` is popped the
first time that directory is collected, and both autouse lookup and fixture
visibility are then keyed by node identity). ``Session.collect`` re-collects a
directory with ``handle_dupes=False`` whenever an initial argument's remaining
parts are exactly one file path — which replaces that directory's cached
report, and with it every child ``Directory`` node on EVERY level down to a
later argument's target. That later argument lands on nodes the fixture
manager has never seen, and each conftest below the re-collected parent is
silently gone.

That is not a hypothetical: three real arguments reproduced it in this repo::

    pytest tests/unit/cli/test_listing.py \\
           tests/unit/test_tuple_return_debt.py \\
           tests/unit/cli/test_cov.py

``tests/unit/cli/conftest.py``'s autouse ``no_logger_output_dir`` — the fixture
that installs the stub ``OttoContext`` — never ran for ``test_cov.py``, so
three ``otto cov get`` validation tests exited 1 down an unrelated
``No active OttoContext`` path. With the bare sibling in the repo root
(``noxfile.py``), even ``tests/conftest.py``'s process-global guards vanish
for the third argument's items. Every PAIR of those files passes, so the
loadgroup gates (which never co-schedule that shape) cannot see it — hence a
pin that owns the argument shape itself rather than hoping for the scheduling.

The probe tree deliberately layers TWO conftest levels below the re-collected
parent:

* ``sub/pkg`` is an argument ANCHOR — the only level the first cut of the
  repair handled;
* ``sub`` is an INTERMEDIATE level: no argument anchors there, so pytest's
  path-filtered ``ihook`` proxy strips conftest-hosted ``pytest_collectstart``
  impls for its nodes entirely. This is why the repair must be registered as
  a real PLUGIN (``config.pluginmanager.register``) — the interim review
  proved a conftest-re-exported hook silently repaired ``pkg`` while losing
  ``sub`` (and, in the real tree, the root guards themselves).

``tests/_fixtures/_conftest_rebind.py`` repairs it; ``tests/conftest.py``
registers it from ``pytest_configure``. Both legs run a real nested pytest
over the same three-argument shape — the second with the plugin registered,
the first without, so the repair is proven against a live reproduction of
BOTH levels rather than a mock of one.
"""

from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytest_plugins = ["pytester"]

# An inert rootdir config: the inner session's behaviour must be pytest's and
# the probe's, never this repo's addopts.
PROBE_INI = """\
[pytest]
"""

# The INTERMEDIATE level (no argument anchors here): its autouse fixture is
# the dangerous half (it vanishes in silence); the plain fixture is the loud
# half (identity-keyed visibility makes it "not found").
SUB_CONFTEST = """\
import builtins

import pytest


@pytest.fixture(autouse=True)
def _sub_autouse_marker():
    builtins.SUB_AUTOUSE_RAN = True
    yield
    builtins.SUB_AUTOUSE_RAN = False


@pytest.fixture
def sub_fixture():
    return "sub-visible"
"""

# The ANCHOR level (two arguments target files inside it).
PKG_CONFTEST = """\
import builtins

import pytest


@pytest.fixture(autouse=True)
def _pkg_autouse_marker():
    builtins.PKG_AUTOUSE_RAN = True
    yield
    builtins.PKG_AUTOUSE_RAN = False


@pytest.fixture
def pkg_fixture():
    return "pkg-visible"
"""

PKG_TEST_A = """\
def test_a():
    assert True
"""

PKG_TEST_B = """\
import builtins


def test_pkg_autouse_fixture_ran():
    assert getattr(builtins, "PKG_AUTOUSE_RAN", False), (
        "pkg/conftest.py autouse fixture did not run"
    )


def test_sub_autouse_fixture_ran():
    assert getattr(builtins, "SUB_AUTOUSE_RAN", False), (
        "sub/conftest.py autouse fixture did not run"
    )


def test_pkg_fixture_is_visible(pkg_fixture):
    assert pkg_fixture == "pkg-visible"


def test_sub_fixture_is_visible(sub_fixture):
    assert sub_fixture == "sub-visible"
"""

# Sits directly in the rootdir — the shared parent of the two `sub/pkg/`
# arguments. Passing it as a bare file argument is what makes pytest rebuild
# `sub/` and `sub/pkg/`.
SIBLING_TEST = """\
def test_sibling():
    assert True
"""

# The REAL wiring mechanism, mirrored: plugin registration from
# pytest_configure — never a conftest hook re-export (path-filtered ihook
# proxies would strip it for the intermediate `sub/` nodes).
REBIND_ROOT_CONFTEST = """\
from tests._fixtures import _conftest_rebind


def pytest_configure(config):
    config.pluginmanager.register(_conftest_rebind, name="probe-rebind")
"""

BARE_ROOT_CONFTEST = """\
# deliberately no rebind plugin — this leg is the standing reproduction
"""

# `-p no:tach`: tach's pytest11 plugin panics on repeated in-tree sessions
# (issue #193), same guard the repo addopts use.
INNER_ARGS = ("-p", "no:tach", "-p", "no:cacheprovider")


def _probe_tree(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, conftest: str):
    """Build the probe rootdir and return the three arguments, in order."""
    # The inner session must not inherit the outer one's env-based config.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for var in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONTEXT",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(var, raising=False)
    # The rebind leg's root conftest imports the REAL module from the repo.
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    pytester.makeini(PROBE_INI)
    root: Path = pytester.path
    (root / "conftest.py").write_text(conftest)
    (root / "test_sibling.py").write_text(SIBLING_TEST)
    sub = root / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text(SUB_CONFTEST)
    pkg = sub / "pkg"
    pkg.mkdir()
    (pkg / "conftest.py").write_text(PKG_CONFTEST)
    (pkg / "test_a.py").write_text(PKG_TEST_A)
    (pkg / "test_b.py").write_text(PKG_TEST_B)
    # Argument ORDER is the whole defect: a bare sibling file between two
    # arguments that descend into `sub/pkg/`.
    return [str(pkg / "test_a.py"), str(root / "test_sibling.py"), str(pkg / "test_b.py")]


def test_duplicate_directory_nodes_drop_both_conftest_levels(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the plugin, the probe reproduces the loss on BOTH levels — the
    pin is not vacuous at either one.

    If this ever goes green, pytest has stopped rebuilding the child
    ``Directory`` nodes (or stopped keying fixtures by node identity) and
    ``tests/_fixtures/_conftest_rebind.py`` plus its registration in
    ``tests/conftest.py`` can be deleted.
    """
    args = _probe_tree(pytester, monkeypatch, BARE_ROOT_CONFTEST)
    result = pytester.runpytest_subprocess(*args, *INNER_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    # test_sibling + pkg/test_a keep their fixtures; all four of pkg/test_b's
    # checks lose theirs — the autouse ones silently, the requested ones
    # loudly, at BOTH the anchor (pkg) and intermediate (sub) level.
    result.assert_outcomes(passed=2, failed=2, errors=2)
    assert "pkg/conftest.py autouse fixture did not run" in combined
    assert "sub/conftest.py autouse fixture did not run" in combined
    assert "pkg_fixture" in combined
    assert "sub_fixture" in combined


def test_registered_rebind_plugin_restores_every_dropped_level(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the plugin registered, the identical argument shape keeps every
    conftest fixture on every level — anchor AND intermediate."""
    args = _probe_tree(pytester, monkeypatch, REBIND_ROOT_CONFTEST)
    result = pytester.runpytest_subprocess(*args, *INNER_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    assert "INTERNALERROR" not in combined, combined
    result.assert_outcomes(passed=6)


def test_rebind_plugin_is_registered_in_this_session(request: pytest.FixtureRequest) -> None:
    """The REAL wiring pin: the probe legs above wire the plugin themselves,
    so only this catches ``tests/conftest.py`` losing its registration. A
    re-export of the hook instead of ``pluginmanager.register`` leaves this
    name absent — and silently misses intermediate/root levels again (the
    interim review's MAJOR)."""
    assert request.config.pluginmanager.has_plugin("otto-conftest-rebind"), (
        "tests/conftest.py no longer registers tests/_fixtures/_conftest_rebind "
        "as the 'otto-conftest-rebind' plugin — pytest_configure must "
        "pluginmanager.register() it (a conftest hook re-export is path-filtered "
        "and does NOT work)"
    )
