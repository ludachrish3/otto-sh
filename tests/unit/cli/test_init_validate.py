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


def _reference_first_host(root: Path, key: str, *, drop: tuple[str, ...] = ("ip",)) -> None:
    """Point the scaffold's first host at *key*, dropping whatever the record will supply.

    *drop* defaults to just ``"ip"`` — the ordinary case, where the
    inventory's ``supplies`` is ``["ip"]`` and every other field
    (``creds`` included) stays inline. A test that also configures
    ``creds_file`` must pass ``drop=("ip", "creds")``: a ``creds_file``
    makes the (effective, ``CredsOverlay``-widened) supplies include
    ``"creds"`` too, and an inline ``creds`` left behind collides with it
    (``'creds' is inventory-owned``), failing the "lab" area for a reason
    unrelated to whatever the test is actually checking.
    """
    # lab_data/lab.json is the scaffold's lab path (see _lab_files(root)).
    lab_file = root / "lab_data" / "lab.json"
    doc = json.loads(lab_file.read_text())
    host = doc["elements"][0]["hosts"][0]
    for field in drop:
        host.pop(field, None)
    host["inventory"] = key
    lab_file.write_text(json.dumps(doc))


def _declare_inventory(root: Path, records: dict) -> Path:
    inv = root / "inventory.json"
    inv.write_text(json.dumps(records))
    settings = root / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: appending [inventory] post-scaffold
        f.write(f'\n[inventory]\nbackend = "json"\npath = "{inv}"\nsupplies = ["ip"]\n')
    return inv


def test_dead_reference_is_a_problem_naming_key_and_label(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _reference_first_host(tmp_path, "ghost")
    _declare_inventory(tmp_path, {"real": {"ip": "10.0.0.1"}})
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "hosts[0]" in result.output
    assert "key 'ghost' not found in inventory 'json:" in result.output


def test_referenced_entry_with_no_inventory_is_a_problem_naming_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _reference_first_host(tmp_path, "k")
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "no inventory is configured" in result.output
    assert "~/.otto/settings.toml" in result.output


def test_orphan_records_warn_and_the_label_is_printed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _reference_first_host(tmp_path, "k")
    inv = _declare_inventory(tmp_path, {"k": {"ip": "10.0.0.1"}, "spare": {"ip": "10.0.0.2"}})
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"inventory: json:{inv}" in result.output
    assert "Warnings" in result.output
    assert "1 record(s) referenced by no lab file here: spare" in result.output


def test_world_readable_creds_file_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    # drop=("ip", "creds"): once creds_file is configured below, the
    # inventory's effective supplies include "creds" too — the scaffold's
    # inline creds would otherwise collide with it (see _reference_first_host).
    _reference_first_host(tmp_path, "k", drop=("ip", "creds"))
    _declare_inventory(tmp_path, {"k": {"ip": "10.0.0.1"}})
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    creds.chmod(0o644)
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: appending creds_file post-scaffold
        f.write(f'creds_file = "{creds}"\n')
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "creds_file" in result.output
    assert "0644" in result.output
    assert "make it 0600" in result.output


def test_broken_user_inventory_settings_is_a_problem_not_a_traceback(tmp_path, monkeypatch):
    """A broken ``~/.otto/settings.toml`` is a named problem row — the doctor never tracebacks.

    Nothing in this repo's own lab files references an inventory key; the
    user file is broken regardless (bad TOML), which is exactly the case
    :func:`otto.config.user_settings.load_user_settings` documents as "a
    configuration error, not 'no inventory'" — it must surface here the same
    way, not crash the whole command.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    user_settings = home / "settings.toml"
    user_settings.write_text("not valid toml [[[")  # sutrepo-exempt: malformed TOML under test
    result = _invoke(["--path", str(tmp_path)])
    # A real crash would leave result.exception something other than the
    # typer.Exit(code=1) the doctor raises deliberately once a row fails.
    assert isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == 1
    assert "inventory:" in result.output
    assert str(user_settings) in result.output


def test_a_null_inventory_key_entry_is_still_validated_when_the_declaration_is_broken(
    tmp_path, monkeypatch
):
    """R7: a ``null`` ``inventory`` key references nothing — its OWN problems are never swallowed.

    The old skip keyed on mere key PRESENCE (``"inventory" in host_data``),
    so an entry carrying ``"inventory": null`` (which means "no reference" —
    the same rule :func:`~otto.inventory.resolve_host_entry` applies) got
    skipped right alongside genuinely-referencing entries whenever the
    declaration was broken, silently hiding an unrelated bad ``os_type`` on
    that same entry. The fix keys the skip on
    :func:`~otto.inventory.doctor.references_inventory` instead, which is
    ``False`` for ``null`` — this entry must be validated regardless of the
    (unrelated) broken declaration.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML under test
        "not valid toml [[["
    )
    lab_file = tmp_path / "lab_data" / "lab.json"
    doc = json.loads(lab_file.read_text())
    host = doc["elements"][0]["hosts"][0]
    host["inventory"] = None
    host["os_type"] = "not-a-real-os-type"
    lab_file.write_text(json.dumps(doc))
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "not-a-real-os-type" in result.output
    assert "is not a registered profile" in result.output


def test_a_referencing_host_is_not_double_reported_when_the_declaration_itself_is_broken(
    tmp_path, monkeypatch
):
    """A host referencing a key is skipped for THIS pass once its own inventory is broken.

    Without the skip, ``resolve_host_entry`` sees a ``None`` inventory (the
    broken build never completed) and raises the "no inventory is configured"
    message — true of an ABSENT declaration, false and misleading here, where
    one exists but fails to compile. The one true problem (the broken
    declaration) must appear alone.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _reference_first_host(tmp_path, "k")
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: declaring a broken [inventory] post-scaffold
        f.write('\n[inventory]\nbackend = "json"\n')  # missing the required 'path'
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "requires a 'path' string" in result.output
    assert "no inventory is configured" not in result.output


def _age_the_snapshot(home: Path, hours: int) -> None:
    """Rewind the snapshot meta's ``fetched_at`` so the next resolution is past the TTL.

    Only ``fetched_at`` moves, so the meta still DESCRIBES the snapshot beside
    it and the cache reads the records back normally — the one thing that
    changes is that they are old.
    """
    metas = sorted((home / "inventory-cache").glob("*.meta.json"))
    # Loud rather than vacuous: with no meta the aging silently does nothing
    # and the staleness assertions below turn into "the backend answered".
    assert len(metas) == 1, f"expected exactly one snapshot meta under {home}, got {metas}"
    meta = json.loads(metas[0].read_text())
    from datetime import datetime, timedelta

    meta["fetched_at"] = (
        datetime.fromisoformat(meta["fetched_at"]) - timedelta(hours=hours)
    ).isoformat()
    metas[0].write_text(json.dumps(meta))


def test_the_doctor_reports_a_snapshot_it_served_because_the_backend_was_down(
    tmp_path, monkeypatch
):
    """``otto init`` REPORTS staleness itself — the cache's warning fires once a process.

    ``_warn_stale`` is deduped per snapshot, and the resolution that spends it
    can be ``entry()``'s completion-cache write, before any console handler
    exists — the same class the ``otto inventory`` verbs report for, one
    surface over. Spec §19.2 pitches ``otto init`` as the dead-reference gate
    to run in CI, and without this it prints a green table against a snapshot
    days old.
    """
    from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device

    # Wider than the autouse 600: the notice quotes the backend's own
    # connection error, which is long, and a fold would split the age and the
    # remedy off the end of the line every assertion below reads.
    monkeypatch.setenv("COLUMNS", "3000")
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    with NetBoxStub([device(1, "nb1")]) as stub:
        with settings.open("a") as f:  # sutrepo-exempt: declaring [inventory] post-scaffold
            f.write(f'\n[inventory]\nbackend = "netbox"\nurl = "{stub.base}"\ncache_ttl = "24h"\n')
        primed = _invoke(["--path", str(tmp_path)])
        assert primed.exit_code == 0, primed.output
        assert "unreachable" not in primed.output, "a live fetch must not report staleness"
    _age_the_snapshot(home, hours=31)

    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output  # advisory: staleness never fails the doctor
    assert "unreachable" in result.output
    assert "31h old" in result.output
    assert "otto inventory refresh" in result.output


def test_a_file_that_fails_to_list_records_warns_rather_than_crashing(tmp_path, monkeypatch):
    """``orphan_warning`` needs ``list_keys()``, which does I/O the first time — it can fail too.

    Nothing here references the inventory, so the "lab" area itself stays
    green; the broken file only bites the warnings pass that tries to list
    its records for the orphan check, and that must degrade to a warning.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    inv = tmp_path / "inventory.json"
    inv.write_text("{not valid json")
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: declaring [inventory] post-scaffold
        f.write(f'\n[inventory]\nbackend = "json"\npath = "{inv}"\n')
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Warnings" in result.output
    assert "could not list records" in result.output


def test_inventory_for_memoises_a_resolved_inventory_per_root(tmp_path, monkeypatch):
    """The SAME cache, asked twice for the same root, returns the SAME object.

    ``otto init`` asks ``_inventory_for`` up to three times per run (the
    "lab" area's own validation, the warnings pass, the label line); without
    memoisation each ask reconstructs the backend. Identity (``is``), not
    just equality, is the proof it was not rebuilt — a fresh
    ``JsonInventory`` would be a fresh object even with the same contents.
    """
    from otto.cli.init import _inventory_for

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _declare_inventory(tmp_path, {"k": {"ip": "10.0.0.1"}})
    cache: dict = {}
    first = _inventory_for(tmp_path, cache)
    second = _inventory_for(tmp_path, cache)
    assert first is not None
    assert first is second
    assert cache[tmp_path] is first


def test_inventory_for_memoises_a_broken_declaration_too(tmp_path, monkeypatch):
    """A broken declaration's EXCEPTION is cached and replayed, not re-parsed on every ask."""
    from otto.cli.init import _inventory_for

    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML under test
        "not valid toml [[["
    )
    cache: dict = {}
    with pytest.raises(ValueError, match=r"Expected '=' after a key") as first:
        _inventory_for(tmp_path, cache)
    with pytest.raises(ValueError, match=r"Expected '=' after a key") as second:
        _inventory_for(tmp_path, cache)
    assert first.value is second.value
    assert cache[tmp_path] is first.value


def test_a_full_otto_init_run_constructs_the_inventory_only_once(tmp_path, monkeypatch):
    """End-to-end proof of the cache: one real ``otto init`` run builds the inventory ONCE.

    A run asks ``_inventory_for`` up to three times (the "lab" area's own
    validation — routed through ``inventory_cache`` specially, since it is
    the one area whose validate hook is not the uniform
    ``Area.validate(root)`` — the warnings pass, and the label line); this
    checks the actual construction call, not just ``_inventory_for``'s own
    cache, so it also proves the "lab" area's special routing is live: if
    that routing regressed back to the uniform ``area.validate(root)`` (which
    passes no cache), this would count the construction TWICE.
    """
    import otto.inventory as otto_inventory

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _reference_first_host(tmp_path, "k")
    _declare_inventory(tmp_path, {"k": {"ip": "10.0.0.1"}})

    real = otto_inventory.build_inventory_from_declarations
    calls: list[int] = []

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(otto_inventory, "build_inventory_from_declarations", _counting)
    result = _invoke(["--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
