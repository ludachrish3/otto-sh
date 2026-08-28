"""otto init validates existing areas via real ingestion code — never rewrites."""

import json
from pathlib import Path

import pytest

from otto.cli.init import AREAS, InitConfig, init_command
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
    """
    monkeypatch.setenv("COLUMNS", "300")


def _scaffold_all(tmp_path: Path) -> None:
    result = _invoke(["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


_EXTRA_HOST = {"ip": "192.0.2.2", "creds": [{"login": "admin", "password": "CHANGE_ME"}]}


def _glob_one_source(tmp_path: Path) -> Path:
    """Point the scaffolded repo's ONE json source at every .json in lab_data/.

    A multi-file source is what the ``paths`` globs make ordinary, and it is
    the only shape in which an in-source duplicate can exist at all.
    """
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: retargeting a product-scaffolded source
        settings.read_text().replace('paths = ["lab_data"]', 'paths = ["lab_data/*.json"]')
    )
    return tmp_path / "lab_data"


def test_duplicate_lab_declaration_across_files_of_one_source_fails(tmp_path: Path) -> None:
    """The doctor must refuse what the loader refuses — one source, one declaration.

    Otherwise `otto init` prints ✓ and exits 0 on a repo where
    `otto --lab example_lab` dies at load, which is exactly the drift routing
    the doctor through the loader's own code exists to prevent.
    """
    _scaffold_all(tmp_path)
    lab_dir = _glob_one_source(tmp_path)
    (lab_dir / "more.json").write_text(json.dumps({"labs": {"example_lab": {}}}))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "declared in both" in result.output
    assert "more.json" in result.output


def test_duplicate_element_across_files_of_one_source_fails(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    lab_dir = _glob_one_source(tmp_path)
    (lab_dir / "more.json").write_text(
        json.dumps(
            {
                "elements": [
                    {"name": "example-device", "labs": ["example_lab"], "hosts": [_EXTRA_HOST]}
                ]
            }
        )
    )
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "duplicate element" in result.output


def test_two_sources_may_each_declare_the_same_lab(tmp_path: Path) -> None:
    """Across SOURCES a re-declaration is the documented override seam, not a typo.

    The duplicate rule is per source; scoping it to the flat file list instead
    would fail the very layering `[[lab.sources]]` exists for.
    """
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: adding a second source to a scaffolded repo
        settings.read_text() + '\n[[lab.sources]]\nbackend = "json"\npaths = ["lab_data_2"]\n'
    )
    second = tmp_path / "lab_data_2" / "lab.json"
    second.parent.mkdir()
    second.write_text(
        json.dumps(
            {
                "labs": {"example_lab": {"resources": ["other-device"]}},
                "elements": [
                    {"name": "other-device", "labs": ["example_lab"], "hosts": [_EXTRA_HOST]}
                ],
            }
        )
    )
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_a_problem_renders_a_swallowed_markup_tail_verbatim(tmp_path: Path) -> None:
    """The verdict table quotes pydantic and the author's regexes — both tag-shaped.

    `[type=extra_forbidden, …]` is valid rich markup, so an unescaped cell
    drops the half of a validation error that says WHY it failed.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"ip"', '"ipp"'))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "type=extra_forbidden" in result.output


def test_valid_repo_reports_all_ok_and_exits_zero(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.count("✓") >= 4


def test_broken_settings_key_fails_with_pydantic_error(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: in-place corruption of a product-scaffolded file
        settings.read_text().replace("version =", "verzion =")
    )
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "verzion" in result.output


def test_invalid_host_field_fails_named(tmp_path: Path) -> None:
    """A bad host field is located by ELEMENT and index, not by a flat host number.

    v2 has no top-level host array to index into, so "hosts[3]" alone would
    no longer tell the author which entry to open.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"ip"', '"ipp"'))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "ipp" in result.output
    assert "element 'example-device' hosts[0]" in result.output


def test_non_dict_host_entry_fails_named(tmp_path: Path) -> None:
    """A non-object hosts[] entry gets a clean located error, not an AttributeError.

    In v2 the host entries live inside an element, so the rejection comes from
    ``ElementSpec`` — the same model the loader validates with — and the
    problem names both the element index and pydantic's own field location.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["hosts"].append("oops")
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "elements[0]" in result.output
    assert "hosts.1" in result.output
    assert "Input should be a valid dictionary" in result.output


def test_v1_lab_file_reports_the_migration_hint(tmp_path: Path) -> None:
    """A repo still on the top-level ``hosts`` array is told where hosts moved.

    The hard cutover (spec §11) means the doctor must not shrug at a v1 file:
    it is the surface a user upgrading otto meets first, and it reports the
    loader's own migration hint rather than a generic parse failure.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(json.dumps({"hosts": []}))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "'hosts'" in result.output
    assert "'elements'" in result.output


def test_warnings_do_not_fail_the_doctor(tmp_path: Path) -> None:
    """A dead membership pattern is advice, printed, and never an exit-1 problem.

    A shared lab file may legitimately serve projects that declare different
    labs (spec §9), so this can only ever be a warning.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["labs"].append("never_declared")
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Warnings" in result.output
    assert "matches no declared lab" in result.output
    assert "never_declared" in result.output


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


def test_invalid_link_entry_fails_named(tmp_path: Path) -> None:
    """A structurally invalid links[] entry surfaces a named validation error."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    # LinkSpec requires exactly two endpoints; one endpoint fails validation.
    data["links"].append({"endpoints": [{"host": "example-device"}]})
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "links[0]" in result.output


def test_valid_link_entry_passes(tmp_path: Path) -> None:
    """A well-formed links[] entry validates clean alongside the example host."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["links"].append({"endpoints": [{"host": "example-device"}, {"host": "other-device"}]})
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_unknown_top_level_section_fails(tmp_path: Path) -> None:
    """The doctor rejects an unknown top-level lab.json section, exactly as the
    runtime loader does — it reuses the loader's section validator, so it cannot
    drift from what otto actually accepts.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["routes"] = []  # not a known section
    lab_file.write_text(json.dumps(data))
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown section" in result.output
    assert "routes" in result.output


def test_missing_libs_dir_reported(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    # Remove only the module's __init__.py, NOT the whole pylib/ tree: the
    # instructions area's `detect` considers the module dir's mere existence
    # sufficient (so re-running --all would silently heal a fully-removed
    # pylib/ as "missing" rather than reporting it broken — see
    # _detect_instructions). Deleting just __init__.py keeps `detect` truthy
    # (module dir still exists) so this routes to `validate`, which does
    # require __init__.py and reports the gap under the "pylib" path.
    (tmp_path / "pylib" / "widget_instructions" / "__init__.py").unlink()
    result = _invoke(["--all", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "pylib" in result.output


def test_parse_lab_sections_tolerates_dollar_schema() -> None:
    from otto.labs.errors import LabRepositoryError
    from otto.labs.json_repository import parse_lab_sections

    data = {
        "$schema": "../.otto/schemas/lab.schema.json",
        "labs": {},
        "elements": [],
        "links": [],
    }
    assert parse_lab_sections(data, "lab.json")["elements"] == []
    with pytest.raises(LabRepositoryError, match="unknown section"):
        parse_lab_sections({"routes": []}, "lab.json")


def test_parse_lab_sections_mixed_key_types_still_raise_lab_error() -> None:
    """Non-string keys sort into the error, not a TypeError out of ``sorted``."""
    from otto.labs.errors import LabRepositoryError
    from otto.labs.json_repository import parse_lab_sections

    # Mixed str/int unknown keys: sorting them raw is a TypeError, so the
    # unknown-section set must be normalised to str before it is sorted.
    with pytest.raises(LabRepositoryError, match="unknown section"):
        parse_lab_sections({5: [], "routes": []}, "lab.json")


def test_schemas_validate_green_after_scaffold_and_reformat(tmp_path: Path) -> None:
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    assert by_name["schemas"].validate(tmp_path) == []
    # reformat-only change stays green: comparison is structural, not bytes
    lab = tmp_path / ".otto" / "schemas" / "lab.schema.json"
    lab.write_text(json.dumps(json.loads(lab.read_text()), indent=4, sort_keys=True))
    assert by_name["schemas"].validate(tmp_path) == []


def test_schemas_validate_flags_stale_missing_orphaned(tmp_path: Path) -> None:
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    out = tmp_path / ".otto" / "schemas"
    stale = json.loads((out / "lab.schema.json").read_text())
    stale["title"] = "tampered"
    (out / "lab.schema.json").write_text(json.dumps(stale))
    (out / "settings.schema.json").unlink()
    (out / "ghost.schema.json").write_text("{}")
    problems = "\n".join(by_name["schemas"].validate(tmp_path))
    assert "lab.schema.json" in problems
    assert "stale" in problems
    assert "settings.schema.json" in problems
    assert "missing" in problems
    assert "ghost.schema.json" in problems
    assert "orphaned" in problems
    assert "otto schema export" in problems  # remedy named


def _tamper_lab_schema(tmp_path: Path, mutate) -> str:
    """Scaffold the schemas area, mutate ``lab.schema.json``, return the problems."""
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    lab = tmp_path / ".otto" / "schemas" / "lab.schema.json"
    doc = json.loads(lab.read_text())
    mutate(doc)
    lab.write_text(json.dumps(doc))
    return "\n".join(by_name["schemas"].validate(tmp_path))


def test_schemas_validate_names_both_versions_on_a_stamp_mismatch(tmp_path: Path) -> None:
    """An upgraded otto reports WHICH otto wrote the file, not a bare "stale"."""
    from otto.version import get_version

    def older(doc: dict) -> None:
        doc["x-otto-version"] = "0.0.1"

    problems = _tamper_lab_schema(tmp_path, older)
    assert "generated by otto 0.0.1" in problems
    assert f"installed otto is {get_version()}" in problems
    assert "stale" not in problems  # the version answer replaces the vague one
    assert "otto schema export" in problems  # remedy still named


def test_schemas_validate_reports_an_unstamped_schema(tmp_path: Path) -> None:
    """A schema from before the stamp existed has no version to name."""
    problems = _tamper_lab_schema(tmp_path, lambda doc: doc.pop("x-otto-version"))
    assert "generated by otto <unstamped>" in problems


def test_schemas_validate_flags_unparsable(tmp_path: Path) -> None:
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    out = tmp_path / ".otto" / "schemas"
    # Corrupt an expected schema file with invalid JSON
    (out / "lab.schema.json").write_text("{not json")
    problems = "\n".join(by_name["schemas"].validate(tmp_path))
    assert "lab.schema.json" in problems
    assert "unparsable" in problems
    assert "otto schema export" in problems  # remedy named
