"""The loader validates each entry once, through the factory, with the same attribution."""

import pytest

from otto.host.power import POWER_CONTROLLERS
from otto.inventory import InventoryError
from otto.labs.errors import LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository
from otto.registry import BackendRegistry
from tests._fixtures.labdata import write_lab_json

_CREDS = [{"login": "u", "password": "p"}]
_BAD = [
    pytest.param({"ip": 5}, r"ip", id="structural"),
    pytest.param(
        {"ip": "10.0.0.1", "os_type": "windows"}, r"Unknown os_type profile 'windows'", id="os_type"
    ),
    pytest.param(
        {"ip": "10.0.0.1", "valid_terms": ["console"]},
        r"host 'bad': term 'console' requires console_options\.server",
        id="contextual",
    ),
    pytest.param(
        {"ip": "10.0.0.1", "power_control": {"type": "command", "on_cmd": 5}},
        r"host 'bad': .*parse failed",
        id="power",
    ),
]


@pytest.mark.parametrize(("entry", "tail"), _BAD)
def test_the_loader_reports_the_factory_s_error_with_file_element_and_index(tmp_path, entry, tail):
    hosts = [
        {"ip": "10.0.0.2", "creds": _CREDS, "element": "ok", "labs": ["l"]},
        {**entry, "creds": _CREDS, "element": "bad", "labs": ["l"]},
    ]
    path = write_lab_json(tmp_path / "lab.json", hosts, links=[])
    with pytest.raises(
        LabRepositoryError,
        match=rf"Lab file '{path}': element 'bad' hosts\[0\] in lab 'l': .*{tail}",
    ):
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")


def test_an_unresolved_inventory_reference_is_attributed_like_a_factory_error(tmp_path):
    """Resolution fails before the factory runs; the loader names the same file, element, index."""
    hosts = [
        {"ip": "10.0.0.2", "creds": _CREDS, "element": "ok", "labs": ["l"]},
        {"ip": "10.0.0.1", "inventory": "k", "creds": _CREDS, "element": "bad", "labs": ["l"]},
    ]
    path = write_lab_json(tmp_path / "lab.json", hosts, links=[])
    with pytest.raises(
        LabRepositoryError,
        match=rf"Lab file '{path}': element 'bad' hosts\[0\] in lab 'l': .*inventory",
    ) as exc:
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")
    assert isinstance(exc.value.__cause__, InventoryError)


def test_a_loaded_host_parses_its_power_table_once(tmp_path, monkeypatch):
    calls: list[str] = []
    real = BackendRegistry.prepare

    def spy(self, name, raw, env, *, source=None):
        if self is POWER_CONTROLLERS:
            calls.append(name)
        return real(self, name, raw, env, source=source)

    monkeypatch.setattr(BackendRegistry, "prepare", spy)
    hosts = [
        {
            "ip": "10.0.0.1",
            "creds": _CREDS,
            "element": "p",
            "labs": ["l"],
            "power_control": {"type": "command", "on_cmd": "on", "off_cmd": "off"},
        }
    ]
    write_lab_json(tmp_path / "lab.json", hosts, links=[])
    JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")
    assert calls == ["command"]
