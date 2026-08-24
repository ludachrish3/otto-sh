"""Path globs and line regexes."""

import re
from pathlib import Path

from otto.coverage.exclusions.paths import glob_to_regex, path_rule_matches
from otto.coverage.exclusions.rules import PathRule, RegexRule
from otto.coverage.exclusions.scan import scan_source


def _path_rule(*globs: str, stat: str = "line") -> PathRule:
    return PathRule(stat=stat, patterns=[glob_to_regex(g) for g in globs], raw_patterns=list(globs))


def test_double_star_crosses_directories() -> None:
    pattern = glob_to_regex("vendor/**")
    assert pattern.match("vendor/a.c")
    assert pattern.match("vendor/deep/nested/b.c")
    assert not pattern.match("src/vendor_helper.c")


def test_leading_double_star_matches_at_any_depth() -> None:
    pattern = glob_to_regex("**/*_generated.c")
    assert pattern.match("a_generated.c")
    assert pattern.match("src/x/a_generated.c")
    assert not pattern.match("src/x/a_generated.h")


def test_single_star_does_not_cross_a_separator() -> None:
    pattern = glob_to_regex("src/*.c")
    assert pattern.match("src/a.c")
    assert not pattern.match("src/nested/a.c")


def test_question_mark_matches_one_non_separator_char() -> None:
    pattern = glob_to_regex("src/?.c")
    assert pattern.match("src/a.c")
    assert not pattern.match("src/ab.c")
    assert not pattern.match("src//.c")


def test_glob_is_anchored_at_both_ends() -> None:
    # The head assertion has to be phrased with ``search``: ``match`` anchors
    # the start on its own, so a ``match`` phrasing would hold whether or not
    # the compiled pattern carries a head anchor, and could never go red.
    pattern = glob_to_regex("src/a.c")
    assert not pattern.search("prefix/src/a.c")
    assert not pattern.match("src/a.c.bak")


def test_relative_glob_matches_relative_to_the_root() -> None:
    rule = _path_rule("vendor/**")
    assert path_rule_matches(Path("/repo/vendor/a.c"), Path("/repo"), rule)
    assert not path_rule_matches(Path("/repo/src/a.c"), Path("/repo"), rule)


def test_a_file_outside_the_root_never_matches_a_relative_glob() -> None:
    """A relative glob is scoped to the root even when it names everything.

    The glob is ``**`` on purpose. A relative glob that opens on a literal
    segment is refused for a second, accidental reason — its translation is
    head-anchored on that literal, and no absolute path starts with it — so
    it would hold even if root scoping were dropped entirely. ``**``
    translates to a catch-all, which leaves the scoping as the only thing
    keeping an outside file out.
    """
    rule = _path_rule("**")
    assert path_rule_matches(Path("/repo/deep/a.c"), Path("/repo"), rule)
    assert not path_rule_matches(Path("/elsewhere/a.c"), Path("/repo"), rule)


def test_an_absolute_glob_matches_the_absolute_path() -> None:
    rule = _path_rule("/opt/vendor/**")
    assert path_rule_matches(Path("/opt/vendor/a.c"), Path("/repo"), rule)


def test_regex_rule_excludes_matching_lines() -> None:
    src = "keep();\n  LOG_DBG(x);\nkeep2();\n  LOG_DBG(y);\n"
    rule = RegexRule(stat="line", pattern=re.compile(r"^\s*LOG_DBG\("))
    assert scan_source(src, [rule]).lines == {2, 4}


def test_regex_rule_with_branch_stat_routes_to_branch_lines() -> None:
    # The line is indented on purpose. The rule below is spec section 2's own
    # flagship sample, and real C indents its asserts; against an unindented
    # line a \b at offset 0 is already a boundary, so the assertion would hold
    # whether the scanner searched the line or only matched at its start.
    src = "a;\n  assert(x);\nb;\n"
    rule = RegexRule(stat="branch", pattern=re.compile(r"\bassert\("))
    result = scan_source(src, [rule])
    assert result.branch_lines == {2}
    assert result.lines == set()


def test_a_regex_rule_matches_against_the_raw_line() -> None:
    """A regex rule sees physical source text, comments and all.

    The scanner keeps three renderings of a line: the raw text, the
    comment-stripped text the directive lexer builds, and the normalized
    directive. Only the raw one is the regex rule's subject. Putting the
    rule's text nowhere but inside a block comment is what tells those three
    apart -- routed through either of the other two, this finds nothing.
    """
    src = "keep();\n/* LOG_DBG(x); */\nkeep2();\n"
    rule = RegexRule(stat="line", pattern=re.compile(r"LOG_DBG\("))
    assert scan_source(src, [rule]).lines == {2}
