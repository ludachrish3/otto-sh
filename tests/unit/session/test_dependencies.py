"""``otto.session.check_dependencies`` — whose unmet requirement refuses, whose warns."""

from types import SimpleNamespace
from typing import Any

import pytest

from otto.config.lab import Lab
from otto.config.scope import ProjectScope
from otto.context import OttoContext
from otto.env.preflight import PreflightResult, Unsatisfied
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.scoping import verdict

_BAD = Unsatisfied(repo="acme", requirement="x>=1", found="0.1")
_WARNING = (
    "repo 'acme' requires 'x>=1' — not satisfied in this environment (found: 0.1), "
    "but acme is inactive for this run — continuing without it"
)


def _ctx(
    *,
    exclude: "tuple[str, ...]" = (),
    scopes: "dict[str, ProjectScope] | None" = None,
) -> OttoContext:
    ctx = OttoContext(lab=Lab(name="t"), exclude_projects=exclude)
    ctx.scopes = dict(scopes or {})
    return ctx


_REPOS = [SimpleNamespace(name="acme"), SimpleNamespace(name="beta")]


def _preflight(monkeypatch: pytest.MonkeyPatch, *, unsatisfied, warnings) -> "list[Any]":
    """Stub bootstrap and the preflight; the returned list records each repos argument."""
    seen: list[Any] = []

    def stub(repos):
        seen.append(repos)
        return PreflightResult(unsatisfied=list(unsatisfied), warnings=list(warnings))

    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: bootstrap_stub([], ordered=_REPOS))
    monkeypatch.setattr("otto.env.preflight.preflight", stub)
    return seen


def test_nothing_unmet_returns_the_preflight_warnings(monkeypatch):
    from otto.session import check_dependencies

    _preflight(monkeypatch, unsatisfied=[], warnings=["could not check beta"])
    assert check_dependencies(_ctx()) == ["could not check beta"]


def test_the_preflight_checks_the_bootstraps_ordered_repos(monkeypatch):
    from otto.session import check_dependencies

    seen = _preflight(monkeypatch, unsatisfied=[], warnings=[])
    check_dependencies(_ctx())
    assert seen == [_REPOS]


def test_an_active_repos_unmet_requirement_refuses(monkeypatch):
    from otto.session import DependencyRefusedError, check_dependencies

    _preflight(monkeypatch, unsatisfied=[_BAD], warnings=["could not check beta"])
    with pytest.raises(DependencyRefusedError) as exc:
        check_dependencies(_ctx())
    assert exc.value.unsatisfied == [_BAD]
    assert exc.value.warnings == ["could not check beta"]


def test_an_excluded_repos_unmet_requirement_warns(monkeypatch):
    from otto.session import check_dependencies

    _preflight(monkeypatch, unsatisfied=[_BAD], warnings=["could not check beta"])
    warnings = check_dependencies(_ctx(exclude=("acme",)))
    assert warnings == ["could not check beta", _WARNING]


def test_a_host_starved_repo_is_inactive_here_so_it_warns(monkeypatch):
    from otto.session import check_dependencies

    _preflight(monkeypatch, unsatisfied=[_BAD], warnings=[])
    ctx = _ctx(scopes={"acme": verdict("acme", universe=())})
    assert check_dependencies(ctx) == [_WARNING]


def test_a_lab_excluded_repo_warns(monkeypatch):
    from otto.session import check_dependencies

    _preflight(monkeypatch, unsatisfied=[_BAD], warnings=[])
    ctx = _ctx(scopes={"acme": verdict("acme", excluded=True)})
    assert check_dependencies(ctx) == [_WARNING]


def test_an_inactive_repo_does_not_suppress_an_active_one(monkeypatch):
    from otto.session import DependencyRefusedError, check_dependencies

    other = Unsatisfied(repo="beta", requirement="y>=2", found="none")
    _preflight(monkeypatch, unsatisfied=[_BAD, other], warnings=[])
    with pytest.raises(DependencyRefusedError) as exc:
        check_dependencies(_ctx(exclude=("acme",)))
    assert exc.value.unsatisfied == [other]
    assert exc.value.warnings == [_WARNING]
