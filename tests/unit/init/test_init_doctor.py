"""The ``otto init`` doctor checks existing areas via real ingestion code — never rewrites.

Every check runs against :func:`otto.init.check_project` directly; how the leaf
renders a :class:`~otto.init.DoctorReport` is pinned in
``tests/unit/cli/test_init_render.py``.
"""

import json
import shutil
from pathlib import Path

import pytest

from otto.init import (
    AREA_NAMES,
    AreaVerdict,
    DoctorReport,
    InitConfig,
    InitInputError,
    ScaffoldReport,
    check_project,
    detect_areas,
    scaffold,
    scaffold_candidates,
)
from tests._fixtures.project_scaffold import make_project
from tests.unit.templating import conftest as _templating

# The templating conftest's fixtures, for the templated tests below: the binding
# proof every templated test file carries, and a private OTTO_HOME.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test
otto_home = _templating.otto_home


def _scaffold(root: Path, *areas: str) -> ScaffoldReport:
    return scaffold(InitConfig(root, "widget", "0.1.0"), areas=list(areas))


def _scaffold_all(tmp_path: Path) -> None:
    """Scaffold every area an all-areas run offers, as ``otto init --all`` does."""
    scaffold(
        InitConfig(tmp_path, "widget", "0.1.0"), areas=scaffold_candidates(tmp_path, all_areas=True)
    )


def _text(report: DoctorReport) -> str:
    """Every line the CLI would render from *report*: problems, details, warnings, labels."""
    lines: list[str] = []
    for verdict in report.verdicts:
        lines.extend(verdict.problems)
        if verdict.detail:
            lines.append(verdict.detail)
    lines.extend(report.warnings)
    lines.extend(label for label in (report.inventory_label, report.creds_label) if label)
    return "\n".join(lines)


_EXTRA_HOST = {"ip": "192.0.2.2", "creds": [{"login": "admin", "password": "CHANGE_ME"}]}


def _glob_one_source(tmp_path: Path) -> Path:
    """Point the scaffolded project's ONE json source at every lab file in lab_data/.

    A multi-file source is what the ``paths`` globs make ordinary, and it is
    the only shape in which an in-source duplicate can exist at all. A split
    lab globs its LAB files, never every JSON beside them: inventory.json and
    creds.json live in the same directory, and a bare ``*.json`` would feed
    them to the lab parser as "unknown section(s)" — a real problem, but not
    the one these tests are pinning.
    """
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: retargeting a product-scaffolded source
        settings.read_text().replace('paths = ["lab_data"]', 'paths = ["lab_data/lab*.json"]')
    )
    return tmp_path / "lab_data"


def test_duplicate_lab_declaration_across_files_of_one_source_fails(tmp_path: Path) -> None:
    """The doctor must refuse what the loader refuses — one source, one declaration.

    Otherwise `otto init` reports ✓ on a project where `otto --lab example_lab`
    dies at load, which is exactly the drift routing the doctor through the
    loader's own code exists to prevent.
    """
    _scaffold_all(tmp_path)
    lab_dir = _glob_one_source(tmp_path)
    (lab_dir / "lab_more.json").write_text(json.dumps({"labs": {"example_lab": {}}}))
    report = check_project(tmp_path)
    assert not report.ok
    assert "declared in both" in _text(report)
    assert "lab_more.json" in _text(report)
    # A regression that globs inventory.json/creds.json into the lab parser
    # (they live in the same directory) would surface as this exact problem,
    # named for one of the two siblings — not as the "declared in both" above.
    assert "unknown section" not in _text(report)


def test_duplicate_element_across_files_of_one_source_fails(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    lab_dir = _glob_one_source(tmp_path)
    (lab_dir / "lab_more.json").write_text(
        json.dumps(
            {
                "elements": [
                    {"name": "example-device", "labs": ["example_lab"], "hosts": [_EXTRA_HOST]}
                ]
            }
        )
    )
    report = check_project(tmp_path)
    assert not report.ok
    assert "duplicate element" in _text(report)
    # See test_duplicate_lab_declaration_across_files_of_one_source_fails: a
    # sweep of inventory.json/creds.json into the lab parser is this problem.
    assert "unknown section" not in _text(report)


def test_two_sources_may_each_declare_the_same_lab(tmp_path: Path) -> None:
    """Across SOURCES a re-declaration is the documented override seam, not a typo.

    The duplicate rule is per source; scoping it to the flat file list instead
    would fail the very layering `[[lab.sources]]` exists for.
    """
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: adding a second source to a scaffolded project
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
    report = check_project(tmp_path)
    assert report.ok, _text(report)


def test_a_referenced_hosts_store_password_never_reaches_the_report(tmp_path: Path) -> None:
    """spec 2026-09-06 creds-store §6.1/§7.1: a report is a doctor finding, not a leak.

    ``resolve_host_entry`` appends a referenced host's ``creds`` LAST when
    the entry has none inline (the scaffold's own shape), so pydantic's
    ``str(ValidationError)`` for a model-level failure on the resolved dict
    ends with ``input_value={..., 'password': '<the store's password>'}``.
    A distinctive password proves it never reached the report, rather than
    merely proving the *report* changed shape.
    """
    _scaffold_all(tmp_path)
    creds_file = tmp_path / "lab_data" / "creds.json"
    creds_file.write_text(creds_file.read_text().replace("CHANGE_ME", "SECRET_XYZ"))
    lab_file = tmp_path / "lab_data" / "lab.json"
    # The referenced host stays referenced (no inline "creds") and gains one
    # field no host spec registers — a model-level failure, not a merge one.
    lab_file.write_text(
        lab_file.read_text().replace('"os_type": "unix"', '"os_type": "unix", "usr": "ghost"')
    )
    report = check_project(tmp_path)
    assert not report.ok
    assert "SECRET_XYZ" not in _text(report)
    assert "usr" in _text(report)  # the finding still names the offending field


def test_an_old_labs_user_key_reports_the_migration_without_leaking(tmp_path: Path) -> None:
    """The doctor is where a lab written before the pin was dropped lands.

    Spec 2026-09-13 cred-scope §5.1: the doctor must report the migration that
    names the fix, not a bare unknown-field finding — and must still hide the
    referenced store's password, which the refused dict's ``input_value``
    carries exactly as in the sibling test above.
    """
    _scaffold_all(tmp_path)
    creds_file = tmp_path / "lab_data" / "creds.json"
    creds_file.write_text(creds_file.read_text().replace("CHANGE_ME", "SECRET_XYZ"))
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(
        lab_file.read_text().replace('"os_type": "unix"', '"os_type": "unix", "user": "ghost"')
    )
    report = check_project(tmp_path)
    assert not report.ok
    assert "user was removed" in _text(report)
    assert "SECRET_XYZ" not in _text(report)


def test_valid_project_reports_all_ok(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert [(v.name, v.state) for v in report.verdicts] == [
        ("settings", "ok"),
        ("schemas", "ok"),
        ("lab", "ok"),
        ("tests", "ok"),
        ("instructions", "ok"),
        ("kmodcov", "absent"),
        ("embedded-gcov", "absent"),
    ]
    assert report.warnings == []  # spec 2026-09-06 §8.1: a fresh scaffold warns of nothing


def _scaffold_all_inline(tmp_path: Path) -> None:
    """Scaffold every area an ``otto init --all --inline-hosts`` run offers."""
    scaffold(
        InitConfig(tmp_path, "widget", "0.1.0", inline_hosts=True),
        areas=scaffold_candidates(tmp_path, all_areas=True, inline_hosts=True),
    )


def test_an_inline_hosts_project_is_ok_and_warns_of_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))  # no user settings
    _scaffold_all_inline(tmp_path)
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert [(v.name, v.state) for v in report.verdicts] == [
        ("settings", "ok"),
        ("schemas", "ok"),
        ("lab", "ok"),
        ("tests", "ok"),
        ("instructions", "ok"),
        ("kmodcov", "absent"),
        ("embedded-gcov", "absent"),
    ]
    assert report.warnings == []
    assert report.inventory_label is None
    assert report.creds_label is None


def test_an_inline_hosts_project_inherits_the_user_inventory(tmp_path, monkeypatch) -> None:
    # The kept fallback: commenting the project's tables out leaves the user's
    # ~/.otto/settings.toml inventory in force, so its records show as unreferenced.
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    home.mkdir(parents=True)
    (home / "inventory.json").write_text(json.dumps({"spare": {"ip": "10.0.0.2"}}))
    (home / "settings.toml").write_text(  # scaffold-exempt: user-level settings under test
        '[inventory]\nbackend = "json"\npath = "inventory.json"\n'
    )
    _scaffold_all_inline(tmp_path)
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.inventory_label == f"json:{home / 'inventory.json'}"
    assert "1 record(s) referenced by no lab file here: spare" in _text(report)


def test_broken_settings_key_fails_with_pydantic_error(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: in-place corruption of a product-scaffolded file
        settings.read_text().replace("version =", "verzion =")
    )
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "failed"
    assert "verzion" in _text(report)


def test_invalid_host_field_fails_named(tmp_path: Path) -> None:
    """A bad host field is located by ELEMENT and index, not by a flat host number.

    v2 has no top-level host array to index into, so "hosts[3]" alone would
    no longer tell the author which entry to open.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"os_type"', '"os_typo"'))
    report = check_project(tmp_path)
    assert not report.ok
    assert "os_typo" in _text(report)
    assert "element 'example-device' hosts[0]" in _text(report)


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
    report = check_project(tmp_path)
    assert not report.ok
    assert "elements[0]" in _text(report)
    assert "hosts.1" in _text(report)
    assert "Input should be a valid dictionary" in _text(report)


def test_v1_lab_file_reports_the_migration_hint(tmp_path: Path) -> None:
    """A project still on the top-level ``hosts`` array is told where hosts moved.

    The hard cutover (spec §11) means the doctor must not shrug at a v1 file:
    it is the surface a user upgrading otto meets first, and it reports the
    loader's own migration hint rather than a generic parse failure.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(json.dumps({"hosts": []}))
    report = check_project(tmp_path)
    assert not report.ok
    assert "'hosts'" in _text(report)
    assert "'elements'" in _text(report)


def test_warnings_do_not_fail_the_doctor(tmp_path: Path) -> None:
    """A dead membership pattern is advice, reported, and never a failure.

    A shared lab file may legitimately serve projects that declare different
    labs (spec §9), so this can only ever be a warning.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["labs"].append("never_declared")
    lab_file.write_text(json.dumps(data))
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.warnings
    assert "matches no declared lab" in _text(report)
    assert "never_declared" in _text(report)


def test_invalid_link_entry_fails_named(tmp_path: Path) -> None:
    """A structurally invalid links[] entry surfaces a named validation error."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    # LinkSpec requires exactly two endpoints; one endpoint fails validation.
    data["links"].append({"endpoints": [{"host": "example-device"}]})
    lab_file.write_text(json.dumps(data))
    report = check_project(tmp_path)
    assert not report.ok
    assert "links[0]" in _text(report)


def test_valid_link_entry_passes(tmp_path: Path) -> None:
    """A well-formed links[] entry validates clean alongside the example host."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["links"].append({"endpoints": [{"host": "example-device"}, {"host": "other-device"}]})
    lab_file.write_text(json.dumps(data))
    report = check_project(tmp_path)
    assert report.ok, _text(report)


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
    report = check_project(tmp_path)
    assert not report.ok
    assert "unknown section" in _text(report)
    assert "routes" in _text(report)


def test_missing_libs_dir_reported(tmp_path: Path) -> None:
    """A declared ``libs`` dir that is gone fails the instructions area, naming it.

    The whole ``pylib/`` goes, not just the module's ``__init__.py``: a
    package directory without one is still a namespace package the loader
    imports, so removing only the file leaves nothing wrong to report.
    """
    _scaffold_all(tmp_path)
    shutil.rmtree(tmp_path / "pylib")
    verdict = check_project(tmp_path).verdict(name="instructions")
    assert verdict.state == "failed"
    assert f"libs dir not found: {tmp_path / 'pylib'}" in verdict.problems


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


def _write_schemas(tmp_path: Path) -> Path:
    """Write the schemas area alone — the doctor reads every other area as absent."""
    from otto.models.jsonschema import write_schemas

    out = tmp_path / ".otto" / "schemas"
    write_schemas(out)
    return out


def _schema_problems(tmp_path: Path) -> str:
    return "\n".join(check_project(tmp_path).verdict(name="schemas").problems)


def test_schemas_validate_green_after_scaffold_and_reformat(tmp_path: Path) -> None:
    out = _write_schemas(tmp_path)
    assert check_project(tmp_path).verdict(name="schemas").state == "ok"
    # reformat-only change stays green: comparison is structural, not bytes
    lab = out / "lab.schema.json"
    lab.write_text(json.dumps(json.loads(lab.read_text()), indent=4, sort_keys=True))
    assert check_project(tmp_path).verdict(name="schemas").state == "ok"


def test_schemas_validate_flags_stale_missing_orphaned(tmp_path: Path) -> None:
    out = _write_schemas(tmp_path)
    stale = json.loads((out / "lab.schema.json").read_text())
    stale["title"] = "tampered"
    (out / "lab.schema.json").write_text(json.dumps(stale))
    (out / "settings.schema.json").unlink()
    (out / "ghost.schema.json").write_text("{}")
    verdict = check_project(tmp_path).verdict(name="schemas")
    assert verdict.state == "failed"
    problems = "\n".join(verdict.problems)
    assert "lab.schema.json" in problems
    assert "stale" in problems
    assert "settings.schema.json" in problems
    assert "missing" in problems
    assert "ghost.schema.json" in problems
    assert "orphaned" in problems
    assert "otto schema export" in problems  # remedy named


def _tamper_lab_schema(tmp_path: Path, mutate) -> str:
    """Write the schemas area, mutate ``lab.schema.json``, return the problems."""
    lab = _write_schemas(tmp_path) / "lab.schema.json"
    doc = json.loads(lab.read_text())
    mutate(doc)
    lab.write_text(json.dumps(doc))
    return _schema_problems(tmp_path)


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
    out = _write_schemas(tmp_path)
    # Corrupt an expected schema file with invalid JSON
    (out / "lab.schema.json").write_text("{not json")
    problems = _schema_problems(tmp_path)
    assert "lab.schema.json" in problems
    assert "unparsable" in problems
    assert "otto schema export" in problems  # remedy named


def _point_first_host_at(root: Path, key: str) -> None:
    """Point the scaffold's referenced host at *key* (its facts come from the two sibling files)."""
    lab_file = root / "lab_data" / "lab.json"
    doc = json.loads(lab_file.read_text())
    doc["elements"][0]["hosts"][0]["inventory"] = key
    lab_file.write_text(json.dumps(doc))


def _make_first_host_inline(root: Path) -> None:
    """Replace the scaffold's referenced host with an inline one (no inventory reference).

    For tests whose concern is the inventory/creds files themselves (a broken
    backend, a missing store) rather than host resolution: an inline host
    keeps the "lab" area green regardless of what those files hold.
    """
    lab_file = root / "lab_data" / "lab.json"
    doc = json.loads(lab_file.read_text())
    doc["elements"][0]["hosts"][0] = dict(_EXTRA_HOST)
    lab_file.write_text(json.dumps(doc))


def _set_inventory_records(root: Path, records: dict) -> Path:
    inv = root / "lab_data" / "inventory.json"
    inv.write_text(json.dumps(records))
    return inv


def _set_creds(root: Path, entries: dict) -> Path:
    creds = root / "lab_data" / "creds.json"
    creds.write_text(json.dumps(entries))
    return creds


def _drop_table(root: Path, name: str) -> None:
    """Remove the live ``[name]`` table the scaffold wrote (up to the next header or comment)."""
    settings = root / ".otto" / "settings.toml"
    out, skipping = [], False
    for line in settings.read_text().splitlines(keepends=True):
        if line.startswith(f"[{name}]"):
            skipping = True
            continue
        if skipping and line.startswith(("[", "#")):
            skipping = False
        if not skipping:
            out.append(line)
    settings.write_text("".join(out))  # scaffold-exempt: editing a product-scaffolded settings file


def test_dead_reference_is_a_problem_naming_key_and_label(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _point_first_host_at(tmp_path, "ghost")
    report = check_project(tmp_path)
    assert not report.ok
    assert "hosts[0]" in _text(report)
    assert "key 'ghost' not found in inventory 'json:" in _text(report)


def test_referenced_entry_with_no_inventory_is_a_problem_naming_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _drop_table(tmp_path, "inventory")
    _drop_table(tmp_path, "creds")  # a lone [creds] would be the other error
    report = check_project(tmp_path)
    assert not report.ok
    assert "no inventory is configured" in _text(report)
    assert "~/.otto/settings.toml" in _text(report)


def test_orphan_records_warn_and_the_label_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    inv = _set_inventory_records(
        tmp_path, {"device-01.lab.example": {"ip": "10.0.0.1"}, "spare": {"ip": "10.0.0.2"}}
    )
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.inventory_label == f"json:{inv}"
    assert report.warnings
    assert "1 record(s) referenced by no lab file here: spare" in _text(report)


def test_world_readable_creds_file_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    (tmp_path / "lab_data" / "creds.json").chmod(0o644)
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert "creds store file" in _text(report)
    assert "0644" in _text(report)
    assert "make it 0600" in _text(report)


def test_broken_user_inventory_settings_is_a_problem_not_a_traceback(tmp_path, monkeypatch):
    """A broken ``~/.otto/settings.toml`` is a named problem — the doctor never raises.

    The project already declares its OWN ``[inventory]``/``[creds]`` (the
    scaffold's live tables), but ``load_user_settings`` is asked regardless —
    a broken user file is a configuration error, not "no inventory", which is
    exactly the case :func:`otto.config.user_settings.load_user_settings`
    documents — it must surface here the same way, not crash the whole
    check.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    user_settings = home / "settings.toml"
    user_settings.write_text("not valid toml [[[")  # scaffold-exempt: malformed TOML under test
    report = check_project(tmp_path)
    assert not report.ok
    problems = "\n".join(report.verdict(name="lab").problems)
    assert "inventory:" in problems
    assert str(user_settings) in problems


def test_a_null_inventory_key_entry_is_still_validated_when_the_declaration_is_broken(
    tmp_path, monkeypatch
):
    """A ``null`` ``inventory`` key references nothing — its OWN problems are never swallowed.

    The old skip keyed on mere key PRESENCE (``"inventory" in host_data``),
    so an entry carrying ``"inventory": null`` (which means "no reference" —
    the same rule :func:`~otto.inventory.resolve_host_entry` applies) got
    skipped right alongside genuinely-referencing entries whenever the
    declaration was broken, silently hiding an unrelated bad ``os_type`` on
    that same entry. The fix keys the skip on
    :func:`~otto.inventory.doctor.selected_inventory` instead, which is
    ``None`` for ``null`` — this entry must be validated regardless of the
    (unrelated) broken declaration.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(  # scaffold-exempt: malformed TOML under test
        "not valid toml [[["
    )
    lab_file = tmp_path / "lab_data" / "lab.json"
    doc = json.loads(lab_file.read_text())
    host = doc["elements"][0]["hosts"][0]
    host["inventory"] = None
    host["os_type"] = "not-a-real-os-type"
    lab_file.write_text(json.dumps(doc))
    report = check_project(tmp_path)
    assert not report.ok
    assert "not-a-real-os-type" in _text(report)
    assert "Unknown os_type profile" in _text(report)


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
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: breaking the scaffolded [inventory] on purpose
        settings.read_text().replace('path = "lab_data/inventory.json"\n', "")
    )
    report = check_project(tmp_path)
    assert not report.ok
    assert "parse failed: path: Field required" in _text(report)
    assert "no inventory is configured" not in _text(report)


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
    """The doctor REPORTS staleness itself — the cache's warning fires once a process.

    ``_warn_stale`` is deduped per snapshot, and the resolution that spends it
    can be ``entry()``'s completion-cache write, before any console handler
    exists — the same class the ``otto inventory`` verbs report for, one
    surface over. Spec §19.2 pitches ``otto init`` as the dead-reference gate
    to run in CI, and without this it reports green against a snapshot days
    old.
    """
    from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device

    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    _scaffold_all(tmp_path)
    # The scaffolded host is inline here (no inventory reference): this test
    # is about the netbox backend's OWN staleness reporting, unrelated to
    # whether any host resolves against it.
    _make_first_host_inline(tmp_path)
    _drop_table(tmp_path, "inventory")
    _drop_table(tmp_path, "creds")
    settings = tmp_path / ".otto" / "settings.toml"
    with NetBoxStub([device(1, "nb1")]) as stub:
        with settings.open("a") as f:  # scaffold-exempt: declaring [inventory] post-scaffold
            f.write(f'\n[inventory]\nbackend = "netbox"\nurl = "{stub.base}"\ncache_ttl = "24h"\n')
        primed = check_project(tmp_path)
        assert primed.ok, _text(primed)
        assert "unreachable" not in _text(primed), "a live fetch must not report staleness"
    _age_the_snapshot(home, hours=31)

    report = check_project(tmp_path)
    assert report.ok, _text(report)  # advisory: staleness never fails the doctor
    assert "unreachable" in _text(report)
    assert "31h old" in _text(report)
    assert "otto inventory refresh" in _text(report)


def test_a_file_that_fails_to_list_records_warns_rather_than_crashing(tmp_path, monkeypatch):
    """``orphan_warning`` needs ``list_keys()``, which does I/O the first time — it can fail too.

    The host is inline (no inventory reference), so the "lab" area itself
    stays green; the broken file only bites the warnings pass that tries to
    list its records for the orphan check, and that must degrade to a
    warning.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _make_first_host_inline(tmp_path)
    (tmp_path / "lab_data" / "inventory.json").write_text("{not valid json")
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.warnings
    assert "could not list records" in _text(report)


def test_inventory_for_memoises_a_resolved_inventory_per_root(tmp_path, monkeypatch):
    """The SAME cache, asked twice for the same root, returns the SAME object.

    One ``check_project`` asks ``_inventory_for`` up to three times (the "lab"
    area's own validation, the warnings pass, the labels); without
    memoisation each ask reconstructs the backend. Identity (``is``), not
    just equality, is the proof it was not rebuilt — a fresh
    ``JsonInventory`` would be a fresh object even with the same contents.
    """
    from otto.init.doctor import _inventory_for

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    cache: dict = {}
    first = _inventory_for(tmp_path, cache)
    second = _inventory_for(tmp_path, cache)
    assert first is not None
    assert first is second
    assert cache[tmp_path] is first


def test_inventory_for_memoises_a_broken_declaration_too(tmp_path, monkeypatch):
    """A broken declaration's EXCEPTION is cached and replayed, not re-parsed on every ask."""
    from otto.init.doctor import _inventory_for

    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    _scaffold_all(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(  # scaffold-exempt: malformed TOML under test
        "not valid toml [[["
    )
    cache: dict = {}
    with pytest.raises(ValueError, match=r"Expected '=' after a key") as first:
        _inventory_for(tmp_path, cache)
    with pytest.raises(ValueError, match=r"Expected '=' after a key") as second:
        _inventory_for(tmp_path, cache)
    assert first.value is second.value
    assert cache[tmp_path] is first.value


def test_one_check_project_constructs_the_inventory_only_once(tmp_path, monkeypatch):
    """End-to-end proof of the cache: one ``check_project`` builds the inventory ONCE.

    A check asks ``_inventory_for`` up to three times (the "lab" area's own
    validation, the warnings pass, and the labels); this counts the actual
    construction call, not just ``_inventory_for``'s own cache, so it also
    proves every one of those askers is handed the call's one cache — any
    that were not would count the construction again.
    """
    import otto.inventory.config as otto_inventory

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)

    real = otto_inventory.build_inventory_from_declarations
    calls: list[int] = []

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(otto_inventory, "build_inventory_from_declarations", _counting)
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert len(calls) == 1


def test_orphan_creds_warn_and_the_store_label_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _set_creds(tmp_path, {"device-01.lab.example": [{"login": "u", "password": "p"}], "stale": []})
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.inventory_label == f"json:{tmp_path / 'lab_data' / 'inventory.json'}"
    assert report.creds_label == f"json:{tmp_path / 'lab_data' / 'creds.json'}"
    assert "1 key(s) the inventory does not hold: stale" in _text(report)


def test_creds_without_an_inventory_is_a_problem_naming_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _drop_table(tmp_path, "inventory")
    report = check_project(tmp_path)
    assert not report.ok
    assert "[creds] is keyed by inventory key and no [inventory] is declared" in _text(report)
    assert str(tmp_path / ".otto" / "settings.toml") in _text(report)


def test_a_missing_creds_store_file_warns_rather_than_failing_the_check(tmp_path, monkeypatch):
    """A creds store that cannot even be read degrades to a warning, never a failure.

    The host is inline (mirrors
    ``test_a_file_that_fails_to_list_records_warns_rather_than_crashing``), so
    the "lab" area itself stays green; the missing creds file only bites
    ``_lab_warnings``'s own doctor pass (``orphan_creds_warning``,
    ``creds_mode_warnings``), and that must degrade to the same warning
    ``creds_mode_warnings`` already names rather than surface the raw
    ``CredsError``.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _make_first_host_inline(tmp_path)
    creds = tmp_path / "lab_data" / "creds.json"
    creds.unlink()  # the live [creds] table now points at a missing file
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert f"{creds} does not exist" in _text(report)
    assert "CredsError" not in _text(report)


@pytest.mark.parametrize("area", ["kmodcov", "embedded-gcov"])
def test_all_never_scaffolds_an_opt_in_area(tmp_path: Path, area: str) -> None:
    assert area not in scaffold_candidates(tmp_path, all_areas=True)
    _scaffold_all(tmp_path)
    assert not (tmp_path / "third_party").exists()
    assert check_project(tmp_path).verdict(name=area).state == "absent"


def test_kmodcov_scaffolds_the_area_and_refreshes_it(tmp_path: Path) -> None:
    from otto import kmodcov

    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "kmodcov")
    vendored = tmp_path / "third_party" / "otto_kmodcov"
    assert kmodcov.check_tree(vendored).state == "current"
    (vendored / "kmodcov.h").write_text("// stale\n")
    again = _scaffold(tmp_path, "kmodcov")
    assert kmodcov.check_tree(vendored).state == "current"
    outcomes = {w.path: w.outcome for w in again.writes}
    assert outcomes[vendored / "kmodcov.h"] == "refreshed"
    starter = tmp_path / "third_party" / "otto_kmodcov-consumer"
    assert {p: o for p, o in outcomes.items() if p.parent == starter} == {
        starter / name: "kept"
        for name in ("kmodcov_begin.c", "kmodcov_end.c", "Kbuild.example", "README.md")
    }


def test_kmodcov_dir_places_the_export(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    scaffold(
        InitConfig(tmp_path, "widget", "0.1.0", kmodcov_dir="vendor/kmodcov"), areas=["kmodcov"]
    )
    assert (tmp_path / "vendor" / "kmodcov" / "kmodcov.h").is_file()


def test_a_differing_vendored_copy_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "kmodcov")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: appends to a settings.toml otto init itself scaffolded
        settings.read_text() + '\n[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "third_party/otto_kmodcov"\n'
        'match = { id = ".*" }\n'
    )
    (tmp_path / "third_party" / "otto_kmodcov" / "kmodcov.c").write_text("// edited\n")
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.verdict(name="kmodcov").state == "ok"
    (warning,) = [w for w in report.warnings if "kmodcov.c" in w]
    assert "otto cov kmodcov export" in warning


def test_a_declared_source_with_no_library_fails_the_kmodcov_area(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: appends to a settings.toml otto init itself scaffolded
        settings.read_text() + '\n[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "vendor/missing"\nmatch = { id = ".*" }\n'
    )
    report = check_project(tmp_path)
    assert not report.ok
    assert report.verdict(name="kmodcov").state == "failed"
    assert "vendor/missing" in _text(report)


def _settings(root: Path, body: str) -> None:
    make_project(root, name="acme", version="1.0.0", extra=body)


def test_a_json_source_without_paths_fails_the_lab_area(tmp_path: Path) -> None:
    """A source's options are its backend's to parse, at preparation: the lab area reports them."""
    _settings(tmp_path, '[[lab.sources]]\nbackend = "json"\n')
    # Detected even though no lab file exists: a declaration that does not
    # prepare counts as present, so nothing is ever scaffolded beside it.
    assert "lab" in detect_areas(root=tmp_path)
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "ok"
    lab = report.verdict(name="lab")
    assert lab.state == "failed"
    assert "parse failed" in lab.problems[0]
    assert "paths" in lab.problems[0]
    assert not report.ok


def test_a_source_whose_backend_registers_in_an_init_module_is_checked_after_init(
    tmp_path: Path,
) -> None:
    """``otto init`` runs no init module, so such a source is deferred, never failed."""
    _settings(tmp_path, '[[lab.sources]]\nbackend = "cmdb"\nserver = "db"\n')
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "ok"
    assert "acme/cmdb#1 (backend 'cmdb'): checked after init" in report.verdict(name="lab").detail


@pytest.mark.parametrize("line", ['libs = "pylib"\n', 'tests = "tests"\n'], ids=["libs", "tests"])
def test_a_non_list_libs_or_tests_fails_settings_and_never_raises(
    tmp_path: Path, line: str
) -> None:
    """A scalar where the settings want a list is the settings area's problem, not a KeyError."""
    _settings(tmp_path, line)
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "failed"


def test_a_non_string_init_entry_fails_settings_once_and_blocks_instructions(
    tmp_path: Path,
) -> None:
    """``init = [1, 2]`` is one settings problem, not a second "init module 1 not found"."""
    _settings(tmp_path, "init = [1, 2]\n")
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "failed"
    assert report.verdict(name="instructions") == AreaVerdict(
        "instructions", "blocked", detail="settings did not compile"
    )


def test_a_non_list_init_fails_settings_once_and_blocks_instructions(tmp_path: Path) -> None:
    """``init = "mod"`` is the settings area's problem; instructions does not repeat it."""
    _settings(tmp_path, 'init = "acme_instructions"\n')
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "failed"
    assert report.verdict(name="instructions") == AreaVerdict(
        "instructions", "blocked", detail="settings did not compile"
    )


def test_an_unreadable_test_file_is_a_problem_never_raises(tmp_path: Path) -> None:
    """A ``test_*.py`` glob lists but nothing can read — dangling link, directory — fails tests."""
    _scaffold_all(tmp_path)
    tests_dir = tmp_path / "tests"
    (tests_dir / "test_dangling.py").symlink_to(tmp_path / "nowhere.py")
    (tests_dir / "test_a_dir.py").mkdir()
    problems = "\n".join(check_project(tmp_path).verdict(name="tests").problems)
    assert "test_dangling.py" in problems
    assert "test_a_dir.py" in problems


def test_a_lab_file_with_a_utf8_bom_fails_as_the_loader_refuses_it(tmp_path: Path) -> None:
    """The loader reads lab files as text and ``json.load`` refuses a BOM — so must the doctor."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_bytes(b"\xef\xbb\xbf" + lab_file.read_bytes())
    verdict = check_project(tmp_path).verdict(name="lab")
    assert verdict.state == "failed"
    assert str(lab_file) in "\n".join(verdict.problems)


def test_a_typod_os_profile_default_fails_settings_and_registers_nothing(tmp_path: Path) -> None:
    from otto.host.os_profile import OS_PROFILES

    before = sorted(OS_PROFILES.names())
    _settings(tmp_path, '[os_profiles.acme-os]\nbase = "unix"\nosTyp = "unix"\n')
    report = check_project(tmp_path)
    assert "unknown default field" in "\n".join(report.verdict(name="settings").problems)
    assert sorted(OS_PROFILES.names()) == before


def test_a_table_over_a_class_whose_module_is_missing_fails_settings(tmp_path: Path) -> None:
    """A class registered by reference whose module is gone is a settings problem, not a raise."""
    from otto.host.os_profile import register_host_class
    from otto.registry import Ref

    register_host_class(
        "plugin-host",
        Ref(target="plugin_hosts_missing:PluginHost"),
        spec=Ref(target="plugin_hosts_missing:PluginSpec"),
    )
    _settings(tmp_path, '[os_profiles.alpha]\nbase = "plugin-host"\n')
    settings = check_project(tmp_path).verdict(name="settings")
    assert settings.state == "failed"
    problems = "\n".join(settings.problems)
    assert "[os_profiles.alpha] in project 'acme'" in problems
    assert f"base 'plugin-host' (host class registered by {__name__})" in problems
    assert "resolution failed: ModuleNotFoundError" in problems


def test_unparsable_settings_fail_once_and_block_lab_and_instructions(tmp_path: Path) -> None:
    (tmp_path / ".otto").mkdir()
    (tmp_path / ".otto" / "settings.toml").write_text(  # scaffold-exempt: malformed TOML under test
        'name = "acme\n'
    )
    report = check_project(tmp_path)
    settings = report.verdict(name="settings")
    assert settings.state == "failed"
    assert len(settings.problems) == 1
    assert report.verdict(name="lab").state == "blocked"
    assert report.verdict(name="instructions").state == "blocked"
    assert _text(report).count(str(tmp_path / ".otto" / "settings.toml")) == 1


def test_a_settings_file_that_is_not_utf8_fails_settings_and_never_raises(tmp_path: Path) -> None:
    """TOML is UTF-8: an undecodable file is the settings area's one problem, not a traceback.

    Every tolerant reader the other areas ask must read it as unreadable too,
    or the doctor raises out of the first one that decodes it.
    """
    (tmp_path / ".otto").mkdir()
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_bytes(b'name = "\xff"\n')  # scaffold-exempt: non-UTF-8 settings under test
    report = check_project(tmp_path)
    assert report.verdict(name="settings").state == "failed"
    assert report.verdict(name="lab").state == "blocked"
    assert report.verdict(name="instructions").state == "blocked"


def test_undecodable_lab_and_test_files_are_problems_never_raises(tmp_path: Path) -> None:
    """A lab file or test file the doctor cannot decode fails its area; it never raises.

    The loader cannot read either one, so each is a problem naming the file —
    not a ``UnicodeDecodeError`` (or, for a NUL byte, ``ast.parse``'s
    ``ValueError``) out of :func:`check_project`.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_bytes(b'{"labs": "\xff"}')
    (tmp_path / "tests" / "test_bad_bytes.py").write_bytes(b'x = "\xff"\n')
    (tmp_path / "tests" / "test_nul.py").write_bytes(b"x = 1\x00\n")
    report = check_project(tmp_path)
    assert report.verdict(name="lab").state == "failed"
    assert str(lab_file) in "\n".join(report.verdict(name="lab").problems)
    tests = "\n".join(report.verdict(name="tests").problems)
    assert "test_bad_bytes.py" in tests
    assert "test_nul.py" in tests


@pytest.mark.parametrize("init_line", ["", "init = []\n"])
def test_no_declared_init_is_absent_and_ok(tmp_path: Path, init_line: str) -> None:
    _settings(tmp_path, init_line)
    report = check_project(tmp_path)
    assert report.verdict(name="instructions") == AreaVerdict(
        "instructions", "absent", detail="no init modules declared"
    )
    assert report.ok


@pytest.mark.parametrize(
    ("init", "files"),
    [
        ("foo", {"pylib/foo.py": ""}),
        ("pkg.sub", {"pylib/pkg/__init__.py": "", "pylib/pkg/sub/__init__.py": ""}),
        ("ns.mod", {"pylib/ns/mod.py": ""}),
    ],
    ids=["single-file", "dotted", "namespace"],
)
def test_init_modules_resolve_like_the_loader(
    tmp_path: Path, init: str, files: dict[str, str]
) -> None:
    _settings(tmp_path, f'libs = ["pylib"]\ninit = ["{init}"]\n')
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert check_project(tmp_path).verdict(name="instructions").state == "ok"


def test_an_init_module_on_sys_path_passes(tmp_path: Path, monkeypatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "doctor_path_only_xyz.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    _settings(tmp_path, 'libs = []\ninit = ["doctor_path_only_xyz"]\n')
    assert check_project(tmp_path).verdict(name="instructions").state == "ok"


def test_a_declared_init_that_resolves_nowhere_fails(tmp_path: Path) -> None:
    _settings(tmp_path, 'libs = ["pylib"]\ninit = ["ghost_xyz"]\n')
    (tmp_path / "pylib").mkdir()
    verdict = check_project(tmp_path).verdict(name="instructions")
    assert verdict.state == "failed"
    assert "ghost_xyz" in verdict.problems[-1]
    assert str(tmp_path / "pylib") in verdict.problems[-1]


def test_check_project_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(InitInputError) as caught:
        check_project(tmp_path / "nope")
    assert caught.value.field == "root"


def test_check_project_never_prints(tmp_path: Path, capsys) -> None:
    _scaffold_all(tmp_path)
    capsys.readouterr()
    check_project(tmp_path)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("line", "area", "named"),
    [
        ('libs = ["~nosuchuser_xyz/lib"]\n', "settings", "~nosuchuser_xyz"),
        ('tests = ["~nosuchuser_xyz/tests"]\n', "settings", "~nosuchuser_xyz"),
        (
            '[[lab.sources]]\nbackend = "json"\npaths = ["~nosuchuser_xyz/lab"]\n',
            "lab",
            "paths: Value error, cannot expand its leading '~'",
        ),
    ],
)
def test_a_path_under_an_unknown_users_home_fails_its_area_and_never_raises(
    tmp_path: Path, line: str, area: str, named: str
) -> None:
    """``~nosuchuser`` cannot expand: the area that parses it reports it; no reader raises.

    A json source's ``paths`` are parsed when the source is prepared, so the
    lab area reports them, naming the field and the rule but never the value
    (a backend config model never echoes one); ``libs`` and ``tests`` are
    settings, whose message quotes the path.
    """
    _settings(tmp_path, line)
    report = check_project(tmp_path)
    assert report.verdict(name=area).state == "failed"
    assert named in _text(report)
    detect_areas(root=tmp_path)
    scaffold_candidates(tmp_path, all_areas=True)


def test_a_kmodcov_source_under_an_unknown_users_home_fails_kmodcov_and_never_raises(
    tmp_path: Path,
) -> None:
    _settings(
        tmp_path,
        '[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "~nosuchuser_xyz/kmodcov"\n'
        'match = { id = ".*" }\n',
    )
    report = check_project(tmp_path)
    assert report.verdict(name="kmodcov").state == "failed"
    assert "~nosuchuser_xyz/kmodcov" in "\n".join(report.verdict(name="kmodcov").problems)


def test_the_doctor_and_the_loader_report_the_same_contextual_error(tmp_path: Path) -> None:
    """One pipeline: the doctor's problem and the loader's error carry the same host-named tail."""
    from otto.init.doctor import _inventory_for
    from otto.init.templates import EXAMPLE_LAB_NAME
    from otto.labs.errors import LabRepositoryError
    from otto.labs.json_repository import _JsonFileLabRepository

    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["hosts"][0].update(valid_terms=["console"])
    lab_file.write_text(json.dumps(data))
    doctor_lines = [
        line for line in _text(check_project(tmp_path)).splitlines() if "hosts[0]" in line
    ]
    assert len(doctor_lines) == 1
    # The scaffold's host is inventory-referenced: the loader resolves it against the
    # inventory the doctor resolves it against.
    repository = _JsonFileLabRepository(search_paths=[tmp_path / "lab_data"])
    with pytest.raises(LabRepositoryError) as exc:
        repository.load_lab(EXAMPLE_LAB_NAME, inventory=_inventory_for(tmp_path))
    tail = "term 'console' requires console_options.server"
    assert tail in doctor_lines[0]
    assert tail in str(exc.value)
    assert doctor_lines[0].split("host '", 1)[1] == str(exc.value).split("host '", 1)[1]


@pytest.mark.parametrize("kind", ["malformed", "uncopyable"])
def test_the_doctor_reports_a_bad_power_table_naming_the_host(tmp_path: Path, kind: str) -> None:
    from pydantic import PrivateAttr

    from otto.host import register_power_controller
    from otto.models.base import OttoModel

    class _Uncopyable(OttoModel):
        _gen: object = PrivateAttr(default_factory=lambda: (i for i in ()))

    register_power_controller("t-uncopyable", config=_Uncopyable, factory=lambda c: None)
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["elements"][0]["hosts"][0]["power_control"] = (
        {"type": "command", "on_cmd": 5} if kind == "malformed" else "t-uncopyable"
    )
    lab_file.write_text(json.dumps(data))
    text = _text(check_project(tmp_path))
    reason = "parse failed" if kind == "malformed" else "cannot be copied"
    assert any(
        "hosts[0]" in line and "host 'example-device'" in line and reason in line
        for line in text.splitlines()
    )


def test_embedded_gcov_scaffolds_the_area_and_refreshes_it(tmp_path: Path) -> None:
    from otto.coverage.embedded_gcov.library import check_patch

    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "embedded-gcov")
    vendored = tmp_path / "third_party" / "embedded-gcov-patch"
    patch_file = vendored / "embedded-gcov-zephyr-gcc12plus.patch"
    assert check_patch(vendored).state == "current"
    assert check_project(tmp_path).verdict(name="embedded-gcov").state == "ok"
    patch_file.write_text("stale\n")
    again = _scaffold(tmp_path, "embedded-gcov")
    assert check_patch(vendored).state == "current"
    outcomes = {w.path: w.outcome for w in again.writes}
    assert outcomes[patch_file] == "refreshed"
    starter = tmp_path / "third_party" / "embedded-gcov-patch-consumer"
    assert {p: o for p, o in outcomes.items() if p.parent == starter} == {
        starter / name: "kept" for name in ("cov_entry.c", "README.md")
    }


def test_a_differing_embedded_gcov_patch_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "embedded-gcov")
    patch_file = (
        tmp_path / "third_party" / "embedded-gcov-patch" / "embedded-gcov-zephyr-gcc12plus.patch"
    )
    patch_file.write_text("edited\n")
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.verdict(name="embedded-gcov").state == "ok"
    (warning,) = [w for w in report.warnings if "embedded-gcov-zephyr-gcc12plus.patch" in w]
    assert f"otto cov embedded-gcov export {patch_file.parent}" in warning
    assert warning.startswith(f"{patch_file.parent} differs from the installed otto's copy: ")


def test_a_current_embedded_gcov_patch_warns_of_nothing(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "embedded-gcov")
    assert check_project(tmp_path).warnings == []


def test_a_patch_under_a_custom_directory_gets_no_drift_warning(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    scaffold(
        InitConfig(tmp_path, "widget", "0.1.0", embedded_gcov_dir="vendor/egcov"),
        areas=["embedded-gcov"],
    )
    (tmp_path / "vendor" / "egcov" / "embedded-gcov-zephyr-gcc12plus.patch").write_text("edited\n")
    report = check_project(tmp_path)
    assert report.warnings == []
    assert report.verdict(name="embedded-gcov").state == "absent"


def test_every_area_without_a_dedicated_branch_has_a_validator() -> None:
    """A new area must name its validator; none may fall through to another area's."""
    from otto.init.doctor import _AREA_VALIDATORS

    dedicated = {"settings", "schemas", "lab", "tests", "instructions"}
    assert set(AREA_NAMES) - dedicated == set(_AREA_VALIDATORS)


def test_a_failing_kmodcov_area_does_not_fail_the_embedded_gcov_verdict(tmp_path: Path) -> None:
    """The verdict of one area is its own validator's: embedded-gcov never runs kmodcov's."""
    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "embedded-gcov")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: appends to a settings.toml otto init itself scaffolded
        settings.read_text() + '\n[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "vendor/missing"\nmatch = { id = ".*" }\n'
    )
    report = check_project(tmp_path)
    assert report.verdict(name="kmodcov").state == "failed"
    assert report.verdict(name="embedded-gcov").state == "ok"


@pytest.mark.parametrize("configured", [True, False])
def test_every_tests_dir_consumer_reads_the_same_dirs(tmp_path: Path, configured: bool) -> None:
    from otto.init.areas import template_tests_present
    from otto.init.doctor import _validate_tests

    _scaffold(tmp_path, "settings", "lab", "tests")
    home, other = tmp_path / "tests", tmp_path / "checks"
    settings = tmp_path / ".otto" / "settings.toml"
    if configured:
        settings.write_text(  # scaffold-exempt: retargeting the scaffolded tests dir
            settings.read_text().replace('tests = ["tests"]', 'tests = ["checks"]', 1)
        )
        home, other = other, home
    else:
        settings.unlink()  # no settings file: every consumer falls back to root/tests
    home.mkdir(exist_ok=True)
    other.mkdir(exist_ok=True)
    if home != tmp_path / "tests":
        shutil.move(str(other / "test_example.py"), str(home / "test_example.py"))

    def reads(root: Path) -> tuple[bool, bool, bool]:
        return (
            "tests" in detect_areas(root=root),
            template_tests_present(root),
            _validate_tests(root) == [],
        )

    assert reads(tmp_path) == (True, True, True)
    shutil.move(str(home / "test_example.py"), str(other / "test_example.py"))
    assert reads(tmp_path) == (False, False, False)


# ── Named inventories: every name checked, each finding attributed (#1176) ─────

_LOGINS = [{"login": "u", "password": "p"}]


def _entry_toml(name: str, path: str) -> str:
    """One ``[[inventories]]`` json entry named *name* over *path*, supplying ``ip``."""
    return (
        f'[[inventories]]\nname = "{name}"\nbackend = "json"\npath = "{path}"\nsupplies = ["ip"]\n'
    )


def _named_project(
    root: Path,
    *,
    name: str = "proj",
    tables: str = "",
    records: "dict[str, dict] | None" = None,
    references: "list[object] | tuple[object, ...]" = (),
) -> Path:
    """A project whose json lab's hosts state *references*, one element each.

    *tables* is settings text written BEFORE ``[[lab.sources]]``, so a
    top-level key in it stays top-level. *records* maps a file under *root*
    to the inventory records it holds.
    """
    files = {path: json.dumps(held) for path, held in (records or {}).items()}
    elements = [
        {
            "name": f"e{index}",
            "labs": ["bench"],
            "hosts": [{"inventory": reference, "os_type": "unix", "creds": _LOGINS}],
        }
        for index, reference in enumerate(references)
    ]
    files["lab/lab.json"] = json.dumps({"labs": {"bench": {}}, "elements": elements})
    extra = tables + '\n[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'
    return make_project(root, name=name, extra=extra, files=files)


def _qualified(name: str, key: str) -> dict[str, str]:
    return {"name": name, "key": key}


def orphan_findings(report: DoctorReport) -> dict[str, list[str]]:
    """Each attributed orphan warning's inventory name and the keys it lists."""
    import re

    found: dict[str, list[str]] = {}
    for warning in report.warnings:
        match = re.match(
            r"(?P<name>[\w-]+): inventory '[^']*': \d+ record\(s\) referenced by no lab "
            r"file here: (?P<keys>.*?) \(expected",
            warning,
        )
        if match:
            found[match["name"]] = match["keys"].split(", ")
    return found


def _projects(*roots: Path) -> list:
    from otto.config.project import Project

    return [Project(root=root) for root in roots]


def _catalog(*roots: Path):
    from otto.inventory.config import deferred_inventory_view

    return deferred_inventory_view(projects=_projects(*roots))


def test_orphans_count_qualified_references_per_name(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = _named_project(
        tmp_path / "p",
        tables=_entry_toml("physical", "physical.json") + _entry_toml("virtual", "virtual.json"),
        records={
            "physical.json": {"a": {"ip": "10.1.0.1"}, "b": {"ip": "10.1.0.2"}},
            "virtual.json": {"a": {"ip": "10.2.0.1"}, "c": {"ip": "10.2.0.3"}},
        },
        references=[_qualified("physical", "a")],
    )
    report = check_project(root)
    assert report.ok, _text(report)
    assert orphan_findings(report) == {"physical": ["b"], "virtual": ["a", "c"]}


def test_two_names_sharing_a_label_are_told_apart(tmp_path, monkeypatch):
    from otto.init.doctor import InventoryLabel

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = _named_project(
        tmp_path / "p",
        tables=_entry_toml("physical", "shared.json") + _entry_toml("virtual", "shared.json"),
        records={"shared.json": {"a": {"ip": "10.1.0.1"}, "b": {"ip": "10.1.0.2"}}},
        references=[_qualified("physical", "a")],
    )
    report = check_project(root)
    assert report.ok, _text(report)
    label = f"json:{root / 'shared.json'}"
    assert orphan_findings(report) == {"physical": ["b"], "virtual": ["a", "b"]}
    assert report.inventories == [
        InventoryLabel(name="physical", label=label, creds_label=None),
        InventoryLabel(name="virtual", label=label, creds_label=None),
    ]
    assert report.inventory_label is None  # no default resolves


def test_a_cross_project_reference_uses_the_active_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    a = _named_project(
        tmp_path / "A",
        name="proj-a",
        tables=_entry_toml("physical", "physical.json"),
        records={"physical.json": {"k": {"ip": "10.1.0.1"}, "spare": {"ip": "10.1.0.2"}}},
        references=[_qualified("physical", "k")],
    )
    b = _named_project(tmp_path / "B", name="proj-b", references=[_qualified("physical", "k")])
    alone = check_project(b)
    assert not alone.ok
    assert "references inventory 'physical' (key 'k'), which is not configured" in _text(alone)
    report = check_project(b, catalog=_catalog(a, b))
    assert report.ok, _text(report)
    assert orphan_findings(report) == {}  # physical is A's: its orphans are A's to report
    assert [entry.name for entry in report.inventories] == ["physical"]


def test_a_d1_refused_reference_is_a_problem_and_not_a_default_reference(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "user.json").write_text(json.dumps({"k": {"ip": "10.9.0.1"}}))
    (home / "settings.toml").write_text(  # scaffold-exempt: the otto home's user file
        f'[inventory]\nbackend = "json"\npath = "{home / "user.json"}"\nsupplies = ["ip"]\n'
    )
    monkeypatch.setenv("OTTO_HOME", str(home))
    root = _named_project(
        tmp_path / "p",
        tables=_entry_toml("physical", "physical.json"),
        records={"physical.json": {"a": {"ip": "10.1.0.1"}}},
        references=["k", _qualified("physical", "a")],
    )
    report = check_project(root)
    assert not report.ok
    problems = report.verdict(name="lab").problems
    assert len(problems) == 1, problems
    assert "bare inventory reference 'k' is refused" in problems[0]
    assert orphan_findings(report) == {"default": ["k"]}


def test_a_missing_key_in_a_named_inventory_is_attributed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = _named_project(
        tmp_path / "p",
        tables=_entry_toml("physical", "physical.json"),
        records={"physical.json": {"a": {"ip": "10.1.0.1"}}},
        references=[_qualified("physical", "zz")],
    )
    report = check_project(root)
    assert not report.ok
    [problem] = report.verdict(name="lab").problems
    assert "element 'e0' hosts[0] physical: " in problem
    assert "'zz'" in problem


def test_a_failing_named_inventory_beside_a_healthy_one(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    broken = '[[inventories]]\nname = "physical"\nbackend = "json"\nsupplies = ["ip"]\n'
    root = _named_project(
        tmp_path / "p",
        tables=broken + _entry_toml("virtual", "virtual.json"),
        records={"virtual.json": {"a": {"ip": "10.2.0.1"}, "c": {"ip": "10.2.0.3"}}},
        references=[_qualified("physical", "x"), _qualified("virtual", "a")],
    )
    report = check_project(root)
    assert not report.ok
    [problem] = report.verdict(name="lab").problems
    assert problem.startswith("inventory: physical: "), problem
    assert "path: Field required" in problem, problem
    assert orphan_findings(report) == {"virtual": ["c"]}
    assert [entry.name for entry in report.inventories] == ["virtual"]


def test_a_programming_error_in_an_own_build_is_a_traceback(tmp_path, monkeypatch):
    """Only the catalog route catches every error: otto's own builds raise as on main."""
    from otto.inventory import config

    def broken(**_kwargs):
        raise TypeError("a bug in otto")

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "build_named_inventory_from_declarations", broken)
    root = _named_project(
        tmp_path / "p",
        tables=_entry_toml("physical", "physical.json"),
        records={"physical.json": {"a": {"ip": "10.1.0.1"}}},
        references=[_qualified("physical", "a")],
    )
    with pytest.raises(TypeError, match="a bug in otto"):
        check_project(root)


def test_any_error_a_catalog_raises_is_a_problem_row(tmp_path, monkeypatch):
    """A catalog is any view: its per-name build may raise anything, never out of the doctor."""
    from otto.inventory import InventoryView

    class Exploding(dict):
        def get(self, name, default=None):
            raise RuntimeError(f"backend for {name} is down")

        def __contains__(self, name):
            return True

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = _named_project(tmp_path / "p", references=[_qualified("physical", "k")])
    report = check_project(root, catalog=InventoryView(inventories=Exploding()))
    assert not report.ok
    [problem] = report.verdict(name="lab").problems
    assert problem == "inventory: physical: RuntimeError: backend for physical is down"


def test_an_external_name_is_checked_despite_a_default_disagreement(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    default = '[inventory]\nbackend = "json"\npath = "default.json"\nsupplies = ["ip"]\n'
    a = _named_project(
        tmp_path / "A",
        name="proj-a",
        tables=default,
        records={"default.json": {"d": {"ip": "10.0.0.1"}}},
        references=["d", _qualified("physical", "k")],
    )
    b = _named_project(
        tmp_path / "B",
        name="proj-b",
        tables=default + _entry_toml("physical", "physical.json"),
        records={
            "default.json": {"e": {"ip": "10.0.0.2"}},
            "physical.json": {"k": {"ip": "1.1.1.1"}},
        },
    )
    from otto.inventory.errors import InventoryError

    view = _catalog(a, b)
    with pytest.raises(InventoryError) as refused:
        view.inventories["default"]
    report = check_project(a, catalog=view)
    # ``default`` is refused, as dispatch refuses it; ``physical`` is checked on its own.
    assert not report.ok
    assert report.verdict(name="lab").problems == [f"inventory: default: {refused.value}"]
    assert [entry.name for entry in report.inventories] == ["physical"]


def test_a_user_declared_name_is_known_standalone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "vms.json").write_text(json.dumps({"k": {"ip": "10.3.0.1"}}))
    user_file = _entry_toml("vms", str(home / "vms.json"))
    (home / "settings.toml").write_text(user_file)  # scaffold-exempt: the otto home's user file
    monkeypatch.setenv("OTTO_HOME", str(home))
    root = _named_project(tmp_path / "p", references=[_qualified("vms", "k")])
    report = check_project(root)
    assert report.ok, _text(report)
    assert orphan_findings(report) == {}  # the user's inventory: not this project's to report


def test_a_default_only_project_reports_as_before(tmp_path, monkeypatch):
    from otto.init.doctor import InventoryLabel

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    inv = _set_inventory_records(
        tmp_path, {"device-01.lab.example": {"ip": "10.0.0.1"}, "spare": {"ip": "10.0.0.2"}}
    )
    creds = tmp_path / "lab_data" / "creds.json"
    report = check_project(tmp_path)
    assert report.ok, _text(report)
    assert report.inventories == [
        InventoryLabel(name="default", label=f"json:{inv}", creds_label=f"json:{creds}")
    ]
    [orphan] = [w for w in report.warnings if "referenced by no lab file" in w]
    assert orphan.startswith(f"inventory 'json:{inv}': 1 record(s)"), orphan


def _workspace(tmp_path, monkeypatch, name: str):
    """One of the pinned doctor setup's two-project workspaces: ``(A, B, the dispatch view)``."""
    from tests.unit.labs import test_unchanged_loading as pins

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    projects = pins._workspaces(tmp_path)[name]
    return projects["A"], projects["B"], _catalog(projects["A"], projects["B"])


def test_a_project_without_an_inventory_resolves_through_the_active_ones(tmp_path, monkeypatch):
    """Row (a): B loads through A's inventory under dispatch, and so does the doctor."""
    a, b, view = _workspace(tmp_path, monkeypatch, "a-bare-references-without-inventory")
    dispatch_default = view.inventories["default"]
    report = check_project(b, catalog=view)
    assert report.ok, _text(report)
    assert report.inventory_label == dispatch_default.label == f"json:{a / 'inventory.json'}"
    # Without the catalog B is not an active project: the project-local rule stays.
    assert not check_project(b).ok


def test_a_project_with_only_creds_composes_with_the_active_inventory(tmp_path, monkeypatch):
    """Row (a'): A's inventory composes with B's [creds], as dispatch builds them."""
    a, b, view = _workspace(tmp_path, monkeypatch, "a2-creds-without-inventory")
    dispatch_default = view.inventories["default"]
    report = check_project(b, catalog=view)
    assert report.ok, _text(report)
    assert report.inventory_label == dispatch_default.label == f"json:{a / 'inventory.json'}"
    assert report.creds_label == f"json:{b / 'creds.json'}"
    assert not check_project(b).ok


def test_two_projects_declaring_different_inventories_fail_as_dispatch_refuses(
    tmp_path, monkeypatch
):
    """Row (b): each project's doctor reports the refusal dispatch raises, in its words."""
    from otto.inventory.errors import InventoryError

    a, b, view = _workspace(tmp_path, monkeypatch, "b-different-inventories")
    with pytest.raises(InventoryError, match="two active projects declare different") as refused:
        view.inventories["default"]
    for root in (a, b):
        report = check_project(root, catalog=view)
        assert not report.ok
        assert f"inventory: {refused.value}" in report.verdict(name="lab").problems
        assert report.inventory_label is None
        assert check_project(root).ok  # not an active project: the project-local rule stays


def test_an_orphan_only_another_active_project_references_is_not_reported(tmp_path, monkeypatch):
    """A's doctor counts B's bare references: k2 is B's, so it is not an orphan of A's inventory."""
    a, _b, view = _workspace(tmp_path, monkeypatch, "a-bare-references-without-inventory")
    assert any("record(s) referenced by no lab file" in w for w in check_project(a).warnings)
    report = check_project(a, catalog=view)
    assert report.ok, _text(report)
    assert not any("referenced by no lab file" in w for w in report.warnings), report.warnings


def test_a_catalog_that_does_not_list_the_root_keeps_the_local_rule(tmp_path, monkeypatch):
    """The root is not one of the catalog's projects: its own tables decide, as before."""
    a, b, _ = _workspace(tmp_path, monkeypatch, "a-bare-references-without-inventory")
    report = check_project(b, catalog=_catalog(a))
    assert not report.ok
    assert "no inventory is configured" in _text(report)
    assert report.inventory_label is None


# ── The same, through ``otto init``: the CLI hands the active catalog over ──────


def _init(root: Path):
    from otto.cli.init import init_command
    from tests._fixtures.dispatch import DispatchRunner

    return DispatchRunner().invoke(
        init_command, ["--schemas", "--path", str(root)], spec_name="init"
    )


def _activate(monkeypatch, *roots: Path) -> None:
    """Make *roots* the active projects, discovered for real under the dispatch harness.

    ``DispatchRunner`` stubs the composition root clean unless a test chose
    its own; this one is the real discovery, so ``otto init`` sees *roots*.
    """
    import os

    from otto import bootstrap

    real = bootstrap.bootstrap

    def discovered():
        return real()

    monkeypatch.setenv("OTTOPATH", os.pathsep.join(str(root) for root in roots))
    monkeypatch.setenv("COLUMNS", "600")
    monkeypatch.setattr(bootstrap, "bootstrap", discovered)
    bootstrap._reset()


def test_a_catalog_conflict_is_a_problem_row_from_the_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    physical = {"physical.json": {"k": {"ip": "10.1.0.1"}}}
    a = _named_project(
        tmp_path / "A",
        name="proj-a",
        tables=_entry_toml("physical", "physical.json"),
        records=physical,
        references=[_qualified("physical", "k")],
    )
    b = _named_project(tmp_path / "B", name="proj-b", references=[_qualified("physical", "k")])
    c = _named_project(
        tmp_path / "C",
        name="proj-c",
        tables=_entry_toml("physical", "other.json"),
        records={"other.json": {"k": {"ip": "10.1.0.9"}}},
    )
    _activate(monkeypatch, a, b, c)
    result = _init(b)
    assert result.exit_code == 1, result.output
    [row] = [line for line in result.output.splitlines() if "two active projects" in line]
    assert "inventory: physical: " in row
    assert str(a / ".otto" / "settings.toml") in row
    assert str(c / ".otto" / "settings.toml") in row
    passed = _init(a)
    assert passed.exit_code == 0, passed.output
    assert f"inventory physical: json:{a / 'physical.json'}" in passed.output


def test_the_exit_code_follows_dispatch_through_the_cli(tmp_path, monkeypatch):
    """``otto init`` passes where dispatch loads (row a) and fails where it refuses (row b)."""
    from tests.unit.labs import test_unchanged_loading as pins

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    spaces = pins._workspaces(tmp_path)
    shared = spaces["a-bare-references-without-inventory"]
    _activate(monkeypatch, shared["A"], shared["B"])
    passed = _init(shared["B"])
    assert passed.exit_code == 0, passed.output
    assert f"inventory: json:{shared['A'] / 'inventory.json'}" in passed.output
    split = spaces["b-different-inventories"]
    _activate(monkeypatch, split["A"], split["B"])
    refused = _init(split["B"])
    assert refused.exit_code == 1, refused.output
    assert "two active projects declare different [inventory] tables" in refused.output


def test_a_named_creds_row_lines_up_with_its_inventory_row(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    tables = (
        _entry_toml("physical", "physical.json")
        + '[inventories.creds]\nbackend = "json"\npath = "creds.json"\n'
    )
    root = _named_project(
        tmp_path / "p",
        tables=tables,
        records={"physical.json": {"a": {"ip": "10.1.0.1"}}, "creds.json": {"a": []}},
        references=[_qualified("physical", "a")],
    )
    (root / "creds.json").chmod(0o600)
    monkeypatch.setenv("COLUMNS", "600")
    result = _init(root)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    [inventory] = [line for line in lines if line.startswith("inventory physical: ")]
    [creds] = [line for line in lines if line.startswith("creds physical:")]
    assert inventory.index("json:") == creds.index("json:"), [inventory, creds]
    assert creds.endswith(f"json:{root / 'creds.json'}")


def test_a_malformed_user_settings_file_is_a_problem_row(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    user_file = home / "settings.toml"
    user_file.write_text("not valid toml [[[")  # scaffold-exempt: malformed TOML under test
    monkeypatch.setenv("OTTO_HOME", str(home))
    default = '[inventory]\nbackend = "json"\npath = "default.json"\nsupplies = ["ip"]\n'
    root = _named_project(
        tmp_path / "p",
        tables=default,
        records={"default.json": {"k": {"ip": "10.0.0.1"}}},
        references=["k"],
    )
    _activate(monkeypatch, root)
    result = _init(root)
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    rows = [line for line in result.output.splitlines() if str(user_file) in line]
    assert len(rows) == 1, result.output
    assert "inventory: " in rows[0]


@pytest.mark.parametrize("toml", ["[inventories]\nname = 'vms'\n", "inventories = ['vms']\n"])
def test_a_malformed_inventories_table_is_a_problem_row(tmp_path, monkeypatch, toml):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = _named_project(tmp_path / "p", tables=toml, references=[_qualified("vms", "k")])
    # Not activated: an active project that fails to load stops every command
    # at the bootstrap gate, before the doctor. This is the project otto init
    # checks before it is ever put on OTTOPATH.
    monkeypatch.setenv("COLUMNS", "600")
    result = _init(root)
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    settings = root / ".otto" / "settings.toml"
    assert any(str(settings) in line for line in result.output.splitlines()), result.output


def test_an_inventory_only_project_reports_its_broken_declaration_once(tmp_path, monkeypatch):
    """A project that only supplies inventories has no lab to report them in (Astra I7).

    Its own ``[[inventories]]`` entry that does not build is one settings
    problem, and no warning or label repeats it.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = tmp_path / "supplier"
    make_project(
        root,
        name="supplier",
        extra='[[inventories]]\nname = "physical"\nbackend = "nope:unregistered"\n',
    )
    report = check_project(root)
    assert not report.ok
    assert report.verdict(name="lab").state == "absent"
    [problem] = report.verdict(name="settings").problems
    assert problem.startswith("inventory: physical: "), problem
    assert "nope:unregistered" in problem
    assert report.warnings == []
    assert report.inventories == []
    assert _text(report).count("inventory: physical:") == 1


# ── Templated labs and inventories: the doctor's own checks never render (#1176) ─

_NOT_RENDERED_TAIL = "; run otto init from inside an active project, or see otto --list-labs"


def _not_rendered(entry: Path, refusal: str) -> str:
    return f"{entry}: not rendered: {refusal}{_NOT_RENDERED_TAIL}"


def _templated_inventory(root: Path, name: str, *, creds: bool = False) -> str:
    """A ``.jsonnet`` json inventory *name* under *root*, optionally with a ``[creds]`` binding."""
    from tests.unit.templating._doctor_project import records
    from tests.unit.templating._lab_project import table, write

    entry = write(root / "lab_data" / f"{name}.jsonnet", json.dumps(records("east")))
    keys: dict = {"name": name, "backend": "json", "path": str(entry)}
    text = table("[[inventories]]", **keys)
    if creds:
        store = write(root / "lab_data" / f"{name}-creds.json", {"dut-east": []})
        store.chmod(0o600)
        text += table("[inventories.creds]", backend="json", path=str(store))
    return text


def test_catalog_membership_never_raises(tmp_path, otto_home):
    from otto.init.doctor import CatalogMembership, catalog_membership
    from otto.inventory import InventoryView
    from tests.unit.templating._doctor_project import catalog, templated

    class Exploding(dict):
        def __getattr__(self, name):
            raise RuntimeError(f"no {name}")

    member = templated(tmp_path / "a")
    outsider = tmp_path / "b"
    outsider.mkdir()
    view = catalog(member)
    assert catalog_membership(None, root=member.root) == CatalogMembership(
        project=None, projects=None, refusal="the project is not one of the active projects"
    )
    assert catalog_membership(InventoryView(inventories=Exploding()), root=member.root) == (
        CatalogMembership(
            project=None, projects=None, refusal="the inventory view is not otto's active catalog"
        )
    )
    assert catalog_membership(view, root=outsider) == CatalogMembership(
        project=None, projects=None, refusal="the project is not one of the catalog's projects"
    )
    found = catalog_membership(view.for_project(root=member.root), root=member.root)
    assert found.refusal is None
    assert found.project is member
    assert found.projects == [member]


def test_membership_is_one_predicate(tmp_path, otto_home, monkeypatch):
    """``otto init``'s catalog choice and the templating rows' eligibility never disagree."""
    from otto.cli.init import _active_catalog
    from otto.init.doctor import catalog_membership
    from tests.unit.templating._doctor_project import templated
    from tests.unit.templating._lab_project import project

    member = templated(tmp_path / "member")
    bare = project(tmp_path / "bare", "", name="bare")  # declares no inventory at all
    outsider = templated(tmp_path / "outsider", name="outsider")
    alias = tmp_path / "alias"
    alias.symlink_to(member.root)
    monkeypatch.setattr("otto.declared.get_projects", lambda: [member, bare])
    for root, expected in (
        (member.root, member),
        (bare.root, bare),
        (alias, member),
        (outsider.root, None),
    ):
        chosen = _active_catalog(root)
        if expected is None:
            assert chosen is None
            continue
        assert chosen is not None
        membership = catalog_membership(chosen, root=root)
        assert membership.project is expected, root
        assert membership.projects == [member, bare]


def test_active_root_without_an_inventory_declaration_is_a_member(tmp_path, otto_home):
    from otto.init.doctor import catalog_membership
    from otto.inventory.config import deferred_catalog_projects
    from tests.unit.templating._doctor_project import catalog, templated
    from tests.unit.templating._lab_project import project

    supplier = templated(tmp_path / "supplier", name="supplier")
    bare = project(tmp_path / "bare", "", name="bare")
    view = catalog(supplier, bare)
    assert deferred_catalog_projects(view) == [supplier, bare]
    assert catalog_membership(view, root=bare.root).project is bare


def test_deferred_catalog_retention_reads_no_user_file(tmp_path, otto_home, monkeypatch):
    from otto.config import user_settings
    from otto.inventory.config import deferred_catalog_projects, deferred_inventory_view
    from tests.unit.templating._doctor_project import templated
    from tests.unit.templating._lab_project import project

    (otto_home / "settings.toml").write_text(  # scaffold-exempt: malformed TOML, never read here
        "not valid toml [[["
    )
    reads: list = []
    real = user_settings.load_user_settings
    monkeypatch.setattr(
        user_settings, "load_user_settings", lambda **kw: reads.append(kw) or real(**kw)
    )
    projects = [templated(tmp_path / "a"), project(tmp_path / "b", "", name="b")]
    view = deferred_inventory_view(projects=projects)
    assert deferred_catalog_projects(view) == projects
    assert deferred_catalog_projects(view) is not deferred_catalog_projects(view)
    assert deferred_catalog_projects(view.for_project(root=projects[1].root)) == projects
    assert reads == []


def test_no_catalog_reports_not_rendered_rows(tmp_path, otto_home, monkeypatch):
    from tests.unit.templating._doctor_project import entry_path, templated
    from tests.unit.templating._render_helpers import count_evaluations

    sut = templated(tmp_path / "sut", extra=_templated_inventory(tmp_path / "sut", "virtual"))
    evaluations = count_evaluations(monkeypatch)
    report = check_project(sut.root)
    assert evaluations() == 0
    assert report.ok, _text(report)
    rows = [w for w in report.warnings if "not rendered" in w]
    assert rows == [
        "virtual: not rendered by the doctor's own checks; see the templating rows",
        _not_rendered(entry_path(sut.root), "the project is not one of the active projects"),
    ]
    assert [label.name for label in report.inventories] == ["physical"]


def test_outside_root_with_only_a_jsonnet_source_is_not_rendered(tmp_path, otto_home, monkeypatch):
    from tests.unit.templating._doctor_project import (
        catalog,
        entry_path,
        recorded_renders,
        sites_entry,
        templated,
    )
    from tests.unit.templating._lab_project import jsonnet_source, project, write
    from tests.unit.templating._render_helpers import count_evaluations

    a = templated(tmp_path / "a", name="proj-a")
    write(entry_path(tmp_path / "b"), sites_entry())
    b = project(
        tmp_path / "b", jsonnet_source(paths=[str(entry_path(tmp_path / "b"))]), name="proj-b"
    )
    evaluations = count_evaluations(monkeypatch)
    opened: list[Path] = []
    real_read = Path.read_text

    def reading(self, *args, **kwargs):
        opened.append(self)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reading)
    with recorded_renders(monkeypatch) as renders:
        report = check_project(b.root, catalog=catalog(a))
    assert evaluations() == 0
    assert renders == []
    assert entry_path(b.root) not in opened
    assert report.warnings == [
        _not_rendered(entry_path(b.root), "the project is not one of the catalog's projects")
    ]


def test_credential_wrapped_templated_inventory_is_never_discovered_by_doctor_checks(
    tmp_path, otto_home, monkeypatch
):
    """A ``.jsonnet`` ``default`` and named inventory with ``[creds]``: only dispatch renders."""
    from otto.inventory.creds import below_creds_overlay
    from otto.inventory.json_backend import JsonInventory
    from tests.unit.templating._doctor_project import catalog, sites_entry
    from tests.unit.templating._lab_project import jsonnet_source, project, table, write

    root = tmp_path / "sut"
    default = write(root / "lab_data" / "default.jsonnet", "{}")
    store = write(root / "lab_data" / "default-creds.json", {})
    store.chmod(0o600)
    entry = write(root / "lab_data" / "layouts" / "sites.jsonnet", sites_entry())
    settings = (
        table("[inventory]", backend="json", path=str(default))
        + table("[creds]", backend="json", path=str(store))
        + _templated_inventory(root, "physical", creds=True)
        + jsonnet_source(paths=[str(entry)], inventories=["physical"])
    )
    sut = project(root, settings)
    rendered: list[JsonInventory] = []
    real = JsonInventory.read_document

    def recording(self, *, refuse):
        if self.templated:
            rendered.append(self)
        return real(self, refuse=refuse)

    monkeypatch.setattr(JsonInventory, "read_document", recording)
    outsider = project(tmp_path / "other", "", name="other")
    for view in (None, catalog(outsider)):
        rendered.clear()
        report = check_project(sut.root, catalog=view)
        assert rendered == [], view
        assert report.ok, _text(report)
        assert [w for w in report.warnings if "doctor's own checks" in w] == [
            "default: not rendered by the doctor's own checks; see the templating rows",
            "physical: not rendered by the doctor's own checks; see the templating rows",
        ]
    rendered.clear()
    member = catalog(sut)
    report = check_project(sut.root, catalog=member)
    assert report.ok, _text(report)
    held = below_creds_overlay(inventory=member.inventories["physical"])
    assert rendered == [held]


def test_any_render_the_doctor_triggers_holds_the_catalogs_protection(
    tmp_path, otto_home, monkeypatch
):
    """No doctor path renders through a view whose protected catalog is the empty default."""
    from tests.unit.templating._doctor_project import catalog, recorded_renders, templated

    sut = templated(
        tmp_path / "sut",
        extra=_templated_inventory(tmp_path / "sut", "virtual"),
        inventories=["physical", "virtual"],
    )
    settings = sut.root / ".otto" / "settings.toml"
    view = catalog(sut)
    with recorded_renders(monkeypatch) as renders:
        report = check_project(sut.root, catalog=view)
    assert report.ok, _text(report)
    assert len(renders) == 2  # the lab entry and the templated inventory, each once
    protected = view.protected()
    assert settings.resolve() in protected.files
    assert all(request.policy.protected == protected for request in renders)


def test_doctor_checks_keep_their_own_inventories(tmp_path, monkeypatch):
    """The golden workspaces against a member catalog: dispatch's outcomes, and nothing rendered."""
    from otto.init import doctor as doctor_module
    from tests.unit.labs import test_unchanged_loading as pins
    from tests.unit.templating._render_helpers import count_evaluations

    real = doctor_module.check_project
    evaluations = count_evaluations(monkeypatch)

    def against_the_catalog(root: Path) -> DoctorReport:
        return real(root, catalog=_catalog(*sorted(root.parent.iterdir())))

    monkeypatch.setattr(doctor_module, "check_project", against_the_catalog)
    got = pins.doctor(tmp_path)
    assert {
        name: {label: report["ok"] for label, report in rows.items()} for name, rows in got.items()
    } == {
        "a-bare-references-without-inventory": {"A": True, "B": True},
        "a2-creds-without-inventory": {"A": True, "B": True},
        "b-different-inventories": {"A": False, "B": False},
    }
    assert evaluations() == 0


def test_unchanged_doctor_output_for_existing_projects(tmp_path, monkeypatch):
    from tests.unit.labs import test_unchanged_loading as pins
    from tests.unit.templating._render_helpers import count_evaluations

    evaluations = count_evaluations(monkeypatch)
    assert pins.doctor(tmp_path) == json.loads(pins.DOCTOR_GOLDEN.read_text())
    assert evaluations() == 0


def test_arbitrary_catalog_regression_stays_green(tmp_path, otto_home):
    """The arbitrary-catalog case, and its templated twin: a not-rendered row, no exception."""
    from otto.inventory import InventoryView
    from tests.unit.templating._doctor_project import entry_path, templated

    class Exploding(dict):
        def get(self, name, default=None):
            raise RuntimeError(f"backend for {name} is down")

        def __contains__(self, name):
            return True

    sut = templated(tmp_path / "sut")
    report = check_project(sut.root, catalog=InventoryView(inventories=Exploding()))
    assert report.ok, _text(report)
    assert [w for w in report.warnings if "not rendered" in w] == [
        _not_rendered(entry_path(sut.root), "the inventory view is not otto's active catalog")
    ]


def test_non_jsonnet_checks_run_as_today_when_not_rendering(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import entry_path, templated
    from tests.unit.templating._lab_project import json_source, write

    hand = tmp_path / "sut" / "lab"
    write(
        hand / "lab.json",
        {
            "labs": {"bench": {}},
            "elements": [
                {"name": "e", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "nonsense": 1}]}
            ],
        },
    )
    sut = templated(tmp_path / "sut", extra=json_source(hand))
    report = check_project(sut.root)
    [problem] = report.verdict(name="lab").problems
    assert problem.startswith(f"{hand / 'lab.json'}: element 'e' hosts[0] ")
    assert "nonsense" in problem
    assert _not_rendered(entry_path(sut.root), "the project is not one of the active projects") in (
        report.warnings
    )


def test_malformed_user_settings_stays_one_problem_row(tmp_path, otto_home):
    """The broken user file, with a templated lab in an active project: reported once."""
    from tests.unit.templating._doctor_project import catalog, entry_path, templated

    user_file = otto_home / "settings.toml"
    user_file.write_text("not valid toml [[[")  # scaffold-exempt: malformed TOML under test
    sut = templated(tmp_path / "sut")
    for view in (None, catalog(sut)):
        report = check_project(sut.root, catalog=view)
        assert not report.ok
        rows = [line for line in _text(report).splitlines() if str(user_file) in line]
        assert len(rows) == 1, rows
        assert rows[0].startswith("inventory: ")
    # Under the catalog, nothing renders: the entry is one warning, not a second problem.
    assert f"{entry_path(sut.root)}: not rendered: no protected-file catalog could be built" in (
        "\n".join(report.warnings)
    )


def test_doctor_reports_a_render_error_naming_entry_and_line(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, entry_path, templated

    sut = templated(tmp_path / "sut", entry="function(otto)\n  { labs: error 'boom' }\n")
    report = check_project(sut.root, catalog=catalog(sut))
    assert not report.ok
    [problem] = report.verdict(name="lab").problems
    assert f"{entry_path(sut.root)}:2: boom" in problem


def test_doctor_validates_rendered_hosts_like_hand_written_ones(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, sites_entry, templated
    from tests.unit.templating._lab_project import CREDS, json_source, project, write

    sut = templated(tmp_path / "sut", entry=sites_entry(os_type="'nosuch'"))
    [rendered] = check_project(sut.root, catalog=catalog(sut)).verdict(name="lab").problems
    hand = tmp_path / "hand" / "lab"
    host = {"ip": "10.0.1.1", "os_type": "nosuch", "creds": CREDS}
    write(
        hand / "lab.json",
        {
            "labs": {"site-east": {}},
            "elements": [{"name": "east-dut0", "labs": ["site-east"], "hosts": [host]}],
        },
    )
    written = project(tmp_path / "hand", json_source(hand), name="hand")
    [hand_written] = check_project(written.root).verdict(name="lab").problems
    reason = "Unknown os_type profile 'nosuch'."
    assert reason in rendered
    assert reason in hand_written
    assert "element 'east-dut0' hosts[0]" in rendered
    assert "element 'east-dut0' hosts[0]" in hand_written


@pytest.mark.parametrize("fallback", [False, True])
def test_symlink_spelled_root_gets_identical_rows(tmp_path, otto_home, fallback):
    from tests.unit.templating._doctor_project import catalog, sites_entry, templated
    from tests.unit.templating._lab_project import jsonnet_source, project, write

    sut = templated(tmp_path / "sut", sites=("east", "west"), entry=sites_entry(os_type="'nosuch'"))
    projects = [sut]
    if fallback:
        entry = write(tmp_path / "sib" / "sib.jsonnet", "function(otto) error 'sibling'\n")
        projects.append(project(tmp_path / "sib", jsonnet_source(paths=[str(entry)]), name="sib"))
    alias = tmp_path / "alias"
    alias.symlink_to(sut.root)
    spelled = check_project(sut.root, catalog=catalog(*projects))
    aliased = check_project(alias, catalog=catalog(*projects))
    rows = spelled.verdict(name="lab").problems
    assert rows
    assert aliased.verdict(name="lab").problems == rows
    if fallback:  # listed per project, then every load meets the sibling's failure
        [row] = rows
        assert "sibling" in row
    else:  # one load failure per lab, each its own text
        assert [("'site-east'" in r, "'site-west'" in r) for r in rows] == [
            (True, False),
            (False, True),
        ]


def test_a_declared_unavailable_lab_reads_alike_hand_written_and_templated(tmp_path, monkeypatch):
    """One document declaring a lab unavailable, as a ``lab.json`` and as a rendered entry.

    The doctor gives both the same outcome: the lab area passes, no problem
    row, and ``otto init`` exits 0. The report's refusal tells a declaration
    from a failed source, so only the second is a problem.
    """
    from tests.unit.templating._lab_project import (
        CREDS,
        json_source,
        jsonnet_source,
        project,
        write,
    )

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    document = {
        "labs": {"bench": {}, "rig": {"unavailable": "dut: expected 2, found 1"}},
        "elements": [
            {
                "name": "dut0",
                "labs": ["bench"],
                "hosts": [{"ip": "10.0.0.1", "os_type": "unix", "creds": CREDS}],
            }
        ],
    }
    hand = write(tmp_path / "hand" / "lab" / "lab.json", document)
    entry = write(
        tmp_path / "tmpl" / "lab" / "lab.jsonnet", f"function(otto) {json.dumps(document)}\n"
    )
    roots = {
        "hand-written": project(tmp_path / "hand", json_source(hand), name="hand").root,
        "templated": project(
            tmp_path / "tmpl", jsonnet_source(paths=[str(entry)]), name="tmpl"
        ).root,
    }
    _activate(monkeypatch, *roots.values())
    outcomes = {}
    for kind, root in roots.items():
        report = check_project(root, catalog=_catalog(*roots.values()))
        result = _init(root)
        outcomes[kind] = (
            report.ok,
            report.verdict(name="lab").state,
            report.verdict(name="lab").problems,
            [w for w in report.warnings if "rig" in w],
            result.exit_code,
        )
    assert outcomes["hand-written"] == outcomes["templated"] == (True, "ok", [], [], 0)


# ── An inventory a template reads is not unused; a stale overrides entry is warned of ─


def _physical_orphans(report: DoctorReport) -> list[str]:
    """The unused-records warnings of the ``physical`` inventory."""
    return [w for w in report.warnings if w.startswith("physical:") and "referenced by no" in w]


def test_an_inventory_only_a_jsonnet_source_reads_is_not_warned_of_as_unused(tmp_path, otto_home):
    """The template reads ``physical`` (``inventories``), and no hand-written lab references it."""
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", sites=("east", "west"))
    for view in (None, catalog(sut)):
        report = check_project(sut.root, catalog=view)
        assert _physical_orphans(report) == [], view


def test_an_inventory_no_jsonnet_source_names_is_still_warned_of_as_unused(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", entry="function(otto) {}\n", inventories=[])
    report = check_project(sut.root, catalog=catalog(sut))
    [orphan] = _physical_orphans(report)
    assert "referenced by no lab file here: dut-east" in orphan


def test_a_jsonnet_read_inventory_keeps_its_other_findings(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated
    from tests.unit.templating._lab_project import CREDS, table, write

    store = write(
        tmp_path / "sut" / "lab_data" / "physical-creds.json", {"dut-east": CREDS, "x": []}
    )
    store.chmod(0o644)
    extra = table("[inventories.creds]", backend="json", path=str(store))
    sut = templated(tmp_path / "sut", extra=extra)
    report = check_project(sut.root, catalog=catalog(sut))
    assert _physical_orphans(report) == []
    text = "\n".join(w for w in report.warnings if w.startswith("physical:"))
    assert "1 key(s) the inventory does not hold: x" in text
    assert "0644" in text


def _jsonnet_read_physical(root: Path, *, backend_table: str) -> Path:
    """A project whose ``physical`` inventory is *backend_table* and a source that reads it."""
    from tests.unit.templating._lab_project import jsonnet_source, project, write

    entry = write(root / "lab_data" / "e.jsonnet", "function(otto) {}\n")
    settings = backend_table + jsonnet_source(paths=[str(entry)], inventories=["physical"])
    return project(root, settings).root


def test_a_jsonnet_read_inventory_that_fails_to_list_still_warns_of_it(tmp_path, otto_home):
    from tests.unit.templating._lab_project import table, write

    bad = write(tmp_path / "sut" / "lab_data" / "physical.json", "{not valid json")
    root = _jsonnet_read_physical(
        tmp_path / "sut",
        backend_table=table("[[inventories]]", name="physical", backend="json", path=str(bad)),
    )
    report = check_project(root)
    assert any("physical:" in w and "could not list records" in w for w in report.warnings)


def test_a_jsonnet_read_inventory_still_reports_its_stale_snapshot(tmp_path, otto_home):
    from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device
    from tests.unit.templating._lab_project import table

    monkey = pytest.MonkeyPatch()
    monkey.setenv("NETBOX_TOKEN", TOKEN)
    try:
        with NetBoxStub([device(1, "nb1")]) as stub:
            root = _jsonnet_read_physical(
                tmp_path / "sut",
                backend_table=table(
                    "[[inventories]]",
                    name="physical",
                    backend="netbox",
                    url=stub.base,
                    cache_ttl="24h",
                ),
            )
            assert not [w for w in check_project(root).warnings if "unreachable" in w]
        _age_the_snapshot(otto_home, hours=31)
        report = check_project(root)
    finally:
        monkey.undo()
    stale = [w for w in report.warnings if w.startswith("physical:") and "unreachable" in w]
    assert len(stale) == 1, report.warnings
    assert "31h old" in stale[0]
    assert not [w for w in report.warnings if "referenced by no" in w]


def _stale_overrides(report: DoctorReport) -> list[str]:
    return [w for w in report.warnings if "overrides" in w and "no element" in w]


def test_a_stale_overrides_entry_is_warned_of_naming_source_and_entry(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", overrides=["east-dut0", "gone-dut9"])
    report = check_project(sut.root, catalog=catalog(sut))
    assert report.ok, _text(report)
    [warning] = _stale_overrides(report)
    assert warning.startswith("source sut/jsonnet#1: ")
    assert "'gone-dut9'" in warning
    assert "east-dut0" not in warning


def test_judging_overrides_evaluates_no_template_again(tmp_path, otto_home, monkeypatch):
    from tests.unit.templating._doctor_project import catalog, templated
    from tests.unit.templating._render_helpers import count_evaluations

    plain = templated(tmp_path / "plain", entry=None)
    judged = templated(tmp_path / "judged", overrides=["east-dut0", "gone-dut9"])
    evaluations = count_evaluations(monkeypatch)
    check_project(plain.root, catalog=catalog(plain))
    without = evaluations()
    check_project(judged.root, catalog=catalog(judged))
    assert without > 0
    assert evaluations() - without == without


def test_an_overrides_entry_spelled_differently_by_slug_rules_is_not_stale(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", overrides=["East_DUT0"])
    report = check_project(sut.root, catalog=catalog(sut))
    assert _stale_overrides(report) == []


def test_a_source_that_failed_never_makes_an_overrides_entry_stale(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    entry = "function(otto)\n  { labs: error 'boom' }\n"
    sut = templated(tmp_path / "sut", entry=entry, overrides=["gone-dut9"])
    report = check_project(sut.root, catalog=catalog(sut))
    assert not report.ok
    assert _stale_overrides(report) == []


def test_a_refusing_catalog_never_makes_an_overrides_entry_stale(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", overrides=["gone-dut9"])
    (otto_home / "settings.toml").write_text("not valid toml [[[")  # scaffold-exempt: refusal
    for view in (None, catalog(sut)):
        report = check_project(sut.root, catalog=view)
        assert _stale_overrides(report) == [], view


def test_each_jsonnet_source_is_judged_by_its_own_elements(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated
    from tests.unit.templating._lab_project import CREDS, jsonnet_source, write

    other = {
        "labs": {"solo": {}},
        "elements": [
            {
                "name": "solo-1",
                "labs": ["solo"],
                "hosts": [{"ip": "10.9.9.9", "os_type": "unix", "creds": CREDS}],
            }
        ],
    }
    second = write(
        tmp_path / "sut" / "lab_data" / "other.jsonnet", f"function(otto) {json.dumps(other)}\n"
    )
    extra = jsonnet_source(paths=[str(second)], overrides=["east-dut0"])
    sut = templated(tmp_path / "sut", extra=extra, overrides=["solo-1"])
    report = check_project(sut.root, catalog=catalog(sut))
    assert report.ok, _text(report)
    stale = _stale_overrides(report)
    assert len(stale) == 2, stale
    assert sum("'solo-1'" in w for w in stale) == 1
    assert sum("'east-dut0'" in w for w in stale) == 1


def test_two_spellings_of_one_stale_overrides_entry_are_one_warning(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog, templated

    sut = templated(tmp_path / "sut", overrides=["Gone-Dut9", "gone_dut9"])
    report = check_project(sut.root, catalog=catalog(sut))
    [warning] = _stale_overrides(report)
    assert "'Gone-Dut9'" in warning
    assert "gone_dut9" not in warning


def _manual_source(root: Path, *entries: "tuple[str, str]", **source) -> Path:
    """A project over ``physical`` whose one jsonnet source renders *entries* (name, text)."""
    from tests.unit.templating._doctor_project import records
    from tests.unit.templating._lab_project import inventory, jsonnet_source, project, write

    paths = [str(write(root / "lab_data" / name, text)) for name, text in entries]
    settings = inventory(root, "physical", records("east")) + jsonnet_source(
        paths=paths, inventories=["physical"], **source
    )
    return project(root, settings).root


def test_a_lab_the_declaration_marks_unavailable_still_judges_overrides(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog
    from tests.unit.templating._lab_project import CREDS

    document = {
        "labs": {"rig": {"unavailable": "dut: expected 2, found 1"}},
        "elements": [
            {
                "name": "rig-1",
                "labs": ["rig"],
                "hosts": [{"ip": "10.0.0.1", "os_type": "unix", "creds": CREDS}],
            }
        ],
    }
    root = _manual_source(
        tmp_path / "sut",
        ("rig.jsonnet", f"function(otto) {json.dumps(document)}\n"),
        overrides=["rig-1", "gone-9"],
    )
    from otto.config.project import Project

    report = check_project(root, catalog=catalog(Project(root=root)))
    assert report.ok, _text(report)
    [warning] = _stale_overrides(report)
    assert "'gone-9'" in warning
    assert "rig-1" not in warning


def test_a_source_with_one_failing_entry_file_gets_no_stale_overrides_warning(tmp_path, otto_home):
    from tests.unit.templating._doctor_project import catalog

    good = {"labs": {"solo": {}}, "elements": []}
    root = _manual_source(
        tmp_path / "sut",
        ("good.jsonnet", f"function(otto) {json.dumps(good)}\n"),
        ("bad.jsonnet", "function(otto) error 'boom'\n"),
        overrides=["gone-9"],
    )
    from otto.config.project import Project

    report = check_project(root, catalog=catalog(Project(root=root)))
    assert not report.ok
    assert _stale_overrides(report) == []


@pytest.mark.parametrize("scoped", [False, True])
def test_a_source_whose_read_has_a_problem_gets_no_stale_overrides_warning(
    tmp_path, otto_home, scoped
):
    """An element twice in one entry: a problem of the read, so no entry can be judged."""
    from otto.config.project import Project
    from tests.unit.templating._doctor_project import catalog
    from tests.unit.templating._lab_project import CREDS

    host = {"ip": "10.0.0.1", "os_type": "unix", "creds": CREDS}
    twice = {
        "labs": {"solo": {}},
        "elements": [{"name": "dup", "labs": ["solo"], "hosts": [host]}] * 2,
    }
    keys = {"scope": {"names": ["solo"]}} if scoped else {}
    root = _manual_source(
        tmp_path / "sut",
        ("twice.jsonnet", f"function(otto) {json.dumps(twice)}\n"),
        overrides=["gone-9"],
        **keys,
    )
    report = check_project(root, catalog=catalog(Project(root=root)))
    assert not report.ok
    assert _stale_overrides(report) == []


def _broken_sibling(root: Path, kind: str) -> Path:
    """An active sibling: broken as *kind* names, or ``healthy`` and referencing nothing."""
    tables = '[os_profiles.bad]\nbase = "nonexistent"\n' if kind == "settings" else ""
    paths = "" if kind == "unprepared" else 'paths = ["lab"]\n'
    sources = f'\n[[lab.sources]]\nbackend = "json"\n{paths}'
    files = {"lab/lab.json": "{ this is not json" if kind == "not-json" else "{}"}
    return make_project(root, name="proj-a", extra=tables + sources, files=files)


@pytest.mark.parametrize("kind", ["settings", "unprepared", "not-json"])
def test_a_broken_sibling_leaves_the_checked_project_alone(tmp_path, monkeypatch, kind):
    """B's whole report with a broken sibling equals its report beside a healthy, silent one.

    Per case: ``unprepared`` (a json source without ``paths``) is the one that
    raises ``ValueError`` in the sibling's lab parse, which
    ``_sibling_references`` catches; ``settings`` (a data profile over an
    unknown base, so the sibling's own doctor is ``blocked``) and ``not-json``
    (a lab file that is not JSON, which the source read reports as a problem)
    never raise there and are read as no references.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    inventory = '[inventory]\nbackend = "json"\npath = "inventory.json"\nsupplies = ["ip"]\n'
    b = _named_project(
        tmp_path / "B",
        name="proj-b",
        tables=inventory,
        records={"inventory.json": {"k": {"ip": "10.0.0.1"}}},
        references=["k"],
    )
    healthy = _broken_sibling(tmp_path / "H", "healthy")
    broken = _broken_sibling(tmp_path / "A", kind)
    expected = check_project(b, catalog=_catalog(healthy, b))
    assert expected.ok, _text(expected)
    assert check_project(b, catalog=_catalog(broken, b)) == expected


def test_doctor_uses_canonical_placement(monkeypatch):
    """The doctor loads a lab with its containers placed over the whole lab, not a run's scope."""
    from otto.init import doctor

    calls = []
    monkeypatch.setattr(
        "otto.session.lab.build_lab", lambda projects, **kw: calls.append(kw) or None
    )
    view = object()
    assert doctor._load_problem([], name="bench", view=view) == []  # ty: ignore[invalid-argument-type]
    assert calls == [{"labs": ["bench"], "view": view, "_unscoped_placement": True}]
