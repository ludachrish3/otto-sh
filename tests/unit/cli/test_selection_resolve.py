"""--tests name resolution: exact names, Class::name, did-you-mean."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from otto.cli.test import _resolve_selection


def _repo_with(collected: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        name="fixture-repo",
        collect_tests=lambda markers=None, suite=None, tests=None: collected,
    )


def _item(nodeid: str, name: str, cls_name: str | None) -> SimpleNamespace:
    # path mirrors the real CollectedTest contract: an absolute(-ish) path whose
    # final segment matches nodeid's file part — _resolve_selection rebuilds the
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
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_login"], "")
    assert nodeids == ["tests/t.py::TestA::test_login", "tests/t.py::TestB::test_login"]


def test_bare_name_matches_all_parametrizations() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_param"], "")
    assert nodeids == ["tests/t.py::TestA::test_param[a]", "tests/t.py::TestA::test_param[b]"]


def test_plain_function_is_selectable() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_plain"], "")
    assert nodeids == ["tests/t.py::test_plain"]


def test_qualified_name_disambiguates() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["TestB::test_login"], "")
    assert nodeids == ["tests/t.py::TestB::test_login"]


def test_unknown_name_raises_with_suggestion() -> None:
    with pytest.raises(typer.BadParameter, match="test_login"):
        _resolve_selection([_repo_with(ITEMS)], ["test_logon"], "")


def test_repo_without_matches_is_omitted() -> None:
    empty = _repo_with([])
    full = _repo_with(ITEMS)
    resolved = _resolve_selection([empty, full], ["test_plain"], "")
    assert len(resolved) == 1
    assert resolved[0][0] is full
