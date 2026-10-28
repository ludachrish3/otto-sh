"""THE SPLIT-BRAIN GUARD for ``otto init`` and ``otto schema export``: each leaf hands the
library exactly its parsed flags and reports exactly the library's refusals, in flag
spelling. No rule may live in a leaf.

Verified red when written: hard-coding ``all_areas=True`` in the leaf's
``scaffold_candidates`` call failed every area-flag row but ``--all`` on the
``assert_called_once_with`` (``Actual: ... all_areas=True``); mapping
``kmodcov_dir`` to ``--path`` in the leaf's flag table failed both kmodcov-dir
refusal rows on the ``Invalid value for --kmodcov-dir`` assertion (the output
read ``Invalid value for --path``).
"""

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from otto.cli.init import init_command
from otto.cli.schema import schema_app
from otto.init import AreaVerdict, DoctorReport, ScaffoldReport
from tests._fixtures.dispatch import DispatchRunner

_EMPTY = ScaffoldReport([], [], [], [])
_OK = DoctorReport([AreaVerdict("settings", "ok")], [], None, None)


def _invoke(argv, **kwargs):
    return DispatchRunner().invoke(init_command, argv, spec_name="init", **kwargs)


@pytest.mark.parametrize(
    ("flags", "requested", "all_areas"),
    [
        (["--schemas"], ["schemas"], False),
        (["--lab"], ["lab"], False),
        (["--tests"], ["tests"], False),
        (["--instructions"], ["instructions"], False),
        (["--kmodcov"], ["kmodcov"], False),
        (["--embedded-gcov"], ["embedded-gcov"], False),
        (["--kmodcov", "--embedded-gcov"], ["kmodcov", "embedded-gcov"], False),
        (["--lab", "--tests"], ["lab", "tests"], False),
        (["--all"], [], True),
    ],
)
def test_area_flags_reach_scaffold_candidates_exactly(
    tmp_path: Path, flags: list[str], requested: list[str], all_areas: bool
) -> None:
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=["lab"]) as candidates,
        patch("otto.init.scaffolder.scaffold", return_value=_EMPTY) as scaffold,
        patch("otto.init.doctor.check_project", return_value=_OK) as check,
    ):
        result = _invoke([*flags, "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    candidates.assert_called_once_with(tmp_path.resolve(), requested=requested, all_areas=all_areas)
    (config, chosen), _ = scaffold.call_args
    assert chosen == ["lab"]
    assert config.root == tmp_path.resolve()
    check.assert_called_once_with(tmp_path.resolve())


def test_name_version_and_kmodcov_dir_reach_the_config(tmp_path: Path) -> None:
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=[]),
        patch("otto.init.scaffolder.scaffold", return_value=_EMPTY) as scaffold,
        patch("otto.init.doctor.check_project", return_value=_OK),
    ):
        result = _invoke(
            [
                "--kmodcov",
                "--kmodcov-dir",
                "vendor/k",
                "--name",
                "acme",
                "--version",
                "2.0.0",
                "--path",
                str(tmp_path),
            ]
        )
    assert result.exit_code == 0, result.output
    config = scaffold.call_args.args[0]
    assert (config.name, config.version, config.kmodcov_dir) == ("acme", "2.0.0", "vendor/k")


def test_embedded_gcov_dir_reaches_the_config(tmp_path: Path) -> None:
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=[]),
        patch("otto.init.scaffolder.scaffold", return_value=_EMPTY) as scaffold,
        patch("otto.init.doctor.check_project", return_value=_OK),
    ):
        result = _invoke(
            ["--embedded-gcov", "--embedded-gcov-dir", "vendor/e", "--path", str(tmp_path)]
        )
    assert result.exit_code == 0, result.output
    assert scaffold.call_args.args[0].embedded_gcov_dir == "vendor/e"


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["--kmodcov", "--kmodcov-dir", "/etc/escape"], "--kmodcov-dir"),
        (["--kmodcov", "--kmodcov-dir", "../escape"], "--kmodcov-dir"),
        (["--embedded-gcov", "--embedded-gcov-dir", "/etc/escape"], "--embedded-gcov-dir"),
        (["--embedded-gcov", "--embedded-gcov-dir", "../escape"], "--embedded-gcov-dir"),
        (["--path", "{tmp}/does-not-exist"], "--path"),
    ],
)
def test_the_real_config_refusals_reach_the_user_in_flag_spelling(
    tmp_path: Path, argv: list[str], flag: str
) -> None:
    argv = [a.format(tmp=tmp_path) for a in argv]
    if "--path" not in argv:
        argv += ["--path", str(tmp_path)]
    result = _invoke(argv)
    assert result.exit_code == 2, result.output
    assert f"Invalid value for {flag}" in result.output
    assert not (tmp_path.parent / "escape").exists()


def test_a_failing_doctor_exits_1(tmp_path: Path) -> None:
    failed = DoctorReport([AreaVerdict("settings", "failed", ["bad"])], [], None, None)
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=[]),
        patch("otto.init.scaffolder.scaffold", return_value=_EMPTY),
        patch("otto.init.doctor.check_project", return_value=failed),
    ):
        result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "bad" in result.output


@pytest.mark.parametrize("builtins_only", [False, True])
def test_schema_export_hands_write_schemas_its_flags(tmp_path: Path, builtins_only: bool) -> None:
    from otto.models.jsonschema import SchemaWrite

    out = tmp_path / "s"
    argv = ["export", "--out", str(out)] + (["--builtins-only"] if builtins_only else [])
    with patch("otto.models.jsonschema.write_schemas", return_value=SchemaWrite([], [])) as write:
        result = CliRunner().invoke(schema_app, argv)
    assert result.exit_code == 0, result.output
    write.assert_called_once_with(out, builtins_only=builtins_only)


def test_a_library_refusal_from_scaffold_reaches_the_user_in_flag_spelling(tmp_path: Path) -> None:
    """The one translation site covers the library calls, not only the config build."""
    from otto.init import InitInputError

    refusal = InitInputError("'x' must stay inside the project", field="kmodcov_dir")
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=["kmodcov"]),
        patch("otto.init.scaffolder.scaffold", side_effect=refusal),
        patch("otto.init.doctor.check_project", return_value=_OK),
    ):
        result = _invoke(["--kmodcov", "--path", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "Invalid value for --kmodcov-dir" in result.output


def test_a_library_refusal_naming_embedded_gcov_dir_reaches_the_user_in_flag_spelling(
    tmp_path: Path,
) -> None:
    from otto.init import InitInputError

    refusal = InitInputError("'x' must stay inside the project", field="embedded_gcov_dir")
    with (
        patch("otto.init.scaffolder.scaffold_candidates", return_value=["embedded-gcov"]),
        patch("otto.init.scaffolder.scaffold", side_effect=refusal),
        patch("otto.init.doctor.check_project", return_value=_OK),
    ):
        result = _invoke(["--embedded-gcov", "--path", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "Invalid value for --embedded-gcov-dir" in result.output


@pytest.mark.parametrize(
    ("option", "default"),
    [("kmodcov_dir", "DEFAULT_KMODCOV_DIR"), ("embedded_gcov_dir", "DEFAULT_EMBEDDED_GCOV_DIR")],
)
def test_a_dir_flag_defaults_to_the_config_constant(option: str, default: str) -> None:
    """The flag and InitConfig read one default: the very object, not an equal literal."""
    import inspect

    from otto.init import area_dirs
    from otto.init.config import InitConfig

    constant = getattr(area_dirs, default)
    assert inspect.signature(init_command).parameters[option].default is constant
    assert {f.name: f.default for f in dataclasses.fields(InitConfig)}[option] is constant
