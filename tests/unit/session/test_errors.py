"""otto.session's refusals carry their facts as attributes; their messages never spell a flag."""

import pytest

from otto.errors import FieldError, OttoError


def test_every_session_name_resolves_lazily():
    from otto import session

    for name in session.__all__:
        assert getattr(session, name) is not None, name


def test_project_selection_error_carries_kind_names_and_suggestion():
    from otto.session import ProjectSelectionError

    err = ProjectSelectionError(
        "no project 'repp' — did you mean 'repo'?",
        field="include_projects",
        kind="unknown",
        names=["repp"],
        suggestion="repo",
    )
    assert isinstance(err, FieldError)
    assert isinstance(err, ValueError)
    assert (err.field, err.kind, err.names, err.suggestion) == (
        "include_projects",
        "unknown",
        ["repp"],
        "repo",
    )


def test_repo_load_error_lists_every_fatal_error_and_keeps_the_demotions():
    from otto.bootstrap import BootstrapError
    from otto.session import RepoLoadError

    a = BootstrapError("/r/a", "a_init", ImportError("x"))
    b = BootstrapError("/r/b", "b_init", ImportError("y"))
    err = RepoLoadError([a, b], demoted=[])
    assert err.errors == [a, b]
    assert err.demoted == []
    assert str(a) in str(err)
    assert str(b) in str(err)
    assert "--" not in str(err)


@pytest.mark.parametrize(
    ("kind", "field"),
    [("no_labs", "labs"), ("unknown_lab", "labs"), ("sources", None), ("inventory", None)],
)
def test_lab_build_error_carries_kind_and_detail(kind, field):
    from otto.session import LabBuildError

    err = LabBuildError("m", field=field, kind=kind, detail="d")
    assert isinstance(err, FieldError)
    assert (err.field, err.kind, err.detail) == (field, kind, "d")


def test_dependency_refused_error_names_each_requirement():
    from otto.env.preflight import Unsatisfied
    from otto.session import DependencyRefusedError

    bad = Unsatisfied(repo="acme", requirement="otto-sh[monitor] >= 1", found="none")
    err = DependencyRefusedError([bad], warnings=["w"])
    assert err.unsatisfied == [bad]
    assert err.warnings == ["w"]
    assert str(err) == (
        "repo 'acme' requires 'otto-sh[monitor] >= 1' — not satisfied in this "
        "environment (found: none)"
    )


def test_dependency_refused_error_with_no_requirements_still_has_a_message():
    from otto.session import DependencyRefusedError

    err = DependencyRefusedError([])
    assert str(err) == "no unsatisfied requirements were given"
    assert err.unsatisfied == []


def test_dependency_refused_error_keeps_one_line_per_requirement():
    from otto.env.preflight import Unsatisfied
    from otto.session import DependencyRefusedError

    first = Unsatisfied(repo="a", requirement="x>=1", found="0.1")
    second = Unsatisfied(repo="b", requirement="y", found="none")
    assert str(DependencyRefusedError([first, second])) == (
        "repo 'a' requires 'x>=1' — not satisfied in this environment (found: 0.1)\n"
        "repo 'b' requires 'y' — not satisfied in this environment (found: none)"
    )


def test_instruction_inactive_error_message_names_fields_not_flags():
    from otto.session import InstructionInactiveError

    err = InstructionInactiveError("blink", "Acme_Repo", project="acme-repo", reason="excluded")
    assert "exclude_projects" in str(err)
    assert "include_projects" in str(err)
    assert "--" not in str(err)
    assert "-I" not in str(err)
    assert (err.instruction, err.owner, err.project, err.reason) == (
        "blink",
        "Acme_Repo",
        "acme-repo",
        "excluded",
    )


def test_logging_levels_conflict_error_is_a_value_error():
    from otto.session import LoggingLevelsConflictError

    assert issubclass(LoggingLevelsConflictError, OttoError)
    assert issubclass(LoggingLevelsConflictError, ValueError)
