"""The six readers that swallow a failed accessor, run against a real bootstrap.

Three readers import an ``otto.bootstrap`` accessor inside a broad ``except``
(spec ``docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md``
§2.1). An import that names a home the accessor no longer has raises
``ImportError``, the ``except`` swallows it, and the reader quietly answers
its "nothing to read" default. Two more, ``scope_for_project`` and the declared
entries, read the composed-projects catalog ``otto.bootstrap`` publishes into
``otto.declared`` (spec 2026-10-11 placement-and-phases §3.5): a root that
stopped publishing leaves them the same quiet default. The sixth,
``otto.config.project.discovered_teardown_deadline``, swallows a failed discovery
and answers ``None``, so the run keeps its 10-second default. Nothing here
patches an accessor: each test writes a real project, points
``OTTOPATH`` at it and asserts the answer only a working read gives.
``tests/unit/test_moved_name_spellings.py`` refuses the old spellings
statically.
"""

import logging

import pytest

import otto.config.project as config_project
from otto import bootstrap as bs
from tests._fixtures.fleet import _lab
from tests._fixtures.project_scaffold import make_project

_DECLARES_SLOT1 = '[project]\nlab_patterns = ["rig"]\nhost_patterns = ["slot1"]'


@pytest.fixture(autouse=True)
def _fresh_bootstrap():
    bs.invalidate()
    yield
    bs.invalidate()


@pytest.fixture
def declaring_project(tmp_path, monkeypatch):
    """One real project, ``scoped``, whose ``[project]`` admits ``slot1`` of lab ``rig``."""
    project = make_project(tmp_path / "scoped", name="scoped", extra=_DECLARES_SLOT1)
    monkeypatch.setenv("OTTOPATH", str(project))
    return project


def _rig():
    return _lab(("slot1", "rig"), ("slot2", "rig"))


def test_scope_for_project_reads_the_real_projects(declaring_project):
    from otto.declared import scope_for_project

    bs.bootstrap()
    scope = scope_for_project("scoped")
    assert scope is not None, "scope_for_project admitted everything: its accessor import failed"
    assert [p.pattern for p in scope.lab_patterns] == ["rig"]


def test_scope_for_project_composes_through_the_initiator(declaring_project):
    """Nothing composed yet: the initiator ``otto.bootstrap`` installed composes, then answers."""
    from otto.declared import scope_for_project

    assert not bs.is_bootstrapped()
    scope = scope_for_project("scoped")
    assert scope is not None, "scope_for_project admitted everything: the initiator never composed"
    assert [p.pattern for p in scope.lab_patterns] == ["rig"]
    assert bs.is_bootstrapped()


def test_the_teardown_deadline_reads_the_real_environment(declaring_project, monkeypatch):
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")
    assert config_project.discovered_teardown_deadline() == 7.5


def test_declared_entries_read_the_real_projects(declaring_project):
    from otto.declared import surviving_projects

    bs.bootstrap()
    assert [r.name for r in surviving_projects()] == ["scoped"], (
        "declared entries saw no projects: the accessor import failed"
    )


def test_the_walk_resolves_its_scopes_from_the_real_projects(declaring_project):
    from otto.config.scope import scopes_of
    from otto.context import OttoContext

    ctx = OttoContext(lab=_rig())
    assert set(scopes_of(ctx)) == {"scoped"}, (
        "the walk saw no declaration: its accessor import failed"
    )
    assert [host.id for host in ctx.all_hosts()] == ["slot1"]


def test_coverage_detection_reads_the_real_projects(tmp_path, monkeypatch, caplog):
    """A ``[coverage].hosts`` selector matching no host warns once: only a read project has one."""
    from otto.context import OttoContext

    project = make_project(tmp_path / "cov", name="cov", extra='[coverage]\nhosts = "no-such-host"')
    monkeypatch.setenv("OTTOPATH", str(project))
    ctx = OttoContext(lab=_rig())
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        assert ctx.cov is False
    assert "coverage detection failed" in caplog.text, (
        "ctx.cov never read [coverage]: its accessor import failed"
    )


def test_the_remote_completion_gate_reads_the_real_projects(declaring_project, monkeypatch):
    from otto.cli import remote_completion as rc

    lab = _rig()
    lab.hosts["slot1"].resources = frozenset({"slot-1"})
    lab.hosts["slot2"].resources = frozenset({"slot-2"})
    monkeypatch.setattr("otto.session.lab.build_lab", lambda projects, labs: lab)
    chain = rc._ChainParams(host_id="", hop="", term=None, labs=["rig"], holder="carol")

    assert rc._reservation_allows(chain) is True  # no [reservations]: the gate is a no-op
    assert rc._required_for(chain) == {"slot-1"}  # slot2 is outside scoped's fleet
