"""The scaffold for a nested pytest session that tests a root-conftest guard.

A guard in ``tests/conftest.py`` is only pinned by a test that runs it: an
inner ``pytester.runpytest_subprocess`` session with the REAL root conftest
registered as a plugin (exactly as an xdist worker gets it), over probe tests
that trip the guard. The inner session must behave as the probes and the root
conftest say, never as the outer run happens to be configured, so
:func:`isolate_inner_session` strips everything the outer session leaks
through the environment and pins the terminal width the inner report is
parsed at.
"""

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# An inert rootdir config: none of this repo's addopts (--cov, -n auto,
# --doctest-modules) reach the inner session.
PROBE_INI = """\
[pytest]
"""

# The root conftest is registered as a PLUGIN rather than copied: a self-test
# is worthless unless it exercises the real hooks and fixtures.
ROOT_CONFTEST_AS_PLUGIN = """\
import tests.conftest as otto_root_conftest


def pytest_configure(config):
    config.pluginmanager.register(otto_root_conftest, name="otto-root-conftest")
"""

# `-p no:tach`: tach's pytest11 plugin panics on repeated in-tree sessions
# (issue #193), the same guard the repo addopts use.
INNER_ARGS = ["-p", "no:tach", "-p", "no:cacheprovider"]

# What the outer session leaks into a child: pytest-cov's subprocess hooks
# (which would write into the outer datafile) and a stale xdist identity.
_OUTER_SESSION_ENV = [
    "PYTEST_ADDOPTS",
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COV_CORE_CONTEXT",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
]

# Wide enough that no section header or summary line of the inner report is
# shortened; pytest sizes them from COLUMNS, which the outer run may set to
# anything.
INNER_COLUMNS = "200"


def isolate_inner_session(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make *pytester*'s subprocess runs independent of the outer session.

    Drops the outer session's env-based config, puts the repo root on
    ``PYTHONPATH`` (so a probe conftest can import ``tests.*``), fixes the
    report width, and writes the inert ini.
    """
    for var in _OUTER_SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))
    monkeypatch.setenv("COLUMNS", INNER_COLUMNS)
    pytester.makeini(PROBE_INI)


def root_conftest_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> pytest.Pytester:
    """An isolated *pytester* rootdir whose conftest registers the repo's root conftest."""
    isolate_inner_session(pytester, monkeypatch)
    (pytester.path / "conftest.py").write_text(ROOT_CONFTEST_AS_PLUGIN)
    return pytester
