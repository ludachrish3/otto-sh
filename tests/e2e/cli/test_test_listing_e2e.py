"""End-to-end tests for ``otto test`` discovery flags (lab-free), and its dry run.

Verifies that ``--list-tests`` and ``--list-markers``
work without supplying ``--lab``, and that ``otto -n test`` prints what
pytest collected.  Each flag exits 0 and the stdout contains
the expected content derived from the ``repo_e2e`` fixture repo.

These serve as regression guards for the Task-2.5 fix: discovery flags are
lab-free and must not require a lab token to be configured.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, assert_no_output_dir, run_otto

pytestmark = pytest.mark.hostless


def test_list_tests_lists_gated_test_under_its_class(tmp_path: Path) -> None:
    """--list-tests exits 0 and shows the fixture class with its test under it."""
    # NO --lab: the discovery flags are lab-free.
    r = run_otto(["test", "--list-tests"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "TestE2EFixture" in r.stdout
    assert "test_gated" in r.stdout
    assert r.stdout.index("TestE2EFixture") < r.stdout.index("test_gated")
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_list_markers_shows_what_pytest_registered(tmp_path: Path) -> None:
    """--list-markers lists the markers pytest knows: a plugin's (asyncio) included."""
    r = run_otto(["test", "--list-markers"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    # Panel header always contains the repo name
    assert "repo_e2e" in r.stdout
    assert "asyncio" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_dry_run_prints_what_pytest_collected_and_runs_nothing(tmp_path: Path) -> None:
    """-n test NAME prints the collected test under its class."""
    r = run_otto(
        ["-n", "test", "TestE2EFixture"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="unix",
    )
    assert r.returncode == 0, r.stderr
    assert "dry run: pytest collected these tests; nothing ran" in r.stdout
    assert r.stdout.index("TestE2EFixture") < r.stdout.index("test_gated")


def test_list_tests_class_scoped(tmp_path: Path) -> None:
    """--list-tests TestE2EFixture exits 0 and narrows output to that class's tests."""
    r = run_otto(["test", "--list-tests", "TestE2EFixture"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "test_gated" in r.stdout
    # The class name keeps output within the fixture class
    assert "TestE2EFixture" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir
