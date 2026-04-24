"""
Direct unit tests for ``otto.utils``.

These test the validation and utility layer directly — not through the CLI —
so that bugs in functions like ``_get_literal_values`` and ``is_literal``
cannot hide behind mocked callers.
"""

from typing import Literal, Union

import pytest

from otto.host.connections import TermType
from otto.host.transfer import FileTransferType
from otto.utils import _get_literal_values, is_literal, splitOnCommas


# ── _get_literal_values ─────────────────────────────────────────────────────

class TestGetLiteralValues:
    def test_simple_literal(self):
        assert _get_literal_values(Literal['a', 'b']) == ['a', 'b']

    def test_union_of_literals(self):
        result = _get_literal_values(Union[Literal['a'], Literal['b']])
        assert result == ['a', 'b']

    def test_nested_union(self):
        result = _get_literal_values(
            Union[Literal['a'], Union[Literal['b'], Literal['c']]]
        )
        assert result == ['a', 'b', 'c']

    def test_term_type(self):
        """Regression test for the match-on-special-form bug."""
        assert _get_literal_values(TermType) == ['ssh', 'telnet']

    def test_file_transfer_type(self):
        """Regression test for the match-on-special-form bug."""
        assert _get_literal_values(FileTransferType) == ['scp', 'sftp', 'ftp', 'nc']

    @pytest.mark.parametrize("bad_type", [int, str, list[str]])
    def test_non_literal_raises_value_error(self, bad_type):
        with pytest.raises(ValueError, match="not a Literal"):
            _get_literal_values(bad_type)


# ── is_literal ──────────────────────────────────────────────────────────────

class TestIsLiteral:
    @pytest.mark.parametrize("value", ['ssh', 'telnet'])
    def test_valid_term_type(self, value):
        assert is_literal(value, TermType) == value

    @pytest.mark.parametrize("value", ['scp', 'sftp', 'ftp', 'nc'])
    def test_valid_file_transfer_type(self, value):
        assert is_literal(value, FileTransferType) == value

    def test_invalid_value_raises_type_error(self):
        with pytest.raises(TypeError, match="not a valid value"):
            is_literal('bogus', TermType)

    def test_return_value_is_same_object(self):
        val = 'ssh'
        assert is_literal(val, TermType) is val


# ── splitOnCommas ───────────────────────────────────────────────────────────

class TestSplitOnCommas:
    def test_string_input(self):
        assert splitOnCommas("a,b,c") == ['a', 'b', 'c']

    def test_list_input(self):
        assert splitOnCommas(["a,b", "c,d"]) == ['a', 'b', 'c', 'd']

    def test_single_value(self):
        assert splitOnCommas("single") == ['single']

    def test_empty_string(self):
        assert splitOnCommas("") == ['']
