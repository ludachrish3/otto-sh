"""`otto schema export` writes the generated schema files."""

import json

from typer.testing import CliRunner

from otto.cli.schema import schema_app

runner = CliRunner()


def test_export_writes_all_documents(tmp_path):
    out = tmp_path / "schemas"
    result = runner.invoke(schema_app, ["export", "--out", str(out)])
    assert result.exit_code == 0, result.output

    written = {p.name for p in out.glob("*.schema.json")}
    assert {
        "unix-host.schema.json",
        "embedded-host.schema.json",
        "lab.schema.json",
        "link.schema.json",
        "settings.schema.json",
        "reservations.schema.json",
    } <= written

    # Every emitted file is valid JSON carrying the schema dialect.
    for path in out.glob("*.schema.json"):
        doc = json.loads(path.read_text())
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_export_reports_what_it_wrote(tmp_path):
    out = tmp_path / "schemas"
    result = runner.invoke(schema_app, ["export", "--out", str(out), "--builtins-only"])
    assert result.exit_code == 0
    assert "lab.schema.json" in result.output


def test_export_defaults_to_dot_otto_schemas(tmp_path, monkeypatch):
    """Without --out, otto schema export writes to .otto/schemas (shared with otto init)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(schema_app, ["export"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "schemas" / "settings.schema.json").is_file()
    assert (tmp_path / ".otto" / "schemas" / "lab.schema.json").is_file()


def test_export_out_flag_still_honored(tmp_path):
    """The --out flag continues to work when explicitly provided."""
    out = tmp_path / "elsewhere"
    result = runner.invoke(schema_app, ["export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "settings.schema.json").is_file()
