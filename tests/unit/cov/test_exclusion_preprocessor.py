"""Preprocessor-arm exclusion: pattern form and macro-parity form."""

import re

from otto.coverage.exclusions.rules import PreprocessorRule
from otto.coverage.exclusions.scan import scan_source

NESTED = (
    "top();\n"  # 1
    "#ifdef DEBUG_LOG\n"  # 2
    "  log_verbose();\n"  # 3
    "  #ifdef VERBOSE\n"  # 4
    "    deep();\n"  # 5
    "  #endif\n"  # 6
    "#else\n"  # 7
    "  prod_path();\n"  # 8
    "#endif\n"  # 9
    "tail();\n"  # 10
)


def _rule(pattern: str, stat: str = "line") -> PreprocessorRule:
    return PreprocessorRule(stat=stat, pattern=re.compile(pattern))


def test_matching_arm_only_the_else_arm_survives() -> None:
    result = scan_source(NESTED, [_rule(r"#ifdef\s+DEBUG_LOG")])
    assert result.lines == {2, 3, 4, 5, 6}
    assert 8 not in result.lines, "the #else arm is production code and must be kept"


def test_the_terminator_directive_is_not_excluded() -> None:
    """The matching directive is greyed; the arm's terminator is not."""
    result = scan_source(NESTED, [_rule(r"#ifdef\s+DEBUG_LOG")])
    assert 7 not in result.lines
    assert 9 not in result.lines


def test_pattern_can_name_if_zero() -> None:
    src = "a;\n#if 0\ndead();\n#endif\nb;\n"
    assert scan_source(src, [_rule(r"#if 0")]).lines == {2, 3}


def test_elif_arm_is_addressable() -> None:
    src = (
        "a;\n"  # 1
        "#if defined(A)\n"  # 2
        "one();\n"  # 3
        "#elif defined(B)\n"  # 4
        "two();\n"  # 5
        "#endif\n"  # 6
    )
    result = scan_source(src, [_rule(r"#elif\s+defined\(B\)")])
    assert result.lines == {4, 5}


def test_no_match_excludes_nothing() -> None:
    assert scan_source(NESTED, [_rule(r"#ifdef\s+NOTHING_HERE")]).lines == set()


def test_branch_stat_routes_the_arm_to_branch_lines() -> None:
    result = scan_source(NESTED, [_rule(r"#ifdef\s+DEBUG_LOG", stat="branch")])
    assert result.branch_lines == {2, 3, 4, 5, 6}
    assert result.lines == set()


def test_pattern_matches_the_normalized_directive_not_raw_text() -> None:
    """A spaced-out directive and a continuation both normalize first."""
    src = "#  ifdef   DEBUG_LOG\nx;\n#endif\n"
    assert scan_source(src, [_rule(r"#ifdef DEBUG_LOG")]).lines == {1, 2}


def test_a_pattern_naming_else_or_endif_claims_nothing() -> None:
    """A conditionless directive is unaddressable, however the pattern reads.

    ``#else`` opens the production fallback, and ``#endif`` opens no arm at
    all — its extent runs to the next same-depth directive, i.e. over the
    live code that follows the whole construct. Without the keyword guard,
    a pattern spelling either one greys out working code.
    """
    assert scan_source(NESTED, [_rule(r"#else")]).lines == set()
    assert scan_source(NESTED, [_rule(r"#endif")]).lines == set()


def test_an_unterminated_arm_runs_to_end_of_file() -> None:
    """A missing ``#endif`` greys to EOF, as an unclosed marker block does.

    Coverage runs over whatever the build compiled; a truncated or spliced
    source must still resolve to a bounded extent rather than raise.
    """
    src = "a;\n#ifdef DEBUG_LOG\nb;\nc;\n"
    assert scan_source(src, [_rule(r"#ifdef\s+DEBUG_LOG")]).lines == {2, 3, 4}


def test_macros_form_excludes_the_matching_arm() -> None:
    """A ``macros`` rule claims the same arm its pattern-form twin does.

    ``kind = "preprocessor"`` with ``macros`` builds a rule whose ``pattern``
    is ``None``; the parity scan resolves it. ``DEBUG_LOG`` is referenced at
    even negation parity by line 2's ``#ifdef``, so the arm goes, and the
    ``#else`` production fallback at line 8 stays.
    """
    rule = PreprocessorRule(stat="line", macros=["DEBUG_LOG"])
    result = scan_source(NESTED, [rule])
    assert result.lines == {2, 3, 4, 5, 6}
    assert 8 not in result.lines


def test_macros_form_leaves_a_negated_reference_alone() -> None:
    """``#ifndef DEBUG_LOG`` is the arm that runs WITHOUT the macro.

    Naming the macro is not enough — odd parity means the flagged macro
    disables the arm rather than enabling it, so the code inside is
    production code.
    """
    src = "a;\n#ifndef DEBUG_LOG\nprod();\n#endif\nb;\n"
    rule = PreprocessorRule(stat="line", macros=["DEBUG_LOG"])
    assert scan_source(src, [rule]).lines == set()
