"""The generated lab schema validates real lab data and rejects bad data."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from otto.models.jsonschema import build_schemas
from tests._fixtures.labdata import lab_data_dir

_LAB_DATA = lab_data_dir()
_LAB_FILES = sorted(_LAB_DATA.glob("*/lab.json"))

_VALID_HOST = {
    "ip": "10.10.200.11",
    "element": "carrot",
    "os_type": "unix",
    "creds": [{"login": "vagrant", "password": "vagrant"}],
}

_VALID_LINK = {
    "endpoints": [{"host": "carrot"}, {"host": "tomato"}],
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
    lab = json.loads(_LAB_FILES[0].read_text())
    bad = copy.deepcopy(lab)
    bad["hosts"][0]["totally_unknown_key"] = "x"
    assert list(lab_validator.iter_errors(bad)), "unknown key should fail validation"


def test_lab_object_with_hosts_links_and_comment_validates(lab_validator):
    lab = {"hosts": [_VALID_HOST], "links": [_VALID_LINK], "_comment": "x"}
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_unknown_top_level_key_is_rejected(lab_validator):
    assert list(lab_validator.iter_errors({"routes": []}))


def test_interface_string_shorthand_validates(lab_validator):
    lab = {"hosts": [{**_VALID_HOST, "interfaces": {"eth0": "10.0.0.5"}}]}
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]


def test_interface_object_form_validates(lab_validator):
    lab = {"hosts": [{**_VALID_HOST, "interfaces": {"eth0": {"ip": "10.0.0.5"}}}]}
    errors = list(lab_validator.iter_errors(lab))
    assert errors == [], [e.message for e in errors]
