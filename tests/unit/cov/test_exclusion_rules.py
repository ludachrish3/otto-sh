"""Exclusion rule models and [coverage.exclusions] loading."""

import pytest

from otto.coverage.errors import CoverageConfigError
from otto.coverage.exclusions.rules import (
    BUILTIN_MARKER_RULES,
    MarkerRule,
    PathRule,
    PreprocessorRule,
    RegexRule,
    load_exclusion_rules,
)


def test_marker_rule_derives_the_lcov_family() -> None:
    rule = MarkerRule(stat="line", name="MYPROJ_NO_COV")
    assert rule.tokens() == {
        "line": "MYPROJ_NO_COV_LINE",
        "start": "MYPROJ_NO_COV_START",
        "stop": "MYPROJ_NO_COV_STOP",
    }


def test_marker_rule_branch_stat_derives_the_br_family() -> None:
    rule = MarkerRule(stat="branch", name="MYPROJ_NO_COV")
    assert rule.tokens() == {
        "line": "MYPROJ_NO_COV_BR_LINE",
        "start": "MYPROJ_NO_COV_BR_START",
        "stop": "MYPROJ_NO_COV_BR_STOP",
    }


def test_builtins_are_ordinary_marker_rules_not_a_special_case() -> None:
    """The built-in set must be derivable from the same family model.

    If this needs hardcoded tuples, the family model is the wrong shape.
    """
    derived = {token for rule in BUILTIN_MARKER_RULES for token in rule.tokens().values()}
    assert derived == {
        "LCOV_EXCL_LINE",
        "LCOV_EXCL_START",
        "LCOV_EXCL_STOP",
        "LCOV_EXCL_BR_LINE",
        "LCOV_EXCL_BR_START",
        "LCOV_EXCL_BR_STOP",
    }


def test_loads_each_rule_kind() -> None:
    cfg = {
        "exclusions": {
            "rules": [
                {"kind": "marker", "name": "MYPROJ_NO_COV"},
                {"kind": "preprocessor", "macros": ["DEBUG_LOG"]},
                {"kind": "preprocessor", "pattern": "#if 0"},
                {"kind": "path", "patterns": ["vendor/**"]},
                {"kind": "regex", "pattern": "assert[(]", "stat": "branch"},
            ]
        }
    }
    rules = load_exclusion_rules(cfg)
    assert [type(r) for r in rules] == [
        MarkerRule,
        PreprocessorRule,
        PreprocessorRule,
        PathRule,
        RegexRule,
    ]
    assert rules[1].macros == ["DEBUG_LOG"]
    assert rules[2].pattern is not None
    assert rules[2].pattern.search("#if 0")
    assert rules[4].stat == "branch"


def test_absent_table_yields_no_rules() -> None:
    assert load_exclusion_rules({}) == []
    assert load_exclusion_rules({"exclusions": {}}) == []


def test_stat_defaults_to_line() -> None:
    rules = load_exclusion_rules({"exclusions": {"rules": [{"kind": "regex", "pattern": "x"}]}})
    assert rules[0].stat == "line"


def test_invalid_regex_fails_loud_naming_the_rule_index() -> None:
    cfg = {"exclusions": {"rules": [{"kind": "regex", "pattern": "unbalanced("}]}}
    with pytest.raises(CoverageConfigError) as excinfo:
        load_exclusion_rules(cfg)
    message = str(excinfo.value)
    assert "rules[0]" in message
    assert "unbalanced(" in message


def test_preprocessor_requires_exactly_one_of_pattern_or_macros() -> None:
    both = {
        "exclusions": {"rules": [{"kind": "preprocessor", "pattern": "#if 0", "macros": ["X"]}]}
    }
    with pytest.raises(CoverageConfigError, match="exactly one"):
        load_exclusion_rules(both)

    neither = {"exclusions": {"rules": [{"kind": "preprocessor"}]}}
    with pytest.raises(CoverageConfigError, match="exactly one"):
        load_exclusion_rules(neither)


def test_colliding_marker_families_are_rejected_naming_both_indices() -> None:
    """Base FOO at stat=branch and base FOO_BR at stat=line both derive
    FOO_BR_LINE. Identical strings, so longest-first cannot separate them.
    """
    cfg = {
        "exclusions": {
            "rules": [
                {"kind": "marker", "name": "FOO", "stat": "branch"},
                {"kind": "marker", "name": "FOO_BR", "stat": "line"},
            ]
        }
    }
    with pytest.raises(CoverageConfigError) as excinfo:
        load_exclusion_rules(cfg)
    message = str(excinfo.value)
    assert "rules[0]" in message
    assert "rules[1]" in message
    assert "FOO_BR_LINE" in message


def test_unknown_kind_is_rejected() -> None:
    cfg = {"exclusions": {"rules": [{"kind": "nonsense"}]}}
    with pytest.raises(CoverageConfigError):
        load_exclusion_rules(cfg)


def test_a_user_family_colliding_with_a_builtin_token_is_rejected() -> None:
    """The built-ins are rules too, so a user family may not re-derive their tokens.

    Base LCOV_EXCL_BR at stat=line derives LCOV_EXCL_BR_LINE, which is the
    built-in BRANCH family's line token. Same string, conflicting stat.
    """
    cfg = {"exclusions": {"rules": [{"kind": "marker", "name": "LCOV_EXCL_BR", "stat": "line"}]}}
    with pytest.raises(CoverageConfigError) as excinfo:
        load_exclusion_rules(cfg)
    message = str(excinfo.value)
    assert "LCOV_EXCL_BR_LINE" in message
    assert "rules[0]" in message
    # the built-in side must name its family, not a fabricated index
    assert "built-in" in message
    assert "LCOV_EXCL" in message
    assert "branch" in message


def test_a_user_family_redeclaring_the_builtin_line_base_is_rejected() -> None:
    cfg = {"exclusions": {"rules": [{"kind": "marker", "name": "LCOV_EXCL"}]}}
    with pytest.raises(CoverageConfigError, match="LCOV_EXCL_LINE"):
        load_exclusion_rules(cfg)


def test_a_family_that_misses_every_builtin_token_is_accepted() -> None:
    """The built-in seeding must not reject ordinary custom families."""
    rules = load_exclusion_rules(
        {"exclusions": {"rules": [{"kind": "marker", "name": "MYPROJ_NO_COV"}]}}
    )
    assert [r.name for r in rules] == ["MYPROJ_NO_COV"]


def test_invalid_stat_is_rejected_naming_the_index_and_the_allowed_values() -> None:
    cfg = {"exclusions": {"rules": [{"kind": "regex", "pattern": "x", "stat": "nonsense"}]}}
    with pytest.raises(CoverageConfigError) as excinfo:
        load_exclusion_rules(cfg)
    message = str(excinfo.value)
    assert "rules[0]" in message
    assert "nonsense" in message
    assert "line" in message
    assert "branch" in message


class TestMarkerBaseIsAUsableToken:
    """A marker base must be a token, because the derived members are searched
    as bare substrings and nothing reports what a rule matched.

    An empty base derives ``_LINE`` / ``_START`` / ``_STOP``, which occur
    inside ordinary identifiers (``MAX_LINE_LEN``). By design there is no
    per-rule accounting, so the result is coverage silently deleted off lines
    nobody marked, with nothing anywhere saying so. Loud at load is the only
    place this can be caught.
    """

    @pytest.mark.parametrize("name", ["", "   ", "MY PROJ", "\tX"])
    def test_a_base_that_is_empty_or_holds_whitespace_is_refused(self, name: str) -> None:
        with pytest.raises(CoverageConfigError) as excinfo:
            load_exclusion_rules({"exclusions": {"rules": [{"kind": "marker", "name": name}]}})
        assert "rules[0]" in str(excinfo.value)

    def test_the_empty_base_would_otherwise_have_matched_an_ordinary_identifier(self) -> None:
        """Names the harm the guard prevents, so the guard cannot be relaxed
        without someone reading what it was for."""
        from otto.coverage.exclusions.scan import scan_source

        rule = MarkerRule(stat="line", name="")
        assert rule.tokens()["line"] == "_LINE"
        assert scan_source("int MAX_LINE_LEN = 80;\n", [rule]).lines == {1}

    def test_an_ordinary_base_still_loads(self) -> None:
        rules = load_exclusion_rules(
            {"exclusions": {"rules": [{"kind": "marker", "name": "MYPROJ_NO_COV"}]}}
        )
        assert [r.name for r in rules] == ["MYPROJ_NO_COV"]
