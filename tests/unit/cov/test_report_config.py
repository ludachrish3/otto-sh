"""[coverage.report] runtime resolution — render thresholds."""

import json

import pytest

from otto.coverage.report_config import load_report_thresholds
from otto.coverage.reporter import run_coverage_report
from otto.coverage.store.model import Thresholds
from otto.coverage.tiers import load_tiers


def test_defaults_when_absent() -> None:
    assert load_report_thresholds({}) == Thresholds(high=80.0, medium=70.0)


def test_reads_report_block() -> None:
    cfg = {"report": {"high": 90, "medium": 75}}
    assert load_report_thresholds(cfg) == Thresholds(high=90.0, medium=75.0)


def test_partial_block_keeps_other_default() -> None:
    assert load_report_thresholds({"report": {"high": 95}}) == Thresholds(high=95.0, medium=70.0)


@pytest.mark.asyncio
async def test_run_coverage_report_stamps_thresholds_into_store_json(tmp_path) -> None:
    out = tmp_path / "report"
    tier_configs = load_tiers({"tiers": {"nightly": {"kind": "e2e", "precedence": 1}}})
    store = await run_coverage_report(
        [],
        out,
        tier_configs=tier_configs,
        thresholds=Thresholds(high=90.0, medium=75.0),
    )
    assert store is not None
    raw = json.loads((out / "store.json").read_text())
    assert raw["thresholds"] == {"high": 90.0, "medium": 75.0}
