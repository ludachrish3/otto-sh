import importlib.util

from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "check_docs_dependency_table.py"


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


def test_live_repo_is_in_sync():
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
