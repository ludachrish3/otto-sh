"""The generated lab schema validates real lab data and rejects bad data."""

import json

import pytest
from jsonschema import Draft202012Validator

from otto.models.jsonschema import build_schemas
from tests._fixtures.labdata import lab_data_dir, lab_json_v2

_LAB_DATA = lab_data_dir()
_LAB_FILES = sorted(_LAB_DATA.glob("*/lab.json"))

# A flat v1-style host dict: `lab_json_v2` hoists `element` onto the element
# wrapper, so what reaches `elements[…].hosts[…]` is the v2 host entry.
_VALID_HOST = {
    "ip": "10.10.200.11",
    "element": "test1",
    "os_type": "unix",
    "creds": [{"login": "vagrant", "password": "vagrant"}],
}

_VALID_LINK = {
    "endpoints": [{"host": "test1"}, {"host": "test2"}],
    "protocol": "udp",
}


@pytest.fixture(scope="module")
def lab_validator() -> Draft202012Validator:
    schema = build_schemas()["lab"]
    Draft202012Validator.check_schema(schema)  # the schema itself is well-formed
    return Draft202012Validator(schema)


def test_lab_data_fixtures_exist():
    assert _LAB_FILES, "expected at least one tests/_fixtures/lab_data/*/lab.json fixture"


@pytest.mark.parametrize("lab_file", _LAB_FILES, ids=lambda p: p.parent.name)
def test_real_lab_json_validates(lab_validator, lab_file):
    lab = json.loads(lab_file.read_text())
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_unknown_host_key_is_rejected(lab_validator):
    bad = lab_json_v2([_VALID_HOST], [_VALID_LINK])
    bad["elements"][0]["hosts"][0]["totally_unknown_key"] = "x"
    assert list(lab_validator.iter_errors(bad)), "unknown key should fail validation"


def test_lab_object_with_elements_links_and_comment_validates(lab_validator):
    lab = {**lab_json_v2([_VALID_HOST], [_VALID_LINK]), "_comment": "x"}
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_unknown_top_level_key_is_rejected(lab_validator):
    assert list(lab_validator.iter_errors({"routes": []}))


def test_v1_document_is_rejected(lab_validator):
    """A v1 file's top-level ``hosts`` is not a permitted property in v2.

    The hard cutover (spec §11) is a parse error at the loader; the schema is
    the editor-side half of it, so an unmigrated file must squiggle on the very
    key that moved rather than validate vacuously.
    """
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError, match=r"'hosts' does not match any of the regexes"):
        lab_validator.validate({"hosts": []})


def test_interface_string_shorthand_validates(lab_validator):
    lab = lab_json_v2([{**_VALID_HOST, "interfaces": {"eth0": "10.0.0.5"}}])
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_interface_object_form_validates(lab_validator):
    lab = lab_json_v2([{**_VALID_HOST, "interfaces": {"eth0": {"ip": "10.0.0.5"}}}])
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_lab_schema_accepts_scaffolded_lab_json(lab_validator, tmp_path):
    """The very file `otto init` writes must validate against the emitted schema."""
    from otto.cli.init import AREAS, InitConfig

    lab_area = next(a for a in AREAS if a.name == "lab")
    lab_area.scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    doc = json.loads((tmp_path / "lab_data" / "lab.json").read_text())
    lab_validator.validate(doc)  # $schema + top-level/_ and host-level _comment


def test_lab_schema_accepts_comment_keys_at_every_level(lab_validator):
    """Every layer whose model strips ``_`` keys must accept them in the schema.

    Document root, the ``labs`` table, the element entry, the host entry and
    the link entry each drop ``_``-prefixed keys before validation, so none of
    them may squiggle the JSON comment idiom `otto init` itself scaffolds.
    """
    doc = lab_json_v2(
        [{**_VALID_HOST, "_note": "runtime strips me"}], [{**_VALID_LINK, "_note": "x"}]
    )
    doc["$schema"] = "../.otto/schemas/lab.schema.json"
    doc["_comment"] = "document level"
    doc["labs"]["_comment"] = "labs-table level"
    doc["elements"][0]["_note"] = "element level"
    lab_validator.validate(doc)


def test_lab_schema_still_rejects_unknown_top_level_key(lab_validator):
    from jsonschema.exceptions import ValidationError

    doc = {**lab_json_v2([_VALID_HOST]), "routes": []}
    # The unknown key itself must be the rejection (the schema admits only
    # declared keys plus ^_ comment keys) — not some other shape complaint.
    with pytest.raises(ValidationError, match=r"'routes' does not match any of the regexes"):
        lab_validator.validate(doc)


def test_standalone_host_and_link_schemas_accept_comment_keys():
    from jsonschema import Draft202012Validator

    docs = build_schemas()
    Draft202012Validator(docs["unix-host"]).validate({**_VALID_HOST, "_note": "x"})
    Draft202012Validator(docs["link"]).validate({**_VALID_LINK, "_note": "x"})


# ── A referenced host entry (spec 2026-08-28 host-inventory §5) ──────────────
#
# Every entry below carries ``sw_version``, which ONLY ``UnixHostSpec``
# declares. That pins which arm of the host ``anyOf`` decides the verdict:
# ``os_type`` carries no enum in the generated schema (the ``discriminator``
# keyword is OpenAPI, and Draft 2020-12 ignores it), so a bare
# ``"os_type": "unix"`` entry is ALSO checked against ``EmbeddedHostSpec``,
# whose looser ``required`` would answer for it. A test written that way
# would pass with the unix arm broken.


def test_referenced_entry_needs_neither_ip_nor_creds(lab_validator):
    """``"inventory": "<key>"`` replaces the fields the record fills.

    ``lab.json`` is a superset of what ``UnixHostSpec`` validates: the loader
    joins the record on before the spec sees the dict, so an editor must not
    red-underline an entry otto loads perfectly.
    """
    lab = lab_json_v2([{"inventory": "test1", "element": "test1", "sw_version": "1.0"}])
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_an_entry_with_neither_an_address_nor_a_reference_is_still_rejected(lab_validator):
    """The relaxation is a CHOICE, not a dropped requirement.

    Dropping ``ip``/``creds`` from ``required`` without restoring them as an
    ``anyOf`` arm would make this document validate — the failure mode the
    arm exists to prevent.
    """
    lab = lab_json_v2([{"element": "test1", "sw_version": "1.0"}])
    assert list(lab_validator.iter_errors(lab)), (
        "an entry with neither 'ip' nor 'inventory' must fail"
    )


def test_an_inline_entry_still_needs_every_field_it_always_needed(lab_validator):
    """The restored arm is the FULL original ``required`` list, not just ``ip``.

    ``UnixHostSpec`` requires ``ip`` AND ``creds``; an arm naming only ``ip``
    would let this credential-less inline entry through.
    """
    lab = lab_json_v2([{"ip": "10.0.0.1", "element": "test1", "sw_version": "1.0"}])
    assert list(lab_validator.iter_errors(lab)), "an inline entry without creds must fail"


def test_the_reference_arm_constrains_the_value_not_just_the_key(lab_validator):
    """``required`` means PRESENT — the arm must also demand a non-empty string.

    Both rejected documents are ones otto REFUSES at load, and both FAILED
    this schema (on the missing ``ip``) before the relaxation existed, so an
    arm keyed on presence alone would widen a hole rather than open the
    intended one. ``null`` is "references nothing" (R7), which makes the
    entry inline and still owing an ``ip``; ``""`` is an ``InventoryError``.
    """
    creds = [{"login": "u", "password": "p"}]
    null_no_ip = lab_json_v2([{"inventory": None, "element": "test1", "sw_version": "1.0"}])
    assert list(lab_validator.iter_errors(null_no_ip)), "'inventory': null is not a reference"

    empty_key = lab_json_v2([{"inventory": "", "element": "test1", "sw_version": "1.0"}])
    assert list(lab_validator.iter_errors(empty_key)), "'inventory': '' is not a key"

    # ...and the other direction, so the constraint cannot be satisfied by
    # simply banning `null`: a NULL reference on a fully inline entry is the
    # schema-legal round-trip R7 exists for, and must still validate.
    null_inline = lab_json_v2(
        [
            {
                "inventory": None,
                "ip": "10.0.0.1",
                "creds": creds,
                "element": "test1",
                "sw_version": "1.0",
            }
        ]
    )
    errors = list(lab_validator.iter_errors(null_inline))
    assert errors == [], [e.message for e in errors]
