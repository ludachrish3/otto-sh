"""The orchestrator — cross-project composition, one debug sweep, and converge.

Everything here is about what the LAB does, not what one project does: the walk
order (dependencies first to build, dependents first to tear down), what a
failure stops and what it does not, and the host-global steps that belong to no
project at all (the debug sweep, the toolchain tools).

THE CONTEXT IS MONKEYPATCHED WHERE THE ORCHESTRATOR LOOKS IT UP.
:func:`otto.project.orchestrator._lab` imports ``get_context`` inside the call
(the package's circular-import idiom), so the attribute read happens on
``otto.context`` at call time and patching that name is what the module
actually sees. The projects come off the context it returns (``ctx.projects`` and
``ctx.ordered_projects``), so the double carries them.

No registry-isolation fixture here, deliberately: the tests register recording
``ProjectActions`` subclasses, and the root conftest's autouse
``_isolate_registries`` reaches ``PROJECT_ACTIONS`` dynamically (the same
reasoning as ``test_actions.py``). They register through
:func:`_register_actions`, which enters the project's init-import marker (the
table refuses a registration outside one) and passes ``overwrite=True``, since
a test that wires two labs in a row registers a project's actions twice.
"""

import io
import logging
import re
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.highlighter import NullHighlighter
from rich.logging import RichHandler
from rich.markup import escape

from otto import project
from otto.bootstrap import ProjectScopeError
from otto.config.dependencies import ResolvedDependency
from otto.config.scope import (
    ProjectScope,
    ProjectScopeConfig,
    require_nonempty_fleet,
    scoped_ids,
    scopes_of,
)
from otto.context import OttoContext
from otto.errors import OttoError
from otto.link import (
    DirectionState,
    FlowDirection,
    ImpairmentParams,
    Link,
    LinkEndpoint,
    LinkState,
    RepairAllReport,
    RepairReport,
    Selector,
)
from otto.logger.formatters import RichFormatter
from otto.models.dependencies import normalize_name
from otto.project import (
    Cleanliness,
    CleanlinessKind,
    CleanupOptions,
    GetLogsOptions,
    InstallOptions,
    InstallState,
    InstallToolsOptions,
    ProjectActions,
    UninstallOptions,
    UninstallToolsOptions,
    orchestrator,
    register_project_actions,
)
from otto.project.orchestrator import InactiveRequiredDependencyError
from otto.registry import registering_project
from otto.result import CommandNotRunError, Result
from otto.tunnel import (
    DiscoveredTunnel,
    DryRunPlan,
    RemovedReport,
    Tunnel,
    TunnelDiscovery,
    TunnelHop,
)
from otto.utils import Status
from tests._fixtures.bootstrap_seam import seed_scope_verdicts
from tests._fixtures.fake_project import fake_project
from tests._fixtures.log_text import shown

# ── doubles ──────────────────────────────────────────────────────────────


class _FakeItem:
    """Product / dev-tool double: an owner stamp and a fixed install state."""

    def __init__(self, owner, installed=False):
        self.owner = owner
        self.installed = installed

    async def is_installed(self, host):
        del host
        return self.installed


class _FakeHost:
    """Recording host double for the orchestrator's four host-global sweeps."""

    def __init__(self, host_id, events, products=(), dev_tools=(), toolchain_absent=True):
        self.id = host_id
        self.products = list(products)
        self.dev_tools = list(dev_tools)
        self.toolchain_absent = toolchain_absent
        self.events = events
        self._scripted = {}

    def script(self, verb, outcome):
        """Make *verb* return *outcome* — or raise it, when it is an exception."""
        self._scripted[verb] = outcome

    def _record(self, verb, default):
        self.events.append((self.id, verb))
        outcome = self._scripted.get(verb, default)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_debug_logs(self, dest=None):
        del dest
        return self._record("get_debug_logs", Result(Status.Success))

    async def install_toolchain_tools(self):
        return self._record("install_toolchain_tools", Result(Status.Success))

    async def remove_toolchain_tools(self):
        return self._record("remove_toolchain_tools", Result(Status.Success))

    async def toolchain_tools_absent(self):
        return self._record("toolchain_tools_absent", self.toolchain_absent)


def _register_actions(project: str, cls: "type[ProjectActions]") -> None:
    """Register *cls* as *project*'s actions, as that project's init import would."""
    with registering_project(project):
        register_project_actions(cls, overwrite=True)


def _fake_lab(links=()):
    """A Lab double: an identity to pass through, and the static links it declares.

    ``static_links()`` is the one method the orchestrator itself calls (to ask
    :func:`~otto.link.placement.impairment_refusal` which skipped links could
    never have been impaired); everything else about the lab is
    ``otto.link``'s / ``otto.tunnel``'s business, and those are doubles here.
    """
    return SimpleNamespace(name="fake-lab", static_links=lambda: list(links))


def _link(link_id="core", *, impairable=True):
    """One static link — interfaced on both ends, or the shape no command can act on.

    ``impairable=False`` is the implicit-hop-edge shape:
    :func:`~otto.link.derive.implicit_links` builds endpoints with no named
    interface, which is exactly what ``impairment_refusal`` refuses.
    """
    iface = "eth1" if impairable else None
    return Link(a=LinkEndpoint("h0", iface), b=LinkEndpoint("h1", iface), id=link_id)


class _FakeCtx:
    """OttoContext double — the two dispatch seams the orchestrator uses, and the lab.

    ``lab`` is near enough a sentinel: the orchestrator reads only
    ``static_links()`` off it and otherwise HANDS it to ``otto.link`` /
    ``otto.tunnel``, and the tests assert the object those two were given is
    this one.
    """

    # The real seam, borrowed rather than reimplemented: ``actions_for`` builds
    # every project's actions around ``ctx.for_project(name)``, so this double is
    # asked for one on each level-1 hop. ``for_project`` only wraps, and a second
    # copy of that wiring here would be a copy that can drift.
    for_project = OttoContext.for_project
    # ``scopes_of(ctx)`` reads the verdicts through the context's own resolver,
    # borrowed for the same reason; seeded below, it returns them unresolved.
    _resolve_scopes = OttoContext._resolve_scopes

    def __init__(self, hosts, lab=None, scopes=None, include_projects=(), exclude_projects=()):
        self.hosts = list(hosts)
        self.lab = _fake_lab() if lab is None else lab
        # The resolver's verdicts, as ``scopes_of(ctx)`` hands them over.
        # An empty mapping is the whole-lab fallback (§6) and the shape every
        # test above this one runs under: nothing declared, nothing narrowed.
        seed_scope_verdicts(self, scopes or {})
        # The ``-I`` / ``-E`` switch axis, TUPLES exactly as ``OttoContext``
        # stores them (one entry per switch the user typed, un-normalized —
        # ``otto.config.scope._switched`` is what normalizes, on the read side).
        # Empty defaults are what keep every test above this one on the
        # no-switch path, where ``active()`` is the lab verdict alone.
        self.include_projects = tuple(include_projects)
        self.exclude_projects = tuple(exclude_projects)

    def all_hosts(self, _scope_owner=None):
        return iter(self.hosts)

    async def do_for_all_hosts(self, method, *args, _scope_owner=None, **kwargs):
        """Apply *method* per host EXACTLY as the production seam does.

        Two properties, both load-bearing. The call shape is ``method(host,
        ...)`` -- the function object it was handed, applied to the host, with
        NO name lookup (``otto/run_context.py``'s ``method(h, *args, **kwargs)``).
        An earlier version dispatched by ``method.__name__``, which is dynamic
        where the real seam is static: it certified host-class overrides the
        production walks did not honour, and hid that bug from every gate.
        Second, exceptions are CAPTURED as values rather than propagated.
        """
        out = {}
        for host in self.hosts:
            try:
                out[host.id] = await method(host, *args, **kwargs)
            except Exception as exc:  # noqa: PERF203,BLE001 — mirrors do_for_all_hosts' capture
                out[host.id] = exc
        return out


def _recording_actions(
    events, flags, questions=None, failing=None, state=None, dirty=(), declining=None
):
    """A ``ProjectActions`` subclass recording ``(project, verb)`` and the flags it got.

    *failing* is one ``(project, verb)`` pair that answers Failed; *declining* is
    one that answers Skipped (``"<verb> kept something"``); *state* scripts
    ``status()`` (one state for every project, or a ``{project: state}`` mapping when
    they differ); *dirty* names the projects whose ``is_clean()`` answers False.

    Questions (``status``/``is_clean``) land in *questions*, never in *events*:
    ``events`` is the list of things the lab was made to DO, and the two are
    kept apart because a project this run does not apply to must appear in
    NEITHER — a status that asks an excluded project for its state has contacted
    hosts outside the fleet of interest just as surely as an install would.
    """
    questions = [] if questions is None else questions

    class _Recording(ProjectActions):
        async def _note(self, verb, **kwargs):
            events.append((self.project.name, verb))
            flags.append((self.project.name, verb, kwargs))
            if failing == (self.project.name, verb):
                return Result(Status.Failed, msg=f"{verb} refused")
            if declining == (self.project.name, verb):
                return Result(Status.Skipped, msg=f"{verb} kept something")
            return Result(Status.Success)

        async def install(self, opts):
            del opts
            return await self._note("install")

        async def uninstall(self, opts):
            return await self._note("uninstall", product_logs=opts.product_logs)

        async def cleanup(self, opts):
            return await self._note("cleanup", product_logs=opts.product_logs)

        async def get_logs(self, opts):
            return await self._note(
                "get_logs",
                product_logs=opts.product_logs,
                require_product_logs=opts.require_product_logs,
            )

        async def install_tools(self, opts):
            return await self._note("install_tools", dev=opts.dev, toolchain=opts.toolchain)

        async def uninstall_tools(self, opts):
            return await self._note("uninstall_tools", dev=opts.dev, toolchain=opts.toolchain)

        async def status(self, opts):
            del opts
            questions.append((self.project.name, "status"))
            if isinstance(state, dict):
                return state[self.project.name]
            return state if state is not None else await super().status()

        async def is_clean(self):
            questions.append((self.project.name, "is_clean"))
            return self.project.name not in dirty

    return _Recording


def _wire_lab(monkeypatch, project_names, ctx, current=None, dependencies=None):
    """Point the orchestrator's context lookup at *ctx*, carrying *project_names* and the driving
    project.

    TWO PROJECT LISTS, DELIBERATELY DIFFERENT. ``ctx.ordered_projects`` hands back
    the dependency-topological walk order (dependencies first), while
    ``ctx.projects`` hands back bootstrap's own ``OTTOPATH`` order, whose
    FIRST entry is the driving project — the project D3 aborts for. *current*
    defaults to the LAST name in walk order, which is where a dependent sits,
    so a gate that read the walk order's first element would be asking about a
    dependency and these fixtures would catch it.

    EVERY fake project carries ``dependencies``, empty by default, because the
    real :class:`~otto.config.project.Project` always does (a ``field`` with
    ``default_factory=list``, populated by bootstrap's resolution pass). A
    double that omitted it would make ``for dep in project.dependencies`` look
    like it needed a ``getattr`` guard the production type never wants.
    *dependencies* maps a project name to the resolved entries it declares.
    """
    deps = dependencies or {}
    ordered = [fake_project(name, dependencies=list(deps.get(name, []))) for name in project_names]
    driving = current if current is not None else (project_names[-1] if project_names else None)
    configured = [project for project in ordered if project.name == driving]
    configured += [project for project in ordered if project.name != driving]
    # The same list objects on every read, unlike a real context's fresh copies:
    # the walk must never rewrite a list it is handed, and only a shared list
    # lets a test see that it did not.
    ctx.projects = configured
    ctx.ordered_projects = ordered
    monkeypatch.setattr("otto.run_context.get_context", lambda: ctx)
    return ordered


def _wire_infra(monkeypatch, events, *, repair=None, reap=None, states=(), discovery=None):
    """Point the four lab-infrastructure seams at doubles, and record every call.

    PATCHED ON ``otto.link.manage`` / ``otto.tunnel.manage`` /
    ``otto.tunnel.discovery``, which is where the orchestrator looks them up:
    it imports all four inside the call (the package's circular-import idiom,
    the same reasoning as ``_lab``), so the attribute read happens on those
    modules at call time.

    The two MUTATING seams also append to *events*, so cleanup's ordering is
    asserted against one list. The two READING seams do not — ``events`` is
    what the lab was made to DO — but every call of all four lands in the
    returned list, which is what lets ``status`` prove it asked neither.
    """
    calls = []

    async def _repair_all(lab):
        calls.append(("repair_all", lab))
        events.append(("lab", "repair_all"))
        return RepairAllReport() if repair is None else repair

    async def _remove_all_tunnels(lab):
        calls.append(("remove_all_tunnels", lab))
        events.append(("lab", "remove_all_tunnels"))
        return RemovedReport([], {}, [], []) if reap is None else reap

    async def _read_link_states(lab):
        calls.append(("read_link_states", lab))
        return list(states)

    async def _discover_tunnels(lab):
        calls.append(("discover_tunnels", lab))
        return TunnelDiscovery([], []) if discovery is None else discovery

    monkeypatch.setattr("otto.link.manage.repair_all", _repair_all)
    monkeypatch.setattr("otto.link.manage.read_link_states", _read_link_states)
    monkeypatch.setattr("otto.tunnel.manage.remove_all_tunnels", _remove_all_tunnels)
    monkeypatch.setattr("otto.tunnel.discovery.discover_tunnels", _discover_tunnels)
    return calls


def _wire(  # noqa: PLR0913 — one independently-optional knob per axis of the lab being wired
    monkeypatch,
    projects,
    hosts=0,
    *,
    repair=None,
    reap=None,
    states=(),
    discovery=None,
    links=(),
    scopes=None,
    current=None,
    include_projects=(),
    exclude_projects=(),
    dependencies=None,
    **actions_kwargs,
):
    """A lab of *projects* (topological order) with recording actions and *hosts* fleet hosts."""
    events, flags, questions = [], [], []
    cls = _recording_actions(events, flags, questions, **actions_kwargs)
    for name in projects:
        _register_actions(name, cls)
    fleet = [_FakeHost(f"h{i}", events) for i in range(hosts)]
    ctx = _FakeCtx(
        fleet,
        lab=_fake_lab(links),
        scopes=scopes,
        include_projects=include_projects,
        exclude_projects=exclude_projects,
    )
    ordered = _wire_lab(monkeypatch, projects, ctx, current=current, dependencies=dependencies)
    calls = _wire_infra(
        monkeypatch, events, repair=repair, reap=reap, states=states, discovery=discovery
    )
    return SimpleNamespace(
        events=events,
        flags=flags,
        questions=questions,
        hosts=fleet,
        ctx=ctx,
        ordered=ordered,
        infra=calls,
    )


def _verbs(events, *names):
    """The recorded event names, narrowed to *names* when given."""
    return [e[1] for e in events if not names or e[1] in names]


def _scope(
    name,
    *,
    excluded=False,
    declared=True,
    loaded=("bench", "floor"),
    universe=("h0",),
    host_patterns=(".*",),
    config=None,
):
    """One resolver verdict for *name* — the REAL frozen dataclass, never a stub.

    ``excluded`` is "declared, and no loaded lab applies", so it empties the
    two fields that follow from applicability; a stub that let those disagree
    could certify an orchestrator reading the wrong one.

    ``universe=()`` is the OTHER unusable shape and is left free of
    ``excluded``: a project whose labs DO apply while its ``host_patterns`` match
    no host there keeps its ``applicable_labs``, which is what makes the two
    conditions — and the two messages — tellable apart.

    ``config`` is the COMPILED declaration, and it stays ``None`` for every
    verdict the orchestrator merely reads: nothing in this module consults it,
    and a stored ``universe`` is what the skip decisions are made from. Pass a
    real :class:`~otto.config.scope.ProjectScopeConfig` only where a test drives
    a fleet surface for real — ``scoped_ids`` re-derives membership from it
    live, so a ``None`` there admits every host and would make an
    "empty fleet" assertion pass for the wrong reason.
    """
    return ProjectScope(
        project_name=name,
        declared=declared,
        config=config,
        applicable_labs=frozenset() if excluded else frozenset(loaded[:1]),
        universe=frozenset() if excluded else frozenset(universe),
        excluded=excluded,
        root=f"/projects/{name}",
        loaded_labs=tuple(loaded),
        lab_patterns=(f"{name}-lab",),
        host_patterns=tuple(host_patterns),
    )


def _one_line(text):
    """*text* with every run of whitespace collapsed — line wrapping must not hide a word."""
    return " ".join(text.split())


def _said(caplog):
    """Every captured WARNING as an OPERATOR receives it, collapsed to one line.

    Each record is read the way every sink shows it (:func:`shown`): its
    markup rendered only if it opted in with ``extra={"markup": True}``. The
    orchestrator's lines do not opt in, so a lab list in brackets is shown as
    written -- and a line that started opting in without escaping its lab list
    would lose it here, which makes every message assertion in this module a
    rendering guard too. The dedicated sink tests below cover all THREE sinks.
    """
    return _one_line(" ".join(shown(record) for record in caplog.records))


# ── install ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_walks_projects_in_topological_order(monkeypatch):
    # Kills: any walk that ignores the resolved order — a dependent installed
    # before the dependency it needs.
    #
    # A FLEET IS WIRED so the event list is exhaustive rather than merely
    # correctly ordered. `install` is the one lifecycle verb with NO
    # host-global step (the toolchain belongs to `install_tools`, the debug
    # sweep to the teardown verbs), and with `hosts=0` the two dispatch seams
    # return an empty mapping: a sweep grown here by mirroring a neighbouring
    # verb would emit nothing at all and this test would stay green while
    # every real lab placed toolchain artifacts on a plain install.
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2)  # base is the dependency
    assert (await project.install()).is_ok
    assert lab.events == [("base", "install"), ("app", "install")]


@pytest.mark.asyncio
async def test_install_is_fail_fast(monkeypatch):
    # Kills: a best-effort install — installing a dependent on top of a
    # dependency that is known not to be there.
    #
    # Exhaustive too, and for a second reason: the fleet is wired, so a
    # host-global step appended in the shape `cleanup` uses (run it anyway,
    # reduce the failures afterwards) is caught here even though it does not
    # touch the project walk. Fail-fast means the FAILURE STOPS EVERYTHING after
    # it, not just the next project.
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2, failing=("base", "install"))
    result = await project.install()
    assert lab.events == [("base", "install")]
    assert not result.is_ok
    assert result.status is Status.Failed  # the project's own status, not a generic one
    assert "base" in result.msg  # kills: dropping WHICH project failed
    assert "install refused" in result.msg


# ── uninstall ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uninstall_walks_reverse_and_is_best_effort(monkeypatch):
    # Kills BOTH: forward-order teardown (dependents must come down first) and
    # fail-fast teardown (a failed project must not strand the rest).
    lab = _wire(monkeypatch, projects=["base", "app"], failing=("app", "uninstall"))
    result = await project.uninstall()
    assert [e[0] for e in lab.events if e[1] == "uninstall"] == ["app", "base"]
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_uninstall_does_not_mutate_the_cached_project_order(monkeypatch):
    # The double hands back the SAME ordered list on every read. Kills: an
    # in-place ``projects.reverse()``, which would leave every later reader of
    # that list — including the next orchestrator verb — walking backwards.
    lab = _wire(monkeypatch, projects=["base", "app"])
    before = list(lab.ordered)
    await project.uninstall()
    assert lab.ordered == before


@pytest.mark.asyncio
async def test_uninstall_forwards_get_product_logs_to_every_project(monkeypatch):
    lab = _wire(monkeypatch, projects=["base", "app"])
    await project.uninstall(UninstallOptions(product_logs=False))
    assert [f[2] for f in lab.flags] == [{"product_logs": False}] * 2


# ── the single debug sweep ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_sweep_runs_once_per_host_after_all_uninstalls(monkeypatch):
    # THE spec §5 rule. Kills: per-project debug gathering (N sweeps, each
    # overwriting the last), and pre-teardown sweeping (Chris reversed that —
    # teardown activity is what debug logs exist to capture).
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2)
    assert (await project.uninstall()).is_ok
    debug_events = [e for e in lab.events if e[1] == "get_debug_logs"]
    assert len(debug_events) == 2  # once per host, not once per project on every host
    last_uninstall = max(i for i, e in enumerate(lab.events) if e[1] == "uninstall")
    first_debug = min(i for i, e in enumerate(lab.events) if e[1] == "get_debug_logs")
    assert first_debug > last_uninstall


@pytest.mark.asyncio
async def test_uninstall_get_debug_logs_false_skips_the_sweep(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    await project.uninstall(UninstallOptions(debug_logs=False))
    assert "get_debug_logs" not in _verbs(lab.events)


@pytest.mark.asyncio
async def test_uninstall_reports_a_failed_debug_sweep(monkeypatch):
    # Kills: firing the sweep and dropping its answer — a lost log set is the
    # frustration this whole surface exists to prevent.
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    lab.hosts[0].script("get_debug_logs", Result(Status.Failed, msg="transfer refused"))
    result = await project.uninstall()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "transfer refused" in result.msg


@pytest.mark.asyncio
async def test_debug_sweep_runs_even_when_a_project_failed_to_uninstall(monkeypatch):
    # Best-effort all the way down: the project that would not tear down is
    # exactly the one whose debug logs are wanted.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, failing=("app", "uninstall"))
    result = await project.uninstall()
    assert ("h0", "get_debug_logs") in lab.events
    assert not result.is_ok
    assert "app" in result.msg  # the FIRST failure is what is reported


# ── cleanup ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_walks_reverse_sweeps_untools_then_repairs_and_reaps_last(monkeypatch):
    # The toolchain is HOST-global (one per host, shared by every owner), so it
    # is removed once at the end rather than by any project. Kills: a per-project
    # toolchain removal, which would take a neighbour's tooling with it.
    #
    # THE LAST TWO ARE THE ORDER THAT MATTERS MOST, and this is the only place
    # it is asserted: a tunnel can BE the access path to a host, so reaping it
    # before the project walk, the sweep or the toolchain removal severs the
    # connection the rest of cleanup still needs. Resetting impairments first
    # only improves that path, which is why it comes second-to-last rather
    # than last. Kills: either step hoisted anywhere earlier, and the two
    # swapped.
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2)
    assert (await project.cleanup()).is_ok
    assert _verbs(lab.events) == [
        "cleanup",
        "cleanup",
        "get_debug_logs",
        "get_debug_logs",
        "remove_toolchain_tools",
        "remove_toolchain_tools",
        "repair_all",
        "remove_all_tunnels",
    ]
    assert [e[0] for e in lab.events if e[1] == "cleanup"] == ["app", "base"]


@pytest.mark.asyncio
async def test_cleanup_hands_both_lab_sweeps_the_active_context_lab(monkeypatch):
    # Kills: loading a second lab, or passing the context itself — both library
    # functions take the Lab, and the one that matters is the one the rest of
    # cleanup just acted on.
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    await project.cleanup()
    assert [(name, obj is lab.ctx.lab) for name, obj in lab.infra] == [
        ("repair_all", True),
        ("remove_all_tunnels", True),
    ]


@pytest.mark.asyncio
async def test_cleanup_removes_the_toolchain_even_when_a_project_failed(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1, failing=("app", "cleanup"))
    result = await project.cleanup()
    assert ("h0", "remove_toolchain_tools") in lab.events
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_cleanup_reports_a_failed_toolchain_removal(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    lab.hosts[0].script("remove_toolchain_tools", Result(Status.Failed, msg="read-only fs"))
    result = await project.cleanup()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "read-only fs" in result.msg


@pytest.mark.asyncio
async def test_cleanup_get_debug_logs_false_still_removes_the_toolchain(monkeypatch):
    # Kills: hanging the host-global removal off the debug-log flag — two
    # unrelated host-global steps that happen to sit next to each other.
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.cleanup(CleanupOptions(debug_logs=False))).is_ok
    assert _verbs(lab.events) == [
        "cleanup",
        "remove_toolchain_tools",
        "repair_all",
        "remove_all_tunnels",
    ]


@pytest.mark.asyncio
async def test_cleanup_forwards_get_product_logs(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"])
    await project.cleanup(CleanupOptions(product_logs=False))
    assert lab.flags == [("app", "cleanup", {"product_logs": False})]


# ── cleanup: the lab's own infrastructure ────────────────────────────────
#
# Impairments and tunnels belong to no project, exactly like the debug sweep and
# the toolchain above: nothing in a project's products or dev tools put them
# there. Both go through the library functions — `repair_all` keeps the
# refuse-a-foreign-qdisc rail, `remove_all_tunnels` keeps the post-kill verify
# — so nothing here reimplements `tc` or `kill`.


@pytest.mark.asyncio
async def test_cleanup_reports_a_link_the_impairment_reset_failed_on(monkeypatch):
    # repair_all NEVER raises: a live failure (host down, a clear that did not
    # take) lands in `failures`. Kills: reading the report's existence as
    # success, which is what dropping the failures bucket looks like.
    lab = _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        repair=RepairAllReport(failures=["core: host 'r1' unreachable"]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "core" in result.msg
    assert "unreachable" in result.msg
    # …and the reap still ran: cleanup is best-effort all the way down.
    assert ("lab", "remove_all_tunnels") in lab.events


def _out_of_scope(*, cleared: bool):
    from otto.link import RepairReport
    from otto.link.placement import FlowDirection, Placement

    near = [Placement("r1", "eth1", FlowDirection.A_TO_B)] if cleared else []
    far = [Placement("bb1350-qemu", "eth0", FlowDirection.B_TO_A)]
    return RepairAllReport(out_of_scope=[RepairReport("core", cleared=near, outside=far)])


@pytest.mark.asyncio
async def test_cleanup_declines_a_link_end_outside_the_lab_scope(monkeypatch):
    # #695: `--lab unix` loads test1 but not bb1350-qemu. otto cleared the end it
    # can see; the far end is not this scope's, so cleanup names it and succeeds.
    lab = _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("core")],
        repair=_out_of_scope(cleared=True),
    )
    result = await project.cleanup()
    assert result.is_ok, result.msg
    assert result.status is Status.Skipped
    assert "bb1350-qemu/eth0 is outside the loaded lab" in result.msg
    assert ("lab", "remove_all_tunnels") in lab.events


@pytest.mark.asyncio
async def test_cleanup_says_nothing_of_an_out_of_scope_link_it_cleared_nothing_on(monkeypatch):
    # Every `--lab unix` cleanup meets test1's wire to the busybox guest. With
    # nothing impaired on test1's end there is nothing to say: status reads clean.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("core")],
        repair=_out_of_scope(cleared=False),
    )
    result = await project.cleanup()
    assert result.status is Status.Success, result.msg


@pytest.mark.asyncio
async def test_cleanup_a_refused_impairment_does_not_read_as_success(monkeypatch):
    # THE REFUSAL CASE. A foreign qdisc otto did not create is DECLINED, not
    # failed — repair_all files it under `skipped` — and cleanup must neither
    # report Success (the impairment is still on the wire) nor fail (declining
    # a qdisc otto never applied is not a teardown failure).
    #
    # THE LINK IS DECLARED IMPAIRABLE in the lab, which is what makes this a
    # refusal worth reporting rather than the structural noise below.
    lab = _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("core")],
        repair=RepairAllReport(skipped=["core: r1/eth1 has a foreign qdisc otto did not create"]),
    )
    result = await project.cleanup()
    assert result.status is not Status.Success  # kills: swallowing the bucket
    assert result.status is Status.Skipped
    assert result.is_ok  # kills: aborting a best-effort teardown over a decline
    assert "core" in result.msg
    assert "foreign qdisc" in result.msg
    assert ("lab", "remove_all_tunnels") in lab.events


@pytest.mark.asyncio
async def test_cleanup_says_nothing_about_a_link_that_could_never_be_impaired(monkeypatch):
    # THE OTHER HALF OF THE SAME BUCKET, and the reason the decline means
    # anything at all. `repair_all` files a refusal per implicit hop edge —
    # every lab has at least one per host — so reporting the bucket whole would
    # make Success unreachable on every real lab, bury each message under N
    # unactionable lines, and leave a genuine foreign-qdisc refusal
    # indistinguishable from standing noise.
    #
    # The link declared here is the implicit shape (no named interface), which
    # `impairment_refusal` refuses without asking a device.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("h0--h1", impairable=False)],
        repair=RepairAllReport(skipped=["h0--h1: 'h0', 'h1' has no named interface"]),
    )
    result = await project.cleanup()
    assert result.status is Status.Success  # kills: reporting structural refusals
    assert result.msg == ""


@pytest.mark.asyncio
async def test_cleanup_a_failed_reap_outranks_an_earlier_decline(monkeypatch):
    """A DECLINE THAT ARRIVES FIRST MUST NOT SHADOW A FAILURE THAT ARRIVES AFTER IT.

    This is the production default, not an exotic case: a lab carrying one
    foreign qdisc declines at the impairment reset, which runs BEFORE the
    tunnel reap. "Return the first non-Success result in argument order" passes
    every other test in this file and fails here — it would hand back the
    decline, whose `is_ok` is True, so `otto run cleanup` would exit 0 with
    tunnel processes still running and `ensure_clean` would call the lab
    converged.
    """
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("core")],
        repair=RepairAllReport(skipped=["core: foreign qdisc"]),
        reap=RemovedReport([], {}, [], [("h0", 42)]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert result.status is Status.Failed
    assert "survived" in result.msg
    assert "h0/42" in result.msg


@pytest.mark.asyncio
async def test_cleanup_names_the_declines_alongside_an_impairment_failure(monkeypatch):
    # The failure is what to act on, but the decline is still the operator's
    # business: both buckets came back from one sweep, and dropping either
    # loses a link that needs a human. Kills: a failure message built from
    # `failures` alone.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        links=[_link("core"), _link("edge")],
        repair=RepairAllReport(
            failures=["edge: host 'r2' unreachable"],
            skipped=["core: foreign qdisc"],
        ),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "edge" in result.msg
    assert "declined" in result.msg
    assert "core" in result.msg


@pytest.mark.asyncio
async def test_cleanup_a_project_failure_outranks_a_refused_impairment(monkeypatch):
    # The decline is `is_ok`, so it must never displace a real failure in the
    # reported result. Kills: a reduction that returns the last non-Success.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        failing=("app", "cleanup"),
        repair=RepairAllReport(skipped=["core: no named interface"]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "cleanup refused" in result.msg


@pytest.mark.asyncio
async def test_cleanup_reports_tunnel_processes_that_survived_the_kill(monkeypatch):
    # THE WHOLE POINT of remove_all_tunnels' post-kill re-scan: a process still
    # present after the kill is a tunnel still carrying traffic. Kills: reading
    # `removed_ids` as the outcome and ignoring `survivors`.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        reap=RemovedReport(
            removed_ids=["tun-abc-5201"],
            killed={"h0": [42]},
            unreachable=[],
            survivors=[("h0", 42)],
        ),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "42" in result.msg


@pytest.mark.asyncio
async def test_cleanup_reports_a_host_the_tunnel_reap_could_not_reach(monkeypatch):
    # A tunnel outlives a partial reap on exactly those hosts, so an
    # unreachable host is a reap that did NOT finish — the CLI's `tunnel
    # remove --all` exits 1 on it for the same reason.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        reap=RemovedReport(removed_ids=[], killed={}, unreachable=["h9"], survivors=[]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "h9" in result.msg


@pytest.mark.asyncio
async def test_cleanup_no_reset_impairments_skips_only_that_step(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.cleanup(CleanupOptions(reset_impairments=False))).is_ok
    assert "repair_all" not in _verbs(lab.events)
    assert ("lab", "remove_all_tunnels") in lab.events


@pytest.mark.asyncio
async def test_cleanup_no_remove_tunnels_skips_only_that_step(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.cleanup(CleanupOptions(remove_tunnels=False))).is_ok
    assert "remove_all_tunnels" not in _verbs(lab.events)
    assert ("lab", "repair_all") in lab.events


@pytest.mark.asyncio
async def test_cleanup_a_dry_run_declines_both_steps_rather_than_reporting_them_done(monkeypatch):
    # THE REAL LIBRARY FUNCTIONS, not the doubles: both short-circuit above
    # device contact and hand back a plan-carrying report, and the point of
    # this test is that cleanup goes THROUGH them (never reimplementing `tc`
    # or `kill`) and turns "nothing was read, nothing was killed" into a
    # decline rather than into `Success`. An empty report is what a real sweep
    # of a clean lab produces too, so Success here would be the one wrong
    # answer indistinguishable from a right one.
    class _Unaskable:
        """A lab host that fails the test if anything reaches for the wire."""

        def __init__(self, host_id):
            self.id = host_id
            self.has_bash = True
            self.ip = "10.0.0.1"
            self.interfaces = {"eth1": "10.0.0.1"}
            self.impairer = "netem"
            self.current_user = "root"

        async def exec(self, *args, **kwargs):
            raise AssertionError("a dry run contacted a device")

        async def run(self, *args, **kwargs):
            raise AssertionError("a dry run contacted a device")

        async def is_running(self):
            raise AssertionError("a dry run probed a device")

    link = Link(a=LinkEndpoint("r1", "eth1"), b=LinkEndpoint("r2", "eth1"), id="core")
    fake_lab = SimpleNamespace(
        name="dry",
        hosts={"r1": _Unaskable("r1"), "r2": _Unaskable("r2")},
        static_links=lambda: [link],
    )
    from otto.invocation import RunPolicy, install_policy, reset_binding

    ctx = _FakeCtx([], lab=fake_lab)
    _wire_lab(monkeypatch, [], ctx)
    monkeypatch.setattr("otto.run_context.try_get_context", lambda: ctx)

    binding = install_policy(RunPolicy(dry_run=True))  # what is_dry_run() reads
    try:
        result = await project.cleanup()
    finally:
        reset_binding(binding)

    assert result.status is Status.NotRun  # not Success, and not a fabricated failure
    assert "dry run" in result.msg


@pytest.mark.asyncio
async def test_cleanup_dry_run_preview_of_the_impairment_reset_is_not_a_repair(monkeypatch):
    # repair_all under a dry run files its previews in `planned` and leaves
    # `repaired` empty. Kills: counting `planned` as work done.
    _wire(
        monkeypatch,
        projects=[],
        hosts=0,
        repair=RepairAllReport(planned=[RepairReport("core")], dry_run=True),
    )
    result = await project.cleanup()
    assert result.status is Status.NotRun
    assert "dry run" in result.msg


@pytest.mark.asyncio
async def test_cleanup_dry_run_preview_of_the_tunnel_reap_is_not_a_reap(monkeypatch):
    # ASKED WITH THE IMPAIRMENT RESET OFF, on purpose: with both steps
    # declining, the reset's own decline is the one that returns and this
    # step's could be dropped entirely without a test noticing. A reap that
    # scanned nothing has all four of its fields empty by CONSTRUCTION, which
    # is precisely what a real reap of a tunnel-free lab reports.
    _wire(
        monkeypatch,
        projects=[],
        hosts=0,
        reap=RemovedReport([], {}, [], [], plan=DryRunPlan(["scan 0 has_bash host(s)"], ["what"])),
    )
    result = await project.cleanup(CleanupOptions(reset_impairments=False))
    assert result.status is Status.NotRun
    assert "dry run" in result.msg


# ── get_logs ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_logs_walks_every_project_then_sweeps_debug_once(monkeypatch):
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2)
    assert (await project.get_logs()).is_ok
    assert _verbs(lab.events) == ["get_logs", "get_logs", "get_debug_logs", "get_debug_logs"]


@pytest.mark.asyncio
async def test_get_logs_is_best_effort_across_projects(monkeypatch):
    # A project whose haul failed must not cost the others theirs.
    lab = _wire(monkeypatch, projects=["base", "app"], failing=("base", "get_logs"))
    result = await project.get_logs()
    assert ("app", "get_logs") in lab.events
    assert not result.is_ok
    assert "base" in result.msg


@pytest.mark.asyncio
async def test_get_logs_debug_false_skips_the_sweep(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.get_logs(GetLogsOptions(debug_logs=False))).is_ok
    assert _verbs(lab.events) == ["get_logs"]


@pytest.mark.asyncio
async def test_get_logs_forwards_require_product_logs(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"])
    await project.get_logs(GetLogsOptions(require_product_logs=True))
    assert lab.flags == [("app", "get_logs", {"product_logs": True, "require_product_logs": True})]


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_with_product_false_is_refused(monkeypatch):
    # Kills: leaning on the per-project refusal, which never fires in a lab with
    # no projects — the requirement would be parsed and silently unenforceable.
    lab = _wire(monkeypatch, projects=[], hosts=1)
    result = await project.get_logs(GetLogsOptions(product_logs=False, require_product_logs=True))
    assert not result.is_ok
    assert "require_product_logs" in result.msg
    assert lab.events == []  # nothing ran, including the debug sweep


# ── install_tools ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_tools_walks_forward_and_is_fail_fast(monkeypatch):
    lab = _wire(monkeypatch, projects=["base", "app"], failing=("base", "install_tools"))
    result = await project.install_tools()
    assert _verbs(lab.events) == ["install_tools"]  # 'app' never started
    assert not result.is_ok
    assert "base" in result.msg


@pytest.mark.asyncio
async def test_install_tools_default_leaves_the_toolchain_alone(monkeypatch):
    # The asymmetric default is the point: toolchain artifacts are large and
    # rarely needed, so asking for them is deliberate.
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.install_tools()).is_ok
    assert _verbs(lab.events) == ["install_tools"]
    # toolchain=False is the default the operator did not change; the flag
    # rides along on the options instance, but the toolchain is host-global, so
    # otto's own body never acts on it.
    assert lab.flags == [("app", "install_tools", {"dev": True, "toolchain": False})]


@pytest.mark.asyncio
async def test_install_tools_toolchain_runs_the_host_global_sweep(monkeypatch):
    # THE host-global half: no project places toolchain tools (one toolchain per
    # host, shared by every owner), so if the orchestrator does not sweep,
    # ``install_tools(toolchain=True)`` is a silent end-to-end no-op.
    lab = _wire(monkeypatch, projects=["app"], hosts=2)
    assert (await project.install_tools(InstallToolsOptions(toolchain=True))).is_ok
    assert _verbs(lab.events) == [
        "install_tools",
        "install_toolchain_tools",
        "install_toolchain_tools",
    ]


@pytest.mark.asyncio
async def test_install_tools_dev_false_toolchain_true_still_sweeps(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.install_tools(InstallToolsOptions(dev=False, toolchain=True))).is_ok
    # --toolchain reaches the project's own options instance — a subclass with
    # toolchain work of its own has somewhere to hang it — while the
    # host-global sweep below stays the orchestrator's, performed once.
    assert lab.flags == [("app", "install_tools", {"dev": False, "toolchain": True})]
    assert ("h0", "install_toolchain_tools") in lab.events


@pytest.mark.asyncio
async def test_install_tools_reports_a_failed_toolchain_sweep(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    lab.hosts[0].script("install_toolchain_tools", Result(Status.Failed, msg="no space"))
    result = await project.install_tools(InstallToolsOptions(toolchain=True))
    assert not result.is_ok
    assert "h0" in result.msg
    assert "no space" in result.msg


@pytest.mark.asyncio
async def test_install_tools_failed_dev_walk_never_starts_the_toolchain(monkeypatch):
    # Fail-fast, mirroring the host verb: the toolchain is not placed on top of
    # tooling that is known to be missing.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, failing=("app", "install_tools"))
    result = await project.install_tools(InstallToolsOptions(toolchain=True))
    assert not result.is_ok
    assert "install_toolchain_tools" not in _verbs(lab.events)


# ── uninstall_tools ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uninstall_tools_walks_reverse_and_is_best_effort(monkeypatch):
    # Teardown order (a dependent's tools come off before its dependency's) and
    # best-effort (one tool that will not go must not strand the rest).
    lab = _wire(monkeypatch, projects=["base", "app"], failing=("app", "uninstall_tools"))
    result = await project.uninstall_tools()
    assert [e[0] for e in lab.events if e[1] == "uninstall_tools"] == ["app", "base"]
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_uninstall_tools_default_leaves_the_toolchain_alone(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.uninstall_tools()).is_ok
    assert _verbs(lab.events) == ["uninstall_tools"]
    assert lab.flags == [("app", "uninstall_tools", {"dev": True, "toolchain": False})]


@pytest.mark.asyncio
async def test_uninstall_tools_toolchain_removes_it_once_per_host(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=2)
    assert (await project.uninstall_tools(UninstallToolsOptions(toolchain=True))).is_ok
    assert _verbs(lab.events) == [
        "uninstall_tools",
        "remove_toolchain_tools",
        "remove_toolchain_tools",
    ]


@pytest.mark.asyncio
async def test_uninstall_tools_removes_the_toolchain_even_after_a_failed_dev_walk(monkeypatch):
    # Removal is best-effort end to end, unlike install-tools: a dev tool that
    # will not unload is no reason to leave the toolchain behind.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, failing=("app", "uninstall_tools"))
    result = await project.uninstall_tools(UninstallToolsOptions(toolchain=True))
    assert ("h0", "remove_toolchain_tools") in lab.events
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_uninstall_tools_reports_a_failed_toolchain_removal(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    lab.hosts[0].script("remove_toolchain_tools", Result(Status.Failed, msg="read-only fs"))
    result = await project.uninstall_tools(UninstallToolsOptions(toolchain=True))
    assert not result.is_ok
    assert "h0" in result.msg
    assert "read-only fs" in result.msg


# ── status ───────────────────────────────────────────────────────────────


def _state_actions(state):
    """A registered subclass that answers *state* — an opinion, held without products."""

    class _Scripted(ProjectActions):
        async def status(self, opts):
            del opts
            return state

    return _Scripted


def _wire_status(monkeypatch, projects):
    """Lab whose projects map name -> (scripted state | None, installed product flags).

    ``None`` means the project registers nothing and gets otto's default actions,
    so its state is computed from the products the fleet actually carries.
    """
    products = []
    for name, (state, flags) in projects.items():
        if state is not None:
            _register_actions(name, _state_actions(state))
        products += [_FakeItem(name, installed=flag) for flag in flags]
    ctx = _FakeCtx([_FakeHost("h0", [], products=products)])
    _wire_lab(monkeypatch, list(projects), ctx)


@pytest.mark.asyncio
async def test_status_uncounted_project_rule_and_empty_aggregate(monkeypatch):
    # A default-actions project owning no products must not drag the aggregate;
    # zero counted projects aggregate to UNINSTALLED (no vacuous INSTALLED).
    _wire_status(monkeypatch, {"docs": (None, [])})
    st = await project.status()
    assert st.overall is InstallState.UNINSTALLED
    assert st.projects == {}


@pytest.mark.asyncio
async def test_status_ignores_an_uncounted_project_beside_a_counted_one(monkeypatch):
    _wire_status(monkeypatch, {"docs": (None, []), "app": (None, [True, True])})
    st = await project.status()
    assert st.overall is InstallState.INSTALLED
    assert "docs" not in st.projects
    assert st.projects == {"app": InstallState.INSTALLED}


@pytest.mark.asyncio
async def test_status_counts_registered_subclass_even_without_products(monkeypatch):
    # "A registered subclass opted into having an opinion."
    _wire_status(monkeypatch, {"app": (InstallState.UNINSTALLED, [])})
    st = await project.status()
    assert st.projects == {"app": InstallState.UNINSTALLED}
    assert st.overall is InstallState.UNINSTALLED


@pytest.mark.asyncio
async def test_status_aggregate_is_partial_when_projects_disagree(monkeypatch):
    # Kills: an aggregate read off the first (or last) project, and one that only
    # sees PARTIAL when some project is itself PARTIAL.
    _wire_status(monkeypatch, {"base": (None, [True]), "app": (None, [False])})
    st = await project.status()
    assert st.overall is InstallState.PARTIAL
    assert st.projects == {"base": InstallState.INSTALLED, "app": InstallState.UNINSTALLED}


@pytest.mark.asyncio
async def test_status_aggregate_follows_a_single_partial_project(monkeypatch):
    _wire_status(monkeypatch, {"app": (None, [True, False])})
    assert (await project.status()).overall is InstallState.PARTIAL


@pytest.mark.asyncio
async def test_status_aggregate_is_uninstalled_when_every_counted_project_is(monkeypatch):
    _wire_status(monkeypatch, {"base": (None, [False]), "app": (None, [False])})
    assert (await project.status()).overall is InstallState.UNINSTALLED


@pytest.mark.asyncio
async def test_status_is_immune_to_impairments_and_tunnels(monkeypatch):
    """INSTALL STATE IS ABOUT PRODUCTS AND NOTHING ELSE (the project owner's rule).

    An impaired link and a live tunnel are lab infrastructure: `cleanup`
    removes them and `is_clean` counts them, but the tri-state answer stays a
    product count. A lab under test with 200ms of delay on a link is still
    INSTALLED, and reporting it PARTIAL would send `ensure_installed` into a
    teardown-and-reinstall over an impairment somebody deliberately applied.

    Asserted as "neither read seam was CALLED", not merely as the state: a
    status that consulted them and happened to ignore the answer would pass a
    state-only assertion and fail the day someone folded the answer in.
    """
    ctx = _FakeCtx([_FakeHost("h0", [], products=[_FakeItem("app", installed=True)])])
    _wire_lab(monkeypatch, ["app"], ctx)
    calls = _wire_infra(
        monkeypatch,
        [],
        states=[_state(whole=ImpairmentParams(delay_ms=200.0))],
        discovery=TunnelDiscovery([_live_tunnel()], []),
    )

    report = await project.status()

    assert report.overall is InstallState.INSTALLED
    assert report.projects == {"app": InstallState.INSTALLED}
    assert calls == []


# ── is_clean ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_clean_is_true_when_every_project_and_host_is_clean(monkeypatch):
    lab = _wire(monkeypatch, projects=["base", "app"], hosts=2)
    assert await project.is_clean() is True
    assert _verbs(lab.events) == ["toolchain_tools_absent"] * 2


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_project_has_leftovers(monkeypatch):
    _wire(monkeypatch, projects=["base", "app"], hosts=1, dirty=("app",))
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_host_toolchain_tool_remains(monkeypatch):
    # Kills: an is_clean that only asks the projects. Toolchain tools belong to no
    # project, so nobody but the orchestrator can see them left behind.
    lab = _wire(monkeypatch, projects=["app"], hosts=2)
    lab.hosts[1].toolchain_absent = False
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_asks_a_project_that_status_does_not_count(monkeypatch):
    # An uncounted project (no products, no registered actions) can still own DEV
    # TOOLS — ``owns_products`` cannot see them. Kills: reusing status()'s
    # counted-project filter here, which would report a lab with a probe still on
    # the board as clean.
    tool = _FakeItem("tools", installed=True)
    ctx = _FakeCtx([_FakeHost("h0", [], dev_tools=[tool])])
    _wire_lab(monkeypatch, ["tools"], ctx)
    assert await project.is_clean() is False


def _state(*, link_id="core", impairable=True, direction=FlowDirection.A_TO_B, **shape):
    """One link's read state, with *shape* applied to *direction* and the other clean.

    THE DIRECTION IS A PARAMETER because the two land on different hosts and
    are read separately: an impairment applied with ``--from`` the b end shows
    up in exactly one of these two cells, and a check that consulted only the
    first would report that lab clean.

    THE LINK ID IS ONE TOO, because a report has a ROW per link: two states
    built from the same id collapse into one row, and an assertion about which
    links got listed would then pass whatever the code did.
    """
    link = _link(link_id)
    if not impairable:
        return LinkState(link, {}, impairable=False, refusal="no named interface")
    # BUILT IN PLACEMENT ORDER (a->b, then b->a) whichever direction carries
    # the shape, exactly as `endpoint_placements` walks it. Keying the impaired
    # direction in first would put it at the head of the dict in both cases,
    # and a check that read only `by_direction`'s first entry would then pass
    # the b->a case it cannot actually see.
    return LinkState(
        link,
        {
            d: DirectionState(**shape) if d is direction else DirectionState()
            for d in (FlowDirection.A_TO_B, FlowDirection.B_TO_A)
        },
    )


def _unread_state(link_id="core", **flags):
    """One link whose impairment state was NOT read; *flags* says which way.

    ``read_link_states`` never raises per link -- it reports ``not_measured`` /
    ``unreachable`` / ``read_errors`` as fields on an otherwise empty state --
    so this is that shape, named once.
    """
    return LinkState(_link(link_id), {}, **flags)


def _live_tunnel():
    """One discovered tunnel — the shape ``tunnel list`` renders a row for."""
    tunnel = Tunnel(protocol="tcp", service_port=5201, path=(TunnelHop("h0"), TunnelHop("h1")))
    return DiscoveredTunnel(
        tunnel=tunnel, present=set(), missing=set(), age_seconds=7, uncertain=False
    )


@pytest.mark.asyncio
async def test_is_clean_is_true_with_links_read_clean_and_no_tunnel(monkeypatch):
    # Kills: treating the mere PRESENCE of a link state as dirt — every lab has
    # link states, so that reading would make ensure_clean run cleanup forever.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, states=[_state(), _state(impairable=False)])
    assert await project.is_clean() is True
    assert [name for name, _lab in lab.infra] == ["read_link_states", "discover_tunnels"]
    assert all(obj is lab.ctx.lab for _name, obj in lab.infra)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [FlowDirection.A_TO_B, FlowDirection.B_TO_A])
async def test_is_clean_is_false_while_a_link_carries_a_whole_link_impairment(
    monkeypatch, direction
):
    # THE agreement rule: cleanup resets impairments, so is_clean must see one.
    # Without this, `ensure_clean` no-ops on a lab `otto run cleanup` would
    # visibly change — the split-brain the whole project layer exists to stop.
    #
    # BOTH DIRECTIONS, because they are read off different hosts and land in
    # different cells: `impair --from <b>` fills only the second, so a check
    # that consulted `by_direction`'s first entry alone would call that lab
    # clean and every one-direction test would still pass.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        states=[_state(direction=direction, whole=ImpairmentParams(delay_ms=50.0))],
    )
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_link_carries_a_port_scoped_impairment(monkeypatch):
    # Kills: reading `whole` alone. A port-scoped impairment leaves `whole`
    # None and lives in `scoped`, and `repair_all` clears it just the same.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        states=[_state(scoped={Selector(5201, "tcp"): ImpairmentParams(loss_pct=2.0)})],
    )
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_ignores_a_foreign_qdisc_because_cleanup_refuses_it(monkeypatch):
    # AGREEMENT CUTS BOTH WAYS. A qdisc otto did not create is one `repair_all`
    # declines to touch (`_ensure_not_foreign`), deliberately — clearing it
    # would clobber tc config a human put on a shared host. Reporting the lab
    # unclean for it would send every `ensure_clean` into a cleanup that
    # provably cannot change the answer.
    _wire(monkeypatch, projects=["app"], hosts=1, states=[_state(foreign=True)])
    assert await project.is_clean() is True


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_tunnel_is_live(monkeypatch):
    # The second half of the agreement rule, and the one with no other witness:
    # a lab dirty ONLY in tunnels was clean to every surface before this.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        discovery=TunnelDiscovery([_live_tunnel()], []),
    )
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_raises_when_a_links_impairment_state_was_not_read(monkeypatch):
    # `read_link_states` never raises per link — it REPORTS the three ways a
    # read can fail — so this layer is the one that must refuse to answer.
    # Reading "we could not look" as clean is the fabrication the dry-run
    # contract is built around; reading it as dirty sends a converge into a
    # cleanup on a fact nobody established. Both are wrong, so neither is
    # chosen.
    link = Link(a=LinkEndpoint("h0", "eth1"), b=LinkEndpoint("h1", "eth1"), id="core")
    for state in (
        LinkState(link, {}, not_measured=True),
        LinkState(link, {}, unreachable=True),
        LinkState(link, {}, read_errors={FlowDirection.A_TO_B: "tc: not found"}),
    ):
        _wire(monkeypatch, projects=["app"], hosts=1, states=[state])
        with pytest.raises(RuntimeError, match="core"):
            await project.is_clean()


@pytest.mark.asyncio
async def test_is_clean_answers_dirty_on_a_tunnel_seen_during_a_partial_scan(monkeypatch):
    # A DEFINITIVE ANSWER BEATS AN INCOMPLETE SCAN. The unreachable host below
    # would raise on its own — nobody knows what it is running — but a tunnel
    # has already been SEEN, and no host that failed to answer can make the lab
    # clean again. Raising here would refuse a question otto has answered, and
    # strand `ensure_clean` on a lab it can see needs cleaning.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        discovery=TunnelDiscovery([_live_tunnel()], ["h9"]),
    )
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_raises_when_the_tunnel_scan_measured_nothing(monkeypatch):
    # Same rule for the reap's read side: a dry run's declined scan and a host
    # that never answered both leave "is a tunnel running?" unanswered, and an
    # empty tunnel list is exactly what a clean lab returns.
    for discovery in (
        TunnelDiscovery([], [], not_measured=True),
        TunnelDiscovery([], ["h9"]),
    ):
        _wire(monkeypatch, projects=["app"], hosts=1, discovery=discovery)
        with pytest.raises(RuntimeError):
            await project.is_clean()


@pytest.mark.asyncio
async def test_is_clean_surfaces_a_hosts_refusal_instead_of_answering(monkeypatch):
    # do_for_all_hosts captures exceptions AS VALUES. A dry run's refusal read
    # as "not clean" would send a converge into a cleanup nobody can run, and a
    # transport failure read the same way is a fact nobody established.
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    lab.hosts[0].script("toolchain_tools_absent", CommandNotRunError("test -e /d/gdb", "h0"))
    with pytest.raises(CommandNotRunError):
        await project.is_clean()


# ── is_uninstalled ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_uninstalled_follows_the_aggregate_through_all_three_states(monkeypatch):
    # PARTIAL IS THE ROW THAT MATTERS. A lab with half its products on is not
    # uninstalled, and a boolean that said otherwise would let
    # `ensure_uninstalled` skip the teardown over remnants still on the fleet --
    # the same conflation the tri-state exists to resolve, reached from the
    # other side.
    for state, expected in [
        (InstallState.UNINSTALLED, True),
        (InstallState.PARTIAL, False),
        (InstallState.INSTALLED, False),
    ]:
        _wire(monkeypatch, projects=["app"], state=state)
        assert await project.is_uninstalled() is expected, state


@pytest.mark.asyncio
async def test_a_partial_lab_is_neither_uninstalled_nor_clean(monkeypatch):
    # THE TWO AXES AT THEIR SHARED BOUNDARY, in one lab. Half the products on
    # is not "uninstalled" (ensure_uninstalled must still tear down) and it is
    # not "clean" either (cleanup still has products to remove). A layer that
    # derived either answer from the other gets exactly one of these wrong.
    _wire(monkeypatch, projects=["app"], hosts=1, state=InstallState.PARTIAL, dirty=("app",))
    assert await project.is_uninstalled() is False
    assert await project.is_clean() is False


def test_the_project_layer_exposes_no_lab_level_is_installed():
    # DELIBERATE ASYMMETRY, and this is the note to whoever comes to "fix" it:
    # False on a lab-level `is_installed()` would cover PARTIAL and UNINSTALLED
    # alike, and the callers that would ask are the converges, which have to
    # tell those two apart to choose between installing and tearing down first.
    # Hosts carry the symmetric pair because a host's answer is per product; a
    # lab's is an aggregate, and an aggregate is where the middle state lives.
    assert not hasattr(project, "is_installed")


# ── cleanliness ──────────────────────────────────────────────────────────


def _rows(report):
    """The report as ``{(kind, name): (state, detail)}`` -- one entry per row."""
    return {(item.kind, item.name): (item.state, item.detail) for item in report.items}


@pytest.mark.asyncio
async def test_cleanliness_reports_a_row_on_every_axis_of_a_clean_lab(monkeypatch):
    # ONE ROW PER THING CLEANUP ACTS ON, the clean ones included: a report that
    # listed only the dirt cannot be told apart from a report of a lab nobody
    # asked.
    _wire(monkeypatch, projects=["base", "app"], hosts=2, states=[_state()])

    report = await project.cleanliness()

    assert _rows(report) == {
        (CleanlinessKind.PROJECT, "base"): (Cleanliness.CLEAN, ""),
        (CleanlinessKind.PROJECT, "app"): (Cleanliness.CLEAN, ""),
        (CleanlinessKind.TOOLCHAIN, "h0"): (Cleanliness.CLEAN, ""),
        (CleanlinessKind.TOOLCHAIN, "h1"): (Cleanliness.CLEAN, ""),
        (CleanlinessKind.IMPAIRMENT, "core"): (Cleanliness.CLEAN, ""),
        (CleanlinessKind.TUNNEL, "lab"): (Cleanliness.CLEAN, "no otto tunnel is running"),
    }
    assert report.overall is Cleanliness.CLEAN


@pytest.mark.asyncio
async def test_cleanliness_rows_arrive_grouped_in_cleanups_own_order(monkeypatch):
    # THE RENDERER'S CONTRACT: `--full` prints a section heading on the row
    # where the kind changes, so a report that interleaved kinds would print
    # the same heading four times. The grouping is promised, not incidental.
    _wire(monkeypatch, projects=["app"], hosts=1, states=[_state()])

    kinds = [item.kind for item in (await project.cleanliness()).items]

    assert kinds == [
        CleanlinessKind.PROJECT,
        CleanlinessKind.TOOLCHAIN,
        CleanlinessKind.IMPAIRMENT,
        CleanlinessKind.TUNNEL,
    ]


@pytest.mark.asyncio
async def test_cleanliness_marks_what_it_could_not_read_instead_of_refusing(monkeypatch):
    # THE OPPOSITE DUTY TO is_clean, and the whole reason `--full` does not
    # just call it: every unreadable row below makes is_clean raise, and a
    # DISPLAY that raised would show nothing about the rows that did answer.
    lab = _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        states=[_state(), _unread_state("edge", unreachable=True)],
        discovery=TunnelDiscovery([], ["h9"]),
    )
    lab.hosts[0].script("toolchain_tools_absent", CommandNotRunError("test -e /d/gdb", "h0"))

    report = await project.cleanliness()
    rows = _rows(report)

    assert rows[(CleanlinessKind.TOOLCHAIN, "h0")][0] is Cleanliness.UNKNOWN
    assert rows[(CleanlinessKind.IMPAIRMENT, "edge")][0] is Cleanliness.UNKNOWN
    # ...and the link that DID answer keeps its answer, which is the half a
    # raise destroys.
    assert rows[(CleanlinessKind.IMPAIRMENT, "core")][0] is Cleanliness.CLEAN
    assert rows[(CleanlinessKind.TUNNEL, "lab")] == (
        Cleanliness.UNKNOWN,
        "the scan could not reach h9",
    )
    assert report.overall is Cleanliness.UNKNOWN
    # The same lab, asked for a DECISION, still refuses to make one up.
    with pytest.raises(CommandNotRunError):
        await project.is_clean()


@pytest.mark.asyncio
async def test_cleanliness_marks_a_project_whose_own_probe_failed(monkeypatch):
    # A project's is_clean walks products and dev tools on real hosts, so it can
    # raise like anything else that touches one. Kills a report that dies with
    # the first project it asked.
    class _Raising(ProjectActions):
        async def is_clean(self):
            raise CommandNotRunError("test -e /opt/acme", "h0")

    _register_actions("app", _Raising)
    _wire_lab(monkeypatch, ["app"], _FakeCtx([_FakeHost("h0", [])]))
    _wire_infra(monkeypatch, [])

    rows = _rows(await project.cleanliness())

    assert rows[(CleanlinessKind.PROJECT, "app")][0] is Cleanliness.UNKNOWN
    with pytest.raises(CommandNotRunError):
        await project.is_clean()


@pytest.mark.asyncio
async def test_cleanliness_names_the_impaired_directions(monkeypatch):
    # A row that only said "dirty" would leave the operator to run `otto link
    # list` to learn which way the delay points -- and `impair --from` places
    # it in exactly one direction, so the two cells are not interchangeable.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        states=[_state(direction=FlowDirection.B_TO_A, whole=ImpairmentParams(delay_ms=50.0))],
    )

    rows = _rows(await project.cleanliness())

    assert rows[(CleanlinessKind.IMPAIRMENT, "core")] == (Cleanliness.DIRTY, "b->a")


@pytest.mark.asyncio
async def test_cleanliness_leaves_out_links_that_could_never_be_impaired(monkeypatch):
    # Every implicit hop edge is one, and an N-host lab resolves at least N of
    # them, none with any state to read or to remove. N rows saying "clean"
    # about links no cleanup can act on would bury the handful that mean
    # something -- the reason `_live_refusals` drops them from cleanup's own
    # report.
    _wire(
        monkeypatch,
        projects=["app"],
        hosts=1,
        states=[_state(), _state(link_id="hop-h0-h1", impairable=False)],
    )

    listed = [
        item.name
        for item in (await project.cleanliness()).items
        if item.kind is CleanlinessKind.IMPAIRMENT
    ]

    assert listed == ["core"]


@pytest.mark.asyncio
async def test_cleanliness_shows_both_the_tunnel_it_found_and_the_host_it_missed(monkeypatch):
    # DIRTY AND UNKNOWN ARE NOT EXCLUSIVE. The aggregate is dirty -- an answer
    # in hand -- but the host nobody reached is still worth printing, because
    # it may be carrying more.
    found = _live_tunnel()
    _wire(monkeypatch, projects=["app"], hosts=1, discovery=TunnelDiscovery([found], ["h9"]))

    report = await project.cleanliness()
    rows = _rows(report)

    assert rows[(CleanlinessKind.TUNNEL, found.tunnel.id)][0] is Cleanliness.DIRTY
    assert rows[(CleanlinessKind.TUNNEL, "lab")][0] is Cleanliness.UNKNOWN
    assert report.overall is Cleanliness.DIRTY


@pytest.mark.asyncio
async def test_cleanliness_asks_every_project_even_past_a_dirty_one(monkeypatch):
    # "Which of them" is the whole question a report is asked, so nothing here
    # short-circuits -- where the boolean below deliberately does.
    _wire(monkeypatch, projects=["base", "app"], hosts=1, dirty=("base",))

    rows = _rows(await project.cleanliness())

    assert rows[(CleanlinessKind.PROJECT, "base")][0] is Cleanliness.DIRTY
    assert rows[(CleanlinessKind.PROJECT, "app")][0] is Cleanliness.CLEAN


@pytest.mark.asyncio
async def test_is_clean_stops_before_the_device_reads_once_a_project_is_dirty(monkeypatch):
    # THE CONVERGE'S CHEAP PATH, and the reason is_clean is not `cleanliness()`
    # reduced: a lab already known to need cleaning is not worth a link read
    # per link and a process scan per host, and `ensure_clean` takes this path
    # before every test case.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, dirty=("app",))

    assert await project.is_clean() is False
    assert lab.infra == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", [True, False])
async def test_is_clean_answers_dirty_with_one_link_impaired_and_another_unreadable(
    monkeypatch, hostile
):
    # THE ASYMMETRY THAT WAS: the tunnel side already answered dirty from a
    # partial scan, while the link side raised on the first unreadable link and
    # threw away an answer it already had. A definitive dirty answer outranks a
    # scan that fell short, on every axis.
    #
    # `hostile=True` puts the UNREADABLE link first -- the order that raises
    # without the fix. The other order passes either way, and is here only to
    # prove the fix is the rule rather than a lucky iteration order.
    impaired = _state(whole=ImpairmentParams(delay_ms=50.0))
    unreadable = _unread_state("edge", unreachable=True)
    states = [unreadable, impaired] if hostile else [impaired, unreadable]
    _wire(monkeypatch, projects=["app"], hosts=1, states=states)

    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_answers_dirty_with_one_host_tooled_and_another_unreachable(monkeypatch):
    # THE SAME RULE ON THE TOOLCHAIN AXIS, and it needs a guard of its own: the
    # rule lives in the shared `_verdict`, so re-inlining a raise-first loop
    # over this one axis regresses the behaviour while every other test here
    # stays green.
    #
    # THE HOSTILE ORDER IS THE POINT: the host that cannot answer is asked
    # FIRST, so a walk that raises the moment it meets one never reaches the
    # host that is provably still carrying a toolchain. An answer in hand is
    # not discarded for a probe that fell short.
    lab = _wire(monkeypatch, projects=["app"], hosts=2)
    lab.hosts[0].script("toolchain_tools_absent", CommandNotRunError("test -e /d/gdb", "h0"))
    lab.hosts[1].toolchain_absent = False

    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_stops_at_the_axis_that_could_not_answer(monkeypatch):
    # Kills: reducing the whole report. Reading the axes after one that cannot
    # answer is device work whose only purpose would be to strengthen a verdict
    # already unavailable.
    lab = _wire(monkeypatch, projects=["app"], hosts=1, states=[_unread_state(unreachable=True)])

    with pytest.raises(RuntimeError, match="core"):
        await project.is_clean()

    assert [name for name, _lab in lab.infra] == ["read_link_states"]


# ── ensure_* ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_installed_converges_from_each_state(monkeypatch):
    for state, expected in [
        (InstallState.INSTALLED, []),
        (InstallState.UNINSTALLED, ["install"]),
        (InstallState.PARTIAL, ["uninstall", "install"]),
    ]:
        lab = _wire(monkeypatch, projects=["app"], state=state)
        result = await project.ensure_installed()
        assert _verbs(lab.events) == expected, state
        assert result.is_ok


@pytest.mark.asyncio
async def test_ensure_installed_already_installed_is_a_skip_not_a_fresh_install(monkeypatch):
    _wire(monkeypatch, projects=["app"], state=InstallState.INSTALLED)
    result = await project.ensure_installed()
    assert result.status is Status.Skipped  # "nothing to converge", not "installed it"


@pytest.mark.asyncio
async def test_ensure_installed_recover_partial_false_skips_the_uninstall(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], state=InstallState.PARTIAL)
    await project.ensure_installed(InstallOptions(recover_partial=False))
    assert _verbs(lab.events) == ["install"]


@pytest.mark.asyncio
async def test_ensure_installed_stops_when_the_recovery_teardown_fails(monkeypatch):
    # Kills: installing on top of remnants that are KNOWN to be stranded —
    # which reproduces the PARTIAL state ensure_installed was called to fix.
    lab = _wire(
        monkeypatch,
        projects=["app"],
        state=InstallState.PARTIAL,
        failing=("app", "uninstall"),
    )
    result = await project.ensure_installed()
    assert _verbs(lab.events) == ["uninstall"]
    assert not result.is_ok
    assert "uninstall refused" in result.msg


@pytest.mark.asyncio
async def test_ensure_uninstalled_converges_from_each_state(monkeypatch):
    for state, expected in [
        (InstallState.UNINSTALLED, []),
        (InstallState.PARTIAL, ["uninstall"]),
        (InstallState.INSTALLED, ["uninstall"]),
    ]:
        lab = _wire(monkeypatch, projects=["app"], state=state)
        assert (await project.ensure_uninstalled()).is_ok
        assert _verbs(lab.events) == expected, state


@pytest.mark.asyncio
async def test_ensure_clean_cleans_only_a_dirty_lab(monkeypatch):
    lab = _wire(monkeypatch, projects=["app"], hosts=1)
    assert (await project.ensure_clean()).status is Status.Skipped
    assert "cleanup" not in _verbs(lab.events)

    lab = _wire(monkeypatch, projects=["app"], hosts=1, dirty=("app",))
    assert (await project.ensure_clean()).is_ok
    assert "cleanup" in _verbs(lab.events)


_DECLINING_VERBS = [
    ("uninstall", "uninstall", lambda on: project.uninstall(UninstallOptions(debug_logs=on))),
    ("get-logs", "get_logs", lambda on: project.get_logs(GetLogsOptions(debug_logs=on))),
    (
        "install-tools",
        "install_tools",
        lambda on: project.install_tools(InstallToolsOptions(toolchain=on)),
    ),
    (
        "uninstall-tools",
        "uninstall_tools",
        lambda on: project.uninstall_tools(UninstallToolsOptions(toolchain=on)),
    ),
    ("cleanup", "cleanup", lambda on: project.cleanup(CleanupOptions(debug_logs=on))),
]
"""Every lab verb that walks projects, and the unrelated flag that adds a host-global step."""


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False], ids=["flag-on", "flag-off"])
@pytest.mark.parametrize(
    ("name", "verb", "run"), _DECLINING_VERBS, ids=[v[0] for v in _DECLINING_VERBS]
)
async def test_a_project_s_decline_reaches_the_result_whatever_the_flag(
    monkeypatch, name, verb, run, on
):
    """One decline, one rendering: the host-global step a flag adds never folds it away.

    Also the default combiner's decline arm: the project's Skipped reaches the
    lab verb's result, stamped with the project, instead of becoming Success.
    """
    _wire(monkeypatch, projects=["app"], hosts=1, declining=("app", verb))

    result = await run(on)

    assert result.status is Status.Skipped, result.msg
    assert result.msg == f"{name} declined in project 'app': {verb} kept something"


@pytest.mark.asyncio
async def test_a_project_s_failure_outranks_another_s_decline_in_either_order(monkeypatch):
    for failing, declining in (
        (("a", "cleanup"), ("b", "cleanup")),
        (("b", "cleanup"), ("a", "cleanup")),
    ):
        _wire(monkeypatch, projects=["a", "b"], hosts=1, failing=failing, declining=declining)

        result = await project.cleanup()

        assert result.status is Status.Failed, result.msg
        assert "cleanup refused" in result.msg


@pytest.mark.asyncio
@pytest.mark.parametrize("left", [True, False])
async def test_ensure_clean_cleans_a_lab_dirty_only_in_docker_staging(monkeypatch, left):
    """Staging cleanup would remove is a leftover: ensure_clean must not call it clean.

    The project keeps otto's own actions, so its ``is_clean`` and ``cleanup``
    both reach the one staging survey, scripted here.
    """
    from otto.docker import compose as compose_mod
    from otto.docker import staging as staging_mod
    from otto.docker.compose import StagingKey, StagingSurvey

    removed = []

    async def _survey(host, project):
        del host, project
        return StagingSurvey([StagingKey("stack-a", True)] if left else [], [], [])

    async def _cleanup_project(host, key, *, stack):
        del stack
        removed.append((host.id, key))
        return Result(Status.Success, value="")  # a removed tree prints nothing

    monkeypatch.setattr(compose_mod, "staging_keys_on", lambda h, p: [StagingKey("stack-a", True)])
    monkeypatch.setattr(compose_mod, "staging_to_remove", _survey)
    monkeypatch.setattr(staging_mod, "cleanup_project", _cleanup_project)
    events = []
    ctx = _FakeCtx([_FakeHost("h0", events)])
    _wire_lab(monkeypatch, ["app"], ctx)
    _wire_infra(monkeypatch, events)

    result = await project.ensure_clean()

    if left:
        assert result.msg != "already clean"
        assert removed == [("h0", "stack-a")]
    else:
        assert result.status is Status.Skipped
        assert result.msg == "already clean"
        assert removed == []


@pytest.mark.asyncio
async def test_install_ensure_flag_delegates_to_the_converge(monkeypatch):
    # The CLI's ``install --ensure`` is this argument, not a second code path.
    lab = _wire(monkeypatch, projects=["app"], state=InstallState.PARTIAL)
    await project.install(InstallOptions(ensure=True))
    assert _verbs(lab.events) == ["uninstall", "install"]

    lab = _wire(monkeypatch, projects=["app"], state=InstallState.PARTIAL)
    await project.install(InstallOptions(ensure=True, recover_partial=False))
    assert _verbs(lab.events) == ["install"]

    # …and without it, install is the plain walk: no status is consulted, so a
    # lab that is already INSTALLED is installed again.
    lab = _wire(monkeypatch, projects=["app"], state=InstallState.INSTALLED)
    await project.install()
    assert _verbs(lab.events) == ["install"]


# ── project scoping: D3's abort, and D3's loud skip ──────────────────────

# THE TWO HALVES OF D3 ARE ASYMMETRIC ON PURPOSE (spec §10). The project DRIVING
# the run — bootstrap's first project root — aborts at project-layer entry when its
# own [project] declaration cannot work: every verb it could run would act on
# nothing, and a silent no-op is the failure this design rates worse than a
# crash. A DEPENDENCY no loaded lab applies to is skipped instead, loudly: one
# project's fleet declaration must never veto another project's run.
#
# EVERY FIXTURE BELOW GIVES THE DRIVING PROJECT AND A DEPENDENCY DIFFERENT
# VERDICTS, which is what makes a gate asking about the wrong project visible. In
# walk order the driving project comes LAST (a dependent follows what it depends
# on), so ``projects[0]`` of the walk order is a DEPENDENCY — the mutant these
# pairs exist to kill, in both directions: with the driving project excluded it
# must raise, and with only the dependency excluded it must not.

_PUBLIC_VERBS = [
    "install",
    "uninstall",
    "cleanup",
    "get_logs",
    "install_tools",
    "uninstall_tools",
    "status",
    "is_uninstalled",
    "cleanliness",
    "is_clean",
    "ensure_installed",
    "ensure_uninstalled",
    "ensure_clean",
]
"""Every verb the package re-exports — the whole project-layer entry surface."""


def test_the_verb_list_is_derived_membership_not_memory():
    """``_PUBLIC_VERBS`` must equal the package's actual orchestrator surface.

    The parametrized gate test below is only as complete as this list, and a
    hand-maintained list joins new verbs by memory — a future verb re-exported
    from the orchestrator would silently skip the D3 contract. Deriving the
    expected set from ``otto.project``'s own namespace makes forgetting
    impossible: exporting a 13th verb reds this test until it is enumerated
    (and thereby gated) here. ``dir()``, not ``vars()``: the package exports
    lazily, so its module dict holds none of the verbs until they resolve.
    """
    exported = {
        name
        for name in dir(project)
        if not name.startswith("_")
        and callable(obj := getattr(project, name))
        and getattr(obj, "__module__", "") == "otto.project.orchestrator"
    }
    assert sorted(_PUBLIC_VERBS) == sorted(exported)


def _scoped(monkeypatch, excluded, **kwargs):
    """A two-project lab (dependency ``base``, driving ``app``) with *excluded* named out."""
    return _wire(
        monkeypatch,
        projects=["base", "app"],
        scopes={
            "base": _scope("base", excluded=excluded == "base"),
            "app": _scope("app", excluded=excluded == "app"),
        },
        **kwargs,
    )


@pytest.mark.parametrize("verb", _PUBLIC_VERBS)
@pytest.mark.asyncio
async def test_every_public_verb_refuses_the_driving_projects_unusable_scope(monkeypatch, verb):
    """D3 at project-layer entry, on every verb — and BEFORE anything is contacted.

    Enumerated rather than sampled because "every public verb begins with the
    gate" is the contract: a thin wrapper (``is_uninstalled``, the three
    ``ensure_*``) inherits it from the verb it delegates to, and this is what
    proves the delegation actually reaches a gated one.

    The three recording lists are asserted EMPTY, not merely the raise: a gate
    that fired after the walk would still raise, having already installed
    products on hosts the declaration says are none of this project's business.
    """
    lab = _scoped(monkeypatch, excluded="app", hosts=2)

    with pytest.raises(ProjectScopeError) as excinfo:
        await getattr(project, verb)()

    assert "'app'" in _one_line(str(excinfo.value))
    assert lab.events == []
    assert lab.questions == []
    assert lab.infra == []


@pytest.mark.asyncio
async def test_the_gate_asks_about_the_driving_project_not_the_first_project_walked(monkeypatch):
    """The reading is ``ctx.projects[0]`` — the first OTTOPATH entry.

    Pinned at the call because the two lists coincide in the commonest lab (one
    project) and disagree in every interesting one: ``ctx.ordered_projects`` is a
    TOPOLOGICAL reorder, so its first element is a dependency. Asserting the
    scopes mapping too pins the other half — the verdicts come from the live
    context, not from a resolution the gate ran for itself.
    """
    asked = []
    monkeypatch.setattr(
        "otto.config.scope.require_current_scope",
        lambda scopes, name: asked.append((name, scopes)),
    )
    lab = _wire(monkeypatch, projects=["base", "app"])

    await project.install()

    assert asked == [("app", scopes_of(lab.ctx))]


@pytest.mark.asyncio
async def test_an_inapplicable_dependency_does_not_abort_the_driving_projects_run(monkeypatch):
    """D3's asymmetry, and the mirror that kills a gate pointed at the wrong project.

    ``base`` is excluded and ``app`` is healthy: a gate reading the walk
    order's first entry — or its last, or any dependency — aborts here, where
    the run must proceed with ``base`` simply left out.
    """
    lab = _scoped(monkeypatch, excluded="base")

    assert (await project.install()).is_ok
    assert lab.events == [("app", "install")]


@pytest.mark.asyncio
async def test_the_refusal_names_the_loaded_labs_and_the_projects_own_patterns(monkeypatch):
    """D3's message carries what was loaded AND what was declared (both, or neither helps).

    Rendered by ``require_current_scope`` itself; the orchestrator hands it the
    verdict and re-renders nothing, which is why this asserts the message
    reaches the caller intact rather than pinning a second copy of the wording.
    """
    _scoped(monkeypatch, excluded="app")

    with pytest.raises(ProjectScopeError) as excinfo:
        await project.install()

    message = _one_line(str(excinfo.value))
    assert "'app'" in message
    assert "bench, floor" in message  # what this run loaded
    assert "app-lab" in message  # what the project declared
    assert "base" not in message  # a healthy dependency is not a suspect


@pytest.mark.asyncio
async def test_a_lab_with_no_projects_has_no_current_scope_to_enforce(monkeypatch):
    """Kills an ``IndexError`` on ``ctx.projects[0]`` — a project-less run is legal."""
    _wire(monkeypatch, projects=[], hosts=1)

    assert (await project.install()).is_ok


@pytest.mark.parametrize(
    "verb", ["install", "uninstall", "cleanup", "get_logs", "install_tools", "uninstall_tools"]
)
@pytest.mark.asyncio
async def test_every_walking_verb_leaves_the_inapplicable_project_alone(monkeypatch, verb):
    """The excluded project's actions record ZERO dispatches, on every walk there is.

    Both halves asserted with ``==``: the skipped project's contact list is
    EMPTY, and the applicable project's is EXACTLY its one dispatch. A skip that
    merely dropped the RESULT would leave the first list full, and a skip that
    dropped both projects would leave the second empty.
    """
    lab = _scoped(monkeypatch, excluded="base")

    await getattr(project, verb)()

    assert [e for e in lab.events if e[0] == "base"] == []
    assert [e for e in lab.events if e[0] == "app"] == [("app", verb)]


@pytest.mark.asyncio
async def test_the_skip_is_announced_once_naming_the_project_the_labs_and_the_patterns(
    monkeypatch, caplog
):
    """A skipped project is LOUD — one warning per skip, carrying the whole diagnosis.

    Name, loaded labs and declared patterns together, because each alone reads
    as the other's fault, and whitespace-normalized so a wrapped log line
    cannot hide a word the reader needs. ONE line, not one per host or per
    project-in-the-lab: a message repeated is a message skipped.
    """
    _scoped(monkeypatch, excluded="base")

    with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
        await project.install()

    said = [_one_line(r.message) for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1, said
    assert "'base'" in said[0]
    assert "bench, floor" in said[0]
    assert "base-lab" in said[0]
    assert "skipping" in said[0]


@pytest.mark.asyncio
async def test_status_never_asks_an_inapplicable_project_and_leaves_it_out_of_the_fold(monkeypatch):
    """The tri-state fold counts applicable projects only (as it already skips uncounted ones).

    The two states DISAGREE on purpose: folding ``base`` in reads PARTIAL and
    would send ``ensure_installed`` into a teardown-and-reinstall over a project
    this run does not apply to. The question list is asserted too — a status
    that asks an excluded project has contacted hosts outside the fleet of
    interest, whatever it then does with the answer.
    """
    lab = _scoped(
        monkeypatch,
        excluded="base",
        state={"base": InstallState.UNINSTALLED, "app": InstallState.INSTALLED},
    )

    report = await project.status()

    assert lab.questions == [("app", "status")]
    assert report.projects == {"app": InstallState.INSTALLED}
    assert report.overall is InstallState.INSTALLED


@pytest.mark.asyncio
async def test_status_reports_each_projects_fleet_of_interest_and_why_one_was_left_out(monkeypatch):
    """``status`` says WHY a project has no state, rather than dropping it silently.

    An absent row is what an uncounted (product-less) project gets, and reusing
    that for an excluded one would leave an operator with a lab whose project
    simply vanished from the report that is supposed to explain it.
    """
    _scoped(monkeypatch, excluded="base", state=InstallState.INSTALLED)

    scoping = (await project.status()).scoping

    assert scoping["base"].applicable is False
    assert scoping["base"].loaded_labs == ("bench", "floor")
    assert scoping["base"].applicable_labs == ()
    assert scoping["app"].applicable is True
    assert scoping["app"].applicable_labs == ("bench",)
    assert scoping["app"].universe == ("h0",)


@pytest.mark.asyncio
async def test_an_undeclared_project_is_walked_like_every_other(monkeypatch):
    """Undeclared is the whole-lab FALLBACK, never an exclusion (§6).

    Kills a skip keyed on ``not declared`` — or on an empty universe — which
    would silently drop every product-less project and every pre-``[project]``
    project from the walks they have always been part of.
    """
    lab = _wire(
        monkeypatch,
        projects=["base", "app"],
        scopes={"base": _scope("base", declared=False), "app": _scope("app")},
    )

    assert (await project.install()).is_ok
    assert lab.events == [("base", "install"), ("app", "install")]


@pytest.mark.asyncio
async def test_is_clean_does_not_probe_an_inapplicable_project(monkeypatch):
    """The cheap converge path skips it too — its leftovers are on nobody's fleet.

    ``base`` is scripted DIRTY, so a probe that reached it would answer False:
    the assertion cannot pass by accident. Skipping matters beyond noise here —
    an excluded project's own fleet walk is refused by ``scoped_ids``, so probing
    it raises rather than answering, and ``ensure_clean`` would die on a
    dependency that has nothing to do with this run.
    """
    lab = _scoped(monkeypatch, excluded="base", dirty=("base",))

    assert await project.is_clean() is True
    assert lab.questions == [("app", "is_clean")]


@pytest.mark.asyncio
async def test_cleanliness_has_no_row_for_an_inapplicable_project(monkeypatch):
    """The display half of the same skip — no row, rather than a row nobody could read."""
    lab = _scoped(monkeypatch, excluded="base")

    report = await project.cleanliness()

    assert [i.name for i in report.items if i.kind is CleanlinessKind.PROJECT] == ["app"]
    assert lab.questions == [("app", "is_clean")]


# ── the OTHER unusable dependency: applicable labs, no matching host ─────────

# D3'S ASYMMETRY GOVERNS BOTH WAYS A DECLARATION CAN COME OUT UNUSABLE, not
# just the excluded one. A dependency whose lab_patterns DO match a loaded lab
# while its host_patterns match no host there has an empty fleet exactly as an
# excluded one does — its own fleet walk is refused by ``require_nonempty_fleet``
# — so admitting it into the walk raises out of the DEPENDENCY's walk and takes
# down the driving project's run: the veto D3 exists to prevent. The current
# project in that shape still aborts up front (``require_current_scope`` reads the
# same two conditions); only the dependency half is a skip.


def _starved(monkeypatch, **kwargs):
    """A two-project lab whose dependency ``base`` applies to a lab but targets no host in it."""
    return _wire(
        monkeypatch,
        projects=["base", "app"],
        scopes={
            "base": _scope("base", universe=(), host_patterns=("sensor-.*",)),
            "app": _scope("app"),
        },
        **kwargs,
    )


@pytest.mark.parametrize(
    "verb", ["install", "uninstall", "cleanup", "get_logs", "install_tools", "uninstall_tools"]
)
@pytest.mark.asyncio
async def test_every_walking_verb_skips_a_dependency_whose_host_patterns_match_nothing(
    monkeypatch, verb
):
    """The second unusable shape is SKIPPED, not raised through.

    Both halves with ``==``: the starved dependency's contact list is EMPTY and
    the healthy project's is EXACTLY its one dispatch. A skip keyed on
    ``excluded`` alone leaves ``base`` in the walk, where its owner-bound fleet
    walk raises ``ProjectScopeError`` — so this test fails LOUD (an error, not
    a wrong list) on that mutant.
    """
    lab = _starved(monkeypatch)

    result = await getattr(project, verb)()

    assert result.is_ok
    assert [e for e in lab.events if e[0] == "base"] == []
    assert [e for e in lab.events if e[0] == "app"] == [("app", verb)]


@pytest.mark.asyncio
async def test_the_starved_skip_names_the_host_patterns_not_the_labs(monkeypatch, caplog):
    """One warning, and it blames the axis that actually failed.

    This project's labs DO apply, so the excluded message ("not applicable to the
    loaded lab(s)") would send the reader to change ``-l`` or ``lab_patterns``
    — both already right — past the host_patterns that are the cause. Two
    conditions, two texts, each reachable only under its own.
    """
    _starved(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
        await project.install()

    said = [_one_line(r.message) for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1, said
    assert "'base'" in said[0]
    assert "sensor-.*" in said[0]  # the patterns that matched nothing
    assert "skipping" in said[0]
    assert "not applicable to the loaded lab(s)" not in said[0]  # the OTHER condition's text


@pytest.mark.asyncio
async def test_status_reports_the_starved_dependency_instead_of_raising(monkeypatch):
    """``status`` is a READING verb: it must report the verdict, never crash on it.

    The two states DISAGREE so the fold is measurable — counting ``base`` in
    reads PARTIAL. The question list is asserted for the reason the excluded
    twin's is: asking a project whose fleet is empty by declaration is a contact
    outside the fleet of interest, whatever is then done with the answer.
    """
    lab = _starved(
        monkeypatch,
        state={"base": InstallState.UNINSTALLED, "app": InstallState.INSTALLED},
    )

    report = await project.status()

    assert lab.questions == [("app", "status")]
    assert report.projects == {"app": InstallState.INSTALLED}
    assert report.overall is InstallState.INSTALLED


@pytest.mark.asyncio
async def test_status_still_carries_a_scoping_row_for_the_starved_dependency(monkeypatch):
    """Left out of the walk, never out of the report — with the row saying WHICH way.

    ``applicable`` stays True (its labs really do apply) while ``usable`` is
    False, and that pair is what lets the renderer print the host-patterns text
    here and the labs text for an excluded project.
    """
    _starved(monkeypatch, state=InstallState.INSTALLED)

    scoping = (await project.status()).scoping

    assert scoping["base"].applicable is True
    assert scoping["base"].usable is False
    assert scoping["base"].applicable_labs == ("bench",)
    assert scoping["base"].universe == ()
    assert scoping["base"].host_patterns == ("sensor-.*",)
    assert scoping["app"].usable is True


@pytest.mark.asyncio
async def test_cleanup_runs_every_host_global_step_when_a_dependency_is_starved(monkeypatch):
    """Best-effort teardown is not abortable by one project's declaration (cleanup's contract).

    The exact regression: ``base`` raising mid-walk stranded the debug sweep,
    the toolchain removal, the impairment reset and the tunnel reap — the last
    of which cleanup's own docstring calls "the one that matters", and which
    leaves processes running on a lab an operator believes is torn down.
    """
    lab = _starved(monkeypatch, hosts=2)

    assert (await project.cleanup()).is_ok
    assert _verbs(lab.events) == [
        "cleanup",
        "get_debug_logs",
        "get_debug_logs",
        "remove_toolchain_tools",
        "remove_toolchain_tools",
        "repair_all",
        "remove_all_tunnels",
    ]
    assert [e[0] for e in lab.events if e[1] == "cleanup"] == ["app"]


@pytest.mark.asyncio
async def test_a_starved_driving_project_still_aborts_the_whole_run(monkeypatch):
    """The current-project half is unchanged: abort, not skip — and nothing contacted.

    The mirror of the pair above, and the mutant it kills is a fix applied at
    the wrong end: ``require_current_scope`` relaxed to skip-like tolerance
    would let a project whose every walk is empty run silently to a green exit.
    """
    lab = _wire(
        monkeypatch,
        projects=["base", "app"],
        hosts=2,
        scopes={
            "base": _scope("base"),
            "app": _scope("app", universe=(), host_patterns=("sensor-.*",)),
        },
    )

    with pytest.raises(ProjectScopeError) as excinfo:
        await project.install()

    message = _one_line(str(excinfo.value))
    assert "'app'" in message
    assert "sensor-.*" in message
    assert lab.events == []
    assert lab.questions == []


# ── the switch axis: -I / -E over the lab verdict (spec §4) ──────────────────
#
# THE ORCHESTRATOR ASKS ``otto.config.scope.active`` AND NOTHING ELSE. The
# resolution order — exclude switch, include switch, lab verdict, default-on —
# lives in that one predicate, so every test here drives a WALK and asserts on
# what the walk did. Re-spelling the order at this layer would create a second
# authority, which is the drift a shared predicate exists to prevent.
#
# ``current=`` MATTERS IN THIS SECTION. ``_lab()`` runs D3's current-project gate
# (``otto.config.scope.require_current_scope``) before ``_applicable`` sees
# anything, and that gate reads the lab verdict ALONE — no switch overrides it,
# by D3's asymmetry. A case that hung an unusable scope on the default driving
# project would abort there and never reach the code under test: green for a
# reason that has nothing to do with switches. So every scoped case below names
# a driving project that is not the scoped one.
#
# NAMES ARE DELIBERATELY NOT NORMALIZATION-INVARIANT where the spelling is what
# is under test. `project1`/`project2` normalize to themselves, so a bed built only
# from them cannot tell `active(project.name, ctx)` from
# `active(normalize_name(project.name), ctx)` -- and the spec names that exact
# confusion as a FAIL-OPEN hazard, because `scopes_of(ctx)` is keyed by the raw
# declared name and a missed verdict resolves ACTIVE. `_UNDERSCORED` is a project
# whose declared name and normalized name differ, so every cell routed through
# it discriminates the two spellings. Its switch is typed in the NORMALIZED
# form, which is what a user types and what `_switched` compares on.
_UNDERSCORED = "Project_Two"
"""A declared project name that is NOT its own normalization (-> ``project-two``)."""

_UNDERSCORED_NORM = "project-two"
"""What :func:`~otto.models.dependencies.normalize_name` makes of it -- spelled
out rather than computed, so a broken normalizer cannot agree with itself."""

_PROVIDER = "Project_One"
"""The dropped PROVIDER's declared name (-> ``project-one``). A dependent declares
its dependency in the normalized form, which is the shape `dropped` is keyed and
looked up in."""

_PROVIDER_NORM = "project-one"


def _dep(name, *, required, status="satisfied"):
    """One resolved dependency — the REAL frozen dataclass, never a stub.

    ``normalized`` is computed by the same
    :func:`~otto.models.dependencies.normalize_name` ``_applicable`` matches
    with, rather than written out by hand: a hand-spelled normalization is a
    fixture that can agree with a wrong implementation.
    """
    return ResolvedDependency(
        name=name,
        normalized=normalize_name(name),
        constraint="",
        required=required,
        status=status,
        provider_version=None,
    )


class TestSwitchAxis:
    """``-E`` drops a project the labs admitted; ``-I`` keeps one they did not."""

    @pytest.mark.asyncio
    async def test_excluded_project_is_skipped_with_the_switch_message(self, monkeypatch, caplog):
        """The line names the SWITCH, not the labs — because nothing else will.

        A lab-inferred skip is explained twice over (this warning, and the
        ``scoping`` rows ``status`` prints), but no row anywhere records that a
        switch was typed. An operator handed the D3 wording here goes looking
        for a settings file that is perfectly correct.
        """
        bed = _wire(
            monkeypatch,
            projects=["project1", _UNDERSCORED],
            exclude_projects=(_UNDERSCORED_NORM,),
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == ["project1"]
        # The project is named as DECLARED and the switch is echoed NORMALIZED --
        # the two differ here, so a message built from the wrong one is visible.
        assert (
            f"project '{_UNDERSCORED}' switched off for this run "
            f"(--exclude-projects {_UNDERSCORED_NORM}) — skipping it for install"
        ) in _said(caplog)

    @pytest.mark.asyncio
    async def test_without_the_switch_the_walk_is_unchanged(self, monkeypatch, caplog):
        """The discriminator for the cell above, and for the whole section.

        Without it, an ``_applicable`` that dropped ``project2`` for any reason at
        all — or one that dropped the last project of every walk — would satisfy
        the switch test and this bed would never notice.
        """
        bed = _wire(monkeypatch, projects=["project1", "project2"])

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == ["project1", "project2"]
        assert "switched off" not in _said(caplog)

    @pytest.mark.parametrize(
        ("include", "walked"),
        [((), ["project1"]), ((_UNDERSCORED_NORM,), ["project1", _UNDERSCORED])],
        ids=["without -I", "with -I"],
    )
    @pytest.mark.asyncio
    async def test_include_overrides_an_excluded_scope(self, monkeypatch, include, walked):
        """``-I`` beats the lab verdict — and the no-switch row proves it had to.

        The project's verdict is UNUSABLE (declared, and no loaded lab applies),
        which is precisely the shape D3 skips on. The parametrization is what
        makes this an override rather than a coincidence: same scope, same
        walk, and the only thing that changed is the switch.

        BOTH ROWS ALSO DISCRIMINATE THE NAME SPELLING, on opposite sides. The
        no-switch row reaches ``scopes_of(ctx).get(project.name)``, keyed by the
        DECLARED name, so a lookup that normalized first would miss the verdict
        and walk an inactive project (the spec's fail-open hazard). The ``-I`` row
        types the NORMALIZED name, which is what a user types, so a switch
        comparison that skipped normalization would not match it.
        """
        bed = _wire(
            monkeypatch,
            projects=["project1", _UNDERSCORED],
            current="project1",  # the D3 current-project gate is switch-blind; see the section note
            scopes={_UNDERSCORED: _scope(_UNDERSCORED, excluded=True)},
            include_projects=include,
        )

        assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == walked


class TestDependencyInteraction:
    """What a KEPT project's dependencies mean once a provider has been dropped.

    Two independent bits decide the outcome: whether the dependency is
    REQUIRED, and whether the provider went away by an explicit ``-E`` or by
    the labs' own verdict. The switch is an operator signing off on exactly
    this ("I installed that one by hand"), so it warns and the walk proceeds.
    The lab verdict is a contradiction nobody signed off on — the labs say the
    provider cannot be here while the dependent says it must be — so on a
    BUILD-UP it raises, before any device is contacted. Which verbs build, and
    what the other ones do with the same contradiction, is
    ``TestTheRefusalIsBuildUpOnly``'s subject.
    """

    @pytest.mark.asyncio
    async def test_required_provider_switched_off_warns_and_proceeds(self, monkeypatch, caplog):
        """The provider is named as DECLARED; the switch is echoed NORMALIZED.

        Those are two different strings here, which is what makes the
        assertion able to fail: the dependent declares its dependency in the
        normalized spelling (as a ``[dependencies]`` entry always is) while the
        provider project is named ``Project_One``, so a ``dropped`` map keyed by the
        raw name never matches the lookup and the whole warning disappears.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            exclude_projects=(_PROVIDER_NORM,),
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert (
            f"project 'project4' requires '{_PROVIDER}', which was switched off "
            f"(--exclude-projects {_PROVIDER_NORM}) — proceeding as though "
            f"{_PROVIDER} is handled externally"
        ) in _said(caplog)
        assert [e[0] for e in bed.events if e[1] == "install"] == ["project4"]

    @pytest.mark.asyncio
    async def test_required_provider_lab_inactive_errors_naming_both_fixes(self, monkeypatch):
        """A refusal, and it offers BOTH ways out rather than picking one.

        "Load a lab it applies to" and "declare it handled externally" are
        genuinely different intents and otto cannot tell which one the operator
        has. Naming only the first would send someone who really did install
        the dependency by hand off to reconfigure their labs.

        ``bed.events`` empty is the load-bearing half: a refusal raised after
        the walk would still raise, having already installed a dependent on top
        of a dependency the labs say is not here.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
        )

        with pytest.raises(InactiveRequiredDependencyError) as excinfo:
            await project.install()

        text = _one_line(str(excinfo.value))
        assert f"project 'project4' requires '{_PROVIDER}'" in text
        assert "not applicable to the loaded lab(s) [unix_alt]" in text
        # `=`, not a space: the hint is meant to be PASTED, and rich folds
        # between words -- see `test_the_refusals_switch_hint_never_folds`.
        assert f"--exclude-projects={_PROVIDER_NORM}" in text
        assert not [e for e in bed.events if e[1] == "install"]

    @pytest.mark.asyncio
    async def test_an_optional_dependency_never_raises_on_either_reason(self, monkeypatch, caplog):
        """The bit that decides raise-vs-warn is ``required``, not the reason.

        Same lab-inactive provider as the cell above, same walk — only the
        dependency's ``required`` flag differs, and that alone is the
        difference between a refusal and a note.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=False)]},
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == ["project4"]
        assert (
            f"project 'project4': optional dependency '{_PROVIDER}' not satisfied for this run "
            f"— {_PROVIDER} is inactive (not applicable to the loaded lab(s) [unix_alt])"
        ) in _said(caplog)

    @pytest.mark.asyncio
    async def test_optional_provider_switched_off_appends_the_switch_reason(
        self, monkeypatch, caplog
    ):
        """The other reason arm, and it must not borrow the lab wording.

        The pair with the cell above is the assertion: "you typed ``-E``" and
        "no loaded lab applies" have different fixes, and one text covering
        both sends half its readers to edit the wrong thing.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            exclude_projects=(_PROVIDER_NORM,),
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=False)]},
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == ["project4"]
        assert (
            f"project 'project4': optional dependency '{_PROVIDER}' not satisfied for this run "
            f"— {_PROVIDER} is inactive (switched off via --exclude-projects {_PROVIDER_NORM})"
        ) in _said(caplog)

    @pytest.mark.asyncio
    async def test_a_dependency_on_a_kept_project_says_nothing(self, monkeypatch, caplog):
        """The anti-vacuity control for the whole class.

        Every cell above asserts a message about a DROPPED provider. Without
        this one, a dependency pass that warned about every declared dependency
        would satisfy all four of them.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == [_PROVIDER, "project4"]
        assert caplog.records == []


class TestTheRefusalIsBuildUpOnly:
    """Which verbs may refuse over a contradictory required dependency: only the two
    that BUILD.

    THE SHAPE IS ONE LAB, DRIVEN SIX WAYS. ``Project_One`` is dropped by the labs;
    ``project4`` is undeclared (whole-lab fallback, so it stays active) and
    requires it. That is a contradiction on every verb, but it is only
    ACTIONABLE on the two that build ``project4`` on top of it. Everything else
    either takes ``project4`` down or reads it, and for those the refusal is worse
    than the contradiction:

    * ``uninstall``/``cleanup`` are best-effort teardowns whose whole contract
      is that every step runs. ``_applicable`` is the first line of the first
      walk, so a raise there tears nothing down at all — no debug sweep, no
      toolchain removal, no impairment reset, no tunnel reap: exactly the
      remnants ``cleanup`` exists to remove.
    * ``get_logs``/``cleanliness``/``is_clean`` only read, and a report that
      dies whole is strictly worse than one carrying a row.

    The operator still learns about it — the survivable arm logs the same two
    facts and says which verb WOULD refuse — so nothing is swallowed.
    """

    @staticmethod
    def _contradiction(monkeypatch, **kwargs):
        """``project4`` (active, undeclared) requiring ``Project_One`` (dropped by the labs)."""
        return _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
            **kwargs,
        )

    @pytest.mark.parametrize("verb", ["install", "install_tools"])
    @pytest.mark.asyncio
    async def test_a_build_up_verb_refuses(self, monkeypatch, verb):
        """Both builders, not just ``install`` — ``install_tools`` places dev tools too."""
        bed = self._contradiction(monkeypatch)

        with pytest.raises(InactiveRequiredDependencyError):
            await getattr(project, verb)()

        assert bed.events == []

    @pytest.mark.parametrize(
        ("verb", "walk"),
        [("uninstall", "uninstall"), ("cleanup", "cleanup"), ("get_logs", "get-logs")],
    )
    @pytest.mark.asyncio
    async def test_a_teardown_or_reading_verb_completes(self, monkeypatch, verb, walk, caplog):
        """It runs, it succeeds, and it still SAYS what it found.

        The message is asserted here rather than in a class of its own because
        "did not raise" alone would be satisfied by a build that silently
        dropped the whole dependency pass on these verbs.
        """
        bed = self._contradiction(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            result = await getattr(project, verb)()

        assert result.is_ok
        assert [e[0] for e in bed.events if e[1] == verb] == ["project4"]
        said = _said(caplog)
        assert f"project 'project4' requires '{_PROVIDER}'" in said
        assert "not applicable to the loaded lab(s) [unix_alt]" in said
        assert f"continuing, because {walk} does not build project4 on top of it" in said
        assert f"--exclude-projects={_PROVIDER_NORM}" in said

    @pytest.mark.asyncio
    async def test_cleanup_still_runs_every_host_global_step(self, monkeypatch):
        """The regression this arm exists to prevent, asserted as the WHOLE step list.

        A raise out of ``_applicable`` strands all four host-global steps, and
        the tunnel reap — ``cleanup``'s own docstring calls it "the one that
        matters" — leaves processes running on a lab an operator believes is
        torn down. Asserted with ``==`` so a step quietly dropped later fails
        here too.
        """
        bed = self._contradiction(monkeypatch, hosts=2)

        assert (await project.cleanup()).is_ok

        assert _verbs(bed.events) == [
            "cleanup",
            "get_debug_logs",
            "get_debug_logs",
            "remove_toolchain_tools",
            "remove_toolchain_tools",
            "repair_all",
            "remove_all_tunnels",
        ]

    @pytest.mark.asyncio
    async def test_the_cleanliness_report_still_renders_a_row(self, monkeypatch):
        """``_project_items`` frames a failing PROBE as an unreadable row; it must not
        die before it has one to frame."""
        _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
        )

        report = await project.cleanliness()

        assert [i.name for i in report.items if i.kind is CleanlinessKind.PROJECT] == ["project4"]

    @pytest.mark.asyncio
    async def test_is_clean_still_answers(self, monkeypatch):
        """The cheap converge path. ``ensure_clean`` asks this BEFORE deciding whether
        to clean, so a refusal here pre-empts the decision it exists to inform."""
        self._contradiction(monkeypatch, dirty=("project4",))

        assert await project.is_clean() is False

    @pytest.mark.asyncio
    async def test_the_switched_off_provider_warns_on_a_builder_too(self, monkeypatch, caplog):
        """The discriminator against "build-up simply stopped refusing".

        ``build_up`` gates the LAB arm only. An explicitly ``-E``'d provider is
        the operator signing off, and that stays a warning on ``install`` —
        where the refusal does fire for the other reason.
        """
        bed = _wire(
            monkeypatch,
            projects=[_PROVIDER, "project4"],
            exclude_projects=(_PROVIDER_NORM,),
            dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == ["project4"]
        assert "handled externally" in _said(caplog)


class TestStatusHonorsSwitches:
    """``status`` reads the same predicate, and is loud on exactly one axis."""

    @pytest.mark.asyncio
    async def test_switched_off_project_is_not_asked_for_state(self, monkeypatch, caplog):
        """Not merely absent from the report — never ASKED.

        ``status`` reaches the fleet, and a switched-off project's is none of this
        run's business. ``bed.questions`` is what proves it was not contacted;
        a project dropped from the mapping after being asked would pass an
        absence-only assertion having already talked to hosts.
        """
        bed = _wire(
            monkeypatch,
            projects=["project1", "project2"],
            hosts=1,
            exclude_projects=("project2",),
            # Both projects declare a usable scope, so the LAB axis leaves both in
            # and the switch is the only thing that can drop one.
            scopes={"project1": _scope("project1"), "project2": _scope("project2")},
            state=InstallState.INSTALLED,
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            result = await project.status()

        assert result.projects == {"project1": InstallState.INSTALLED}
        assert bed.questions == [("project1", "status")]
        assert (
            "project 'project2' switched off for this run (--exclude-projects project2) "
            "— skipping it for status"
        ) in _said(caplog)
        # The row survives the skip: `scoping` is the display's account of the
        # whole lab, and a project that vanished from it entirely would be the
        # silent disappearance the warning above exists to prevent.
        assert set(result.scoping) == {"project1", "project2"}

    @pytest.mark.asyncio
    async def test_a_lab_inactive_project_is_left_out_silently(self, monkeypatch, caplog):
        """And the OTHER axis stays quiet, deliberately — the answer is the display.

        The scoping row already carries "not applicable" beside the project, and a
        warning as well would put a line in front of every operator whose lab
        simply does not include a project. Switches get one because no row
        mentions them; verdicts do not, because every row is one.

        The pair with the cell above is the whole assertion. A ``status`` that
        logged nothing at all fails there; one that logged on both axes fails
        here.
        """
        bed = _wire(
            monkeypatch,
            projects=["project1", _UNDERSCORED],
            hosts=1,
            current="project1",  # the D3 current-project gate again — see the section note
            scopes={_UNDERSCORED: _scope(_UNDERSCORED, excluded=True)},
            state=InstallState.INSTALLED,
        )

        with caplog.at_level(logging.WARNING, logger="otto.project.orchestrator"):
            result = await project.status()

        assert result.projects == {"project1": InstallState.INSTALLED}
        assert bed.questions == [("project1", "status")]
        assert result.scoping[_UNDERSCORED].applicable is False
        assert caplog.records == []


class TestForcedInclusionOfAnEmptyFleet:
    """``-I`` on a HOST-STARVED project: forced in, then loud from its own walk.

    Plan deviation 3. ``_applicable`` does not second-guess the switch — the
    operator said this project participates — so the project is walked and the
    refusal comes out of its own fleet surface, where the message can name the
    ``host_patterns`` that are the actual cause. An ``-I`` that honoured one
    unusable shape and quietly declined the other would be a switch meaning
    different things depending on which half of a declaration went wrong.
    """

    @pytest.mark.parametrize(
        ("include", "walked"),
        [((), ["project1"]), (("project2",), ["project1", "project2"])],
        ids=["without -I", "with -I"],
    )
    @pytest.mark.asyncio
    async def test_include_forces_a_host_starved_project_into_the_walk(
        self, monkeypatch, include, walked
    ):
        bed = _wire(
            monkeypatch,
            projects=["project1", "project2"],
            current="project1",  # the D3 current-project gate again — see the section note
            scopes={"project2": _scope("project2", universe=(), host_patterns=("nope",))},
            include_projects=include,
        )

        assert (await project.install()).is_ok

        assert [e[0] for e in bed.events if e[1] == "install"] == walked

    def test_the_forced_projects_own_fleet_walk_is_still_refused(self):
        """The consequence, through PRODUCTION code rather than this bed's doubles.

        ``_FakeCtx.all_hosts`` ignores ``_scope_owner``, so no walk in this
        module can show what a real owner-bound one does — asserting the raise
        through this bed would be asserting a double. So the two functions a
        real fleet walk calls are driven directly, against a verdict carrying a
        REAL compiled declaration and a NON-EMPTY host mapping: the fleet is
        empty because the declaration says so, not because the fixture handed
        over nothing. The widening row is what pins that distinction.
        """
        hosts = {
            "h0": SimpleNamespace(source_lab="bench"),
            "h1": SimpleNamespace(source_lab="bench"),
        }
        starved = _scope(
            "project2",
            universe=(),
            host_patterns=("nope",),
            config=ProjectScopeConfig([re.compile("bench")], [re.compile("nope")]),
        )
        widened = _scope(
            "project2",
            config=ProjectScopeConfig([re.compile("bench")], [re.compile("h.*")]),
        )

        assert scoped_ids(hosts, {"project2": starved}, "project2") == set()
        assert scoped_ids(hosts, {"project2": widened}, "project2") == {"h0", "h1"}

        with pytest.raises(ProjectScopeError) as excinfo:
            require_nonempty_fleet({"project2": starved}, set(), "project2")

        assert "nope" in _one_line(str(excinfo.value))


# ── how these messages RENDER, which is not what they say ────────────────────
#
# A log record is rendered as rich markup only when it opts in with
# ``extra={"markup": True}``, and the CLI error boundary prints through
# ``rich``. Where markup is rendered, rich reads ``[unix_alt]`` as a style tag
# and DELETES it, so a message that is perfect as a string reaches the operator
# with the one fact they needed missing — measured, not assumed: a
# ``loaded lab(s) [bench, floor]`` rendered as markup reads ``loaded lab(s)``.
# These tests render the orchestrator's lines through every sink, so a line
# that started opting in to markup without escaping its lab list is caught.


class _Sinks:
    """Everywhere one otto log record is rendered, captured together.

    THREE SINKS, NOT ONE, and that is the whole reason this class exists rather
    than a lone ``RichHandler``. An earlier version of these tests captured the
    console alone and certified a fix (``extra={"markup": False}``) that, at the
    time, repaired ONLY the console: the file formatter rendered every record as
    markup without consulting ``record.markup``, so both log files stayed broken
    and the guard could not see it. A rendering test that samples one of three
    sinks is a rendering test that passes for two thirds of the wrong reasons.

    ``console`` mirrors ``install_console``'s ``RichHandler`` kwargs;
    ``plain`` is ``console.log``'s handler (``RichFormatter``, ANSI stripped)
    and ``ansi`` is ``verbose.log``'s (``RichFormatter``, ANSI kept) — both
    built exactly as ``otto.logger.management._make_file_handler`` builds them.
    """

    def __init__(self, tmp_path):
        self.buf = io.StringIO()
        self.console_handler = RichHandler(
            console=Console(file=self.buf, width=300, force_terminal=False),
            markup=False,
            highlighter=NullHighlighter(),
            show_time=False,
            show_path=False,
        )
        self.paths = {}
        self.handlers = [self.console_handler]
        for name, rich in (("plain", False), ("ansi", True)):
            path = tmp_path / f"{name}.log"
            handler = logging.FileHandler(path, mode="x")
            formatter = RichFormatter()
            formatter.rich = rich
            handler.setFormatter(formatter)
            self.paths[name] = path
            self.handlers.append(handler)

    def rendered(self):
        """``{sink: one-line text}`` for every sink, flushed."""
        for handler in self.handlers:
            handler.flush()
        out = {"console": _one_line(self.buf.getvalue())}
        for name, path in self.paths.items():
            out[name] = _one_line(path.read_text())
        return out


@pytest.fixture
def rendered_warnings(monkeypatch, tmp_path):
    """The orchestrator's warnings through every sink otto renders them in.

    The width is pinned wide so a FOLD cannot masquerade as an eaten word.
    """
    sinks = _Sinks(tmp_path)
    monkeypatch.setattr(orchestrator.logger, "handlers", sinks.handlers)
    monkeypatch.setattr(orchestrator.logger, "propagate", False)
    monkeypatch.setattr(orchestrator.logger, "level", logging.WARNING)
    yield sinks
    for handler in sinks.handlers:
        handler.close()


def test_an_opted_in_lab_list_is_eaten_on_every_sink(rendered_warnings):
    """The negative control, so the cells below are not tautologies — on EVERY sink.

    Every sink renders an opted-in record as markup, and rich eats the lab
    list. Per-sink rather than "somewhere in the output", because the two file
    sinks are exactly the ones a console-only guard was blind to.
    """
    orchestrator.logger.warning(
        "not applicable to the loaded lab(s) [unix_alt]", extra={"markup": True}
    )

    for sink, text in rendered_warnings.rendered().items():
        assert "[unix_alt]" not in text, sink


def test_a_plain_record_keeps_its_lab_list_on_every_sink(rendered_warnings):
    """The plain-record cell (markup absent, the default) of the opt-in pair.

    Paired with the opted-in negative control above: ``record.markup`` is read
    by the console AND both files, so a record that does not ask for markup
    keeps its bracketed lab list on all three.
    """
    orchestrator.logger.warning(
        "not applicable to the loaded lab(s) [unix_alt]", extra={"markup": False}
    )

    for sink, text in rendered_warnings.rendered().items():
        assert "[unix_alt]" in text, sink


@pytest.mark.asyncio
async def test_the_skip_line_keeps_its_lab_list_in_every_sink(monkeypatch, rendered_warnings):
    """D3's own skip message, rendered. It names two lists, and every sink keeps both."""
    _wire(
        monkeypatch,
        projects=["project1", "project2"],
        current="project1",
        scopes={"project2": _scope("project2", excluded=True, loaded=("unix_alt", "bench"))},
    )

    assert (await project.install()).is_ok

    for sink, text in rendered_warnings.rendered().items():
        assert "not applicable to the loaded lab(s) [unix_alt, bench]" in text, sink
        assert "(lab_patterns: project2-lab)" in text, sink
        assert "\\[" not in text, sink


@pytest.mark.asyncio
async def test_the_optional_dependency_note_keeps_its_lab_list(monkeypatch, rendered_warnings):
    """The new message with the same shape — its reason parenthetical is a lab list."""
    _wire(
        monkeypatch,
        projects=[_PROVIDER, "project4"],
        scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
        dependencies={"project4": [_dep(_PROVIDER_NORM, required=False)]},
    )

    assert (await project.install()).is_ok

    for sink, text in rendered_warnings.rendered().items():
        assert (
            f"{_PROVIDER} is inactive (not applicable to the loaded lab(s) [unix_alt])"
        ) in text, sink
        assert "\\[" not in text, sink


@pytest.mark.asyncio
async def test_the_refusal_survives_the_cli_error_boundary_and_lands_on_stdout(monkeypatch, capsys):
    """``otto.cli.main``'s boundary frame is what a user meets this error through.

    Three facts, none of them visible to a ``str(exc)`` assertion:

    * the class is an :class:`~otto.errors.OttoError`, which is the ONLY reason
      the boundary's ``except OttoError`` reaches it rather than letting a
      traceback out;
    * ``print_error`` escapes markup, so the lab list arrives — the boundary
      renders ``f"error: {e}"`` and that is the exact call made here;
    * it lands on STDOUT. Worth pinning because "errors go to stderr" is the
      reasonable assumption and it is wrong here: ``print_error`` prints
      through rich's global console.

    Imported inside the test: ``otto.cli`` is a layer above this one, and this
    module's imports are deliberately the project layer's.
    """
    from otto.cli.invoke import print_error

    _wire(
        monkeypatch,
        projects=[_PROVIDER, "project4"],
        scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
        dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
    )
    with pytest.raises(InactiveRequiredDependencyError) as excinfo:
        await project.install()

    assert isinstance(excinfo.value, OttoError)
    monkeypatch.setenv("COLUMNS", "300")  # a fold must not masquerade as an eaten word
    print_error(f"error: {excinfo.value}")

    captured = capsys.readouterr()
    assert "not applicable to the loaded lab(s) [unix_alt]" in _one_line(captured.out)
    assert f"--exclude-projects={_PROVIDER_NORM}" in _one_line(captured.out)
    assert captured.err == ""


_NARROW = 80
"""The width rich falls back to when it cannot detect a terminal, and so the
narrowest a real invocation ever renders at."""


def _switch_survives(message, token, width=_NARROW):
    """Whether *token* — the switch AND its value — lands on ONE rendered line.

    *token* is handed in rather than parsed back out of *message*: recovering
    it by splitting on the switch name yields an empty string for the
    space-separated form, so the helper would look for the bare
    ``--exclude-projects``, find it, and report every case as surviving. That
    is a control that cannot fail, and it is what this signature exists to
    prevent.

    Rendered through a real console at *width*, escaped exactly as
    ``print_error`` escapes, because markup tags occupy no width and a
    character count done in the head measures the wrong string.
    """
    buf = io.StringIO()
    Console(file=buf, width=width, no_color=True).print(escape(message))
    return any(token in line for line in buf.getvalue().splitlines())


#: ``(dependent name length, provider name length)`` pairs measured to fold the
#: SPACE-separated form of this message at 80 columns. NOT guessed, and not
#: one-dimensional: swept over dependent 3-20 x provider 3-59, 167 of those
#: 1026 combinations split the switch from its value, across provider lengths
#: 13-21 and 31-41. One pair per exposed provider length is pinned below.
#:
#: The two dimensions are the point. Whether a given provider name folds
#: depends on where the DEPENDENT name left the preceding line, so a sweep that
#: fixes the dependent reports a different, narrower band and reads as "only
#: pathological names are affected". They are not: 15 and 16 are in the band,
#: and `otto-fleet-tools` is 16 characters.
_FOLDING_NAME_LENGTHS = [
    (3, 20),
    (3, 21),
    (3, 37),
    (3, 38),
    (3, 39),
    (3, 40),
    (3, 41),
    (5, 19),
    (10, 18),
    (12, 17),
    (14, 16),
    (15, 36),
    (16, 15),
    (16, 35),
    (17, 34),
    (18, 14),
    (18, 33),
    (19, 32),
    (20, 13),
    (20, 31),
]


@pytest.mark.parametrize(("dependent_len", "length"), _FOLDING_NAME_LENGTHS)
def test_the_refusals_switch_hint_never_folds(dependent_len, length):
    """``--exclude-projects=<name>`` is ONE token, so rich cannot break it.

    Rich folds between words. A hint reading ``--exclude-projects <name>``
    therefore hands the reader, at exactly these name lengths, a switch on one
    line and its value on the next — a thing that cannot be pasted, which is
    the whole point of printing it. ``print_error``'s own docstring states the
    rule ("would a line break inside it destroy something the reader has to
    retype?"); the boundary frame that renders this message does not pass
    ``soft_wrap``, and changing that would alter every otto error at once, so
    the fix is local: remove the break opportunity.

    EVERY CASE IS PROVEN TO FOLD FIRST. Without the control assertion this
    table would go quietly inert the day the surrounding prose changed length,
    and a passing width test that cannot fail is worse than none.
    """
    dependent = _name(dependent_len)
    provider = _name(length)

    assert not _switch_survives(
        _refusal(dependent, provider, " "), f"--exclude-projects {provider}"
    ), f"control: ({dependent_len}, {length}) no longer folds — re-sweep and retune the table"
    assert _switch_survives(_refusal(dependent, provider, "="), f"--exclude-projects={provider}")


def _name(length):
    """A project name of exactly *length* characters, with no break opportunity inside it."""
    return ("p" * (length - 1)) + "z"


def _refusal(dependent, provider, separator):
    """The production refusal, with the CLI boundary's own ``error: `` prefix.

    ``otto.cli.main`` renders ``f"error: {e}"``, and those seven characters are
    part of what has to fit — measuring the bare message would understate the
    fold by a word.
    """
    return (
        f"error: project {dependent!r} requires {provider!r}, but "
        f"{provider} is not applicable to the loaded lab(s) "
        f"[unix] (lab_patterns: unix-lab). "
        f"Load a lab {provider} applies to, or pass "
        f"--exclude-projects{separator}{provider} to declare it handled externally."
    )


@pytest.mark.asyncio
async def test_the_real_refusal_uses_the_unbreakable_form(monkeypatch):
    """The table above renders a reconstruction; this pins the REAL message's shape.

    Two cells, because either alone is weak: the table proves the joined form
    survives a fold, and this proves the code actually emits the joined form.
    """
    _wire(
        monkeypatch,
        projects=[_PROVIDER, "project4"],
        scopes={_PROVIDER: _scope(_PROVIDER, excluded=True, loaded=("unix_alt",))},
        dependencies={"project4": [_dep(_PROVIDER_NORM, required=True)]},
    )

    with pytest.raises(InactiveRequiredDependencyError) as excinfo:
        await project.install()

    assert f"--exclude-projects={_PROVIDER_NORM}" in str(excinfo.value)
    assert f"--exclude-projects {_PROVIDER_NORM}" not in str(excinfo.value)


# ── re-exports ───────────────────────────────────────────────────────────


def test_orchestrator_functions_are_reachable_from_the_package():
    # Tasks 9-10 (default instructions, CLI) wrap these names; the package IS
    # the public surface.
    for name in (
        "install",
        "uninstall",
        "cleanup",
        "get_logs",
        "install_tools",
        "uninstall_tools",
        "status",
        "is_uninstalled",
        "is_clean",
        "cleanliness",
        "ensure_installed",
        "ensure_uninstalled",
        "ensure_clean",
    ):
        assert callable(getattr(project, name)), name


# ── per-project options: one source, one options class per body ─────────────


class TestPerProjectOptions:
    """Each body is built its OWN options class from the shared source."""

    @pytest.fixture
    def two_project_lab(self, monkeypatch):
        """Wire a two-project lab (``a`` then ``b`` in walk order) around the given classes.

        The classes arrive already registered by ``@register_project_actions``
        -- that decorator is what attributes their instruction bodies to a project
        -- so this only re-registers them under the project names this file's
        doubles use, and points the orchestrator's lookups at them.
        """

        def wire(**classes):
            for label, cls in classes.items():
                _register_actions(label.lower(), cls)
            ctx = _FakeCtx([])
            _wire_lab(monkeypatch, ["a", "b"], ctx)
            return ctx

        return wire

    @pytest.mark.asyncio
    async def test_kwargs_source_hands_each_project_its_declared_class(self, two_project_lab):
        # Kills: one options instance built once and handed to every body. Project
        # ``a`` declared a field of its own, so the flag the operator typed has
        # to reach IT -- while project ``b``, which declared nothing, must still
        # get otto's own class rather than its neighbour's.
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.project import ProjectActions, register_project_actions
        from otto.project.orchestrator import run_project_instruction
        from otto.registry import registering_project

        seen: "dict[str, object]" = {}

        @options
        class AInstall(InstallOptions):
            variant: Annotated[str, typer.Option(help="v")] = "field"

        with registering_project("a"):

            @register_project_actions
            class A(ProjectActions):
                @instruction(options=AInstall)
                async def install(self, opts: AInstall):
                    seen["a"] = opts
                    return Result(Status.Success)

        with registering_project("b"):

            @register_project_actions
            class B(ProjectActions):
                async def install(self, opts):
                    seen["b"] = opts
                    return Result(Status.Success)

        two_project_lab(A=A, B=B)

        result = await run_project_instruction(
            "install", {"ensure": False, "recover_partial": True, "variant": "debug"}
        )

        assert result.is_ok
        assert type(seen["a"]) is AInstall
        assert seen["a"].variant == "debug"
        assert type(seen["b"]) is InstallOptions

    @pytest.mark.asyncio
    async def test_a_project_added_instruction_walks_and_combines(self, two_project_lab):
        """A name otto never declared gets the same walk, order and combiner."""
        from otto.cli.run import instruction
        from otto.project import ProjectActions, register_project_actions
        from otto.project.orchestrator import run_project_instruction
        from otto.registry import registering_project

        order: "list[str]" = []

        with registering_project("a"):

            @register_project_actions
            class A(ProjectActions):
                # The FIRST declaration fixes the walk shape; b's omits it and
                # must inherit it, which is what makes the order below a fact
                # about the table rather than about two agreeing decorators.
                @instruction(walk="reverse")
                async def deploy(self):
                    order.append("a")
                    return Result(Status.Success)

        with registering_project("b"):

            @register_project_actions
            class B(ProjectActions):
                @instruction()
                async def deploy(self):
                    order.append("b")
                    return Result(Status.Success)

        two_project_lab(A=A, B=B)

        result = await run_project_instruction("deploy", {})

        assert order == ["b", "a"]
        assert result.is_ok

    @pytest.mark.asyncio
    async def test_first_failure_names_the_project_and_stops_a_forward_walk(self, two_project_lab):
        """``install``'s shape: fail-fast, with the project named in the message."""
        from otto.project import ProjectActions, register_project_actions
        from otto.project.orchestrator import run_project_instruction
        from otto.registry import registering_project

        ran: "list[str]" = []

        with registering_project("a"):

            @register_project_actions
            class A(ProjectActions):
                async def install(self, opts):
                    del opts
                    ran.append("a")
                    return Result(Status.Failed, msg="boom")

        with registering_project("b"):

            @register_project_actions
            class B(ProjectActions):
                async def install(self, opts):
                    del opts
                    ran.append("b")
                    return Result(Status.Success)

        two_project_lab(A=A, B=B)

        result = await run_project_instruction("install", {})

        assert result.msg == "install failed in project 'a': boom"
        assert ran == ["a"]

    @pytest.mark.asyncio
    async def test_continue_on_failure_walks_every_project_and_reports_the_first(
        self, two_project_lab
    ):
        """``uninstall``'s shape: reversed, best-effort, first failure reported."""
        from otto.project import ProjectActions, register_project_actions
        from otto.project.orchestrator import run_project_instruction
        from otto.registry import registering_project

        ran: "list[str]" = []

        with registering_project("a"):

            @register_project_actions
            class A(ProjectActions):
                async def uninstall(self, opts):
                    del opts
                    ran.append("a")
                    return Result(Status.Success)

        with registering_project("b"):

            @register_project_actions
            class B(ProjectActions):
                async def uninstall(self, opts):
                    del opts
                    ran.append("b")
                    return Result(Status.Failed, msg="stuck")

        two_project_lab(A=A, B=B)

        result = await run_project_instruction("uninstall", {"debug_logs": False})

        assert ran == ["b", "a"]
        assert result.msg == "uninstall failed in project 'b': stuck"

    @pytest.mark.asyncio
    async def test_a_non_result_body_with_no_combiner_is_not_judged(self, two_project_lab):
        """The default combiner reads ``is_ok`` off a Result and off nothing else.

        A project may declare an instruction whose bodies answer in a vocabulary of
        their own -- a state, a count, a row -- and declare no ``combine_results``
        for it. Reading ``is_ok`` off such a value would raise ``AttributeError``
        out of the orchestrator on the one declaration shape that never asked
        for a verdict.
        """
        from otto.cli.run import instruction
        from otto.project import ProjectActions, register_project_actions
        from otto.project.orchestrator import run_project_instruction
        from otto.registry import registering_project

        ran: "list[str]" = []

        with registering_project("a"):

            @register_project_actions
            class A(ProjectActions):
                @instruction()
                async def survey(self):
                    ran.append("a")
                    return "a is fine"

        with registering_project("b"):

            @register_project_actions
            class B(ProjectActions):
                @instruction()
                async def survey(self):
                    ran.append("b")
                    return "b is fine"

        two_project_lab(A=A, B=B)

        result = await run_project_instruction("survey", {})

        assert ran == ["a", "b"]
        assert result.status is Status.Success

    @pytest.mark.asyncio
    async def test_the_six_entries_are_resolved_on_the_module_at_call_time(self, monkeypatch):
        """Kills a ``_ENTRIES`` of function OBJECTS frozen at import.

        The next layer's tests drive the published commands and monkeypatch
        these very names; a dispatcher holding the original objects would keep
        running them while every other caller ran the double.
        """
        from otto.params import OptionsSource
        from otto.project.orchestrator import run_project_instruction

        seen: "list[OptionsSource]" = []

        async def _recorder(source):
            seen.append(source)
            return Result(Status.Success)

        monkeypatch.setattr(orchestrator, "install", _recorder)

        result = await run_project_instruction(
            "install", {"ensure": True, "recover_partial": False}
        )

        assert result.is_ok
        assert len(seen) == 1
        assert isinstance(seen[0], OptionsSource)
        assert seen[0].build(InstallOptions) == InstallOptions(ensure=True, recover_partial=False)
