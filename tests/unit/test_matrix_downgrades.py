"""The release stage's directional gate: which matrix changes may auto-commit.

Every case INJECTS its own pair of matrices rather than reading the committed
artifact. A gate tested against the repo's current (healthy) state would score
green no matter what it did.
"""

import json

import pytest

from scripts.check_matrix_downgrades import (
    ALLOWED,
    BLOCKING,
    REMOVED,
    classify,
    main,
    transitions,
)


def _matrix(cells):
    """A matrix carrying just enough shape for the classifier."""
    return {"cells": {s: {p: {"status": st} for p, st in row.items()} for s, row in cells.items()}}


def _write(tmp_path, name, cells):
    path = tmp_path / name
    path.write_text(json.dumps(_matrix(cells)), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        # A new or changed claim that otto is broken.
        ("measured-ok", "measured-broken", BLOCKING),
        ("untested", "measured-broken", BLOCKING),
        ("not-observable", "measured-broken", BLOCKING),
        (None, "measured-broken", BLOCKING),
        # A measured-ok verdict lost, for any reason.
        ("measured-ok", "not-observable", BLOCKING),
        ("measured-ok", "untested", BLOCKING),
        # Improvements and new non-broken cells.
        ("measured-broken", "measured-ok", ALLOWED),
        ("untested", "measured-ok", ALLOWED),
        ("not-observable", "measured-ok", ALLOWED),
        (None, "measured-ok", ALLOWED),
        # No claim about the product either way.
        ("untested", "not-observable", ALLOWED),
        ("measured-broken", "measured-broken", ALLOWED),
        ("measured-ok", "measured-ok", ALLOWED),
        # A tree change, never a measurement.
        ("measured-ok", None, REMOVED),
    ],
)
def test_each_transition_is_classified(old, new, kind):
    assert classify(old, new) == kind


def test_a_downgrade_blocks_and_names_the_cell(tmp_path, capsys):
    base = _write(tmp_path, "base.json", {"timeout": {"gnu": "measured-ok"}})
    cand = _write(tmp_path, "cand.json", {"timeout": {"gnu": "measured-broken"}})
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 1
    captured = capsys.readouterr()
    assert "timeout" in captured.out
    assert "gnu" in captured.out
    assert "measured-ok -> measured-broken" in captured.out


def test_an_improvement_is_allowed(tmp_path):
    base = _write(tmp_path, "base.json", {"timeout": {"gnu": "measured-broken"}})
    cand = _write(tmp_path, "cand.json", {"timeout": {"gnu": "measured-ok"}})
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 0


def test_one_downgrade_among_many_improvements_still_blocks(tmp_path):
    """The gate is not a majority vote."""
    base = _write(
        tmp_path,
        "base.json",
        {
            "timeout": {"gnu": "measured-broken", "zephyr-3.7": "measured-broken"},
            "exec": {"gnu": "measured-ok"},
        },
    )
    cand = _write(
        tmp_path,
        "cand.json",
        {
            "timeout": {"gnu": "measured-ok", "zephyr-3.7": "measured-ok"},
            "exec": {"gnu": "measured-broken"},
        },
    )
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 1


def test_an_identical_matrix_reports_no_transitions(tmp_path, capsys):
    cells = {"timeout": {"gnu": "measured-ok"}}
    base = _write(tmp_path, "base.json", cells)
    cand = _write(tmp_path, "cand.json", cells)
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 0
    assert "no cell changed status" in capsys.readouterr().out


def test_a_removed_cell_is_reported_but_does_not_block(tmp_path, capsys):
    base = _write(tmp_path, "base.json", {"timeout": {"gnu": "measured-ok", "old": "measured-ok"}})
    cand = _write(tmp_path, "cand.json", {"timeout": {"gnu": "measured-ok"}})
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 0
    assert "removed" in capsys.readouterr().out.lower()


def test_a_new_broken_cell_blocks_even_though_it_is_new(tmp_path):
    """A newly added host measured broken is still a claim that otto is broken."""
    base = _write(tmp_path, "base.json", {"timeout": {"gnu": "measured-ok"}})
    cand = _write(
        tmp_path, "cand.json", {"timeout": {"gnu": "measured-ok", "newhost": "measured-broken"}}
    )
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 1


def test_unreadable_input_exits_two_not_one(tmp_path):
    """A parse failure must never look like a clean gate, nor like a downgrade."""
    base = tmp_path / "base.json"
    base.write_text("{not json", encoding="utf-8")
    cand = _write(tmp_path, "cand.json", {"timeout": {"gnu": "measured-ok"}})
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 2


def test_a_missing_file_exits_two(tmp_path):
    base = _write(tmp_path, "base.json", {"timeout": {"gnu": "measured-ok"}})
    assert main(["--baseline", str(base), "--candidate", str(tmp_path / "absent.json")]) == 2


def test_transitions_lists_only_what_changed():
    base = _matrix({"a": {"p": "measured-ok", "q": "measured-ok"}})
    cand = _matrix({"a": {"p": "measured-ok", "q": "measured-broken"}})
    changed = transitions(base, cand)
    assert [(t.surface, t.profile, t.kind) for t in changed] == [("a", "q", BLOCKING)]


def test_the_gate_reads_the_committed_matrix_by_default(tmp_path):
    """--candidate defaults to the real artifact, so the Makefile need not name it.

    Compared against ITSELF, so this asserts the default resolves and parses --
    not that the repo happens to be healthy.
    """
    from scripts.check_matrix_downgrades import MATRIX_PATH

    assert MATRIX_PATH.exists(), "the committed matrix moved; the default is now wrong"
    assert main(["--baseline", str(MATRIX_PATH)]) == 0
