"""--report JSON (spec 2026-09-24 §3.6)."""

import json
from dataclasses import dataclass, field

import pytest

import otto.check.formats as check_formats
from otto.check import FeatureResult, UnmeasuredReason, Verdict
from otto.check.report import REPORT_SCHEMA, report_to_json


@dataclass(frozen=True)
class _Result:
    link_id: str
    results: list[FeatureResult] = field(default_factory=list)


def test_envelope_and_enum_values() -> None:
    result = _Result(
        "edge",
        [
            FeatureResult("delay", Verdict.PASS, measured="+200.4ms"),
            FeatureResult("rate", Verdict.UNMEASURED, reason=UnmeasuredReason.MISSING_TOOL),
        ],
    )
    doc = json.loads(report_to_json(result, kind="link"))
    assert doc["schema"] == REPORT_SCHEMA == "otto-check/1"
    assert doc["kind"] == "link"
    assert doc["otto_version"]
    assert doc["proven_revision"]
    assert doc["result"]["link_id"] == "edge"
    assert doc["result"]["results"][0]["verdict"] == "pass"
    assert doc["result"]["results"][1]["reason"] == "missing-tool"


@pytest.mark.parametrize("version", check_formats.CHECK_REPORT_WRITE_VERSIONS)
def test_the_report_stamps_each_declared_write_version(version):
    assert json.loads(report_to_json(_Result("edge"), kind="link"))["schema"] == version
