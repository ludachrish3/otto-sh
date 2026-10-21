"""The settings a coverage report is built from, resolved once for every caller.

``otto cov report`` and the report ``otto test --cov-report`` renders after
a run both call :func:`resolve_report_inputs`, so the two cannot drift.
"""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.project import Project
    from .exclusions.rules import ExclusionRule
    from .overrides import OverrideConfig
    from .store.model import Thresholds
    from .tickets import TicketSpec
    from .tiers import TierConfig


@dataclasses.dataclass(frozen=True)
class ReportInputs:
    """What ``[coverage]`` says about a report; all-default is the git-less path.

    ``project_root`` is the first ``[coverage]``-declaring project's ``root``
    (:func:`otto.config.coverage_settings.get_cov_project`); ``tier_configs`` is
    that project's ``[coverage.tiers]``, parsed by
    :func:`otto.coverage.tiers.load_tiers`. With ``project_root`` ``None``,
    :func:`otto.coverage.reporter.run_coverage_report` reports tracefiles
    alone and refuses run directories; the ``--tier`` escape hatch relies on
    that — it never resolves settings, so it passes ``ReportInputs()``
    explicitly.

    ``exclusion_rules`` comes from ``[coverage.exclusions].rules`` — the
    compiled rules (:func:`otto.coverage.exclusions.rules.load_exclusion_rules`)
    applied by the reporter's filter stage, which DELETES the lines and
    branches they name from the merged store. An empty list is not
    feature-absent: the built-in ``LCOV_EXCL_*`` families always apply on top
    of whatever is configured here.

    ``thresholds`` comes from ``[coverage.report]`` — render thresholds
    forwarded to the reporter/renderer
    (:func:`otto.coverage.report_config.load_report_thresholds`); ``None``
    selects :class:`~otto.coverage.store.model.Thresholds`'s own 80.0/70.0
    defaults.

    ``ticket_spec`` comes from ``[coverage.tickets]`` — the compiled
    commit-message ticket pattern (:func:`otto.coverage.tickets.load_ticket_spec`).
    ``None`` is the feature-absent signal: the reporter runs no git log walk
    and the report is unchanged.

    ``overrides`` comes from ``.otto/coverage-overrides.toml`` (or the path
    named by ``[coverage.overrides].file``) via
    :func:`otto.coverage.overrides.load_override_config`. ``None`` is the
    feature-absent signal: no asserted entries fold in and no reattribution
    reaches ticket attribution.
    """

    project_root: Path | None = None
    tier_configs: "list[TierConfig] | None" = None
    exclusion_rules: "list[ExclusionRule]" = dataclasses.field(default_factory=list)
    thresholds: "Thresholds | None" = None
    ticket_spec: "TicketSpec | None" = None
    overrides: "OverrideConfig | None" = None


def resolve_report_inputs(projects: "list[Project]") -> ReportInputs:
    """Resolve the report inputs from the first project that declares ``[coverage]``.

    The same first-project selection ``otto cov get`` and ``clean`` use
    (:func:`otto.config.coverage_settings.get_cov_project`). A tree with no
    ``[coverage]`` table gets ``ReportInputs()`` — the git-less fallback,
    which reports tracefiles alone.

    Raises:
        otto.config.coverage_settings.CoverageConfigError: a malformed
            ``[coverage.exclusions]`` rule.
        otto.coverage.overrides.OverrideConfigError: a malformed override file.
    """
    from ..config.coverage_settings import get_cov_config, get_cov_project
    from .exclusions.rules import load_exclusion_rules
    from .overrides import load_override_config
    from .report_config import load_report_thresholds
    from .tickets import load_ticket_spec
    from .tiers import load_tiers

    cov_project = get_cov_project(projects)
    if cov_project is None:
        return ReportInputs()
    cov_config = get_cov_config(projects)
    tier_configs = load_tiers(cov_config)
    return ReportInputs(
        project_root=cov_project.root,
        tier_configs=tier_configs,
        exclusion_rules=load_exclusion_rules(cov_config),
        thresholds=load_report_thresholds(cov_config),
        ticket_spec=load_ticket_spec(cov_config),
        overrides=load_override_config(cov_config, cov_project.root, tier_configs),
    )
