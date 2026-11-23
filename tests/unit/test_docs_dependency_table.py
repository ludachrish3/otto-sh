import importlib.util
import json

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

_MODULE_PATH = CHECKOUT_ROOT / "scripts" / "check_docs_dependency_table.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_docs_dependency_table", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PYPROJECT = """\
[project]
name = "otto-sh"
dependencies = [
    "aioftp>=0.27.2,<0.29",
    "pydantic>=2.6,<3",
    "typer>=0.26,<0.27",
]
"""

TABLE_DOC = """\
## Dependencies reference

### Direct runtime dependencies

| Package | Min version | Below | Purpose |
| ------- | ----------- | ----- | ------- |
| `aioftp` | 0.27.2 | 0.29 | FTP |
| `pydantic` | 2.6 | 3 | Validation |
| `typer` | 0.26 | 0.27 | CLI |

### Native-extension transitive dependencies

| Package | Pulled in by | Notes |
| ------- | ------------ | ----- |
| `cffi` | cryptography | not part of the direct table |
"""


def test_parse_dependencies_extracts_floor_and_bound_once():
    mod = _load()
    parsed = mod.parse_dependencies(PYPROJECT)
    assert {k: (d.floor, d.bound) for k, d in parsed.items()} == {
        "aioftp": ("0.27.2", "0.29"),
        "pydantic": ("2.6", "3"),
        "typer": ("0.26", "0.27"),
    }


def test_parse_dependencies_keys_are_canonical_but_names_stay_as_written():
    mod = _load()
    parsed = mod.parse_dependencies('[project]\nname = "x"\ndependencies = ["Foo_Bar>=1.0,<2"]\n')
    assert list(parsed) == ["foo-bar"]
    assert parsed["foo-bar"].name == "Foo_Bar"


def test_parse_dependencies_rejects_an_entry_with_no_floor():
    mod = _load()
    with pytest.raises(SystemExit, match="cannot parse dependency floor"):
        mod.parse_dependencies('[project]\nname = "x"\ndependencies = ["foo<2"]\n')


def test_parse_table_scoped_to_direct_section():
    mod = _load()
    parsed = mod.parse_table(TABLE_DOC)
    assert {k: (r.floor, r.bound) for k, r in parsed.items()} == {
        "aioftp": ("0.27.2", "0.29"),
        "pydantic": ("2.6", "3"),
        "typer": ("0.26", "0.27"),
    }
    assert "cffi" not in parsed


def _diff(doc=TABLE_DOC, pyproject=PYPROJECT):
    mod = _load()
    return mod.diff(mod.parse_dependencies(pyproject), mod.parse_table(doc))


def test_in_sync_passes():
    assert _diff() == []


def test_missing_table_row_is_drift():
    problems = _diff(TABLE_DOC.replace("| `typer` | 0.26 | 0.27 | CLI |\n", ""))
    assert any("typer" in p and "missing" in p for p in problems)


def test_stale_min_version_is_drift():
    problems = _diff(TABLE_DOC.replace("| `pydantic` | 2.6 |", "| `pydantic` | 2.5 |"))
    assert any("pydantic" in p for p in problems)


def test_extra_table_row_is_drift():
    doc = TABLE_DOC.replace(
        "| `typer` | 0.26 | 0.27 | CLI |",
        "| `typer` | 0.26 | 0.27 | CLI |\n| `left-pad` | 1.0 | 2 | ? |",
    )
    assert any("left-pad" in p for p in _diff(doc))


def test_table_bound_that_differs_names_the_package_and_both_values():
    problems = _diff(TABLE_DOC.replace("| `pydantic` | 2.6 | 3 |", "| `pydantic` | 2.6 | 4 |"))
    assert len(problems) == 1
    assert "pydantic" in problems[0]
    assert "4" in problems[0]
    assert "3" in problems[0]


def test_old_three_column_table_fails_with_a_clear_message():
    doc = (
        "### Direct runtime dependencies\n\n| Package | Min version | Purpose |\n"
        "| --- | --- | --- |\n| `aioftp` | 0.27.2 | FTP |\n| `pydantic` | 2.6 | 3 |\n"
        "| `typer` | 0.26 | CLI |\n"
    )
    problems = _diff(doc)
    assert len(problems) == 3
    assert all("no 'Below' column" in p for p in problems)


def test_live_checkout_is_in_sync():
    mod = _load()
    assert mod.main([]) == 0


def test_main_exits_1_on_drift(tmp_path):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text(PYPROJECT)
    doc = tmp_path / "installation.md"
    doc.write_text(TABLE_DOC.replace("| `pydantic` | 2.6 |", "| `pydantic` | 2.5 |"))
    assert mod.main([str(py), str(doc)]) == 1


def test_main_reports_missing_section(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text(PYPROJECT)
    doc = tmp_path / "installation.md"
    doc.write_text(TABLE_DOC.replace("### Direct runtime dependencies", "### Renamed"))
    assert mod.main([str(py), str(doc)]) == 1
    assert "no rows found" in capsys.readouterr().out


def test_floor_prints_one_dependencys_floor(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[project]\nname = "otto-sh"\ndependencies = ["uv>=0.11,<0.14", "rich>=15.0.0"]\n'
    )
    assert mod.main(["--floor", "uv", str(py)]) == 0
    assert capsys.readouterr().out == "0.11\n"


def test_floor_of_an_unknown_dependency_fails(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "otto-sh"\ndependencies = ["rich>=15.0.0"]\n')
    assert mod.main(["--floor", "uv", str(py)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "uv: not a direct dependency in" in captured.err


def test_floor_without_a_name_is_a_usage_error(capsys):
    mod = _load()
    assert mod.main(["--floor"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


def test_floor_with_extra_arguments_is_a_usage_error(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "otto-sh"\ndependencies = ["uv>=0.11,<0.14"]\n')
    assert mod.main(["--floor", "uv", str(py), "extra"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


# -- --constraints: the floors, pinned, for the floor smoke's resolution (#1181) --


def test_constraints_pin_every_direct_dependency_to_its_floor(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[project]\nname = "otto-sh"\n'
        'dependencies = ["requests>=2.26.0", "uv>=0.11,<0.14", "pydantic>=2.10.4,<3"]\n'
    )
    assert mod.main(["--constraints", str(py)]) == 0
    assert capsys.readouterr().out == "requests==2.26.0\nuv==0.11\npydantic==2.10.4\n"


def test_constraints_of_the_live_checkout_cover_every_dependency(capsys):
    mod = _load()
    assert mod.main(["--constraints"]) == 0
    lines = capsys.readouterr().out.splitlines()
    deps = mod.parse_dependencies((CHECKOUT_ROOT / "pyproject.toml").read_text())
    assert lines == [f"{d.name}=={d.floor}" for d in deps.values()]


def test_constraints_with_extra_arguments_is_a_usage_error(tmp_path, capsys):
    mod = _load()
    assert mod.main(["--constraints", "a", "b"]) == 2
    assert "usage:" in capsys.readouterr().err


# -- upper bounds: every dependency is capped at the next version that may break (#1287) --


def _lock(**versions):
    """A minimal uv.lock text; a value may be one version or a list (platform forks)."""
    blocks = []
    for name, value in versions.items():
        versions = [value] if isinstance(value, str) else value
        blocks.extend(f'[[package]]\nname = "{name}"\nversion = "{v}"\n' for v in versions)
    return "\n".join(blocks)


def _bound_problems(deps, **locked):
    mod = _load()
    py = f'[project]\nname = "otto-sh"\ndependencies = {json.dumps(deps)}\n'
    return mod.bound_problems(mod.parse_dependencies(py), mod.parse_locked(_lock(**locked)))


def test_bounds_rule_one_x_below_next_major_passes():
    assert _bound_problems(["requests>=2.30.0,<3"], requests="2.34.2") == []


def test_bounds_rule_zero_x_below_next_minor_passes():
    assert _bound_problems(["typer>=0.29,<0.30"], typer="0.29.1") == []


def test_bounds_missing_upper_bound_names_the_package():
    problems = _bound_problems(["requests>=2.30.0"], requests="2.34.2")
    assert len(problems) == 1
    assert "requests" in problems[0]
    assert "no upper bound" in problems[0]


def test_bounds_looser_than_rule_for_one_x_is_rejected():
    problems = _bound_problems(["requests>=2.30.0,<4"], requests="2.34.2")
    assert len(problems) == 1
    assert "requests" in problems[0]
    assert "looser" in problems[0]
    assert "<3" in problems[0]


def test_bounds_looser_than_rule_for_zero_x_is_rejected():
    problems = _bound_problems(["typer>=0.29,<1"], typer="0.29.1")
    assert len(problems) == 1
    assert "typer" in problems[0]
    assert "looser" in problems[0]
    assert "<0.30" in problems[0]


def test_bounds_tighter_than_rule_is_accepted():
    assert _bound_problems(["requests>=2.30.0,<2.40"], requests="2.34.2") == []


def test_bounds_locked_version_outside_range_is_rejected():
    problems = _bound_problems(["requests>=2.30.0,<2.34"], requests="2.34.2")
    assert len(problems) == 1
    assert "requests" in problems[0]
    assert "2.34.2" in problems[0]
    assert "outside" in problems[0]


def test_bounds_every_locked_fork_must_satisfy_the_rule():
    problems = _bound_problems(["foo>=1.0,<2"], foo=["1.2.0", "2.0.1"])
    assert len(problems) == 1
    assert "foo" in problems[0]
    assert "2.0.1" in problems[0]


def test_bounds_marker_entry_is_checked_like_any_other():
    dep = "jsonnet>=0.22.0,<0.23; sys_platform == 'linux'"
    assert _bound_problems([dep], jsonnet="0.22.0") == []
    problems = _bound_problems(["jsonnet>=0.22.0; sys_platform == 'linux'"], jsonnet="0.22.0")
    assert len(problems) == 1
    assert "jsonnet" in problems[0]


def test_bounds_dependency_missing_from_lock_is_reported():
    problems = _bound_problems(["requests>=2.30.0,<3"])
    assert len(problems) == 1
    assert "requests" in problems[0]
    assert "uv.lock" in problems[0]


def test_bounds_hold_for_the_live_checkout():
    mod = _load()
    deps = mod.parse_dependencies((CHECKOUT_ROOT / "pyproject.toml").read_text())
    locked = mod.parse_locked((CHECKOUT_ROOT / "uv.lock").read_text())
    assert mod.bound_problems(deps, locked) == []


def test_main_exits_1_on_a_missing_bound(tmp_path, capsys):
    mod = _load()
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "otto-sh"\ndependencies = ["rich>=15.0.0"]\n')
    (tmp_path / "uv.lock").write_text(_lock(rich="15.0.0"))
    doc = tmp_path / "installation.md"
    doc.write_text(
        "### Direct runtime dependencies\n\n| Package | Min version | Below | Purpose |\n"
        "| --- | --- | --- | --- |\n| `rich` | 15.0.0 | 16 | x |\n"
    )
    assert mod.main([str(py), str(doc)]) == 1
    assert "rich: no upper bound" in capsys.readouterr().out


def test_floors_ignore_the_upper_bound():
    mod = _load()
    deps = mod.parse_dependencies(
        '[project]\nname = "x"\n'
        'dependencies = ["a>=1.2,<2", "b>=0.3.1,<0.4; sys_platform == \'linux\'"]\n'
    )
    assert {k: d.floor for k, d in deps.items()} == {"a": "1.2", "b": "0.3.1"}


def test_names_are_compared_canonically_against_lock_and_table():
    mod = _load()
    py = '[project]\nname = "x"\ndependencies = ["Foo_Bar>=1.0,<2"]\n'
    deps = mod.parse_dependencies(py)
    assert mod.bound_problems(deps, mod.parse_locked(_lock(**{"foo-bar": "1.2.0"}))) == []
    table = mod.parse_table(
        "### Direct runtime dependencies\n\n| Package | Min version | Below | Purpose |\n"
        "| --- | --- | --- | --- |\n| `foo-bar` | 1.0 | 2 | x |\n"
    )
    assert mod.diff(deps, table) == []


@pytest.mark.parametrize("entry", ["foo>=1.0,~=1.2", "foo>=1.0,==1.2.0", "foo>=1.0,<=1.9"])
def test_bounds_another_spelling_of_the_upper_bound_is_rejected_by_name(entry):
    problems = _bound_problems([entry], foo="1.2.0")
    assert len(problems) == 1
    assert "foo" in problems[0]
    assert "write the upper bound as `<X`" in problems[0]
    assert "no upper bound" not in problems[0]
