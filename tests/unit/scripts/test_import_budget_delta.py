"""Pins for scripts/import_budget_delta.py — the release's import-budget delta.

Every record directory here is synthetic (``_side``), shaped exactly as ``measure``
writes it; no test runs strace, uv or scripts/import_budget.py. The Makefile side
(the stage after check-python, the approval variable) is pinned in
``tests/unit/test_make_release_lanes.py``.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from scripts import import_budget
from scripts import import_budget_delta as delta
from scripts.import_budget_delta import (
    FIXTURE_CANARIES,
    KNOWN_SURFACES,
    REPO_SURFACES,
    Approval,
    MeasureError,
    Provenance,
    Settings,
    SideJob,
    base_ref,
    decide,
    instrument_id,
    judge_minor,
    main,
    measure,
    measure_side,
    parse_approvals,
    reproduced,
    stale_approvals,
    validate_side,
)
from tests._fixtures.gitrepo import TmpGitRepo

pytestmark = pytest.mark.interpreter_agnostic

SHA, TREE, PY, INSTR = "a" * 40, "b" * 40, "3.10.14", "c" * 40 + ":" + "d" * 40
PROV = Provenance(sha=SHA, tree=TREE, python=PY, instrument=INSTR)
META = {s.key: s for s in import_budget.SURFACES}
REAL = next(s.key for s in import_budget.SURFACES if s.real_entry and not s.tracked)
PLAIN = next(s.key for s in import_budget.SURFACES if not s.real_entry and not s.tracked)
TRACKED = next(s.key for s in import_budget.SURFACES if s.tracked)
NONE: "list[Approval]" = []
GROWN = {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}}}
SHUFFLED = {"buckets": {PLAIN: {"otto:*": 498, "otto:x": 2}}}
QUIET: dict = {}


def _row(
    key: str,
    ops: int,
    buckets: "dict[str, int] | None" = None,
    exit_code: "int | None" = None,
    ending: "list[str] | None" = None,
) -> dict:
    meta = META[key]
    expected = meta.expect_exit if meta.real_entry else None
    return {
        "surface": key,
        "tier": "tracked" if meta.tracked else "gated",
        "file_ops": ops,
        "workspace": 10,
        "exit": expected if exit_code is None else exit_code,
        "ending": [] if ending is None else ending,
        "by_bucket": buckets if buckets is not None else {"otto:*": ops},
        "by_process": {"python": ops},
    }


def _side(
    root: Path,
    name: str,
    minors: "tuple[str, ...]" = ("3.10",),
    ops: "dict[str, int] | None" = None,
    buckets: "dict[str, dict[str, int]] | None" = None,
    exits: "dict[str, int] | None" = None,
    endings: "dict[str, list[str]] | None" = None,
) -> Path:
    """A complete record directory: every known surface at 500 ops unless *ops* says otherwise."""
    side = root / name
    side.mkdir(parents=True, exist_ok=True)
    for minor in minors:
        table = [
            _row(
                k,
                (ops or {}).get(k, 500),
                (buckets or {}).get(k),
                (exits or {}).get(k),
                (endings or {}).get(k),
            )
            for k in KNOWN_SURFACES
        ]
        (side / f"{minor}.json").write_text(json.dumps(table))
        status = {"sha": SHA, "tree": TREE, "instrument": INSTR, "minor": minor,
                  "python": f"{minor}.14", "sync": 0, "version": 0, "report": 0,
                  "elapsed": {"sync": 1.0, "version": 0.1, "report": 2.0}}  # fmt: skip
        (side / f"{minor}.status.json").write_text(json.dumps(status))
    return side


def _edit(path: Path, fn) -> None:
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def _drop(key: str):
    return lambda d: d.__setitem__(slice(None), [r for r in d if r["surface"] != key])


def _pairs(tmp_path: Path, *tips: dict) -> "list[tuple[Path, Path]]":
    return [(_side(tmp_path, f"b{i}"), _side(tmp_path, f"t{i}", **kw)) for i, kw in enumerate(tips)]


# --- the known-surface pin -------------------------------------------------


def test_known_surfaces_is_import_budget_s_list_in_order():
    """A surface added to import_budget.py is added here too, or every record is refused."""
    assert [s.key for s in import_budget.SURFACES] == KNOWN_SURFACES


def test_repo_surfaces_are_every_surface_that_carries_the_generated_repo():
    assert [s.key for s in import_budget.SURFACES if s.sut_files is not None] == REPO_SURFACES


def test_the_fixture_canaries_are_the_repo_surfaces_that_dispatch_its_noop():
    """The canary dispatches the fixture's own `noop`: if that fails, the repo does not run."""
    from tests._fixtures import generated_repo

    assert "async def noop(" in generated_repo._REALISTIC_INIT
    dispatching = [
        s.key
        for s in import_budget.SURFACES
        if s.sut_files is not None and s.real_entry and s.argv[-2:] == ["run", "noop"]
    ]
    assert dispatching == FIXTURE_CANARIES
    assert set(FIXTURE_CANARIES) <= set(REPO_SURFACES)


# --- validate ---------------------------------------------------------------


def test_a_complete_side_validates_with_and_without_provenance(tmp_path: Path):
    side = _side(tmp_path, "s")
    assert validate_side(side, "3.10", None) == []
    assert validate_side(side, "3.10", PROV) == []


def test_a_minor_never_measured_is_one_named_problem(tmp_path: Path):
    side = _side(tmp_path, "s")
    assert validate_side(side, "3.12", None) == [f"{side}: 3.12 not measured (no status)"]


@pytest.mark.parametrize("step", ["sync", "version", "report"])
def test_a_failed_step_is_refused_by_name_and_nothing_else_is_read(tmp_path: Path, step: str):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.status.json", lambda d: d.update({step: 1}))
    (side / "3.10.json").write_text("{not json")  # never read: the failed step is the only problem
    assert validate_side(side, "3.10", None) == [f"{side}: 3.10 step {step} exited 1, not 0"]


def test_a_status_without_a_step_is_refused(tmp_path: Path):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.status.json", lambda d: d.pop("report"))
    assert validate_side(side, "3.10", None) == [f"{side}: 3.10 step report exited None, not 0"]


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("sha", "e" * 40, f"sha {'e' * 40}, not {SHA}"),
        ("tree", "e" * 40, f"tree {'e' * 40}, not {TREE}"),
        ("python", "3.10.15", "python 3.10.15, not 3.10.14"),
        ("instrument", "e" * 40, f"instrument {'e' * 40}, not {INSTR}"),
    ],
)
def test_provenance_that_does_not_match_is_refused_by_field(
    tmp_path: Path, field: str, value: str, problem: str
):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.status.json", lambda d: d.update({field: value}))
    assert validate_side(side, "3.10", PROV) == [f"{side}: 3.10 {problem}"]
    assert validate_side(side, "3.10", None) == [], (
        "without expected provenance the record is still evidence"
    )


def test_a_foreign_minor_is_refused_even_without_provenance(tmp_path: Path):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.status.json", lambda d: d.update({"python": "3.11.2", "minor": "3.11"}))
    assert validate_side(side, "3.10", None) == [f"{side}: 3.10 ran under 3.11.2"]


def test_an_unreadable_or_misshapen_report_is_refused(tmp_path: Path):
    side = _side(tmp_path, "s")
    (side / "3.10.json").write_text("{not json")
    [problem] = validate_side(side, "3.10", None)
    assert problem.startswith(f"{side}: 3.10 unreadable:")
    (side / "3.10.json").write_text(json.dumps({"surface": "x"}))
    assert validate_side(side, "3.10", None) == [f"{side}: 3.10 report is not a list of rows"]


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda r: r.update(file_ops="5"), "file_ops is not an int"),
        (lambda r: r.update(file_ops=True), "file_ops is not an int"),
        (lambda r: r.update(file_ops=-1), "file_ops is negative"),
        (lambda r: r.pop("exit"), "exit is missing"),
        (lambda r: r.update(exit="0"), "exit is not an int or null"),
        (lambda r: r.pop("ending"), "ending is missing"),
        (lambda r: r.update(ending="boom"), "ending is not a list of str"),
        (lambda r: r.update(ending=[1]), "ending is not a list of str"),
        (lambda r: r.update(by_bucket={"otto:*": True}), "by_bucket is not {str: int}"),
        (lambda r: r.update(by_bucket={"otto:*": -500}), "by_bucket holds a negative count"),
        (lambda r: r.pop("workspace"), "workspace is not an int"),
    ],
)
def test_every_consumed_field_is_checked(tmp_path: Path, change, why: str):
    side = _side(tmp_path, "s")

    def apply(d):
        for r in d:
            if r["surface"] == PLAIN:
                change(r)

    _edit(side / "3.10.json", apply)
    assert validate_side(side, "3.10", None) == [f"{side}: 3.10 row {PLAIN} is misshapen ({why})"]


def test_a_report_must_hold_every_known_surface_once(tmp_path: Path):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.json", _drop(PLAIN))
    assert validate_side(side, "3.10", None) == [
        (
            f"{side}: 3.10 reports {len(KNOWN_SURFACES) - 1} rows, "
            f"not one per known surface: ['{PLAIN}']"
        )
    ]
    dup = _side(tmp_path, "dup")
    _edit(dup / "3.10.json", lambda d: d.append(dict(d[0])))
    assert validate_side(dup, "3.10", None) == [
        f"{dup}: 3.10 reports {len(KNOWN_SURFACES) + 1} rows, not one per known surface: []"
    ]
    (side / "3.10.json").write_text("[]")
    assert validate_side(side, "3.10", None) == [
        f"{side}: 3.10 reports 0 rows, not one per known surface: {sorted(KNOWN_SURFACES)}"
    ]


def test_buckets_must_sum_to_file_ops(tmp_path: Path):
    side = _side(tmp_path, "s", buckets={PLAIN: {"otto:*": 499}})
    assert validate_side(side, "3.10", None) == [
        f"{side}: 3.10 {PLAIN} buckets sum to 499, not its 500 file ops"
    ]


def test_a_wrong_exit_is_not_a_validation_problem(tmp_path: Path):
    """A base that cannot run a surface is still evidence for the others; the judge lists it."""
    side = _side(tmp_path, "s", exits={REAL: META[REAL].expect_exit + 1})
    assert validate_side(side, "3.10", None) == []


# --- approvals --------------------------------------------------------------


def test_parse_approvals_reads_every_shape():
    assert parse_approvals(["help"]) == [Approval("help", None, None)]
    assert parse_approvals(["help@3.14"]) == [Approval("help", "3.14", None)]
    assert parse_approvals(["help=stdlib+4,otto:__pycache__+1,otto:*-2"]) == [
        Approval("help", None, {"stdlib": 4, "otto:__pycache__": 1, "otto:*": -2})
    ]
    assert parse_approvals(["run@3.10=site:sphinxcontrib_jsmath-1+2"]) == [
        Approval("run", "3.10", {"site:sphinxcontrib_jsmath-1": 2})
    ]


@pytest.mark.parametrize(
    ("words", "why"),
    [
        (["nope"], "--approve nope: not a known surface"),
        (["help@3.9"], "--approve help@3.9: not a measured minor"),
        (["help=stdlib"], "--approve help=stdlib: bucket 'stdlib' has no +N/-N"),
        (["help=stdlib+x"], "--approve help=stdlib+x: bucket 'stdlib+x' has no +N/-N"),
        (["help="], "--approve help=: names no bucket"),
        (["help=otto:*+0"], "--approve help=otto:*+0: bucket 'otto:*+0' moves nothing"),
        (
            ["help=stdlib+1,stdlib+2"],
            "--approve help=stdlib+1,stdlib+2: bucket 'stdlib' named twice",
        ),
        (["help=stdlib+1", "help=stdlib+2"], "--approve help=stdlib+2: a second entry for help"),
        (
            ["help@3.10", "help@3.10=stdlib+1"],
            "--approve help@3.10=stdlib+1: a second entry for help@3.10",
        ),
    ],
)
def test_parse_approvals_refuses_by_word(words: "list[str]", why: str):
    with pytest.raises(ValueError, match="^" + re.escape(why) + "$"):
        parse_approvals(words, minors=["3.10", "3.14"])


def test_an_approval_renders_back_to_its_word():
    assert (
        Approval("help", "3.14", {"stdlib": 4, "otto:*": -1}).word()
        == "help@3.14=otto:*-1,stdlib+4"
    )
    assert Approval("help", None, None).word() == "help"


# --- judge_minor ------------------------------------------------------------


def _judge(tmp_path: Path, tip_kw: dict, approve: "list[str]" = (), minor: str = "3.10"):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", **tip_kw)
    return judge_minor(base, tip, minor, parse_approvals(list(approve)))


def test_nothing_changed_is_quiet(tmp_path: Path):
    v = _judge(tmp_path, QUIET)
    assert v.stop == []
    assert (v.named, v.declined, v.shuffles, v.unmeasurable, v.grown, v.used) == (
        [],
        [],
        [],
        {},
        [],
        [],
    )
    assert v.reason is None


def test_growth_on_an_unapproved_surface_stops_and_prints_the_entry_to_paste(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 500, "otto:x": 4}}})
    assert v.stop == [
        (
            f"3.10 {PLAIN} 500 -> 504 {{'otto:x': 4}}: grew and is not approved; "
            f'to approve and attribute: BUDGET_APPROVE="{PLAIN}@3.10=otto:x+4"'
        )
    ]
    assert v.reason == "growth"
    assert v.grown == [(PLAIN, "3.10")]


def test_growth_on_a_tracked_surface_is_judged_like_any_other(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {TRACKED: 501}, "buckets": {TRACKED: {"otto:*": 501}}})
    assert len(v.stop) == 1
    assert v.stop[0].startswith(f"3.10 {TRACKED} 500 -> 501")


def test_an_approved_surface_without_buckets_is_unattributed(tmp_path: Path):
    v = _judge(tmp_path, GROWN, [PLAIN])
    assert v.stop == [
        (
            f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: approved but not attributed; "
            f'to attribute: BUDGET_APPROVE="{PLAIN}@3.10=stdlib+4"'
        )
    ]
    assert v.grown == [(PLAIN, "3.10")], "growth is growth whatever the approval said"


def test_an_approval_with_the_measured_buckets_is_named_and_used(tmp_path: Path):
    v = _judge(tmp_path, GROWN, [f"{PLAIN}=stdlib+4"])
    assert v.stop == []
    assert v.named == [f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: approved as {PLAIN}=stdlib+4"]
    assert v.used == [Approval(PLAIN, None, {"stdlib": 4})]


def test_an_approval_whose_buckets_differ_stops(tmp_path: Path):
    v = _judge(tmp_path, GROWN, [f"{PLAIN}=stdlib+3"])
    assert v.stop == [
        (
            f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: approved as {PLAIN}=stdlib+3 but moved "
            f"{{'stdlib': 4}} (the tip is not the tree that was approved); "
            f'to attribute: BUDGET_APPROVE="{PLAIN}@3.10=stdlib+4"'
        )
    ]


def test_a_minor_specific_entry_wins_over_the_generic_one(tmp_path: Path):
    v = _judge(tmp_path, GROWN, [f"{PLAIN}=stdlib+9", f"{PLAIN}@3.10=stdlib+4"])
    assert v.stop == []
    assert v.used == [Approval(PLAIN, "3.10", {"stdlib": 4})]
    other = _judge(tmp_path, GROWN, [f"{PLAIN}@3.14=stdlib+4"])
    assert len(other.stop) == 1
    assert "grew and is not approved" in other.stop[0], "an entry for another minor does not apply"


def test_a_bucket_name_a_word_cannot_hold_stops_by_name(tmp_path: Path):
    v = _judge(
        tmp_path, {"ops": {PLAIN: 501}, "buckets": {PLAIN: {"otto:*": 500, "otto:a,b": 1}}}, [PLAIN]
    )
    assert v.stop == [
        (
            f"3.10 {PLAIN} 500 -> 501 {{'otto:a,b': 1}}: bucket 'otto:a,b' cannot be written as an "
            "approval word (it holds ',' or '='); rename the path"
        )
    ]


def test_a_decline_is_reported_and_never_stops(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {PLAIN: 490}, "buckets": {PLAIN: {"otto:*": 490}}})
    assert v.stop == []
    assert v.declined == [f"3.10 {PLAIN} 500 -> 490"]
    assert v.reason is None


def test_a_shuffle_at_an_unchanged_total_needs_confirmation(tmp_path: Path):
    v = _judge(tmp_path, SHUFFLED)
    assert v.stop == []
    assert v.shuffles == [f"3.10 {PLAIN} 500 moved {{'otto:*': -2, 'otto:x': 2}}"]
    assert v.reason == "a shuffle"


def test_a_surface_the_base_cannot_run_is_listed_not_judged(tmp_path: Path):
    base = _side(tmp_path, "base", exits={REAL: META[REAL].expect_exit + 1}, ops={REAL: 7})
    tip = _side(tmp_path, "tip", ops={REAL: 900}, buckets={REAL: {"otto:*": 900}})
    v = judge_minor(base, tip, "3.10", NONE)
    assert v.stop == []
    assert v.unmeasurable == {
        REAL: f"exited {META[REAL].expect_exit + 1}, not {META[REAL].expect_exit}"
    }
    assert v.grown == []


BOOM = "`x`: the measured command exited 1 with no exception, not exit 1 having imported y"
"""A harness ending verdict: the run ended with the expected exit, but not as the surface."""


def test_a_base_row_with_the_expected_exit_that_did_not_end_as_expected_is_listed(
    tmp_path: Path,
):
    """The lab failed to load at the tag: same exit, another path; the harness says so."""
    base = _side(tmp_path, "base", endings={REAL: [BOOM, "and more"]}, ops={REAL: 7})
    tip = _side(tmp_path, "tip", ops={REAL: 900}, buckets={REAL: {"otto:*": 900}})
    v = judge_minor(base, tip, "3.10", NONE)
    assert v.unmeasurable == {REAL: f"{BOOM}; and more"}
    assert (v.stop, v.grown) == ([], [])


def test_a_canary_the_base_cannot_run_lists_every_repo_surface(tmp_path: Path, capsys):
    """The generated repo is one fixture: no repo surface's base is evidence once it cannot run."""
    [canary] = FIXTURE_CANARIES
    base = _side(tmp_path, "base", endings={canary: [BOOM]})
    grown = dict.fromkeys(REPO_SURFACES, 900)
    tip = _side(tmp_path, "tip", ops=grown, buckets={k: {"otto:*": 900} for k in grown})
    v = judge_minor(base, tip, "3.10", NONE)
    because = f"the generated repo does not run at base: {canary}: {BOOM}"
    assert v.unmeasurable == {k: (BOOM if k == canary else because) for k in REPO_SURFACES}
    assert (v.stop, v.grown) == ([], [])
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10"]) == 0
    out = capsys.readouterr().out
    assert f"not measurable at base:\n  {min(REPO_SURFACES)}: " in out
    assert f"  help_repo: {because}" in out
    assert "| help_repo |" not in out, "an unmeasurable surface is not tabled"


# --- reproduced, decide, stale -----------------------------------------------


def test_reproduced_compares_the_exit_the_ending_and_the_workspace(tmp_path: Path):
    first = _side(tmp_path, "first")
    exit_ = _side(tmp_path, "exit", exits={REAL: 7})
    assert reproduced(first, exit_, "3.10") == [
        f"{REAL}: ended differently ({META[REAL].expect_exit} vs 7)"
    ]
    ending = _side(tmp_path, "ending", endings={REAL: [BOOM]})
    assert reproduced(first, ending, "3.10") == [
        f"{REAL}: measurable in one measurement, not the other"
    ]
    other = _side(tmp_path, "other", endings={REAL: [BOOM + " /tmp/otto-budget-b"]})
    both = _side(tmp_path, "both", endings={REAL: [BOOM + " /tmp/otto-budget-a"]})
    assert reproduced(other, both, "3.10") == [], "unmeasurable on both: nothing else compared"
    ws = _side(tmp_path, "ws")
    _edit(ws / "3.10.json", lambda d: [r.update(workspace=11) for r in d if r["surface"] == REAL])
    assert reproduced(first, ws, "3.10") == [f"{REAL}: workspace 10 vs 11"]


def test_a_base_that_could_not_run_a_surface_once_does_not_confirm_the_pair(tmp_path: Path):
    """Pair 1's base skipped a surface; pair 2's measured it, and the tip grew there.

    Approved growth elsewhere asks for the second pair; were the exit not part
    of the record, pair 2 would "confirm" pair 1 and the unapproved growth
    pair 2 measured would never be judged.
    """
    tip = {
        "ops": {PLAIN: 504, REAL: 900},
        "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}, REAL: {"otto:*": 900}},
    }
    pairs = [
        (_side(tmp_path, "b0", exits={REAL: 9}), _side(tmp_path, "t0", **tip)),
        (_side(tmp_path, "b1"), _side(tmp_path, "t1", **tip)),
    ]
    v = decide(pairs, "3.10", parse_approvals([f"{PLAIN}=stdlib+4"]))
    assert v.confirmed is None
    assert v.more
    assert f"base {REAL}: ended differently (9 vs {META[REAL].expect_exit})" in v.stop[0]
    assert "needs a third measurement" in v.stop[0]


def test_a_surface_neither_measurement_could_run_confirms_despite_a_temp_path(tmp_path: Path):
    """The ending quotes a per-run mkdtemp path; two such endings still agree."""
    tip = {
        "ops": {PLAIN: 504, REAL: 900},
        "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}, REAL: {"otto:*": 900}},
    }
    pairs = [
        (_side(tmp_path, f"b{i}", endings={REAL: [f"{BOOM} /tmp/otto-budget-{i}x"]}),
         _side(tmp_path, f"t{i}", **tip))
        for i in range(2)
    ]  # fmt: skip
    assert reproduced(pairs[0][0], pairs[1][0], "3.10") == []
    v = decide(pairs, "3.10", parse_approvals([f"{PLAIN}=stdlib+4"]))
    assert (v.confirmed, v.more) == (2, False)
    assert v.stop == []


def test_reproduced_names_every_record_that_differs(tmp_path: Path):
    first, same = _side(tmp_path, "first"), _side(tmp_path, "same")
    assert reproduced(first, same, "3.10") == []
    moved = _side(tmp_path, "moved", buckets={PLAIN: {"otto:*": 499, "otto:y": 1}})
    assert reproduced(first, moved, "3.10") == [
        f"{PLAIN}: {{'otto:*': 500}} vs {{'otto:*': 499, 'otto:y': 1}}"
    ]
    more = _side(tmp_path, "more", ops={REAL: 501}, buckets={REAL: {"otto:*": 501}})
    assert reproduced(first, more, "3.10") == [f"{REAL}: 500 vs 501 file ops"]


def test_decide_passes_a_quiet_pair_on_one_measurement(tmp_path: Path):
    v = decide(_pairs(tmp_path, QUIET), "3.10", NONE)
    assert v.stop == []
    assert (v.confirmed, v.evidence, v.more) == (None, 0, False)


def test_decide_asks_for_a_second_pair_for_growth_or_a_shuffle(tmp_path: Path):
    v = decide(_pairs(tmp_path, GROWN), "3.10", NONE)
    assert v.stop == ["3.10 needs a second measurement to confirm growth (judge --pair twice)"]
    assert (v.more, v.reason, v.grown) == (True, "growth", [])
    s = decide(_pairs(tmp_path, SHUFFLED), "3.10", NONE)
    assert s.stop == ["3.10 needs a second measurement to confirm a shuffle (judge --pair twice)"]
    assert (s.more, s.reason) == (True, "a shuffle")


def test_two_agreeing_pairs_confirm_the_first_verdict(tmp_path: Path):
    approve = parse_approvals([f"{PLAIN}=stdlib+4"])
    v = decide(_pairs(tmp_path, GROWN, GROWN), "3.10", approve)
    assert v.stop == []
    assert (v.confirmed, v.evidence, v.more) == (2, 0, False)
    assert v.grown == [(PLAIN, "3.10")]
    s = decide(_pairs(tmp_path, SHUFFLED, SHUFFLED), "3.10", NONE)
    assert s.stop == [f"3.10 {PLAIN} 500 moved {{'otto:*': -2, 'otto:x': 2}}: reproduced"]


def test_a_quiet_first_pair_is_no_veto_over_two_agreeing_grown_pairs(tmp_path: Path):
    w = decide(_pairs(tmp_path, QUIET, GROWN, GROWN), "3.10", NONE)
    assert len(w.stop) == 1
    assert "grew and is not approved" in w.stop[0]
    assert (w.confirmed, w.evidence) == (3, 1)


def test_a_second_pair_that_disagrees_asks_for_a_third(tmp_path: Path):
    v = decide(_pairs(tmp_path, GROWN, QUIET), "3.10", NONE)
    assert v.stop == [
        (
            f"3.10 did not reproduce (pair 1 vs 2):\n  tip {PLAIN}: 504 vs 500 file ops\n"
            "  needs a third measurement (judge --pair three times)"
        )
    ]
    assert (v.more, v.grown, v.evidence) == (True, [], None), "the first pair's growth is not kept"


def test_the_second_and_third_pairs_decide_when_they_agree(tmp_path: Path):
    v = decide(_pairs(tmp_path, GROWN, QUIET, QUIET), "3.10", NONE)
    assert v.stop == []
    assert (v.confirmed, v.evidence, v.grown) == (3, 1, [])


def test_three_pairs_that_never_agree_are_inconclusive_in_decide(tmp_path: Path):
    other = {"ops": {PLAIN: 503}, "buckets": {PLAIN: {"otto:*": 503}}}
    v = decide(_pairs(tmp_path, GROWN, QUIET, other), "3.10", NONE)
    assert v.stop == [
        (
            f"3.10 inconclusive after three measurements:\n"
            f"  pair 1 vs 2: tip {PLAIN}: 504 vs 500 file ops\n"
            f"  pair 2 vs 3: tip {PLAIN}: 500 vs 503 file ops\n"
            "  measure again when the box is quiet"
        )
    ]
    assert (v.grown, v.named, v.evidence, v.more) == ([], [], None, False)


def test_a_base_that_does_not_reproduce_is_named_as_the_base(tmp_path: Path):
    pairs = _pairs(tmp_path, GROWN, GROWN)

    def bump(d):
        for r in d:
            if r["surface"] == REAL:
                r.update(file_ops=501, by_bucket={"otto:*": 501})

    _edit(pairs[1][0] / "3.10.json", bump)
    v = decide(pairs, "3.10", NONE)
    assert v.stop[0].startswith(
        f"3.10 did not reproduce (pair 1 vs 2):\n  base {REAL}: 500 vs 501 file ops"
    )


def test_decide_refuses_more_than_three_pairs(tmp_path: Path):
    with pytest.raises(ValueError, match="at most 3 pairs"):
        decide(_pairs(tmp_path, QUIET, QUIET, QUIET, QUIET), "3.10", NONE)


def test_stale_approvals_are_growth_based_and_span_wide():
    grown = [(PLAIN, "3.14"), (REAL, "3.10")]
    approvals = parse_approvals(
        [f"{PLAIN}@3.14=stdlib+4", f"{REAL}=otto:*+9", "help@3.10", f"{PLAIN}=stdlib+1"]
    )
    assert stale_approvals(approvals, grown) == [
        "help@3.10: approved but help did not grow on 3.10 (stale BUDGET_APPROVE)",
    ]
    assert stale_approvals(approvals[:1], []) == [
        f"{PLAIN}@3.14=stdlib+4: approved but {PLAIN} did not grow on 3.14 (stale BUDGET_APPROVE)"
    ]
    assert stale_approvals(approvals[3:], []) == [
        (
            f"{PLAIN}=stdlib+1: approved but {PLAIN} did not grow on any measured minor "
            "(stale BUDGET_APPROVE)"
        )
    ]
    assert stale_approvals(approvals[3:], [], unresolved=["3.14"]) == [], (
        "a generic entry waits on every minor"
    )
    assert stale_approvals(approvals[2:3], [], unresolved=["3.10"]) == [], (
        "a scoped entry waits on its minor"
    )
    assert len(stale_approvals(approvals[2:3], [], unresolved=["3.14"])) == 1, (
        "another minor's wait is not its own"
    )


# --- the CLI's judge / validate / tables -------------------------------------


def test_judge_exits_one_on_a_stop_and_prints_the_tables(tmp_path: Path, capsys):
    base = _side(tmp_path, "base", minors=("3.10", "3.11"))
    tip = _side(
        tmp_path, "tip", minors=("3.10", "3.11"), ops={PLAIN: 501}, buckets={PLAIN: {"otto:*": 501}}
    )
    rc = main(
        [
            "judge",
            "--pair",
            str(base),
            str(tip),
            "--pair",
            str(base),
            str(tip),
            "--minors",
            "3.10",
            "3.11",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "CPython 3.10" in out
    assert "CPython 3.11" in out
    assert f"| {PLAIN} | 500 | 501 | +1 |" in out
    assert "STOP:" in out
    assert "grew and is not approved" in out


def test_judge_prints_the_deciding_pair_s_tables(tmp_path: Path, capsys):
    pairs = _pairs(tmp_path, GROWN, QUIET, QUIET)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10"]) == 0
    out = capsys.readouterr().out
    assert f"| {PLAIN} | 500 | 500 | +0 |" in out
    assert "504" not in out, "the discarded first pair's growth is not shown as evidence"
    assert "confirmed by pair 3 (tables: pair 2)" in out


def test_judge_labels_every_pair_s_tables_when_inconclusive(tmp_path: Path, capsys):
    other = {"ops": {PLAIN: 503}, "buckets": {PLAIN: {"otto:*": 503}}}
    pairs = _pairs(tmp_path, GROWN, QUIET, other)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10"]) == 1
    out = capsys.readouterr().out
    assert "CPython 3.10 (pair 1)" in out
    assert "CPython 3.10 (pair 2)" in out
    assert "CPython 3.10 (pair 3)" in out


def test_judge_exits_zero_when_nothing_grew_and_reports_declines(tmp_path: Path, capsys):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", ops={PLAIN: 499}, buckets={PLAIN: {"otto:*": 499}})
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10"]) == 0
    assert f"DECLINED:\n  3.10 {PLAIN} 500 -> 499" in capsys.readouterr().out


def test_judge_lists_what_the_base_cannot_run_with_the_reason(tmp_path: Path, capsys):
    expected = META[REAL].expect_exit
    base = _side(tmp_path, "base", exits={REAL: expected + 1}, endings={PLAIN: [BOOM]})
    tip = _side(tmp_path, "tip")
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10"]) == 0
    listed = f"  {PLAIN}: {BOOM}\n  {REAL}: exited {expected + 1}, not {expected}"
    assert f"not measurable at base:\n{listed}" in capsys.readouterr().out


def test_judge_refuses_a_tip_that_cannot_run_a_surface(tmp_path: Path, capsys):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", exits={REAL: META[REAL].expect_exit + 1})
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10"]) == 2
    assert (
        f"3.10 {REAL} exited {META[REAL].expect_exit + 1}, not {META[REAL].expect_exit}"
        in capsys.readouterr().out
    )


def test_judge_refuses_a_tip_row_that_did_not_end_as_expected(tmp_path: Path, capsys):
    """The expected exit is not enough at the tip either: the harness's verdict is the rule."""
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", endings={REAL: [BOOM, "and more"]})
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10"]) == 2
    assert (
        f"{tip}: 3.10 {REAL} did not end as expected: {BOOM}; and more" in capsys.readouterr().out
    )


def test_judge_names_every_invalid_side_not_only_the_first(tmp_path: Path, capsys):
    base, tip = (
        _side(tmp_path, "base", minors=("3.10", "3.11")),
        _side(tmp_path, "tip", minors=("3.10", "3.11")),
    )
    (base / "3.10.status.json").unlink()
    _edit(tip / "3.11.json", lambda d: [r.update(exit=99) for r in d if r["surface"] == REAL])
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10", "3.11"]) == 2
    out = capsys.readouterr().out
    assert "3.10 not measured (no status)" in out
    assert f"3.11 {REAL} exited 99" in out


def test_judge_refuses_a_bad_approval_before_reading_any_record(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    (tip / "3.10.json").write_text("{not json")  # would be a different message if read
    assert (
        main(["judge", "--pair", str(base), str(tip), "--minors", "3.10", "--approve", "nope"]) == 2
    )
    out = capsys.readouterr().out
    assert "--approve nope: not a known surface" in out
    assert "unreadable" not in out


def test_judge_refuses_a_fourth_pair(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    args = ["--pair", str(base), str(tip)] * 4
    assert main(["judge", *args, "--minors", "3.10"]) == 2
    assert "at most 3 pairs" in capsys.readouterr().out


def test_judge_reports_a_stale_approval_at_the_end(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    assert (
        main(
            [
                "judge",
                "--pair",
                str(base),
                str(tip),
                "--minors",
                "3.10",
                "--approve",
                f"{PLAIN}=otto:*+1",
            ]
        )
        == 1
    )
    assert (
        f"STOP:\n  {PLAIN}=otto:*+1: approved but {PLAIN} did not grow on any measured minor"
        in capsys.readouterr().out
    )


def test_judge_does_not_call_an_approval_stale_while_its_minor_is_unresolved(
    tmp_path: Path, capsys
):
    """A pending or inconclusive minor has not shown the surface did NOT grow."""
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip", **GROWN)
    assert (
        main(
            [
                "judge",
                "--pair",
                str(base),
                str(tip),
                "--minors",
                "3.10",
                "--approve",
                f"{PLAIN}=stdlib+4",
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "needs a second measurement" in out
    assert "stale" not in out
    other = {"ops": {PLAIN: 503}, "buckets": {PLAIN: {"otto:*": 503}}}
    pairs = _pairs(tmp_path, GROWN, QUIET, other)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10", "--approve", f"{PLAIN}@3.10=stdlib+4"]) == 1
    out = capsys.readouterr().out
    assert "inconclusive" in out
    assert "stale" not in out


def test_validate_prints_valid_or_every_problem(tmp_path: Path, capsys):
    side = _side(tmp_path, "s", minors=("3.10", "3.11"))
    assert main(["validate", str(side), "--minors", "3.10", "3.11"]) == 0
    assert capsys.readouterr().out.strip() == "valid"
    _edit(side / "3.11.status.json", lambda d: d.update({"report": 3}))
    assert main(["validate", str(side), "--minors", "3.10", "3.11"]) == 1
    assert "3.11 step report exited 3" in capsys.readouterr().out


def test_tables_prints_every_minor_and_the_totals(tmp_path: Path, capsys):
    base = _side(tmp_path, "base", minors=("3.10", "3.14"))
    tip = _side(
        tmp_path, "tip", minors=("3.10", "3.14"), ops={PLAIN: 510}, buckets={PLAIN: {"otto:*": 510}}
    )
    assert main(["tables", str(base), str(tip), "--minors", "3.10", "3.14"]) == 0
    out = capsys.readouterr().out
    n = len(KNOWN_SURFACES)
    assert f"| 3.10 | {n} | {n * 500} | {n * 500 + 10} | 1 |" in out
    assert f"| 3.14 | {n} | {n * 500} | {n * 500 + 10} | 1 |" in out


# --- measure ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    """The delta's own git calls inherit the process env: keep the host's config out of them."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _repo(tmp_path: Path) -> "tuple[TmpGitRepo, str, str]":
    """A repo with a tagged base commit and a tip commit; returns (repo, base sha, tip sha)."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("scripts/import_budget.py", "# base\n")
    repo.write("tests/_fixtures/generated_repo.py", "# fixtures v1\n")
    base = repo.commit("base")
    repo.git("tag", "v1.0.0")
    repo.write("scripts/import_budget.py", "# tip\n")
    tip = repo.commit("tip")
    return repo, base, tip


class FakeRunner:
    """Plays uv and the measured interpreters: writes the files each step would, logs every call.

    The minor of a measurement step comes from UV_PROJECT_ENVIRONMENT's
    `venv-<minor>` suffix, as the real steps' does; a standalone interpreter
    probe (`/fake/python<minor> -c …`, from `uv python find`) carries its minor
    in argv[0] and is logged under `probes`, not `calls`. The TREE a report
    describes is the tree the venv was synced from (`synced`, by venv), read
    from the first line of scripts/import_budget.py in the sync's cwd; the
    report step itself runs from the tip worktree, whose first line is "# tip".
    *ops* maps (tree, surface) -> file_ops. *series* maps (tree, minor) -> per-
    report overrides applied to successive reports (an unstable measurement).
    *fail* maps a step to the exit status to return. *versions* maps a minor
    to the full version every interpreter of that minor reports.
    """

    def __init__(self, ops=None, fail=None, series=None, versions=None):
        self.ops = ops or {}
        self.fail = fail or {}
        self.series = series or {}
        self.versions = versions or {}
        self.calls: "list[tuple[str, str, str]]" = []  # (step, tree, minor)
        self.probes: "list[str]" = []  # minors whose interpreter was looked up
        self.synced: "dict[str, str]" = {}  # venv -> tree
        self.reports: "dict[tuple[str, str], int]" = {}

    @staticmethod
    def _tree(cwd: Path) -> str:
        return (cwd / "scripts/import_budget.py").read_text().splitlines()[0].removeprefix("# ")

    def _version(self, minor: str) -> str:
        return self.versions.get(minor, f"{minor}.14")

    def __call__(
        self, argv: "list[str]", cwd: Path, env: "dict[str, str]", out: "Path | None"
    ) -> int:
        if argv[:3] == ["uv", "python", "find"]:
            assert "VIRTUAL_ENV" not in env, "the probe runs venv-clean, as the sync does"
            assert "--system" in argv, "a .venv above the cwd is not the sync's interpreter"
            assert out is not None
            out.write_text(f"/fake/python{argv[-1]}\n")
            return 0
        if argv[0].startswith("/fake/python"):
            assert "VIRTUAL_ENV" not in env
            assert argv[1:3] == ["-c", delta.VERSION_SNIPPET]
            assert out is not None
            minor = argv[0].removeprefix("/fake/python")
            self.probes.append(minor)
            out.write_text(self._version(minor) + "\n")
            return 0
        venv = env["UV_PROJECT_ENVIRONMENT"]
        minor = venv.rsplit("venv-", 1)[-1]
        if argv[:2] == ["uv", "sync"]:
            assert "--frozen" in argv
            assert argv[argv.index("--python") + 1] == minor
            self.synced[venv] = self._tree(cwd)
            self.calls.append(("sync", self._tree(cwd), minor))
            return self.fail.get("sync", 0)
        tree = self.synced[venv]
        assert argv[0] == str(Path(venv) / "bin" / "python"), "the measured venv's interpreter"
        if argv[1:3] == ["-c", delta.VERSION_SNIPPET]:
            assert out is not None
            out.write_text(self._version(minor) + "\n")
            self.calls.append(("version", tree, minor))
            return self.fail.get("version", 0)
        if "--report-json" in argv:
            assert argv[1] == "scripts/import_budget.py"
            assert self._tree(cwd) == "tip", "the tip's harness, from the tip worktree"
            self.calls.append(("report", tree, minor))
            n = self.reports[(tree, minor)] = self.reports.get((tree, minor), 0) + 1
            overrides = self.series.get((tree, minor), [])
            override = overrides[n - 1] if n - 1 < len(overrides) else {}
            table = []
            for k in KNOWN_SURFACES:
                ops = override.get(k, self.ops.get((tree, k), 500))
                table.append(_row(k, ops, {"otto:*": ops}))
            Path(argv[argv.index("--report-json") + 1]).write_text(json.dumps(table))
            return self.fail.get("report", 0)
        raise AssertionError(f"unexpected command {argv}")


def _settings(
    repo: TmpGitRepo, out: Path, minors=("3.10",), approve=(), base=None, tip="HEAD"
) -> Settings:
    return Settings(
        repo=repo.root, out=out, base=base or base_ref(repo.root), tip=tip,
        minors=list(minors), approve=list(approve), holder_root=out.parent / "holders",
    )  # fmt: skip


def _steps(runner: FakeRunner, tree: str, minor: str = "3.10") -> "list[str]":
    return [s for s, t, m in runner.calls if t == tree and m == minor]


def test_base_ref_is_the_nearest_tag(tmp_path: Path):
    repo, _base, _tip = _repo(tmp_path)
    assert base_ref(repo.root) == "v1.0.0"


def test_instrument_id_is_the_harness_blob_and_the_fixtures_tree(tmp_path: Path):
    repo, base_sha, tip_sha = _repo(tmp_path)
    blob = repo.git("rev-parse", f"{tip_sha}:scripts/import_budget.py").strip()
    tree = repo.git("rev-parse", f"{tip_sha}:tests/_fixtures").strip()
    assert instrument_id(repo.root, tip_sha) == f"{blob}:{tree}"
    assert instrument_id(repo.root, base_sha) != instrument_id(repo.root, tip_sha), (
        "the harness changed between them"
    )


def _holders_empty(root: Path) -> bool:
    """True when the stable holder directory under *root* keeps no `<side>-<minor>` holder."""
    return not list((root / "otto-budget-delta").iterdir())


def test_a_quiet_pair_passes_and_leaves_base_and_head_records(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/caller/.venv")  # what `uv run` exports to the script
    repo, base_sha, tip_sha = _repo(tmp_path)
    runner = FakeRunner()
    out = tmp_path / "out"
    assert measure(_settings(repo, out), runner) == 0
    base, head = out / "v1.0.0", out / "head"
    instrument = instrument_id(repo.root, tip_sha)
    base_tree = repo.git("rev-parse", "v1.0.0^{tree}").strip()
    tip_tree = repo.git("rev-parse", "HEAD^{tree}").strip()
    assert validate_side(base, "3.10", Provenance(base_sha, base_tree, "3.10.14", instrument)) == []
    assert validate_side(head, "3.10", Provenance(tip_sha, tip_tree, "3.10.14", instrument)) == []
    assert _steps(runner, "base") == ["sync", "version", "report"]
    assert _steps(runner, "tip") == ["sync", "version", "report"]
    assert runner.probes == ["3.10"]
    assert not (out / "again-1").exists(), "a quiet pair is measured once"
    assert "| 3.10 |" in capsys.readouterr().out
    assert repo.git("worktree", "list").count("\n") == 1, "every throwaway worktree is removed"
    assert _holders_empty(tmp_path / "holders"), "every holder is removed"
    status = json.loads((head / "3.10.status.json").read_text())
    assert set(status["elapsed"]) == {"sync", "version", "report"}
    for side in (base, head):
        assert sorted(p.name for p in side.iterdir()) == ["3.10.json", "3.10.status.json"]
    assert sorted(p.name for p in (out / "scratch").iterdir()) == [
        "3.10.python.txt", "find-3.10.txt", "version-3.10.txt"
    ]  # fmt: skip


def test_a_quiet_run_over_two_minors_measures_each_minor_once_per_side(tmp_path: Path):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner()
    assert measure(_settings(repo, tmp_path / "out", minors=("3.10", "3.11")), runner) == 0
    assert runner.calls == [
        ("sync", "base", "3.10"), ("version", "base", "3.10"), ("report", "base", "3.10"),
        ("sync", "tip", "3.10"), ("version", "tip", "3.10"), ("report", "tip", "3.10"),
        ("sync", "base", "3.11"), ("version", "base", "3.11"), ("report", "base", "3.11"),
        ("sync", "tip", "3.11"), ("version", "tip", "3.11"), ("report", "tip", "3.11"),
    ]  # fmt: skip


def test_the_base_cache_is_reused_and_the_tip_is_measured_again(tmp_path: Path):
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    second = FakeRunner()
    assert measure(_settings(repo, out), second) == 0
    assert _steps(second, "base") == [], "the cached base is not measured again"
    assert _steps(second, "tip") == ["sync", "version", "report"]


@pytest.mark.parametrize(
    "break_it",
    [
        "no_status",
        "failed_step",
        "other_sha",
        "other_tree",
        "other_patch",
        "other_instrument",
        "missing_row",
    ],
)
def test_a_base_cache_that_is_not_the_base_or_not_this_instrument_is_measured_again(
    tmp_path: Path, break_it: str
):
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    base = out / "v1.0.0"
    again = FakeRunner()
    if break_it == "no_status":
        (base / "3.10.status.json").unlink()
    elif break_it == "failed_step":
        _edit(base / "3.10.status.json", lambda d: d.update({"report": 1}))
    elif break_it == "other_sha":
        _edit(base / "3.10.status.json", lambda d: d.update({"sha": "0" * 40}))
    elif break_it == "other_tree":
        _edit(base / "3.10.status.json", lambda d: d.update({"tree": "0" * 40}))
    elif break_it == "other_patch":
        # The interpreter uv finds today is another patch.
        again = FakeRunner(versions={"3.10": "3.10.15"})
    elif break_it == "other_instrument":
        # The instrument changed, the subject did not.
        repo.write("tests/_fixtures/generated_repo.py", "# fixtures v2\n")
        repo.commit("fixture change")
    else:
        _edit(base / "3.10.json", _drop(PLAIN))
    assert measure(_settings(repo, out), again) == 0
    assert _steps(again, "base") == ["sync", "version", "report"], break_it
    assert validate_side(base, "3.10", None) == []


def test_an_interpreter_uv_cannot_find_says_the_cache_cannot_be_reused(tmp_path: Path, capsys):
    class NoProbe(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            if argv[:3] == ["uv", "python", "find"]:
                return 2
            return super().__call__(argv, cwd, env, out)

    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    capsys.readouterr()
    runner = NoProbe()
    assert measure(_settings(repo, out), runner) == 0
    text = capsys.readouterr().out
    assert "interpreter for 3.10 not found by uv; the base cache cannot be reused" in text
    assert _steps(runner, "base") == ["sync", "version", "report"], "the base is measured"


def test_a_cache_hit_still_confirms_growth_with_a_fresh_base_pair(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    runner = FakeRunner(ops={("tip", PLAIN): 504})
    assert measure(_settings(repo, out), runner) == 1
    assert _steps(runner, "base") == ["sync", "version", "report"], (
        "the confirming pair measures the base afresh"
    )
    assert _steps(runner, "tip") == ["sync", "version", "report"] * 2
    assert "confirmed by pair 2 (tables: pair 1)" in capsys.readouterr().out


def test_growth_is_confirmed_by_a_second_pair_and_stops_with_the_entry_to_paste(
    tmp_path: Path, capsys
):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(ops={("tip", PLAIN): 504})
    out = tmp_path / "out"
    assert measure(_settings(repo, out), runner) == 1
    assert _steps(runner, "base") == ["sync", "version", "report"] * 2
    assert _steps(runner, "tip") == ["sync", "version", "report"] * 2
    assert validate_side(out / "again-1" / "v1.0.0", "3.10", None) == []
    assert validate_side(out / "again-1" / "head", "3.10", None) == []
    text = capsys.readouterr().out
    assert "confirming 3.10: growth appeared; measuring the pair again (2 of 3)" in text
    assert "confirmed by pair 2 (tables: pair 1)" in text
    assert f'BUDGET_APPROVE="{PLAIN}@3.10=otto:*+4"' in text
    assert "STOP:" in text


def test_approved_and_attributed_growth_passes_after_confirmation(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(ops={("tip", PLAIN): 504})
    assert measure(_settings(repo, tmp_path / "out", approve=[f"{PLAIN}=otto:*+4"]), runner) == 0
    assert "APPROVED GROWTH" in capsys.readouterr().out


def test_growth_on_the_canary_only_passes_with_a_canary_entry_and_no_stale_stop(
    tmp_path: Path, capsys
):
    class CanaryGrows(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            minor = env.get("UV_PROJECT_ENVIRONMENT", "").rsplit("venv-", 1)[-1]
            self.ops = {("tip", PLAIN): 504} if minor == "3.14" else {}
            return super().__call__(argv, cwd, env, out)

    repo, _b, _t = _repo(tmp_path)
    settings = _settings(
        repo, tmp_path / "out", minors=("3.10", "3.14"), approve=[f"{PLAIN}@3.14=otto:*+4"]
    )
    assert measure(settings, CanaryGrows()) == 0
    text = capsys.readouterr().out
    assert "stale" not in text
    assert "APPROVED GROWTH" in text
    unapproved = _settings(repo, tmp_path / "out2", minors=("3.10", "3.14"))
    assert measure(unapproved, CanaryGrows()) == 1
    assert "grew and is not approved" in capsys.readouterr().out


def test_a_second_pair_that_disagrees_gets_a_third_and_the_agreeing_pairs_decide(
    tmp_path: Path, capsys
):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(series={("tip", "3.10"): [{PLAIN: 504}, {}, {}]})
    assert measure(_settings(repo, tmp_path / "out"), runner) == 0
    text = capsys.readouterr().out
    assert "measuring the pair again (3 of 3)" in text
    assert "confirmed by pair 3 (tables: pair 2)" in text
    assert "504" not in text, "the discarded first pair is not shown as evidence"
    assert "STOP" not in text


def test_three_pairs_that_never_agree_are_inconclusive_in_measure(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(series={("tip", "3.10"): [{PLAIN: 504}, {}, {PLAIN: 503}]})
    assert measure(_settings(repo, tmp_path / "out"), runner) == 1
    text = capsys.readouterr().out
    assert "3.10 inconclusive after three measurements" in text
    assert f"tip {PLAIN}: 504 vs 500 file ops" in text
    assert "CPython 3.10 (pair 3)" in text
    assert "grew" not in text


def test_a_shuffle_that_reproduces_stops(tmp_path: Path, capsys):
    class Shuffling(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            rc = super().__call__(argv, cwd, env, out)
            if "--report-json" in argv and self.calls[-1][1] == "tip":
                path = Path(argv[argv.index("--report-json") + 1])
                table = json.loads(path.read_text())
                for r in table:
                    if r["surface"] == PLAIN:
                        r["by_bucket"] = {"otto:*": 498, "otto:x": 2}
                path.write_text(json.dumps(table))
            return rc

    repo, _b, _t = _repo(tmp_path)
    assert measure(_settings(repo, tmp_path / "out"), Shuffling()) == 1
    text = capsys.readouterr().out
    assert "confirming 3.10: a shuffle appeared" in text
    assert f"STOP:\n  3.10 {PLAIN} 500 moved {{'otto:*': -2, 'otto:x': 2}}: reproduced" in text


def test_the_primary_stops_before_the_other_minors_are_measured(tmp_path: Path):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(ops={("tip", PLAIN): 501})
    assert measure(_settings(repo, tmp_path / "out", minors=("3.10", "3.11")), runner) == 1
    assert {m for _s, _t, m in runner.calls} == {"3.10"}, "3.11 never ran"


def test_a_stale_approval_is_reported_after_every_minor(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    assert (
        measure(_settings(repo, tmp_path / "out", approve=[f"{PLAIN}=otto:*+1"]), FakeRunner()) == 1
    )
    assert (
        f"{PLAIN}=otto:*+1: approved but {PLAIN} did not grow on any measured minor"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("step", ["sync", "version", "report"])
def test_a_failed_step_is_two_and_names_the_step(tmp_path: Path, step: str, capsys):
    repo, _b, _t = _repo(tmp_path)
    assert measure(_settings(repo, tmp_path / "out"), FakeRunner(fail={step: 3})) == 2
    assert f"step {step} exited 3" in capsys.readouterr().out


def test_the_same_tree_on_both_sides_is_two_before_any_worktree(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner()
    assert measure(_settings(repo, tmp_path / "out", base="HEAD"), runner) == 2
    assert "same tree" in capsys.readouterr().out
    assert runner.calls == []


def test_a_bad_approval_is_two_before_any_measurement(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner()
    assert measure(_settings(repo, tmp_path / "out", approve=["nope"]), runner) == 2
    assert runner.calls == []


def test_a_git_failure_is_two_not_a_traceback(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    assert measure(_settings(repo, tmp_path / "out", tip="no-such-ref"), FakeRunner()) == 2
    assert "git rev-parse" in capsys.readouterr().out


def test_a_missing_executable_is_two_not_a_traceback(tmp_path: Path, capsys):
    """The default runner turns a missing `uv` (or python) into a MeasureError, never an OSError."""
    repo, _b, _t = _repo(tmp_path)
    with pytest.raises(MeasureError, match="no such executable"):
        delta._run(["/no/such/uv", "sync"], repo.root, dict(os.environ), None)


def test_measure_side_runs_the_three_steps_in_a_sanitized_env_and_names_the_side(
    tmp_path: Path, monkeypatch
):
    repo, base_sha, tip_sha = _repo(tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    envs, cwds = [], []

    class Spy(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            envs.append(dict(env))
            cwds.append(cwd)
            return super().__call__(argv, cwd, env, out)

    spy = Spy()
    log = tmp_path / "run.log"
    job = SideJob(repo=repo.root, name="base", sha=base_sha, harness_sha=tip_sha, minor="3.10",
                  dest=tmp_path / "dest", log=log, scratch=tmp_path / "scratch",
                  holder_root=tmp_path / "holders")  # fmt: skip
    measure_side(job, spy)
    assert validate_side(job.dest, "3.10", None) == []
    assert all("PYTHONPATH" not in e for e in envs)
    assert all(e.get("VIRTUAL_ENV", "") != "/elsewhere" for e in envs)
    venv = tmp_path / "holders" / "otto-budget-delta" / "base-3.10" / "venv-3.10"
    assert {e["UV_PROJECT_ENVIRONMENT"] for e in envs} == {str(venv)}, (
        "one stable holder per side and minor, so the harness's bytecode mirror is reused"
    )
    for inner in envs[1:]:
        assert inner["VIRTUAL_ENV"] == str(venv)
        assert inner["PATH"].startswith(f"{venv / 'bin'}{os.pathsep}"), "the venv's bin first"
    assert [c.name for c in cwds] == ["tree-base", "tree-base", "tree-tip"], (
        "sync and probe in the base tree; the harness from the tip tree"
    )
    assert log.read_text().count("$ ") == 3
    assert re.search(r"\(\d+\.\d s\)", log.read_text()), "elapsed seconds per command"
    assert json.loads((job.dest / "3.10.status.json").read_text())["python"] == "3.10.14"


def _job(repo: TmpGitRepo, tmp_path: Path, sha: str, harness_sha: str) -> SideJob:
    return SideJob(
        repo=repo.root,
        name="base",
        sha=sha,
        harness_sha=harness_sha,
        minor="3.10",
        dest=tmp_path / "dest",
        log=tmp_path / "log",
        scratch=tmp_path / "scratch",
        holder_root=tmp_path / "holders",
    )


def _git_failing_on(real_git, *verbs: str):
    """A `_git` double that raises MeasureError on any call whose first two words are in *verbs*."""

    def fake(repo_path, *args):
        if " ".join(args[:2]) in verbs:
            raise MeasureError(f"git {' '.join(args[:2])} failed: simulated")
        return real_git(repo_path, *args)

    return fake


def test_a_bad_sha_is_refused_before_any_holder_is_made(tmp_path: Path):
    repo, _base_sha, tip_sha = _repo(tmp_path)
    with pytest.raises(MeasureError, match="git rev-parse"):
        measure_side(_job(repo, tmp_path, "0" * 40, tip_sha), FakeRunner())
    assert not (tmp_path / "holders").exists(), "nothing was allocated"


def test_a_failed_worktree_add_leaves_no_holder_behind(tmp_path: Path, monkeypatch):
    repo, base_sha, tip_sha = _repo(tmp_path)
    monkeypatch.setattr(delta, "_git", _git_failing_on(delta._git, "worktree add"))
    with pytest.raises(MeasureError, match="git worktree add"):
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert _holders_empty(tmp_path / "holders"), "the holder is removed on every path"
    status = json.loads((tmp_path / "dest" / "3.10.status.json").read_text())
    assert "sync" not in status, "no step ran; the status still records the attempt"


def test_a_failed_prune_is_reported_beside_the_failed_remove(tmp_path: Path, monkeypatch):
    repo, base_sha, tip_sha = _repo(tmp_path)
    real, calls = delta._git, []

    def fake(repo_path, *args):
        """Let the holder's opening prune through; fail the remove and the prune after it."""
        calls.append(" ".join(args[:2]))
        if calls.count("worktree prune") > 1 or args[:2] == ("worktree", "remove"):
            raise MeasureError(f"git {' '.join(args[:2])} failed: simulated")
        return real(repo_path, *args)

    monkeypatch.setattr(delta, "_git", fake)
    with pytest.raises(MeasureError) as info:
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert "worktree remove failed" in str(info.value)
    assert "worktree prune failed" in str(info.value)


def test_a_holder_that_cannot_be_deleted_is_reported(tmp_path: Path, monkeypatch):
    repo, base_sha, tip_sha = _repo(tmp_path)

    def refuse(path, *args, **kwargs):
        raise OSError(f"simulated: {path}")

    monkeypatch.setattr(delta.shutil, "rmtree", refuse)
    with pytest.raises(MeasureError, match="not deleted: simulated"):
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert validate_side(tmp_path / "dest", "3.10", None) == [], "the measurement is still recorded"


def _raising(argv, cwd, env, out):
    raise OSError("disk")


def test_a_status_that_cannot_be_written_is_reported_and_the_measurement_failure_wins(
    tmp_path: Path,
):
    repo, base_sha, tip_sha = _repo(tmp_path)
    (tmp_path / "dest" / "3.10.status.json").mkdir(parents=True)  # the status path is a directory
    with pytest.raises(MeasureError, match="status not written"):
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    with pytest.raises(MeasureError) as both:
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), _raising)
    text = str(both.value)
    assert text.startswith("disk"), "the measurement's own failure is the error"
    assert "cleanup also failed" in text
    assert "status not written" in text


def _interrupting(argv, cwd, env, out):
    raise KeyboardInterrupt


def test_an_interrupted_measurement_still_cleans_up_and_propagates(tmp_path: Path):
    repo, base_sha, tip_sha = _repo(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), _interrupting)
    assert _holders_empty(tmp_path / "holders"), "the holder is gone"
    assert repo.git("worktree", "list").count("\n") == 1, (
        "both throwaway worktrees are unregistered"
    )


def test_a_filesystem_failure_in_measure_is_two_not_a_traceback(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    (tmp_path / "file").write_text("")
    assert measure(_settings(repo, tmp_path / "file" / "out"), FakeRunner()) == 2
    assert "file" in capsys.readouterr().out


def test_a_cleanup_failure_is_reported_after_the_status_is_written(tmp_path: Path, monkeypatch):
    repo, base_sha, tip_sha = _repo(tmp_path)
    monkeypatch.setattr(delta, "_git", _git_failing_on(delta._git, "worktree remove"))
    job = _job(repo, tmp_path, base_sha, tip_sha)
    with pytest.raises(MeasureError, match="recording or cleanup failed"):
        measure_side(job, FakeRunner())
    assert validate_side(job.dest, "3.10", None) == [], "the measurement itself was recorded"
    assert _holders_empty(tmp_path / "holders"), "the holder is deleted even when git refused"


def test_a_holder_left_by_an_interrupted_run_is_removed_before_the_next(tmp_path: Path):
    """The holder's path is stable, so a leftover (worktree still registered) is cleared first."""
    repo, base_sha, tip_sha = _repo(tmp_path)
    stale = tmp_path / "holders" / "otto-budget-delta" / "base-3.10"
    stale.mkdir(parents=True)
    repo.git("worktree", "add", "--detach", "--quiet", str(stale / "tree-base"), base_sha)
    (stale / "venv-3.10").mkdir()
    (stale / "venv-3.10" / "left-over").write_text("")
    measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert validate_side(tmp_path / "dest", "3.10", None) == []
    assert _holders_empty(tmp_path / "holders")
    assert repo.git("worktree", "list").count("\n") == 1, "the leftover worktree is unregistered"


def test_a_registration_whose_directory_is_gone_is_pruned_before_the_worktree_is_added(
    tmp_path: Path,
):
    """A run killed without cleanup, its holder swept away: the stale path is free again."""
    repo, base_sha, tip_sha = _repo(tmp_path)
    stale = tmp_path / "holders" / "otto-budget-delta" / "base-3.10"
    stale.mkdir(parents=True)
    repo.git("worktree", "add", "--detach", "--quiet", str(stale / "tree-base"), base_sha)
    shutil.rmtree(stale)
    measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert validate_side(tmp_path / "dest", "3.10", None) == []
    assert repo.git("worktree", "list").count("\n") == 1, "no registration is left"


def test_a_git_whose_output_cannot_be_decoded_is_a_measure_error(tmp_path: Path, monkeypatch):
    def undecodable(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(delta.subprocess, "run", undecodable)
    with pytest.raises(MeasureError, match="git status could not run"):
        delta._git(tmp_path, "status")


def test_the_cli_measure_verb_wires_the_default_runner(tmp_path: Path, monkeypatch):
    repo, _b, _t = _repo(tmp_path)
    fake = FakeRunner()
    monkeypatch.setattr(delta, "_run", fake)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    rc = main(
        ["measure", "--repo", str(repo.root), "--out", str(tmp_path / "out"), "--minors", "3.10"]
    )
    assert rc == 0
    assert fake.calls
