"""The generated hosts schema validates real lab data and rejects bad data."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from otto.models.jsonschema import build_schemas
from tests._fixtures.labdata import lab_data_dir

_LAB_DATA = lab_data_dir()
_LAB_FILES = sorted(_LAB_DATA.glob("*/lab.json"))


@pytest.fixture(scope="module")
def hosts_validator() -> Draft202012Validator:
    schema = build_schemas()["hosts"]
    Draft202012Validator.check_schema(schema)  # the schema itself is well-formed
    return Draft202012Validator(schema)


def test_lab_data_fixtures_exist():
    assert _LAB_FILES, "expected at least one tests/_fixtures/lab_data/*/lab.json fixture"


@pytest.mark.parametrize("lab_file", _LAB_FILES, ids=lambda p: p.parent.name)
def test_real_hosts_json_validates(hosts_validator, lab_file):
    hosts = json.loads(lab_file.read_text())["hosts"]
    errors = list(hosts_validator.iter_errors(hosts))
    assert errors == [], [e.message for e in errors]


def test_unknown_host_key_is_rejected(hosts_validator):
    hosts = json.loads(_LAB_FILES[0].read_text())["hosts"]
    bad = copy.deepcopy(hosts)
    bad[0]["totally_unknown_key"] = "x"
    assert list(hosts_validator.iter_errors(bad)), "unknown key should fail validation"
