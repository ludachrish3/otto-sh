"""End-to-end tests for otto's coverage reporting pipeline.

Emulates the real user workflow::

    otto -l veggies test --cov TestCoverageProduct
    otto -l veggies cov <log_dir> --report ./report

Both stages run as real subprocesses so every code path a user exercises
(argv parsing, Typer wiring, ``--lab`` resolution, configmodule init,
logger output-dir creation, ``_run_coverage`` fetch, ``CoverageReporter``)
is covered. Subprocess coverage is captured via ``coverage.process_startup()``
invoked from ``tests/_coverage_bootstrap/sitecustomize.py`` — the same
mechanism ``tests/unit/configmodule/test_completion_cache.py`` uses.

**Prerequisites**:

- Vagrant test VMs ``carrot``, ``tomato``, ``pepper`` must be running.
- ``gcc`` and ``lcov`` must be installed on the dev VM.

**Running**::

    uv run pytest tests/unit/cov/test_coverage_e2e.py -m integration -v \\
        --override-ini 'addopts='

All tests carry ``@pytest.mark.xdist_group("coverage_e2e")`` so pytest-xdist
pins them to a single worker. Without this pinning, concurrent workers
would race on the shared Vagrant VMs (both running ``_cov_clean_remotes``
and ``_run_coverage`` against the same ``/var/coverage/product`` directory),
which deadlocks asyncssh transfers.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from otto.coverage.store.model import CoverageStore, FileRecord

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1_DIR = PROJECT_ROOT / "tests" / "repo1"
PRODUCT_DIR = REPO1_DIR / "product"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"
OTTO_BIN = Path(sys.executable).parent / "otto"


# ---------------------------------------------------------------------------
# Subprocess runner — mirrors the pattern in test_completion_cache.py
# ---------------------------------------------------------------------------

def _otto_env(xdir: Path) -> dict[str, str]:
    """Env for an ``otto`` subprocess with subprocess-coverage enabled."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(REPO1_DIR),
        "OTTO_XDIR": str(xdir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }


def _run_otto(
    argv: list[str],
    *,
    xdir: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto ARGV`` and fail loudly if it exits non-zero."""
    result = subprocess.run(
        [str(OTTO_BIN), *argv],
        env=_otto_env(xdir),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"otto {' '.join(argv)} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return result


def _find_test_log_dir(xdir: Path) -> Path:
    """Locate the timestamped output dir ``otto test`` created under xdir.

    Logger layout: ``<xdir>/test/<YYYYMMDD_HHMMSS_mmm>_<subcommand>``.
    Only one run happens per fixture invocation, so the glob is
    unambiguous.
    """
    candidates = sorted((xdir / "test").glob("*"))
    if len(candidates) != 1:
        raise AssertionError(
            f"Expected exactly one ``test`` output dir under {xdir}, "
            f"found {candidates}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Fixture: run the real CLI, return (store, report_dir, cov_dir)
# ---------------------------------------------------------------------------

def _find_file_record(store: CoverageStore, suffix: str) -> FileRecord:
    for fr in store.files():
        if str(fr.path).endswith(suffix):
            return fr
    raise AssertionError(f"No FileRecord ending with {suffix!r} in store")


def _file_link(path: str) -> str:
    """Replicate HtmlRenderer._file_link mangling."""
    safe = path.replace("/", "_").replace("\\", "_").lstrip("_")
    return f"files/{safe}.html"


@pytest.fixture(scope="module")
def coverage_run(tmp_path_factory):
    """Run the real ``otto test --cov`` + ``otto cov`` pipeline.

    Returns ``(store, report_dir, cov_dir)``:
      - ``store``: ``CoverageStore`` loaded from ``store.json`` that
        ``CoverageReporter.run()`` persists next to ``index.html``.
      - ``report_dir``: HTML report directory.
      - ``cov_dir``: the ``cov/`` subdirectory inside the ``otto test``
        log dir that holds per-host ``.gcda`` files.
    """
    tmp_dir = tmp_path_factory.mktemp("coverage_e2e")
    xdir = tmp_dir / "xdir"
    xdir.mkdir()
    report_dir = tmp_dir / "report"

    # Stage 1 — run the suite and fetch .gcda files from the remotes.
    _run_otto(
        ["-l", "veggies", "test", "--cov", "TestCoverageProduct"],
        xdir=xdir,
        timeout=600,
    )

    log_dir = _find_test_log_dir(xdir)
    cov_dir = log_dir / "cov"
    assert cov_dir.is_dir(), f"Expected {cov_dir} after coverage fetch"

    # Stage 2 — render the HTML report and persist the store JSON.
    _run_otto(
        ["-l", "veggies", "cov", "report", str(log_dir), "--report", str(report_dir)],
        xdir=xdir,
        timeout=120,
    )

    store_path = report_dir / "store.json"
    assert store_path.is_file(), (
        f"CoverageReporter did not write {store_path}"
    )
    store = CoverageStore.load(store_path)

    yield store, report_dir, cov_dir


# ---------------------------------------------------------------------------
# Test Class 1: HTML Report Structure
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestHtmlReportStructure:
    """Verify the HTML report has the correct file structure and content."""

    def test_index_html_exists(self, coverage_run):
        _, report_dir, *_ = coverage_run
        index = report_dir / "index.html"
        assert index.exists(), "index.html was not generated"
        assert index.stat().st_size > 0, "index.html is empty"

    def test_per_file_html_for_math_ops(self, coverage_run):
        store, report_dir, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        link = _file_link(str(rec.path))
        assert (report_dir / link).exists(), f"Per-file page not found at {link}"

    def test_per_file_html_for_main_c(self, coverage_run):
        store, report_dir, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        link = _file_link(str(rec.path))
        assert (report_dir / link).exists(), f"Per-file page not found at {link}"

    def test_exactly_two_file_pages(self, coverage_run):
        _, report_dir, *_ = coverage_run
        files_dir = report_dir / "files"
        assert files_dir.is_dir(), "files/ directory not created"
        html_files = list(files_dir.glob("*.html"))
        assert len(html_files) == 2, (
            f"Expected 2 per-file pages, found {len(html_files)}: {html_files}"
        )

    def test_index_links_to_file_pages(self, coverage_run):
        store, report_dir, *_ = coverage_run
        index_html = (report_dir / "index.html").read_text()
        for suffix in ("math_ops.c", "main.c"):
            rec = _find_file_record(store, suffix)
            link = _file_link(str(rec.path))
            assert link in index_html, f"index.html missing link to {link}"

    def test_index_shows_nonzero_system_pct(self, coverage_run):
        store, report_dir, *_ = coverage_run
        index_html = (report_dir / "index.html").read_text()
        # Summary table row carries a "tier-system" pill followed by numeric cells.
        assert "tier-system" in index_html, "System tier pill missing from summary"
        # Store should agree the system coverage is non-zero.
        assert store.overall_pct("system") > 0

    def test_file_html_has_all_source_lines(self, coverage_run):
        store, report_dir, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        link = _file_link(str(rec.path))
        html = (report_dir / link).read_text()
        # Source rows carry class="line ..." — the leading space disambiguates
        # from summary-table rows which use class="summary-row ...".
        tr_count = html.count('<tr class="line ')
        source_line_count = len(
            PRODUCT_DIR.joinpath("math_ops.c").read_text().splitlines()
        )
        assert tr_count == source_line_count, (
            f"Expected {source_line_count} <tr> rows, found {tr_count}"
        )

    def test_file_html_contains_branch_pills(self, coverage_run):
        store, report_dir, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        link = _file_link(str(rec.path))
        html = (report_dir / link).read_text()
        assert "branch-pill" in html, "No branch pills found"
        assert "branch-taken" in html, "No taken branch pills found"

    def test_html_hit_counts_match_store(self, coverage_run):
        store, report_dir, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        link = _file_link(str(rec.path))
        html = (report_dir / link).read_text()

        expected_hits = rec.lines[4].hits.for_tier("system")
        assert expected_hits > 0

        # The system-tier hit count is the first "hits hits-system" cell
        # following line #4's lineno cell.  The template emits one such
        # cell per configured tier in precedence order.
        pattern = (
            r'<td class="lineno">4</td>\s*'
            r'<td class="hits hits-system">(\d+)</td>'
        )
        match = re.search(pattern, html)
        assert match, "Could not find hits-system cell for line 4"
        assert int(match.group(1)) == expected_hits


# ---------------------------------------------------------------------------
# Test Class 2: Line Hit Counts
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestLineHitCounts:
    """Verify exact line-level hit counts in the CoverageStore."""

    # 3 hosts: carrot, tomato, pepper
    # All hosts run: add, sub, mul, div (1 call each)
    # First host (carrot): extra div 1 0 (divide-by-zero)
    # Last host (pepper): 3 clamp calls (below, above, in-range)
    MATH_OPS_EXPECTED = {
        4: 3,    # return a + b (3 hosts × 1 add)
        8: 3,    # return a - b (3 hosts × 1 sub)
        12: 3,   # return a * b (3 hosts × 1 mul)
        16: 4,   # if (b == 0): 3 normal div + 1 div-by-zero
        17: 1,   # return -1: only the div-by-zero on first host
        19: 3,   # *result = a / b: 3 normal divides
        20: 3,   # return 0: 3 normal divides
        24: 3,   # if (value < lo): 3 clamp calls on last host
        25: 1,   # return lo: clamp(1,5,10) -> 5
        27: 2,   # if (value > hi): 2 remaining clamp calls
        28: 1,   # return hi: clamp(15,5,10) -> 10
        30: 1,   # return value: clamp(7,5,10) -> 7
    }

    def test_store_has_exactly_two_files(self, coverage_run):
        store, *_ = coverage_run
        assert store.file_count() == 2

    def test_math_ops_line_hits(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        for lineno, expected in self.MATH_OPS_EXPECTED.items():
            assert lineno in rec.lines, f"Line {lineno} not in coverage data"
            actual = rec.lines[lineno].hits.for_tier("system")
            assert actual == expected, (
                f"math_ops.c:{lineno}: expected {expected} system hits, got {actual}"
            )

    def test_main_c_dispatcher_hits(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        actual = rec.lines[23].hits.for_tier("system")
        assert actual == 3, (
            f"main.c:23 (add printf): expected 3, got {actual}"
        )

    def test_main_c_untaken_paths_are_zero(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        for lineno in (14, 15):
            if lineno in rec.lines:
                assert rec.lines[lineno].hits.for_tier("system") == 0, (
                    f"main.c:{lineno} should have 0 hits (argc guard)"
                )
        if 44 in rec.lines:
            assert rec.lines[44].hits.for_tier("system") == 0

    def test_math_ops_100pct_line_coverage(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        pct = rec.line_coverage_pct("system")
        assert pct == 100.0, f"math_ops.c system line coverage: {pct}%"

    def test_main_c_less_than_100pct(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        pct = rec.line_coverage_pct("system")
        assert 50.0 < pct < 100.0, f"main.c system line coverage: {pct}%"

    def test_no_unit_or_manual_hits(self, coverage_run):
        store, *_ = coverage_run
        for fr in store.files():
            for lineno, lr in fr.lines.items():
                unit = lr.hits.for_tier("unit")
                manual = lr.hits.for_tier("manual")
                assert unit == 0, (
                    f"{fr.path}:{lineno}: unexpected unit hits ({unit})"
                )
                assert manual == 0, (
                    f"{fr.path}:{lineno}: unexpected manual hits ({manual})"
                )

    def test_hits_are_additive_not_max(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        actual = rec.lines[4].hits.for_tier("system")
        assert actual == 3, (
            f"Line 4 hits = {actual}, expected 3 (additive across 3 hosts)"
        )


# ---------------------------------------------------------------------------
# Test Class 3: Branch Coverage
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestBranchCoverage:
    """Verify branch-level coverage data."""

    def test_divide_condition_has_two_branches(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        lr = rec.lines[16]
        assert len(lr.branches) >= 2

    def test_divide_both_branches_taken(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        for b in rec.lines[16].branches:
            assert b.hits.for_tier("system") > 0, (
                f"Line 16 branch ({b.block}.{b.branch}) not taken"
            )

    def test_clamp_lt_lo_both_branches_taken(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        lr = rec.lines[24]
        assert len(lr.branches) >= 2
        for b in lr.branches:
            assert b.hits.for_tier("system") > 0

    def test_clamp_gt_hi_both_branches_taken(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        lr = rec.lines[27]
        assert len(lr.branches) >= 2
        for b in lr.branches:
            assert b.hits.for_tier("system") > 0

    def test_all_math_ops_branches_taken(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        for lineno, lr in rec.lines.items():
            for b in lr.branches:
                assert b.hits.for_tier("system") > 0, (
                    f"math_ops.c:{lineno} branch ({b.block}.{b.branch}) not taken"
                )

    def test_math_ops_branch_coverage_100pct(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        pct = rec.branch_coverage_pct("system", conservative=True)
        assert pct == 100.0

    def test_main_c_has_untaken_branches(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        lr = rec.lines.get(13)
        if lr and lr.branches:
            untaken = [b for b in lr.branches if b.hits.for_tier("system") == 0]
            assert len(untaken) > 0

    def test_main_c_branch_coverage_below_100(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        pct = rec.branch_coverage_pct("system", conservative=True)
        assert pct < 100.0

    def test_branch_reachability_system_only(self, coverage_run):
        store, *_ = coverage_run
        for fr in store.files():
            for lineno, lr in fr.lines.items():
                for b in lr.branches:
                    # System tier saw the branch (recorded reachability).
                    assert b.is_reachable("system") is not None
                    # No data for unit/manual tiers.
                    assert b.is_reachable("unit") is None
                    assert b.is_reachable("manual") is None

    def test_conservative_vs_nonconservative(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "main.c")
        conservative = rec.branch_coverage_pct("system", conservative=True)
        non_conservative = rec.branch_coverage_pct("system", conservative=False)
        assert 0.0 <= conservative <= 100.0
        assert 0.0 <= non_conservative <= 100.0


# ---------------------------------------------------------------------------
# Test Class 4: Coverage Integrity
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestCoverageIntegrity:
    """Structural integrity and edge-case verifications."""

    def test_store_json_roundtrip(self, coverage_run, tmp_path):
        store, *_ = coverage_run
        json_path = tmp_path / "store.json"
        store.save(json_path)
        loaded = CoverageStore.load(json_path)
        assert loaded.file_count() == store.file_count()
        assert loaded.overall_pct() == pytest.approx(store.overall_pct())
        orig = _find_file_record(store, "math_ops.c")
        roundtripped = _find_file_record(loaded, "math_ops.c")
        assert (
            roundtripped.lines[4].hits.for_tier("system")
            == orig.lines[4].hits.for_tier("system")
        )

    def test_single_host_data_preserved_in_merge(self, coverage_run):
        store, *_ = coverage_run
        rec = _find_file_record(store, "math_ops.c")
        assert 24 in rec.lines
        assert rec.lines[24].hits.for_tier("system") == 3
        assert 25 in rec.lines
        assert rec.lines[25].hits.for_tier("system") == 1

    def test_file_paths_are_resolved_and_exist(self, coverage_run):
        store, *_ = coverage_run
        for fr in store.files():
            assert fr.path.is_absolute(), f"Path not absolute: {fr.path}"
            assert fr.path.exists(), f"Path does not exist: {fr.path}"

    def test_overall_pct_within_bounds(self, coverage_run):
        store, *_ = coverage_run
        pct = store.overall_pct("system")
        assert 0 < pct <= 100

    def test_overall_branch_pct_within_bounds(self, coverage_run):
        store, *_ = coverage_run
        pct = store.overall_branch_pct("system", conservative=True)
        assert 0 < pct <= 100

    def test_static_assets_copied(self, coverage_run):
        _, report_dir, *_ = coverage_run
        static_src = PROJECT_ROOT / "src/otto/coverage/renderer/static"
        if static_src.exists():
            assert (report_dir / "static").is_dir()


# ---------------------------------------------------------------------------
# Test Class 5: .gcda Fetch Structure
# ---------------------------------------------------------------------------

# Expected host layout from tests/lab_data/tech1/hosts.json for lab "veggies".
# Discovering the list from disk rather than calling all_hosts() keeps this
# test process free of otto.configmodule singletons.
EXPECTED_HOST_IDS = {"carrot_seed", "tomato_seed", "pepper_seed"}


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestGcdaFetchStructure:
    """Verify that the .gcda fetch creates the expected directory layout."""

    def test_cov_directory_exists(self, coverage_run):
        *_, cov_dir = coverage_run
        assert cov_dir.is_dir(), f"cov/ directory not found at {cov_dir}"

    def test_every_host_has_subdirectory(self, coverage_run):
        *_, cov_dir = coverage_run
        actual_dirs = {d.name for d in cov_dir.iterdir() if d.is_dir()}
        assert EXPECTED_HOST_IDS == actual_dirs, (
            f"Expected host dirs {EXPECTED_HOST_IDS}, found {actual_dirs}"
        )

    def test_every_host_has_gcda_files(self, coverage_run):
        *_, cov_dir = coverage_run
        for host_dir in cov_dir.iterdir():
            if not host_dir.is_dir():
                continue
            gcda_files = list(host_dir.glob("*.gcda"))
            assert gcda_files, f"No .gcda files in {host_dir}"

    def test_gcda_files_are_nonempty(self, coverage_run):
        *_, cov_dir = coverage_run
        for host_dir in cov_dir.iterdir():
            if not host_dir.is_dir():
                continue
            for gcda in host_dir.glob("*.gcda"):
                assert gcda.stat().st_size > 0, f"{gcda} is empty"

    def test_gcda_file_count_per_host(self, coverage_run):
        """Each host should have exactly 2 .gcda files (main.c + math_ops.c)."""
        *_, cov_dir = coverage_run
        for host_dir in cov_dir.iterdir():
            if not host_dir.is_dir():
                continue
            gcda_files = list(host_dir.glob("*.gcda"))
            assert len(gcda_files) == 2, (
                f"Expected 2 .gcda files in {host_dir.name}, "
                f"found {len(gcda_files)}: {[f.name for f in gcda_files]}"
            )


# ---------------------------------------------------------------------------
# Test Class 6: Suite Runner Integration
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def suite_run_exit_code(tmp_path_factory):
    """Run ``otto test TestCoverageProduct`` as a subprocess.

    Verifies the real ``otto test`` invocation path (``run_suite``),
    catching class-lifecycle issues (e.g. missing event loops in
    ``setup_class``) that direct-call e2e tests would miss. Pinned to the
    same xdist group as the rest of this file so it doesn't race on VMs.
    """
    tmp_dir = tmp_path_factory.mktemp("suite_runner")
    xdir = tmp_dir / "xdir"
    xdir.mkdir()

    result = subprocess.run(
        [str(OTTO_BIN), "-l", "veggies", "test", "TestCoverageProduct"],
        env=_otto_env(xdir),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=600,
    )
    return result


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestSuiteRunnerIntegration:
    """Verify that TestCoverageProduct passes when run via ``otto test``.

    Mirrors the real ``otto test`` invocation path and catches
    class-lifecycle issues (e.g. missing event loops in ``setup_class``)
    that direct-call e2e tests would miss.
    """

    def test_suite_exits_successfully(self, suite_run_exit_code):
        assert suite_run_exit_code.returncode == 0, (
            f"otto test TestCoverageProduct exited with "
            f"{suite_run_exit_code.returncode}\n"
            f"--- stdout ---\n{suite_run_exit_code.stdout}\n"
            f"--- stderr ---\n{suite_run_exit_code.stderr}"
        )
