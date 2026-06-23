"""The generated hosts schema validates real lab data and rejects bad data."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from otto.models.jsonschema import build_schemas
from tests._fixtures.labdata import lab_data_dir

_LAB_DATA = lab_data_dir()
_HOST_FILES = sorted(_LAB_DATA.glob('*/hosts.json'))


@pytest.fixture(scope='module')
def hosts_validator() -> Draft202012Validator:
    schema = build_schemas()['hosts']
    Draft202012Validator.check_schema(schema)  # the schema itself is well-formed
    return Draft202012Validator(schema)


def test_lab_data_fixtures_exist():
    assert _HOST_FILES, 'expected at least one tests/lab_data/*/hosts.json fixture'


@pytest.mark.parametrize('hosts_file', _HOST_FILES, ids=lambda p: p.parent.name)
def test_real_hosts_json_validates(hosts_validator, hosts_file):
    data = json.loads(hosts_file.read_text())
    errors = list(hosts_validator.iter_errors(data))
    assert errors == [], [e.message for e in errors]


def test_unknown_host_key_is_rejected(hosts_validator):
    base = json.loads(_HOST_FILES[0].read_text())
    bad = copy.deepcopy(base)
    bad[0]['totally_unknown_key'] = 'x'
    assert list(hosts_validator.iter_errors(bad)), 'unknown key should fail validation'
