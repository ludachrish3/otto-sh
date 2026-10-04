"""Project selection and the bootstrap gate, decided by the library."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto import bootstrap as bs
from otto.config.scope import ProjectScopeConfig
from otto.session import (
    ProjectSelection,
    ProjectSelectionError,
    RepoLoadError,
    check_project_overlap,
    check_repos,
    select_projects,
)


def _repo(name, *, lab_patterns=None):
    scope = (
        None
        if lab_patterns is None
        else ProjectScopeConfig(
            lab_patterns=[re.compile(p) for p in lab_patterns], host_patterns=[]
        )
    )
    return SimpleNamespace(name=name, sut_dir=Path(f"/repos/{name}"), project_scope=scope)


def _result(*, errors=(), repos=()):
    return bs.BootstrapResult(env=None, repos=list(repos), errors=list(errors), warnings=[])


def _broken(repo):
    return bs.BootstrapError(repo.sut_dir, f"{repo.name}_init", ImportError("paramiko"))


class TestSelectProjects:
    def test_names_are_normalised_deduplicated_and_kept_in_order(self):
        repos = [_repo("My_Repo"), _repo("other")]
        sel = select_projects(repos, ["My_Repo", "my-repo"], ["Other"])
        assert sel == ProjectSelection(include=["my-repo"], exclude=["other"])

    def test_an_unknown_name_is_refused_with_a_suggestion(self):
        with pytest.raises(ProjectSelectionError) as exc:
            select_projects([_repo("firmware")], ["firmwre"], [])
        err = exc.value
        assert (err.kind, err.field, err.names, err.suggestion) == (
            "unknown",
            "include_projects",
            ["firmwre"],
            "firmware",
        )
        assert str(err) == "no project 'firmwre' — did you mean 'firmware'?"

    def test_an_unknown_exclude_names_its_own_field_and_may_have_no_suggestion(self):
        with pytest.raises(ProjectSelectionError) as exc:
            select_projects([_repo("firmware")], [], ["zzz"])
        assert (exc.value.field, exc.value.suggestion) == ("exclude_projects", None)
        assert str(exc.value) == "no project 'zzz'"

    def test_overlap_is_refused_before_any_name_is_looked_up(self):
        # `ghost` is unknown too; overlap must win, as the CLI's order has it.
        with pytest.raises(ProjectSelectionError) as exc:
            select_projects([], ["ghost", "b"], ["B", "ghost"])
        assert (exc.value.kind, exc.value.names) == ("overlap", ["b", "ghost"])
        assert str(exc.value) == (
            "project(s) b, ghost appear in both include_projects and exclude_projects — pick one"
        )

    def test_the_suggestion_is_computed_from_the_normalised_name(self):
        # Raw, ``My_Rep0`` is too far from ``my-repo`` for any suggestion;
        # normalised to ``my-rep0`` it is one character off.
        with pytest.raises(ProjectSelectionError) as exc:
            select_projects([_repo("my-repo")], ["My_Rep0"], [])
        assert (exc.value.names, exc.value.suggestion) == (["my-rep0"], "my-repo")

    def test_no_switches_selects_nothing_and_needs_no_repos(self):
        assert select_projects([], [], []) == ProjectSelection()


def test_check_project_overlap_passes_disjoint_lists():
    check_project_overlap(["a"], ["b"])  # must not raise


def test_check_project_overlap_refuses_a_name_in_both_lists_once_normalised():
    with pytest.raises(ProjectSelectionError) as exc:
        check_project_overlap(["Beta", "a", "c.d"], ["C_D", "beta"])
    err = exc.value
    assert (err.kind, err.field, err.names) == ("overlap", "include_projects", ["beta", "c-d"])


class TestCheckRepos:
    def test_no_errors_demote_nothing(self):
        assert check_repos(_result(), ["unix"], ProjectSelection()).demoted == []

    def test_an_excluded_repos_error_is_demoted(self):
        repo = _repo("Repo2")
        check = check_repos(
            _result(errors=[_broken(repo)], repos=[repo]), [], ProjectSelection(exclude=["repo2"])
        )
        [d] = check.demoted
        assert (d.repo, d.project, d.reason) == ("Repo2", "repo2", "excluded")
        assert d.message == (
            "repo 'Repo2' failed to load, but is inactive for this run "
            "(exclude_projects repo2) — continuing without it"
        )

    def test_an_out_of_scope_repos_error_is_demoted_with_the_labs(self):
        repo = _repo("repo2", lab_patterns=["unix_alt"])
        check = check_repos(
            _result(errors=[_broken(repo)], repos=[repo]), ["unix"], ProjectSelection()
        )
        [d] = check.demoted
        assert (d.reason, d.labs) == ("out_of_scope", ["unix"])
        assert "not applicable to lab(s) [unix]" in d.message

    def test_an_active_repos_error_is_fatal(self):
        repo = _repo("repo2")
        err = _broken(repo)
        with pytest.raises(RepoLoadError) as exc:
            check_repos(_result(errors=[err], repos=[repo]), ["unix"], ProjectSelection())
        assert exc.value.errors == [err]

    def test_include_forces_an_out_of_scope_error_fatal(self):
        repo = _repo("repo2", lab_patterns=["unix_alt"])
        with pytest.raises(RepoLoadError):
            check_repos(
                _result(errors=[_broken(repo)], repos=[repo]),
                ["unix"],
                ProjectSelection(include=["repo2"]),
            )

    def test_an_error_with_no_repo_is_always_fatal(self):
        err = bs.BootstrapError(Path("/repos/unparsable"), "settings.toml", ValueError("bad"))
        with pytest.raises(RepoLoadError):
            check_repos(_result(errors=[err]), [], ProjectSelection(exclude=["unparsable"]))

    def test_a_fatal_error_keeps_the_demotions_beside_it(self):
        active, excluded = _repo("a"), _repo("b")
        fatal, demoted = _broken(active), _broken(excluded)
        with pytest.raises(RepoLoadError) as exc:
            check_repos(
                _result(errors=[fatal, demoted], repos=[active, excluded]),
                [],
                ProjectSelection(exclude=["b"]),
            )
        # The demoted error sits beside the fatal one, never among it.
        assert exc.value.errors == [fatal]
        assert [d.error for d in exc.value.demoted] == [demoted]
        assert [d.repo for d in exc.value.demoted] == ["b"]

    def test_no_labs_never_infers_out_of_scope(self):
        repo = _repo("repo2", lab_patterns=["unix_alt"])
        with pytest.raises(RepoLoadError):
            check_repos(_result(errors=[_broken(repo)], repos=[repo]), [], ProjectSelection())
