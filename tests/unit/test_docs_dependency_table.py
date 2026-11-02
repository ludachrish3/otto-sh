import importlib.util

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
    "aioftp>=0.27.2",
    "pydantic>=2.6,<3",
    "typer>=0.26",
]
"""

TABLE_DOC = """\
## Dependencies reference

### Direct runtime dependencies

| Package | Min version | Purpose |
| ------- | ----------- | ------- |
| `aioftp` | 0.27.2 | FTP |
| `pydantic` | 2.6 | Validation |
| `typer` | 0.26 | CLI |

### Native-extension transitive dependencies

| Package | Pulled in by | Notes |
| ------- | ------------ | ----- |
| `cffi` | cryptography | not part of the direct table |
"""


def test_parse_pyproject_extracts_floors():
    mod = _load()
    assert mod.parse_pyproject(PYPROJECT) == {
        "aioftp": "0.27.2",
        "pydantic": "2.6",
        "typer": "0.26",
    }


def test_parse_table_scoped_to_direct_section():
    mod = _load()
    parsed = mod.parse_table(TABLE_DOC)
    assert parsed == {"aioftp": "0.27.2", "pydantic": "2.6", "typer": "0.26"}
    assert "cffi" not in parsed


def test_in_sync_passes():
    mod = _load()
    assert mod.diff(mod.parse_pyproject(PYPROJECT), mod.parse_table(TABLE_DOC)) == []


def test_missing_table_row_is_drift():
    mod = _load()
    doc = TABLE_DOC.replace("| `typer` | 0.26 | CLI |\n", "")
    problems = mod.diff(mod.parse_pyproject(PYPROJECT), mod.parse_table(doc))
    assert any("typer" in p and "missing" in p for p in problems)


def test_stale_min_version_is_drift():
    mod = _load()
    doc = TABLE_DOC.replace("| `pydantic` | 2.6 |", "| `pydantic` | 2.5 |")
    problems = mod.diff(mod.parse_pyproject(PYPROJECT), mod.parse_table(doc))
    assert any("pydantic" in p for p in problems)


def test_extra_table_row_is_drift():
    mod = _load()
    doc = TABLE_DOC.replace(
        "| `typer` | 0.26 | CLI |", "| `typer` | 0.26 | CLI |\n| `left-pad` | 1.0 | ? |"
    )
    problems = mod.diff(mod.parse_pyproject(PYPROJECT), mod.parse_table(doc))
    assert any("left-pad" in p for p in problems)


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
