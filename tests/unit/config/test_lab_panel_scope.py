"""``--list-labs`` panels list only the labs their project's ``lab_patterns`` admit."""

import io
import re
from pathlib import Path
from types import MappingProxyType

from rich.console import Console

from otto.config.project import Project, _lab_rows
from otto.config.scope import ProjectScopeConfig
from otto.labs import (
    Diagnostic,
    LabScope,
    LabStatus,
    LabStatusReport,
    SourceRef,
    SourceUnavailable,
)
from otto.labs.sources import LabListing
from tests._fixtures.project_scaffold import make_project

_ROOT_A = Path("/proj/a")
_ROOT_B = Path("/proj/b")
_NO_LAB = (
    "⚠ no lab listed: lab_patterns ({patterns}) in this project's .otto/settings.toml "
    "match no lab any source declares"
)
_ANOTHER_PROJECT = "its labs come from another project's lab sources"


def _scope(*patterns: str) -> ProjectScopeConfig:
    return ProjectScopeConfig([re.compile(p) for p in patterns], [])


def _report(
    declared: dict[Path, list[LabStatus]],
    *,
    outages: list[SourceUnavailable] | None = None,
) -> LabStatusReport:
    """A report whose labs are *declared* by project root, sorted by name."""
    by_name: dict[str, LabStatus] = {}
    refs: dict[str, list[SourceRef]] = {}
    for root, statuses in declared.items():
        for status in statuses:
            by_name[status.name] = status
            refs.setdefault(status.name, []).append(SourceRef(label="json:x", project_root=root))
    return LabStatusReport(
        labs=[by_name[n] for n in sorted(by_name)],
        declared_by=refs,
        unavailable_sources=outages or [],
    )


def _rows(
    report: LabStatusReport,
    *,
    scope: ProjectScopeConfig | None,
    root: Path = _ROOT_A,
    absence_provable: bool = True,
):
    listed = frozenset(s.name for s in report.labs)
    return _lab_rows(
        report=report,
        project_root=root,
        scope=scope,
        listed_labs=listed,
        absence_provable=absence_provable,
    )


def _plain(rows) -> list[str]:
    return rows.plain.splitlines()


class TestFiltering:
    def test_out_of_scope_lab_not_listed(self):
        report = _report(
            {
                _ROOT_A: [
                    LabStatus(name="bench"),
                    LabStatus(name="other"),
                    LabStatus(name="other_down", unavailable=Diagnostic(where="x", reason="off")),
                ]
            }
        )
        rows = _plain(_rows(report, scope=_scope("bench")))
        assert rows == ["• bench"]

    def test_out_of_scope_unavailable_lab_not_listed(self):
        report = _report(
            {
                _ROOT_A: [
                    LabStatus(name="bench", unavailable=Diagnostic(where="x", reason="off")),
                    LabStatus(name="gone", unavailable=Diagnostic(where="x", reason="off")),
                ]
            }
        )
        rows = _plain(_rows(report, scope=_scope("bench")))
        assert rows == ["• bench — unavailable: off"]

    def test_undeclared_project_lists_every_lab(self):
        report = _report({_ROOT_A: [LabStatus(name="a"), LabStatus(name="b")]})
        assert _plain(_rows(report, scope=None)) == ["• a", "• b"]

    def test_source_outage_row_kept(self):
        outage = SourceUnavailable(
            source=SourceRef(label="cmdb", project_root=_ROOT_A),
            scope=LabScope(names=frozenset({"elsewhere"})),
            diagnostic=Diagnostic(where="cmdb", reason="connection refused"),
        )
        report = _report({_ROOT_A: [LabStatus(name="bench")]}, outages=[outage])
        rows = _rows(report, scope=_scope("bench"), absence_provable=False)
        assert _plain(rows) == ["• bench", "⚠ source cmdb unavailable: connection refused"]


class TestNoLabMatches:
    def test_r2_warning_when_no_listed_lab_matches(self):
        report = _report({_ROOT_A: [LabStatus(name="other")]})
        rows = _rows(report, scope=_scope("bench", "floor.*"))
        assert _plain(rows) == [_NO_LAB.format(patterns="bench, floor.*")]
        assert [span.style for span in rows.spans] == ["yellow"]

    def test_r2_warning_for_a_project_with_no_labs_at_all(self):
        report = _report({})
        rows = _rows(report, scope=_scope("bench"))
        assert _plain(rows) == [_NO_LAB.format(patterns="bench")]

    def test_r2_not_for_an_undeclared_project(self):
        assert _plain(_rows(_report({}), scope=None)) == []

    def test_r2_suppressed_when_a_source_is_unavailable(self):
        report = _report({_ROOT_A: [LabStatus(name="other")]})
        rows = _rows(report, scope=_scope("bench"), absence_provable=False)
        assert not any("no lab listed" in line for line in _plain(rows))

    def test_dim_row_when_labs_come_from_another_project(self):
        report = _report({_ROOT_A: [LabStatus(name="other")], _ROOT_B: [LabStatus(name="bench")]})
        rows = _rows(report, scope=_scope("bench"))
        assert _plain(rows) == [_ANOTHER_PROJECT]
        assert [span.style for span in rows.spans] == ["dim"]

    def test_undeclared_project_gets_no_dim_row(self):
        report = _report({_ROOT_B: [LabStatus(name="bench")]})
        assert _plain(_rows(report, scope=None)) == []

    def test_own_row_means_no_dim_row(self):
        report = _report({_ROOT_A: [LabStatus(name="bench")], _ROOT_B: [LabStatus(name="bench")]})
        assert _plain(_rows(report, scope=_scope("bench"))) == ["• bench"]


def _project(tmp_path: Path, name: str, extra: str) -> Project:
    return Project(root=make_project(tmp_path / name, name=name, extra=extra))


_SOURCE = '[[lab.sources]]\nbackend = "json"\npaths = ["labdata"]\n'


class TestPanel:
    def test_no_lab_sources_project_unchanged(self, tmp_path):
        project = _project(tmp_path, "plain", '[project]\nlab_patterns = ["bench"]\n')
        panel = project.get_lab_panel(listed_labs=frozenset({"x"}), absence_provable=True)
        buf = io.StringIO()
        Console(file=buf, width=200, highlight=False).print(panel)
        assert "no [[lab.sources]] declared" in buf.getvalue()
        assert "no lab listed" not in buf.getvalue()

    def test_panel_threads_the_projects_declared_scope(self, tmp_path):
        project = _project(tmp_path, "scoped", _SOURCE + '[project]\nlab_patterns = ["bench"]\n')
        report = _report(
            {project.root: [LabStatus(name="bench"), LabStatus(name="other")]},
        )
        panel = project.get_lab_panel(
            report=report, listed_labs=frozenset({"bench", "other"}), absence_provable=True
        )
        assert panel.renderable.plain == "• bench"  # type: ignore[union-attr]

    def test_r2_suppressed_on_per_project_fallback(self, tmp_path, monkeypatch):
        """With no combined report there is nothing to prove absence from."""
        from otto.cli.main import _lab_panels

        project = _project(tmp_path, "scoped", _SOURCE + '[project]\nlab_patterns = ["bench"]\n')
        own = _report({project.root: [LabStatus(name="other")]})
        listing = LabListing(report=None, per_project=MappingProxyType({project.root: own}))
        monkeypatch.setattr("otto.labs.sources.lab_listing", lambda **_: listing)
        monkeypatch.setattr("otto.inventory.config.deferred_inventory_view", lambda **_: None)
        (panel,) = _lab_panels([project])
        assert panel.renderable.plain == ""  # type: ignore[union-attr]

    def test_r2_from_the_combined_report(self, tmp_path, monkeypatch):
        from otto.cli.main import _lab_panels

        project = _project(tmp_path, "scoped", _SOURCE + '[project]\nlab_patterns = ["bench"]\n')
        combined = _report({project.root: [LabStatus(name="other")]})
        listing = LabListing(report=combined, per_project=MappingProxyType({}))
        monkeypatch.setattr("otto.labs.sources.lab_listing", lambda **_: listing)
        monkeypatch.setattr("otto.inventory.config.deferred_inventory_view", lambda **_: None)
        (panel,) = _lab_panels([project])
        assert panel.renderable.plain == _NO_LAB.format(patterns="bench")  # type: ignore[union-attr]

    def test_r2_suppressed_when_the_combined_report_has_an_outage(self, tmp_path, monkeypatch):
        from otto.cli.main import _lab_panels

        project = _project(tmp_path, "scoped", _SOURCE + '[project]\nlab_patterns = ["bench"]\n')
        outage = SourceUnavailable(
            source=SourceRef(label="cmdb", project_root=project.root),
            scope=LabScope(names=frozenset({"bench"})),
            diagnostic=Diagnostic(where="cmdb", reason="connection refused"),
        )
        combined = _report({project.root: [LabStatus(name="other")]}, outages=[outage])
        listing = LabListing(report=combined, per_project=MappingProxyType({}))
        monkeypatch.setattr("otto.labs.sources.lab_listing", lambda **_: listing)
        monkeypatch.setattr("otto.inventory.config.deferred_inventory_view", lambda **_: None)
        (panel,) = _lab_panels([project])
        assert "no lab listed" not in panel.renderable.plain  # type: ignore[union-attr]
