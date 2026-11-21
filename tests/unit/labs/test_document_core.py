"""The document core every file-backed source shares: its seam and its extracted helpers."""

import json
import logging
from pathlib import Path

import pytest

from otto.host.os_profile import ProfileContext
from otto.inventory import InventoryView
from otto.labs.json_repository import _addressing, _DocumentLabRepository, _JsonFileLabRepository
from otto.link import HostAddressing
from otto.models.lab import ElementSpec

_CREDS = [{"login": "admin", "password": "pw"}]


def _write(tmp_path: Path, elements: list[dict]) -> _JsonFileLabRepository:
    (tmp_path / "lab.json").write_text(json.dumps({"labs": {"bench": {}}, "elements": elements}))
    return _JsonFileLabRepository(search_paths=[tmp_path])


def _view() -> InventoryView:
    return InventoryView.of_default(inventory=None)


def test_the_core_cannot_be_built_without_its_seam() -> None:
    with pytest.raises(TypeError, match="_read_source"):
        _DocumentLabRepository()  # type: ignore[abstract]  # the point: the seam is required


def test_a_subclass_of_the_json_backend_still_builds_and_loads(tmp_path: Path) -> None:
    class Mine(_JsonFileLabRepository):  # type: ignore[misc]  # final to the checker, open at runtime
        pass

    _write(
        tmp_path,
        [{"name": "sw", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "creds": _CREDS}]}],
    )
    assert list(Mine(search_paths=[tmp_path]).load_lab("bench").hosts) == ["sw"]


def _one_id_twice(tmp_path: Path, *, second_ip: str) -> dict[str, HostAddressing]:
    """``a-b`` at 10.0.0.1 and ``a`` + board ``b`` at *second_ip*: one id, two records."""
    repository = _write(
        tmp_path,
        [
            {"name": "a-b", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "creds": _CREDS}]},
            {
                "name": "a",
                "labs": ["bench"],
                "hosts": [{"ip": second_ip, "board": "b", "creds": _CREDS}],
            },
        ],
    )
    docs = repository._load_documents(best_effort=False, view=_view())
    return _addressing(docs, view=_view(), profiles=ProfileContext.empty())


def test_one_id_at_equal_addressing_keeps_the_first_record_quietly(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="otto.labs.json_repository"):
        found = _one_id_twice(tmp_path, second_ip="10.0.0.1")
    assert found == {"a-b": HostAddressing(ip="10.0.0.1")}
    assert not caplog.records


def test_one_id_at_differing_addressing_keeps_the_first_and_warns(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="otto.labs.json_repository"):
        found = _one_id_twice(tmp_path, second_ip="10.0.0.2")
    assert found == {"a-b": HostAddressing(ip="10.0.0.1")}
    (record,) = caplog.records
    assert record.getMessage() == (
        "Duplicate host id 'a-b' across lab files with differing addressing; keeping the "
        "first. Differentiate the element name, or set board/slot."
    )


def test_summaries_match_each_element_against_the_labs_once(tmp_path: Path, monkeypatch) -> None:
    """Membership is resolved per element, not per host entry."""
    hosts = [{"ip": f"10.0.0.{i}", "board": f"b{i}", "creds": _CREDS} for i in range(1, 4)]
    repository = _write(tmp_path, [{"name": "sw", "labs": ["bench"], "hosts": hosts}])
    calls: list[str] = []
    real = ElementSpec.matches

    def counting(self: ElementSpec, name: str) -> bool:
        calls.append(name)
        return real(self, name)

    monkeypatch.setattr(ElementSpec, "matches", counting)
    summaries = repository.list_host_summaries_in_view(view=_view())
    assert len(summaries) == 3
    assert calls == ["bench"]
