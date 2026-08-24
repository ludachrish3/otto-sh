"""Source lexing and marker-rule resolution."""

import re

from otto.coverage.exclusions.rules import (
    BUILTIN_MARKER_RULES,
    MarkerRule,
    PathRule,
    PreprocessorRule,
    RegexRule,
)
from otto.coverage.exclusions.scan import lex_source, scan_source


def test_builtin_line_and_block_markers() -> None:
    src = (
        "int main() {\n"
        "  int a = 1;             // LCOV_EXCL_LINE\n"
        "  // LCOV_EXCL_START\n"
        "  debug_dump();\n"
        "  // LCOV_EXCL_STOP\n"
        "  return 0;\n"
        "}\n"
    )
    result = scan_source(src, BUILTIN_MARKER_RULES)
    assert result.lines == {2, 3, 4, 5}


def test_branch_family_lands_in_branch_lines_not_lines() -> None:
    src = "a;\nif (x) {}   // LCOV_EXCL_BR_LINE\nb;\n"
    result = scan_source(src, BUILTIN_MARKER_RULES)
    assert result.branch_lines == {2}
    assert result.lines == set()


def test_custom_family_requires_the_lcov_suffix() -> None:
    """The bare base is NOT a marker — the family is base + _LINE."""
    rules = [MarkerRule(stat="line", name="MYPROJ_NO_COV")]
    assert scan_source("a;\nb; // MYPROJ_NO_COV\nc;\n", rules).lines == set()
    assert scan_source("a;\nb; // MYPROJ_NO_COV_LINE\nc;\n", rules).lines == {2}


def test_unclosed_start_runs_to_eof() -> None:
    src = "a;\n// LCOV_EXCL_START\nb;\nc;\n"
    assert scan_source(src, BUILTIN_MARKER_RULES).lines == {2, 3, 4}


def test_start_and_stop_on_one_line_is_a_one_line_block() -> None:
    src = "a;\n// LCOV_EXCL_START LCOV_EXCL_STOP\nb;\n"
    assert scan_source(src, BUILTIN_MARKER_RULES).lines == {2}


def test_stop_then_start_on_one_line_reopens() -> None:
    src = (
        "a;\n// LCOV_EXCL_START\nx;\n// LCOV_EXCL_STOP LCOV_EXCL_START\ny;\n// LCOV_EXCL_STOP\nz;\n"
    )
    assert scan_source(src, BUILTIN_MARKER_RULES).lines == {2, 3, 4, 5, 6}


def test_longest_token_wins_at_a_position() -> None:
    """A _START token must not also fire the _LINE matcher of a shorter base."""
    rules = [MarkerRule(stat="line", name="A"), MarkerRule(stat="line", name="A_START")]
    # 'A_START_LINE' is A_START's line marker, and contains A's start token
    # 'A_START'. Longest-first must pick A_START_LINE, so line 2 is a single
    # excluded line and NOT the opening of a block that swallows line 3.
    src = "x;\n// A_START_LINE\ny;\n"
    assert scan_source(src, rules).lines == {2}


def test_lexer_finds_directives_and_skips_ones_inside_block_comments() -> None:
    src = "a;\n/*\n#ifdef NOT_A_DIRECTIVE\n*/\n#ifdef REAL\nb;\n#endif\n"
    lexed = lex_source(src)
    assert [(d.lineno, d.keyword, d.condition) for d in lexed.directives] == [
        (5, "ifdef", "REAL"),
        (7, "endif", ""),
    ]


def test_a_line_comment_wins_over_a_block_opener_it_precedes() -> None:
    """``/*`` inside a ``// ...`` tail never opens a block comment.

    Read it as an opener and the lexer believes every following line sits
    inside a comment, so the real ``#ifdef`` two lines down is not a
    directive at all and a rule naming it silently excludes nothing — the
    quietest failure this module has, because an exclusion that vanishes
    looks exactly like a rule that matched nothing.
    """
    src = "x; // see /* note\n#ifdef DEBUG_LOG\ndbg();\n#endif\n"
    assert [(d.lineno, d.keyword) for d in lex_source(src).directives] == [
        (2, "ifdef"),
        (4, "endif"),
    ]
    rule = PreprocessorRule(stat="line", macros=["DEBUG_LOG"])
    assert scan_source(src, [rule]).lines == {2, 3}


def test_a_block_opener_wins_over_a_line_comment_it_precedes() -> None:
    """The mirror image: ``//`` INSIDE ``/* ...`` is comment text.

    The block comment here is never closed, so everything below it is
    comment and the ``#ifdef`` is not a directive. Treating the ``//`` as
    the start of a line comment would end the comment at the newline and
    hand the lexer a directive that the compiler never saw.
    """
    src = "x; /* note // more\n#ifdef DEBUG_LOG\ndbg();\n#endif\n"
    assert lex_source(src).directives == []
    rule = PreprocessorRule(stat="line", macros=["DEBUG_LOG"])
    assert scan_source(src, [rule]).lines == set()


def test_lexer_joins_continuations_into_one_logical_directive() -> None:
    src = "#if defined(A) && \\\n    defined(B)\nx;\n#endif\n"
    lexed = lex_source(src)
    first = lexed.directives[0]
    assert first.lineno == 1
    assert first.end_lineno == 2
    assert first.condition == "defined(A) && defined(B)"


def test_lexer_normalizes_the_hash_to_keyword_gap() -> None:
    lexed = lex_source("#  ifdef   X\ny;\n#endif\n")
    assert lexed.directives[0].keyword == "ifdef"
    assert lexed.directives[0].condition == "X"


def test_lexer_tracks_nesting_depth() -> None:
    src = "#ifdef A\n#ifdef B\nx;\n#endif\n#endif\n"
    lexed = lex_source(src)
    assert [(d.lineno, d.depth) for d in lexed.directives] == [
        (1, 0),
        (2, 1),
        (4, 1),
        (5, 0),
    ]


def test_a_partially_overlapping_shorter_token_does_not_fire() -> None:
    """Claiming is by OVERLAP, not containment.

    In ``// A_START_LINE``, base ``START``'s line token ``START_LINE`` claims
    [5, 15) and base ``A``'s start token ``A_START`` sits at [3, 10). It is
    not *contained* in the claimed span, so a containment test lets ``A``
    fire a start event and open a block that swallows the file to EOF. The
    two derived token sets do not intersect, so the config-time collision
    guard cannot catch this one either.
    """
    rules = [MarkerRule(stat="line", name="A"), MarkerRule(stat="line", name="START")]
    assert scan_source("x;\n// A_START_LINE\ny;\nz;\n", rules).lines == {2}


def test_scan_source_unions_marker_and_regex_rules_and_skips_path_rules() -> None:
    """``scan_source`` is the entry point every rule kind is handed to.

    Marker and regex rules both resolve against source and union here: the
    marker takes line 2, the regex takes line 3. Path rules never resolve
    here at all — they name whole files, so ``apply.py`` drops the matching
    ``FileRecord`` before any source is read.

    The path rule below carries the translation of the glob ``**``, which
    matches every subject there is. That makes "skipped" falsifiable: a
    scanner that mistook ``PathRule.patterns`` for line regexes — an easy
    slip, since both kinds hold compiled patterns — would take the whole
    file instead of just line 3.
    """
    rules = [
        MarkerRule(stat="line", name="LCOV_EXCL"),
        RegexRule(stat="line", pattern=re.compile(r"^\s*assert\b")),
        PathRule(
            stat="line",
            patterns=[re.compile(r"\A.*\Z")],
            raw_patterns=["**"],
        ),
    ]
    result = scan_source("a;\nb; // LCOV_EXCL_LINE\nassert x;\n", rules)
    assert result.lines == {2, 3}
    assert result.branch_lines == set()


def test_a_form_feed_does_not_shift_line_numbers() -> None:
    """Lines are counted the way gcov counts them: on newline, and nothing else.

    ``str.splitlines`` also breaks on form feed. A form feed is ordinary in
    GNU-style C -- the kernel uses them as page separators -- and a compiler
    reads one as plain whitespace, so gcov never starts a line on it. Split
    on it here and every later line is numbered one high, for every rule
    kind, and ``apply.py`` deletes records by line number.

    The form feed sits on its own line, the kernel's convention, which is
    also the shape ``splitlines`` mis-reads worst: it ends a line on the
    form feed AND on the newline after it, inventing one extra line.
    """
    src = "int a;\n\x0c\nint b; // LCOV_EXCL_LINE\n#ifdef DEBUG\nx;\n#endif\n"
    assert scan_source(src, BUILTIN_MARKER_RULES).lines == {3}
    assert [d.lineno for d in lex_source(src).directives] == [4, 6]


def test_a_crlf_line_ending_stays_out_of_the_line_text() -> None:
    """Counting on newline alone would otherwise leave the carriage return behind.

    ``splitlines`` used to absorb it. Now that lines are cut on newline only,
    the carriage return has to be dropped explicitly, or a rule anchored with
    ``$`` would match an LF file and miss the identical CRLF one.

    The list assertion also pins the other half of that cut: the final
    newline terminates ``b;`` rather than opening a fourth, empty line.
    """
    src = "a;\r\n  LOG_DBG(x);\r\nb;\r\n"
    assert lex_source(src).lines == ["a;", "  LOG_DBG(x);", "b;"]
    rule = RegexRule(stat="line", pattern=re.compile(r"LOG_DBG\(x\);$"))
    assert scan_source(src, [rule]).lines == {2}


def test_lexer_strips_a_trailing_block_comment_from_the_condition() -> None:
    """A comment must not leave macro names in the condition.

    ``#ifdef PROD /* not DEBUG_LOG */`` names a production arm. If the
    comment survived into ``condition``, the macro-parity form would see
    ``DEBUG_LOG`` there and exclude the arm it exists to keep.
    """
    lexed = lex_source("#ifdef PROD /* not DEBUG_LOG */\nx;\n#endif\n")
    assert lexed.directives[0].condition == "PROD"


def test_lexer_strips_a_block_comment_from_the_middle_of_a_condition() -> None:
    lexed = lex_source("#if defined(A) /* x */ && defined(B)\nx;\n#endif\n")
    assert lexed.directives[0].condition == "defined(A) && defined(B)"


def test_lexer_strips_a_line_comment_tail_from_the_condition() -> None:
    lexed = lex_source("#ifdef PROD // not DEBUG_LOG\nx;\n#endif\n")
    assert lexed.directives[0].condition == "PROD"


def test_lexer_keeps_a_url_inside_a_block_comment_from_truncating_the_condition() -> None:
    """A ``//`` INSIDE ``/* ... */`` is comment text, not a line-comment start.

    Which delimiter comes first decides the whole rest of the directive. Read
    the ``//`` of a URL as a line comment and everything after it is dropped —
    here the ``&& defined(BAR)`` half of the condition. The parity scan would
    then match this arm on ``FOO`` alone, excluding an arm that in truth needs
    ``BAR`` defined as well.
    """
    lexed = lex_source("#if defined(FOO) /* see http://wiki/foo */ && defined(BAR)\nx;\n#endif\n")
    assert lexed.directives[0].condition == "defined(FOO) && defined(BAR)"


def test_lexer_strips_an_unterminated_block_comment_to_end_of_directive() -> None:
    """The comment runs on past the directive, so nothing after ``/*`` is condition."""
    lexed = lex_source("#ifdef PROD /* not DEBUG_LOG\n   still comment */\nx;\n#endif\n")
    assert lexed.directives[0].condition == "PROD"


def test_lexer_reports_elif_and_else_at_their_openers_depth() -> None:
    """``#elif``/``#else`` sit at the depth of the ``#if`` they belong to.

    Nested, so the value is not trivially 0: the inner chain reports depth
    1 while the outer ``#else`` reports 0.
    """
    src = (
        "#ifdef A\n"  # 1  opener, depth 0
        "#ifdef B\n"  # 2  opener, depth 1
        "x;\n"  # 3
        "#elif defined(C)\n"  # 4  inner chain, depth 1
        "y;\n"  # 5
        "#else\n"  # 6  inner chain, depth 1
        "z;\n"  # 7
        "#endif\n"  # 8  closes the inner, depth 1
        "#else\n"  # 9  outer chain, depth 0
        "w;\n"  # 10
        "#endif\n"  # 11 closes the outer, depth 0
    )
    lexed = lex_source(src)
    assert [(d.lineno, d.keyword, d.depth) for d in lexed.directives] == [
        (1, "ifdef", 0),
        (2, "ifdef", 1),
        (4, "elif", 1),
        (6, "else", 1),
        (8, "endif", 1),
        (9, "else", 0),
        (11, "endif", 0),
    ]


def test_a_stray_else_or_endif_without_an_opener_reports_depth_zero() -> None:
    """Depth is clamped at 0: a fragment can open below its own first line.

    Coverage runs over whatever the build compiled, including sources that
    were spliced or truncated. A negative depth would make every later
    directive compare wrong in :func:`arm_extent`.
    """
    assert lex_source("#else\nx;\n").directives[0].depth == 0
    assert lex_source("#endif\nx;\n").directives[0].depth == 0
