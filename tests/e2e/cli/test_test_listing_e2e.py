"""End-to-end tests for ``otto test`` discovery flags (lab-free).

Verifies that ``--list-suites``, ``--list-tests``, and ``--list-markers``
work without supplying ``--lab``.  Each flag exits 0 and the stdout contains
the expected content derived from the ``repo_e2e`` fixture repo.

These serve as regression guards for the Task-2.5 fix: discovery flags are
lab-free and must not require a lab token to be configured.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, assert_no_output_dir, run_otto

pytestmark = pytest.mark.hostless


def test_list_suites_lists_fixture_suite(tmp_path: Path) -> None:
    """--list-suites exits 0 and includes the fixture suite name."""
    # NO --lab: Task 2.5 makes the discovery flags lab-free.
    r = run_otto(["test", "--list-suites"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "TestE2EFixture" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_list_tests_lists_gated_test(tmp_path: Path) -> None:
    """--list-tests exits 0 and includes the fixture suite's test name."""
    r = run_otto(["test", "--list-tests"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "test_gated" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_list_markers_exits_zero(tmp_path: Path) -> None:
    """--list-markers exits 0 (repo_e2e has no pyproject.toml so emits the empty panel)."""
    # repo_e2e has no pyproject.toml → configured_markers() returns [].
    # The panel still renders with "(no markers configured)"; exit 0 is the contract.
    r = run_otto(["test", "--list-markers"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    # Panel header always contains the repo name
    assert "repo_e2e" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_list_tests_suite_scoped(tmp_path: Path) -> None:
    """--list-tests TestE2EFixture exits 0 and narrows output to that suite's tests."""
    r = run_otto(["test", "--list-tests", "TestE2EFixture"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "test_gated" in r.stdout
    # The suite selector keeps output within the fixture suite
    assert "TestE2EFixture" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir
