"""Negation-parity matching for #if conditions.

otto never evaluates a condition. It only asks whether a flagged macro is
referenced at even negation parity — which is enough, because a dead arm
has no coverage records to delete in the first place.
"""

import pytest

from otto.coverage.exclusions.parity import references_positively

MACROS = ["DEBUG_LOG"]


@pytest.mark.parametrize(
    ("keyword", "condition", "expected"),
    [
        ("ifdef", "DEBUG_LOG", True),
        ("ifndef", "DEBUG_LOG", False),
        ("if", "defined(DEBUG_LOG)", True),
        ("if", "defined DEBUG_LOG", True),
        ("if", "!defined(DEBUG_LOG)", False),
        ("if", "defined(A) && defined(DEBUG_LOG)", True),
        ("if", "defined(A) && !defined(DEBUG_LOG)", False),
        ("if", "DEBUG_LOG > 2", True),
        ("if", "!(A && !defined(DEBUG_LOG))", True),
        ("elif", "defined(DEBUG_LOG)", True),
        ("if", "defined(A) || defined(B)", False),
        ("else", "", False),
        ("endif", "", False),
        # Malformed, but really emitted by lex_source: an unbalanced ')' and a
        # bare '#ifdef'. Coverage runs over whatever the build compiled, so
        # these must resolve to an answer rather than raise IndexError.
        ("if", "A) && DEBUG_LOG", True),
        ("ifdef", "", False),
    ],
)
def test_parity(keyword: str, condition: str, expected: bool) -> None:
    assert references_positively(keyword, condition, MACROS) is expected


def test_defined_keyword_is_not_itself_a_macro_reference() -> None:
    assert references_positively("if", "defined(X)", ["defined"]) is False


def test_an_unlisted_macro_never_matches() -> None:
    assert references_positively("if", "defined(OTHER)", MACROS) is False


@pytest.mark.parametrize(
    "condition",
    [
        "defined(DEBUG_LOG) || !defined(DEBUG_LOG)",
        "!defined(DEBUG_LOG) || defined(DEBUG_LOG)",
    ],
)
def test_a_macro_referenced_twice_at_mixed_parity_counts_as_positive(condition: str) -> None:
    """Any positive reference wins, whichever parity is seen FIRST.

    The scan must keep looking after a negated reference. Deciding on the
    first flagged name instead answers False for the reversed order, and the
    positive half of that condition is exactly what enables the arm.
    """
    assert references_positively("if", condition, MACROS) is True
