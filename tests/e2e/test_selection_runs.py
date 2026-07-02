"""``otto test --tests a,b`` and ``-m`` alone: suite-less selection runs.

No suite subcommand is required — exact test names (optionally
``Class::name`` qualified) and/or a marker expression are resolved against
every repo's collected tests, and pytest runs once per repo whose selection
matched. Plain pytest functions are first-class runnable targets too.
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import run_otto
from tests.e2e._selection_fixtures import FAILING_SUITE_SRC, PLAIN_SUITE_SRC, make_selection_repo

pytestmark = pytest.mark.hostless


def _testcase_count(junit_path: Path) -> int:
    tree = ET.parse(junit_path)  # noqa: S314 — trusted output written by our own subprocess run
    return len(tree.getroot().findall(".//testcase"))


def _junit_files(xdir: Path) -> list[Path]:
    """All junit_*.xml / junit.xml files under the most recent otto test output dir."""
    test_root = xdir / "test"
    run_dirs = sorted(p for p in test_root.iterdir() if p.is_dir())
    assert run_dirs, f"no otto test output dir under {test_root}"
    return sorted(run_dirs[-1].glob("junit*.xml"))


def test_tests_flag_runs_named_tests_across_suites(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "--tests", "test_alpha_one,test_beta_one"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 2


def test_plain_function_runs_via_tests_flag(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "--tests", "test_plain_function"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 1


def test_qualified_name_selects_one_suite(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "--tests", "TestAlpha::test_alpha_one"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 1


def test_marker_alone_runs_both_suites(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "-m", "shared"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 2


def test_unknown_name_is_loud_with_suggestion(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "--tests", "test_alpha_won"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    # Rich wraps the error panel to terminal width and can insert a
    # box-drawing border character mid-phrase with no surrounding whitespace
    # (e.g. "...(did \xe2\x94\x82\n\xe2\x94\x82 you mean..."); strip those
    # border glyphs before collapsing whitespace so wrapping can't hide the
    # substring.
    raw = (r.stdout + r.stderr).replace("│", " ")
    combined = " ".join(raw.split())
    assert r.returncode != 0
    assert "did you mean" in combined.lower()
    assert "test_alpha_one" in combined


def test_bare_otto_test_still_shows_help(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Usage" in r.stdout
    test_root = xdir / "test"
    assert not test_root.is_dir() or not list(test_root.iterdir())


def test_stability_mode_works_on_selection(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "-i", "2", "--tests", "test_plain_function"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Stability Results" in r.stdout


def test_multi_repo_selection_runs_one_session_per_repo(tmp_path: Path) -> None:
    repo_a = make_selection_repo(tmp_path, name="repoA", suite_src=PLAIN_SUITE_SRC)
    repo_b = make_selection_repo(tmp_path, name="repoB", suite_src=PLAIN_SUITE_SRC, with_lab=False)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    sut_dirs = f"{repo_a}{os.pathsep}{repo_b}"
    r = run_otto(
        ["test", "--tests", "test_plain_function"],
        xdir=xdir,
        lab="veggies",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    junit_files = _junit_files(xdir)
    names = {p.name for p in junit_files}
    assert names == {"junit_repoA.xml", "junit_repoB.xml"}
    for junit in junit_files:
        assert _testcase_count(junit) == 1


def test_multi_repo_worst_exit_code_wins(tmp_path: Path) -> None:
    repo_a = make_selection_repo(tmp_path, name="repoA", suite_src=PLAIN_SUITE_SRC)
    repo_b = make_selection_repo(
        tmp_path, name="repoB", suite_src=FAILING_SUITE_SRC, with_lab=False
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    sut_dirs = f"{repo_a}{os.pathsep}{repo_b}"
    r = run_otto(
        ["test", "--tests", "test_plain_function"],
        xdir=xdir,
        lab="veggies",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode != 0
    junit_files = _junit_files(xdir)
    names = {p.name for p in junit_files}
    assert names == {"junit_repoA.xml", "junit_repoB.xml"}
    junit_a = next(p for p in junit_files if p.name == "junit_repoA.xml")
    assert _testcase_count(junit_a) == 1
