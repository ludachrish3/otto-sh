"""The `+` lab-combining grammar, shared by --lab, OTTO_LAB, and load_lab()."""

import pytest

from otto.config.lab import LAB_SEPARATOR, split_lab_names


def test_separator_is_plus():
    assert LAB_SEPARATOR == "+"


def test_single_name():
    assert split_lab_names("tech1") == ["tech1"]


def test_combines_on_plus():
    assert split_lab_names("tech1+overlay") == ["tech1", "overlay"]


def test_segments_are_stripped():
    """Shells make the spaced form easy to type by accident."""
    assert split_lab_names("tech1 + overlay") == ["tech1", "overlay"]


def test_comma_is_an_ordinary_character():
    """The comma lost its meaning: `a,b` is ONE lab whose name contains a comma."""
    assert split_lab_names("lab_a,lab_b") == ["lab_a,lab_b"]


@pytest.mark.parametrize("bad", ["a++b", "+a", "a+", "", " ", "a + + b"])
def test_empty_segment_is_fail_loud(bad):
    with pytest.raises(ValueError, match="empty lab name"):
        split_lab_names(bad)
