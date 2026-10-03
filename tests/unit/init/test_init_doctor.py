"""The ``otto init`` doctor checks existing areas via real ingestion code — never rewrites.

Every check runs against :func:`otto.init.check_repo` directly; how the leaf
renders a :class:`~otto.init.DoctorReport` is pinned in
``tests/unit/cli/test_init_render.py``.
"""

import json
import shutil
from pathlib import Path

import pytest

from otto.init import (
    AreaVerdict,
    DoctorReport,
    InitConfig,
    InitInputError,
    ScaffoldReport,
    check_repo,
    detect_areas,
    scaffold,
    scaffold_candidates,
)
from tests._fixtures.sutrepo import make_sut_repo


def _scaffold(root: Path, *areas: str) -> ScaffoldReport:
    return scaffold(InitConfig(root, "widget", "0.1.0"), list(areas))


def _scaffold_all(tmp_path: Path) -> None:
    """Scaffold every area an all-areas run offers, as ``otto init --all`` does."""
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), scaffold_candidates(tmp_path, all_areas=True))


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
    """Point the scaffolded repo's ONE json source at every lab file in lab_data/.

    A multi-file source is what the ``paths`` globs make ordinary, and it is
    the only shape in which an in-source duplicate can exist at all. A split
    lab globs its LAB files, never every JSON beside them: inventory.json and
    creds.json live in the same directory, and a bare ``*.json`` would feed
    them to the lab parser as "unknown section(s)" — a real problem, but not
    the one these tests are pinning.
    """
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: retargeting a product-scaffolded source
        settings.read_text().replace('paths = ["lab_data"]', 'paths = ["lab_data/lab*.json"]')
    )
    return tmp_path / "lab_data"


def test_duplicate_lab_declaration_across_files_of_one_source_fails(tmp_path: Path) -> None:
    """The doctor must refuse what the loader refuses — one source, one declaration.

    Otherwise `otto init` reports ✓ on a repo where `otto --lab example_lab`
    dies at load, which is exactly the drift routing the doctor through the
    loader's own code exists to prevent.
    """
    _scaffold_all(tmp_path)
    lab_dir = _glob_one_source(tmp_path)
    (lab_dir / "lab_more.json").write_text(json.dumps({"labs": {"example_lab": {}}}))
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
    assert not report.ok
    assert "user was removed" in _text(report)
    assert "SECRET_XYZ" not in _text(report)


def test_valid_repo_reports_all_ok(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert [(v.name, v.state) for v in report.verdicts] == [
        ("settings", "ok"),
        ("schemas", "ok"),
        ("lab", "ok"),
        ("tests", "ok"),
        ("instructions", "ok"),
        ("kmodcov", "absent"),
    ]
    assert report.warnings == []  # spec 2026-09-06 §8.1: a fresh scaffold warns of nothing


def test_broken_settings_key_fails_with_pydantic_error(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: in-place corruption of a product-scaffolded file
        settings.read_text().replace("version =", "verzion =")
    )
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert "verzion" in _text(report)


def test_invalid_host_field_fails_named(tmp_path: Path) -> None:
    """A bad host field is located by ELEMENT and index, not by a flat host number.

    v2 has no top-level host array to index into, so "hosts[3]" alone would
    no longer tell the author which entry to open.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(lab_file.read_text().replace('"os_type"', '"os_typo"'))
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
    assert not report.ok
    assert "elements[0]" in _text(report)
    assert "hosts.1" in _text(report)
    assert "Input should be a valid dictionary" in _text(report)


def test_v1_lab_file_reports_the_migration_hint(tmp_path: Path) -> None:
    """A repo still on the top-level ``hosts`` array is told where hosts moved.

    The hard cutover (spec §11) means the doctor must not shrug at a v1 file:
    it is the surface a user upgrading otto meets first, and it reports the
    loader's own migration hint rather than a generic parse failure.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_text(json.dumps({"hosts": []}))
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
    assert not report.ok
    assert "links[0]" in _text(report)


def test_valid_link_entry_passes(tmp_path: Path) -> None:
    """A well-formed links[] entry validates clean alongside the example host."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    data = json.loads(lab_file.read_text())
    data["links"].append({"endpoints": [{"host": "example-device"}, {"host": "other-device"}]})
    lab_file.write_text(json.dumps(data))
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
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
    verdict = check_repo(tmp_path).verdict("instructions")
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
    return "\n".join(check_repo(tmp_path).verdict("schemas").problems)


def test_schemas_validate_green_after_scaffold_and_reformat(tmp_path: Path) -> None:
    out = _write_schemas(tmp_path)
    assert check_repo(tmp_path).verdict("schemas").state == "ok"
    # reformat-only change stays green: comparison is structural, not bytes
    lab = out / "lab.schema.json"
    lab.write_text(json.dumps(json.loads(lab.read_text()), indent=4, sort_keys=True))
    assert check_repo(tmp_path).verdict("schemas").state == "ok"


def test_schemas_validate_flags_stale_missing_orphaned(tmp_path: Path) -> None:
    out = _write_schemas(tmp_path)
    stale = json.loads((out / "lab.schema.json").read_text())
    stale["title"] = "tampered"
    (out / "lab.schema.json").write_text(json.dumps(stale))
    (out / "settings.schema.json").unlink()
    (out / "ghost.schema.json").write_text("{}")
    verdict = check_repo(tmp_path).verdict("schemas")
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
    settings.write_text("".join(out))  # sutrepo-exempt: editing a product-scaffolded settings file


def test_dead_reference_is_a_problem_naming_key_and_label(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _point_first_host_at(tmp_path, "ghost")
    report = check_repo(tmp_path)
    assert not report.ok
    assert "hosts[0]" in _text(report)
    assert "key 'ghost' not found in inventory 'json:" in _text(report)


def test_referenced_entry_with_no_inventory_is_a_problem_naming_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _drop_table(tmp_path, "inventory")
    _drop_table(tmp_path, "creds")  # a lone [creds] would be the other error
    report = check_repo(tmp_path)
    assert not report.ok
    assert "no inventory is configured" in _text(report)
    assert "~/.otto/settings.toml" in _text(report)


def test_orphan_records_warn_and_the_label_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    inv = _set_inventory_records(
        tmp_path, {"device-01.lab.example": {"ip": "10.0.0.1"}, "spare": {"ip": "10.0.0.2"}}
    )
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert report.inventory_label == f"json:{inv}"
    assert report.warnings
    assert "1 record(s) referenced by no lab file here: spare" in _text(report)


def test_world_readable_creds_file_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    (tmp_path / "lab_data" / "creds.json").chmod(0o644)
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert "creds store file" in _text(report)
    assert "0644" in _text(report)
    assert "make it 0600" in _text(report)


def test_broken_user_inventory_settings_is_a_problem_not_a_traceback(tmp_path, monkeypatch):
    """A broken ``~/.otto/settings.toml`` is a named problem — the doctor never raises.

    The repo already declares its OWN ``[inventory]``/``[creds]`` (the
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
    user_settings.write_text("not valid toml [[[")  # sutrepo-exempt: malformed TOML under test
    report = check_repo(tmp_path)
    assert not report.ok
    problems = "\n".join(report.verdict("lab").problems)
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
    report = check_repo(tmp_path)
    assert not report.ok
    assert "not-a-real-os-type" in _text(report)
    assert "is not a registered profile" in _text(report)


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
    settings.write_text(  # sutrepo-exempt: breaking the scaffolded [inventory] on purpose
        settings.read_text().replace('path = "lab_data/inventory.json"\n', "")
    )
    report = check_repo(tmp_path)
    assert not report.ok
    assert "requires a 'path' string" in _text(report)
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
        with settings.open("a") as f:  # sutrepo-exempt: declaring [inventory] post-scaffold
            f.write(f'\n[inventory]\nbackend = "netbox"\nurl = "{stub.base}"\ncache_ttl = "24h"\n')
        primed = check_repo(tmp_path)
        assert primed.ok, _text(primed)
        assert "unreachable" not in _text(primed), "a live fetch must not report staleness"
    _age_the_snapshot(home, hours=31)

    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert report.warnings
    assert "could not list records" in _text(report)


def test_inventory_for_memoises_a_resolved_inventory_per_root(tmp_path, monkeypatch):
    """The SAME cache, asked twice for the same root, returns the SAME object.

    One ``check_repo`` asks ``_inventory_for`` up to three times (the "lab"
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


def test_one_check_repo_constructs_the_inventory_only_once(tmp_path, monkeypatch):
    """End-to-end proof of the cache: one ``check_repo`` builds the inventory ONCE.

    A check asks ``_inventory_for`` up to three times (the "lab" area's own
    validation, the warnings pass, and the labels); this counts the actual
    construction call, not just ``_inventory_for``'s own cache, so it also
    proves every one of those askers is handed the call's one cache — any
    that were not would count the construction again.
    """
    import otto.inventory as otto_inventory

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)

    real = otto_inventory.build_inventory_from_declarations
    calls: list[int] = []

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(otto_inventory, "build_inventory_from_declarations", _counting)
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert len(calls) == 1


def test_orphan_creds_warn_and_the_store_label_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _set_creds(tmp_path, {"device-01.lab.example": [{"login": "u", "password": "p"}], "stale": []})
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert report.inventory_label == f"json:{tmp_path / 'lab_data' / 'inventory.json'}"
    assert report.creds_label == f"json:{tmp_path / 'lab_data' / 'creds.json'}"
    assert "1 key(s) the inventory does not hold: stale" in _text(report)


def test_creds_without_an_inventory_is_a_problem_naming_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _scaffold_all(tmp_path)
    _drop_table(tmp_path, "inventory")
    report = check_repo(tmp_path)
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
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert f"{creds} does not exist" in _text(report)
    assert "CredsError" not in _text(report)


def test_all_never_scaffolds_the_kmodcov_area(tmp_path: Path) -> None:
    assert "kmodcov" not in scaffold_candidates(tmp_path, all_areas=True)
    _scaffold_all(tmp_path)
    assert not (tmp_path / "third_party").exists()
    assert check_repo(tmp_path).verdict("kmodcov").state == "absent"


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
    scaffold(InitConfig(tmp_path, "widget", "0.1.0", kmodcov_dir="vendor/kmodcov"), ["kmodcov"])
    assert (tmp_path / "vendor" / "kmodcov" / "kmodcov.h").is_file()


def test_a_differing_vendored_copy_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    _scaffold(tmp_path, "kmodcov")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: appends to a settings.toml otto init itself scaffolded
        settings.read_text() + '\n[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "third_party/otto_kmodcov"\n'
        'match = { id = ".*" }\n'
    )
    (tmp_path / "third_party" / "otto_kmodcov" / "kmodcov.c").write_text("// edited\n")
    report = check_repo(tmp_path)
    assert report.ok, _text(report)
    assert report.verdict("kmodcov").state == "ok"
    (warning,) = [w for w in report.warnings if "kmodcov.c" in w]
    assert "otto cov kmodcov export" in warning


def test_a_declared_source_with_no_library_fails_the_kmodcov_area(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # sutrepo-exempt: appends to a settings.toml otto init itself scaffolded
        settings.read_text() + '\n[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
        'artifact = "build/otto_kmodcov.ko"\nsource = "vendor/missing"\nmatch = { id = ".*" }\n'
    )
    report = check_repo(tmp_path)
    assert not report.ok
    assert report.verdict("kmodcov").state == "failed"
    assert "vendor/missing" in _text(report)


def _settings(root: Path, body: str) -> None:
    make_sut_repo(root, name="acme", version="1.0.0", extra=body)


def test_a_json_source_without_paths_fails_settings_and_blocks_lab(tmp_path: Path) -> None:
    _settings(tmp_path, '[[lab.sources]]\nbackend = "json"\n')
    # Detected even though no lab file exists: a declaration that does not
    # compile counts as present, so nothing is ever scaffolded beside it.
    assert "lab" in detect_areas(tmp_path)
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert report.verdict("lab") == AreaVerdict("lab", "blocked", detail="settings did not compile")
    assert not report.ok


@pytest.mark.parametrize("line", ['libs = "pylib"\n', 'tests = "tests"\n'], ids=["libs", "tests"])
def test_a_non_list_libs_or_tests_fails_settings_and_never_raises(
    tmp_path: Path, line: str
) -> None:
    """A scalar where the settings want a list is the settings area's problem, not a KeyError."""
    _settings(tmp_path, line)
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"


def test_a_non_string_init_entry_fails_settings_once_and_blocks_instructions(
    tmp_path: Path,
) -> None:
    """``init = [1, 2]`` is one settings problem, not a second "init module 1 not found"."""
    _settings(tmp_path, "init = [1, 2]\n")
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert report.verdict("instructions") == AreaVerdict(
        "instructions", "blocked", detail="settings did not compile"
    )


def test_a_non_list_init_fails_settings_once_and_blocks_instructions(tmp_path: Path) -> None:
    """``init = "mod"`` is the settings area's problem; instructions does not repeat it."""
    _settings(tmp_path, 'init = "acme_instructions"\n')
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert report.verdict("instructions") == AreaVerdict(
        "instructions", "blocked", detail="settings did not compile"
    )


def test_an_unreadable_test_file_is_a_problem_never_raises(tmp_path: Path) -> None:
    """A ``test_*.py`` glob lists but nothing can read — dangling link, directory — fails tests."""
    _scaffold_all(tmp_path)
    tests_dir = tmp_path / "tests"
    (tests_dir / "test_dangling.py").symlink_to(tmp_path / "nowhere.py")
    (tests_dir / "test_a_dir.py").mkdir()
    problems = "\n".join(check_repo(tmp_path).verdict("tests").problems)
    assert "test_dangling.py" in problems
    assert "test_a_dir.py" in problems


def test_a_lab_file_with_a_utf8_bom_fails_as_the_loader_refuses_it(tmp_path: Path) -> None:
    """The loader reads lab files as text and ``json.load`` refuses a BOM — so must the doctor."""
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_bytes(b"\xef\xbb\xbf" + lab_file.read_bytes())
    verdict = check_repo(tmp_path).verdict("lab")
    assert verdict.state == "failed"
    assert str(lab_file) in "\n".join(verdict.problems)


def test_a_typod_os_profile_default_fails_settings_and_registers_nothing(tmp_path: Path) -> None:
    from otto.host.os_profile import OS_PROFILES

    before = sorted(OS_PROFILES.names())
    _settings(tmp_path, '[os_profiles.acme-os]\nbase = "unix"\nosTyp = "unix"\n')
    report = check_repo(tmp_path)
    assert "unknown default field" in "\n".join(report.verdict("settings").problems)
    assert sorted(OS_PROFILES.names()) == before


def test_unparsable_settings_fail_once_and_block_lab_and_instructions(tmp_path: Path) -> None:
    (tmp_path / ".otto").mkdir()
    (tmp_path / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML under test
        'name = "acme\n'
    )
    report = check_repo(tmp_path)
    settings = report.verdict("settings")
    assert settings.state == "failed"
    assert len(settings.problems) == 1
    assert report.verdict("lab").state == "blocked"
    assert report.verdict("instructions").state == "blocked"
    assert _text(report).count(str(tmp_path / ".otto" / "settings.toml")) == 1


def test_a_settings_file_that_is_not_utf8_fails_settings_and_never_raises(tmp_path: Path) -> None:
    """TOML is UTF-8: an undecodable file is the settings area's one problem, not a traceback.

    Every tolerant reader the other areas ask must read it as unreadable too,
    or the doctor raises out of the first one that decodes it.
    """
    (tmp_path / ".otto").mkdir()
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_bytes(b'name = "\xff"\n')  # sutrepo-exempt: non-UTF-8 settings under test
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert report.verdict("lab").state == "blocked"
    assert report.verdict("instructions").state == "blocked"


def test_undecodable_lab_and_test_files_are_problems_never_raises(tmp_path: Path) -> None:
    """A lab file or test file the doctor cannot decode fails its area; it never raises.

    The loader cannot read either one, so each is a problem naming the file —
    not a ``UnicodeDecodeError`` (or, for a NUL byte, ``ast.parse``'s
    ``ValueError``) out of :func:`check_repo`.
    """
    _scaffold_all(tmp_path)
    lab_file = tmp_path / "lab_data" / "lab.json"
    lab_file.write_bytes(b'{"labs": "\xff"}')
    (tmp_path / "tests" / "test_bad_bytes.py").write_bytes(b'x = "\xff"\n')
    (tmp_path / "tests" / "test_nul.py").write_bytes(b"x = 1\x00\n")
    report = check_repo(tmp_path)
    assert report.verdict("lab").state == "failed"
    assert str(lab_file) in "\n".join(report.verdict("lab").problems)
    tests = "\n".join(report.verdict("tests").problems)
    assert "test_bad_bytes.py" in tests
    assert "test_nul.py" in tests


@pytest.mark.parametrize("init_line", ["", "init = []\n"])
def test_no_declared_init_is_absent_and_ok(tmp_path: Path, init_line: str) -> None:
    _settings(tmp_path, init_line)
    report = check_repo(tmp_path)
    assert report.verdict("instructions") == AreaVerdict(
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
    assert check_repo(tmp_path).verdict("instructions").state == "ok"


def test_an_init_module_on_sys_path_passes(tmp_path: Path, monkeypatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "doctor_path_only_xyz.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    _settings(tmp_path, 'libs = []\ninit = ["doctor_path_only_xyz"]\n')
    assert check_repo(tmp_path).verdict("instructions").state == "ok"


def test_a_declared_init_that_resolves_nowhere_fails(tmp_path: Path) -> None:
    _settings(tmp_path, 'libs = ["pylib"]\ninit = ["ghost_xyz"]\n')
    (tmp_path / "pylib").mkdir()
    verdict = check_repo(tmp_path).verdict("instructions")
    assert verdict.state == "failed"
    assert "ghost_xyz" in verdict.problems[-1]
    assert str(tmp_path / "pylib") in verdict.problems[-1]


def test_check_repo_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(InitInputError) as caught:
        check_repo(tmp_path / "nope")
    assert caught.value.field == "root"


def test_check_repo_never_prints(tmp_path: Path, capsys) -> None:
    _scaffold_all(tmp_path)
    capsys.readouterr()
    check_repo(tmp_path)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "line",
    [
        'libs = ["~nosuchuser_xyz/lib"]\n',
        'tests = ["~nosuchuser_xyz/tests"]\n',
        '[[lab.sources]]\nbackend = "json"\npaths = ["~nosuchuser_xyz/lab"]\n',
    ],
)
def test_a_path_under_an_unknown_users_home_fails_settings_and_never_raises(
    tmp_path: Path, line: str
) -> None:
    """``~nosuchuser`` cannot expand: the settings area reports it; no reader raises."""
    _settings(tmp_path, line)
    report = check_repo(tmp_path)
    assert report.verdict("settings").state == "failed"
    assert "~nosuchuser_xyz" in _text(report)
    detect_areas(tmp_path)
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
    report = check_repo(tmp_path)
    assert report.verdict("kmodcov").state == "failed"
    assert "~nosuchuser_xyz/kmodcov" in "\n".join(report.verdict("kmodcov").problems)
