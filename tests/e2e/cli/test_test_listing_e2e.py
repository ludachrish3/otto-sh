"""End-to-end tests for ``otto test`` discovery flags (lab-free), and its dry run.

Verifies that ``--list-tests`` and ``--list-markers``
work without supplying ``--lab``, and that ``otto -n test`` prints what
pytest collected.  Each flag exits 0 and the stdout contains
the expected content derived from the ``project_e2e`` fixture project.

These serve as regression guards for the Task-2.5 fix: discovery flags are
lab-free and must not require a lab token to be configured.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import PROJECT_E2E, assert_no_output_dir, run_otto
from tests.e2e._scope_workspace import scoped_roots

pytestmark = pytest.mark.hostless


def test_list_tests_lists_gated_test_under_its_class(tmp_path: Path) -> None:
    """--list-tests exits 0 and shows the fixture class with its test under it."""
    # NO --lab: the discovery flags are lab-free.
    r = run_otto(["test", "--list-tests"], xdir=tmp_path, roots=PROJECT_E2E)
    assert r.returncode == 0, r.stderr
    assert "TestE2EFixture" in r.stdout
    assert "test_gated" in r.stdout
    assert r.stdout.index("TestE2EFixture") < r.stdout.index("test_gated")
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_list_markers_shows_what_pytest_registered(tmp_path: Path) -> None:
    """--list-markers lists the markers pytest knows: a plugin's (asyncio) included."""
    r = run_otto(["test", "--list-markers"], xdir=tmp_path, roots=PROJECT_E2E)
    assert r.returncode == 0, r.stderr
    # Panel header always contains the project name
    assert "project_e2e" in r.stdout
    assert "asyncio" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


def test_dry_run_prints_what_pytest_collected_and_runs_nothing(tmp_path: Path) -> None:
    """-n test NAME prints the collected test under its class."""
    r = run_otto(
        ["-n", "test", "TestE2EFixture"],
        xdir=tmp_path,
        roots=PROJECT_E2E,
        lab="unix",
    )
    assert r.returncode == 0, r.stderr
    assert "dry run: pytest collected these tests; nothing ran" in r.stdout
    assert r.stdout.index("TestE2EFixture") < r.stdout.index("test_gated")


def test_list_tests_class_scoped(tmp_path: Path) -> None:
    """--list-tests TestE2EFixture exits 0 and narrows output to that class's tests."""
    r = run_otto(["test", "--list-tests", "TestE2EFixture"], xdir=tmp_path, roots=PROJECT_E2E)
    assert r.returncode == 0, r.stderr
    assert "test_gated" in r.stdout
    # The class name keeps output within the fixture class
    assert "TestE2EFixture" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery flag — no run dir


# ── scope: only the projects that take part ──────────────────────────────────


def _otto(tmp_path: Path, argv: list[str], **kwargs):
    return run_otto(
        argv,
        xdir=tmp_path / "x",
        roots=None,
        extra_argv_prefix=["-R", *kwargs.pop("prefix", [])],
        extra_env={"OTTOPATH": scoped_roots(tmp_path), **kwargs.pop("env", {})},
        **kwargs,
    )


def test_e2e_list_tests_other_lab_omits_project(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["test", "--list-tests"], lab="alpha")
    assert r.returncode == 0, r.stderr
    assert "test_in_a" in r.stdout
    assert "project_b" not in r.stdout
    assert "test_in_b" not in r.stdout


def test_e2e_list_tests_otto_lab_env_narrows(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["test", "--list-tests"], env={"OTTO_LAB": "alpha"})
    assert r.returncode == 0, r.stderr
    assert "test_in_a" in r.stdout
    assert "test_in_b" not in r.stdout


def test_e2e_exclude_list_tests_omits_project(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["test", "--list-tests"], prefix=["-E", "project_b"])
    assert r.returncode == 0, r.stderr
    assert "test_in_a" in r.stdout
    assert "test_in_b" not in r.stdout
    assert "no lab selected, showing every project" in r.stderr


def test_e2e_test_name_in_inactive_project_reports_not_searched(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["test", "--list-tests", "test_only_b"], lab="alpha")
    blob = r.stdout + r.stderr
    assert r.returncode == 2, blob
    assert "project project_b was not searched" in blob
    assert "-I project_b" in blob


def test_e2e_list_tests_without_lab_lists_every_project(tmp_path: Path) -> None:
    """No lab: every project not switched off takes part, one declaring lab_patterns too."""
    r = _otto(tmp_path, ["test", "--list-tests"])
    assert r.returncode == 0, r.stderr
    assert "test_in_a" in r.stdout
    assert "test_in_b" in r.stdout
    assert "no lab selected, showing every project" in r.stderr
