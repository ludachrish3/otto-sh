"""The plugins of ``otto test``'s inner pytest session do not depend on the environment.

Two ways the environment used to decide them:

- pytest-cov is a development dependency. The inner session blocked it with
  pytest-cov's own ``--no-cov``, which pytest cannot parse without the
  plugin, so every session died with a usage error wherever it was not
  installed (#593). It is blocked with pytest's ``-p no:pytest_cov`` now.
- pytest-asyncio, pytest-timeout and pytest-randomly are runtime
  dependencies, but they reached the session only through pytest's plugin
  autoload. With ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` set, ``--randomly-seed``
  was a usage error, and a ``--no-random`` run silently dropped
  ``asyncio_mode`` and ``timeout_method``. otto names the three with ``-p``
  now.

The dev venv carries pytest-cov, so these tests block it through
``PYTEST_ADDOPTS``, which is how pytest sees an environment without it. The
release check against a wheel installed with runtime dependencies only is
``nox -s wheel_smoke``.
"""

import subprocess
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import run_otto
from tests.e2e._selection_fixtures import make_selection_repo

pytestmark = pytest.mark.hostless

_SUITE = """\
import asyncio

import pytest


def test_inner_session_plugins(request):
    plugins = request.config.pluginmanager
    assert not plugins.has_plugin("pytest_cov")
    assert plugins.has_plugin("asyncio")
    assert plugins.has_plugin("timeout")


async def test_async():
    await asyncio.sleep(0)


@pytest.mark.timeout(1)
def test_times_out():
    import time

    time.sleep(30)
"""

_WITHOUT_PYTEST_COV = {"PYTEST_ADDOPTS": "-p no:tach -p no:pytest_cov"}
_NO_AUTOLOAD = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

_ENVS = pytest.mark.parametrize(
    "extra_env",
    [_WITHOUT_PYTEST_COV, None, _NO_AUTOLOAD],
    ids=["pytest-cov-absent", "pytest-cov-installed", "no-plugin-autoload"],
)


def _run(
    tmp_path: Path, argv: list[str], extra_env: dict[str, str] | None
) -> subprocess.CompletedProcess[str]:
    repo = make_selection_repo(tmp_path, suite_src=_SUITE)
    return run_otto(argv, xdir=tmp_path, sut_dirs=repo, lab="unix", extra_env=extra_env)


@_ENVS
def test_list_tests(tmp_path: Path, extra_env: dict[str, str] | None) -> None:
    """--list-tests collects, whatever pytest's environment."""
    r = _run(tmp_path, ["test", "--list-tests"], extra_env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "test_inner_session_plugins" in r.stdout


@_ENVS
@pytest.mark.parametrize("order", [["--seed", "7"], ["--no-random"]], ids=["seeded", "no-random"])
def test_run(tmp_path: Path, extra_env: dict[str, str] | None, order: list[str]) -> None:
    """A run has otto's runtime plugins and not pytest-cov, whatever pytest's environment."""
    r = _run(
        tmp_path,
        ["test", "test_inner_session_plugins", "test_async", "--no-cov", *order],
        extra_env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed" in r.stdout


@_ENVS
def test_timeout_marker_fires(tmp_path: Path, extra_env: dict[str, str] | None) -> None:
    """@pytest.mark.timeout interrupts a test, whatever pytest's environment."""
    r = _run(tmp_path, ["test", "test_times_out", "--no-cov", "--no-random"], extra_env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Timeout" in r.stdout
    assert "1 failed" in r.stdout
