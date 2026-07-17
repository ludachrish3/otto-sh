"""Each init area scaffolds artifacts that otto's real ingestion accepts."""

import json
from pathlib import Path

from otto.cli.init import AREAS, InitConfig

CFG = InitConfig(name="widget", version="0.1.0")
BY_NAME = {a.name: a for a in AREAS}


def test_area_order_is_settings_first() -> None:
    assert [a.name for a in AREAS] == ["settings", "schemas", "lab", "tests", "instructions"]


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


def test_settings_scaffold_has_commented_monitor_tls_block(tmp_path: Path) -> None:
    """The commented example sections include a `[monitor]` sibling to `[docker]`.

    Pins the raw template text alongside the two TLS keys
    `MonitorSettingsSpec` accepts, so a user uncommenting the block gets a
    working starting point. Commented-out TOML uses the no-space `#key`
    convention, so the uncomment drift test in test_init_templates.py also
    validates the block against the real model.
    """
    BY_NAME["settings"].scaffold(tmp_path, CFG)
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert "#[monitor]" in text
    assert '#tls_cert = "~/.config/otto/tls/monitor-cert.pem"' in text
    assert '#tls_key = "~/.config/otto/tls/monitor-key.pem"' in text


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
    assert data["$schema"] == "../.otto/schemas/lab.schema.json"


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


def test_tests_scaffold_creates_shared_options_module(tmp_path: Path) -> None:
    created = BY_NAME["tests"].scaffold(tmp_path, CFG)
    options_mod = tmp_path / "pylib" / "widget_options.py"
    assert options_mod in created
    src = options_mod.read_text()
    assert "class RepoOptions" in src
    assert "hello from widget" in src
    suite_src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "from widget_options import RepoOptions" in suite_src
    assert "class _Options(RepoOptions)" in suite_src


def test_instructions_scaffold_creates_shared_options_module(tmp_path: Path) -> None:
    created = BY_NAME["instructions"].scaffold(tmp_path, CFG)
    assert tmp_path / "pylib" / "widget_options.py" in created
    src = (tmp_path / "pylib" / "widget_instructions" / "__init__.py").read_text()
    assert "from widget_options import RepoOptions" in src
    assert "@instruction(options=_Options)" in src


def test_options_module_scaffold_is_idempotent_either_order(tmp_path: Path) -> None:
    first = BY_NAME["tests"].scaffold(tmp_path, CFG)
    options_mod = tmp_path / "pylib" / "widget_options.py"
    assert options_mod in first
    marker = "# user edited\n" + options_mod.read_text()
    options_mod.write_text(marker)
    second = BY_NAME["instructions"].scaffold(tmp_path, CFG)
    assert options_mod not in second  # not re-created...
    assert options_mod.read_text() == marker  # ...and never overwritten
    # reverse order in a fresh tree
    other = tmp_path / "other"
    other.mkdir()
    assert other / "pylib" / "widget_options.py" in BY_NAME["instructions"].scaffold(other, CFG)
    assert other / "pylib" / "widget_options.py" not in BY_NAME["tests"].scaffold(other, CFG)


def test_module_names_are_sanitized_identifiers(tmp_path: Path) -> None:
    cfg = InitConfig(name="my-repo 2.0", version="0.1.0")
    assert cfg.module_base == "my_repo_2_0"
    BY_NAME["settings"].scaffold(tmp_path, cfg)
    BY_NAME["instructions"].scaffold(tmp_path, cfg)
    import tomli

    data = tomli.loads((tmp_path / ".otto" / "settings.toml").read_text())
    assert data["name"] == "my-repo 2.0"  # display name keeps the raw value
    assert data["init"] == ["my_repo_2_0_instructions"]
    assert (tmp_path / "pylib" / "my_repo_2_0_instructions" / "__init__.py").exists()
    assert (tmp_path / "pylib" / "my_repo_2_0_options.py").exists()


def test_schemas_scaffold_writes_schema_files(tmp_path: Path) -> None:
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    out = tmp_path / ".otto" / "schemas"
    for stem in ("settings", "lab", "link", "reservations"):
        assert out / f"{stem}.schema.json" in created
    data = json.loads((out / "lab.schema.json").read_text())
    assert data["title"] == "otto lab.json"


def test_schemas_scaffold_writes_vscode_wiring_when_absent(tmp_path: Path) -> None:
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    settings = tmp_path / ".vscode" / "settings.json"
    extensions = tmp_path / ".vscode" / "extensions.json"
    assert settings in created
    assert extensions in created
    wiring = json.loads(settings.read_text())
    urls = [entry["url"] for entry in wiring["json.schemas"]]
    assert "./.otto/schemas/lab.schema.json" in urls
    assert "./.otto/schemas/reservations.schema.json" in urls
    assert "evenBetterToml.schema.associations" in wiring
    toml_associations = wiring["evenBetterToml.schema.associations"]
    assert toml_associations[r".*/settings\.toml$"] == "./.otto/schemas/settings.schema.json"
    assert "tamasfe.even-better-toml" in json.loads(extensions.read_text())["recommendations"]


def test_existing_vscode_settings_left_byte_for_byte_untouched(tmp_path: Path) -> None:
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    original = '// user file with comments\n{ "editor.rulers": [88] }\n'  # JSONC on purpose
    (vscode / "settings.json").write_text(original)
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    assert (vscode / "settings.json").read_text() == original
    assert vscode / "settings.json" not in created
    assert vscode / "extensions.json" in created  # independent only-if-absent check
