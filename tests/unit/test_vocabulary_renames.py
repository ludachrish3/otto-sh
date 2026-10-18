"""The public names the project vocabulary renamed: each old spelling is gone, each new one present.

One row per public name, field or parameter that said "repo" or "SUT" (and the
coverage report title, which said ``project_name``). Each row is checked against
the API golden (``tests/unit/api_snapshot/public_api.txt``), which
``make api-snapshot`` regenerates from the tree, so a row cannot pass while the
tree still exports the old spelling. Module moves are pinned separately, in
``test_moved_name_spellings.py``.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "api_snapshot" / "public_api.txt"


@dataclass(frozen=True)
class Rename:
    """One renamed public name.

    ``kind`` is ``name`` (a module-level class or function), ``member`` (a
    field, method or property of a public class), ``enum`` (an enum member,
    ``owner.MEMBER``) or ``param`` (a parameter of a public callable). For
    ``name``, ``member`` and ``enum``, ``old`` and ``new`` are the golden's
    qualified names. For ``param``, they are the owning callable before and
    after, and ``old_param`` / ``new_param`` are the parameter.
    """

    kind: str
    old: str
    new: str
    old_param: str = ""
    new_param: str = ""


def _golden() -> list[list[str]]:
    return [
        line.split("\t") for line in GOLDEN.read_text().splitlines() if not line.startswith("#")
    ]


def _has_name(rows: list[list[str]], kind: str, qualname: str) -> bool:
    return any(len(r) > 1 and r[0] == kind and r[1] == qualname for r in rows)


def _params(rows: list[list[str]], qualname: str) -> list[str]:
    names: list[str] = []
    for r in rows:
        if len(r) > 2 and r[0] in ("call", "member") and r[1] == qualname:
            names += re.findall(r"\b[A-Z]{2}:(\w+):", "\t".join(r[2:]))
    return names


RENAMES: list[Rename] = [
    Rename(
        "param", "otto.bootstrap:BootstrapError", "otto.bootstrap:BootstrapError", "sut_dir", "root"
    ),
    Rename(
        "param",
        "otto.bootstrap:BootstrapResult",
        "otto.bootstrap:BootstrapResult",
        "repos",
        "projects",
    ),
    Rename(
        "param",
        "otto.bootstrap:BootstrapResult",
        "otto.bootstrap:BootstrapResult",
        "ordered_repos",
        "ordered_projects",
    ),
    Rename(
        "member",
        "otto.bootstrap:BootstrapResult.ordered_repos",
        "otto.bootstrap:BootstrapResult.ordered_projects",
    ),
    Rename(
        "member", "otto.bootstrap:BootstrapResult.repos", "otto.bootstrap:BootstrapResult.projects"
    ),
    Rename(
        "param",
        "otto.bootstrap:BootstrapWarning",
        "otto.bootstrap:BootstrapWarning",
        "sut_dir",
        "root",
    ),
    Rename(
        "member", "otto.bootstrap:BootstrapWarning.sut_dir", "otto.bootstrap:BootstrapWarning.root"
    ),
    Rename(
        "param",
        "otto.bootstrap:DependencyError",
        "otto.bootstrap:DependencyError",
        "sut_dir",
        "root",
    ),
    Rename(
        "param",
        "otto.bootstrap:ProjectScopeError",
        "otto.bootstrap:ProjectScopeError",
        "sut_dir",
        "root",
    ),
    Rename("name", "otto.bootstrap:get_ordered_repos", "otto.bootstrap:get_ordered_projects"),
    Rename("name", "otto.bootstrap:get_repos", "otto.bootstrap:get_projects"),
    Rename("name", "otto.config:Repo", "otto.config:Project"),
    Rename("param", "otto.config:Repo", "otto.config:Project", "sut_dir", "root"),
    Rename("member", "otto.config:Repo.sut_dir", "otto.config:Project.root"),
    Rename("member", "otto.context:OttoContext.for_repo", "otto.context:OttoContext.for_project"),
    Rename(
        "param",
        "otto.context:OttoContext.for_repo",
        "otto.context:OttoContext.for_project",
        "repo_name",
        "name",
    ),
    Rename(
        "member",
        "otto.context:OttoContext.ordered_repos",
        "otto.context:OttoContext.ordered_projects",
    ),
    Rename("member", "otto.context:OttoContext.repos", "otto.context:OttoContext.projects"),
    Rename(
        "param",
        "otto.context:ProjectContextView",
        "otto.context:ProjectContextView",
        "repo_name",
        "project_name",
    ),
    Rename(
        "param",
        "otto.coverage:CoverageReporter",
        "otto.coverage:CoverageReporter",
        "project_name",
        "report_title",
    ),
    Rename(
        "param",
        "otto.coverage:ReportInputs",
        "otto.coverage:ReportInputs",
        "repo_root",
        "project_root",
    ),
    Rename(
        "member", "otto.coverage:ReportInputs.repo_root", "otto.coverage:ReportInputs.project_root"
    ),
    Rename(
        "param", "otto.coverage:clean_coverage", "otto.coverage:clean_coverage", "repos", "projects"
    ),
    Rename(
        "param",
        "otto.coverage:collect_coverage",
        "otto.coverage:collect_coverage",
        "repos",
        "projects",
    ),
    Rename(
        "param", "otto.coverage:get_coverage", "otto.coverage:get_coverage", "repos", "projects"
    ),
    Rename(
        "param",
        "otto.coverage:resolve_report_inputs",
        "otto.coverage:resolve_report_inputs",
        "repos",
        "projects",
    ),
    Rename(
        "param",
        "otto.coverage:run_coverage_report",
        "otto.coverage:run_coverage_report",
        "project_name",
        "report_title",
    ),
    Rename("param", "otto.docker:BuildReport", "otto.docker:BuildReport", "repos", "projects"),
    Rename("member", "otto.docker:BuildReport.repos", "otto.docker:BuildReport.projects"),
    Rename("param", "otto.docker:FailedImage", "otto.docker:FailedImage", "repo", "project"),
    Rename("member", "otto.docker:FailedImage.repo", "otto.docker:FailedImage.project"),
    Rename("name", "otto.docker:RepoBuild", "otto.docker:ProjectBuild"),
    Rename("param", "otto.docker:RepoBuild", "otto.docker:ProjectBuild", "repo", "project"),
    Rename("member", "otto.docker:RepoBuild.repo", "otto.docker:ProjectBuild.project"),
    Rename("param", "otto.docker:build_images", "otto.docker:build_images", "repo", "project"),
    Rename("param", "otto.docker:build_on", "otto.docker:build_on", "repo", "project"),
    Rename("param", "otto.docker:compose_down", "otto.docker:compose_down", "repo", "project"),
    Rename("param", "otto.docker:compose_up", "otto.docker:compose_up", "repo", "project"),
    Rename("param", "otto.docker:composed", "otto.docker:composed", "repo", "project"),
    Rename(
        "param",
        "otto.docker:get_user_compose_project",
        "otto.docker:get_user_compose_project",
        "repo_name",
        "project_name",
    ),
    Rename("name", "otto.examples.options:RepoOptions", "otto.examples.options:ProjectOptions"),
    Rename(
        "param",
        "otto.examples.reservations:ExampleReservationBackend",
        "otto.examples.reservations:ExampleReservationBackend",
        "repo_dir",
        "project_root",
    ),
    Rename(
        "member", "otto.host:ProfileContext.from_repos", "otto.host:ProfileContext.from_projects"
    ),
    Rename(
        "param",
        "otto.host:ProfileContext.from_repos",
        "otto.host:ProfileContext.from_projects",
        "repos",
        "projects",
    ),
    Rename(
        "param",
        "otto.host:check_data_profiles",
        "otto.host:check_data_profiles",
        "repos",
        "projects",
    ),
    Rename("member", "otto.init:InitConfig.for_repo", "otto.init:InitConfig.for_project"),
    Rename("name", "otto.init:check_repo", "otto.init:check_project"),
    Rename(
        "param",
        "otto.inventory:build_inventory",
        "otto.inventory:build_inventory",
        "repos",
        "projects",
    ),
    Rename(
        "param", "otto.lab:fleet_of_interest", "otto.lab:fleet_of_interest", "repos", "projects"
    ),
    Rename("param", "otto.labs:LabSourceEnv", "otto.labs:LabSourceEnv", "repo_dir", "project_root"),
    Rename("member", "otto.labs:LabSourceEnv.repo_dir", "otto.labs:LabSourceEnv.project_root"),
    Rename(
        "param", "otto.labs:build_lab_sources", "otto.labs:build_lab_sources", "repos", "projects"
    ),
    Rename(
        "member", "otto.models:OttoEnvSettings.sut_dirs", "otto.models:OttoEnvSettings.ottopath"
    ),
    Rename(
        "param", "otto.monitor:MonitorTlsError", "otto.monitor:MonitorTlsError", "repos", "projects"
    ),
    Rename(
        "param",
        "otto.monitor:resolve_monitor_tls",
        "otto.monitor:resolve_monitor_tls",
        "repos",
        "projects",
    ),
    Rename("param", "otto.monitor:serve_review", "otto.monitor:serve_review", "repos", "projects"),
    Rename(
        "param", "otto.project:ProjectActions", "otto.project:ProjectActions", "repo", "project"
    ),
    Rename(
        "param", "otto.project:ProjectStatus", "otto.project:ProjectStatus", "repos", "projects"
    ),
    Rename("member", "otto.project:ProjectStatus.repos", "otto.project:ProjectStatus.projects"),
    Rename("name", "otto.project:RepoPlan", "otto.project:ProjectPlan"),
    Rename("param", "otto.project:RepoPlan", "otto.project:ProjectPlan", "repo", "project"),
    Rename("member", "otto.project:RepoPlan.repo", "otto.project:ProjectPlan.project"),
    Rename("name", "otto.project:RepoScope", "otto.project:ProjectScopeSummary"),
    Rename("param", "otto.project:actions_for", "otto.project:actions_for", "repo", "project"),
    Rename("member", "otto.registry:BackendRegistry.repo", "otto.registry:BackendRegistry.project"),
    Rename("member", "otto.registry:Registry.repo", "otto.registry:Registry.project"),
    Rename("member", "otto.registry:RegistryView.repo", "otto.registry:RegistryView.project"),
    Rename("name", "otto.registry:RequireRepo", "otto.registry:RequireProject"),
    Rename("name", "otto.registry:registering_repo", "otto.registry:registering_project"),
    Rename(
        "param",
        "otto.reservations:NullReservationBackend",
        "otto.reservations:NullReservationBackend",
        "repo_dir",
        "project_root",
    ),
    Rename(
        "param",
        "otto.reservations:ReservationBackendBase",
        "otto.reservations:ReservationBackendBase",
        "repo_dir",
        "project_root",
    ),
    Rename(
        "param",
        "otto.reservations:ReservationEnv",
        "otto.reservations:ReservationEnv",
        "repo_dir",
        "project_root",
    ),
    Rename(
        "member",
        "otto.reservations:ReservationEnv.repo_dir",
        "otto.reservations:ReservationEnv.project_root",
    ),
    Rename(
        "param",
        "otto.reservations:build_backend",
        "otto.reservations:build_backend",
        "repo_dir",
        "project_root",
    ),
    Rename(
        "param",
        "otto.reservations:build_reservation_gate",
        "otto.reservations:build_reservation_gate",
        "repos",
        "projects",
    ),
    Rename(
        "param",
        "otto.reservations:gate_from_settings",
        "otto.reservations:gate_from_settings",
        "repo_dir",
        "project_root",
    ),
    Rename("name", "otto.session:DemotedRepo", "otto.session:DemotedProject"),
    Rename("param", "otto.session:DemotedRepo", "otto.session:DemotedProject", "repo", "name"),
    Rename("member", "otto.session:DemotedRepo.repo", "otto.session:DemotedProject.name"),
    Rename("name", "otto.session:RepoCheck", "otto.session:ProjectCheck"),
    Rename("name", "otto.session:RepoLoadError", "otto.session:ProjectLoadError"),
    Rename("param", "otto.session:build_lab", "otto.session:build_lab", "repos", "projects"),
    Rename("name", "otto.session:check_repos", "otto.session:check_projects"),
    Rename(
        "param",
        "otto.session:merge_host_preferences",
        "otto.session:merge_host_preferences",
        "repos",
        "projects",
    ),
    Rename(
        "param",
        "otto.session:merge_logging_levels",
        "otto.session:merge_logging_levels",
        "repos",
        "projects",
    ),
    Rename(
        "param", "otto.session:select_projects", "otto.session:select_projects", "repos", "projects"
    ),
    Rename(
        "param", "otto.suite:RunOptions", "otto.suite:RunOptions", "project_name", "report_title"
    ),
    Rename("member", "otto.suite:RunOptions.project_name", "otto.suite:RunOptions.report_title"),
    Rename(
        "param",
        "otto.testing:assert_lab_repository_conforms",
        "otto.testing:assert_lab_repository_conforms",
        "repo",
        "repository",
    ),
    Rename("member", "otto:OttoContext.for_repo", "otto:OttoContext.for_project"),
    Rename(
        "param", "otto:OttoContext.for_repo", "otto:OttoContext.for_project", "repo_name", "name"
    ),
    Rename("member", "otto:OttoContext.ordered_repos", "otto:OttoContext.ordered_projects"),
    Rename("member", "otto:OttoContext.repos", "otto:OttoContext.projects"),
    Rename("param", "otto:ProjectActions", "otto:ProjectActions", "repo", "project"),
    Rename("param", "otto:RunOptions", "otto:RunOptions", "project_name", "report_title"),
    Rename("member", "otto:RunOptions.project_name", "otto:RunOptions.report_title"),
    Rename("enum", "otto.project:CleanlinessKind.REPO", "otto.project:CleanlinessKind.PROJECT"),
    Rename(
        "member", "otto.session:DemotedRepo.project", "otto.session:DemotedProject.normalized_name"
    ),
    Rename(
        "param",
        "otto.session:DemotedRepo",
        "otto.session:DemotedProject",
        "project",
        "normalized_name",
    ),
]


@pytest.mark.parametrize("row", RENAMES, ids=lambda r: f"{r.kind}:{r.old}:{r.old_param}")
def test_the_old_spelling_is_gone_and_the_new_one_is_present(row: Rename) -> None:
    rows = _golden()
    if row.kind == "enum":
        owner, _, old_member = row.old.rpartition(".")
        new_owner, _, new_member = row.new.rpartition(".")
        members = {
            r[2] for r in rows if len(r) > 2 and r[0] == "enum" and r[1] in (owner, new_owner)
        }
        assert old_member not in members, f"{row.old} is still in the golden"
        assert new_member in members, f"{row.new} is not in the golden"
    elif row.kind == "param":
        old_params, new_params = _params(rows, row.old), _params(rows, row.new)
        assert row.old_param not in new_params, f"{row.new} still takes {row.old_param!r}"
        assert row.new_param in new_params, f"{row.new} does not take {row.new_param!r}"
        if row.old != row.new:
            assert not old_params, f"{row.old} is still in the golden"
    else:
        assert not _has_name(rows, row.kind, row.old), f"{row.old} is still in the golden"
        assert _has_name(rows, row.kind, row.new), f"{row.new} is not in the golden"


def test_the_project_member_s_value_is_project_in_the_golden() -> None:
    """``CleanlinessKind.PROJECT``'s VALUE says project too, not only its member name."""
    row = [r for r in _golden() if r[:3] == ["enum", "otto.project:CleanlinessKind", "PROJECT"]]
    assert row == [["enum", "otto.project:CleanlinessKind", "PROJECT", 'S:"project"']]
