"""slug() + make_host_id() — the frozen host-id derivation contract."""

import pytest

from otto.host.remote_host import make_host_id, slug


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


def test_make_host_id_simple_element_is_identity():
    # Contract: a simple [a-z0-9] element slugs to itself, so ids are byte-identical
    # to the pre-slug make_host_id — existing link ids/fixtures are undisturbed.
    assert make_host_id("test", 5, "boardx", 2) == "test5_boardx2"
    assert make_host_id("solo", None, None, None) == "solo"
    assert make_host_id("Test", 5, "BoardX", 2) == "test5_boardx2"


def test_make_host_id_multiword_element_slugs():
    assert make_host_id("Lab X Server", None, None, None) == "lab-x-server"
    assert make_host_id("Lab X Server", 2, None, None) == "lab-x-server2"


def test_make_host_id_only_underscore_is_board_delimiter():
    # Hyphens live only inside slugs; the single underscore is the board delimiter.
    hid = make_host_id("edge node", 1, "line card", 3)
    assert hid == "edge-node1_line-card3"
    assert hid.count("_") == 1
