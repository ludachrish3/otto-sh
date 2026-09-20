"""The assertions repo5's kmod coverage e2e and the otto_kgcov toolchain matrix share.

Everything here is a fact about the demo module's coverage that does not
depend on which compiler built it: which hosts and files the fetch tree
holds, how many ``.gcda`` files a host yields, what the run log says about
the library, the hit counts of lines that are real statements, whether a
guard carries both a taken and an untaken arc, the exit routine's hits, and
the no-double-count property of the mid-suite dump. The two pins that encode
gcc's branch folding (the four-arc ``-ERANGE`` compare and its per-arc
count) stay in the routine e2e's own module.
"""

from pathlib import Path

from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore, FileRecord
from tests.e2e.cov._repo5_build import DEMO_SRC, _hits, _line_of, _record

DEMO = "otto_kmod_demo"
HOSTS = {"test1", "test2"}


def has_a_taken_and_an_untaken_branch(rec: FileRecord, lineno: int) -> bool:
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


def capture_file(capture: Capture, name: str) -> CaptureFileCov | None:
    """The per-host ``capture.json`` entry whose path ends with *name*, or ``None``."""
    for rel, cov in capture.files.items():
        if rel.endswith(name):
            return cov
    return None


def assert_only_the_demo_is_fetched_from_the_two_hosts(cov_dir: Path) -> None:
    # The suite's own fixture installs only on test1/test2 — otto test never
    # touches a product verb, so no other host's leaf (in particular, no
    # container leaf on test3) can appear.
    assert {d.name for d in cov_dir.iterdir() if d.is_dir()} == HOSTS
    for host in HOSTS:
        assert sorted(p.name for p in (cov_dir / host).iterdir()) == [DEMO]
        assert Capture.load(cov_dir / host / DEMO / "capture.json").product == DEMO


def assert_three_gcda_per_host(cov_dir: Path) -> None:
    for host in HOSTS:
        names = sorted(p.name for p in (cov_dir / host / DEMO).glob("*.gcda"))
        assert names == ["demo_main.gcda", "demo_parse.gcda", "demo_policy.gcda"]
        assert all(p.stat().st_size > 0 for p in (cov_dir / host / DEMO).glob("*.gcda"))


def assert_run_log_reports_the_library_loaded_on_demand(log_dir: Path) -> None:
    # otto_kgcov is a DEV TOOL here, not a product: nothing measures it, so it
    # can never be a `report.missing()` row and no partial-instrumentation
    # warning can name it. What the run log proves instead is the positive
    # fact — the demo pulled its own library up at install, on both hosts.
    # KmodProduct._ensure_library says so at INFO once per real load (never
    # when the library was already resident), and the run's verbose.log holds
    # it: a root-logger handler floored at INFO, so the line lands there
    # regardless of --log-level. The demo repo names its kgcov entry
    # `otto_kgcov`, which is the name in the parentheses.
    verbose = (log_dir / "verbose.log").read_text()
    collapsed = " ".join(verbose.split())
    assert f"test1: {DEMO}: loading otto_kgcov (otto_kgcov)" in collapsed, verbose[-3000:]
    assert f"test2: {DEMO}: loading otto_kgcov (otto_kgcov)" in collapsed, verbose[-3000:]


def assert_store_has_the_three_demo_files(store: CoverageStore) -> None:
    names = {Path(str(f.path)).name for f in store.files()}
    assert names == {"demo_main.c", "demo_parse.c", "demo_policy.c"}


def assert_policy_paths_have_the_expected_hits(store: CoverageStore) -> None:
    rec = _record(store, "demo_policy.c")
    src = DEMO_SRC / "demo_policy.c"
    # FIFO/LIFO-full drop: once per host in COMMON (enqueue 5 into a full fifo queue).
    assert _hits(rec, _line_of(src, "return -ENOSPC;")) == 2
    # drop-oldest eviction: test1 only, the 5th enqueue into a 4-slot queue.
    # `_line_of` returns the FIRST match; the identical statement recurs in
    # the drain path, but the enqueue-path one is the intended line here.
    assert _hits(rec, _line_of(src, "q->head = (q->head + 1) % q->cap;")) == 1
    # The stop marker: test2 only (enqueue -1, drain). The bare `break;` body
    # may be folded into its `if (v < 0)` condition's own line, so the "taken
    # exactly once" fact is read off that condition line's branch data: a
    # two-way `if` whose least-taken arc was taken once.
    stop_marker_if = _line_of(src, "if (v < 0)")
    assert stop_marker_if in rec.lines, f"{rec.path}:{stop_marker_if} carries no coverage data"
    stop_marker_hits = sorted(b.hits.for_tier("system") for b in rec.lines[stop_marker_if].branches)
    assert stop_marker_hits[0] == 1, f"demo_policy.c:{stop_marker_if}: {stop_marker_hits}"
    # Never exercised on purpose: the -EBUSY guard and the switch's
    # unreachable `default:` arm, read off their guarding condition lines.
    assert has_a_taken_and_an_untaken_branch(rec, _line_of(src, "if (cap < q->len)"))
    assert has_a_taken_and_an_untaken_branch(rec, _line_of(src, "switch (q->policy) {"))


def assert_parse_hits_that_do_not_depend_on_folding(store: CoverageStore) -> None:
    rec = _record(store, "demo_parse.c")
    src = DEMO_SRC / "demo_parse.c"
    # drain <arg>, never sent: a folded single-statement body (`return
    # -EINVAL;`); "never taken" is read off its `if (arg)` guard.
    assert has_a_taken_and_an_untaken_branch(rec, _line_of(src, "if (arg)"))
    assert _hits(rec, _line_of(src, "*out = DEMO_DROP_OLDEST;")) == 1  # test1 only
    assert _hits(rec, _line_of(src, "*out = DEMO_LIFO;")) == 2  # both hosts


def assert_exit_routine_lines_are_hit_once_per_host(store: CoverageStore) -> None:
    rec = _record(store, "demo_main.c")
    src = DEMO_SRC / "demo_main.c"
    assert _hits(rec, _line_of(src, 'pr_info("unloaded after')) == 2
    assert _hits(rec, _line_of(src, "demo_queue_free(&queue);")) == 2
    assert _hits(rec, _line_of(src, 'pr_info("loaded, queue capacity')) == 2
    # Both hosts' mixes end with an extra enqueue (see TEST1_ONLY/TEST2_ONLY in
    # tests/repo5/tests/test_kmod_demo.py), so the queue is non-empty at
    # unload time on both hosts and the exit-time drain branch fires twice.
    assert _hits(rec, _line_of(src, 'pr_info("draining')) == 2


def assert_policy_switch_records_branches(store: CoverageStore) -> None:
    rec = _record(store, "demo_policy.c")
    lr = rec.lines[_line_of(DEMO_SRC / "demo_policy.c", "switch (q->policy) {")]
    assert len(lr.branches) >= 3
    taken = [b for b in lr.branches if b.hits.for_tier("system") > 0]
    untaken = [b for b in lr.branches if b.hits.for_tier("system") == 0]
    assert taken  # fifo/lifo and drop-oldest taken
    assert untaken  # the default arm not


def assert_mid_suite_dump_does_not_double_count(cov_dir: Path) -> None:
    """Merge semantics, per host: two dumps of one run must not double count.

    test1's mix triggers a runtime dump right after its drop-oldest eviction,
    then teardown's uninstall triggers a second dump at unload. otto_kgcov's
    own contract ("a dump never double counts, and two dumps of the same run
    add correctly") means the drop-oldest line reads exactly 1 hit, not 2.
    Checked on test1's PER-HOST ``capture.json`` — the raw retrieval before
    any cross-host merge — so a merge-stage bug cannot paper over a
    double-counting dump. test2 never runs `policy drop-oldest`, so the same
    line is absent (or 0) in its capture.
    """
    lineno = _line_of(DEMO_SRC / "demo_parse.c", "*out = DEMO_DROP_OLDEST;")
    test1_capture = Capture.load(cov_dir / "test1" / DEMO / "capture.json")
    test1_cov = capture_file(test1_capture, "demo_parse.c")
    assert test1_cov is not None, list(test1_capture.files)
    assert test1_cov.lines.get(lineno) == 1, test1_cov.lines
    test2_capture = Capture.load(cov_dir / "test2" / DEMO / "capture.json")
    test2_cov = capture_file(test2_capture, "demo_parse.c")
    if test2_cov is not None:
        assert test2_cov.lines.get(lineno, 0) == 0, test2_cov.lines
