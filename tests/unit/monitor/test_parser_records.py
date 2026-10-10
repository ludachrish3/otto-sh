"""Parser sets and SNMP descriptors are records: collisions, keys, atomicity and copies."""

import re

import pytest

from otto.monitor.parsers import (
    HOST_PATTERN_PARSERS,
    PROJECT_PARSERS,
    PatternParsersEntry,
    ProjectParserEntry,
    get_host_parsers,
    pattern_key,
    register_host_parsers,
    register_parsers,
)
from otto.monitor.snmp import SNMP_METRICS, SnmpMetric, register_snmp_metric
from otto.registry import DuplicateRegistration, FrozenMap


class _P:
    def __init__(self, command: str) -> None:
        self.command = command


def test_re_registering_an_exact_host_needs_overwrite():
    register_host_parsers("h1", {"uptime": _P("uptime")})
    with pytest.raises(DuplicateRegistration):
        register_host_parsers("h1", {})
    register_host_parsers("h1", {}, overwrite=True)
    assert get_host_parsers("h1") == {}


def test_one_pattern_with_two_flag_sets_is_two_registrations():
    register_host_parsers(re.compile(r"box-.*"), {})
    register_host_parsers(re.compile(r"box-.*", re.IGNORECASE), {})
    assert len([k for k in HOST_PATTERN_PARSERS.names() if k.endswith(":box-.*")]) == 2


def test_register_parsers_accepts_a_generator_and_stays_atomic():
    register_parsers(_P(c) for c in ["a", "b"])
    assert {"a", "b"} <= set(PROJECT_PARSERS.names())
    with pytest.raises(DuplicateRegistration):
        register_parsers(_P(c) for c in ["c", "d", "a"])
    assert "c" not in PROJECT_PARSERS
    assert "d" not in PROJECT_PARSERS


def test_re_teaching_a_built_in_oid_needs_overwrite():
    oid = SNMP_METRICS.names()[0]
    metric = SnmpMetric(oid=oid, label="Mine", chart="Mine", y_title="Mine")
    with pytest.raises(DuplicateRegistration):
        register_snmp_metric(metric)
    register_snmp_metric(metric, overwrite=True)
    assert SNMP_METRICS.get(oid).label == "Mine"


def test_a_returned_parser_dict_is_a_deep_private_copy():
    register_host_parsers("h2", {"x": _P("x")})
    got = get_host_parsers("h2")
    got["x"].command = "mutated"  # mutate a returned PARSER, not just the dict
    got.clear()
    again = get_host_parsers("h2")
    assert "x" in again
    assert again["x"].command == "x"


def test_a_raw_snmp_metric_under_a_foreign_oid_is_refused():
    with pytest.raises(ValueError, match=r"9\.9\.9"):
        SNMP_METRICS.register("1.2.3", SnmpMetric(oid="9.9.9", label="Foreign", chart="Foreign"))
    assert "1.2.3" not in SNMP_METRICS


def test_a_raw_project_parser_under_a_foreign_command_is_refused():
    with pytest.raises(ValueError, match="other"):
        PROJECT_PARSERS.register("mine", ProjectParserEntry(_P("other")))
    assert "mine" not in PROJECT_PARSERS


def test_a_raw_pattern_set_under_a_foreign_key_is_refused():
    with pytest.raises(ValueError, match=r"'0:x'.*:y'"):
        HOST_PATTERN_PARSERS.register("0:x", PatternParsersEntry(re.compile("y"), FrozenMap()))
    assert "0:x" not in HOST_PATTERN_PARSERS


def test_register_parsers_refuses_a_command_repeated_within_the_batch():
    with pytest.raises(DuplicateRegistration, match="repeated"):
        register_parsers([_P("r"), _P("r")])
    assert "r" not in PROJECT_PARSERS


def test_the_callers_dict_is_copied_when_registered():
    parsers = {"x": _P("x")}
    register_host_parsers("h3", parsers)
    parsers["y"] = _P("y")
    assert set(get_host_parsers("h3")) == {"x"}


def test_two_flag_sets_of_one_source_are_named_by_their_keys():
    register_host_parsers(re.compile(r"twin-.*"), {})
    register_host_parsers(re.compile(r"twin-.*", re.IGNORECASE), {})
    with pytest.raises(ValueError, match="matches multiple parser patterns") as excinfo:
        get_host_parsers("twin-1")
    message = str(excinfo.value)
    assert message.count("twin-.*") == 2
    assert f"'{pattern_key(re.compile(r'twin-.*'))}'" in message
    assert f"'{pattern_key(re.compile(r'twin-.*', re.IGNORECASE))}'" in message
