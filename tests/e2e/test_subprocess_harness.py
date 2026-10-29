"""The e2e subprocess harness must never let a child touch the real ``~/.otto``.

The completion caches live in the user-level workspace home
(:func:`otto.config.home.otto_home`), so a child spawned without
``OTTO_HOME`` reads the developer's real cache — and writes its fixture
corpus back into it. That is not hypothetical: before the harness pinned
the home, thousands of tmp-path-keyed workspace dirs accumulated under the
developer's ``~/.otto``, and the stable ``project_e2e`` workspace stayed WARM
across whole test sessions, which is how a cache-content bug produced an
e2e failure that only reproduced on the second-ever run on a machine.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import (
    _INSTALLER_INDEX_VARS,
    _isolate_installer_config,
    otto_subprocess_env,
)

pytestmark = pytest.mark.hostless


def test_env_pins_otto_home_beside_the_xdir(tmp_path: Path) -> None:
    """With an xdir, the home sits beside it — per-test, warm WITHIN a test.

    Beside the xdir rather than a fresh tempdir per call, deliberately: a
    test's second ``run_otto`` in the same xdir must be legitimately warm, or
    every warm-path regression would be masked by permanent coldness.
    """
    env = otto_subprocess_env(xdir=tmp_path)
    assert env["OTTO_HOME"] == str(tmp_path / "otto-home")


def test_env_without_an_xdir_still_isolates_otto_home() -> None:
    """No xdir is not a license to fall back to the real home."""
    env = otto_subprocess_env()
    home = Path(env["OTTO_HOME"])
    assert home != Path.home() / ".otto"
    assert Path.home() not in home.parents, f"OTTO_HOME leaked under $HOME: {home}"


def test_an_explicit_otto_home_still_wins(tmp_path: Path) -> None:
    """``extra_env`` overrides, so tests probing home resolution keep control."""
    env = otto_subprocess_env(xdir=tmp_path, extra_env={"OTTO_HOME": "/elsewhere"})
    assert env["OTTO_HOME"] == "/elsewhere"


def test_subprocess_env_isolates_installer_config(tmp_path: Path) -> None:
    """User, global and site installer config cannot add an index that serves
    the fixture's local version: the config files are switched off and every
    index override is dropped, even one the caller's own env carries."""
    hostile = {
        "PIP_INDEX_URL": "https://mirror.invalid/simple",
        "PIP_EXTRA_INDEX_URL": "https://extra.invalid/simple",
        "UV_INDEX_URL": "https://mirror.invalid/simple",
        "UV_EXTRA_INDEX_URL": "https://extra.invalid/simple",
        "UV_DEFAULT_INDEX": "https://default.invalid/simple",
        "UV_INDEX": "https://named.invalid/simple",
        "PIP_FIND_LINKS": "/somewhere/wheels",
        "UV_FIND_LINKS": "/somewhere/wheels",
        "UV_CONFIG_FILE": "/somewhere/uv.toml",
    }
    with pytest.MonkeyPatch.context() as mp:
        for key, value in hostile.items():
            mp.setenv(key, value)
        env = otto_subprocess_env(xdir=tmp_path)
    assert env["PIP_CONFIG_FILE"] == "/dev/null"
    assert env["UV_NO_CONFIG"] == "1"
    for key in hostile:
        assert key not in env


def test_isolate_installer_config_drops_index_sources_and_keeps_the_rest() -> None:
    """Drives the step directly on a dict that carries every droppable variable,
    so the guard can fail: the build-from-scratch env alone would pass vacuously."""
    env = dict.fromkeys(_INSTALLER_INDEX_VARS, "planted")
    env["UNRELATED_VAR"] = "kept"
    _isolate_installer_config(env)
    for var in _INSTALLER_INDEX_VARS:
        assert var not in env
    assert env["UNRELATED_VAR"] == "kept"
    assert env["PIP_CONFIG_FILE"] == "/dev/null"
    assert env["UV_NO_CONFIG"] == "1"
