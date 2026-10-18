"""One classifier decides which bootstrap errors stop a run (spec 4 §4 Rule 2).

``check_projects`` (the CLI and ``open_context``) and ``scopes_of(ctx)`` (a
hand-built context) both ask it, so the two can never disagree about a repo.
"""

from typing import Any, cast

import pytest

from otto.bootstrap import BootstrapError, BootstrapResult, DependencyError
from otto.config.project import Project
from otto.session import projects
from otto.session.errors import ProjectLoadError
from otto.session.projects import ProjectSelection, check_projects, classify_load_errors
from tests._fixtures.sutrepo import make_sut_repo


def _repo(tmp_path, name, *, labs=None):
    extra = f'[project]\nlab_patterns = ["{labs}"]' if labs else ""
    return Project(root=make_sut_repo(tmp_path / name, name=name, extra=extra))


def _result(projects, errors):
    return BootstrapResult(
        env=cast("Any", None),
        projects=list(projects),
        errors=list(errors),
        ordered_projects=list(projects),
    )


def _skip(project):
    return DependencyError(project.root, f"{project.name} requires ghost")


def test_an_error_no_repo_owns_is_fatal(tmp_path):
    unowned = BootstrapError(tmp_path / "broken", ".otto/settings.toml", ValueError("bad toml"))
    verdicts = classify_load_errors(_result([], [unowned]), [], include=[], exclude=[])
    assert verdicts.fatal == [unowned]
    assert verdicts.demoted == []


def test_an_active_repos_error_is_fatal(tmp_path):
    project = _repo(tmp_path, "my-repo")
    err = _skip(project)
    assert classify_load_errors(_result([project], [err]), [], include=[], exclude=[]).fatal == [
        err
    ]


def test_exclusion_demotes_and_is_compared_normalised(tmp_path):
    project = _repo(tmp_path, "my-repo")
    verdicts = classify_load_errors(
        _result([project], [_skip(project)]), [], include=[], exclude=["My_Repo"]
    )
    assert verdicts.fatal == []
    assert [(d.name, d.reason) for d in verdicts.demoted] == [("my-repo", "excluded")]


def test_a_name_in_both_lists_counts_as_excluded(tmp_path):
    project = _repo(tmp_path, "my-repo")
    verdicts = classify_load_errors(
        _result([project], [_skip(project)]), [], include=["my-repo"], exclude=["my-repo"]
    )
    assert verdicts.fatal == []
    assert [d.reason for d in verdicts.demoted] == ["excluded"]


def test_lab_names_are_never_normalised(tmp_path):
    project = _repo(tmp_path, "bench", labs="Bench_A")
    err = _skip(project)
    verdicts = classify_load_errors(_result([project], [err]), ["Bench_A"], include=[], exclude=[])
    assert verdicts.fatal == [err]  # lab-active, so fatal


def test_a_lab_inactive_repo_is_demoted_with_the_labs_it_was_judged_against(tmp_path):
    project = _repo(tmp_path, "bench", labs="other")
    (demoted,) = classify_load_errors(
        _result([project], [_skip(project)]), ["rig"], include=[], exclude=[]
    ).demoted
    assert (demoted.reason, demoted.labs) == ("out_of_scope", ["rig"])


def test_include_forces_a_lab_inactive_repos_error_fatal(tmp_path):
    project = _repo(tmp_path, "bench", labs="other")
    err = _skip(project)
    verdicts = classify_load_errors(
        _result([project], [err]), ["rig"], include=["Bench"], exclude=[]
    )
    assert verdicts.fatal == [err]


def test_check_repos_asks_the_classifier(tmp_path, monkeypatch):
    seen = []
    real = projects.classify_load_errors

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("otto.session.projects.classify_load_errors", _spy)
    project = _repo(tmp_path, "my-repo")
    check_projects(_result([project], [_skip(project)]), [], ProjectSelection(exclude=["my-repo"]))
    assert seen == [{"include": [], "exclude": ["my-repo"]}]


# --- #630: a failed project an active project REQUIRES is judged as active ---


def _dep_project(tmp_path, name, *, required=(), optional=(), labs=None):
    """A project declaring [dependencies]; resolve with :func:`_resolved` before classifying."""
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    extra = f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    if labs:
        extra += f'\n[project]\nlab_patterns = ["{labs}"]\n'
    return Project(root=make_sut_repo(tmp_path / name, name=name, extra=extra))


def _resolved(*projects):
    """Run the dependency pass, which fills each project's resolved ``dependencies``."""
    from otto.config.dependencies import resolve_dependencies

    resolve_dependencies(list(projects))
    return list(projects)


def _init_failure(project):
    return BootstrapError(project.root, f"{project.name}_init", RuntimeError("boom"))


def test_an_excluded_project_an_active_project_requires_is_fatal(tmp_path):
    a, b = _resolved(_dep_project(tmp_path, "a", required=["b"]), _dep_project(tmp_path, "b"))
    err = _init_failure(b)
    verdicts = classify_load_errors(_result([a, b], [err]), [], include=[], exclude=["b"])
    assert verdicts.fatal == [err]
    assert verdicts.demoted == []


def test_an_out_of_scope_project_an_active_project_requires_is_fatal(tmp_path):
    a, b = _resolved(
        _dep_project(tmp_path, "a", required=["b"], labs="rig"),
        _dep_project(tmp_path, "b", labs="other"),
    )
    err = _init_failure(b)
    verdicts = classify_load_errors(_result([a, b], [err]), ["rig"], include=[], exclude=[])
    assert verdicts.fatal == [err]
    assert verdicts.demoted == []


def test_a_transitive_required_chain_makes_the_failure_fatal(tmp_path):
    # a (active) -> b (excluded) -> c (excluded, broken): a still needs c.
    a, b, c = _resolved(
        _dep_project(tmp_path, "a", required=["b"]),
        _dep_project(tmp_path, "b", required=["c"]),
        _dep_project(tmp_path, "c"),
    )
    err = _init_failure(c)
    verdicts = classify_load_errors(_result([a, b, c], [err]), [], include=[], exclude=["b", "c"])
    assert verdicts.fatal == [err]


def test_an_optional_dependency_keeps_the_failure_demoted(tmp_path):
    a, b = _resolved(_dep_project(tmp_path, "a", optional=["b"]), _dep_project(tmp_path, "b"))
    err = _init_failure(b)
    verdicts = classify_load_errors(_result([a, b], [err]), [], include=[], exclude=["b"])
    assert verdicts.fatal == []
    assert [(d.name, d.reason) for d in verdicts.demoted] == [("b", "excluded")]


def test_an_optional_edge_breaks_a_required_chain(tmp_path):
    # a optionally needs b, b requires c: no active project REQUIRES c.
    a, b, c = _resolved(
        _dep_project(tmp_path, "a", optional=["b"]),
        _dep_project(tmp_path, "b", required=["c"]),
        _dep_project(tmp_path, "c"),
    )
    err = _init_failure(c)
    verdicts = classify_load_errors(_result([a, b, c], [err]), [], include=[], exclude=["b", "c"])
    assert verdicts.fatal == []
    assert [d.name for d in verdicts.demoted] == ["c"]


def test_a_failed_project_only_inactive_projects_require_stays_demoted(tmp_path):
    a, b, c = _resolved(
        _dep_project(tmp_path, "a"),
        _dep_project(tmp_path, "b", required=["c"]),
        _dep_project(tmp_path, "c"),
    )
    err = _init_failure(c)
    verdicts = classify_load_errors(_result([a, b, c], [err]), [], include=[], exclude=["b", "c"])
    assert verdicts.fatal == []
    assert [(d.name, d.reason) for d in verdicts.demoted] == [("c", "excluded")]


def test_check_projects_refuses_when_an_active_project_requires_the_broken_one(tmp_path):
    a, b = _resolved(_dep_project(tmp_path, "a", required=["b"]), _dep_project(tmp_path, "b"))
    err = _init_failure(b)
    with pytest.raises(ProjectLoadError) as info:
        check_projects(_result([a, b], [err]), [], ProjectSelection(exclude=["b"]))
    assert info.value.errors == [err]


@pytest.mark.timeout(10)
def test_a_required_cycle_terminates_and_stays_fatal(tmp_path):
    # a <-> b, both required; b excluded and broken. The walk must not spin.
    a, b = _resolved(
        _dep_project(tmp_path, "a", required=["b"]), _dep_project(tmp_path, "b", required=["a"])
    )
    err = _init_failure(b)
    verdicts = classify_load_errors(_result([a, b], [err]), [], include=[], exclude=["b"])
    assert verdicts.fatal == [err]
    assert [(r.name, r.required_by) for r in verdicts.required] == [("b", ["a"])]


def _resolve_errors(*projects):
    from otto.config.dependencies import resolve_dependencies

    return resolve_dependencies(list(projects)).errors


def test_an_unsatisfied_required_edge_leaves_the_provider_demoted(tmp_path):
    # a requires b >= 2 but b is 1.0: a carries its own DependencyError (fatal,
    # a is active); b's init failure is not reached through that edge.
    a, b = _dep_project(tmp_path, "a", required=["b >= 2"]), _dep_project(tmp_path, "b")
    (a_err,) = _resolve_errors(a, b)
    assert a_err.root == a.root
    b_err = _init_failure(b)
    verdicts = classify_load_errors(_result([a, b], [a_err, b_err]), [], include=[], exclude=["b"])
    assert verdicts.fatal == [a_err]
    assert [(d.name, d.reason) for d in verdicts.demoted] == [("b", "excluded")]
    assert verdicts.required == []


def test_an_include_forced_requirer_seeds_the_walk(tmp_path):
    # Both are outside lab "rig"; include_projects a makes a active, and a requires b.
    a, b = _resolved(
        _dep_project(tmp_path, "a", required=["b"], labs="other"),
        _dep_project(tmp_path, "b", labs="other"),
    )
    err = _init_failure(b)
    without = classify_load_errors(_result([a, b], [err]), ["rig"], include=[], exclude=[])
    assert without.fatal == []
    forced = classify_load_errors(_result([a, b], [err]), ["rig"], include=["a"], exclude=[])
    assert forced.fatal == [err]
    (required,) = forced.required
    assert (required.name, required.reason, required.required_by, required.labs) == (
        "b",
        "out_of_scope",
        ["a"],
        ["rig"],
    )


def test_the_refusal_names_who_requires_the_inactive_project(tmp_path):
    a, b, c = _resolved(
        _dep_project(tmp_path, "a", required=["b"]),
        _dep_project(tmp_path, "b", required=["c"]),
        _dep_project(tmp_path, "c"),
    )
    err = _init_failure(c)
    with pytest.raises(ProjectLoadError) as info:
        check_projects(_result([a, b, c], [err]), [], ProjectSelection(exclude=["b", "c"]))
    (required,) = info.value.required
    assert required.required_by == ["a"]
    assert required.message == (
        "project 'c' failed to load and is inactive for this run (exclude_projects c), "
        "but active project 'a' requires it — switch 'a' off too with exclude_projects, "
        "or fix 'c'"
    )
    assert required.message in str(info.value)


def test_the_message_names_every_active_requirer(tmp_path):
    a, d, b = _resolved(
        _dep_project(tmp_path, "a", required=["b"]),
        _dep_project(tmp_path, "d", required=["b"]),
        _dep_project(tmp_path, "b", labs="other"),
    )
    err = _init_failure(b)
    (required,) = classify_load_errors(
        _result([a, d, b], [err]), ["rig"], include=[], exclude=[]
    ).required
    assert required.message == (
        "project 'b' failed to load and is inactive for this run "
        "(not applicable to lab(s) [rig]), but active projects 'a', 'd' require it"
        " — switch them off too with exclude_projects, or fix 'b'"
    )
