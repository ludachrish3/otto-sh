"""The e2e subprocess harness must never let a child touch the real ``~/.otto``.

The completion caches live in the user-level workspace home
(:func:`otto.config.home.otto_home`), so a child spawned without
``OTTO_HOME`` reads the developer's real cache — and writes its fixture
corpus back into it. That is not hypothetical: before the harness pinned
the home, thousands of tmp-path-keyed workspace dirs accumulated under the
developer's ``~/.otto``, and the stable ``repo_e2e`` workspace stayed WARM
across whole test sessions, which is how a cache-content bug produced an
e2e failure that only reproduced on the second-ever run on a machine.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import otto_subprocess_env

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
