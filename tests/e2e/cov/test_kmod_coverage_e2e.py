"""End-to-end: kernel-module coverage through otto_kgcov on the unix bed.

    otto -l unix test --cov TestKmodDemo      (repo5)
    otto -l unix cov report <run> --dir <report>

Prerequisites: test1/test2 up (6.8.0-86-generic), the headers for that
release on the dev VM, gcc and lcov on the dev VM. The modules are built by
``tests/repo5/kmod/build.sh`` when missing. Pinned to one xdist worker: both
hosts' modules and /var/cov/otto_kmod_demo are shared bed state.
"""

import subprocess

import pytest

from otto.coverage.store.model import CoverageStore
from tests.e2e._otto_subprocess import REPO5, run_otto
from tests.e2e.cov._kmod_assertions import (
    DEMO,
    assert_exit_routine_lines_are_hit_once_per_host,
    assert_mid_suite_dump_does_not_double_count,
    assert_only_the_demo_is_fetched_from_the_two_hosts,
    assert_parse_hits_that_do_not_depend_on_folding,
    assert_policy_paths_have_the_expected_hits,
    assert_policy_switch_records_branches,
    assert_run_log_reports_the_library_loaded_on_demand,
    assert_store_has_the_three_demo_files,
    assert_three_gcda_per_host,
)
from tests.e2e.cov._repo5_build import DEMO_SRC, _line_of, _record, ensure_kmod_artifacts

_LAB = "unix"


@pytest.fixture(scope="module")
def built_modules(tmp_path_factory):
    """Build the kernel-module artifact set when stale."""
    ensure_kmod_artifacts(tmp_path_factory)
    demo_ko = DEMO_SRC / f"{DEMO}.ko"
    release = subprocess.run(
        ["uname", "-r"], check=True, capture_output=True, text=True
    ).stdout.strip()
    vermagic = subprocess.run(
        ["modinfo", "-F", "vermagic", str(demo_ko)], check=True, capture_output=True, text=True
    ).stdout
    assert vermagic.startswith(release + " "), f"demo built for {vermagic!r}, dev VM runs {release}"


def _run_otto(argv, *, xdir, timeout):
    result = run_otto(argv, xdir=xdir, sut_dirs=REPO5, lab=_LAB, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(
            f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def coverage_run(built_modules, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kmod_e2e")
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    _run_otto(["test", "--cov", "--cov-clean", "TestKmodDemo"], xdir=xdir, timeout=900)
    (log_dir,) = sorted((xdir / "test").glob("*"))
    cov_dir = log_dir / "cov"
    _run_otto(["cov", "report", str(log_dir), "--dir", str(report_dir)], xdir=xdir, timeout=120)
    store = CoverageStore.load(report_dir / "store.json")
    return store, cov_dir, log_dir


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestKmodFetchTree:
    def test_only_the_demo_is_fetched_and_only_from_the_two_hosts(self, coverage_run):
        assert_only_the_demo_is_fetched_from_the_two_hosts(coverage_run[1])

    def test_three_gcda_per_host_one_per_translation_unit(self, coverage_run):
        assert_three_gcda_per_host(coverage_run[1])

    def test_the_run_log_says_the_library_was_loaded_for_the_demo(self, coverage_run):
        assert_run_log_reports_the_library_loaded_on_demand(coverage_run[2])


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestKmodLineAndBranchHits:
    """Exact counts: both hosts run COMMON; test1 adds TEST1_ONLY, test2 TEST2_ONLY.

    Both hosts' mixes end with one more ``enqueue`` (repo5's suite), so the
    queue is non-empty when the suite's teardown unloads the module — the
    exit-time drain and its ``pr_info`` line run once per host, not zero.
    """

    def test_store_has_the_three_demo_files(self, coverage_run):
        assert_store_has_the_three_demo_files(coverage_run[0])

    def test_policy_paths_have_the_expected_hits(self, coverage_run):
        assert_policy_paths_have_the_expected_hits(coverage_run[0])

    def test_parse_errors_have_the_expected_hits(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "demo_parse.c")
        src = DEMO_SRC / "demo_parse.c"
        # `return -ERANGE;` is a single-statement body the compiler folds into
        # its guarding `if`, so "taken exactly once" (test1's `limit 999`) is
        # read off that condition line's branch data.
        erange_if = _line_of(src, 'if (!strcmp(cmd, "limit") && (v < 1 || v > 64))')
        assert erange_if in rec.lines, f"{rec.path}:{erange_if} carries no coverage data"
        erange_branches = rec.lines[erange_if].branches
        # gcc folds `v < 1 || v > 64` (in series with the `!strcmp` check) into
        # one 4-arc compare; if a future compiler changes that fold, this pin
        # fails loudly instead of the hit-count assertion below misreading it.
        # A gcc-shaped pin, which is why it stays here rather than in the
        # shared assertions the toolchain matrix runs for every compiler.
        assert len(erange_branches) == 4, erange_branches
        erange_hits = sorted(b.hits.for_tier("system") for b in erange_branches)
        assert erange_hits[0] == 1, f"demo_parse.c:{erange_if} branches: {erange_hits}"
        assert_parse_hits_that_do_not_depend_on_folding(store)

    def test_exit_routine_lines_are_hit_once_per_host(self, coverage_run):
        assert_exit_routine_lines_are_hit_once_per_host(coverage_run[0])

    def test_branches_are_recorded_for_the_policy_switch(self, coverage_run):
        assert_policy_switch_records_branches(coverage_run[0])

    def test_the_mid_suite_dump_does_not_double_count_against_the_exit_dump(self, coverage_run):
        assert_mid_suite_dump_does_not_double_count(coverage_run[1])
