"""otto init prompt/flag semantics."""

from pathlib import Path

import typer
from typer.testing import CliRunner

from otto.cli.init import init_command
from otto.utils import async_typer_command

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    # Registered commands run through the root app, which wraps every leaf via
    # async_typer_command (see registry.py / resolve_spec_command). Mirror that
    # here so a bare-app CliRunner invocation can drive the async command body.
    # NB: a Typer() app with exactly one registered command collapses to that
    # command directly (Typer 0.26 single-command behavior) — no "init"
    # subcommand token is expected on the invoked argv.
    app.command("init")(async_typer_command(init_command))
    return app


def test_interactive_prompts_per_missing_area(tmp_path: Path) -> None:
    # name, version, then y/n per area: settings=y, lab=y, tests=n, instructions=n
    result = runner.invoke(_app(), ["--path", str(tmp_path)], input="widget\n0.1.0\ny\ny\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "settings.toml").is_file()
    assert (tmp_path / "lab_data" / "hosts.json").is_file()
    assert not (tmp_path / "tests" / "test_example.py").exists()


def test_all_flag_scaffolds_everything_without_prompts(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for artifact in (
        ".otto/settings.toml",
        "lab_data/hosts.json",
        "lab_data/README.md",
        "tests/test_example.py",
        "tests/conftest.py",
        "pylib/widget_instructions/__init__.py",
    ):
        assert (tmp_path / artifact).exists(), artifact


def test_area_flag_pulls_in_missing_settings_with_note(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--lab", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "settings.toml").is_file()
    assert (tmp_path / "lab_data" / "hosts.json").is_file()
    assert not (tmp_path / "tests" / "test_example.py").exists()
    assert "repo marker" in result.output


def test_existing_area_is_never_rewritten(tmp_path: Path) -> None:
    runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    settings = tmp_path / ".otto" / "settings.toml"
    before = settings.read_text() + "# user edit\n"
    settings.write_text(before)
    result = runner.invoke(_app(), ["--all", "--name", "other", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert settings.read_text() == before
