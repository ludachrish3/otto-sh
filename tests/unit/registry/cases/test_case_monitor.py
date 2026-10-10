"""Conformance cases for the monitor's parser-set and SNMP-descriptor registries."""

import functools
import re

import pytest
from pydantic import ConfigDict
from typing_extensions import override

from otto.monitor.parsers import (
    HOST_PARSERS,
    HOST_PATTERN_PARSERS,
    PROJECT_PARSERS,
    HostParsersEntry,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    PatternParsersEntry,
    ProjectParserEntry,
    pattern_key,
    register_host_parsers,
    register_parsers,
)
from otto.monitor.snmp import SNMP_METRICS, SnmpMetric, register_snmp_metric
from otto.registry import FrozenMap, IncompleteRegistration
from tests.unit.registry import conformance

COVERS = [
    "otto.monitor.parsers:HOST_PARSERS",
    "otto.monitor.parsers:HOST_PATTERN_PARSERS",
    "otto.monitor.parsers:PROJECT_PARSERS",
    "otto.monitor.snmp:SNMP_METRICS",
]


class _CaseParser(MetricParser):
    """A parser for *command*; equal only to itself, like every parser."""

    y_title = "Case"
    unit = ""
    chart = "Case"

    def __init__(self, command: str) -> None:
        self.command = command

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}


_PARSER = _CaseParser("case-cmd")
"""One parser instance, so a wrapper's record and the raw record compare equal."""


def test_host_parsers_raw_case():
    conformance.assert_raw_registry(
        HOST_PARSERS,
        make=lambda i: ("case-host", HostParsersEntry(FrozenMap({f"c{i}": _PARSER}))),
    )


def test_host_pattern_parsers_raw_case():
    pattern = re.compile(r"case-.*")
    conformance.assert_raw_registry(
        HOST_PATTERN_PARSERS,
        make=lambda i: (
            pattern_key(pattern),
            PatternParsersEntry(pattern, FrozenMap({f"c{i}": _PARSER})),
        ),
        invalid=("0:x", PatternParsersEntry(re.compile("y"), FrozenMap())),
    )


def test_project_parsers_raw_case():
    conformance.assert_raw_registry(
        PROJECT_PARSERS,
        make=lambda i: ("case-cmd", ProjectParserEntry(_CaseParser("case-cmd"))),
        invalid=("case-cmd", ProjectParserEntry(_CaseParser("other-cmd"))),
    )


def test_snmp_metrics_raw_case():
    conformance.assert_raw_registry(
        SNMP_METRICS,
        make=lambda i: ("9.8.7.6", SnmpMetric(oid="9.8.7.6", label=f"Case {i}", chart="Case")),
        invalid=("9.8.7.6", SnmpMetric(oid="9.9.9", label="Foreign", chart="Case")),
    )


def test_register_host_parsers_wrapper_case_for_an_exact_id():
    conformance.assert_wrapper_matches_raw(
        HOST_PARSERS,
        via_wrapper=functools.partial(register_host_parsers, "case-host", {"c": _PARSER}),
        record_for=lambda: HostParsersEntry(FrozenMap({"c": _PARSER})),
    )


def test_register_host_parsers_wrapper_case_for_a_pattern():
    pattern = re.compile(r"case-w.*", re.IGNORECASE)
    conformance.assert_wrapper_matches_raw(
        HOST_PATTERN_PARSERS,
        via_wrapper=functools.partial(register_host_parsers, pattern, {"c": _PARSER}),
        record_for=lambda: PatternParsersEntry(pattern, FrozenMap({"c": _PARSER})),
    )


def test_register_parsers_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        PROJECT_PARSERS,
        via_wrapper=functools.partial(register_parsers, [_PARSER]),
        record_for=lambda: ProjectParserEntry(_PARSER),
    )


def test_register_parsers_commits_a_batch_of_three_as_one_revision():
    revision = PROJECT_PARSERS.revision
    register_parsers(_CaseParser(f"case-batch-{i}") for i in range(3))
    assert PROJECT_PARSERS.revision == revision + 1
    assert {f"case-batch-{i}" for i in range(3)} <= set(PROJECT_PARSERS.names())


def test_register_snmp_metric_wrapper_case():
    metric = SnmpMetric(oid="9.8.7.5", label="Case", chart="Case")
    conformance.assert_wrapper_matches_raw(
        SNMP_METRICS,
        via_wrapper=functools.partial(register_snmp_metric, metric),
        record_for=lambda: metric,
    )


def test_a_mutable_snmp_metric_subclass_is_refused():
    """A descriptor that could change its OID after registration would break the key invariant."""

    class MutableMetric(SnmpMetric):
        model_config = ConfigDict(frozen=False)

    metric = MutableMetric(oid="9.8.7.4", label="Case", chart="Case")
    with pytest.raises(IncompleteRegistration, match="MutableMetric is not frozen"):
        register_snmp_metric(metric)
    assert metric.oid not in SNMP_METRICS
