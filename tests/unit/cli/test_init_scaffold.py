"""Each init area scaffolds artifacts that otto's real ingestion accepts."""

import json
from pathlib import Path

from otto.cli.init import AREAS, InitConfig

CFG = InitConfig(name="widget", version="0.1.0")
BY_NAME = {a.name: a for a in AREAS}


def test_area_order_is_settings_first() -> None:
    assert [a.name for a in AREAS] == ["settings", "lab", "tests", "instructions"]


def test_settings_scaffold_parses_via_settings_model(tmp_path: Path) -> None:
    created = BY_NAME["settings"].scaffold(tmp_path, CFG)
    settings = tmp_path / ".otto" / "settings.toml"
    assert settings in created
    import tomli

    from otto.models.settings import SettingsModel

    data = tomli.loads(settings.read_text())
    model = SettingsModel.model_validate(data)  # adapt: match how Repo parses (see repo.py:532-561)
    assert model.name == "widget"
    # conventional paths pre-wired so later area scaffolds never edit settings
    assert data["labs"] == ["${sut_dir}/lab_data"]
    assert data["tests"] == ["${sut_dir}/tests"]
    assert data["libs"] == ["${sut_dir}/pylib"]
    assert data["init"] == ["widget_instructions"]


def test_lab_scaffold_passes_hostspec_ingest(tmp_path: Path) -> None:
    BY_NAME["lab"].scaffold(tmp_path, CFG)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    from otto.models.host import UnixHostSpec

    assert data["links"] == []  # links section present, empty by default
    hosts = data["hosts"]
    spec = UnixHostSpec.model_validate(hosts[0])
    assert spec.element == "example-device"
    assert spec.labs == ["example_lab"]
    assert "_comment" in hosts[0]  # the docs pointer rides in the host entry
    assert (tmp_path / "lab_data" / "README.md").exists()


def test_tests_scaffold_suite_autoregisters(tmp_path: Path) -> None:
    BY_NAME["tests"].scaffold(tmp_path, CFG)
    src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "class TestExample(OttoSuite" in src
    assert "register_suite" not in src  # decorator is gone (companion plan)
    assert "def test_example_function" in src
    assert (tmp_path / "tests" / "conftest.py").exists()


def test_instructions_scaffold_imports(tmp_path: Path) -> None:
    BY_NAME["instructions"].scaffold(tmp_path, CFG)
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").exists()


def test_detect_flips_after_scaffold(tmp_path: Path) -> None:
    for area in AREAS:
        assert not area.detect(tmp_path)
        area.scaffold(tmp_path, CFG)
        assert area.detect(tmp_path)
