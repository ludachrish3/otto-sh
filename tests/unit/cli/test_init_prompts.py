"""otto init prompt/flag semantics."""

import os
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
    assert (tmp_path / "lab_data" / "lab.json").is_file()
    assert not (tmp_path / "tests" / "test_example.py").exists()


def test_all_flag_scaffolds_everything_without_prompts(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for artifact in (
        ".otto/settings.toml",
        "lab_data/lab.json",
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
    assert (tmp_path / "lab_data" / "lab.json").is_file()
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


def test_later_area_uses_existing_settings_name(tmp_path: Path) -> None:
    # scaffold settings (+ everything) under an explicit name that differs from the dir
    runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    import shutil

    shutil.rmtree(tmp_path / "pylib")
    result = runner.invoke(_app(), ["--instructions", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").exists()
    assert not (tmp_path / "pylib" / f"{tmp_path.name}_instructions").exists()


def test_epilogue_prints_next_steps(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert f"export OTTO_SUT_DIRS={tmp_path}" in result.output.replace("\n", "")
    assert "otto --install-completion" in result.output
    assert "otto test --list-suites" in result.output


def test_epilogue_skips_sut_dirs_when_already_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", str(tmp_path))
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output


def test_second_run_is_pure_report(tmp_path: Path) -> None:
    runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    hosts = tmp_path / "lab_data" / "lab.json"
    mtime = hosts.stat().st_mtime_ns
    result = runner.invoke(_app(), ["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert hosts.stat().st_mtime_ns == mtime
    assert "scaffolded" not in result.output  # nothing new was written


def test_epilogue_skips_sut_dirs_when_pathsep_separated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", f"/somewhere/else{os.pathsep}{tmp_path}")
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output


def test_epilogue_skips_sut_dirs_when_comma_space_separated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", f"/somewhere/else, {tmp_path}")
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output
