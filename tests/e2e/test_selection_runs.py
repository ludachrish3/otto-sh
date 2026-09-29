"""``otto test a b`` and ``-m`` alone: selection runs.

Test names (a test, a class, or ``Class::name``) and/or a marker expression
select tests inside each repo's own pytest session, and a repo whose session
had nothing to run takes no part in the result. Plain pytest functions are first-class runnable
targets too.
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


def test_names_run_named_tests_across_classes(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "test_alpha_one", "test_beta_one"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 2


def test_plain_function_runs_by_name(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "test_plain_function"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 1


def test_qualified_name_selects_one_class(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "TestAlpha::test_alpha_one"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 1


def test_marker_alone_runs_both_classes(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "-m", "shared"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 2


def test_unknown_name_is_loud_with_suggestion(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "test_alpha_won"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
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


def test_a_class_name_selects_every_test_in_it(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "TestAlpha"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    [junit] = _junit_files(xdir)
    assert _testcase_count(junit) == 2


def test_bare_otto_test_is_a_usage_error(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "at least one test name or -m" in " ".join((r.stdout + r.stderr).split())
    test_root = xdir / "test"
    assert not test_root.is_dir() or not list(test_root.iterdir())


def test_stability_mode_works_on_selection(tmp_path: Path) -> None:
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    r = run_otto(
        ["test", "-i", "2", "test_plain_function"],
        xdir=xdir,
        sut_dirs=repo,
        lab="unix",
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
        ["test", "test_plain_function"],
        xdir=xdir,
        lab="unix",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    junit_files = _junit_files(xdir)
    names = {p.name for p in junit_files}
    assert names == {"junit_repoA.xml", "junit_repoB.xml"}
    for junit in junit_files:
        assert _testcase_count(junit) == 1


def test_multi_repo_explicit_results_fans_out_per_repo(tmp_path: Path) -> None:
    # An explicit --results path in a multi-repo selection must not have every
    # repo's session clobber the same file — each participating repo's junit
    # gets its own name derived from the --results stem.
    repo_a = make_selection_repo(tmp_path, name="repoA", suite_src=PLAIN_SUITE_SRC)
    repo_b = make_selection_repo(tmp_path, name="repoB", suite_src=PLAIN_SUITE_SRC, with_lab=False)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    sut_dirs = f"{repo_a}{os.pathsep}{repo_b}"
    results_path = xdir / "custom.xml"
    r = run_otto(
        ["test", "test_plain_function", "--results", str(results_path)],
        xdir=xdir,
        lab="unix",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    custom_files = sorted(xdir.glob("custom*.xml"))
    names = {p.name for p in custom_files}
    assert names == {"custom_repoA.xml", "custom_repoB.xml"}
    for junit in custom_files:
        assert _testcase_count(junit) == 1


def test_marker_alone_leaves_nothing_for_a_repo_without_matches(tmp_path: Path) -> None:
    # repo_a's SUITE_SRC has @pytest.mark.shared tests; repo_b's PLAIN_SUITE_SRC
    # has none. Both repos are searched, one session each; repo_b's matches
    # nothing, which is no match there, not a failure of the run, and leaves
    # no JUnit file behind.
    repo_a = make_selection_repo(tmp_path, name="repoA")
    repo_b = make_selection_repo(tmp_path, name="repoB", suite_src=PLAIN_SUITE_SRC, with_lab=False)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    sut_dirs = f"{repo_a}{os.pathsep}{repo_b}"
    r = run_otto(
        ["test", "-m", "shared"],
        xdir=xdir,
        lab="unix",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    # Two repos searched -> the per-repo JUnit name, whichever of them matched.
    [junit] = _junit_files(xdir)
    assert junit.name == "junit_repoA.xml"
    assert _testcase_count(junit) == 2


def test_a_warm_run_still_finds_the_names(tmp_path: Path) -> None:
    # The second run reads the collected-tests table the first one wrote and
    # collects only the file holding the name.
    repo = make_selection_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    for _ in range(2):
        r = run_otto(["test", "TestAlpha"], xdir=xdir, sut_dirs=repo, lab="unix")
        assert r.returncode == 0, r.stdout + r.stderr
        [junit] = _junit_files(xdir)
        assert _testcase_count(junit) == 2


def test_multi_repo_worst_exit_code_wins(tmp_path: Path) -> None:
    repo_a = make_selection_repo(tmp_path, name="repoA", suite_src=PLAIN_SUITE_SRC)
    repo_b = make_selection_repo(
        tmp_path, name="repoB", suite_src=FAILING_SUITE_SRC, with_lab=False
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    sut_dirs = f"{repo_a}{os.pathsep}{repo_b}"
    r = run_otto(
        ["test", "test_plain_function"],
        xdir=xdir,
        lab="unix",
        extra_env={"OTTO_SUT_DIRS": sut_dirs},
    )
    assert r.returncode != 0
    junit_files = _junit_files(xdir)
    names = {p.name for p in junit_files}
    assert names == {"junit_repoA.xml", "junit_repoB.xml"}
    junit_a = next(p for p in junit_files if p.name == "junit_repoA.xml")
    assert _testcase_count(junit_a) == 1
