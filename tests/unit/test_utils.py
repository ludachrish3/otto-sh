"""
Direct unit tests for ``otto.utils``.

These test the validation and utility layer directly — not through the CLI —
so that bugs in functions like ``_get_literal_values`` and ``is_literal``
cannot hide behind mocked callers.
"""

from typing import Literal

import pytest

from otto.utils import _get_literal_values, complete_separated_list, is_literal, split_on

# Sample Literal aliases used purely as fixtures for the utility functions
# below. The host term/transfer selectors are now plain ``str`` (the registries
# own validation), so these local Literals stand in to exercise
# ``_get_literal_values`` / ``is_literal`` without coupling to host types.
TermLiteral = Literal["ssh", "telnet"]
FileTransferLiteral = Literal["scp", "sftp", "ftp", "nc"]


# ── _get_literal_values ─────────────────────────────────────────────────────


class TestGetLiteralValues:
    def test_simple_literal(self):
        assert _get_literal_values(Literal["a", "b"]) == ["a", "b"]

    def test_union_of_literals(self):
        result = _get_literal_values(Literal["a", "b"])
        assert result == ["a", "b"]

    def test_nested_union(self):
        result = _get_literal_values(Literal["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_term_type(self):
        """Regression test for the match-on-special-form bug."""
        assert _get_literal_values(TermLiteral) == ["ssh", "telnet"]

    def test_file_transfer_type(self):
        """Regression test for the match-on-special-form bug."""
        assert _get_literal_values(FileTransferLiteral) == ["scp", "sftp", "ftp", "nc"]

    @pytest.mark.parametrize("bad_type", [int, str, list[str]])
    def test_non_literal_raises_value_error(self, bad_type):
        with pytest.raises(ValueError, match="not a Literal"):
            _get_literal_values(bad_type)


# ── is_literal ──────────────────────────────────────────────────────────────


class TestIsLiteral:
    @pytest.mark.parametrize("value", ["ssh", "telnet"])
    def test_valid_term_type(self, value):
        assert is_literal(value, TermLiteral) == value

    @pytest.mark.parametrize("value", ["scp", "sftp", "ftp", "nc"])
    def test_valid_file_transfer_type(self, value):
        assert is_literal(value, FileTransferLiteral) == value

    def test_invalid_value_raises_type_error(self):
        with pytest.raises(TypeError, match="not a valid value"):
            is_literal("bogus", TermLiteral)

    def test_return_value_is_same_object(self):
        val = "ssh"
        assert is_literal(val, TermLiteral) is val


# ── split_on ────────────────────────────────────────────────────────────────


class TestSplitOn:
    def test_string_input(self):
        assert split_on("a,b,c") == ["a", "b", "c"]

    def test_list_input(self):
        assert split_on(["a,b", "c,d"]) == ["a", "b", "c", "d"]

    def test_single_value(self):
        assert split_on("single") == ["single"]

    def test_empty_string(self):
        assert split_on("") == [""]

    def test_custom_separator(self):
        assert split_on("a+b", sep="+") == ["a", "b"]

    def test_custom_separator_leaves_the_default_alone(self):
        """With sep='+', a comma is just a character — and vice versa."""
        assert split_on("a,b", sep="+") == ["a,b"]
        assert split_on("a+b") == ["a+b"]


# ── complete_separated_list ─────────────────────────────────────────────────


class TestCompleteSeparatedList:
    def test_completes_first_segment(self):
        assert complete_separated_list(["tech1", "tech2", "prod"], "tech") == ["tech1", "tech2"]

    def test_keeps_prefix_and_drops_already_chosen(self):
        assert complete_separated_list(["tech1", "tech2"], "tech1,tech") == ["tech1,tech2"]

    def test_custom_separator(self):
        assert complete_separated_list(["tech1", "tech2"], "tech1+tech", sep="+") == ["tech1+tech2"]

    def test_default_separator_is_still_the_comma(self):
        """The regression this refactor invites: `--tests` must not acquire a new separator.

        Both assertions flip if the default separator ever changes to `+`.
        """
        # A comma IS a separator by default: the typed prefix is kept and the
        # already-chosen candidate is dropped.
        assert complete_separated_list(["x", "y"], "x,") == ["x,y"]
        # A `+` is NOT: it is an ordinary character, so nothing completes it.
        assert complete_separated_list(["x", "y"], "x+") == []
