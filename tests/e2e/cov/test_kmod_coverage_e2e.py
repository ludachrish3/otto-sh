"""End-to-end: kernel-module coverage through otto_kgcov on the unix bed.

    otto -l unix test --cov TestKmodDemo      (repo5)
    otto -l unix cov report <run> --dir <report>

Prerequisites: test1/test2 up (6.8.0-86-generic), the headers for that
release on the dev VM, gcc and lcov on the dev VM. The modules are built by
``tests/repo5/build.sh`` when missing. Pinned to one xdist worker: both
hosts' modules and /var/cov/otto_kmod_demo are shared bed state.
"""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore, FileRecord
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests.e2e._otto_subprocess import REPO5, run_otto

BUILD = REPO5 / "build"
DEMO_SRC = REPO5 / "kmod" / "demo"
KGCOV = PROJECT_ROOT / "docs" / "examples" / "kgcov"
DEMO = "otto_kmod_demo"
HOSTS = {"test1", "test2"}
_LAB = "unix"


def _demo_and_kgcov_sources() -> list[Path]:
    """Everything a rebuild depends on: the demo's own files and otto_kgcov's."""
    return [
        # Kbuild writes the generated <module>.mod.c beside the sources AFTER the
        # library .ko; counting it would make every build look stale.
        *(p for p in DEMO_SRC.glob("*.c") if not p.name.endswith(".mod.c")),
        *DEMO_SRC.glob("*.h"),
        DEMO_SRC / "Kbuild",
        DEMO_SRC / "Makefile",
        *(p for p in KGCOV.rglob("*") if p.is_file()),
    ]


def _kos_are_stale(kos: list[Path], sources: list[Path]) -> bool:
    """True when a ``.ko`` is missing, or older than any source it was built from."""
    if any(not ko.is_file() for ko in kos):
        return True
    newest_source = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
    oldest_ko = min(ko.stat().st_mtime for ko in kos)
    return oldest_ko < newest_source


@pytest.fixture(scope="module")
def built_modules(tmp_path_factory):
    """Build both modules for the running kernel when missing or stale."""
    committed = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(DEMO_SRC)],
        cwd=REPO5,
        env=git_env(tmp_path_factory.mktemp("githome")),
        check=False,
    )
    assert committed.returncode == 0, (
        f"{DEMO_SRC} has uncommitted changes: a capture anchors every measured file to a "
        "committed git blob at HEAD, while _line_of() below reads the worktree — an "
        "uncommitted edit makes the two disagree on line numbers silently. Commit or "
        "revert before running this e2e."
    )
    lib_ko = BUILD / "lib" / "otto_kgcov.ko"
    demo_ko = DEMO_SRC / f"{DEMO}.ko"
    if _kos_are_stale([lib_ko, demo_ko], _demo_and_kgcov_sources()):
        try:
            subprocess.run([str(REPO5 / "build.sh")], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{REPO5 / 'build.sh'} failed (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc
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


def _line_of(path: Path, needle: str) -> int:
    """1-based line number of the first source line containing *needle*."""
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found in {path}")


def _record(store: CoverageStore, name: str) -> FileRecord:
    for fr in store.files():
        if str(fr.path).endswith(name):
            return fr
    have = [str(f.path) for f in store.files()]
    raise AssertionError(f"no FileRecord ending with {name!r}; have {have}")


def _hits(rec: FileRecord, lineno: int) -> int:
    assert lineno in rec.lines, f"{rec.path}:{lineno} carries no coverage data"
    return rec.lines[lineno].hits.for_tier("system")


def _has_a_taken_and_an_untaken_branch(rec: FileRecord, lineno: int) -> bool:
    """Whether *lineno* (an ``if``/``switch``) carries both a taken and an untaken arc.

    A single-statement branch body the compiler folds into its condition's
    own line (observed live: a bare ``return``/``break`` right after an
    ``if``) leaves that body line with NO ``DA:`` record at all — not a zero
    count, an absence — so a never-taken path is checked on the *condition*
    line's branch data instead of the body line's (possibly-missing) hits.

    Requiring a taken arc too (not just "some arc reads 0") rules out the
    degenerate case where the *whole line* never ran: every arc would read 0
    then, which a bare ``any(hits == 0)`` check cannot tell apart from "one
    arc taken, the other not."
    """
    hits = sorted(b.hits.for_tier("system") for b in rec.lines[lineno].branches)
    return hits[0] == 0 and hits[-1] > 0


def _capture_file(capture: Capture, name: str) -> CaptureFileCov | None:
    """The per-host ``capture.json`` entry whose path ends with *name*, or ``None``."""
    for rel, cov in capture.files.items():
        if rel.endswith(name):
            return cov
    return None


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
        _, cov_dir, _ = coverage_run
        assert {d.name for d in cov_dir.iterdir() if d.is_dir()} == HOSTS
        for host in HOSTS:
            assert sorted(p.name for p in (cov_dir / host).iterdir()) == [DEMO]
            assert Capture.load(cov_dir / host / DEMO / "capture.json").product == DEMO

    def test_three_gcda_per_host_one_per_translation_unit(self, coverage_run):
        _, cov_dir, _ = coverage_run
        for host in HOSTS:
            names = sorted(p.name for p in (cov_dir / host / DEMO).glob("*.gcda"))
            assert names == ["demo_main.gcda", "demo_parse.gcda", "demo_policy.gcda"]
            assert all(p.stat().st_size > 0 for p in (cov_dir / host / DEMO).glob("*.gcda"))

    def test_the_run_log_says_the_library_is_not_instrumented(self, coverage_run):
        # otto_kgcov's settings.toml entry declares `instrumented = false`
        # (its .ko legitimately contains "__gcov_" bytes — it implements
        # gcov's own runtime callbacks for consumers — which otherwise trips
        # the generic byte-scan into a false instrumented=True). That makes
        # it a `report.missing()` row on both hosts even under forced --cov,
        # so decide_coverage()'s partial-instrumentation warning
        # (otto.coverage.instrumentation._rows_text) fires and is captured
        # in the run's verbose.log (a root-logger handler floored at INFO,
        # so a WARNING always lands there regardless of --log-level).
        _, _, log_dir = coverage_run
        verbose = (log_dir / "verbose.log").read_text()
        collapsed = " ".join(verbose.split())
        assert "test1: otto_kgcov — no" in collapsed, verbose[-3000:]
        assert "test2: otto_kgcov — no" in collapsed, verbose[-3000:]


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestKmodLineAndBranchHits:
    """Exact counts: both hosts run COMMON; test1 adds TEST1_ONLY, test2 TEST2_ONLY.

    Both hosts' mixes end with one more ``enqueue`` (repo5's suite), so the
    queue is non-empty when the suite's teardown unloads the module — the
    exit-time drain and its ``pr_info`` line run once per host, not zero.
    """

    def test_store_has_the_three_demo_files(self, coverage_run):
        store, *_ = coverage_run
        names = {Path(str(f.path)).name for f in store.files()}
        assert names == {"demo_main.c", "demo_parse.c", "demo_policy.c"}

    def test_policy_paths_have_the_expected_hits(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "demo_policy.c")
        src = DEMO_SRC / "demo_policy.c"
        # FIFO/LIFO-full drop: once per host in COMMON (enqueue 5 into a full fifo queue).
        assert _hits(rec, _line_of(src, "return -ENOSPC;")) == 2
        # drop-oldest eviction: test1 only, the 5th enqueue into a 4-slot queue.
        # `_line_of` returns the FIRST match; the identical statement recurs
        # in the drain path (~demo_policy.c:67), but the enqueue-path one
        # above is the intended line here.
        assert _hits(rec, _line_of(src, "q->head = (q->head + 1) % q->cap;")) == 1
        # The stop marker: test2 only (enqueue -1, drain). The compiler folds
        # the bare `break;` body into its `if (v < 0)` condition's own line
        # (no separate DA: record survives for the body), so the "taken
        # exactly once" fact is read off that condition line's branch data.
        stop_marker_if = _line_of(src, "if (v < 0)")
        assert stop_marker_if in rec.lines, f"{rec.path}:{stop_marker_if} carries no coverage data"
        stop_marker_branches = rec.lines[stop_marker_if].branches
        stop_marker_hits = sorted(b.hits.for_tier("system") for b in stop_marker_branches)
        assert stop_marker_hits[0] == 1, f"demo_policy.c:{stop_marker_if}: {stop_marker_hits}"
        # Never exercised on purpose. Both `return -EBUSY;` and the switch's
        # unreachable `default: return -EINVAL;` are single-statement bodies
        # the compiler folds into their guarding condition — no DA: record of
        # their own survives, so "never taken" is read off the condition.
        assert _has_a_taken_and_an_untaken_branch(
            rec, _line_of(src, "if (cap < q->len)")
        )  # -EBUSY guard
        assert _has_a_taken_and_an_untaken_branch(
            rec, _line_of(src, "switch (q->policy) {")
        )  # the default arm

    def test_parse_errors_have_the_expected_hits(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "demo_parse.c")
        src = DEMO_SRC / "demo_parse.c"
        # `return -ERANGE;` is a single-statement body the compiler folds into
        # its guarding `if`, so "taken exactly once" (test1's `limit 999`) is
        # read off that condition line's branch data — same folding as
        # demo_policy.c's stop marker above.
        erange_if = _line_of(src, 'if (!strcmp(cmd, "limit") && (v < 1 || v > 64))')
        assert erange_if in rec.lines, f"{rec.path}:{erange_if} carries no coverage data"
        erange_branches = rec.lines[erange_if].branches
        # gcc folds `v < 1 || v > 64` (in series with the `!strcmp` check) into
        # one 4-arc compare; if a future compiler changes that fold, this pin
        # fails loudly instead of the hit-count assertion below misreading it.
        assert len(erange_branches) == 4, erange_branches
        erange_hits = sorted(b.hits.for_tier("system") for b in erange_branches)
        assert erange_hits[0] == 1, f"demo_parse.c:{erange_if} branches: {erange_hits}"
        # drain <arg>, never sent: another folded single-statement body
        # (`return -EINVAL;`); "never taken" is read off its `if (arg)` guard.
        assert _has_a_taken_and_an_untaken_branch(rec, _line_of(src, "if (arg)"))
        assert _hits(rec, _line_of(src, "*out = DEMO_DROP_OLDEST;")) == 1  # test1 only
        assert _hits(rec, _line_of(src, "*out = DEMO_LIFO;")) == 2  # both hosts

    def test_exit_routine_lines_are_hit_once_per_host(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "demo_main.c")
        src = DEMO_SRC / "demo_main.c"
        assert _hits(rec, _line_of(src, 'pr_info("unloaded after')) == 2
        assert _hits(rec, _line_of(src, "demo_queue_free(&queue);")) == 2
        assert _hits(rec, _line_of(src, 'pr_info("loaded, queue capacity')) == 2
        # Both hosts' mixes end with an extra enqueue (see TEST1_ONLY/TEST2_ONLY
        # in tests/repo5/tests/test_kmod_demo.py), so the queue is non-empty at
        # unload time on both hosts and the exit-time drain branch fires twice.
        assert _hits(rec, _line_of(src, 'pr_info("draining')) == 2

    def test_branches_are_recorded_for_the_policy_switch(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "demo_policy.c")
        lr = rec.lines[_line_of(DEMO_SRC / "demo_policy.c", "switch (q->policy) {")]
        assert len(lr.branches) >= 3
        taken = [b for b in lr.branches if b.hits.for_tier("system") > 0]
        untaken = [b for b in lr.branches if b.hits.for_tier("system") == 0]
        assert taken  # fifo/lifo and drop-oldest taken
        assert untaken  # the default arm not

    def test_the_mid_suite_dump_does_not_double_count_against_the_exit_dump(self, coverage_run):
        """Merge semantics, per host: two dumps of one run must not double count.

        test1's mix triggers a runtime dump right after its drop-oldest
        eviction (``test_test1_mix_and_a_mid_suite_dump``'s ``echo 1 >
        .../dump``), then teardown's uninstall triggers a second dump at
        unload. otto_kgcov's own contract (docs/examples/kgcov/README.md:
        "a dump never double counts, and two dumps of the same run add
        correctly") means the drop-oldest line should read exactly 1 hit,
        not 2. Checked directly on test1's PER-HOST ``capture.json`` — the
        raw retrieval before any cross-host merge into ``store.json`` — so a
        merge-stage bug can't accidentally paper over a double-counting
        dump. test2 never runs `policy drop-oldest`, so the same line is
        absent (or 0) in its capture.
        """
        _, cov_dir, _ = coverage_run
        lineno = _line_of(DEMO_SRC / "demo_parse.c", "*out = DEMO_DROP_OLDEST;")
        test1_capture = Capture.load(cov_dir / "test1" / DEMO / "capture.json")
        test1_cov = _capture_file(test1_capture, "demo_parse.c")
        assert test1_cov is not None, list(test1_capture.files)
        assert test1_cov.lines.get(lineno) == 1, test1_cov.lines
        test2_capture = Capture.load(cov_dir / "test2" / DEMO / "capture.json")
        test2_cov = _capture_file(test2_capture, "demo_parse.c")
        if test2_cov is not None:
            assert test2_cov.lines.get(lineno, 0) == 0, test2_cov.lines
