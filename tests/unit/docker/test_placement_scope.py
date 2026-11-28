"""Docker parents and placeholders come from the one scope judge (spec 2026-11-24 4.9, Q5, Q8, Q35).

A lab build places declared containers through a build-time scope source,
``otto.docker.compose.PlacementSource``, read through
:class:`otto.lab_view.ScopedLab` as a run reads its context. So the default
parent is chosen by the one rule among the hosts the run may target, and only
the projects that take part get placeholders. At run time the same rule reads
``ctx.lab``; one test drives both sources with the same input.

Real projects (parsed from a real ``settings.toml``) and real hosts (built by
the factory). The lab ``alpha`` holds ``a-dut`` and ``a-jump``, both
docker-capable unless a test says otherwise, and the built-in runner;
``a-jump`` ranks higher, so it is the whole lab's default parent. The project
``inscope`` targets ``a-dut.*`` only. The names this change adds are imported
inside the tests, so the regression tests here run on the code before it.
"""

from pathlib import Path

import pytest

from otto.config.project import Project
from otto.config.scope import HostOutOfScopeError
from otto.docker.compose import get_container_host, register_declared_container_hosts
from otto.docker.observe import (
    DockerVerbError,
    default_docker_parent,
    docker_parent,
    docker_parents,
)
from otto.host.docker_host import DockerContainerHost
from tests._fixtures.fleet import _lab, add_builtin_local, install_scoped_context
from tests._fixtures.project_scaffold import make_project


def _toml_list(items):
    return "[" + ", ".join(f"'{item}'" for item in items) + "]"


def _docker_project(tmp_path, name, *, labs=None, hosts=(".*",), use_case=None):
    """A real project declaring one compose (service ``api``); a ``[project]`` scope when *labs*."""
    lines = []
    if labs is not None:
        lines += [
            "[project]",
            f"lab_patterns = {_toml_list(labs)}",
            f"host_patterns = {_toml_list(hosts)}",
            "",
        ]
    lines += [
        "[[docker.composes]]",
        'name = "core"',
        'path = "docker/compose.yml"',
        'services = ["api"]',
    ]
    if use_case is not None:
        lines += ["", "[[docker.use_cases]]", f'name = "{use_case}"', 'composes = ["core"]']
    root = make_project(
        tmp_path / name,
        name=name,
        extra="\n".join(lines) + "\n",
        files={"docker/compose.yml": "services: {}\n"},
    )
    return Project(root=root)


def _alpha_lab(**capable):
    """The ``alpha`` lab with the built-in runner; *capable* maps a host id to its docker_priority.

    A host *capable* does not name is not docker-capable. With no *capable*,
    ``a-dut`` ranks 0 and ``a-jump`` ranks 10.
    """
    ranks = capable or {"a-dut": 0, "a-jump": 10}
    lab = _lab(("a-dut", "alpha"), ("a-jump", "alpha"), component_names=["alpha"], subnet="192.0.2")
    add_builtin_local(lab)
    for host_id in ("a-dut", "a-jump"):
        host = lab.hosts[host_id]
        host.docker_capable = host_id in ranks
        host.docker_priority = ranks.get(host_id, 0)
    return lab


def _containers(lab):
    """The ids of the container hosts registered in *lab*, in lab order."""
    return [hid for hid, host in lab.hosts.items() if isinstance(host, DockerContainerHost)]


def _settings_file(project):
    return str(Path(project.root) / ".otto" / "settings.toml")


def _placement_view(lab, projects, *, exclude=()):
    """The build-time view placement reads, over the same switches a run would carry."""
    from otto.config.scope import ScopeSelection
    from otto.docker.compose import PlacementSource, placement_selection
    from otto.lab_view import ScopedLab

    selection = placement_selection(
        ScopeSelection(labs=tuple(lab.component_names), include=(), exclude=tuple(exclude)),
        component_names=lab.component_names,
    )
    return ScopedLab(PlacementSource(lab, projects, selection=selection))


# ── placement ────────────────────────────────────────────────────────────────


def test_placeholders_follow_scoped_default_parent(tmp_path):
    """The placeholder sits under the default parent among targetable hosts, not the whole lab's."""
    lab = _alpha_lab()
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])

    assert register_declared_container_hosts(lab, [inscope]) == 1
    assert _containers(lab) == ["a-dut.inscope.api"]

    # Nothing capable in scope: the rule refuses, so nothing is placed, and nothing raises.
    lab = _alpha_lab(**{"a-jump": 0})
    assert register_declared_container_hosts(lab, [inscope]) == 0
    assert _containers(lab) == []


def test_placeholders_only_for_participating_projects(tmp_path):
    """A project outside the lab's patterns or switched off gets no placeholder; -I restores one."""
    from otto.config.scope import ScopeSelection

    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    elsewhere = _docker_project(tmp_path, "elsewhere", labs=["beta"])
    switched = _docker_project(tmp_path, "switched", labs=["alpha"], hosts=["a-dut.*"])
    projects = [inscope, elsewhere, switched]

    lab = _alpha_lab()
    off = ScopeSelection(labs=("alpha",), include=(), exclude=("switched",))
    assert register_declared_container_hosts(lab, projects, selection=off) == 1
    assert _containers(lab) == ["a-dut.inscope.api"]

    lab = _alpha_lab()
    forced = ScopeSelection(labs=("alpha",), include=("elsewhere",), exclude=("switched",))
    assert register_declared_container_hosts(lab, projects, selection=forced) == 2
    assert _containers(lab) == ["a-dut.inscope.api", "a-dut.elsewhere.api"]


def test_direct_default_registration_normalizes(tmp_path):
    """With no selection the walk still judges the lab axis: the same participants as a run's."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    elsewhere = _docker_project(tmp_path, "elsewhere", labs=["beta"])
    lab = _alpha_lab()

    assert register_declared_container_hosts(lab, [inscope, elsewhere]) == 1
    assert _containers(lab) == ["a-dut.inscope.api"]


def test_unscoped_registration_places_over_the_whole_lab(tmp_path):
    """``unscoped=True`` picks the parent among every lab host: the lab as configured."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()

    assert register_declared_container_hosts(lab, [inscope], unscoped=True) == 1
    assert _containers(lab) == ["a-jump.inscope.api"]


def test_default_registration_does_not_raise(tmp_path):
    """A tie, or no docker-capable host at all, registers nothing and raises nothing."""
    tied = _docker_project(tmp_path, "tied", labs=["alpha"])
    lab = _alpha_lab(**{"a-dut": 0, "a-jump": 0})
    assert register_declared_container_hosts(lab, [tied]) == 0

    lab = _alpha_lab()
    for host_id in ("a-dut", "a-jump"):
        lab.hosts[host_id].docker_capable = False
    assert register_declared_container_hosts(lab, [tied]) == 0
    assert _containers(lab) == []


def test_placement_source_resolves_scopes_once(tmp_path, monkeypatch):
    """Every read of the placement view, and a host registered after it, shares one resolution."""
    from otto.host import builtin_hosts

    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    calls = []
    real = builtin_hosts.lab_scopes

    def _counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(builtin_hosts, "lab_scopes", _counted)
    view = _placement_view(lab, [inscope])

    assert "a-dut" in view.hosts
    assert "a-jump" not in view.hosts
    assert view.hosts.get("a-jump") is None
    assert view.hosts["a-dut"] is lab.hosts["a-dut"]
    with pytest.raises(HostOutOfScopeError):
        view.hosts["a-jump"]
    assert list(view.hosts) == ["a-dut", "local"]

    container = DockerContainerHost(
        parent=lab.hosts["a-dut"],
        container_id="",
        project="inscope",
        service="db",
        compose_project="alpha-inscope",
    )
    container.source_lab = "alpha"
    lab.add_host(container)
    assert container.id in view.hosts  # admitted live, by its parent
    assert len(calls) == 1


def test_placement_and_runtime_agree(tmp_path):
    """One input through the lab build's placement and the run's rule: one parent, one refusal.

    Placement is driven through ``register_declared_container_hosts``, as a
    lab build calls it; the run reads ``ctx.lab`` over the same lab and
    projects. Where a capable host is in scope, the placeholder sits under
    the parent the run's rule names, and only the projects the run takes
    part in get one; where none is, placement places nothing and the run's
    rule refuses.
    """
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    outscope = _docker_project(tmp_path, "outscope", labs=["beta"])
    projects = [inscope, outscope]

    lab = _alpha_lab()
    assert register_declared_container_hosts(lab, projects) == 1
    ctx = install_scoped_context(lab, projects)
    parent = default_docker_parent(lab=ctx.lab)
    placed = [lab.hosts[cid] for cid in _containers(lab)]
    assert [host.parent.id for host in placed] == [parent.id] == ["a-dut"]
    assert [host.project for host in placed] == [p.name for p in ctx.projects] == ["inscope"]

    starved = _alpha_lab(**{"a-jump": 0})
    assert register_declared_container_hosts(starved, projects) == 0
    assert _containers(starved) == []
    ctx = install_scoped_context(starved, projects)
    with pytest.raises(DockerVerbError) as refused:
        default_docker_parent(lab=ctx.lab)
    assert str(refused.value).startswith("no docker-capable host in lab 'alpha' is in scope")


def test_placement_judges_participation_before_the_lab_is_read(tmp_path):
    """Regression (Q8): placement keeps a host-starved project that the run drops.

    Placement judges which projects take part as a lab-free command does,
    by the loaded labs and the switches (tier P), so a project whose
    ``host_patterns`` match no host there still gets placeholders, under the
    parent another project puts in scope. The run judges it against the
    loaded lab's hosts (tier L), where it does not take part.
    """
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    starved = _docker_project(tmp_path, "starved", labs=["alpha"], hosts=["nothing-here"])
    projects = [inscope, starved]

    lab = _alpha_lab()
    assert register_declared_container_hosts(lab, projects) == 2
    assert _containers(lab) == ["a-dut.inscope.api", "a-dut.starved.api"]
    ctx = install_scoped_context(lab, projects)
    assert [p.name for p in ctx.projects] == ["inscope"]


# ── the run-time rule ────────────────────────────────────────────────────────


def test_no_capable_in_scope_names_outside_and_settings(tmp_path):
    """Q5: nothing capable in scope names the capable hosts outside and the files to widen."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    ctx = install_scoped_context(_alpha_lab(**{"a-jump": 0}), [inscope])
    expected = (
        "no docker-capable host in lab 'alpha' is in scope; capable but outside: a-jump; "
        f"widen host_patterns in {_settings_file(inscope)}"
    )
    for refuse in (
        lambda: default_docker_parent(lab=ctx.lab),
        lambda: docker_parents(lab=ctx.lab, parent=None),
    ):
        with pytest.raises(DockerVerbError) as refused:
            refuse()
        assert str(refused.value) == expected
        assert refused.value.field == "parent"


def test_no_capable_host_at_all_keeps_todays_text(tmp_path):
    """A lab with no docker-capable host anywhere says so, as before."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    for host_id in ("a-dut", "a-jump"):
        lab.hosts[host_id].docker_capable = False
    ctx = install_scoped_context(lab, [inscope])
    with pytest.raises(DockerVerbError, match=r"^no host in lab 'alpha' is marked docker_capable$"):
        default_docker_parent(lab=ctx.lab)


def test_named_out_of_scope_parent_refused_with_scope_text(tmp_path):
    """``docker_parent(parent=...)`` for an out-of-scope host raises the scope's own text."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    ctx = install_scoped_context(_alpha_lab(), [inscope])
    with pytest.raises(HostOutOfScopeError) as lookup:
        ctx.get_host("a-jump")
    with pytest.raises(DockerVerbError) as refused:
        docker_parent(lab=ctx.lab, parent="a-jump")
    assert str(refused.value) == str(lookup.value)
    assert refused.value.field == "parent"


def test_default_parent_among_targetable(tmp_path):
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    ctx = install_scoped_context(lab, [inscope])
    assert default_docker_parent(lab=lab).id == "a-jump"
    assert default_docker_parent(lab=ctx.lab).id == "a-dut"


def test_default_parent_tie_collapses_under_scope(tmp_path):
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab(**{"a-dut": 0, "a-jump": 0})
    ctx = install_scoped_context(lab, [inscope])
    with pytest.raises(DockerVerbError, match="2 docker-capable hosts at priority 0"):
        default_docker_parent(lab=lab)
    assert default_docker_parent(lab=ctx.lab).id == "a-dut"


def test_fan_out_targetable_only(tmp_path):
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    ctx = install_scoped_context(lab, [inscope])
    assert [h.id for h in docker_parents(lab=lab, parent=None)] == ["a-dut", "a-jump"]
    assert [h.id for h in docker_parents(lab=ctx.lab, parent=None)] == ["a-dut"]


def test_facts_read_whole_lab(tmp_path, monkeypatch):
    """The use-case facts resolve an address from the whole lab, handed the scoped view."""
    from otto.docker import resolve as resolve_mod
    from otto.docker.resolve import SelectedFragment, Selection, build_facts

    from .test_resolve_select import _frag
    from .test_resolve_select import _project as _fragment_project

    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    ctx = install_scoped_context(_alpha_lab(), [inscope])
    monkeypatch.setattr(resolve_mod, "scope_for_project", lambda _name: None)
    selection = Selection(
        use_case="integration", fragments=[SelectedFragment(_fragment_project("a"), _frag())]
    )
    facts = build_facts(
        selection, ctx.lab, compose_project="p", parent_id="a-jump", files={}, scratch_dir="/s"
    )
    assert facts["parent"] == {"id": "a-jump", "addr": "192.0.2.2"}
    assert "a-jump" in facts["hosts"]


# ── get_container_host ───────────────────────────────────────────────────────


def _container_on(lab, parent_id, service="api"):
    container = DockerContainerHost(
        parent=lab.hosts[parent_id],
        container_id="cid",
        project="inscope",
        service=service,
        compose_project="alpha-inscope",
    )
    container.source_lab = "alpha"
    lab.add_host(container)
    return container


def test_get_container_host_out_of_scope_refused(tmp_path):
    """A container whose parent is outside the scope is refused as ``get_host`` refuses it."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    container = _container_on(lab, "a-jump")
    ctx = install_scoped_context(lab, [inscope])
    with pytest.raises(HostOutOfScopeError) as lookup:
        ctx.get_host(container.id)
    with pytest.raises(HostOutOfScopeError) as refused:
        get_container_host(container.id)
    assert str(refused.value) == str(lookup.value)
    assert "outside this run's scope" in str(refused.value)


def test_get_container_host_empty_scope_fleet_text(tmp_path):
    """With nothing in play, the refusal is the empty-scope text that names every declaration."""
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    container = _container_on(lab, "a-dut")
    ctx = install_scoped_context(lab, [inscope], exclude_projects=("inscope",))
    empty = ctx.empty_scope_refusal()
    assert empty is not None
    with pytest.raises(HostOutOfScopeError) as refused:
        get_container_host(container.id)
    assert str(refused.value) == str(empty)


def test_get_container_host_missing_message_unchanged(tmp_path):
    inscope = _docker_project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    lab = _alpha_lab()
    container = _container_on(lab, "a-dut")
    install_scoped_context(lab, [inscope])
    assert get_container_host(container.id) is container
    for host_id in ("nope", "a-dut"):  # unknown, and a host that is no container
        with pytest.raises(KeyError) as missing:
            get_container_host(host_id)
        assert not isinstance(missing.value, HostOutOfScopeError)
        assert missing.value.args[0] == (
            f"No container host registered with id {host_id!r}. "
            f"Did you call `otto docker compose up` (or `compose_up`) first?"
        )


# ── the CLI's project reads ──────────────────────────────────────────────────


def test_use_cases_listing_participating_only(tmp_path, monkeypatch):
    """``docker use-cases`` and the deploy verbs' default use-case read participating projects."""
    from otto.cli import docker as docker_cli
    from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap
    from tests._fixtures.dispatch import DispatchRunner

    inscope = _docker_project(
        tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"], use_case="inuc"
    )
    outscope = _docker_project(tmp_path, "outscope", labs=["beta"], use_case="outuc")
    projects = [inscope, outscope]
    install_composed(monkeypatch, patch_bootstrap(monkeypatch, projects))
    install_scoped_context(_alpha_lab(), projects)

    result = DispatchRunner().invoke(
        docker_cli.docker_app, ["use-cases"], spec_name="docker", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "inuc" in result.output
    assert "outuc" not in result.output
    assert docker_cli._default_use_case(None) == "inuc"
