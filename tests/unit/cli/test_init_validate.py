"""otto init validates existing areas via real ingestion code — never rewrites."""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.init import init_command
from otto.utils import async_typer_command

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the rich console width so table cells never fold inside asserted text.

    The report table's ``detail`` column uses ``overflow="fold"``; under
    CliRunner (non-tty) rich resolves its width from the ``COLUMNS`` env var,
    defaulting to 80. The fold point then depends on the length of the
    tmp-path rendered in the same cell — long CI basetemp paths shifted it
    into the middle of ``"must be a JSON object"`` and broke the substring
    assertions (GH issue #89). A fixed, generous width makes rendering
    deterministic everywhere.
    """
    monkeypatch.setenv("COLUMNS", "300")


def _app() -> typer.Typer:
    app = typer.Typer()
    # Mirror registry.py / resolve_spec_command: every leaf command is wrapped
    # with async_typer_command. A Typer() app with exactly one registered
    # command collapses to that command directly (Typer 0.26 single-command
    # behavior), so no "init" subcommand token is expected on argv.
    app.command("init")(async_typer_command(init_command))
    return app


def _scaffold_all(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_valid_repo_reports_all_ok_and_exits_zero(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.count("✓") >= 4


def test_broken_settings_key_fails_with_pydantic_error(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(settings.read_text().replace("version =", "verzion ="))
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "verzion" in result.output


def test_invalid_host_field_fails_named(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"ip"', '"ipp"'))
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "ipp" in result.output


def test_non_dict_host_entry_fails_named(tmp_path: Path) -> None:
    """A non-object hosts[] entry gets a clean indexed error, not an AttributeError."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["hosts"].append("oops")
    lab_file.write_text(json.dumps(data))
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "must be a JSON object" in result.output
    assert "str" in result.output


def test_invalid_link_entry_fails_named(tmp_path: Path) -> None:
    """A structurally invalid links[] entry surfaces a named validation error."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    # LinkSpec requires exactly two endpoints; one endpoint fails validation.
    data["links"].append({"endpoints": [{"host": "example-device"}]})
    lab_file.write_text(json.dumps(data))
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "links[0]" in result.output


def test_valid_link_entry_passes(tmp_path: Path) -> None:
    """A well-formed links[] entry validates clean alongside the example host."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["links"].append({"endpoints": [{"host": "example-device"}, {"host": "other-device"}]})
    lab_file.write_text(json.dumps(data))
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_missing_libs_dir_reported(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    # Remove only the module's __init__.py, NOT the whole pylib/ tree: the
    # instructions area's `detect` considers the module dir's mere existence
    # sufficient (so re-running --all would silently heal a fully-removed
    # pylib/ as "missing" rather than reporting it broken — see
    # _detect_instructions). Deleting just __init__.py keeps `detect` truthy
    # (module dir still exists) so this routes to `validate`, which does
    # require __init__.py and reports the gap under the "pylib" path.
    (tmp_path / "pylib" / "widget_instructions" / "__init__.py").unlink()
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "pylib" in result.output
