"""otto init prompt/flag semantics."""

import json
import os
from pathlib import Path

from otto.cli.init import init_command
from tests._fixtures.dispatch import DispatchRunner

# init_command is a plain async function loader: DispatchRunner's dispatch
# seam wraps it in a single-command Typer named "init", which flattens to a
# bare leaf (Typer 0.26 single-command behavior) — no "init" subcommand token
# is expected on the invoked argv — and the leaf-invoke wrapper bridges the
# async body through run_command, exactly like a real `otto init` dispatch.
runner = DispatchRunner()


def _invoke(args, **kwargs):
    return runner.invoke(init_command, args, spec_name="init", **kwargs)


def test_interactive_prompts_per_missing_area(tmp_path: Path) -> None:
    # name, version, then y/n per area: settings=y, schemas=y, lab=y, tests=n, instructions=n
    result = _invoke(["--path", str(tmp_path)], input="widget\n0.1.0\ny\ny\ny\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "settings.toml").is_file()
    assert (tmp_path / ".otto" / "schemas" / "settings.schema.json").is_file()
    assert (tmp_path / "lab_data" / "lab.json").is_file()
    assert not (tmp_path / "tests" / "test_example.py").exists()


def test_all_flag_scaffolds_everything_without_prompts(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for artifact in (
        ".otto/settings.toml",
        ".otto/schemas/settings.schema.json",
        ".otto/schemas/lab.schema.json",
        "lab_data/lab.json",
        "lab_data/inventory.json",
        "lab_data/creds.json",
        "lab_data/README.md",
        "tests/test_example.py",
        "tests/conftest.py",
        "pylib/widget_instructions/__init__.py",
        ".vscode/settings.json",
        ".vscode/extensions.json",
    ):
        assert (tmp_path / artifact).exists(), artifact
    assert sorted(p.name for p in (tmp_path / "pylib").iterdir()) == ["widget_instructions"]


def test_area_flag_pulls_in_missing_settings_with_note(tmp_path: Path) -> None:
    result = _invoke(["--lab", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "settings.toml").is_file()
    assert (tmp_path / "lab_data" / "lab.json").is_file()
    assert not (tmp_path / "tests" / "test_example.py").exists()
    assert "repo marker" in result.output


def test_tests_pull_in_the_init_module_that_registers_their_options(tmp_path: Path) -> None:
    """``--tests`` alone also scaffolds the instructions module, with a note.

    The example tests import ``RepoOptions`` from the init module, which
    declares and registers it, so a tests-only repo would fail at run time.
    """
    result = _invoke(["--tests", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    init_module = tmp_path / "pylib" / "widget_instructions" / "__init__.py"
    assert '@otto.options(verbs=["run", "test"])' in init_module.read_text()
    assert "instructions area is a prerequisite" in result.output


def test_the_tests_area_interactively_scaffolds_instructions_without_asking(
    tmp_path: Path,
) -> None:
    # name, version, then y/n per area: settings=y, schemas=n, lab=n, tests=y — and
    # no prompt for instructions: the tests need it, so it is not offered.
    result = _invoke(["--path", str(tmp_path)], input="widget\n0.1.0\ny\nn\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "tests" / "test_example.py").is_file()
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    assert "Scaffold the instructions area?" not in result.output


def test_tests_beside_an_existing_instructions_area_name_the_registration(tmp_path: Path) -> None:
    """The instructions area is there already: otto does not read it, and says what it needs."""
    _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    for scaffolded in (tmp_path / "tests").iterdir():
        scaffolded.unlink()

    result = _invoke(["--tests", "--name", "widget", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "tests" / "test_example.py").is_file()
    assert "import RepoOptions from widget_instructions" in result.output
    assert '@otto.options(verbs=["run", "test"])' in result.output
    assert "prerequisite" not in result.output


def test_tests_beside_an_init_module_of_another_name_import_from_it(tmp_path: Path) -> None:
    """The example tests import ``RepoOptions`` from the repo's real init module.

    ``init = ["foo"]`` already exists, so the instructions area is found and
    not scaffolded: the tests must import from ``foo``, and the note must name
    ``foo`` and what to add to it, never a module the repo does not have.
    """
    from tests._fixtures.sutrepo import make_sut_repo

    make_sut_repo(
        tmp_path,
        name="widget",
        tests=["tests"],
        extra='libs = ["pylib"]\ninit = ["foo"]',
        files={"pylib/foo/__init__.py": ""},
    )

    result = _invoke(["--tests", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    test_src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "from foo import RepoOptions" in test_src
    assert "widget_instructions" not in test_src
    assert "import RepoOptions from foo" in result.output
    assert '@otto.options(verbs=["run", "test"])' in result.output
    assert "message" in result.output
    assert "widget_instructions" not in result.output
    assert not (tmp_path / "pylib" / "widget_instructions").exists()


def test_every_prerequisite_comes_after_the_area_that_needs_it() -> None:
    """The scaffold loop learns an area's prerequisites as it passes it, in ``AREAS`` order."""
    from otto.cli.init import AREA_PREREQUISITES, AREAS

    order = [area.name for area in AREAS]
    for area, needs in AREA_PREREQUISITES.items():
        for need in needs:
            assert order.index(need) > order.index(area), (area, need)


def test_all_scaffolds_instructions_without_the_prerequisite_note(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "prerequisite" not in result.output


def test_existing_area_is_never_rewritten(tmp_path: Path) -> None:
    _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    settings = tmp_path / ".otto" / "settings.toml"
    before = settings.read_text() + "# user edit\n"
    settings.write_text(  # sutrepo-exempt: user-edit of a scaffolded file IS the subject
        before
    )
    result = _invoke(["--all", "--name", "other", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert settings.read_text() == before


def test_later_area_uses_existing_settings_name(tmp_path: Path) -> None:
    # scaffold settings (+ everything) under an explicit name that differs from the dir
    _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    import shutil

    shutil.rmtree(tmp_path / "pylib")
    result = _invoke(["--instructions", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").exists()
    assert not (tmp_path / "pylib" / f"{tmp_path.name}_instructions").exists()


def test_epilogue_prints_next_steps(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert f"export OTTO_SUT_DIRS={tmp_path}" in result.output.replace("\n", "")
    assert "otto --install-completion" in result.output
    # Installing the completion script does not activate it in the current
    # shell; the banner has to say so (see test_init_banner.py).
    assert "source ~/.bash_completions/otto.sh" in result.output
    assert "otto test --list-tests" in result.output
    # These three need a lab to run; the printed lines must name it (like
    # step 4's `otto --lab example_lab --list-hosts`) or they fail as
    # printed with "Missing option '--lab'".
    output = result.output.replace("\n", "")
    assert "otto --lab example_lab test TestExample" in output
    assert "otto --lab example_lab test test_example_function" in output
    assert "otto --lab example_lab run smoke" in output


def test_epilogue_skips_sut_dirs_when_already_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", str(tmp_path))
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output


def test_second_run_is_pure_report(tmp_path: Path) -> None:
    _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    hosts = tmp_path / "lab_data" / "lab.json"
    mtime = hosts.stat().st_mtime_ns
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert hosts.stat().st_mtime_ns == mtime
    assert "scaffolded" not in result.output  # nothing new was written


def test_epilogue_skips_sut_dirs_when_pathsep_separated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", f"/somewhere/else{os.pathsep}{tmp_path}")
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output


def test_epilogue_skips_sut_dirs_when_comma_space_separated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_SUT_DIRS", f"/somewhere/else, {tmp_path}")
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert "export OTTO_SUT_DIRS" not in result.output


def test_schemas_flag_refreshes_stale_files(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lab_schema = tmp_path / ".otto" / "schemas" / "lab.schema.json"
    lab_schema.write_text("{}")  # simulate stale/tampered
    result = _invoke(["--schemas", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(lab_schema.read_text()).get("title") == "otto lab.json"
