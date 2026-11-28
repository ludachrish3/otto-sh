"""A scoped command's lab view: only the hosts the run may target (spec 2026-11-24 §3.7, Q30-Q32).

``ScopedLab`` reads a run's canonical lab through the run's scope judge. Its
``hosts`` is a read-only mapping of the targetable hosts in the lab's order,
judged live; its links drop only a link with an endpoint in the lab and
outside the scope; every write reaches the canonical lab; and a merge refuses
from either side.

Real projects and real hosts through
:func:`tests._fixtures.fleet.scope_workspace`: ``inscope`` declares ``alpha`` /
``a-dut.*``, ``outscope`` declares ``beta`` / ``b-dut.*``, and ``a-jump`` /
``b-jump`` are targeted by neither. The view's module is imported inside the
tests, so the regression test here runs on the code before it.
"""

import dataclasses
import re

import pytest

from otto.config.scope import scopes_of
from tests._fixtures.fleet import (
    _host,
    _lab,
    _project,
    add_builtin_local,
    install_scoped_context,
    scope_workspace,
)

_MERGE = "a scoped lab view cannot be merged; merge ctx.unscoped.lab"
_READ_ONLY = (
    "ctx.lab.hosts is read-only in a scoped command; "
    "write through ctx.lab.add_host or ctx.unscoped.lab"
)


def _view(ctx):
    """The scoped view over *ctx*."""
    from otto.lab_view import ScopedLab

    return ScopedLab(ctx)


def _container(parent, service):
    """A docker container on *parent*, stamped with its parent's lab."""
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost(
        parent=parent,
        container_id=f"cid-{service}",
        project="r1",
        service=service,
        compose_project="otto-r1",
    )
    host.source_lab = parent.source_lab
    return host


def _link(a, b):
    """A declared link between hosts *a* and *b*."""
    from otto.link.model import Link, LinkEndpoint

    return Link(a=LinkEndpoint(host=a), b=LinkEndpoint(host=b))


def _ends(links):
    """Each link's endpoint host ids, in order."""
    return [(link.a.host, link.b.host) for link in links]


def _backlinks(lab):
    """Each remote host's lab backlink, by id."""
    from otto.host.remote_host import RemoteHost

    return {
        host_id: host._lab for host_id, host in lab.hosts.items() if isinstance(host, RemoteHost)
    }


# ── hosts ─────────────────────────────────────────────────────────────────────


def test_view_hosts_targetable_only_in_lab_order(tmp_path):
    """Fail-first: the targetable hosts, in the canonical lab's order, as the canonical objects.

    ``-l beta+alpha`` puts ``b-dut`` first; ``a-jump`` and ``b-jump`` are in
    the lab and in no declaration, so they are left out.
    """
    ws = scope_workspace(tmp_path, labs=("beta", "alpha"))
    view = _view(ws.ctx)

    assert list(view.hosts) == ["b-dut", "a-dut", "local"]
    assert len(view.hosts) == 3
    assert list(view.hosts) == ws.ctx.targetable_ids()
    # Membership agrees with iteration for an out-of-scope lab host.
    assert "a-jump" in ws.lab.hosts
    assert "a-jump" not in view.hosts
    assert "a-jump" not in list(view.hosts)
    for host_id, host in view.hosts.items():
        assert host is ws.lab.hosts[host_id]


def test_view_repr_shows_only_targetable_ids(tmp_path):
    """Fail-first: the view and its hosts print the lab's name and the targetable ids only."""
    ws = scope_workspace(tmp_path, labs=("beta", "alpha"))
    view = _view(ws.ctx)

    assert repr(view) == "ScopedLab(name='beta+alpha', hosts=['b-dut', 'a-dut', 'local'])"
    assert repr(view.hosts) == "_ScopedHosts(['b-dut', 'a-dut', 'local'])"
    assert "a-jump" not in repr(view)


def test_view_repr_never_raises(tmp_path, monkeypatch):
    """Fail-first: a view whose scope cannot be resolved still prints, by its name alone."""
    from otto.session.errors import ProjectLoadError

    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)

    def refused():
        raise ProjectLoadError("a project failed to load")

    monkeypatch.setattr(ws.ctx, "_host_scope", refused)

    assert repr(view) == "ScopedLab(name='alpha')"
    assert repr(view.hosts) == "_ScopedHosts(...)"


def test_view_lookup_refuses_as_get_host(tmp_path):
    """Fail-first: ``view.hosts[id]`` and ``get_host`` refuse one id with the same error.

    An out-of-scope lab host raises ``HostOutOfScopeError``; an id the lab
    does not hold raises the plain unknown-id ``KeyError``.
    """
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)

    for host_id in ("a-jump", "nope"):
        with pytest.raises(KeyError) as through_view:
            view.hosts[host_id]
        with pytest.raises(KeyError) as through_get_host:
            ws.ctx.get_host(host_id)
        assert type(through_view.value) is type(through_get_host.value)
        assert str(through_view.value) == str(through_get_host.value)
    with pytest.raises(HostOutOfScopeError):
        view.hosts["a-jump"]


def test_view_judges_container_registered_after_read(tmp_path):
    """Fail-first: a container registered after the view was read is judged when read.

    On the targetable ``a-dut`` it joins the view; on the out-of-scope
    ``a-jump`` it stays out.
    """
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    assert list(view.hosts) == ["a-dut", "local"]

    inside = _container(ws.lab.hosts["a-dut"], "db")
    outside = _container(ws.lab.hosts["a-jump"], "web")
    ws.lab.add_host(inside)
    ws.lab.add_host(outside)

    assert inside.id in view.hosts
    assert view.hosts[inside.id] is inside
    assert view.hosts.get(inside.id) is inside
    assert outside.id not in view.hosts
    assert list(view.hosts) == ["a-dut", "local", inside.id]


def test_write_through_view_hosts_names_unscoped(tmp_path):
    """Fail-first: every write through ``view.hosts`` refuses, naming the two ways to write.

    The canonical lab is left as it was.
    """
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    before = dict(ws.lab.hosts)
    container = _container(ws.lab.hosts["a-dut"], "db")
    hosts = view.hosts

    writes = {
        "assign": lambda: hosts.__setitem__(container.id, container),
        "delete": lambda: hosts.__delitem__("a-dut"),
        "pop": lambda: hosts.pop("a-dut"),
        "update": lambda: hosts.update({container.id: container}),
        "setdefault": lambda: hosts.setdefault(container.id, container),
        "clear": hosts.clear,
    }
    for name, write in writes.items():
        with pytest.raises(TypeError, match=re.escape(_READ_ONLY)):
            write()
        assert ws.lab.hosts == before, name


def test_add_host_through_view_reaches_canonical(tmp_path):
    """Fail-first: ``view.add_host`` registers in the canonical lab, and the view then holds it."""
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    container = _container(ws.lab.hosts["a-dut"], "db")

    view.add_host(container)

    assert ws.lab.hosts[container.id] is container
    assert view.hosts[container.id] is container


def test_backlink_is_canonical(tmp_path):
    """Fail-first: every host's backlink is the canonical lab, one added through the view too."""
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    added = _host("a-dut2", "alpha", 9, subnet="192.0.2")

    view.add_host(added)

    assert added.id in view.hosts
    backlinks = _backlinks(ws.lab)
    assert added.id in backlinks
    assert all(lab is ws.lab for lab in backlinks.values())


def test_membership_and_get_judge_one_id(tmp_path, monkeypatch):
    """Fail-first: ``in`` and ``.get`` judge the one id once and never build the fleet.

    A known-outside id (``a-jump``), an unknown id and an in-scope id each
    call the judge's ``admits`` once; the whole-fleet reads
    (``targetable_ids``, the refusal, the hosts in play) are never made.
    """
    from otto.context import OttoContext
    from otto.host.targeting import HostScope

    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    scopes_of(ws.ctx)  # resolved ahead, so only the reads are measured
    judged: list[str] = []
    admits = HostScope.admits

    def _counted(self, host_id):
        judged.append(host_id)
        return admits(self, host_id)

    def _refused(*_args, **_kwargs):
        raise AssertionError("a one-id read read the whole fleet")

    monkeypatch.setattr(HostScope, "admits", _counted)
    monkeypatch.setattr(HostScope, "targetable_ids", _refused)
    monkeypatch.setattr(HostScope, "refusal", _refused)
    monkeypatch.setattr(OttoContext, "_hosts_in_play", _refused)
    sentinel = object()

    reads = [
        ("a-jump", lambda: "a-jump" in view.hosts, False),
        ("a-jump", lambda: view.hosts.get("a-jump"), None),
        ("a-jump", lambda: view.hosts.get("a-jump", sentinel), sentinel),
        ("nope", lambda: "nope" in view.hosts, False),
        ("nope", lambda: view.hosts.get("nope"), None),
        ("a-dut", lambda: "a-dut" in view.hosts, True),
        ("a-dut", lambda: view.hosts.get("a-dut"), ws.lab.hosts["a-dut"]),
    ]
    for host_id, read, expected in reads:
        judged.clear()
        assert read() is expected
        assert judged == [host_id]


# ── links ─────────────────────────────────────────────────────────────────────


def test_links_drop_only_in_lab_out_of_scope_endpoints(tmp_path):
    """Fail-first: a link with an in-lab, out-of-scope endpoint drops; the rest stay, as they are.

    Declared links and the implicit hop edges alike: under ``alpha+beta``
    ``a-jump`` and ``b-jump`` are out, so ``local`` to ``b-jump`` and the
    ``a-dut`` to ``a-jump`` hop edge drop while ``local`` to ``b-dut`` stays.
    """
    ws = scope_workspace(tmp_path, labs=("alpha", "beta"))
    kept, dropped = _link("a-dut", "b-dut"), _link("a-dut", "a-jump")
    ws.lab.links = [kept, dropped]
    view = _view(ws.ctx)
    outside = {"a-jump", "b-jump"}

    assert view.links == (kept,)
    canonical = ws.lab.static_links()
    assert any(outside & {link.a.host, link.b.host} for link in canonical)
    expected = [link for link in canonical if not outside & {link.a.host, link.b.host}]
    assert view.static_links() == expected
    assert ("local", "b-dut") in _ends(expected)


def test_view_keeps_link_with_unloaded_endpoint(tmp_path):
    """Fail-first: a link to a host the loaded lab does not hold stays in the view (Q32).

    With only ``alpha`` loaded, ``b-dut`` is not in the lab, so the link from
    the targetable ``a-dut`` stays; ``link list`` shows it as not impairable.
    """
    ws = scope_workspace(tmp_path)
    unloaded = _link("a-dut", "b-dut")
    ws.lab.links = [unloaded]
    view = _view(ws.ctx)

    assert "b-dut" not in ws.lab.hosts
    assert view.links == (unloaded,)
    assert unloaded in view.static_links()


def test_view_links_refuse_a_write(tmp_path):
    """Fail-first: ``ctx.lab.links`` is immutable, so a write fails loudly instead of vanishing.

    Each read is a fresh filtered snapshot; a write goes through
    ``ctx.unscoped.lab.links``, and the view then reads it.
    """
    ws = scope_workspace(tmp_path)
    link = _link("a-dut", "b-dut")
    view = _view(ws.ctx)

    assert isinstance(view.links, tuple)
    with pytest.raises(AttributeError):
        view.links.append(link)  # ty: ignore[unresolved-attribute] — the refused write
    assert view.links == ()
    ws.ctx.unscoped.lab.links.append(link)
    assert view.links == (link,)


def test_undeclared_view_links_equal_canonical(tmp_path):
    """Fail-first, by the import only: with no declaration the view's hosts and links are the lab's.

    The behaviour it pins is today's: no project declares a scope, so every
    lab host is targetable and no link drops.
    """
    lab = add_builtin_local(_lab(("a-dut", "alpha"), ("a-jump", "alpha"), hops={"a-dut": "a-jump"}))
    lab.links = [_link("a-dut", "a-jump"), _link("a-jump", "elsewhere")]
    ctx = install_scoped_context(lab, [_project(tmp_path, "plain")])
    view = _view(ctx)

    assert list(view.hosts) == list(lab.hosts)
    assert list(view.links) == lab.links
    assert view.static_links() == lab.static_links()


# ── forwarded fields ──────────────────────────────────────────────────────────


def test_forwarded_fields_keep_merged_name_and_summary(tmp_path):
    """Fail-first: name, components, resources, metadata and summary are the canonical lab's.

    A merged ``alpha + beta`` keeps its merged name and components, though the
    view holds only ``alpha``'s host.
    """
    alpha = _lab(("a-dut", "alpha"))
    alpha.resources = {"bench-a"}
    alpha.metadata = {"alpha": {"rack": 1}}
    beta = _lab(("b-dut", "beta"), subnet="192.0.3")
    beta.resources = {"bench-b"}
    beta.metadata = {"beta": {"rack": 2}}
    merged = alpha + beta
    project = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    view = _view(install_scoped_context(merged, [project]))

    assert list(view.hosts) == ["a-dut"]
    assert view.name == "alpha+beta"
    assert view.component_names is merged.component_names
    assert view.component_names == ["alpha", "beta"]
    assert view.resources is merged.resources
    assert view.metadata is merged.metadata
    assert view.__otto_summary__() == merged.__otto_summary__() == "<Lab alpha+beta>"


# ── merge and replace ─────────────────────────────────────────────────────────


def test_merge_refused_both_orders_backlinks_unchanged(tmp_path):
    """Fail-first: ``lab + view``, ``view + lab`` and ``sum([view])`` refuse, and change nothing.

    The canonical lab and another lab keep their names, hosts and backlinks.
    """
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)
    other = _lab(("c-dut", "gamma"), subnet="192.0.4")
    labs = (ws.lab, other)
    before = [
        (lab.name, list(lab.component_names), dict(lab.hosts), _backlinks(lab)) for lab in labs
    ]

    merges = {
        "lab + view": lambda: ws.lab + view,
        "other + view": lambda: other + view,
        "view + lab": lambda: view + ws.lab,
        "view + other": lambda: view + other,
        "sum([view])": lambda: sum([view]),
        "sum([view], other)": lambda: sum([view], other),
    }
    for name, merge in merges.items():
        with pytest.raises(TypeError, match=re.escape(_MERGE)):
            merge()
        after = [
            (lab.name, list(lab.component_names), dict(lab.hosts), _backlinks(lab)) for lab in labs
        ]
        assert after == before, name


def test_replace_refused(tmp_path):
    """Fail-first: ``dataclasses.replace`` on a view raises, and the canonical lab is unchanged."""
    ws = scope_workspace(tmp_path)
    view = _view(ws.ctx)

    with pytest.raises(TypeError):
        dataclasses.replace(view)
    with pytest.raises(TypeError):
        dataclasses.replace(view, name="other")
    assert ws.lab.name == "alpha"


def test_lab_add_non_lab_operand_unchanged(tmp_path):
    """Regression: ``Lab + Lab`` merges as before, and a non-lab operand fails as before.

    The merge refusal reaches only a scoped view: two labs merge, re-pointing
    the added hosts' backlinks; an operand that is no lab at all still raises
    its ``AttributeError`` before the lab changes.
    """
    alpha = _lab(("a-dut", "alpha"))
    beta = _lab(("b-dut", "beta"), subnet="192.0.3")

    with pytest.raises(AttributeError):
        alpha + 5
    assert alpha.name == "alpha"

    merged = alpha + beta
    assert merged is alpha
    assert merged.name == "alpha+beta"
    assert list(merged.hosts) == ["a-dut", "b-dut"]
    assert all(lab is merged for lab in _backlinks(merged).values())


# ── ctx.lab and get_lab() ─────────────────────────────────────────────────────


def _unreadable(_view):
    """The patched ``ScopedLab.hosts``: a reader that reaches it fails the test."""
    raise AssertionError("a core path read the scoped view's hosts")


def test_scoped_command_lab_is_view(tmp_path):
    """Fail-first: a scoped command's ``ctx.lab`` and ``get_lab()`` are one view of its targets.

    The view is exported from ``otto.lab``, holds the targetable hosts in the
    lab's order, and is the one a project's context view reads too; the
    canonical lab stays behind ``ctx.unscoped.lab``.
    """
    import otto.lab
    from otto.lab_view import ScopedLab

    ws = scope_workspace(tmp_path, labs=("alpha", "beta"))
    view = ws.ctx.lab

    assert otto.lab.ScopedLab is ScopedLab
    assert isinstance(view, ScopedLab)
    assert otto.lab.get_lab() is view
    assert ws.ctx.lab is view
    assert ws.ctx.for_project(name="inscope").lab is view
    assert list(view.hosts) == ws.ctx.targetable_ids() == ["a-dut", "b-dut", "local"]
    assert "a-jump" not in view.hosts
    assert ws.ctx.unscoped.lab is ws.lab


def test_unscoped_command_lab_is_canonical(tmp_path):
    """Regression: an opted-out command's ``ctx.lab`` and ``get_lab()`` are the canonical lab."""
    from otto.fleet import get_lab

    lab = add_builtin_local(_lab(("a-dut", "alpha"), ("a-jump", "alpha"), subnet="192.0.2"))
    inscope = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    ctx = install_scoped_context(lab, [inscope], scoped=False)

    assert ctx.lab is lab
    assert get_lab() is lab
    assert list(ctx.lab.hosts) == ["a-dut", "a-jump", "local"]


def test_retained_view_follows_lab_replacement(tmp_path):
    """Fail-first (Review Focus 4): a view kept across ``ctx.lab = other`` reads the new lab.

    The projects, the view's hosts and the scope verdicts are all read over
    ``alpha`` first, then the lab is replaced with ``alpha+beta``: the verdicts
    are resolved again (``outscope`` now targets ``b-dut``, so it takes part),
    and the kept view lists the new lab's targetable hosts, as its canonical
    objects.
    """
    from otto.lab_view import ScopedLab

    ws = scope_workspace(tmp_path)
    ctx = ws.ctx
    kept = ctx.lab
    assert isinstance(kept, ScopedLab)
    assert [project.name for project in ctx.projects] == ["inscope"]
    assert list(kept.hosts) == ["a-dut", "local"]
    assert "b-dut" not in scopes_of(ctx)["outscope"].universe

    wider = add_builtin_local(
        _lab(
            ("a-dut", "alpha"),
            ("a-jump", "alpha"),
            ("b-dut", "beta"),
            ("b-jump", "beta"),
            component_names=["alpha", "beta"],
            subnet="192.0.2",
        )
    )
    ctx.lab = wider

    assert ctx.unscoped.lab is wider
    assert "b-dut" in scopes_of(ctx)["outscope"].universe
    assert [project.name for project in ctx.projects] == ["inscope", "outscope"]
    assert kept.name == "alpha+beta"
    assert list(kept.hosts) == ["a-dut", "b-dut", "local"]
    assert kept.hosts["b-dut"] is wider.hosts["b-dut"]
    assert list(ctx.lab.hosts) == list(kept.hosts)


def test_lab_setter_drops_a_cached_refusal(tmp_path, monkeypatch):
    """Regression: a refused scope read is not re-raised over the lab that replaced it."""
    from otto.session.errors import ProjectLoadError

    ws = scope_workspace(tmp_path)
    resolve = ws.ctx._resolve_verdicts

    def refused():
        raise ProjectLoadError("a project failed to load")

    monkeypatch.setattr(ws.ctx, "_resolve_verdicts", refused)
    for _ in range(2):  # cached: the second read re-raises without resolving again
        with pytest.raises(ProjectLoadError):
            ws.ctx.targetable_ids()
    monkeypatch.setattr(ws.ctx, "_resolve_verdicts", resolve)
    with pytest.raises(ProjectLoadError):
        ws.ctx.targetable_ids()

    ws.ctx.lab = ws.lab

    assert ws.ctx.targetable_ids() == ["a-dut", "local"]


def test_setter_unwraps_view(tmp_path):
    """Fail-first: a context given a view, assigned or at construction, takes its canonical lab.

    Assigning another context's view takes that context's lab; assigning the
    context its own view changes nothing.
    """
    from otto.context import OttoContext
    from otto.lab_view import ScopedLab

    ws = scope_workspace(tmp_path)
    beta = add_builtin_local(_lab(("b-dut", "beta"), subnet="192.0.3"))
    other = OttoContext(lab=beta)

    ws.ctx.lab = ScopedLab(other)
    assert ws.ctx.unscoped.lab is beta
    assert isinstance(ws.ctx.lab, ScopedLab)
    assert ws.ctx.lab.name == "beta"

    ws.ctx.lab = ws.ctx.lab
    assert ws.ctx.unscoped.lab is beta

    assert OttoContext(lab=ScopedLab(ws.ctx)).unscoped.lab is beta


def test_fleet_of_interest_accepts_view(tmp_path):
    """Fail-first: ``fleet_of_interest`` reads a view through to its lab; the projects alone decide.

    Handed the run's view, it answers what it answers for the canonical lab,
    for no declaration (the whole lab), one, and both.
    """
    from otto.fleet import fleet_of_interest
    from otto.lab_view import ScopedLab

    ws = scope_workspace(tmp_path, labs=("alpha", "beta"))
    view = ScopedLab(ws.ctx)

    for projects in ([], [ws.inscope], [ws.inscope, ws.outscope]):
        assert fleet_of_interest(view, projects=projects) == fleet_of_interest(
            ws.lab, projects=projects
        )
    assert fleet_of_interest(view, projects=[]) == ["a-dut", "a-jump", "b-dut", "b-jump"]


def test_core_never_reads_the_view(tmp_path, monkeypatch):
    """Fail-first: the core reads answer with the view's ``hosts`` unreadable.

    Scope resolution, admission, the walks (a project's included), a lookup
    and its refusal, the hosts in play, the reservation requirement, the
    fleet-of-interest query and the peer resolver all read the canonical lab.
    Its precondition, that ``ctx.lab`` is the view, fails before the view is
    handed out; the reads it then guards would pass there too.
    """
    from otto.config.scope import HostOutOfScopeError
    from otto.fleet import fleet_of_interest, get_hosts_in_play
    from otto.lab_view import ScopedLab
    from otto.reservations import ReservationGate, ResolvedIdentity

    ws = scope_workspace(tmp_path, labs=("alpha", "beta"))
    ctx = ws.ctx
    assert isinstance(ctx.lab, ScopedLab)
    monkeypatch.setattr(ScopedLab, "hosts", property(_unreadable))

    assert set(scopes_of(ctx)) == {"inscope", "outscope"}
    assert ctx.targetable_ids() == ["a-dut", "b-dut", "local"]
    assert {"a-dut", "b-dut"} <= ctx.admissible_ids()
    assert "a-jump" not in ctx.admissible_ids()
    assert [host.id for host in ctx.all_hosts()] == ["a-dut", "b-dut"]
    assert [host.id for host in ctx.for_project(name="inscope").all_hosts()] == ["a-dut"]
    assert ctx.get_host("a-dut") is ws.lab.hosts["a-dut"]
    with pytest.raises(HostOutOfScopeError):
        ctx.get_host("a-jump")
    assert ctx._hosts_in_play() == {"a-dut", "b-dut"}
    assert get_hosts_in_play() == {"a-dut", "b-dut"}
    gate = ReservationGate(skip_check=True, identity=ResolvedIdentity(username="u", source="$USER"))
    assert gate.evaluate().skipped is True
    assert fleet_of_interest(ctx.lab, projects=ctx.unscoped.projects) == ["a-dut", "b-dut"]

    # No host keeps a backlink, so each hop resolves through the installed resolver.
    for host in ws.lab.hosts.values():
        if getattr(host, "_lab", None) is not None:
            host._lab = None
    jump = ws.lab.hosts["a-dut"].hop_host()
    assert jump is ws.lab.hosts["a-jump"]
    assert jump.hop_host() is ws.lab.hosts["b-jump"]


def test_self_lockout_guard_reads_past_the_view(tmp_path, monkeypatch):
    """Regression: the hop-dependent walk, handed ``ctx.lab``, reads the whole lab.

    ``a-jump`` is outside the run's scope and still depends on ``b-jump``, so
    an impairment there would cut it off; the view's ``hosts`` is unreadable.
    """
    from otto.lab_view import ScopedLab
    from otto.link.manage import _hop_dependents

    ws = scope_workspace(tmp_path, labs=("alpha", "beta"))
    monkeypatch.setattr(ScopedLab, "hosts", property(_unreadable))

    assert [host_id for host_id, _ip in _hop_dependents(ws.ctx.lab, "b-jump")] == [
        "a-dut",
        "a-jump",
    ]


# ── settings files ────────────────────────────────────────────────────────────


def test_settings_files_of_view_and_lab(tmp_path):
    """A scoped view names its judge's files to widen; any other lab names none."""
    from otto.lab_view import settings_files_of

    ws = scope_workspace(tmp_path, exclude_projects=("outscope",))
    assert settings_files_of(_view(ws.ctx)) == ws.ctx._host_scope().settings_files()
    assert settings_files_of(_view(ws.ctx)) == [str(ws.inscope.root / ".otto" / "settings.toml")]
    assert settings_files_of(ws.lab) == []
