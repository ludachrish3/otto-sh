"""otto_kgcov toolchain matrix: the kmod coverage e2e once per compiler, on the unix bed.

    OTTO_KGCOV_TOOLCHAINS=gcc-9,gcc-10,gcc-11,gcc-12,gcc-13,gcc-14,clang \\
        uv run pytest tests/e2e/cov/test_kgcov_toolchains_e2e.py -n0 --no-cov

(`make kgcov` sets the variable from KGCOV_TOOLCHAINS and runs this together
with the cross build.) For each compiler the kernel-module fixture is rebuilt
for the running kernel — CC=<name> for a gcc, with the older-gcc overrides
when the kernel was configured for a newer one; LLVM=1 with the gcc-kernel
overrides for clang (tests/e2e/cov/_repo5_build.py has both) — the demo's
.init_array bracket is checked in the .ko, and the routine kmod e2e's
compiler-independent assertions run through the shared helpers on a real
``otto test --cov`` run. otto reads each host's counters with the gcov the
counters' own stamp names (``gcov-N`` for a gcc, ``llvm-cov`` for clang, from
PATH), so the bed hosts are left unconfigured for every gcc and the run's
green is that discovery's proof; clang's arm alone layers an overlay SUT repo
over repo5 with OTTO_SUT_DIRS (tests/e2e/cov/_repo5_build.py) naming an lcov
wrapper that ignores the kernel headers llvm-cov reports relative to the
kernel tree. Unset or empty, the variable means the system's
default compiler, so this module never collects an empty, skipped parameter
set; a compiler that is not installed FAILS the run naming it — a release
must be able to trust a green here, so nothing skips. Carries `kgcov`:
excluded from every default lane, selected by `make kgcov`, which
`make release` invokes. Pinned to one xdist worker like the routine e2e:
both hosts' modules and /var/cov/otto_kmod_demo are shared bed state.
"""

import os
import re
import subprocess

import pytest

from otto.coverage.store.model import CoverageStore
from tests._ambient_env import ambient
from tests.e2e._otto_subprocess import REPO5, run_otto
from tests.e2e.cov._kmod_assertions import (
    assert_exit_routine_lines_are_hit_once_per_host,
    assert_mid_suite_dump_does_not_double_count,
    assert_only_the_demo_is_fetched_from_the_two_hosts,
    assert_parse_hits_that_do_not_depend_on_folding,
    assert_policy_paths_have_the_expected_hits,
    assert_policy_switch_records_branches,
    assert_run_log_reports_the_library_uninstrumented,
    assert_store_has_the_three_demo_files,
    assert_three_gcda_per_host,
)
from tests.e2e.cov._repo5_build import (
    BUILD,
    DEMO_SRC,
    ensure_kmod_artifacts,
    init_array_symbols,
    overlay_repo,
    toolchain,
)

pytestmark = [pytest.mark.integration, pytest.mark.kgcov, pytest.mark.xdist_group("coverage_e2e")]

_LAB = "unix"
_NAMES = [n.strip() for n in ambient("OTTO_KGCOV_TOOLCHAINS", "").split(",") if n.strip()] or [
    "default"
]
_GCOV_CONSTRUCTORS = {"_sub_I_00100_0", "__llvm_gcov_init"}


@pytest.fixture(scope="module", params=_NAMES, ids=_NAMES)
def built_with(request, tmp_path_factory):
    """The kernel-module artifact set, rebuilt with this parameter's compiler."""
    tc = toolchain(request.param)
    ensure_kmod_artifacts(tmp_path_factory, tc)
    return tc


def _run_otto(argv, *, xdir, timeout, overlay=None):
    # `sut_dirs` takes one Path; the two-repo layout goes through extra_env,
    # which run_otto merges last. repo5 FIRST and the overlay second: the
    # composite reads lab sources in OTTO_SUT_DIRS order and the later one's
    # elements win, which is the whole point of the overlay.
    env = {"OTTO_SUT_DIRS": os.pathsep.join([str(REPO5), str(overlay)])} if overlay else None
    result = run_otto(argv, xdir=xdir, sut_dirs=REPO5, lab=_LAB, timeout=timeout, extra_env=env)
    if result.returncode != 0:
        raise AssertionError(
            f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def coverage_run(built_with, tmp_path_factory):
    tmp = tmp_path_factory.mktemp(f"kgcov_{built_with.name}")
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    overlay = overlay_repo(built_with, tmp / "overlay")
    _run_otto(
        ["test", "--cov", "--cov-clean", "TestKmodDemo"],
        xdir=xdir,
        timeout=900,
        overlay=overlay,
    )
    (log_dir,) = sorted((xdir / "test").glob("*"))
    cov_dir = log_dir / "cov"
    _run_otto(
        ["cov", "report", str(log_dir), "--dir", str(report_dir)],
        xdir=xdir,
        timeout=120,
        overlay=overlay,
    )
    store = CoverageStore.load(report_dir / "store.json")
    return store, cov_dir, log_dir


def _comment(ko) -> str:
    return subprocess.run(
        ["readelf", "-p", ".comment", str(ko)], check=True, capture_output=True, text=True
    ).stdout


class TestBuild:
    def test_both_modules_name_the_requested_compiler(self, built_with):
        for ko in (BUILD / "lib" / "otto_kgcov.ko", DEMO_SRC / "otto_kmod_demo.ko"):
            comment = _comment(ko)
            if built_with.name == "clang":
                assert "clang version" in comment, comment
            elif built_with.name.startswith("gcc-"):
                major = built_with.name.split("-", 1)[1]
                assert re.search(rf"GCC: \([^)]*\) {major}\.", comment), comment
            else:
                assert "GCC" in comment or "clang version" in comment, comment

    def test_the_init_array_is_bracketed_by_the_sentinels(self, built_with):
        syms = init_array_symbols(DEMO_SRC / "otto_kmod_demo.ko")
        assert syms[0] == "__kgcov_begin_marker", syms
        assert syms[-1] == "__kgcov_end_marker", syms
        # Three instrumented units, one gcov constructor each, all from the
        # same compiler family.
        assert len(syms[1:-1]) == 3, syms
        assert len(set(syms[1:-1])) == 1, syms
        assert syms[1] in _GCOV_CONSTRUCTORS, syms


class TestCoverage:
    def test_only_the_demo_is_fetched_from_the_two_hosts(self, coverage_run):
        assert_only_the_demo_is_fetched_from_the_two_hosts(coverage_run[1])

    def test_three_gcda_per_host(self, coverage_run):
        assert_three_gcda_per_host(coverage_run[1])

    def test_the_run_log_says_the_library_is_not_instrumented(self, coverage_run):
        assert_run_log_reports_the_library_uninstrumented(coverage_run[2])

    def test_store_has_the_three_demo_files(self, coverage_run):
        assert_store_has_the_three_demo_files(coverage_run[0])

    def test_policy_paths_have_the_expected_hits(self, coverage_run):
        assert_policy_paths_have_the_expected_hits(coverage_run[0])

    def test_parse_hits(self, coverage_run):
        assert_parse_hits_that_do_not_depend_on_folding(coverage_run[0])

    def test_exit_routine_lines_are_hit_once_per_host(self, coverage_run):
        assert_exit_routine_lines_are_hit_once_per_host(coverage_run[0])

    def test_branches_are_recorded_for_the_policy_switch(self, coverage_run):
        assert_policy_switch_records_branches(coverage_run[0])

    def test_the_mid_suite_dump_does_not_double_count(self, coverage_run):
        assert_mid_suite_dump_does_not_double_count(coverage_run[1])
