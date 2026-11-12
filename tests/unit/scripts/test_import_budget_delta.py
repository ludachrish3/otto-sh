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
import subprocess
import sys
import tempfile
from dataclasses import fields
from pathlib import Path

import pytest

from scripts import import_budget
from scripts import import_budget_delta as delta
from scripts.import_budget_delta import (
    BUCKET_NOISE,
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
    reproduced,
    stale_approvals,
    validate_side,
    within_noise,
)
from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SHA, TREE, PY, INSTR = "a" * 40, "b" * 40, "3.10.14", "c" * 40 + ":" + "d" * 40
PROV = Provenance(tree=TREE, python=PY, instrument=INSTR)
META = {s.key: s for s in import_budget.SURFACES}
REAL = next(s.key for s in import_budget.SURFACES if s.real_entry and not s.tracked)
PLAIN = next(s.key for s in import_budget.SURFACES if not s.real_entry and not s.tracked)
TRACKED = next(s.key for s in import_budget.SURFACES if s.tracked)
NONE: "list[Approval]" = []
BASE_TAG = "v0.20.0"
WHY = "the new lock-file read"
STDLIB4 = [Approval(PLAIN, None, {"stdlib": 4}, WHY)]
EMPTY = delta.Approvals(base=BASE_TAG, in_force=[], spent=[])
GROWN = {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}}}
SHUFFLED = {"buckets": {PLAIN: {"otto:*": 497, "otto:x": 3}}}
WOBBLE = {"fd": 111, "other": 173, "otto:*": 216}
"""v0.17.0's 3.10 host_local_exec: one operation lands in `fd` or `other` from run to run."""
QUIET: dict = {}


def _row(
    key: str,
    ops: int,
    buckets: "dict[str, int] | None" = None,
    exit_code: "int | None" = None,
    ending: "list[str] | None" = None,
    by_path: "dict[str, int] | None" = None,
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
        "by_path": by_path if by_path is not None else {"fd": ops},
    }


def _side(
    root: Path,
    name: str,
    minors: "tuple[str, ...]" = ("3.10",),
    ops: "dict[str, int] | None" = None,
    buckets: "dict[str, dict[str, int]] | None" = None,
    exits: "dict[str, int] | None" = None,
    endings: "dict[str, list[str]] | None" = None,
    paths: "dict[str, dict[str, int]] | None" = None,
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
                (paths or {}).get(k),
            )
            for k in KNOWN_SURFACES
        ]
        (side / f"{minor}.json").write_text(json.dumps(table))
        status = {"sha": SHA, "measured_tree": TREE, "instrument": INSTR, "minor": minor,
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
    assert [s.key for s in import_budget.SURFACES if s.project_files is not None] == REPO_SURFACES


def test_the_fixture_canaries_are_the_repo_surfaces_that_dispatch_its_noop():
    """The canary dispatches the fixture's own `noop`: if that fails, the repo does not run."""
    from tests._fixtures import generated_project

    assert "async def noop(" in generated_project._REALISTIC_INIT
    dispatching = [
        s.key
        for s in import_budget.SURFACES
        if s.project_files is not None and s.real_entry and s.argv[-2:] == ["run", "noop"]
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
        ("measured_tree", "e" * 40, f"measured_tree {'e' * 40}, not {TREE}"),
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


def _entry(surface: str, buckets: dict, minor: "str | None" = None, why: str = WHY) -> dict:
    return {"surface": surface, "minor": minor, "buckets": buckets, "why": why}


def _approvals_path(tmp_path: Path, entries, base: str = BASE_TAG) -> Path:
    path = tmp_path / "approved_growth.json"
    path.write_text(json.dumps({"base": base, "entries": list(entries)}))
    return path


def _approvals(tmp_path: Path, entries, base: str = BASE_TAG, base_tag: str = BASE_TAG):
    path = _approvals_path(tmp_path, entries, base)
    return delta.load_approvals(path, base_tag)


def _judge_args(tmp_path: Path, entries=(), base: str = BASE_TAG) -> "list[str]":
    """The `judge` arguments naming an approvals file and the base tag."""
    return ["--approvals", str(_approvals_path(tmp_path, entries, base)), "--base-tag", BASE_TAG]


def test_load_approvals_reads_in_force_entries_and_refuses_by_entry(tmp_path: Path):
    got = _approvals(
        tmp_path, [_entry(PLAIN, {"stdlib": 4}), _entry(PLAIN, {"fd": 1}, "3.14", "x")]
    )
    assert (got.base, got.spent) == (BASE_TAG, [])
    assert got.in_force == [
        Approval(PLAIN, None, {"stdlib": 4}, WHY),
        Approval(PLAIN, "3.14", {"fd": 1}, "x"),
    ]
    assert got.in_force[0].entry() == _entry(PLAIN, {"stdlib": 4})
    assert str(delta.APPROVALS_PATH) == "tests/unit/import_budget/approved_growth.json"


@pytest.mark.parametrize(
    ("entries", "why"),
    [
        ([_entry("nope", {"a": 1})], "entry 0: 'nope' is not a known surface"),
        ([_entry(PLAIN, {"a": 1}, "3.9")], f"entry 0 ({PLAIN}@3.9): not a supported minor"),
        ([{"surface": PLAIN, "buckets": {"a": 1}}], f"entry 0 ({PLAIN}): why is missing or empty"),
        ([_entry(PLAIN, {"a": 1}, why="  ")], f"entry 0 ({PLAIN}): why is missing or empty"),
        (
            [_entry(PLAIN, {"a": 1}, why=delta.WARRANTED_WHY)],
            f"entry 0 ({PLAIN}): why is still the placeholder",
        ),
        ([_entry(PLAIN, {})], f"entry 0 ({PLAIN}): buckets is not a non-empty object"),
        *[
            (
                [_entry(PLAIN, {"stdlib": bad})],
                f"entry 0 ({PLAIN}): bucket 'stdlib' is not a positive int",
            )
            for bad in (0, -1, "4", True, 1.5)
        ],
        (
            [_entry(PLAIN, {"a": 1}), _entry(PLAIN, {"a": 2})],
            f"entry 1 ({PLAIN}): a second entry for {PLAIN}",
        ),
        (
            [_entry(PLAIN, {"a": 1}, "3.10"), _entry(PLAIN, {"a": 2}, "3.10")],
            f"entry 1 ({PLAIN}@3.10): a second entry for {PLAIN}@3.10",
        ),
    ],
)
def test_load_approvals_refuses_the_entry_it_names(tmp_path: Path, entries, why: str):
    with pytest.raises(ValueError, match=re.escape(why)):
        _approvals(tmp_path, entries)


@pytest.mark.parametrize("base", [BASE_TAG, "v0.19.0"])
def test_an_entry_for_a_supported_minor_this_run_does_not_measure_loads(tmp_path: Path, base):
    """CI runs one minor per leg: a 3.14 entry must not turn the 3.10 leg red."""
    got = _approvals(tmp_path, [_entry(PLAIN, {"fd": 1}, "3.14")], base=base)
    if base == BASE_TAG:
        assert got.in_force == [Approval(PLAIN, "3.14", {"fd": 1}, WHY)]
    else:
        assert got.in_force == []
        assert len(got.spent) == 1


def test_the_supported_minors_are_the_makefile_s_nox_minors():
    """One list: the script's SUPPORTED_MINORS is the Makefile's primary + middle + canary."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    var = {
        k: re.search(rf"^{k} := (.*)$", text, re.MULTILINE).group(1).split()
        for k in ("NOX_PRIMARY", "NOX_MIDDLE", "NOX_CANARY")
    }
    assert var["NOX_PRIMARY"] + var["NOX_MIDDLE"] + var["NOX_CANARY"] == delta.SUPPORTED_MINORS


def test_load_approvals_refuses_a_file_that_is_not_one(tmp_path: Path):
    path = tmp_path / "approved_growth.json"
    with pytest.raises(ValueError, match="unparsable"):
        delta.load_approvals(path, BASE_TAG)  # no such file
    path.write_text("{not json")
    with pytest.raises(ValueError, match="unparsable"):
        delta.load_approvals(path, BASE_TAG)
    path.write_text(json.dumps({"entries": []}))
    with pytest.raises(ValueError, match="no base"):
        delta.load_approvals(path, BASE_TAG)


def test_entries_for_another_base_are_spent_notes_not_evidence(tmp_path: Path):
    entries = [_entry(PLAIN, {"stdlib": 4}), _entry(PLAIN, {"fd": 1}, "3.14")]
    got = _approvals(tmp_path, entries, base="v0.19.0")
    assert got.in_force == []
    assert got.spent == [
        f"{PLAIN}: approved for v0.19.0, the base is {BASE_TAG} (spent)",
        f"{PLAIN}@3.14: approved for v0.19.0, the base is {BASE_TAG} (spent)",
    ]
    with pytest.raises(ValueError, match="not a known surface"):
        _approvals(tmp_path, [_entry("nope", {"a": 1})], base="v0.19.0")


# --- by_path: the paths a growth moved ---------------------------------------


def test_a_record_without_by_path_is_refused_by_name(tmp_path: Path):
    side = _side(tmp_path, "s")
    _edit(side / "3.10.json", lambda d: [r.pop("by_path") for r in d if r["surface"] == PLAIN])
    problems = validate_side(side, "3.10", None)
    assert len(problems) == 1
    assert f"{PLAIN}: no by_path" in problems[0]


def _rec(by_path: "dict[str, int]") -> delta.Record:
    return delta.Record(PLAIN, sum(by_path.values()), 0, None, {}, [], by_path)


def test_path_attribution_orders_new_vanished_then_by_size():
    before = _rec({"a": 5, "b": 3, "gone": 2, "gone2": 4, "same": 1})
    after = _rec({"a": 9, "b": 2, "new": 3, "new2": 1, "same": 1})
    assert delta.path_attribution(before, after) == [
        "- gone2 4",
        "+ new 3",
        "- gone 2",
        "+ new2 1",
        "  a 5 -> 9",
        "  b 3 -> 2",
    ]
    assert delta.path_attribution(before, before) == []


def test_the_stop_prints_each_grown_surface_s_paths(tmp_path: Path, capsys):
    tip = {
        "ops": {PLAIN: 504},
        "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}},
        "paths": {PLAIN: {"fd": 500, "stdlib:os.py": 3, "stdlib:": 1}},
    }
    pairs = _pairs(tmp_path, tip, tip)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10", *_judge_args(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"{PLAIN} paths:\n  + stdlib:os.py 3\n  + stdlib: 1\n" in out
    v = decide(pairs, "3.10", NONE)
    assert v.attribution == {PLAIN: ["+ stdlib:os.py 3", "+ stdlib: 1"]}


def test_growth_with_no_path_change_says_so(tmp_path: Path, capsys):
    tip = {
        "ops": {PLAIN: 504},
        "buckets": {PLAIN: {"otto:*": 500, "stdlib": 4}},
        "paths": {PLAIN: {"fd": 500}},
    }
    pairs = _pairs(tmp_path, tip, tip)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10", *_judge_args(tmp_path)]) == 1
    assert f"{PLAIN}: the same paths, more operations" in capsys.readouterr().out


def test_reproduced_ignores_by_path(tmp_path: Path):
    one = _side(tmp_path, "one", paths={PLAIN: {"fd": 500}})
    two = _side(tmp_path, "two", paths={PLAIN: {"a": 300, "b": 200}})
    assert reproduced(one, two, "3.10") == []
    assert judge_minor(one, two, "3.10", NONE).reason is None
    assert decide([(one, two), (one, two)], "3.10", NONE).stop == []


# --- judge_minor ------------------------------------------------------------


def _judge(tmp_path: Path, tip_kw: dict, approvals=EMPTY, minor: str = "3.10"):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", **tip_kw)
    return judge_minor(base, tip, minor, approvals.in_force)


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


def test_growth_on_an_unapproved_surface_stops_and_records_the_warranted_entry(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 500, "otto:x": 4}}})
    assert v.stop == [f"3.10 {PLAIN} 500 -> 504 {{'otto:x': 4}}: grew and is not approved"]
    assert v.warranted == [Approval(PLAIN, "3.10", {"otto:x": 4}, delta.WARRANTED_WHY).entry()]
    assert v.reason == "growth"
    assert v.grown == [(PLAIN, "3.10")]


def test_growth_on_a_tracked_surface_is_judged_like_any_other(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {TRACKED: 501}, "buckets": {TRACKED: {"otto:*": 501}}})
    assert len(v.stop) == 1
    assert v.stop[0].startswith(f"3.10 {TRACKED} 500 -> 501")


def test_growth_within_the_amounts_is_approved_and_says_why(tmp_path: Path):
    for amount in (4, 9):
        entry = Approval(PLAIN, None, {"stdlib": amount}, WHY)
        v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [entry], []))
        assert v.stop == []
        assert v.named == [
            (
                f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: "
                f"approved up to {{'stdlib': {amount}}} ({WHY})"
            )
        ]
        assert v.used == [entry]
        assert v.grown == [(PLAIN, "3.10")], "growth is growth whatever the approval said"
        assert v.warranted == []
    generic = Approval(PLAIN, None, {"stdlib": 9}, "generic")
    exact = Approval(PLAIN, "3.10", {"stdlib": 4}, "exact")
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [generic, exact], []))
    assert v.used == [exact], "a minor-specific entry wins over the generic one"


def test_growth_over_an_amount_or_in_an_unnamed_bucket_stops(tmp_path: Path):
    over = Approval(PLAIN, None, {"stdlib": 3}, WHY)
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [over], []))
    assert v.stop == [
        f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: grew beyond {{'stdlib': 3}} ({WHY})"
    ]
    assert v.warranted == [Approval(PLAIN, "3.10", {"stdlib": 4}, delta.WARRANTED_WHY).entry()]
    assert v.used == []
    unnamed = Approval(PLAIN, None, {"otto:x": 9}, WHY)
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [unnamed], []))
    assert v.stop == [
        f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: grew beyond {{'otto:x': 9}} ({WHY})"
    ]
    other = Approval(PLAIN, "3.14", {"stdlib": 4}, WHY)
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [other], []))
    assert v.stop == [f"3.10 {PLAIN} 500 -> 504 {{'stdlib': 4}}: grew and is not approved"], (
        "an entry for another minor does not apply"
    )


def test_a_warranted_replacement_keeps_the_standing_entry_s_buckets(tmp_path: Path):
    """Raising one bucket keeps the others: the other environment's allowance is not lost."""
    standing = Approval(PLAIN, "3.10", {"stdlib": 3, "site:yaml": 14}, WHY)
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [standing], []))
    assert v.warranted == [
        Approval(PLAIN, "3.10", {"stdlib": 4, "site:yaml": 14}, delta.WARRANTED_WHY).entry()
    ]
    file = delta.approval_file(BASE_TAG, [v], [standing])
    assert file["entries"] == v.warranted


def test_a_per_minor_replacement_of_a_generic_entry_carries_its_buckets(tmp_path: Path):
    """The new 3.10 entry shadows the generic one there, so it must carry what that allowed."""
    generic = Approval(PLAIN, None, {"stdlib": 2, "site:yaml": 14}, WHY)
    v = _judge(tmp_path, GROWN, delta.Approvals(BASE_TAG, [generic], []))
    assert v.warranted == [
        Approval(PLAIN, "3.10", {"stdlib": 4, "site:yaml": 14}, delta.WARRANTED_WHY).entry()
    ]
    file = delta.approval_file(BASE_TAG, [v], [generic])
    assert file["entries"] == [generic.entry(), *v.warranted], "the generic one holds elsewhere"


def test_an_unnamed_bucket_may_move_within_the_noise(tmp_path: Path):
    near = {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 499, "stdlib": 4, "fd": 1}}}
    v = _judge(tmp_path, near, delta.Approvals(BASE_TAG, STDLIB4, []))
    assert v.stop == []
    assert len(v.named) == 1
    far = {"ops": {PLAIN: 504}, "buckets": {PLAIN: {"otto:*": 497, "stdlib": 4, "fd": 3}}}
    v = _judge(tmp_path, far, delta.Approvals(BASE_TAG, STDLIB4, []))
    assert len(v.stop) == 1
    assert "grew beyond {'stdlib': 4}" in v.stop[0]


def test_a_stop_records_the_warranted_entry_and_judge_prints_it_as_json(tmp_path: Path, capsys):
    pairs = _pairs(tmp_path, GROWN, GROWN)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *args, "--minors", "3.10", *_judge_args(tmp_path)]) == 1
    out = capsys.readouterr().out
    entry = Approval(PLAIN, "3.10", {"stdlib": 4}, delta.WARRANTED_WHY).entry()
    block = json.dumps({"base": BASE_TAG, "entries": [entry]}, indent=2)
    assert ("to approve, write tests/unit/import_budget/approved_growth.json:\n" + block) in out


def test_a_spent_file_still_stops_on_growth_and_prints_the_notes(tmp_path: Path, capsys):
    pairs = _pairs(tmp_path, GROWN, GROWN)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    spent = _judge_args(tmp_path, [_entry(PLAIN, {"stdlib": 4})], base="v0.19.0")
    assert main(["judge", *args, "--minors", "3.10", *spent]) == 1
    out = capsys.readouterr().out
    assert f"NOTE:\n  {PLAIN}: approved for v0.19.0, the base is {BASE_TAG} (spent)" in out
    assert "grew and is not approved" in out


def test_an_unused_entry_is_a_note_never_a_stop(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    inforce = _judge_args(tmp_path, [_entry(PLAIN, {"stdlib": 4}), _entry(REAL, {"fd": 1}, "3.10")])
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10", *inforce]) == 0
    out = capsys.readouterr().out
    assert (
        f"NOTE:\n  {PLAIN}: approved but did not grow on any measured minor (unused)\n"
        f"  {REAL}@3.10: approved but did not grow on 3.10 (unused)"
    ) in out
    assert "STOP" not in out


def test_a_decline_is_reported_and_never_stops(tmp_path: Path):
    v = _judge(tmp_path, {"ops": {PLAIN: 490}, "buckets": {PLAIN: {"otto:*": 490}}})
    assert v.stop == []
    assert v.declined == [f"3.10 {PLAIN} 500 -> 490"]
    assert v.reason is None


def test_a_shuffle_at_an_unchanged_total_needs_confirmation(tmp_path: Path):
    v = _judge(tmp_path, SHUFFLED)
    assert v.stop == []
    assert v.shuffles == [f"3.10 {PLAIN} 500 moved {{'otto:*': -3, 'otto:x': 3}}"]
    assert v.reason == "a shuffle"


def test_a_move_within_the_noise_at_an_unchanged_total_is_no_shuffle(tmp_path: Path):
    v = _judge(tmp_path, {"buckets": {PLAIN: {"otto:*": 498, "otto:x": 2}}})
    assert (v.stop, v.shuffles, v.reason) == ([], [], None)


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
    assert (
        main(["judge", *_judge_args(tmp_path), "--pair", str(base), str(tip), "--minors", "3.10"])
        == 0
    )
    out = capsys.readouterr().out
    assert f"not measurable at base:\n  {min(REPO_SURFACES)}: " in out
    assert f"  help_project: {because}" in out
    assert "| help_project |" not in out, "an unmeasurable surface is not tabled"


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
    v = decide(pairs, "3.10", STDLIB4)
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
    v = decide(pairs, "3.10", STDLIB4)
    assert (v.confirmed, v.more) == (2, False)
    assert v.stop == []


def test_reproduced_names_every_record_that_differs(tmp_path: Path):
    first, same = _side(tmp_path, "first"), _side(tmp_path, "same")
    assert reproduced(first, same, "3.10") == []
    moved = _side(tmp_path, "moved", buckets={PLAIN: {"otto:*": 497, "otto:y": 3}})
    assert reproduced(first, moved, "3.10") == [
        f"{PLAIN}: {{'otto:*': 500}} vs {{'otto:*': 497, 'otto:y': 3}}"
    ]
    more = _side(tmp_path, "more", ops={REAL: 503}, buckets={REAL: {"otto:*": 503}})
    assert reproduced(first, more, "3.10") == [f"{REAL}: 500 vs 503 file ops"]


def test_reproduced_agrees_within_the_noise(tmp_path: Path):
    """Two runs of one tree may move BUCKET_NOISE between buckets; the total stays exact."""
    one = _side(tmp_path, "one", buckets={PLAIN: WOBBLE})
    flipped = {**WOBBLE, "fd": 112, "other": 172}
    assert reproduced(one, _side(tmp_path, "flip", buckets={PLAIN: flipped}), "3.10") == []
    more = _side(tmp_path, "more", ops={REAL: 501}, buckets={REAL: {"otto:*": 501}})
    assert reproduced(_side(tmp_path, "first"), more, "3.10") == [f"{REAL}: 500 vs 501 file ops"]
    far = {**WOBBLE, "fd": 114, "other": 170}
    assert reproduced(one, _side(tmp_path, "far", buckets={PLAIN: far}), "3.10") == [
        f"{PLAIN}: {WOBBLE} vs {far}"
    ]


def test_within_noise_bounds_every_bucket_and_the_total():
    assert within_noise({"a": 5}, {"a": 3, "b": 2})
    assert not within_noise({"a": 5}, {"a": 2, "b": 3}), "a bucket moved by three"
    assert not within_noise({"a": 5}, {"a": 6}), "the total moved"
    assert within_noise({}, {})
    assert BUCKET_NOISE == 2


def test_pairs_whose_totals_differ_never_confirm_growth(tmp_path: Path):
    """Growth is exact, so its confirmation is: a quiet pair 2 is not pair 1's +2 again."""
    two = {"ops": {PLAIN: 502}, "buckets": {PLAIN: {"otto:*": 500, "stdlib": 2}}}
    v = decide(_pairs(tmp_path, two, QUIET), "3.10", NONE)
    assert v.stop[0].startswith(f"3.10 did not reproduce (pair 1 vs 2):\n  tip {PLAIN}: 502 vs 500")
    assert (v.more, v.grown) == (True, [])


def test_pairs_that_disagree_on_a_shuffle_do_not_reproduce_it(tmp_path: Path):
    """Pair 1 moved three (a shuffle), pair 2 one (none): within the noise, not one verdict."""
    small = {"buckets": {PLAIN: {"otto:*": 499, "otto:x": 1}}}
    v = decide(_pairs(tmp_path, SHUFFLED, small), "3.10", NONE)
    assert v.stop == [
        (
            f"3.10 did not reproduce (pair 1 vs 2):\n"
            f"  {PLAIN}: a shuffle in one pair, not the other\n"
            "  needs a third measurement (judge --pair three times)"
        )
    ]


def test_decide_confirms_growth_whose_pairs_differ_within_the_noise(tmp_path: Path):
    """The 0.18.0 release stopped inconclusive on this wobble at an unchanged total."""
    wobbled = {"ops": GROWN["ops"], "buckets": {PLAIN: {"otto:*": 499, "stdlib": 4, "fd": 1}}}
    v = decide(_pairs(tmp_path, GROWN, wobbled), "3.10", STDLIB4)
    assert v.stop == []
    assert (v.confirmed, v.evidence, v.grown) == (2, 0, [(PLAIN, "3.10")])


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
    approve = STDLIB4
    v = decide(_pairs(tmp_path, GROWN, GROWN), "3.10", approve)
    assert v.stop == []
    assert (v.confirmed, v.evidence, v.more) == (2, 0, False)
    assert v.grown == [(PLAIN, "3.10")]
    s = decide(_pairs(tmp_path, SHUFFLED, SHUFFLED), "3.10", NONE)
    assert s.stop == [f"3.10 {PLAIN} 500 moved {{'otto:*': -3, 'otto:x': 3}}: reproduced"]


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
                r.update(file_ops=503, by_bucket={"otto:*": 503})

    _edit(pairs[1][0] / "3.10.json", bump)
    v = decide(pairs, "3.10", NONE)
    assert v.stop[0].startswith(
        f"3.10 did not reproduce (pair 1 vs 2):\n  base {REAL}: 500 vs 503 file ops"
    )


def test_decide_refuses_more_than_three_pairs(tmp_path: Path):
    with pytest.raises(ValueError, match="at most 3 pairs"):
        decide(_pairs(tmp_path, QUIET, QUIET, QUIET, QUIET), "3.10", NONE)


def test_stale_approvals_are_growth_based_and_span_wide():
    grown = [(PLAIN, "3.14"), (REAL, "3.10")]
    approvals = [
        Approval(PLAIN, "3.14", {"stdlib": 4}, WHY),
        Approval(REAL, None, {"otto:*": 9}, WHY),
        Approval("help", "3.10", {"fd": 1}, WHY),
        Approval(PLAIN, None, {"stdlib": 1}, WHY),
    ]
    assert stale_approvals(approvals, grown) == [
        "help@3.10: approved but did not grow on 3.10 (unused)",
    ]
    assert stale_approvals(approvals[:1], []) == [
        f"{PLAIN}@3.14: approved but did not grow on 3.14 (unused)"
    ]
    assert stale_approvals(approvals[3:], []) == [
        f"{PLAIN}: approved but did not grow on any measured minor (unused)"
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
            *_judge_args(tmp_path),
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
    assert main(["judge", *_judge_args(tmp_path), *args, "--minors", "3.10"]) == 0
    out = capsys.readouterr().out
    assert f"| {PLAIN} | 500 | 500 | +0 |" in out
    assert "| 504 |" not in out, "the discarded first pair's growth is not shown as evidence"
    assert "confirmed by pair 3 (tables: pair 2)" in out


def test_judge_labels_every_pair_s_tables_when_inconclusive(tmp_path: Path, capsys):
    other = {"ops": {PLAIN: 503}, "buckets": {PLAIN: {"otto:*": 503}}}
    pairs = _pairs(tmp_path, GROWN, QUIET, other)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    assert main(["judge", *_judge_args(tmp_path), *args, "--minors", "3.10"]) == 1
    out = capsys.readouterr().out
    assert "CPython 3.10 (pair 1)" in out
    assert "CPython 3.10 (pair 2)" in out
    assert "CPython 3.10 (pair 3)" in out


def test_judge_exits_zero_when_nothing_grew_and_reports_declines(tmp_path: Path, capsys):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", ops={PLAIN: 499}, buckets={PLAIN: {"otto:*": 499}})
    assert (
        main(["judge", *_judge_args(tmp_path), "--pair", str(base), str(tip), "--minors", "3.10"])
        == 0
    )
    assert f"DECLINED:\n  3.10 {PLAIN} 500 -> 499" in capsys.readouterr().out


def test_judge_lists_what_the_base_cannot_run_with_the_reason(tmp_path: Path, capsys):
    expected = META[REAL].expect_exit
    base = _side(tmp_path, "base", exits={REAL: expected + 1}, endings={PLAIN: [BOOM]})
    tip = _side(tmp_path, "tip")
    assert (
        main(["judge", *_judge_args(tmp_path), "--pair", str(base), str(tip), "--minors", "3.10"])
        == 0
    )
    listed = f"  {PLAIN}: {BOOM}\n  {REAL}: exited {expected + 1}, not {expected}"
    assert f"not measurable at base:\n{listed}" in capsys.readouterr().out


def test_judge_refuses_a_tip_that_cannot_run_a_surface(tmp_path: Path, capsys):
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", exits={REAL: META[REAL].expect_exit + 1})
    assert (
        main(["judge", *_judge_args(tmp_path), "--pair", str(base), str(tip), "--minors", "3.10"])
        == 2
    )
    assert (
        f"3.10 {REAL} exited {META[REAL].expect_exit + 1}, not {META[REAL].expect_exit}"
        in capsys.readouterr().out
    )


def test_judge_refuses_a_tip_row_that_did_not_end_as_expected(tmp_path: Path, capsys):
    """The expected exit is not enough at the tip either: the harness's verdict is the rule."""
    base = _side(tmp_path, "base")
    tip = _side(tmp_path, "tip", endings={REAL: [BOOM, "and more"]})
    assert (
        main(["judge", *_judge_args(tmp_path), "--pair", str(base), str(tip), "--minors", "3.10"])
        == 2
    )
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
    assert (
        main(
            [
                "judge",
                *_judge_args(tmp_path),
                "--pair",
                str(base),
                str(tip),
                "--minors",
                "3.10",
                "3.11",
            ]
        )
        == 2
    )
    out = capsys.readouterr().out
    assert "3.10 not measured (no status)" in out
    assert f"3.11 {REAL} exited 99" in out


def test_judge_refuses_a_bad_approval_before_reading_any_record(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    (tip / "3.10.json").write_text("{not json")  # would be a different message if read
    bad = _judge_args(tmp_path, [_entry("nope", {"a": 1})])
    assert main(["judge", "--pair", str(base), str(tip), "--minors", "3.10", *bad]) == 2
    out = capsys.readouterr().out
    assert "'nope' is not a known surface" in out
    assert "unreadable" not in out


def test_judge_refuses_a_fourth_pair(tmp_path: Path, capsys):
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip")
    args = ["--pair", str(base), str(tip)] * 4
    assert main(["judge", *_judge_args(tmp_path), *args, "--minors", "3.10"]) == 2
    assert "at most 3 pairs" in capsys.readouterr().out


def test_judge_does_not_call_an_entry_unused_while_its_minor_is_unresolved(tmp_path: Path, capsys):
    """A pending or inconclusive minor has not shown the surface did NOT grow."""
    base, tip = _side(tmp_path, "base"), _side(tmp_path, "tip", **GROWN)
    entries = [_entry(PLAIN, {"stdlib": 4})]
    assert (
        main(
            [
                "judge",
                "--pair",
                str(base),
                str(tip),
                "--minors",
                "3.10",
                *_judge_args(tmp_path, entries),
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "needs a second measurement" in out
    assert "unused" not in out
    other = {"ops": {PLAIN: 503}, "buckets": {PLAIN: {"otto:*": 503}}}
    pairs = _pairs(tmp_path, GROWN, QUIET, other)
    args = [a for b, t in pairs for a in ("--pair", str(b), str(t))]
    entries = [_entry(PLAIN, {"stdlib": 4}, "3.10")]
    assert main(["judge", *args, "--minors", "3.10", *_judge_args(tmp_path, entries)]) == 1
    out = capsys.readouterr().out
    assert "inconclusive" in out
    assert "unused" not in out


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
    repo.write("tests/_fixtures/generated_project.py", "# fixtures v1\n")
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
    repo: TmpGitRepo, out: Path, minors=("3.10",), approve=(), base=None, tip="HEAD", file_base=None
) -> Settings:
    """*approve* is the entries of an approvals file naming the repo's tag as its base."""
    approvals = _approvals_path(repo.root.parent, approve, file_base or base_ref(repo.root))
    return Settings(
        repo=repo.root, out=out, base=base or base_ref(repo.root), tip=tip,
        minors=list(minors), approvals=approvals, holder_root=out.parent / "holders",
    )  # fmt: skip


def _steps(runner: FakeRunner, tree: str, minor: str = "3.10") -> "list[str]":
    return [s for s, t, m in runner.calls if t == tree and m == minor]


def _approvals_only_repo(tmp_path: Path) -> "tuple[TmpGitRepo, str, str]":
    """A tagged base commit and a tip that adds only the approvals file."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("scripts/import_budget.py", "# tip\n")
    repo.write("tests/_fixtures/generated_project.py", "# fixtures v1\n")
    base = repo.commit("base")
    repo.git("tag", "v1.0.0")
    repo.write(delta.APPROVALS_PATH.as_posix(), json.dumps({"base": "v1.0.0", "entries": []}))
    return repo, base, repo.commit("approvals only")


def test_measured_tree_ignores_the_approvals_file_and_nothing_else(tmp_path: Path):
    repo, base_sha, tip_sha = _repo(tmp_path)
    approvals = delta.APPROVALS_PATH.as_posix()
    repo.write(approvals, "{}")
    added = repo.commit("add the approvals file")
    repo.write(approvals, '{"edited": true}')
    edited = repo.commit("edit it")
    repo.write("other.txt", "x")
    other = repo.commit("another file")
    index = repo.root / ".git" / "index"
    before = index.read_bytes()
    tree = delta.measured_tree
    assert tree(repo.root, added) == tree(repo.root, tip_sha) == tree(repo.root, edited)
    assert tree(repo.root, other) != tree(repo.root, edited)
    assert tree(repo.root, base_sha) != tree(repo.root, tip_sha)
    assert repo.git("rev-parse", f"{added}^{{tree}}") != repo.git(
        "rev-parse", f"{tip_sha}^{{tree}}"
    )
    assert index.read_bytes() == before, "the repository's own index is never touched"


def test_a_tip_side_that_validates_is_reused_and_logged(tmp_path: Path, capsys):
    repo, _b, tip_sha = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    capsys.readouterr()
    second = FakeRunner()
    assert measure(_settings(repo, out), second) == 0
    assert second.calls == [], "neither side is measured again"
    tip_dir = out / delta.measured_tree(repo.root, tip_sha)
    assert f"reusing HEAD on 3.10 from {tip_dir}" in capsys.readouterr().out


def test_an_approvals_only_tip_judges_a_zero_delta_and_exits_zero(tmp_path: Path, capsys):
    repo, _b, _t = _approvals_only_repo(tmp_path)
    runner = FakeRunner()
    unused = [_entry(PLAIN, {"stdlib": 1})]
    assert measure(_settings(repo, tmp_path / "out", approve=unused), runner) == 0
    out = capsys.readouterr().out
    assert "measure the same tree (only the approvals file differs); nothing grew" in out
    assert f"| {PLAIN} | 500 | 500 | +0 |" in out
    assert "approved but did not grow on any measured minor (unused)" in out
    assert len(runner.calls) == 3, "the base is measured once and is both sides"


def test_other_tips_are_pruned_and_the_base_cache_is_kept(tmp_path: Path):
    repo, _b, tip_sha = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    for stale in ("a" * 40, "b" * 40):
        (out / stale).mkdir()
        (out / stale / "3.10.json").write_text("[]")
    (out / "notes").mkdir()
    assert measure(_settings(repo, out), FakeRunner()) == 0
    kept = sorted(p.name for p in out.iterdir())
    assert delta.measured_tree(repo.root, tip_sha) in kept
    assert "v1.0.0" in kept
    assert "notes" in kept
    assert not {"a" * 40, "b" * 40} & set(kept)


def test_provenance_no_longer_compares_the_sha(tmp_path: Path):
    assert "sha" not in {f.name for f in fields(Provenance)}
    side = _side(tmp_path, "s")
    _edit(side / "3.10.status.json", lambda d: d.update({"sha": "e" * 40}))
    assert validate_side(side, "3.10", PROV) == []


def test_base_ref_resolves_at_the_given_revision_and_the_verb_prints_it(tmp_path: Path, capsys):
    repo, base_sha, _t = _repo(tmp_path)
    repo.git("tag", "v2.0.0")
    assert base_ref(repo.root) == "v2.0.0"
    assert base_ref(repo.root, base_sha) == "v1.0.0"
    assert main(["base-tag", "--repo", str(repo.root), "--rev", base_sha]) == 0
    assert capsys.readouterr().out == "v1.0.0\n"
    assert main(["base-tag", "--repo", str(repo.root), "--rev", "no-such-rev"]) == 2
    assert "git describe" in capsys.readouterr().out


def test_measure_base_rev_names_the_base_and_excludes_base(tmp_path: Path, monkeypatch, capsys):
    repo, base_sha, _t = _repo(tmp_path)
    repo.git("tag", "v2.0.0")
    fake = FakeRunner()
    monkeypatch.setattr(delta, "_run", fake)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    fake.ops = {("tip", PLAIN): 504}
    held = [_entry(PLAIN, {"otto:*": 1})]
    approvals = _approvals_path(repo.root.parent, held, "v1.0.0")
    argv = ["measure", "--repo", str(repo.root), "--out", str(tmp_path / "out")]
    argv += ["--minors", "3.10", "--approvals", str(approvals)]
    assert main([*argv, "--base-rev", base_sha]) == 1
    out = capsys.readouterr().out
    assert "budget-delta: v1.0.0 (" in out
    assert "grew beyond" in out, "the file for v1.0.0 is in force, not spent, at the base's tag"
    assert "(spent)" not in out
    assert '"base": "v1.0.0"' in out
    with pytest.raises(SystemExit):
        main([*argv, "--base", "v1.0.0", "--base-rev", base_sha])


def test_the_base_is_resolved_once_and_names_the_records_and_the_approvals(
    tmp_path: Path, monkeypatch, capsys
):
    """`Settings.base` is the tag: measure never resolves it again, so one name keys both."""
    repo, base_sha, _t = _repo(tmp_path)
    fake = FakeRunner()
    monkeypatch.setattr(delta, "_run", fake)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    calls = []
    real = delta.base_ref
    monkeypatch.setattr(delta, "base_ref", lambda *a: calls.append(a) or real(*a))
    out = tmp_path / "out"
    argv = ["measure", "--repo", str(repo.root), "--out", str(out), "--minors", "3.10"]
    argv += ["--approvals", str(_approvals_path(repo.root.parent, [], "v1.0.0"))]
    assert main([*argv, "--base-rev", base_sha]) == 0
    assert len(calls) == 1
    assert (out / "v1.0.0").is_dir()
    assert main([*argv, "--base", "v1.0.0"]) == 0
    assert len(calls) == 2, "--base is validated as a tag, once"
    capsys.readouterr()
    assert main([*argv, "--base", base_sha]) == 2
    assert (
        f"--base {base_sha} is not a release tag (it describes as v1.0.0)"
        in capsys.readouterr().out
    )
    assert base_sha not in {p.name for p in out.iterdir()}


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
    base_tree = delta.measured_tree(repo.root, base_sha)
    tip_tree = delta.measured_tree(repo.root, tip_sha)
    base, head = out / "v1.0.0", out / tip_tree
    instrument = instrument_id(repo.root, tip_sha)
    assert validate_side(base, "3.10", Provenance(base_tree, "3.10.14", instrument)) == []
    assert validate_side(head, "3.10", Provenance(tip_tree, "3.10.14", instrument)) == []
    assert _steps(runner, "base") == ["sync", "version", "report"]
    assert _steps(runner, "tip") == ["sync", "version", "report"]
    assert runner.probes == ["3.10"]
    assert not (head / "again-1").exists(), "a quiet pair is measured once"
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


@pytest.mark.parametrize(
    "break_it",
    [
        "no_status",
        "failed_step",
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
    elif break_it == "other_tree":
        _edit(base / "3.10.status.json", lambda d: d.update({"measured_tree": "0" * 40}))
    elif break_it == "other_patch":
        # The interpreter uv finds today is another patch.
        again = FakeRunner(versions={"3.10": "3.10.15"})
    elif break_it == "other_instrument":
        # The instrument changed, the subject did not.
        repo.write("tests/_fixtures/generated_project.py", "# fixtures v2\n")
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
    assert "interpreter for 3.10 not found by uv; the cache cannot be reused" in text
    assert _steps(runner, "base") == ["sync", "version", "report"], "the base is measured"


def test_a_cache_hit_still_confirms_growth_with_a_fresh_base_pair(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner()) == 0
    repo.write("more.txt", "a new tip tree\n")
    repo.commit("a new tip")
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
    tip_tree = delta.measured_tree(repo.root, _t)
    again = out / tip_tree / "again-1"
    assert validate_side(again / "v1.0.0", "3.10", None) == []
    assert validate_side(again / tip_tree, "3.10", None) == []
    text = capsys.readouterr().out
    assert "confirming 3.10: growth appeared; measuring the pair again (2 of 3)" in text
    assert "confirmed by pair 2 (tables: pair 1)" in text
    entry = Approval(PLAIN, "3.10", {"otto:*": 4}, delta.WARRANTED_WHY).entry()
    block = json.dumps({"base": "v1.0.0", "entries": [entry]}, indent=2)
    assert ("to approve, write tests/unit/import_budget/approved_growth.json:\n" + block) in text
    assert "STOP:" in text


def test_an_approvals_only_retry_after_a_growth_stop_measures_nothing(tmp_path: Path, capsys):
    """The confirming pair lives beneath its tip's records and is reused like the first."""
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner(ops={("tip", PLAIN): 504})) == 1
    repo.write(delta.APPROVALS_PATH.as_posix(), json.dumps({"base": "v1.0.0", "entries": []}))
    repo.commit("approve the growth")
    retry = FakeRunner(ops={("tip", PLAIN): 504})
    approve = [_entry(PLAIN, {"otto:*": 4})]
    assert measure(_settings(repo, out, approve=approve), retry) == 0
    assert retry.calls == [], "neither pair is measured again"
    assert "confirmed by pair 2 (tables: pair 1)" in capsys.readouterr().out


def test_a_pruned_tip_takes_its_confirming_pairs_with_it(tmp_path: Path):
    repo, _b, _t = _repo(tmp_path)
    out = tmp_path / "out"
    assert measure(_settings(repo, out), FakeRunner(ops={("tip", PLAIN): 504})) == 1
    old = delta.measured_tree(repo.root, _t)
    assert (out / old / "again-1").is_dir()
    repo.write("other.txt", "x")
    repo.commit("another tree")
    assert measure(_settings(repo, out), FakeRunner()) == 0
    assert not (out / old).exists()
    assert not list(out.glob("again-*"))


def test_approved_and_attributed_growth_passes_after_confirmation(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner(ops={("tip", PLAIN): 504})
    assert (
        measure(_settings(repo, tmp_path / "out", approve=[_entry(PLAIN, {"otto:*": 4})]), runner)
        == 0
    )
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
        repo,
        tmp_path / "out",
        minors=("3.10", "3.14"),
        approve=[_entry(PLAIN, {"otto:*": 4}, "3.14")],
    )
    assert measure(settings, CanaryGrows()) == 0
    text = capsys.readouterr().out
    assert "unused" not in text
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
    assert f"tip {PLAIN}: 504" not in text, "the discarded first pair is not shown as evidence"
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
                        r["by_bucket"] = {"otto:*": 497, "otto:x": 3}
                path.write_text(json.dumps(table))
            return rc

    repo, _b, _t = _repo(tmp_path)
    assert measure(_settings(repo, tmp_path / "out"), Shuffling()) == 1
    text = capsys.readouterr().out
    assert "confirming 3.10: a shuffle appeared" in text
    assert f"STOP:\n  3.10 {PLAIN} 500 moved {{'otto:*': -3, 'otto:x': 3}}: reproduced" in text


def _grows(*minors: str, ops: "dict[str, int] | None" = None) -> FakeRunner:
    """A runner whose tip grows by *ops* (default PLAIN +4) on *minors*, in every pair."""
    grown = ops or {PLAIN: 504}
    return FakeRunner(series={("tip", m): [grown] * 3 for m in minors})


def test_measure_judges_every_minor_before_it_stops(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = _grows("3.10")
    assert measure(_settings(repo, tmp_path / "out", minors=("3.10", "3.11")), runner) == 1
    out = capsys.readouterr().out
    assert "CPython 3.10" in out
    assert "CPython 3.11" in out, "the quiet minor after the growing one is still judged"
    assert {m for _s, _t, m in runner.calls} == {"3.10", "3.11"}
    assert "other environment" not in out


def test_the_stop_block_is_one_complete_approvals_file(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = _grows("3.10", ops={PLAIN: 504, REAL: 501})
    runner.series[("tip", "3.14")] = [{PLAIN: 506, REAL: 501}] * 3
    held = [
        _entry(REAL, {"otto:*": 1}),
        _entry(PLAIN, {"otto:*": 2}, "3.10"),
        _entry("help", {"fd": 1}, "3.11", "only the other environment needs it"),
    ]
    minors = ("3.10", "3.11", "3.14")
    settings = _settings(repo, tmp_path / "out", minors=minors, approve=held)
    assert measure(settings, runner) == 1
    out = capsys.readouterr().out
    head = "to approve, write tests/unit/import_budget/approved_growth.json:\n"
    assert out.count(head) == 1
    written = json.loads(out.split(head)[1])
    assert written["base"] == "v1.0.0"
    entries = written["entries"]
    assert [(e["surface"], e["minor"], e["buckets"]) for e in entries] == [
        (REAL, None, {"otto:*": 1}),
        ("help", "3.11", {"fd": 1}),
        (PLAIN, "3.10", {"otto:*": 4}),
        (PLAIN, "3.14", {"otto:*": 6}),
    ], "the exceeded entry appears once, with the new amount; every other in-force entry survives"
    assert [e["why"] for e in entries] == [
        WHY,
        "only the other environment needs it",
        delta.WARRANTED_WHY,
        delta.WARRANTED_WHY,
    ]
    assert "help@3.11: approved but did not grow on 3.11 (unused)" in out, "still NOTEd, not pruned"
    for e in entries[2:]:
        e["why"] = "worth it"
    target = tmp_path / "written.json"
    target.write_text(json.dumps(written))
    assert len(delta.load_approvals(target, "v1.0.0").in_force) == 4, (
        "the printed file is loadable as it stands, once the whys are written"
    )


def test_a_per_minor_entry_decides_on_its_minor_and_the_all_minors_one_elsewhere(tmp_path: Path):
    entries = [_entry(PLAIN, {"stdlib": 9}, why="everywhere"), _entry(PLAIN, {"fd": 1}, "3.10")]
    got = _approvals(tmp_path, entries)
    assert [a.why for a in got.in_force] == ["everywhere", WHY], "both are loadable together"
    assert delta._applicable(got.in_force, PLAIN, "3.10") is got.in_force[1]
    assert delta._applicable(got.in_force, PLAIN, "3.14") is got.in_force[0]


def test_an_operational_failure_on_one_minor_still_judges_the_others_and_exits_2(
    tmp_path: Path, capsys
):
    class SyncFailsOn311(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            minor = env.get("UV_PROJECT_ENVIRONMENT", "").rsplit("venv-", 1)[-1]
            self.fail = {"sync": 3} if minor == "3.11" else {}
            return super().__call__(argv, cwd, env, out)

    repo, _b, _t = _repo(tmp_path)
    runner = SyncFailsOn311(series={("tip", "3.12"): [{PLAIN: 504}] * 3})
    settings = _settings(repo, tmp_path / "out", minors=("3.10", "3.11", "3.12"))
    assert measure(settings, runner) == 2, "the worst status over the minors"
    out = capsys.readouterr().out
    assert "step sync exited 3" in out
    assert "CPython 3.10" in out
    assert "CPython 3.12" in out, "the minor after the failed one is judged"
    assert "grew and is not approved" in out


def test_the_other_environment_s_numbers_print_when_its_records_exist(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    out_dir = tmp_path / "out"
    (out_dir / "other").mkdir(parents=True)
    table = [
        _row(k, 777 if k == PLAIN else 500, {"stdlib": 777} if k == PLAIN else None)
        for k in KNOWN_SURFACES
    ]
    (out_dir / "other" / "3.10.json").write_text(json.dumps(table))
    assert measure(_settings(repo, out_dir), _grows("3.10")) == 1
    text = capsys.readouterr().out
    assert "grew and is not approved\n    other environment: 777 {'stdlib': 777}" in text


def test_an_unused_entry_is_a_note_after_every_minor_never_a_stop(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    settings = _settings(repo, tmp_path / "out", approve=[_entry(PLAIN, {"otto:*": 1})])
    assert measure(settings, FakeRunner()) == 0
    assert (
        f"NOTE:\n  {PLAIN}: approved but did not grow on any measured minor (unused)"
        in capsys.readouterr().out
    )


def test_a_spent_file_is_a_note_and_growth_still_stops_in_measure(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    settings = _settings(
        repo, tmp_path / "out", approve=[_entry(PLAIN, {"otto:*": 4})], file_base="v0.19.0"
    )
    assert measure(settings, FakeRunner(ops={("tip", PLAIN): 504})) == 1
    out = capsys.readouterr().out
    assert f"NOTE:\n  {PLAIN}: approved for v0.19.0, the base is v1.0.0 (spent)" in out
    assert "grew and is not approved" in out


@pytest.mark.parametrize("step", ["sync", "version", "report"])
def test_a_failed_step_is_two_and_names_the_step(tmp_path: Path, step: str, capsys):
    repo, _b, _t = _repo(tmp_path)
    assert measure(_settings(repo, tmp_path / "out"), FakeRunner(fail={step: 3})) == 2
    assert f"step {step} exited 3" in capsys.readouterr().out


def test_the_same_tree_on_both_sides_judges_the_base_against_itself(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    repo.git("tag", "v1.1.0")
    runner = FakeRunner()
    assert measure(_settings(repo, tmp_path / "out", base="v1.1.0"), runner) == 0
    out = capsys.readouterr().out
    assert "measure the same tree (only the approvals file differs); nothing grew" in out
    assert len(runner.calls) == 3, "one measurement: the base is both sides"


def test_a_bad_approval_is_two_before_any_measurement(tmp_path: Path, capsys):
    repo, _b, _t = _repo(tmp_path)
    runner = FakeRunner()
    assert (
        measure(_settings(repo, tmp_path / "out", approve=[_entry("nope", {"a": 1})]), runner) == 2
    )
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

    def fake(repo_path, *args, **kw):
        if " ".join(args[:2]) in verbs:
            raise MeasureError(f"git {' '.join(args[:2])} failed: simulated")
        return real_git(repo_path, *args, **kw)

    return fake


def test_a_bad_sha_is_refused_before_any_holder_is_made(tmp_path: Path):
    repo, _base_sha, tip_sha = _repo(tmp_path)
    with pytest.raises(MeasureError, match="git read-tree"):
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

    def fake(repo_path, *args, **kw):
        """Let the holder's opening prune through; fail the remove and the prune after it."""
        calls.append(" ".join(args[:2]))
        if calls.count("worktree prune") > 1 or args[:2] == ("worktree", "remove"):
            raise MeasureError(f"git {' '.join(args[:2])} failed: simulated")
        return real(repo_path, *args, **kw)

    monkeypatch.setattr(delta, "_git", fake)
    with pytest.raises(MeasureError) as info:
        measure_side(_job(repo, tmp_path, base_sha, tip_sha), FakeRunner())
    assert "worktree remove failed" in str(info.value)
    assert "worktree prune failed" in str(info.value)


def test_a_holder_that_cannot_be_deleted_is_reported(tmp_path: Path, monkeypatch):
    repo, base_sha, tip_sha = _repo(tmp_path)

    real = delta.shutil.rmtree

    def refuse(path, *args, **kwargs):
        if "otto-budget-delta" not in str(path):  # the scratch index's own cleanup is not tested
            return real(path, *args, **kwargs)
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
    default = repo.root / delta.APPROVALS_PATH
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"base": "v1.0.0", "entries": []}))
    rc = main(
        ["measure", "--repo", str(repo.root), "--out", str(tmp_path / "out"), "--minors", "3.10"]
    )
    assert rc == 0
    assert fake.calls


def test_the_report_step_hands_the_harness_the_tip_s_command_list(tmp_path: Path):
    """The harness runs under the BASE's otto, which may predate the command-list helper.

    So the delta, which runs under the tip's otto, reads the list once and hands it
    to the harness: the base side measures the same tracked verbs as the tip, and
    the harness never imports a helper an older release does not have.
    """
    from otto.cli.builtin_commands import builtin_command_names

    repo, base_sha, tip_sha = _repo(tmp_path)
    reports = []

    class Spy(FakeRunner):
        def __call__(self, argv, cwd, env, out):
            if "scripts/import_budget.py" in argv:
                reports.append(dict(env))
            return super().__call__(argv, cwd, env, out)

    measure_side(_job(repo, tmp_path, base_sha, tip_sha), Spy())
    assert [env[import_budget.VERBS_ENV] for env in reports] == [",".join(builtin_command_names())]


def test_the_harness_measures_an_otto_that_has_no_command_list_helper(tmp_path: Path):
    """An older base (v0.17.0) has no `otto.cli.builtin_commands`; the harness must still load."""
    harness = str(Path(import_budget.__file__))
    script = (
        "import importlib.util, sys\n"
        "sys.modules['otto.cli.builtin_commands'] = None\n"
        f"spec = importlib.util.spec_from_file_location('h', {harness!r})\n"
        "h = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(h)\n"
        "print(' '.join(s.key for s in h.SURFACES if s.tracked))\n"
    )
    env = {**os.environ, import_budget.VERBS_ENV: "init,run,docker"}
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [
        "tracked_init", "tracked_docker", "tracked_host_probe", "tracked_host_power",
    ]  # fmt: skip


def test_a_handed_command_list_is_trimmed_and_an_empty_one_refused(monkeypatch):
    monkeypatch.setenv(import_budget.VERBS_ENV, " init , ,docker ")
    assert import_budget.first_party_commands() == ["init", "docker"]
    monkeypatch.setenv(import_budget.VERBS_ENV, " , ")
    with pytest.raises(ValueError, match="names no command"):
        import_budget.first_party_commands()


def test_the_handed_command_list_never_reaches_the_measured_otto(monkeypatch):
    monkeypatch.setenv(import_budget.VERBS_ENV, "init")
    assert import_budget.VERBS_ENV not in import_budget._sanitized_env()
