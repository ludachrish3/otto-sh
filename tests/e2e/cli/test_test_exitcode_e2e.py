"""End-to-end tests for ``otto test`` exit-code contract.

Verifies that suite runs propagate the pytest exit code correctly:
- a passing suite exits 0
- a failing suite exits non-zero
- an unknown suite name exits non-zero without printing a traceback

These serve as regression guards for the Task-2.6 fix: exit-code propagation
from the inner pytest.main() call.

All tests use ``--lab veggies`` (required for suite runs) but ``TestE2EFixture``
requests no host, so the suite itself is hostless; no Vagrant VM is contacted.
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
    """A suite whose tests all pass exits 0."""
    r = run_otto(
        ["--lab", "veggies", "test", "TestE2EFixture"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert_output_dir(tmp_path, "test")  # a real suite run produces results — output dir created


def test_suite_fail_exits_nonzero(tmp_path: Path) -> None:
    """A suite with a failing test exits non-zero (OTTO_E2E_FAIL=1 triggers the failure)."""
    r = run_otto(
        ["--lab", "veggies", "test", "TestE2EFixture"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        extra_env={"OTTO_E2E_FAIL": "1"},
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert_output_dir(tmp_path, "test")  # the suite still ran — output dir created


def test_unknown_suite_clean_error_nonzero(tmp_path: Path) -> None:
    """An unknown suite name exits non-zero and does NOT print a Python traceback."""
    r = run_otto(
        ["--lab", "veggies", "test", "NoSuchSuite"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
    )
    assert r.returncode != 0
    assert "Traceback (most recent call last)" not in (r.stdout + r.stderr)
    assert_no_output_dir(tmp_path)  # unknown suite errors before any run — no output dir
