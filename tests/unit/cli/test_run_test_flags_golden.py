"""The flags, help and completion of ``otto run`` and ``otto test``, pinned as a golden.

For ``otto test`` and each ``otto run`` command of the world
(:mod:`tests._fixtures.options_golden_world`), under each selection of
:data:`SELECTIONS`, :func:`capture` records every option the command's
``--help`` shows, in order, and the words a TAB offers for ``--`` and for the
word after each value-taking flag, through the shim and through the Typer fast
path. The capture is compared with :data:`GOLDEN`: per selection
(:func:`key`), per command, its ``"help"`` rows and its ``"tab"``,
``{"shim": {tail: words}, "fast_path": {tail: words}}``, where a TAB the
shim hands over records ``{"handed_over": reason}``. A row's ``origin`` and
``project`` are attributed, not observed: they are what
:data:`tests._fixtures.options_golden_world.ORIGINS` says of the row's
parameter name, so they pin which flag is which, not how otto knows it.

Where the shim and the fast path disagree today, the golden records the
disagreement as it is: the fast path offers no values for a registered
class's enum flag where the shim and a cold TAB do (#1385), and the shim
offers nothing for ``otto test --markers`` where the fast path does (#1386).

``python -m tests.unit.cli.test_run_test_flags_golden`` rewrites the golden
from the code as it stands, so a red test here needs a ruling on the change,
never a regeneration to make it green.

Typer's completion installers are left out of every row and every answer:
some paths offer them on a ``run`` leaf and others do not, a difference that
exists without any options class. Refs #1383
"""

import functools
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests._fixtures.help_rows import help_rows
from tests._fixtures.options_golden_world import (
    COMMANDS,
    ORIGINS,
    OWN,
    World,
    options_golden_world,
)
from tests._fixtures.paths import CHECKOUT_ROOT, GOLDENS
from tests._fixtures.portable import portable
from tests._fixtures.warm_tab import (
    INSTALLERS,
    run_otto,
    shim_answer,
    typer_tab,
    without_installers,
)

GOLDEN = GOLDENS / "options_run_test_flags.json"

SELECTIONS = [[], ["-l", "alpha1"], ["-I", "b"], ["-E", "b"]]
"""The root flags a command runs under: none, a lab of ``a``'s, ``b`` forced on, ``b`` off."""


def key(selection: "list[str]") -> str:
    """The golden's key for *selection*: ``key([]) == "[]"``."""
    return json.dumps(selection)


@functools.cache
def golden_data() -> "dict[str, Any]":
    """The golden, read once."""
    return json.loads(GOLDEN.read_text())


def _attributed(row: "dict[str, Any]") -> "dict[str, Any]":
    origin, project = ORIGINS.get(row["name"], OWN)
    return {**row, "origin": origin, "project": project}


def _spelled(row: "dict[str, Any]") -> str:
    """The spelling a user types for *row*: its first long one."""
    return next((s for s in row["spellings"] if s.startswith("--")), row["spellings"][0])


def _shim_words(env: "dict[str, str]", line: str) -> "list[str] | dict[str, str]":
    """The shim's words for *line*, or the reason it hands the TAB over."""
    out = shim_answer(env, line)
    if out.items is None:
        return {"handed_over": out.reason}
    return without_installers(out.items)


def _rows(
    env: "dict[str, str]", selection: "list[str]", words: "list[str]"
) -> "list[dict[str, Any]]":
    """The rows ``otto <selection> <words> --help`` shows, attributed, installers left out."""
    return [
        _attributed(row)
        for row in help_rows(env, *selection, *words, "--help")
        if not set(row["spellings"]) & set(INSTALLERS)
    ]


def probed_tails(base_rows: "list[dict[str, Any]]") -> "list[str]":
    """The tails a TAB is asked about on a command: ``--``, and the word after each value flag.

    A property of the world, not of the selection: *base_rows* are the
    command's rows with no selection, so every selection probes the same
    tails and can change only the answers.
    """
    return ["--", *(f"{_spelled(row)} " for row in base_rows if row["takes_value"])]


def _command(
    env: "dict[str, str]", prefix: str, rows: "list[dict[str, Any]]", tails: "list[str]"
) -> "dict[str, Any]":
    """One command's capture: its help *rows*, and the TAB answers after *prefix* for *tails*."""
    return {
        "help": rows,
        "tab": {
            "shim": {tail: _shim_words(env, f"{prefix} {tail}") for tail in tails},
            "fast_path": {
                tail: without_installers(typer_tab(env, f"{prefix} {tail}")) for tail in tails
            },
        },
    }


def capture(world: World, selections: "list[list[str]]" = SELECTIONS) -> "dict[str, Any]":
    """Each of *selections*' commands: each one's help rows and its TAB answers.

    The caches are seeded first, so every TAB is answered warm and in any
    order: the names cache by a first ``otto``, the test-names tables by the
    first TAB that reads them (a cold table blocks that TAB until it is
    written). Every selection probes the same tails, those of the command's
    help with no selection (:func:`probed_tails`). The world's root is
    written as ``<world>``.
    """
    seeded = run_otto(world.env, "--help")
    assert seeded.returncode == 0, seeded.stderr
    typer_tab(world.env, "otto test --markers ")
    base = {name: _rows(world.env, [], words) for name, words in COMMANDS.items()}
    got = {
        key(selection): {
            name: _command(
                world.env,
                " ".join(["otto", *selection, *words]),
                _rows(world.env, selection, words) if selection else base[name],
                probed_tails(base[name]),
            )
            for name, words in COMMANDS.items()
        }
        for selection in selections
    }
    return json.loads(portable(json.dumps(got), places={"<world>": world.root}))


@pytest.mark.parametrize("selection", SELECTIONS, ids=key)
def test_the_flags_of_run_and_test_match_the_golden(tmp_path, selection):
    """One selection per test: each runs some forty ``otto`` subprocesses."""
    got = capture(options_golden_world(tmp_path), [selection])
    assert got[key(selection)] == golden_data()[key(selection)]


def test_the_golden_holds_every_kind_of_flag_from_both_projects():
    """So the comparison is not vacuous."""
    rows = [row for cmd in golden_data()[key([])].values() for row in cmd["help"]]
    assert {r["origin"] for r in rows} == {
        "own_parameter",
        "own_class",
        "body_class",
        "run_class",
        "test_class",
        "run_and_test_class",
    }
    assert {r["project"] for r in rows} >= {"a", "b", None}


GONE = {key(["-l", "alpha1"]): "b", key(["-E", "b"]): "b", key(["-I", "b"]): None, key([]): None}
"""The project each selection leaves out of the command; ``None``: every project takes part."""


def kept_rows(rows: "list[dict[str, Any]]", gone: "str | None") -> "list[dict[str, Any]]":
    """*rows* without those of project *gone*; a row of no project (otto's own) is always kept."""
    return [row for row in rows if gone is None or row["project"] != gone]


def test_a_selection_removes_exactly_the_out_of_scope_projects_flags_from_help():
    data = golden_data()
    assert set(GONE) == {key(s) for s in SELECTIONS}
    for selection, gone in GONE.items():
        for name, command in data[key([])].items():
            assert data[selection][name]["help"] == kept_rows(command["help"], gone), (
                selection,
                name,
            )


def test_a_selection_changes_no_completion_yet():
    """TAB still offers every project's flags; the change that filters TAB replaces this test."""
    data = golden_data()
    base = data[key([])]
    assert all(data[s][n]["tab"] == base[n]["tab"] for s in data for n in base)


def _generate() -> None:
    """Write the golden from the code as it stands; see the module docstring.

    The test's own route: one fresh world per selection.
    """
    got: "dict[str, Any]" = {}
    for selection in SELECTIONS:
        with tempfile.TemporaryDirectory(prefix="otto-run-test-flags-") as scratch:
            got.update(capture(options_golden_world(Path(scratch)), [selection]))
    GOLDEN.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.write(f"wrote {GOLDEN.relative_to(CHECKOUT_ROOT)}\n")


if __name__ == "__main__":
    _generate()
