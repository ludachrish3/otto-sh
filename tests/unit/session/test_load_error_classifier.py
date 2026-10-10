"""One classifier decides which bootstrap errors stop a run (spec 4 §4 Rule 2).

``check_repos`` (the CLI and ``open_context``) and ``OttoContext.scopes`` (a
hand-built context) both ask it, so the two can never disagree about a repo.
"""

from typing import Any, cast

from otto.bootstrap import BootstrapError, BootstrapResult, DependencyError
from otto.config.repo import Repo
from otto.session import projects
from otto.session.projects import ProjectSelection, check_repos, classify_load_errors
from tests._fixtures.sutrepo import make_sut_repo


def _repo(tmp_path, name, *, labs=None):
    extra = f'[project]\nlab_patterns = ["{labs}"]' if labs else ""
    return Repo(sut_dir=make_sut_repo(tmp_path / name, name=name, extra=extra))


def _result(repos, errors):
    return BootstrapResult(
        env=cast("Any", None), repos=list(repos), errors=list(errors), ordered_repos=list(repos)
    )


def _skip(repo):
    return DependencyError(repo.sut_dir, f"{repo.name} requires ghost")


def test_an_error_no_repo_owns_is_fatal(tmp_path):
    unowned = BootstrapError(tmp_path / "broken", ".otto/settings.toml", ValueError("bad toml"))
    verdicts = classify_load_errors(_result([], [unowned]), [], include=[], exclude=[])
    assert verdicts.fatal == [unowned]
    assert verdicts.demoted == []


def test_an_active_repos_error_is_fatal(tmp_path):
    repo = _repo(tmp_path, "my-repo")
    err = _skip(repo)
    assert classify_load_errors(_result([repo], [err]), [], include=[], exclude=[]).fatal == [err]


def test_exclusion_demotes_and_is_compared_normalised(tmp_path):
    repo = _repo(tmp_path, "my-repo")
    verdicts = classify_load_errors(
        _result([repo], [_skip(repo)]), [], include=[], exclude=["My_Repo"]
    )
    assert verdicts.fatal == []
    assert [(d.repo, d.reason) for d in verdicts.demoted] == [("my-repo", "excluded")]


def test_a_name_in_both_lists_counts_as_excluded(tmp_path):
    repo = _repo(tmp_path, "my-repo")
    verdicts = classify_load_errors(
        _result([repo], [_skip(repo)]), [], include=["my-repo"], exclude=["my-repo"]
    )
    assert verdicts.fatal == []
    assert [d.reason for d in verdicts.demoted] == ["excluded"]


def test_lab_names_are_never_normalised(tmp_path):
    repo = _repo(tmp_path, "bench", labs="Bench_A")
    err = _skip(repo)
    verdicts = classify_load_errors(_result([repo], [err]), ["Bench_A"], include=[], exclude=[])
    assert verdicts.fatal == [err]  # lab-active, so fatal


def test_a_lab_inactive_repo_is_demoted_with_the_labs_it_was_judged_against(tmp_path):
    repo = _repo(tmp_path, "bench", labs="other")
    (demoted,) = classify_load_errors(
        _result([repo], [_skip(repo)]), ["rig"], include=[], exclude=[]
    ).demoted
    assert (demoted.reason, demoted.labs) == ("out_of_scope", ["rig"])


def test_include_forces_a_lab_inactive_repos_error_fatal(tmp_path):
    repo = _repo(tmp_path, "bench", labs="other")
    err = _skip(repo)
    verdicts = classify_load_errors(_result([repo], [err]), ["rig"], include=["Bench"], exclude=[])
    assert verdicts.fatal == [err]


def test_check_repos_asks_the_classifier(tmp_path, monkeypatch):
    seen = []
    real = projects.classify_load_errors

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("otto.session.projects.classify_load_errors", _spy)
    repo = _repo(tmp_path, "my-repo")
    check_repos(_result([repo], [_skip(repo)]), [], ProjectSelection(exclude=["my-repo"]))
    assert seen == [{"include": [], "exclude": ["my-repo"]}]
