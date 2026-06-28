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
        "hosts.schema.json",
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
    assert "hosts.schema.json" in result.output
