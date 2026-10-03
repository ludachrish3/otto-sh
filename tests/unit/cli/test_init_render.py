"""``otto init`` renders the doctor's findings verbatim through the rich console.

The findings themselves are pinned against the library
(``tests/unit/init/test_init_doctor.py``); what is left here is the leaf's
rendering: rich markup escaped, the warnings block printed only when there is
something to warn about, and the inventory/creds label rows.
"""

import json
from pathlib import Path

import pytest

from otto.cli.init import init_command
from tests._fixtures.dispatch import DispatchRunner

# See tests/unit/cli/test_init_prompts.py: init_command dispatches as a
# flattened single-command app under the production bridge.
runner = DispatchRunner()


def _invoke(args, **kwargs):
    return runner.invoke(init_command, args, spec_name="init", **kwargs)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the rich console width so table cells never fold inside asserted text.

    The report table's ``detail`` column uses ``overflow="fold"``; under
    CliRunner (non-tty) rich resolves its width from the ``COLUMNS`` env var,
    defaulting to 80. The fold point then depends on the length of the
    tmp-path rendered in the same cell — long CI basetemp paths shifted it
    into the middle of ``"must be a JSON object"`` and broke the substring
    assertions (GH issue #89). A fixed, generous width makes rendering
    deterministic everywhere.

    Bumped from 300 to 600: an inventory finding embeds an absolute path
    (sometimes twice — once for the lab file, once for the ``json:<path>``
    label) inside a ``pytest``-generated ``tmp_path``, whose basename already
    includes the test's own (sometimes long) name — long enough on this
    test's name that 300 columns still folded ``"...not found in inventory"``
    onto its own line, splitting it from the ``'json:...'`` that followed and
    breaking the substring assertion the same way GH #89 did.
    """
    monkeypatch.setenv("COLUMNS", "600")


def _scaffold_all(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_a_problem_renders_a_swallowed_markup_tail_verbatim(tmp_path: Path) -> None:
    """The verdict table quotes pydantic and the author's regexes — both tag-shaped.

    `[type=extra_forbidden, …]` is valid rich markup, so an unescaped cell
    drops the half of a validation error that says WHY it failed.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"os_type"', '"os_typo"'))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "type=extra_forbidden" in result.output


def test_a_warning_renders_a_tag_shaped_regex_verbatim(tmp_path: Path) -> None:
    """A character class must survive the rich console — it IS the message.

    `[a-z]` is valid rich markup, so an unescaped warning would print the
    pattern as `'nope+'` and send the author looking for a pattern they never
    wrote.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["labs"].append("nope[a-z]+")
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "'nope[a-z]+'" in result.output


def test_a_clean_repo_prints_no_warnings_block(tmp_path: Path) -> None:
    """The scaffold itself must be warning-free, or the block is just noise."""
    _scaffold_all(tmp_path)
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Warnings" not in result.output


def test_orphan_creds_warn_and_the_store_label_is_printed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    creds = tmp_path / "lab_data" / "creds.json"
    creds.write_text(
        json.dumps({"device-01.lab.example": [{"login": "u", "password": "p"}], "stale": []})
    )
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"inventory: json:{tmp_path / 'lab_data' / 'inventory.json'}" in result.output
    assert f"creds:     json:{tmp_path / 'lab_data' / 'creds.json'}" in result.output
    assert "1 key(s) the inventory does not hold: stale" in result.output


def test_statuses_render_scaffolded_not_present_and_blocked(tmp_path: Path) -> None:
    result = _invoke(["--lab", "--name", "widget", "--path", str(tmp_path)])
    assert "scaffolded" in result.output
    assert "not present" in result.output
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: a settings file that does not compile IS the subject
        'name = "w\n'
    )
    result = _invoke(["--lab", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert "settings did not compile" in result.output


def test_a_refreshed_schemas_area_lists_the_orphan_it_pruned(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    orphan = tmp_path / ".otto" / "schemas" / "gone.schema.json"
    orphan.write_text("{}")
    result = _invoke(["--schemas", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "pruned .otto/schemas/gone.schema.json" in result.output
    assert not orphan.exists()
