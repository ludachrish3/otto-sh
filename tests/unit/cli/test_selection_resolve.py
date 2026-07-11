"""--tests name resolution: exact names, Class::name, did-you-mean."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.suite.selection import (
    UnknownSelectionError,
    repos_with_marker_matches,
    resolve_selection,
)


def _repo_with(collected: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        name="fixture-repo",
        collect_tests=lambda markers=None, suite=None, tests=None: collected,
    )


def _item(nodeid: str, name: str, cls_name: str | None) -> SimpleNamespace:
    # path mirrors the real CollectedTest contract: an absolute(-ish) path whose
    # final segment matches nodeid's file part — resolve_selection rebuilds the
    # nodeid from `path` (never the collection-rootdir-relative `nodeid` field
    # itself), since a later, independent pytest.main() call cannot resolve a
    # path relative to a rootdir chosen by an earlier, unrelated collection pass.
    file_part = nodeid.split("::", 1)[0]
    return SimpleNamespace(nodeid=nodeid, name=name, path=Path(file_part), cls_name=cls_name)


ITEMS = [
    _item("tests/t.py::TestA::test_login", "test_login", "TestA"),
    _item("tests/t.py::TestB::test_login", "test_login", "TestB"),
    _item("tests/t.py::test_plain", "test_plain", None),
    _item("tests/t.py::TestA::test_param[a]", "test_param[a]", "TestA"),
    _item("tests/t.py::TestA::test_param[b]", "test_param[b]", "TestA"),
]


def test_bare_name_matches_every_suite() -> None:
    [(_, nodeids)] = resolve_selection([_repo_with(ITEMS)], ["test_login"], "")
    assert nodeids == ["tests/t.py::TestA::test_login", "tests/t.py::TestB::test_login"]


def test_bare_name_matches_all_parametrizations() -> None:
    [(_, nodeids)] = resolve_selection([_repo_with(ITEMS)], ["test_param"], "")
    assert nodeids == ["tests/t.py::TestA::test_param[a]", "tests/t.py::TestA::test_param[b]"]


def test_plain_function_is_selectable() -> None:
    [(_, nodeids)] = resolve_selection([_repo_with(ITEMS)], ["test_plain"], "")
    assert nodeids == ["tests/t.py::test_plain"]


def test_qualified_name_disambiguates() -> None:
    [(_, nodeids)] = resolve_selection([_repo_with(ITEMS)], ["TestB::test_login"], "")
    assert nodeids == ["tests/t.py::TestB::test_login"]


def test_unknown_name_raises_with_suggestion() -> None:
    # The library raises its own UnknownSelectionError (a ValueError subclass)
    # carrying the did-you-mean message; the CLI adapter converts it to
    # typer.BadParameter with the same message + param_hint.
    with pytest.raises(UnknownSelectionError, match="test_login") as excinfo:
        resolve_selection([_repo_with(ITEMS)], ["test_logon"], "")
    assert excinfo.value.param_hint == "--tests"


def test_unknown_name_with_empty_universe_returns_empty() -> None:
    # No collected tests anywhere -> nothing to suggest -> no did-you-mean
    # error; the caller's generic "no tests matched" path takes over instead.
    assert resolve_selection([_repo_with([])], ["test_anything"], "") == []


def test_repo_without_matches_is_omitted() -> None:
    empty = _repo_with([])
    full = _repo_with(ITEMS)
    resolved = resolve_selection([empty, full], ["test_plain"], "")
    assert len(resolved) == 1
    assert resolved[0][0] is full


def test_repos_with_marker_matches_omits_repo_without_marker_hits() -> None:
    empty = _repo_with([])
    full = _repo_with(ITEMS)
    matched = repos_with_marker_matches([empty, full], "shared")
    assert matched == [full]


def test_repos_with_marker_matches_keeps_all_when_all_match() -> None:
    full_a = _repo_with(ITEMS)
    full_b = _repo_with(ITEMS)
    matched = repos_with_marker_matches([full_a, full_b], "shared")
    assert matched == [full_a, full_b]


def test_repos_with_marker_matches_empty_when_none_match() -> None:
    empty_a = _repo_with([])
    empty_b = _repo_with([])
    matched = repos_with_marker_matches([empty_a, empty_b], "shared")
    assert matched == []
