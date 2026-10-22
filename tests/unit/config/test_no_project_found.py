"""No project found: one message, from how discovery works, wherever it surfaces (#677).

Otto discovers projects through ``OTTOPATH`` only; the current directory is
never searched. With nothing discovered, ``--list-products`` printed a
header-only table and ``--list-labs`` nothing, both exiting 0, and a lab
command blamed "no project declares a [[lab.sources]] entry". Each now says
no project was found, names ``OTTOPATH`` as it stands, and exits 1.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.config.project import no_project_found_message
from tests._fixtures.project_scaffold import make_project

runner = CliRunner()


@pytest.fixture
def nowhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty cwd that is not a project root, with OTTOPATH unset."""
    monkeypatch.delenv("OTTOPATH", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_unset_ottopath_outside_a_project(nowhere: Path) -> None:
    message = no_project_found_message()
    assert message.startswith("no otto project found: OTTOPATH is unset")
    assert str(nowhere) in message
    assert "not a project root" in message
    assert "\n" not in message


def test_unset_ottopath_inside_a_project_root_names_the_export(
    nowhere: Path,
) -> None:
    make_project(nowhere, name="p", version="1")
    message = no_project_found_message()
    assert message.startswith("no otto project found: OTTOPATH is unset")
    assert "never the current directory" in message
    assert f"export OTTOPATH={nowhere}" in message


@pytest.mark.parametrize("value", ["", ":", "::"])
def test_an_ottopath_that_names_no_directory_says_so(
    nowhere: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OTTOPATH", value)
    message = no_project_found_message()
    assert message.startswith("no otto project found: OTTOPATH is set but names no directory")


def test_an_ottopath_whose_roots_did_not_load_names_them(
    nowhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTTOPATH", f"{nowhere / 'a'}:{nowhere / 'b'}")
    message = no_project_found_message()
    assert message.startswith("no otto project found: ")
    assert str(nowhere / "a") in message
    assert str(nowhere / "b") in message
    # A root may be a real project whose settings failed to parse, or one of two
    # duplicates: the message points at the warnings, never claims it is no root.
    assert "see the warnings above" in message
    assert "must be a project root" not in message
    assert "not a project root" not in message


def test_the_export_line_quotes_a_cwd_with_a_space(tmp_path: Path, monkeypatch) -> None:
    root = make_project(tmp_path / "my project", name="p", version="1")
    monkeypatch.delenv("OTTOPATH", raising=False)
    monkeypatch.chdir(root)
    assert no_project_found_message().endswith(f"export OTTOPATH='{root}'")


@pytest.mark.parametrize("flag", ["--list-labs", "--list-products", "--list-tools"])
def test_a_listing_with_no_project_says_why_and_exits_1(nowhere: Path, flag: str) -> None:
    with patch("otto.declared.get_projects", return_value=[]):
        result = runner.invoke(app, [flag])
    assert result.exit_code == 1, result.output
    assert "no otto project found: OTTOPATH is unset" in result.output


def test_build_lab_with_no_project_reports_the_same_cause(nowhere: Path) -> None:
    from otto.session.errors import LabBuildError
    from otto.session.lab import build_lab

    with pytest.raises(LabBuildError) as caught:
        build_lab([], ["pflab"])
    assert caught.value.kind == "no_project"
    assert str(caught.value) == no_project_found_message()
    assert "[[lab.sources]]" not in str(caught.value)


def test_a_lab_command_with_no_project_prints_it_as_a_listing_does_and_exits_1(
    nowhere: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    from otto.cli.invoke import report_lab_context_error
    from otto.session.errors import LabBuildError

    err = LabBuildError(no_project_found_message(), field=None, kind="no_project")
    with pytest.raises(typer.Exit) as exited:
        report_lab_context_error(err)
    assert exited.value.exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error: no otto project found: OTTOPATH is unset")
    assert out.count("\n") == 1, "one line, never folded"
