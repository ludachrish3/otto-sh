"""slug() + make_host_id() — the frozen host-id derivation contract."""

import re

import pytest

from otto.host.remote_host import make_host_id, make_host_name, slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("server", "server"),  # simple token -> identity
        ("Server", "server"),  # case-folded
        ("Lab X Server", "lab-x-server"),  # spaces -> single hyphen
        ("Big  Board!", "big-board"),  # punctuation + run collapse
        ("wr_linux", "wr-linux"),  # underscore folds to hyphen (never reaches id as _)
        ("--edge--", "edge"),  # strip leading/trailing hyphens
    ],
)
def test_slug_cases(raw, expected):
    assert slug(raw) == expected


def test_slug_empty_is_empty_string():
    # All-punctuation slugs to empty; callers treat empty as an error.
    assert slug("___") == ""
    assert slug("") == ""


_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@pytest.mark.parametrize(
    ("element", "board", "slot", "expected"),
    [
        ("test1", None, None, "test1"),
        ("test1", "bb", None, "test1-bb"),
        ("test1", "bb", 0, "test1-bb-0"),
        ("Edge Router", "line.card", 3, "edge-router-line-card-3"),
        ("Test", "BoardX", 2, "test-boardx-2"),
        ("edge-node", "line", None, "edge-node-line"),
    ],
)
def test_make_host_id_joins_every_portion_with_a_hyphen(element, board, slot, expected):
    hid = make_host_id(element, board, slot)
    assert hid == expected
    assert _ID.fullmatch(hid), hid


def test_slot_zero_is_a_portion():
    """A falsy slot is still a slot: 0 renders as `-0`, never as nothing."""
    assert make_host_id("test1", "bb", 0) == "test1-bb-0"
    assert make_host_id("test1", "bb", None) == "test1-bb"


def test_no_underscore_ever_reaches_an_id():
    assert "_" not in make_host_id("edge_node", "line_card", 7)
    assert make_host_id("edge_node", "line_card", 7) == "edge-node-line-card-7"


def test_selector_matches_the_new_seam():
    """A [host_preferences] selector is a fullmatch regex over the id (capability.py)."""
    assert re.fullmatch("test1-.*", make_host_id("test1", "bb", 0))
    assert not re.fullmatch("test1_.*", make_host_id("test1", "bb", 0))


@pytest.mark.parametrize(
    ("element", "board", "slot", "expected"),
    [
        ("Edge Router", None, None, "Edge Router"),
        ("Edge Router", "Line.Card", None, "Edge Router Line.Card"),
        ("Edge Router", "Line.Card", 0, "Edge Router Line.Card 0"),
        ("Edge Router", None, 3, "Edge Router"),  # a slot without a board is ignored
    ],
)
def test_make_host_name_is_the_space_joined_label_as_written(element, board, slot, expected):
    """The display name: element, board, slot — verbatim, slot only with a board."""
    assert make_host_name(element, board, slot) == expected
