"""Unit tests for the AppShell parse engine (Task 12).

Covers ``Parsed`` and the ``parse_one`` / ``parse_all`` / ``apply_parse``
functions in :mod:`otto.host.app_shell`:

* class-definition-time guards (compiled ``pattern`` required; the pattern's
  named groups must be a subset of the model's fields and a superset of its
  required fields — pattern/model drift is impossible);
* regex-driven type conversion via pydantic;
* the three ``parse=`` forms (single model / ``list[Model]`` / callable escape
  hatch);
* nested region-recursion: a field typed as another ``Parsed`` (or
  ``list[Sub]`` / ``Sub | None``) is parsed from the region its group captured.

The nested ``Row``/``Table``/``QueryStats``/``Select`` models are taken
verbatim from spec §9 and exercised against a representative mysql ``SELECT``
output block.
"""

import re

import pytest

from otto.host.app_shell import (
    Parsed,
    ParseMismatch,
    apply_parse,
    parse_all,
    parse_one,
)


class Kv(Parsed):
    """``key=number`` pair — the workhorse fixture for the flat-parse tests."""

    pattern = re.compile(r"(?P<key>\w+)=(?P<n>\d+)")
    key: str
    n: int


# --------------------------------------------------------------------------- #
# class-definition-time guards
# --------------------------------------------------------------------------- #
def test_parsed_requires_pattern():
    with pytest.raises(TypeError, match="pattern"):

        class NoPattern(Parsed):
            x: int


def test_parsed_group_field_drift_is_class_def_error():
    with pytest.raises(TypeError, match="named groups"):

        class Drift(Parsed):
            pattern = re.compile(r"(?P<typo>\d+)")
            x: int


def test_parsed_required_field_without_group_is_class_def_error():
    with pytest.raises(TypeError, match="required fields"):

        class Missing(Parsed):
            pattern = re.compile(r"(?P<key>\w+)")
            key: str
            n: int  # required, but no (?P<n>...) group -> drift the other way


# --------------------------------------------------------------------------- #
# parse_one — single search + pydantic conversion
# --------------------------------------------------------------------------- #
def test_parse_one_converts_types():
    result = parse_one(Kv, "a=5")
    assert isinstance(result, Kv)
    assert result.key == "a"
    assert result.n == 5  # str "5" -> int 5 via pydantic
    assert isinstance(result.n, int)


def test_parse_one_mismatch_raises():
    with pytest.raises(ParseMismatch) as excinfo:
        parse_one(Kv, "nothing to match here")
    # The offending pattern is surfaced (repr'd) in the message for debugging.
    assert repr(Kv.pattern.pattern) in str(excinfo.value)


# --------------------------------------------------------------------------- #
# parse_all — finditer over the whole text
# --------------------------------------------------------------------------- #
def test_parse_all_returns_one_per_match():
    rows = parse_all(Kv, "a=1 b=2 c=3")
    assert [(r.key, r.n) for r in rows] == [("a", 1), ("b", 2), ("c", 3)]


def test_parse_all_empty_is_valid():
    # No match anywhere -> the empty list is a valid "zero rows" answer,
    # NOT a mismatch.
    assert parse_all(Kv, "no key value pairs present") == []


# --------------------------------------------------------------------------- #
# optional groups -> None
# --------------------------------------------------------------------------- #
def test_optional_group_maps_to_none():
    class Opt(Parsed):
        pattern = re.compile(r"x(?P<opt>\d+)?")
        opt: str | None = None

    # "x" matches but the optional group never participates -> None.
    assert parse_one(Opt, "x").opt is None
    # When it does participate the group text flows through pydantic.
    assert parse_one(Opt, "x42").opt == "42"


# --------------------------------------------------------------------------- #
# nested region-recursion — the spec §9 mysql SELECT example (verbatim models)
# --------------------------------------------------------------------------- #
class Row(Parsed):
    pattern = re.compile(r"^\|(?P<cells>.+)\|$", re.MULTILINE)
    cells: str  # or one named group per column


class Table(Parsed):
    pattern = re.compile(r"(?P<rows>(?:^\|.+\|$\n?)+)", re.MULTILINE)
    rows: list[Row]


class QueryStats(Parsed):
    pattern = re.compile(r"(?P<count>\d+) rows? in set \((?P<seconds>[\d.]+) sec\)")
    count: int
    seconds: float


class Select(Parsed):
    pattern = re.compile(
        r"(?P<table>^\+-[\s\S]+?^\+-[^\n]*$)\s*(?P<stats>\d+ rows? in set[^\n]*)",
        re.MULTILINE,
    )
    table: Table
    stats: QueryStats


# A representative mysql SELECT block: a `+--+` bordered table with exactly
# five `| ... |` data rows followed by the stats line. It is deliberately
# header-less: the verbatim ``Table`` pattern greedily captures the *first*
# contiguous run of pipe-lines, so a column header separated from the data by
# a `+--+` border rule would be captured instead of the rows. Keeping the five
# data rows as the sole pipe-run matches the spec's own annotation that
# ``result.value.table.rows[0]`` is a *data* row (Alice), not a header.
SELECT_OUTPUT = (
    "+----+---------+-------------+\n"
    "|  1 | Alice   | Engineering |\n"
    "|  2 | Bob     | Sales       |\n"
    "|  3 | Carol   | Marketing   |\n"
    "|  4 | Dave    | Engineering |\n"
    "|  5 | Eve     | Support     |\n"
    "+----+---------+-------------+\n"
    "5 rows in set (0.00 sec)"
)


def test_nested_select_example():
    value = apply_parse(Select, SELECT_OUTPUT)

    assert isinstance(value, Select)
    # trailing stats line parsed by QueryStats' own pattern over its region
    assert value.stats.count == 5
    assert value.stats.seconds == 0.0
    # the table -> rows region recursed all the way down to five Row objects
    assert len(value.table.rows) == 5
    assert all(isinstance(r, Row) for r in value.table.rows)
    # rows[0] is the first *data* row, not a header
    assert "Alice" in value.table.rows[0].cells


# --------------------------------------------------------------------------- #
# apply_parse dispatch — list form and callable escape hatch
# --------------------------------------------------------------------------- #
def test_apply_parse_list_form():
    rows = apply_parse(list[Kv], "a=1 b=2")
    assert [(r.key, r.n) for r in rows] == [("a", 1), ("b", 2)]


def test_apply_parse_single_model_form():
    value = apply_parse(Kv, "a=7")
    assert isinstance(value, Kv)
    assert value.n == 7


def test_apply_parse_callable_form():
    assert apply_parse(str.upper, "x") == "X"


def test_apply_parse_callable_exception_is_wrapped():
    def boom(_text: str) -> str:
        raise RuntimeError("callable blew up")

    with pytest.raises(ParseMismatch, match="callable blew up"):
        apply_parse(boom, "anything")


def test_apply_parse_list_of_non_parsed_is_type_error():
    # list[...] is reserved for list[Parsed]; a scalar element is a misuse.
    with pytest.raises(TypeError, match="Parsed subclass"):
        apply_parse(list[str], "a=1")


def test_apply_parse_unsupported_spec_is_type_error():
    # Not a Parsed subclass, not list[Parsed], not callable -> rejected.
    with pytest.raises(TypeError, match="unsupported parse spec"):
        apply_parse(42, "anything")
