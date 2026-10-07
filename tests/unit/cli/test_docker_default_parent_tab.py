"""A TAB reads the default parent from the STORED names cache, never by loading a lab."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from otto import bootstrap
from otto.cli import docker as docker_cli
from otto.config import completion_cache as cc
from tests._fixtures.sutrepo import touch_settings


@pytest.fixture
def stored_names(tmp_path, monkeypatch):
    """Write a real cache carrying the map, read it back, install it as the TAB snapshot."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    repo = MagicMock()
    repo.sut_dir = tmp_path / "sut"
    repo.sut_dir.mkdir()
    touch_settings(repo.sut_dir)
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.inventory_settings = {}
    cc.write_cache(
        [repo],
        instructions=[],
        hosts=[],
        docker_default_parent_by_lab={"east": "test3", "west": "alt2"},
    )
    view = cc.read_cache([repo])
    assert view is not None
    saved = bootstrap.get_completion_names()
    bootstrap.set_completion_names(view)
    try:
        yield
    finally:
        bootstrap.set_completion_names(saved)


def _ctx(labs=None):
    root = SimpleNamespace(params={"labs": labs} if labs else {}, parent=None)
    return SimpleNamespace(params={}, parent=root, resilient_parsing=True)


def test_the_selected_labs_entry_is_read_from_the_stored_map(stored_names):
    assert docker_cli._default_parent_for_tab(_ctx(["east"])) == "test3"
    assert docker_cli._default_parent_for_tab(_ctx(["west"])) == "alt2"


def test_two_labs_with_different_defaults_is_not_guessed_at(stored_names):
    assert docker_cli._default_parent_for_tab(_ctx(["east", "west"])) is None
    assert docker_cli._default_parent_for_tab(_ctx()) is None


def test_a_lab_the_map_lacks_has_no_default(stored_names):
    assert docker_cli._default_parent_for_tab(_ctx(["north"])) is None


def test_a_selection_with_a_lab_the_map_lacks_offers_nothing(stored_names):
    """east resolves, north refused: the verb may refuse the merged lab, so TAB offers none."""
    assert docker_cli._default_parent_for_tab(_ctx(["east", "north"])) is None
    assert docker_cli._default_parent_for_tab(_ctx(["north", "east"])) is None


# The mirror table: ``(by_lab, labs, expected)``. Twin of ``MIRROR_ROWS`` in
# tests/unit/shim/test_observed_source.py, which runs the SAME rows through the shim's
# ``_default_parent`` -- keep the two verbatim (the "change both or neither" contract is this
# table). The real collector only writes ``dict[str, str]``, so the non-str rows are the only
# thing that pins the ``isinstance`` filter on each branch.
_AGREE = {"east": "dut1", "west": "dut3", "north": "dut1"}
MIRROR_ROWS = [
    pytest.param(_AGREE, ["east"], "dut1", id="one-lab-resolves"),
    pytest.param(_AGREE, ["east", "north"], "dut1", id="labs-agree"),
    pytest.param(_AGREE, ["east", "west"], None, id="labs-disagree"),
    pytest.param(_AGREE, ["south"], None, id="selected-lab-absent"),
    pytest.param(_AGREE, ["east", "south"], None, id="one-absent-after-one-resolves"),
    pytest.param(_AGREE, ["south", "east"], None, id="one-absent-before-one-resolves"),
    pytest.param(_AGREE, [], None, id="no-labs-two-str-entries"),
    pytest.param({"east": 5}, ["east"], None, id="selected-lab-non-str-int"),
    pytest.param({"east": None}, ["east"], None, id="selected-lab-non-str-none"),
    pytest.param({"east": 5, "west": 5}, ["east", "west"], None, id="labs-agree-on-a-non-str"),
    pytest.param({"east": "dut1"}, [], "dut1", id="no-labs-one-str-entry"),
    pytest.param({"east": "dut1", "west": 7}, [], "dut1", id="no-labs-non-str-entry-ignored"),
    pytest.param({"east": 5}, [], None, id="no-labs-only-a-non-str-entry"),
    pytest.param({"east": "dut1"}, ["east", "south"], None, id="single-entry-but-lab-absent"),
    pytest.param(["east"], [], None, id="non-dict-map-no-labs"),
    pytest.param(["east"], ["east"], None, id="non-dict-map-with-labs"),
    pytest.param("dut1", [], None, id="str-map"),
]


@pytest.mark.parametrize(("by_lab", "labs", "expected"), MIRROR_ROWS)
def test_default_parent_mirror_table(by_lab, labs, expected, monkeypatch):
    """The shim twin runs the identical rows; the two functions must agree on every one."""
    saved = bootstrap.get_completion_names()
    bootstrap.set_completion_names({"docker_default_parent_by_lab": by_lab})
    monkeypatch.setattr(docker_cli, "_selected_labs_for_tab", lambda ctx: list(labs))
    try:
        assert docker_cli._default_parent_for_tab(_ctx()) == expected
    finally:
        bootstrap.set_completion_names(saved)


def test_reading_the_default_parent_never_bootstraps(stored_names):
    with patch(
        "otto.bootstrap.get_repos", side_effect=AssertionError("bootstrap in a TAB")
    ) as get_repos:
        assert docker_cli._default_parent_for_tab(_ctx(["east"])) == "test3"
    get_repos.assert_not_called()
