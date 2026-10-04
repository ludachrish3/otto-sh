"""Sibling test directories on one command line collect cleanly.

``--doctest-modules`` (pyproject ``addopts``) makes pytest collect every
``conftest.py`` as a ``DoctestModule``, and that collector imports the file
like a test module: ``import_path`` in the default ``prepend`` mode, under the
module name ``conftest`` when the directory is not a package. It never clears
``sys.modules["conftest"]`` first, so it gets whichever bare ``conftest`` was
imported LAST — and pytest loads every command-line argument's conftest up
front, before collection starts. With two such directories on one command line
the last-loaded one is in ``sys.modules`` when the first one's doctest
collector runs, and collection fails with ``import file mismatch``.

The root ``tests/conftest.py``'s ``pytest_ignore_collect`` repairs it by not
collecting a doctest-free ``conftest.py`` as a doctest module. These pins run
the REAL shape — two sibling unit directories with non-package conftests — in
a child pytest over this repo: once to prove it collects, once to prove both
conftests still load as plugins and serve their fixtures.
"""

import os
import subprocess
import sys

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# Two siblings whose conftests both import as the bare module name
# ``conftest`` (neither directory has an ``__init__.py``). The two smallest
# such directories: collecting both takes about a second.
_SIBLINGS = ["tests/unit/monitor", "tests/unit/suite"]

# pyproject's addopts stay on: ``--doctest-modules`` is the defect's trigger.
# The rest is speed and hygiene only — one process, no coverage, no cache.
_CHILD_ARGS = ["-q", "-p", "no:randomly", "-p", "no:cacheprovider", "-n0", "--no-cov"]

# One test per sibling that requests a fixture its directory's conftest
# defines, so resolving it proves that conftest is loaded and bound. Each test
# name is owned by another file: renaming it there turns the fixtures leg red,
# so rename it here too.
_FIXTURE_USERS = {
    # owned by tests/unit/monitor/test_review.py
    "test_serve_review_hands_the_server_the_declared_cert_and_key": (
        "tls_pair -- tests/unit/monitor/conftest.py"
    ),
    # owned by tests/unit/suite/test_artifact_layout.py
    "test_nothing_is_created_unless_requested": "otto_output_dir -- tests/unit/suite/conftest.py",
}

# Runaway guard, not a measurement: each child takes about a second.
_CHILD_TIMEOUT_S = 300


def _run_child(*args: str) -> tuple[int, str, str]:
    """Run pytest over the two siblings in a child; return (exit, output, detail)."""
    # The parent run must not steer the child: PYTEST_* carries the xdist
    # worker id and any ambient PYTEST_ADDOPTS, COV_CORE_* pytest-cov's
    # subprocess hand-off.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_"))
    }
    argv = [sys.executable, "-m", "pytest", *_CHILD_ARGS, *args, *_SIBLINGS]
    completed = subprocess.run(
        argv,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_S,
        check=False,
    )
    output = completed.stdout + completed.stderr
    detail = f"argv: {' '.join(argv)}\noutput tail:\n{output[-3000:]}"
    return completed.returncode, output, detail


def test_the_siblings_conftests_are_bare_modules() -> None:
    """The pins below are only a reproduction while both conftests import as
    the top-level name ``conftest``. If either directory becomes a package,
    the pair stops colliding and the pins pass for free — pick another pair
    of non-package siblings."""
    for sibling in _SIBLINGS:
        directory = PROJECT_ROOT / sibling
        assert (directory / "conftest.py").is_file(), f"{sibling} has no conftest.py"
        assert not (directory / "__init__.py").exists(), (
            f"{sibling} is now a package, so its conftest no longer collides with "
            "its sibling's; choose another pair of non-package directories"
        )


@pytest.mark.timeout(_CHILD_TIMEOUT_S + 30)
def test_sibling_directories_with_bare_conftests_collect_cleanly() -> None:
    """Two sibling directories on one command line collect with no error."""
    returncode, output, detail = _run_child("--collect-only")
    assert "import file mismatch" not in output, detail
    assert returncode == 0, detail


@pytest.mark.timeout(_CHILD_TIMEOUT_S + 30)
def test_both_colliding_conftests_still_serve_their_fixtures() -> None:
    """Skipping a conftest's doctest collector leaves its plugin load alone:
    in the same two-sibling shape, a test in each directory resolves the
    fixture its own conftest defines."""
    returncode, output, detail = _run_child(
        "--fixtures-per-test", "-k", " or ".join(_FIXTURE_USERS)
    )
    # `--fixtures-per-test` exits 0 even when collection errors, so the
    # mismatch is checked by text here too.
    assert "import file mismatch" not in output, detail
    assert returncode == 0, detail
    for test_name, fixture_line in _FIXTURE_USERS.items():
        assert f"fixtures used by {test_name}" in output, detail
        assert fixture_line in output, detail
