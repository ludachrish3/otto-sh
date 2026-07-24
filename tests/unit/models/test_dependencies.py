"""Dependency-entry parsing, name normalization, and satisfiability."""

import pytest

from otto.models.dependencies import (
    DependencyClause,
    clauses_satisfiable,
    normalize_name,
    parse_dependency_entry,
)


class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("MyLib") == "mylib"

    def test_collapses_separator_runs(self):
        assert normalize_name("My_Lib") == "my-lib"
        assert normalize_name("my..__--lib") == "my-lib"

    def test_plain_name_unchanged(self):
        assert normalize_name("vantage") == "vantage"


class TestParseEntry:
    def test_bare_name_means_any_version(self):
        dep = parse_dependency_entry("vantage", required=True)
        assert dep.name == "vantage"
        assert dep.normalized == "vantage"
        assert dep.constraint == ""
        assert dep.clauses == []
        assert dep.required is True

    def test_single_clause(self):
        dep = parse_dependency_entry("vantage >= 2.1", required=False)
        assert dep.constraint == ">= 2.1"
        assert dep.clauses == [DependencyClause(op=">=", version=(2, 1, 0))]
        assert dep.required is False

    def test_comma_anded_clauses_and_zero_padding(self):
        dep = parse_dependency_entry("vantage >= 2.1, < 3", required=True)
        assert dep.clauses == [
            DependencyClause(op=">=", version=(2, 1, 0)),
            DependencyClause(op="<", version=(3, 0, 0)),
        ]

    def test_all_six_operators(self):
        entry = "x == 1, != 2, >= 3, <= 4, > 5, < 6"
        ops = [c.op for c in parse_dependency_entry(entry, required=True).clauses]
        assert ops == ["==", "!=", ">=", "<=", ">", "<"]

    def test_raw_preserved(self):
        dep = parse_dependency_entry("  vantage >= 2.1  ", required=True)
        assert dep.raw == "vantage >= 2.1"

    def test_extra_tag_in_clause_rejected(self):
        with pytest.raises(ValueError, match="extra tags are not allowed"):
            parse_dependency_entry("vantage >= 1.2.3-rc1", required=True)

    def test_bad_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            parse_dependency_entry("-badname >= 1", required=True)

    def test_name_with_space_rejected(self):
        with pytest.raises(ValueError, match="name"):
            parse_dependency_entry("my lib >= 1", required=True)

    def test_empty_clause_rejected(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage >= 1,, < 2", required=True)

    def test_garbage_clause_rejected(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage ~= 1.2", required=True)

    def test_caret_operator_rejected_as_clause(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage ^= 1.2", required=True)

    def test_garbage_between_name_and_clause_rejected(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage~bad >= 1.2", required=True)

    def test_entry_with_no_name_before_operator_is_framed_not_crashed(self):
        # ">= 1": nothing precedes the operator, so the extracted "name" is
        # "" -- must still raise the framed name error, not crash while
        # computing the hint (regression: `_prefix, *rest = "".split(None, 1)`
        # raised "not enough values to unpack" instead).
        with pytest.raises(ValueError, match="name ''") as exc_info:
            parse_dependency_entry(">= 1", required=True)
        assert "must start with" in str(exc_info.value)
        assert "hint" not in str(exc_info.value)

    def test_empty_entry_is_framed_not_crashed(self):
        with pytest.raises(ValueError, match="name ''") as exc_info:
            parse_dependency_entry("", required=True)
        assert "must start with" in str(exc_info.value)
        assert "hint" not in str(exc_info.value)

    def test_name_version_no_operator_gets_hint(self):
        """A bare "name version" (forgotten operator) is a name error but should
        hint at the fix rather than leaving the user to guess."""
        with pytest.raises(ValueError, match="name") as exc_info:
            parse_dependency_entry("vantage 2.1", required=True)
        assert "hint" in str(exc_info.value)
        assert "'name == 1.2'" in str(exc_info.value)

    def test_name_with_space_no_digit_after_gets_no_hint(self):
        # "my lib >= 1": the offending text "my lib" has no digit-starting
        # token after the whitespace, so no hint is appended.
        with pytest.raises(ValueError, match="name") as exc_info:
            parse_dependency_entry("my lib >= 1", required=True)
        assert "hint" not in str(exc_info.value)


def _clauses(entry: str) -> list[DependencyClause]:
    return parse_dependency_entry(f"x {entry}", required=True).clauses


class TestClauseMatches:
    def test_eq(self):
        (c,) = _clauses("== 1.2.3")
        assert c.matches((1, 2, 3))
        assert not c.matches((1, 2, 4))

    def test_bounds(self):
        lo, hi = _clauses(">= 1.2, < 2")
        assert lo.matches((1, 2, 0))
        assert hi.matches((1, 2, 0))
        assert not lo.matches((1, 1, 9))
        assert not hi.matches((2, 0, 0))


class TestSatisfiable:
    def test_empty_clause_list(self):
        assert clauses_satisfiable([])

    def test_ordinary_range(self):
        assert clauses_satisfiable(_clauses(">= 1.2, < 2"))

    def test_crossed_bounds(self):
        assert not clauses_satisfiable(_clauses(">= 3, < 2"))

    def test_touching_bounds_inclusive_ok(self):
        assert clauses_satisfiable(_clauses(">= 2, <= 2"))

    def test_touching_bounds_exclusive_empty(self):
        assert not clauses_satisfiable(_clauses(">= 2, < 2"))
        assert not clauses_satisfiable(_clauses("> 2, <= 2"))

    def test_no_triple_between_consecutive_patches(self):
        assert not clauses_satisfiable(_clauses("> 1.2.3, < 1.2.4"))

    def test_conflicting_pins(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, == 1.2.4"))

    def test_pin_outside_bounds(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, >= 2"))

    def test_pin_excluded(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, != 1.2.3"))

    def test_pin_inside_bounds_ok(self):
        assert clauses_satisfiable(_clauses("== 1.5.0, >= 1, < 2"))

    def test_finite_point_set_fully_excluded(self):
        assert not clauses_satisfiable(_clauses(">= 1.2.3, <= 1.2.4, != 1.2.3, != 1.2.4"))

    def test_finite_point_set_partially_excluded_ok(self):
        assert clauses_satisfiable(_clauses(">= 1.2.3, <= 1.2.5, != 1.2.3, != 1.2.4"))

    def test_exclusions_cannot_empty_infinite_range(self):
        assert clauses_satisfiable(_clauses(">= 1.2, < 2, != 1.2.0"))
        assert clauses_satisfiable(_clauses(">= 1.2, != 1.2.0"))
