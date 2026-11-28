"""The context carries the run's projects: one BootstrapResult, read lazily, cached (spec 2 §4)."""

import pytest

from otto import bootstrap as bs
from otto.config.scope import scopes_of
from otto.context import OttoContext
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, seed_scope_verdicts
from tests._fixtures.fake_project import fake_project

LIBRARY_LAB = "<library>"
"""The library sentinel's lab name: the host-less context ``run_tests`` builds when none is
active. Spelled here, not imported, because only its value is public behaviour; the verdicts
special-case it, so a lab of any other name fails the sentinel test below."""


@pytest.fixture(autouse=True)
def _fresh():
    bs.invalidate()
    yield
    bs.invalidate()


def _counting(monkeypatch, *results):
    """Make bootstrap() return *results* in turn (an exception instance raises); count the calls."""
    calls = []

    def fake():
        calls.append(1)
        outcome = results[min(len(calls), len(results)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("otto.bootstrap.bootstrap", fake)
    return calls


def test_a_hand_built_context_reads_its_projects_once_lazily(monkeypatch):
    project = fake_project("alpha")
    calls = _counting(monkeypatch, fake_bootstrap_result([project]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert calls == []  # nothing read at construction
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert [r.name for r in ctx.ordered_projects] == ["alpha"]
    assert calls == [1]


def test_a_given_result_is_used_and_bootstrap_is_never_called(monkeypatch):
    project = fake_project("alpha")
    calls = _counting(monkeypatch, RuntimeError("must not be called"))
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([project]))
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert calls == []


def test_known_empty_and_unavailable_are_distinct_and_a_refusal_is_cached(monkeypatch):
    empty = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    assert empty.projects == []
    assert scopes_of(empty) == {}

    refusal = FileNotFoundError("no such project root")
    calls = _counting(monkeypatch, refusal)
    unavailable = OttoContext(lab=Lab(name="rig"))
    for _ in range(2):
        with pytest.raises(FileNotFoundError) as raised:
            _ = unavailable.projects
        assert raised.value is refusal
    with pytest.raises(FileNotFoundError):
        scopes_of(unavailable)
    assert calls == [1]


def test_the_library_sentinel_still_bootstraps_for_its_projects(monkeypatch):
    project = fake_project("alpha")
    _counting(monkeypatch, fake_bootstrap_result([project]))
    ctx = OttoContext(lab=Lab(name=LIBRARY_LAB))
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert scopes_of(ctx) == {}  # verdicts special-case the sentinel; projects do not


def test_projects_ordered_projects_and_the_verdicts_read_one_result_across_an_invalidate(
    monkeypatch,
):
    first, second = fake_project("first"), fake_project("second")
    _counting(monkeypatch, fake_bootstrap_result([first]), fake_bootstrap_result([second]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert [r.name for r in ctx.projects] == ["first"]
    bs.invalidate()
    assert [r.name for r in ctx.ordered_projects] == ["first"]
    assert list(scopes_of(ctx)) == [
        "first"
    ]  # every parsed project is keyed, so "second" would show


def test_mutating_the_returned_projects_changes_nothing():  # Review Focus 5
    alpha, beta = fake_project("alpha"), fake_project("beta")
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([alpha]))
    ctx.projects.append(beta)
    ctx.ordered_projects.clear()
    # Resolved only now, after the mutations: verdicts over a live list would key "beta" too.
    assert list(scopes_of(ctx)) == ["alpha"]
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert [r.name for r in ctx.ordered_projects] == ["alpha"]


def test_scopes_is_no_longer_a_member_and_the_private_alias_is_gone():
    assert not hasattr(OttoContext, "scopes")
    assert not hasattr(OttoContext, "_admissible_ids")


def test_seeded_verdicts_are_what_scopes_of_returns():
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    seed_scope_verdicts(ctx, {"alpha": object()})
    assert list(scopes_of(ctx)) == ["alpha"]


def _first_then_later(monkeypatch):
    """bootstrap() answers a project named "given" once, then "later" on every further call."""
    return _counting(
        monkeypatch,
        fake_bootstrap_result([fake_project("given")]),
        fake_bootstrap_result([fake_project("later")]),
    )


@pytest.mark.parametrize("root_callback_ran", [True, False])
def test_the_cli_context_holds_the_result_ensure_lab_context_read(monkeypatch, root_callback_ran):
    from types import SimpleNamespace

    import typer
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from otto.invocation import RunPolicy, install_policy, reset_binding
    from tests._fixtures.rootoptions import make_root_options

    calls = _first_then_later(monkeypatch)
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs, **_kw: BuiltLab(
            lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None)
        ),
    )
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    binding = install_policy(RunPolicy()) if root_callback_ran else None
    try:
        with typer.Context(TyperGroup(name="otto")) as ctx:
            ctx.meta["_otto_root_options"] = make_root_options(labs=["rig"])
            built = ensure_lab_context(ctx)
            # The one read the lab was built from, not a second one by the context.
            assert [r.name for r in built.projects] == ["given"]
            assert [r.name for r in built.ordered_projects] == ["given"]
    finally:
        if binding is not None:
            reset_binding(binding)
    assert calls == [1]


@pytest.mark.asyncio
async def test_open_context_hands_its_context_the_result_it_read(monkeypatch):
    from otto.context import open_context

    _first_then_later(monkeypatch)
    # open_context reads bootstrap() first; a context reading its own would see "later".
    async with open_context(lab=Lab(name="rig")) as ctx:
        assert [r.name for r in ctx.projects] == ["given"]
        assert [r.name for r in ctx.ordered_projects] == ["given"]


# ── ctx.unscoped: the whole universe, read past any project scope ─────────────


def test_unscoped_projects_without_run_composes(monkeypatch):
    """With no run installed, the whole-universe reader composes, as ``current_projects`` does."""
    from otto.declared import unscoped_projects
    from otto.run_context import try_get_context

    assert try_get_context() is None  # the premise: nothing is installed
    calls = _counting(monkeypatch, fake_bootstrap_result([fake_project("composed")]))
    assert [r.name for r in unscoped_projects()] == ["composed"]
    assert calls == [1]


def test_unscoped_view_is_the_canonical_lab_and_every_project():
    """``ctx.unscoped`` reads the context live: its lab, then every parsed project, fresh lists."""
    from otto.context import UnscopedView

    alpha, skipped = fake_project("alpha"), fake_project("skipped")
    lab = Lab(name="rig")
    ctx = OttoContext(lab=lab, bootstrap=fake_bootstrap_result([alpha, skipped], ordered=[alpha]))
    view = ctx.unscoped
    assert isinstance(view, UnscopedView)
    assert view.lab is lab
    assert [r.name for r in view.projects] == ["alpha", "skipped"]
    assert [r.name for r in view.ordered_projects] == ["alpha"]
    view.projects.clear()  # a fresh list: changing it changes nothing
    assert [r.name for r in view.projects] == ["alpha", "skipped"]
    with pytest.raises(AttributeError):
        view.lab = Lab(name="other")  # a read-only property
    replacement = Lab(name="other")
    ctx.lab = replacement
    assert view.lab is replacement  # live: a later ctx.lab is the lab read here


class _Reached(BaseException):
    """Raised by the stand-in's project lists: the reader went through ``ctx.unscoped``."""


class _ReadScoped(BaseException):
    """Raised by the patched ``ctx.projects`` and ``ctx.ordered_projects``: the reader stayed."""


class _StandIn:
    """What ``OttoContext.unscoped`` returns under the stand-in: distinguishable by raising."""

    def __init__(self, ctx):
        # The canonical lab: a scoped view would judge its hosts through the
        # stand-in's own raising project lists.
        self.lab = ctx._lab

    @property
    def projects(self):
        raise _Reached("projects")

    @property
    def ordered_projects(self):
        raise _Reached("ordered_projects")


def _read_scoped(_ctx):
    raise _ReadScoped("the reader read ctx.projects / ctx.ordered_projects")


def _no_op(*_args, **_kwargs):
    return None


def _reader_orchestrator_lab(ctx, monkeypatch, tmp_path):
    from otto.project import orchestrator

    monkeypatch.setattr("otto.project.orchestrator._enforce_current_scope", _no_op)
    return orchestrator._lab()


def _reader_driving_scope(ctx, monkeypatch, tmp_path):
    from otto.project.orchestrator import _enforce_current_scope

    return _enforce_current_scope(ctx)


def _reader_body_options(ctx, monkeypatch, tmp_path):
    from otto.project.orchestrator import project_instruction_body_options

    return project_instruction_body_options("install", ctx, {}, announce=False)


def _reader_status(ctx, monkeypatch, tmp_path):
    from otto.project.orchestrator import status

    monkeypatch.setattr("otto.project.orchestrator._lab", lambda: (ctx, []))
    return status()


def _reader_plan(ctx, monkeypatch, tmp_path):
    from otto.project.plan import plan_instruction

    return plan_instruction(name="install", ctx=ctx, kwargs={})


def _reader_dependencies(ctx, monkeypatch, tmp_path):
    from otto.session.dependencies import check_dependencies

    return check_dependencies(ctx=ctx)


def _reader_prepare_run(ctx, monkeypatch, tmp_path):
    from otto.suite.run import RunOptions, prepare_run

    return prepare_run(RunOptions(cov_tickets_json=tmp_path / "tickets.json"), dry_run=True)


def _reader_run_tests(ctx, monkeypatch, tmp_path):
    from otto.suite.run import run_tests

    return run_tests(names=["test_x"], output_dir=tmp_path / "out")


def _reader_listing(ctx, monkeypatch, tmp_path):
    from otto.suite.run import _collect_listing

    return _collect_listing(["test_x"], "")


def _reader_monitor_driving_scope(ctx, monkeypatch, tmp_path):
    from otto.monitor.live import _enforce_driving_project_scope

    return _enforce_driving_project_scope()


def _reader_monitor_tls(ctx, monkeypatch, tmp_path):
    from otto.monitor.live import run_live

    def _tls_unreached(*, projects):
        raise AssertionError(f"the TLS lookup ran over {projects!r}, not the unscoped projects")

    monkeypatch.setattr("otto.monitor.live._enforce_driving_project_scope", _no_op)
    monkeypatch.setattr("otto.monitor.live.select_monitor_hosts", lambda **_kw: [])
    monkeypatch.setattr("otto.monitor.tls.resolve_monitor_tls", _tls_unreached)
    return run_live()


def _reader_clean_coverage(ctx, monkeypatch, tmp_path):
    from otto.coverage.collect import clean_coverage

    return clean_coverage()


def _reader_collect_coverage(ctx, monkeypatch, tmp_path):
    from otto.coverage.collect import collect_coverage

    return collect_coverage(tmp_path / "cov")


def _reader_detect_for_lab(ctx, monkeypatch, tmp_path):
    from otto.coverage.instrumentation import detect_for_lab

    return detect_for_lab()


def _reader_get_coverage(ctx, monkeypatch, tmp_path):
    from otto.coverage.get import get_coverage

    return get_coverage(output_dir=tmp_path / "get")


def _reader_detect_cov(ctx, monkeypatch, tmp_path):
    return ctx.cov


_MOVED_READERS = {
    "orchestrator walk": (_reader_orchestrator_lab, "ordered_projects"),
    "orchestrator driving scope": (_reader_driving_scope, "projects"),
    "orchestrator body options": (_reader_body_options, "ordered_projects"),
    "status scoping rows": (_reader_status, "projects"),
    "install preview": (_reader_plan, "ordered_projects"),
    "dependency preflight": (_reader_dependencies, "ordered_projects"),
    "prepare_run tickets": (_reader_prepare_run, "projects"),
    "run_tests layout": (_reader_run_tests, "projects"),
    "test listing": (_reader_listing, "projects"),
    "monitor driving scope": (_reader_monitor_driving_scope, "projects"),
    "monitor TLS": (_reader_monitor_tls, "projects"),
    "cov clean": (_reader_clean_coverage, "projects"),
    "cov collect": (_reader_collect_coverage, "projects"),
    "cov detect": (_reader_detect_for_lab, "projects"),
    "cov get": (_reader_get_coverage, "projects"),
    "ctx.cov": (_reader_detect_cov, "projects"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", list(_MOVED_READERS), ids=list(_MOVED_READERS))
async def test_moved_project_readers_read_unscoped(reader, monkeypatch, tmp_path):
    """Each whole-universe project reader reads ``ctx.unscoped``, never ``ctx.projects``.

    ``OttoContext.unscoped`` is patched to a stand-in whose project lists
    raise :class:`_Reached`, and ``ctx.projects`` / ``ctx.ordered_projects``
    to raise :class:`_ReadScoped`. Both are ``BaseException``, so a reader's
    own ``except Exception`` (``ctx.cov``'s) cannot swallow the signal. A
    reader still on the scoped lists raises the second, or reads them and
    goes on to finish or fail some other way.
    """
    import inspect

    from otto.run_context import set_context

    drive, which = _MOVED_READERS[reader]
    monkeypatch.setattr(OttoContext, "unscoped", property(_StandIn), raising=False)
    monkeypatch.setattr(OttoContext, "projects", property(_read_scoped))
    monkeypatch.setattr(OttoContext, "ordered_projects", property(_read_scoped))
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    set_context(ctx)

    async def _read():
        outcome = drive(ctx, monkeypatch, tmp_path)
        if inspect.isawaitable(outcome):
            await outcome

    with pytest.raises(_Reached) as reached:
        await _read()
    assert reached.value.args == (which,)


def test_driving_scope_one_function(monkeypatch):
    """The orchestrator's entry and ``otto monitor --live`` judge the driving project in one place.

    Both reach ``otto.config.scope.require_driving_scope`` with the context,
    so a change to which project drives the run cannot land in one of them only.
    """
    from otto.monitor.live import _enforce_driving_project_scope
    from otto.project.orchestrator import _enforce_current_scope
    from otto.run_context import set_context

    asked = []
    monkeypatch.setattr("otto.config.scope.require_driving_scope", asked.append, raising=False)
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([fake_project("app")]))
    set_context(ctx)

    _enforce_current_scope(ctx)
    _enforce_driving_project_scope()
    assert asked == [ctx, ctx]


# ── ctx.projects: the projects taking part (scoping spec §3.7) ────────────────


def _names(projects):
    return [p.name for p in projects]


@pytest.mark.parametrize(
    ("switches", "expected"),
    [
        ({}, ["inscope", "plain"]),
        ({"include_projects": ("outscope",)}, ["inscope", "outscope", "plain"]),
        ({"exclude_projects": ("inscope",)}, ["plain"]),
    ],
    ids=["lab", "include", "exclude"],
)
def test_projects_participating_discovery_order(tmp_path, switches, expected):
    """A scoped command's ``ctx.projects`` is the projects that take part, in discovery order.

    ``outscope`` declares ``lab_patterns=["beta"]`` and only ``alpha`` is loaded;
    ``-I`` brings it back in its discovery place, ``-E`` switches one off.
    """
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path, mixed=True, **switches)
    assert _names(ws.ctx.unscoped.projects) == ["inscope", "outscope", "plain"]
    assert _names(ws.ctx.projects) == expected


def test_ordered_projects_participating_dependency_order(tmp_path):
    """``ctx.ordered_projects`` narrows too (Q1), keeping the dependency order, not discovery's.

    The ``ordered=`` given is an arbitrary order, unlike discovery's, chosen
    so the test shows the narrowed list preserves whatever order it is given.
    """
    from tests._fixtures.fleet import install_scoped_context, scope_workspace

    ws = scope_workspace(tmp_path, mixed=True)
    inscope, outscope, plain = ws.ctx.unscoped.projects
    ctx = install_scoped_context(
        ws.lab, [inscope, outscope, plain], ordered=[plain, outscope, inscope]
    )
    assert _names(ctx.unscoped.ordered_projects) == ["plain", "outscope", "inscope"]
    assert _names(ctx.ordered_projects) == ["plain", "inscope"]


def test_current_projects_follows_run(tmp_path):
    """With a scoped run installed, the context-free readers answer its participating projects."""
    from otto.declared import current_ordered_projects, current_projects, unscoped_projects
    from tests._fixtures.fleet import scope_workspace

    scope_workspace(tmp_path, mixed=True)
    assert _names(current_projects()) == ["inscope", "plain"]
    assert _names(current_ordered_projects()) == ["inscope", "plain"]
    assert _names(unscoped_projects()) == ["inscope", "outscope", "plain"]


def test_current_projects_without_run_every_project(monkeypatch):
    """With no run installed there is no lab and no switch, so every composed project answers."""
    import re

    from otto.config.scope import ProjectScopeConfig
    from otto.declared import current_ordered_projects, current_projects
    from otto.run_context import try_get_context

    assert try_get_context() is None  # the premise: nothing is installed
    elsewhere = fake_project(
        "elsewhere",
        project_scope=ProjectScopeConfig(lab_patterns=[re.compile("nowhere")], host_patterns=[]),
    )
    plain = fake_project("plain")
    _counting(monkeypatch, fake_bootstrap_result([elsewhere, plain], ordered=[plain, elsewhere]))
    assert _names(current_projects()) == ["elsewhere", "plain"]
    assert _names(current_ordered_projects()) == ["plain", "elsewhere"]


def test_unscoped_command_projects_every_project(tmp_path):
    """A command that opted out of scoping reads every parsed project, ``-E`` notwithstanding."""
    from otto.declared import current_projects
    from tests._fixtures.fleet import install_scoped_context, scope_workspace

    ws = scope_workspace(tmp_path, mixed=True)
    every = ws.ctx.unscoped.projects
    ctx = install_scoped_context(ws.lab, every, exclude_projects=("inscope",), scoped=False)
    assert not ctx.scoped
    assert _names(ctx.projects) == ["inscope", "outscope", "plain"]
    assert _names(ctx.ordered_projects) == ["inscope", "outscope", "plain"]
    assert _names(current_projects()) == ["inscope", "outscope", "plain"]


def test_projects_and_scope_resolution_do_not_recurse(tmp_path, monkeypatch):
    """Scope resolution reads the unscoped projects, never the participating lists it decides.

    First the participating lists raise, and the verdicts, the host judge, the
    view and admission still answer over a context whose verdicts were not yet
    resolved. Then, on a fresh context, ``ctx.projects`` is the first read: the
    resolution runs inside it and never re-enters either list.
    """
    from otto.config.scope import participating
    from tests._fixtures.fleet import install_scoped_context, scope_workspace

    ws = scope_workspace(tmp_path, mixed=True)
    every = ws.ctx.unscoped.projects
    real_projects, real_ordered = OttoContext.projects, OttoContext.ordered_projects

    monkeypatch.setattr(OttoContext, "projects", property(_read_scoped))
    monkeypatch.setattr(OttoContext, "ordered_projects", property(_read_scoped))
    assert list(scopes_of(ws.ctx)) == ["inscope", "outscope", "plain"]
    assert "a-dut" in ws.ctx.targetable_ids()
    assert "a-dut" in list(ws.ctx.lab.hosts)
    assert "a-dut" in ws.ctx.admissible_ids()
    assert _names(participating(every, ctx=ws.ctx).kept) == ["inscope", "plain"]

    entered: list[str] = []

    def _once(prop, name):
        def read(self):
            if entered:
                raise AssertionError(f"{name} re-entered from {entered[-1]}")
            entered.append(name)
            try:
                return prop.fget(self)
            finally:
                entered.pop()

        return property(read)

    monkeypatch.setattr(OttoContext, "projects", _once(real_projects, "projects"))
    monkeypatch.setattr(OttoContext, "ordered_projects", _once(real_ordered, "ordered_projects"))
    fresh = install_scoped_context(ws.lab, every)
    assert _names(fresh.projects) == _names(fresh.ordered_projects)
    assert "inscope" in _names(fresh.projects)
