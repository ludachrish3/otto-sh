"""``[coverage.report]`` runtime resolution — render thresholds.

Mirrors :mod:`otto.coverage.tiers`: the pydantic spec
(:class:`otto.models.settings.CoverageReportSpec`) validates the block at
settings-parse time; this module re-reads the raw dict at report time.
"""

from typing import Any

from .store.model import Thresholds


def load_report_thresholds(cov_config: dict[str, Any]) -> Thresholds:
    """Build render thresholds from a raw ``[coverage]`` settings dict."""
    report = cov_config.get("report") or {}
    return Thresholds(
        high=float(report.get("high", 80.0)),
        medium=float(report.get("medium", 70.0)),
    )
