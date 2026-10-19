"""resolve_report_inputs is the one resolver otto cov report and run_tests share."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otto.coverage.report_inputs import ReportInputs, resolve_report_inputs
from otto.coverage.store.model import Thresholds
from tests._fixtures.gitrepo import TmpGitRepo


def _project(cov: dict | None, root):
    project = MagicMock()
    project.root = root
    project.settings = {"coverage": cov} if cov is not None else {}
    return project


def test_no_coverage_table_gives_the_git_less_fallback(tmp_path):
    with patch("otto.config.coverage_settings.get_cov_project", return_value=None):
        inputs = resolve_report_inputs([_project(None, tmp_path)])
    assert inputs == ReportInputs()
    assert inputs.exclusion_rules == []


def test_a_coverage_table_resolves_every_input(tmp_path):
    """A real (empty) tmp_path project root needs no patching at all: no override
    file exists there, so load_override_config returns None without ever
    touching git — only a MagicMock project (never a real git repository) is needed
    to avoid a real git-log walk."""
    project = _project({"tiers": {"e2e": {"kind": "e2e", "precedence": 1}}}, tmp_path)
    inputs = resolve_report_inputs([project])
    assert inputs.project_root == tmp_path
    assert inputs.tier_configs is not None
    assert [t.name for t in inputs.tier_configs] == ["e2e"]
    assert inputs.exclusion_rules == []
    assert inputs.thresholds == Thresholds()
    assert inputs.ticket_spec is None
    assert inputs.overrides is None


@pytest.mark.asyncio
async def test_run_coverage_report_refuses_a_non_empty_output_dir(tmp_path):
    from otto.coverage.config import DestinationError
    from otto.coverage.reporter import run_coverage_report

    (tmp_path / "stale.html").write_text("stale")
    with pytest.raises(DestinationError, match=r"output_dir target .* set overwrite=True"):
        await run_coverage_report([], tmp_path, ReportInputs())


@pytest.mark.asyncio
async def test_run_coverage_report_overwrite_clears_and_proceeds(tmp_path, monkeypatch):
    from otto.coverage import reporter as reporter_module

    (tmp_path / "stale.html").write_text("stale")
    legacy = MagicMock()

    async def fake_legacy(*args, **kwargs):
        legacy(*args, **kwargs)

    monkeypatch.setattr(reporter_module, "_run_legacy_report", fake_legacy)
    await reporter_module.run_coverage_report([], tmp_path, ReportInputs(), overwrite=True)
    assert not (tmp_path / "stale.html").exists()
    legacy.assert_called_once()


@pytest.mark.asyncio
async def test_run_coverage_report_creates_a_missing_output_dir(tmp_path, monkeypatch):
    """run_coverage_report's own prepare_destination call creates output_dir
    (and any missing parents) before dispatching — the gate's other half,
    alongside the refuse/clear behaviour the two tests above pin."""
    from otto.coverage import reporter as reporter_module

    out = tmp_path / "new" / "report"
    legacy = MagicMock()

    async def fake_legacy(*args, **kwargs):
        legacy(*args, **kwargs)

    monkeypatch.setattr(reporter_module, "_run_legacy_report", fake_legacy)
    await reporter_module.run_coverage_report([], out, ReportInputs())
    assert out.is_dir()
    legacy.assert_called_once()


# ── resolve_report_inputs — [coverage.exclusions].rules wiring ──────────────


class TestResolveReportInputsExclusionRules:
    @staticmethod
    def _project(coverage_cfg, root=None):
        project = MagicMock()
        project.settings = {"coverage": coverage_cfg} if coverage_cfg is not None else {}
        project.root = root or Path("/sut")
        return project

    def test_reads_exclusion_rules_from_settings(self):
        project = self._project(
            {
                "tiers": {"system": {"kind": "e2e", "precedence": 1}},
                "exclusions": {"rules": [{"kind": "marker", "name": "MYPROJ_NO_COV"}]},
            }
        )
        inputs = resolve_report_inputs([project])
        assert inputs.project_root == project.root
        assert inputs.tier_configs is not None
        assert [r.name for r in inputs.exclusion_rules] == ["MYPROJ_NO_COV"]
        assert inputs.thresholds == Thresholds()
        assert inputs.ticket_spec is None

    def test_no_exclusions_table_yields_no_rules(self):
        project = self._project({"tiers": {"system": {"kind": "e2e", "precedence": 1}}})
        inputs = resolve_report_inputs([project])
        assert inputs.exclusion_rules == []

    def test_no_cov_project_yields_no_rules(self):
        inputs = resolve_report_inputs([])
        assert inputs.project_root is None
        assert inputs.tier_configs is None
        assert inputs.exclusion_rules == []
        assert inputs.thresholds is None
        assert inputs.ticket_spec is None

    def test_reads_ticket_spec_from_settings(self):
        """[coverage.tickets] (via load_ticket_spec) reaches resolve_report_inputs's
        return value — the feature-absent None default is exercised above."""
        project = self._project(
            {
                "tiers": {"system": {"kind": "e2e", "precedence": 1}},
                "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
            }
        )
        inputs = resolve_report_inputs([project])
        assert inputs.ticket_spec is not None
        assert inputs.ticket_spec.extract("fix PROJ-7") == ["PROJ-7"]

    def test_reads_overrides_from_settings_with_matching_manual_tier(self, tmp_path):
        """[coverage.overrides] must resolve using the SAME tier list
        load_tiers produced for this settings tree, not an empty or wrong
        one — load_override_config rejects a top-level table whose name
        isn't a declared kind="manual" tier, so passing the wrong list here
        would make a well-formed [[bench]] entry look like an unknown
        table."""
        sut = TmpGitRepo(tmp_path / "sut")
        root = sut.root
        sut.write("f.c", "int a;\n")
        sha = sut.commit("work")

        (root / ".otto").mkdir()
        (root / ".otto" / "coverage-overrides.toml").write_text(
            f'[[bench]]\ncommit = "{sha}"\nreason = "manual pass"\n'
        )

        project = self._project(
            {
                "tiers": {
                    "system": {"kind": "e2e", "precedence": 1},
                    "bench": {"kind": "manual", "precedence": 2},
                },
                "tickets": {"pattern": "#(?P<n>[0-9]+)"},
            },
            root=root,
        )
        inputs = resolve_report_inputs([project])
        assert inputs.overrides is not None
        assert [e.key for e in inputs.overrides.asserted] == [f"commit:{sha}"]
        assert inputs.overrides.asserted[0].tier == "bench"
