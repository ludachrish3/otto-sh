"""End-to-end: kernel-module coverage through otto_kmodcov on the unix bed.

    otto -l unix test --cov TestKmodDemo      (project5)
    otto -l unix cov report <run> --dir <report>

and the same for ``TestKmodReload``: two loads of the demo in one session.

Prerequisites: test1/test2 up (6.8.0-86-generic), the headers for that
release on the dev VM, gcc and lcov on the dev VM. The modules are built by
``tests/project5/kmod/build.sh`` when missing. Pinned to one xdist worker: both
hosts' modules and /var/cov/otto_kmod_demo are shared bed state.
"""

import subprocess

import pytest

from otto import layout
from otto.coverage.capture.model import Capture
from otto.coverage.store.model import CoverageStore
from tests.e2e._otto_subprocess import PROJECT5, run_otto
from tests.e2e.cov._kmod_assertions import (
    DEMO,
    HOSTS,
    assert_exit_routine_lines_are_hit_once_per_host,
    assert_mid_suite_dump_does_not_double_count,
    assert_only_the_demo_is_fetched_from_the_two_hosts,
    assert_parse_hits_that_do_not_depend_on_folding,
    assert_policy_paths_have_the_expected_hits,
    assert_policy_switch_records_branches,
    assert_run_log_reports_the_library_loaded_on_demand,
    assert_store_has_the_three_demo_files,
    assert_three_gcda_per_host,
    capture_file,
)
from tests.e2e.cov._project5_build import (
    BUILD,
    DEMO_SRC,
    _hits,
    _line_of,
    _record,
    ensure_kmod_artifacts,
)

RELOAD_CYCLES = 10
"""What tests/project5/tests/test_kmod_demo.py's RELOAD_CYCLES is: enqueues per load."""

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
    # Interface 2's structural proof: the control files are sysfs, so the
    # built library imports no debugfs symbol. No bed lacks debugfs, which is
    # why the proof is the import table rather than a run without it.
    undefined = subprocess.run(
        ["nm", "--undefined-only", str(BUILD / "lib" / "otto_kmodcov.ko")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    debugfs = [line for line in undefined.splitlines() if "debugfs" in line]
    assert debugfs == [], f"otto_kmodcov.ko imports debugfs symbols: {debugfs}"


def _run_otto(argv, *, xdir, timeout):
    result = run_otto(argv, xdir=xdir, roots=PROJECT5, lab=_LAB, timeout=timeout)
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
    (log_dir,) = sorted(layout.command_output_dir(xdir, "test").glob("*"))
    cov_dir = layout.cov_dir(log_dir)
    _run_otto(["cov", "report", str(log_dir), "--dir", str(report_dir)], xdir=xdir, timeout=120)
    store = CoverageStore.load(report_dir / "store.json")
    return store, cov_dir, log_dir


@pytest.fixture(scope="module")
def reload_run(built_modules, tmp_path_factory):
    """``TestKmodReload``: two loads of the demo per host in one session, each unloaded."""
    tmp = tmp_path_factory.mktemp("kmod_reload_e2e")
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    _run_otto(["test", "--cov", "--cov-clean", "TestKmodReload"], xdir=xdir, timeout=900)
    (log_dir,) = sorted(layout.command_output_dir(xdir, "test").glob("*"))
    _run_otto(["cov", "report", str(log_dir), "--dir", str(report_dir)], xdir=xdir, timeout=120)
    return CoverageStore.load(report_dir / "store.json"), layout.cov_dir(log_dir)


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestKmodReload:
    """#670: a module unloaded and loaded again keeps the first load's counts.

    Each load enqueues RELOAD_CYCLES values (tests/project5/tests/test_kmod_demo.py),
    so per host the enqueue line reads two loads' worth, and the init and exit
    routines' lines read two — one per load. Before the fix the second load's
    first dump overwrote the first load's exit dump, and each read one load.
    """

    def test_each_hosts_capture_holds_both_loads(self, reload_run):
        _store, cov_dir = reload_run
        policy, main = DEMO_SRC / "demo_policy.c", DEMO_SRC / "demo_main.c"
        expected = {
            ("demo_policy.c", _line_of(policy, "q->enqueued++;")): 2 * RELOAD_CYCLES,
            ("demo_main.c", _line_of(main, 'pr_info("loaded, queue capacity')): 2,
            ("demo_main.c", _line_of(main, 'pr_info("unloaded after')): 2,
        }
        for host in HOSTS:
            capture = Capture.load(cov_dir / host / DEMO / "capture.json")
            for (name, lineno), hits in expected.items():
                cov = capture_file(capture, name)
                assert cov is not None, (host, list(capture.files))
                assert cov.lines.get(lineno) == hits, (host, name, lineno, cov.lines)

    def test_the_report_sums_both_loads_on_both_hosts(self, reload_run):
        store, _cov_dir = reload_run
        rec = _record(store, "demo_policy.c")
        lineno = _line_of(DEMO_SRC / "demo_policy.c", "q->enqueued++;")
        assert _hits(rec, lineno) == 2 * RELOAD_CYCLES * len(HOSTS)


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

    Both hosts' mixes end with one more ``enqueue`` (project5's suite), so the
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
