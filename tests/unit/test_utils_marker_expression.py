"""The -m expression rule: complete the trailing identifier, keep the head verbatim."""

from otto.utils import complete_marker_expression

NAMES = ["deep", "slow", "smoke"]


def test_bare_name():
    assert complete_marker_expression(NAMES, "s") == ["slow", "smoke"]


def test_head_up_to_the_last_whitespace_is_kept():
    assert complete_marker_expression(NAMES, "smoke and s") == ["smoke and slow", "smoke and smoke"]


def test_head_up_to_the_last_paren_is_kept():
    assert complete_marker_expression(NAMES, "not (d") == ["not (deep"]


def test_keywords_are_never_offered():
    assert complete_marker_expression(["and", "or", "not", "deep"], "") == ["deep"]


def test_empty_tail_offers_everything_in_order():
    assert complete_marker_expression(NAMES, "smoke and ") == [
        "smoke and deep",
        "smoke and slow",
        "smoke and smoke",
    ]
