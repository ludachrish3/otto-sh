"""
Direct unit tests for ``otto.utils``.

These test the validation and utility layer directly — not through the CLI —
so that bugs in functions like ``_get_literal_values`` and ``is_literal``
cannot hide behind mocked callers.
"""

import os
from typing import Literal

import pytest

from otto.utils import (
    _get_literal_values,
    complete_separated_list,
    is_literal,
    ottopath_entries,
    split_on,
)

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
        """The regression this refactor invites: `--hosts` must not acquire a new separator.

        Both assertions flip if the default separator ever changes to `+`.
        """
        # A comma IS a separator by default: the typed prefix is kept and the
        # already-chosen candidate is dropped.
        assert complete_separated_list(["x", "y"], "x,") == ["x,y"]
        # A `+` is NOT: it is an ordinary character, so nothing completes it.
        assert complete_separated_list(["x", "y"], "x+") == []


# ── ottopath_entries: the split ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        ("/a", ["/a"]),
        (f"/a{os.pathsep}/b", ["/a", "/b"]),
        (f"{os.pathsep}/a{os.pathsep}{os.pathsep}/b{os.pathsep}", ["/a", "/b"]),
    ],
)
def test_ottopath_entries_splits_on_pathsep_and_drops_only_empties(
    value: str, expected: list[str]
) -> None:
    """Splitting is pinned on the one OTTOPATH reader that remains."""
    assert ottopath_entries(value) == expected


@pytest.mark.parametrize("value", ["/a,/b", "/a, /b", "/a,"])
def test_ottopath_entries_keeps_a_comma_inside_its_entry(value: str) -> None:
    """Only ``os.pathsep`` separates: a comma is an ordinary path character."""
    assert ottopath_entries(value) == [value]


def test_ottopath_entries_keeps_whitespace_as_part_of_an_entry(tmp_path, monkeypatch) -> None:
    """``/a: b `` names ``" b "``, a relative entry, joined to the cwd as written."""
    monkeypatch.chdir(tmp_path)
    assert ottopath_entries(f"/a{os.pathsep} b ") == ["/a", f"{tmp_path}/ b "]


# ---------------------------------------------------------------------------
# PerLoop: one asyncio primitive per event loop
# ---------------------------------------------------------------------------


def _contend(lock_of) -> None:
    """Make two callers wait on the lock *lock_of* returns, on a fresh loop."""
    import asyncio

    async def hold() -> None:
        async with lock_of():
            await asyncio.sleep(0)  # the other caller now has to wait

    async def both() -> None:
        await asyncio.gather(hold(), hold())

    asyncio.run(both())


def test_per_loop_hands_each_loop_a_primitive_it_can_wait_on():
    """A primitive contended on one loop binds to it; the next loop gets its own."""
    import asyncio

    from otto.utils import PerLoop

    per_loop = PerLoop(asyncio.Lock)
    _contend(per_loop.get)
    _contend(per_loop.get)


def test_per_loop_keeps_one_primitive_within_a_loop():
    import asyncio

    from otto.utils import PerLoop

    per_loop = PerLoop(asyncio.Lock)

    async def twice() -> bool:
        return per_loop.get() is per_loop.get()

    assert asyncio.run(twice())
