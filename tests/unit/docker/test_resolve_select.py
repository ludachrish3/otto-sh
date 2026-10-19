"""Provider competition tables (spec §4). Pure engine — no hosts, no lab."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from otto.config.scope import DockerUseCase
from otto.docker.resolve import (
    UseCaseResolutionError,
    declared_use_cases,
    select_fragments,
)
from tests._fixtures.fake_project import fake_project


def _project(name, *fragments):
    return fake_project(name, docker_settings=SimpleNamespace(use_cases=tuple(fragments)))


def _frag(name="integration", **kw):
    defaults = {
        "composes": ("core",),
        "provides": None,
        "priority": 0,
        "env": {},
        "pass_env": (),
    }
    defaults.update(kw)
    return DockerUseCase(name=name, **defaults)


def test_unconditional_fragments_all_participate():
    sel = select_fragments("integration", [_project("a", _frag()), _project("b", _frag())])
    assert [f.project.name for f in sel.fragments] == ["a", "b"]
    assert sel.displaced == []


def test_higher_priority_provider_displaces_mock():
    real = _project("a", _frag(provides="edge", priority=10))
    mock = _project("b", _frag(), _frag(provides="edge"))  # core + mock-edge
    sel = select_fragments("integration", [real, mock])
    assert [f.project.name for f in sel.fragments] == ["a", "b"]  # b keeps its core frag
    (d,) = sel.displaced
    assert (d.capability, d.loser_project, d.winner_project, d.winner_priority) == (
        "edge",
        "b",
        "a",
        10,
    )


def test_mock_wins_when_real_inactive():
    mock = _project("b", _frag(provides="edge"))
    sel = select_fragments("integration", [mock])
    assert [f.project.name for f in sel.fragments] == ["b"]
    assert sel.displaced == []


def test_cross_project_tie_is_hard_error_naming_knob():
    a = _project("a", _frag(provides="edge", priority=5))
    b = _project("b", _frag(provides="edge", priority=5))
    with pytest.raises(UseCaseResolutionError, match="--provide"):
        select_fragments("integration", [a, b])


def test_provide_override_breaks_tie():
    a = _project("a", _frag(provides="edge", priority=5))
    b = _project("b", _frag(provides="edge", priority=5))
    sel = select_fragments("integration", [a, b], provide={"edge": "b"})
    assert [f.project.name for f in sel.fragments] == ["b"]


def test_provide_override_must_name_a_candidate():
    a = _project("a", _frag(provides="edge"))
    with pytest.raises(UseCaseResolutionError, match="not a candidate"):
        select_fragments("integration", [a], provide={"edge": "nope"})


def test_same_project_tie_is_config_error_no_knob():
    """The natural (unforced) same-project tie: no --provide in play at all.

    Anchored on a phrase unique to this raise site (never says --provide) so
    it cannot be satisfied by the override-branch tie message instead.
    """
    a = _project("a", _frag(provides="edge", priority=5), _frag(provides="edge", priority=5))
    with pytest.raises(UseCaseResolutionError, match="same project tie has no knob"):
        select_fragments("integration", [a])


def test_provide_override_cannot_break_a_same_project_tie():
    """--provide narrows the field to a project; it cannot pick among that project's own ties.

    Anchored on a phrase unique to the override branch's raise site, distinct
    from the natural-tie message above.
    """
    a = _project("a", _frag(provides="edge", priority=5), _frag(provides="edge", priority=5))
    with pytest.raises(UseCaseResolutionError, match="does not break a tie"):
        select_fragments("integration", [a], provide={"edge": "a"})


def test_provide_override_narrows_then_resolves_by_priority():
    """--provide picks a PROJECT, not a fragment: normal priority rules apply within it.

    Two same-capability fragments in one project at *different* priorities is not
    a tie, so naming that project with --provide must resolve cleanly to the
    higher-priority fragment rather than being refused as a same-project tie.
    """
    a = _project("a", _frag(provides="edge", priority=1), _frag(provides="edge", priority=10))
    sel = select_fragments("integration", [a], provide={"edge": "a"})
    assert [f.fragment.priority for f in sel.fragments] == [10]
    (d,) = sel.displaced
    assert (
        d.capability,
        d.loser_project,
        d.loser_priority,
        d.winner_project,
        d.winner_priority,
    ) == (
        "edge",
        "a",
        1,
        "a",
        10,
    )


def test_provide_names_unknown_capability():
    a = _project("a", _frag(provides="edge", priority=5))
    with pytest.raises(
        UseCaseResolutionError, match=r"--provide names capability nope\b.*provided: edge"
    ):
        select_fragments("integration", [a], provide={"nope": "a"})


def test_unknown_use_case_lists_declared():
    with pytest.raises(UseCaseResolutionError, match="integration"):
        select_fragments("nope", [_project("a", _frag())])


def test_an_undeclared_use_case_says_no_active_project_declares_it():
    with pytest.raises(UseCaseResolutionError, match="no active project declares use-case 'nope'"):
        select_fragments("nope", [_project("a", _frag())])


def test_declared_use_cases_groups_by_name():
    projects = [_project("a", _frag(), _frag(name="solo")), _project("b", _frag())]
    d = declared_use_cases(projects)
    assert sorted(d) == ["integration", "solo"]
    assert [f.project.name for f in d["integration"]] == ["a", "b"]
