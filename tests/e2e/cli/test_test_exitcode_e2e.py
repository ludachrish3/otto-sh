"""End-to-end tests for ``otto test`` exit-code contract.

Verifies that runs propagate the pytest exit code correctly:
- a passing run exits 0
- a failing run exits non-zero
- an unknown test name is a usage error (exit 2) without a traceback

These serve as regression guards for the Task-2.6 fix: exit-code propagation
from the inner pytest.main() call.

All tests use ``--lab unix`` (required for a run) but ``TestE2EFixture``
requests no host, so the run itself is hostless; no Vagrant VM is contacted.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import (
    REPO_E2E,
    assert_no_output_dir,
    assert_output_dir,
    run_otto,
)

pytestmark = pytest.mark.hostless


def test_suite_pass_exits_zero(tmp_path: Path) -> None:
    """A run whose tests all pass exits 0."""
    r = run_otto(
        ["--lab", "unix", "test", "TestE2EFixture"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert_output_dir(tmp_path, "test")  # a real suite run produces results — output dir created


def test_suite_fail_exits_nonzero(tmp_path: Path) -> None:
    """A run with a failing test exits non-zero (OTTO_E2E_FAIL=1 triggers the failure)."""
    r = run_otto(
        ["--lab", "unix", "test", "TestE2EFixture"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        extra_env={"OTTO_E2E_FAIL": "1"},
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert_output_dir(tmp_path, "test")  # the suite still ran — output dir created


def test_unknown_name_clean_usage_error(tmp_path: Path) -> None:
    """An unknown test name exits 2 with did-you-mean, and prints no Python traceback.

    A name is resolved by the run's own collection, after the run directory is
    made, so (unlike a missing name or ``-m``) it does leave a run directory
    holding the log of that collection.
    """
    r = run_otto(
        ["--lab", "unix", "test", "NoSuchTest"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback (most recent call last)" not in (r.stdout + r.stderr)


def test_no_name_is_a_usage_error_before_any_run(tmp_path: Path) -> None:
    """``otto test`` with neither a name nor ``-m`` exits 2 and makes no run directory."""
    r = run_otto(["--lab", "unix", "test"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 2, r.stdout + r.stderr
    assert_no_output_dir(tmp_path)
