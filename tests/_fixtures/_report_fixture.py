"""Deterministic coverage-report fixture.

One builder shared by the report browser suite
(tests/e2e/cov/report_browser/) and the docs-media screenshot
(scripts/capture_docs_media.py), so the pixels users see in the guide are
produced by the exact report the browser tests pin.

Three tiers (system, unit, manual), two files, every pill state the renderer
knows: branch-taken, branch-not-taken, branch-unreachable — plus a fully
covered file and a partially covered one so sorting has something to
reorder. Rendered with ``prefix=base_dir`` so displayed paths are the
deterministic ``product/main.c`` / ``product/utils.c`` regardless of the tmp
dir. Registers a run table (spec §10): two ``system`` runs sharing the
label "nightly-full" (multi-host: router-a/router-b) crediting main.c's
system-tier lines, one ``unit`` run ("unit harvest", host ci-01) crediting
its unit-tier lines, one fully-revoked ``manual`` run ("smoke-old" — a
stale line with no live hits) and one aging, dirty-remapped ``manual`` run
("field bring-up", ticket FW-1188) — every state (``t-<tier>``/``s-excl``/
``s-stale``/``s-aging``) the SPA's file-page row precedence renders is
reachable from this one fixture.

Per-ticket attribution (Task 13): two commit-message-attributed tickets,
distinct from ``RunRecord.ticket`` above (a different axis — see design §1).
``PROJ-204`` owns main.c's ``checked_add()`` body through the stale brace
(lines 3-7) and nothing in utils.c, so pinning it hides utils.c's tree row;
its one uncovered line (6, the stale/no-hit line) gives the tickets page a
real missing-range to expand and a real ``?lines=`` deep link to click
through. ``PROJ-9`` owns only utils.c's one hit line, fully covered, and
carries no tracker ``url`` (the other ticket does) — exercising both of
``TicketIdCell``'s render variants in one fixture.
"""

from pathlib import Path

from otto.coverage.renderer.spa_renderer import SpaRenderer
from otto.coverage.store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    LineRecord,
    TicketRecord,
)

_MAIN_C = """\
#include <stdio.h>

int checked_add(int a, int b) {
    if (a > 0 && b > 0) {
        return a + b;
    }
    return 0;
}

int main(void) {
    printf("%d\\n", checked_add(20, 22));
    return 0;
}
"""

_UTILS_C = """\
int double_it(int x) {
    return x * 2;
}

int never_called(int x) {
    return x - 1;  // LCOV_EXCL_LINE
}
"""

# Fake-but-plausible 40-char shas — every run below anchors to one of these.
_BASE_COMMIT_NIGHTLY = "a1" * 20
_BASE_COMMIT_FIELD = "b2" * 20


def _line(number: int, hits: dict[str, int], run_hits: dict[int, int] | None = None) -> LineRecord:
    rec = LineRecord(line_number=number)
    for tier, n in hits.items():
        rec.hits.add(tier, n)
    if run_hits:
        rec.run_hits = dict(run_hits)
    return rec


def _branch(block: int, branch: int, hits: dict[str, int], *, reachable: bool) -> BranchHits:
    bh = BranchHits(block=block, branch=branch)
    for tier, n in hits.items():
        bh.hits.add(tier, n)
    for tier in ("system", "unit"):
        bh.set_reachable(tier, reachable)
    return bh


def build_fixture_report(base_dir: Path) -> Path:
    """Write sample sources under *base_dir* and render the report; return its dir.

    FileRecords carry absolute paths (the renderer reads the sources from
    them); ``prefix=base_dir`` makes the *displayed* paths the short
    ``product/...`` form — same strings in the browser pins and the docs
    screenshot.
    """
    src_dir = base_dir / "product"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "main.c").write_text(_MAIN_C)
    (src_dir / "utils.c").write_text(_UTILS_C)

    store = CoverageStore(tier_order=["system", "unit", "manual"])

    # -- Runs (spec §10) -----------------------------------------------
    # Two system runs sharing one label — a multi-host nightly run.
    run_sys_a = store.add_run(
        tier="system",
        label="nightly-full",
        board="router-a",
        host="router-a",
        labs=["lab1"],
        captured_at="2026-07-20T02:00:00Z",
        base_commit=_BASE_COMMIT_NIGHTLY,
    )
    run_sys_b = store.add_run(
        tier="system",
        label="nightly-full",
        board="router-b",
        host="router-b",
        labs=["lab1"],
        captured_at="2026-07-20T02:05:00Z",
        base_commit=_BASE_COMMIT_NIGHTLY,
    )
    run_unit = store.add_run(
        tier="unit",
        label="unit harvest",
        board="ci-01",
        host="ci-01",
        labs=["ci"],
        captured_at="2026-07-21T09:00:00Z",
        base_commit=_BASE_COMMIT_NIGHTLY,
    )
    # Fully revoked: the anchor chain couldn't verify it at report time —
    # no live hits anywhere, just a stale mark and the revoked run id.
    run_smoke_old = store.add_run(
        tier="manual",
        label="smoke-old",
        board="bench-3",
        host="bench-3",
        labs=["lab2"],
        captured_at="2026-06-01T00:00:00Z",
        base_commit=_BASE_COMMIT_FIELD,
    )
    # Valid but old, and its anchor chain needed a dirty-tree remap.
    run_field = store.add_run(
        tier="manual",
        label="field bring-up",
        board="bench-7",
        host="bench-7",
        labs=["lab3"],
        captured_at="2026-05-01T00:00:00Z",
        tester={"name": "M. Reyes"},
        ticket="FW-1188",
        dirty_remap=True,
        base_commit=_BASE_COMMIT_FIELD,
    )
    # add_run has no `aging` kwarg — validity.py sets it post-hoc too.
    store.runs[run_field].aging = True

    # -- main.c ----------------------------------------------------------
    main_rec = FileRecord(path=src_dir / "main.c")
    # router-a covers checked_add()'s body; router-b covers main()'s body —
    # same "nightly-full" label, two hosts, so ctx_lines rolls both up.
    for lineno, hits, run_hits in [
        (3, {"system": 4, "unit": 12}, {run_sys_a: 4, run_unit: 12}),
        (4, {"system": 4, "unit": 12}, {run_sys_a: 4, run_unit: 12}),
        (5, {"system": 4, "unit": 8}, {run_sys_a: 4, run_unit: 8}),
        (7, {"unit": 4}, {run_unit: 4}),
        (10, {"system": 4}, {run_sys_b: 4}),
        (11, {"system": 4}, {run_sys_b: 4}),
        (12, {"system": 4}, {run_sys_b: 4}),
    ]:
        main_rec.lines[lineno] = _line(lineno, hits, run_hits)
    # The `if (a > 0 && b > 0)` line: one taken pair-half, one never-taken,
    # one unreachable — all three pill classes on one line.
    main_rec.lines[4].branches = [
        _branch(0, 0, {"system": 4, "unit": 8}, reachable=True),
        _branch(0, 1, {}, reachable=True),
        _branch(0, 2, {}, reachable=False),
    ]
    # Line 6 ("    }", the if-block's closing brace — gcov never emits a
    # DA: for it, so no tier ever measures it): smoke-old's manual claim
    # here was fully revoked by report time — a stale mark, no hits, no
    # live evidence at all (s-stale).
    stale_line = main_rec.get_or_create_line(6)
    stale_line.state = "stale"
    stale_line.stale_runs = [run_smoke_old]
    # Line 9 (blank, same reasoning): field bring-up's manual claim is
    # still valid (unlike smoke-old above) but old enough to render aging
    # (s-aging) — credited via `run_hits` only, deliberately no per-tier
    # `hits` entry: the SPA's row-precedence (Global Constraints) reads a
    # tier hit ahead of aging/stale, so a `hits`-bearing "manual" entry
    # here would render `t-manual`, not `s-aging` (see FilePage.test.tsx's
    # "aging beats stale when neither has a hit" pin) — this is what
    # actually renders the aging state, not what apply_manual_capture's
    # insertion order happens to produce today.
    aging_line = main_rec.get_or_create_line(9)
    aging_line.state = "aging"
    aging_line.run_hits[run_field] = 3
    # PROJ-204 (Task 13, per-ticket attribution): owns checked_add()'s body
    # through the stale brace (3-7) — 4 hit lines, 1 uncovered (the stale
    # line 6, which carries no per-tier hit) — and nothing in utils.c, so a
    # pinned PROJ-204 hides utils.c's tree row entirely (module docstring).
    for lineno in (3, 4, 5, 6, 7):
        main_rec.lines[lineno].ticket = ["PROJ-204"]
    store.merge_file(main_rec)

    # -- utils.c -----------------------------------------------------------
    utils_rec = FileRecord(path=src_dir / "utils.c")
    utils_rec.lines[2] = _line(2, {"unit": 6}, {run_unit: 6})
    utils_rec.lines[6] = _line(6, {})  # LCOV_EXCL_LINE-marked in source -> s-excl at render
    # PROJ-9: owns only this one, fully-covered line — never touches
    # main.c, and carries no tracker `url` (PROJ-204 does), exercising
    # TicketIdCell's plain-text render variant.
    utils_rec.lines[2].ticket = ["PROJ-9"]
    store.merge_file(utils_rec)

    store.tickets["PROJ-204"] = TicketRecord(
        id="PROJ-204", url="https://example.test/issues/204", commits=[_BASE_COMMIT_NIGHTLY]
    )
    store.tickets["PROJ-9"] = TicketRecord(id="PROJ-9", url=None, commits=[_BASE_COMMIT_FIELD])

    report_dir = base_dir / "report"
    SpaRenderer(report_dir, project_name="otto example product", prefix=base_dir).render(store)
    return report_dir
