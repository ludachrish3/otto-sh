"""Per-project ``ProjectActions`` — owner-scoped defaults, tri-state status, registration.

NO LOCAL REGISTRY-ISOLATION FIXTURE HERE, deliberately. ``PROJECT_ACTIONS`` is
an ``otto.registry.Registry``, and the root conftest's autouse
``_isolate_registries`` discovers every ``Registry`` reachable from a loaded
``otto.*`` module dynamically — this one included, from the import above.
Pinned by ``test_registration_survives_a_repeat_run`` below, which registers
the same project name a second time in the same process.
"""

import pytest

from otto import layout
from otto.context import OttoContext, ProjectContextView
from otto.errors import OttoError
from otto.labs.loader import Lab
from otto.project import (
    PROJECT_ACTIONS,
    Cleanliness,
    CleanlinessItem,
    CleanlinessKind,
    CleanlinessReport,
    CleanupOptions,
    GetLogsOptions,
    InstallOptions,
    InstallState,
    InstallToolsOptions,
    ProjectActions,
    ProjectStatus,
    StatusOptions,
    UninstallOptions,
    UninstallToolsOptions,
    actions_for,
    register_project_actions,
)
from otto.registry import registering_project
from otto.result import Result
from otto.utils import Status
from tests._fixtures.fake_project import fake_project

PROJECT = fake_project("acme")
OTHER_REPO = fake_project("other")


def _fail(msg):
    return Result(Status.Failed, msg=msg)


class _FakeItem:
    """Product/dev-tool double — the four lifecycle verbs plus ``name``/``owner``."""

    def __init__(self, name, owner, installed=False, **scripted):
        self.name = name
        self.owner = owner
        self.installed = installed
        self.calls = []
        self._scripted = scripted

    async def _verb(self, verb, host):
        self.calls.append((verb, host.id))
        return self._scripted.get(verb, Result(Status.Success))

    async def stage(self, host):
        return await self._verb("stage", host)

    async def install(self, host):
        return await self._verb("install", host)

    async def uninstall(self, host):
        return await self._verb("uninstall", host)

    async def is_installed(self, host):
        self.calls.append(("is_installed", host.id))
        return self.installed


# The host verbs ProjectActions' dispatch helpers call on each fleet host.
# Anything else asked of the double is a mistake and raises AttributeError,
# rather than being silently recorded as a verb the host layer does not have.
_HOST_VERBS = (
    "install",
    "uninstall",
    "get_product_logs",
    "install_dev_tools",
    "uninstall_dev_tools",
)


class _FakeHost:
    """Recording host double: fleet verbs are recorded, products/dev tools are data."""

    def __init__(self, host_id, products=(), dev_tools=(), log_dir=None):
        self.id = host_id
        self.products = list(products)
        self.dev_tools = list(dev_tools)
        self.calls = []
        self._scripted = {}
        self._log_dir = log_dir

    def script(self, verb, outcome):
        """Make *verb* return *outcome* — or raise it, when it is an exception."""
        self._scripted[verb] = outcome

    def log_dest(self, dest=None):
        assert self._log_dir is not None, f"host {self.id} was given no log dir"
        return self._log_dir

    def __getattr__(self, name):
        if name not in _HOST_VERBS:
            raise AttributeError(name)

        async def _recorder(*_args, **kwargs):
            self.calls.append((name, kwargs))
            outcome = self._scripted.get(name, Result(Status.Success))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return _recorder


class _FakeCtx:
    """OttoContext double — the two dispatch seams ProjectActions uses.

    A PLAIN-CONTEXT double, deliberately: ``ProjectActions`` is constructed
    around ``ctx.for_project(name)``, so what these tests exercise is the REAL
    :class:`~otto.context.ProjectContextView` sitting on top of this. The owner
    every host verb below records therefore comes from production code, not
    from a double reimplementing the injection it is meant to certify.

    ``_scope_owner`` is the view's other half — the universe binding — and is
    recorded rather than honoured (this double holds a list, not a lab, so
    there is nothing here to narrow). Recording it is what lets a test assert
    the project layer's walks are BOUND as well as stamped.
    """

    # The real seam, applied to the double: `for_project` only wraps, so borrowing
    # OttoContext's own method keeps a second copy of the wiring out of the
    # tests that exist to certify it.
    for_project = OttoContext.for_project

    def __init__(self, hosts):
        self.hosts = list(hosts)
        self.scope_owners = []

    def all_hosts(self, _scope_owner=None):
        self.scope_owners.append(_scope_owner)
        return iter(self.hosts)

    async def do_for_all_hosts(self, method, *args, _scope_owner=None, **kwargs):
        """Apply *method* per host EXACTLY as the production seam does.

        The call shape is ``method(host, ...)`` -- the function object it was
        handed, applied to the host, with NO name lookup (``otto/run_context.py``'s
        ``method(h, *args, **kwargs)``). An earlier version looked the verb up
        by ``method.__name__`` on the double when it had one, which is dynamic
        dispatch where the real seam is static: it certified host-class
        overrides the production walks did not honour. Exceptions are CAPTURED
        as values rather than propagated, as there.

        Every coroutine ``ProjectActions`` hands this therefore runs FOR REAL
        against the double -- the dispatch helpers land on the recorded fleet
        verbs below, and the owned-dev-tool walkers on its product/tool lists.
        """
        self.scope_owners.append(_scope_owner)
        out = {}
        for host in self.hosts:
            try:
                out[host.id] = await method(host, *args, **kwargs)
            except Exception as exc:  # noqa: PERF203,BLE001 — mirrors do_for_all_hosts' capture
                out[host.id] = exc
        return out


def _fake_ctx(n=2, **host_kwargs):
    hosts = [_FakeHost(f"h{i}", **host_kwargs) for i in range(n)]
    return _FakeCtx(hosts), hosts


def _actions(ctx, project=PROJECT, cls=ProjectActions):
    """Build actions the way production does — around ``ctx.for_project(project.name)``.

    ``actions_for`` is the only constructor a user's project ever reaches, and the
    project-scoped view it supplies is what carries the owner scope now that no
    ``ProjectActions`` body spells ``owner=``. Constructing around the plain
    double instead would exercise an unscoped instance nothing produces, and
    every ``{"owner": "acme"}`` assertion below would be measuring the absence
    of a seam rather than the seam.
    """
    return cls(project=project, ctx=ctx.for_project(project.name))


def _ctx_with_products(owner, installed_flags, other_flags=()):
    """One host carrying *owner*'s products plus another project's, per flag list."""
    products = [_FakeItem(f"p{i}", owner, installed=f) for i, f in enumerate(installed_flags)]
    products += [_FakeItem(f"o{i}", "other", installed=f) for i, f in enumerate(other_flags)]
    host = _FakeHost("h0", products=products)
    return _FakeCtx([host]), host


# ── install ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_install_dispatches_owner_scoped_to_all_hosts():
    # Kills: forgetting the owner filter — project A's actions would install
    # project B's products (the exact cross-project bleed the design forbids).
    ctx, hosts = _fake_ctx(n=2)
    result = await _actions(ctx).install(InstallOptions())
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("install", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_every_project_walk_is_bound_to_its_own_projects_universe():
    """The view's OTHER half: this layer's walks name their project as well as stamp it.

    Every ``{"owner": "acme"}`` assertion in this file reads the stamp, which
    the host verbs record. The universe binding is a second, separate override
    on the view and leaves no trace in those records at all — a view that bound
    nothing would pass every other test here while walking the whole union.
    Both surfaces are exercised because both are overridden: ``install``
    dispatches, ``owns_products`` iterates.
    """
    ctx, _ = _fake_ctx(n=1)
    actions = _actions(ctx)

    assert (await actions.install(InstallOptions())).is_ok
    assert actions.owns_products is False
    assert ctx.scope_owners == ["acme", "acme"]


@pytest.mark.asyncio
async def test_default_install_reduces_first_host_failure():
    ctx, hosts = _fake_ctx(n=2)
    hosts[1].script("install", _fail("no space"))
    result = await _actions(ctx).install(InstallOptions())
    assert not result.is_ok
    assert result.status is Status.Failed  # the host's own status, not a generic one
    assert hosts[1].id in result.msg  # kills: dropping WHICH host failed
    assert "no space" in result.msg


@pytest.mark.asyncio
async def test_a_failed_command_result_with_no_msg_still_names_why():
    """A hook that returns ``host.run``'s own failure carries its reason in ``value``.

    ``DeclaredProduct.install`` hands back the ``Results`` of its install
    command, whose ``msg`` is empty: the reason is the failing command and its
    output. The summary once ended ``host 'h1': `` with nothing after it.
    """
    from otto.result import CommandResult, Results

    ctx, hosts = _fake_ctx(n=2)
    failed = CommandResult(
        Status.Failed,
        value="chmod: cannot access '/opt/stage/app.bin': Not a directory\n",
        command="chmod +x /opt/stage/app.bin",
        retcode=1,
    )
    hosts[1].script("install", Results.collect([failed]))
    result = await _actions(ctx).install(InstallOptions())
    assert result.status is Status.Failed
    assert result.msg.startswith(f"host {hosts[1].id!r}: ")
    assert "chmod +x /opt/stage/app.bin" in result.msg
    assert "exit 1" in result.msg
    assert "Not a directory" in result.msg


@pytest.mark.asyncio
async def test_a_failed_result_with_no_reason_at_all_still_says_how_it_ended():
    ctx, hosts = _fake_ctx(n=1)
    hosts[0].script("install", Result(Status.Error))
    result = await _actions(ctx).install(InstallOptions())
    assert not result.msg.endswith(": "), result.msg
    assert result.msg == f"host {hosts[0].id!r}: failed (Error) with no reason given"


@pytest.mark.asyncio
async def test_install_reduces_a_captured_host_exception():
    # do_for_all_hosts captures exceptions AS VALUES; a reduction that only
    # understands Results would treat a crashed host as a pass.
    ctx, hosts = _fake_ctx(n=2)
    hosts[0].script("install", OSError("ssh died"))
    result = await _actions(ctx).install(InstallOptions())
    assert not result.is_ok
    assert "h0" in result.msg
    assert "ssh died" in result.msg


# ── uninstall / cleanup ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uninstall_hardwires_debug_logs_off():
    # THE spec §5 rule at the project layer: host debug logs belong to no project,
    # so a per-project uninstall must never gather them (N projects would each sweep,
    # overwriting the last). Kills: forwarding get_debug_logs, or omitting it
    # and inheriting the host default of True.
    ctx, hosts = _fake_ctx(n=1)
    await _actions(ctx).uninstall(UninstallOptions())
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": True, "get_debug_logs": False, "owner": "acme"}),
    ]


@pytest.mark.asyncio
async def test_uninstall_forwards_get_product_logs_false():
    ctx, hosts = _fake_ctx(n=1)
    await _actions(ctx).uninstall(UninstallOptions(product_logs=False))
    assert hosts[0].calls[0][1]["get_product_logs"] is False


@pytest.mark.asyncio
async def test_cleanup_uninstalls_then_removes_owner_scoped_dev_tools():
    # Kills: cleanup that skips the tools (leaving probes behind), that drops
    # the owner scope (removing another project's tools), or that removes tools
    # BEFORE the products that may need them.
    #
    # The tool WALK is the host verb's -- filter, order and best-effort rule
    # are pinned in tests/unit/host/test_host_lifecycle_filters.py. What this
    # layer owes is dispatching it once per host, owner-scoped, second; the
    # empty tool records below are the other half of that claim (a re-inlined
    # walk here would show up as calls on the tools themselves).
    mine = _FakeItem("probe", "acme")
    theirs = _FakeItem("their-probe", "other")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine, theirs])
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert result.is_ok
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": True, "get_debug_logs": False, "owner": "acme"}),
        ("uninstall_dev_tools", {"owner": "acme"}),
    ]
    assert mine.calls == []
    assert theirs.calls == []


@pytest.mark.asyncio
async def test_cleanup_forwards_get_product_logs_to_the_uninstall_half():
    # Kills: `await self.uninstall(UninstallOptions())` with the flag dropped on the floor.
    # `cleanup` is `uninstall` plus the tooling, so a caller that asked to skip
    # the log haul asked cleanup's uninstall half to skip it too — and hard-
    # wiring True passes every other cleanup test in this file, all of which
    # take the default.
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    result = await _actions(ctx).cleanup(CleanupOptions(product_logs=False))
    assert result.is_ok
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": False, "get_debug_logs": False, "owner": "acme"}),
        ("uninstall_dev_tools", {"owner": "acme"}),
    ]


@pytest.mark.asyncio
async def test_cleanup_reports_a_failed_dev_tool_removal():
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall_dev_tools", _fail("busy"))
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert not result.is_ok
    assert "h0" in result.msg
    assert "busy" in result.msg


@pytest.mark.asyncio
async def test_cleanup_still_removes_dev_tools_after_a_failed_uninstall():
    # Best-effort teardown: a stranded product must not strand the tooling too.
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall", _fail("busy"))
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert not result.is_ok
    assert "busy" in result.msg  # the FIRST failure is what is reported
    assert hosts[0].calls[-1] == ("uninstall_dev_tools", {"owner": "acme"})


@pytest.fixture
def staging_removals(monkeypatch):
    """Record each docker-staging removal cleanup asks for, as ``(host, project)``.

    Patched where it is defined: the step's own behaviour on a docker parent is
    pinned in tests/unit/docker/test_project_staging_cleanup.py. What this
    layer owes is asking it on every fleet host, for this project, after the
    dev tools.
    """
    from otto.docker import compose as compose_mod

    seen = []
    outcomes = {}

    async def _record(host, project):
        seen.append((host.id, project.name))
        host.calls.append(("remove_project_staging", {}))
        return outcomes.get(host.id, Result(Status.Success))

    monkeypatch.setattr(compose_mod, "remove_project_staging", _record)
    _record.seen = seen
    _record.outcomes = outcomes
    return _record


@pytest.mark.asyncio
async def test_cleanup_removes_the_project_s_docker_staging_on_every_host_last(staging_removals):
    ctx, hosts = _fake_ctx(n=2, dev_tools=[_FakeItem("probe", "acme")])
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert result.is_ok
    assert staging_removals.seen == [("h0", "acme"), ("h1", "acme")]
    for host in hosts:
        assert [verb for verb, _ in host.calls] == [
            "uninstall",
            "uninstall_dev_tools",
            "remove_project_staging",
        ]


@pytest.mark.asyncio
async def test_cleanup_reports_a_failed_staging_removal(staging_removals):
    ctx, _hosts = _fake_ctx(n=2)
    staging_removals.outcomes["h1"] = _fail("rm: cannot remove: Permission denied")
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert not result.is_ok
    assert "h1" in result.msg
    assert "Permission denied" in result.msg
    assert staging_removals.seen == [("h0", "acme"), ("h1", "acme")]


@pytest.mark.asyncio
async def test_cleanup_still_removes_the_staging_after_the_earlier_steps_failed(staging_removals):
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall", _fail("busy"))
    hosts[0].script("uninstall_dev_tools", _fail("locked"))
    result = await _actions(ctx).cleanup(CleanupOptions())
    assert "busy" in result.msg  # the FIRST failure is what is reported
    assert staging_removals.seen == [("h0", "acme")]


@pytest.fixture
def staging_survey(monkeypatch):
    """Script the one survey both ``cleanup`` and ``is_clean`` ask, and record removals.

    ``staging_to_remove`` and ``staging_keys_on`` are patched where they are
    defined, so the REAL ``remove_project_staging`` and the real ``is_clean``
    pass both read the same scripted survey: the same input through both entry
    points.
    """
    from otto.docker import compose as compose_mod
    from otto.docker import staging as staging_mod
    from otto.docker.compose import StagingKey, StagingSurvey

    # A removed tree: the command succeeded and printed nothing.
    removed = Result(Status.Success, value="")
    state = {"survey": StagingSurvey([], [], []), "removed": [], "rm": removed}

    async def _survey(host, project):
        del host, project
        return state["survey"]

    async def _cleanup_project(host, key, *, stack):
        del stack
        state["removed"].append((host.id, key))
        return state["rm"]

    monkeypatch.setattr(compose_mod, "staging_keys_on", lambda h, p: [StagingKey("s", True)])
    monkeypatch.setattr(compose_mod, "staging_to_remove", _survey)
    monkeypatch.setattr(staging_mod, "cleanup_project", _cleanup_project)

    def _script(remove=(), kept=(), unreadable=()):
        state["survey"] = StagingSurvey(
            [StagingKey(k, True) for k in remove], list(kept), list(unreadable)
        )

    _script.state = state
    return _script


@pytest.mark.asyncio
async def test_cleanup_and_is_clean_agree_on_a_staging_leftover(staging_survey):
    staging_survey(remove=["stack-a"])
    ctx, _hosts = _fake_ctx(n=1)
    actions = _actions(ctx)

    assert await actions.is_clean() is False
    result = await actions.cleanup(CleanupOptions())

    assert result.status is Status.Success, result.msg
    assert staging_survey.state["removed"] == [("h0", "stack-a")]


@pytest.mark.asyncio
async def test_cleanup_and_is_clean_agree_on_a_kept_live_stack(staging_survey):
    """A kept stack is clean (cleanup will not touch it) AND named in cleanup's result."""
    staging_survey(kept=["stack-a"])
    ctx, _hosts = _fake_ctx(n=1)
    actions = _actions(ctx)

    assert await actions.is_clean() is True
    result = await actions.cleanup(CleanupOptions())

    assert result.status is Status.Skipped
    assert "h0" in result.msg
    assert "stack-a" in result.msg
    assert staging_survey.state["removed"] == []


@pytest.mark.asyncio
async def test_cleanup_and_is_clean_agree_on_a_stack_nobody_could_list(staging_survey):
    staging_survey(unreadable=["stack-a"])
    ctx, _hosts = _fake_ctx(n=1)
    actions = _actions(ctx)

    with pytest.raises(OttoError, match="stack-a"):
        await actions.is_clean()
    result = await actions.cleanup(CleanupOptions())

    assert result.status is Status.Skipped
    assert "stack-a" in result.msg
    assert staging_survey.state["removed"] == []


@pytest.mark.asyncio
async def test_a_failed_staging_removal_outranks_a_declined_dev_tool_removal(staging_survey):
    """The ladder's other order: the decline comes first, the failure after it."""
    from otto.result import CommandResult

    staging_survey(remove=["stack-a"])
    staging_survey.state["rm"] = CommandResult(
        Status.Failed, value="rm: Permission denied", command="rm", retcode=1
    )
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall_dev_tools", Result(Status.Skipped, msg="h0: container not running"))

    result = await _actions(ctx).cleanup(CleanupOptions())

    assert not result.is_ok
    assert "Permission denied" in result.msg


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, False])
async def test_get_logs_reports_a_passed_over_host_whatever_the_requirement(required):
    """A decline in the haul is reported with or without ``require_product_logs``.

    The host verb opens its message with the host's id; the reduction names
    the host once.
    """
    ctx, hosts = _fake_ctx(n=1)
    hosts[0].script("get_product_logs", Result(Status.Skipped, msg="h0: container not running"))

    result = await _actions(ctx).get_logs(GetLogsOptions(require_product_logs=required))

    assert result.status is Status.Skipped
    assert result.msg == "host 'h0': container not running"


@pytest.mark.asyncio
async def test_a_failure_outranks_a_kept_stack_in_cleanup_s_result(staging_survey):
    """The decline ladder: a failed step is reported over a kept stack that comes after it."""
    staging_survey(kept=["stack-a"])
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall_dev_tools", _fail("locked"))

    result = await _actions(ctx).cleanup(CleanupOptions())

    assert not result.is_ok
    assert "locked" in result.msg


@pytest.mark.asyncio
async def test_cleanup_hands_a_project_override_an_instance_of_its_own_class():
    """``cleanup``'s product half honours a project's ``uninstall``, with the project's class.

    THE HEADLINE USE CASE IS THE TRIGGER: a project adds a flag to one of the
    six. ``cleanup``'s product half is ``self.uninstall(...)``, which resolves
    through the MRO to the project's body -- and used to hand it an instance of
    OTTO's class, so the first read of the project's own field raised
    ``AttributeError`` out of ``otto run cleanup`` and out of every
    ``ensure("clean")`` marker. The instance is rebuilt as the registered
    body's class now.

    Two claims, and the second is the one a name-matching rebuild would get
    wrong: the project's own ``soft`` takes its DEFAULT, and ``product_logs``
    ARRIVES -- ``cleanup``'s caller asked to skip the log haul and the
    override has to hear it, which it does because the field is shared by
    declaring class.

    Kills: handing over the base instance (``AttributeError`` on ``soft``),
    and building the project's class from defaults only (``product_logs`` comes
    back True).
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction

    seen = []

    @options
    class SoftUninstall(UninstallOptions):
        soft: Annotated[bool, typer.Option(help="Quiesce before teardown.")] = True

    with registering_project("softy"):

        @register_project_actions
        class Softy(ProjectActions):
            @instruction(options=SoftUninstall)
            async def uninstall(self, opts: SoftUninstall):
                seen.append((type(opts).__name__, opts.soft, opts.product_logs))
                return await super().uninstall(opts)

    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    result = await _actions(ctx, cls=Softy).cleanup(CleanupOptions(product_logs=False))

    assert result.is_ok
    assert seen == [("SoftUninstall", True, False)]
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": False, "get_debug_logs": False, "owner": "acme"}),
        ("uninstall_dev_tools", {"owner": "acme"}),
    ]


@pytest.mark.asyncio
async def test_cleanup_refuses_an_uninstall_override_with_a_required_field():
    """A field with NO default cannot be built from otto's class, so ``cleanup`` refuses.

    The one shape the rebuild cannot serve. ``cleanup`` holds a
    ``CleanupOptions``; a required field on the project's ``uninstall`` class was
    never asked for on ``otto run cleanup``'s command line and has no default
    to fall back on, so there is no value to pass and no way to invent one.

    REFUSED BY NAME, up front, rather than left to the constructor: a pydantic
    options class would raise ``ValidationError`` (rendered as a CLI
    ``BadParameter``) and a plain dataclass a bare ``TypeError``, and neither
    mentions ``cleanup`` -- which is the only place the project can fix it.
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.instruction_catalog import ProjectInstructionError

    ran = []

    @options(kw_only=True)
    class DrainUninstall(UninstallOptions):
        drain: Annotated[bool, typer.Option(help="Drain before teardown.")]

    with registering_project("drainer"):

        @register_project_actions
        class Drainer(ProjectActions):
            @instruction(options=DrainUninstall)
            async def uninstall(self, opts: DrainUninstall):
                ran.append(opts.drain)
                return await super().uninstall(opts)

    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    with pytest.raises(ProjectInstructionError) as caught:
        await _actions(ctx, cls=Drainer).cleanup(CleanupOptions())

    message = str(caught.value)
    assert "drain" in message
    assert "DrainUninstall" in message
    assert "cleanup" in message  # names the method the project must override too
    assert ran == []
    assert hosts[0].calls == []  # refused BEFORE the teardown started


@pytest.mark.asyncio
async def test_cleanup_over_an_unregistered_subclass_falls_back_to_the_base_options():
    """No registered body for this class means nothing better to build than otto's own.

    A subclass used WITHOUT ``@register_project_actions`` -- a test double, or
    a class driven directly -- has no body in the table, so ``body_for`` lands
    on otto's, whose options class is the one ``cleanup`` already holds. The
    fallback is what keeps such a class working rather than raising on a
    lookup that was never going to find anything.
    """
    seen = []

    class Unregistered(ProjectActions):
        async def uninstall(self, opts):
            seen.append(type(opts).__name__)
            return await super().uninstall(opts)

    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    result = await _actions(ctx, cls=Unregistered).cleanup(CleanupOptions(product_logs=False))

    assert result.is_ok
    assert seen == ["UninstallOptions"]
    assert hosts[0].calls[0][1]["get_product_logs"] is False


@pytest.mark.asyncio
async def test_dev_tool_walks_dispatch_through_the_host_instance():
    """A host CLASS that overrides either verb must be the one that runs.

    Kills: handing ``do_for_all_hosts`` an unbound ``BaseHost.<verb>``, which
    calls that body with no attribute lookup on the host -- freezing the walk
    to ``BaseHost`` and silently bypassing every registered host-class
    override, so ``otto host <id> …`` and ``otto run …`` would disagree while
    both reported success.
    """

    class _OverridingHost(_FakeHost):
        async def install_dev_tools(self, owner=None):
            self.calls.append(("overridden-install", {"owner": owner}))
            return Result(Status.Success)

        async def uninstall_dev_tools(self, owner=None):
            self.calls.append(("overridden-uninstall", {"owner": owner}))
            return Result(Status.Success)

    host = _OverridingHost("h0", dev_tools=[_FakeItem("probe", "acme")])
    actions = _actions(_FakeCtx([host]))
    assert (await actions.install_tools(InstallToolsOptions())).is_ok
    assert (await actions.cleanup(CleanupOptions())).is_ok
    assert ("overridden-install", {"owner": "acme"}) in host.calls
    assert ("overridden-uninstall", {"owner": "acme"}) in host.calls


@pytest.mark.asyncio
async def test_cleanup_twice_over_a_gone_docker_container_succeeds(tmp_path, monkeypatch):
    """A REAL docker_image product whose container is gone cleans up twice (issue #688).

    The earlier ``run uninstall`` removed the container, so ``docker logs`` and
    ``docker rm -f`` both answer "No such container". Kills: the product-log
    haul turning that answer into the failure ``otto run cleanup`` exits 2 on.
    """
    from pathlib import Path

    from otto.declared import DeclaredEntry
    from otto.host import product as product_mod
    from otto.host.local_host import LocalHost
    from otto.registry import resolved
    from otto.result import CommandResult

    monkeypatch.chdir(tmp_path)  # the product-log haul lands under the CWD
    gone = "Error response from daemon: No such container: edge"

    calls = []

    async def _exec(cmd, **_kw):
        calls.append(cmd)
        if "docker logs" in cmd or "docker rm -f" in cmd:
            return CommandResult(Status.Error, value=gone, command=cmd, retcode=1)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    host = LocalHost()
    host.exec = _exec
    entry = DeclaredEntry(
        name="edge",
        kind="docker_image",
        seam="products",
        owner="acme",
        base_dir=Path("/project"),
        match={},
        params={"image": "edge:1"},
    )
    edge = resolved(product_mod.PRODUCT_KINDS.get("docker_image").factory)(entry, host)
    edge.owner = "acme"  # what attaching stamps; the factory alone leaves it unset
    host.products = [edge]
    actions = _actions(_FakeCtx([host]))
    first = await actions.cleanup(CleanupOptions())
    second = await actions.cleanup(CleanupOptions())
    assert calls.count("docker logs edge 2>&1") == 2  # the haul really ran, both times
    assert first.status is Status.Success, first.msg
    assert second.status is Status.Success, second.msg


# ── tools ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_tools_dispatches_the_owner_scoped_dev_tool_install():
    # Kills: dropping the owner scope (this project's install_tools would place a
    # neighbour's tooling), and kills re-walking the tools here -- the walk's
    # order and stop-on-first-failure rule belong to the host verb, and a copy
    # at this layer drifts from it (it did, until the verb learned owner=).
    mine = _FakeItem("probe", "acme")
    theirs = _FakeItem("their-probe", "other")
    ctx, hosts = _fake_ctx(n=2, dev_tools=[mine, theirs])
    result = await _actions(ctx).install_tools(InstallToolsOptions())
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("install_dev_tools", {"owner": "acme"})]
    assert mine.calls == []
    assert theirs.calls == []


@pytest.mark.asyncio
async def test_install_tools_reports_a_failed_dev_tool_install_naming_the_host():
    # The walk's own rules -- stage before install, first failure stops it --
    # are the host verb's and are pinned in tests/unit/host/. What this layer
    # owes is reporting that failure WITH the host that produced it.
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("install_dev_tools", _fail("exec format error"))
    result = await _actions(ctx).install_tools(InstallToolsOptions())
    assert not result.is_ok
    assert "h0" in result.msg
    assert "exec format error" in result.msg


@pytest.mark.asyncio
async def test_install_tools_dev_false_touches_nothing():
    mine = _FakeItem("probe", "acme")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine])
    assert (await _actions(ctx).install_tools(InstallToolsOptions(dev=False))).is_ok
    assert hosts[0].calls == []
    assert mine.calls == []


@pytest.mark.asyncio
async def test_install_tools_toolchain_is_a_project_level_noop_by_design():
    # Toolchain artifacts are HOST-global (one toolchain, all owners), so the
    # orchestrator places them once; a project's actions own no part of that.
    # Pinned rather than left implicit: silently doing nothing must be the
    # DECLARED contract of this seam, not an oversight a reader has to guess at.
    mine = _FakeItem("probe", "acme")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine])
    result = await _actions(ctx).install_tools(InstallToolsOptions(dev=False, toolchain=True))
    assert result.is_ok
    assert hosts[0].calls == []
    assert mine.calls == []


@pytest.mark.asyncio
async def test_install_tools_toolchain_true_does_not_swallow_the_dev_walk():
    # THE COMBINATION is the case the two single-flag tests cannot reach.
    # `install_tools(dev=True, toolchain=True)` is what a subclass calling
    # `super().install_tools(**caller_flags)` forwards, and what the host and
    # orchestrator verbs' shared signature invites. Kills an implementation
    # that spells the toolchain no-op as an early return on `toolchain` ahead
    # of the dev walk, which reports success having installed NOTHING -- and
    # which passes both the dev-only test above (toolchain=False never reaches
    # the return) and the toolchain-only one (dev=False, so there was nothing
    # to install either way).
    mine = _FakeItem("probe", "acme")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine])
    result = await _actions(ctx).install_tools(InstallToolsOptions(dev=True, toolchain=True))
    assert result.is_ok
    assert hosts[0].calls == [("install_dev_tools", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_uninstall_tools_removes_only_this_projects_dev_tools_on_every_host():
    # Kills: dropping the owner scope (a neighbour's tooling removed), touching
    # products (uninstall-tools is the dev-tool half of cleanup only), and
    # re-walking the tools here instead of dispatching the host verb.
    mine = _FakeItem("probe", "acme")
    theirs = _FakeItem("their-probe", "other")
    app = _FakeItem("app", "acme")
    ctx, hosts = _fake_ctx(n=2, products=[app], dev_tools=[mine, theirs])
    result = await _actions(ctx).uninstall_tools(UninstallToolsOptions())
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("uninstall_dev_tools", {"owner": "acme"})]
    assert app.calls == mine.calls == theirs.calls == []


@pytest.mark.asyncio
async def test_uninstall_tools_is_best_effort_across_hosts_and_names_the_failing_one():
    ctx, hosts = _fake_ctx(n=2, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall_dev_tools", _fail("module is in use"))
    result = await _actions(ctx).uninstall_tools(UninstallToolsOptions())
    assert not result.is_ok
    assert "h0" in result.msg
    assert "module is in use" in result.msg
    assert hosts[1].calls == [("uninstall_dev_tools", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_uninstall_tools_dev_false_touches_nothing():
    mine = _FakeItem("probe", "acme")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine])
    result = await _actions(ctx).uninstall_tools(UninstallToolsOptions(dev=False, toolchain=True))
    assert result.is_ok
    assert hosts[0].calls == []
    assert mine.calls == []


# ── logs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_logs_dispatches_owner_scoped_product_haul():
    ctx, hosts = _fake_ctx(n=2)
    result = await _actions(ctx).get_logs(GetLogsOptions())
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("get_product_logs", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_get_logs_product_false_gathers_nothing():
    # There is no debug half here on purpose (host debug logs belong to no
    # project), so product=False leaves this action with nothing to do.
    ctx, hosts = _fake_ctx(n=1)
    assert (await _actions(ctx).get_logs(GetLogsOptions(product_logs=False))).is_ok
    assert hosts[0].calls == []


def _log_dir(base, name, host_id, *, delivered, product="app"):
    """A host's log root, with *product*'s log dir populated or left empty.

    Built through :mod:`otto.layout` rather than by hand, so this double holds
    the SAME run tree the host verb writes: the check under test reads
    ``logs/<host>/<product>/product/``, and a double that kept the old shared
    ``product/`` dir would pass a check that looks nowhere near it.
    """
    root = base / name
    logs = layout.product_logs_dir(root, host_id, product)
    logs.mkdir(parents=True)
    if delivered:
        (logs / "app.log").write_text("hi", encoding="utf-8")
    return layout.host_logs_dir(root, host_id)


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_fails_when_an_owning_host_retrieved_none(tmp_path):
    # Kills: parsing the requirement and never enforcing it — exit 0 having
    # promised logs nobody went looking for. BOTH hosts own products here, so
    # the requirement genuinely applies to both and the empty one is the fault.
    ctx = _FakeCtx(
        [
            _FakeHost(
                "h0",
                products=[_FakeItem("app", "acme")],
                log_dir=_log_dir(tmp_path, "full", "h0", delivered=True),
            ),
            _FakeHost(
                "h1",
                products=[_FakeItem("app", "acme")],
                log_dir=_log_dir(tmp_path, "empty", "h1", delivered=False),
            ),
        ]
    )
    result = await _actions(ctx).get_logs(GetLogsOptions(require_product_logs=True))
    assert not result.is_ok
    assert "h1" in result.msg
    assert "h0" not in result.msg  # the host that DID deliver is not accused


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_only_asks_hosts_this_project_owns(tmp_path):
    # Kills: demanding a haul from EVERY fleet host. A project whose products live
    # on part of the fleet (firmware on the embedded target, say) can retrieve
    # everything it owns and still be failed — named after an innocent host it
    # never deploys to — which makes the flag unusable for that whole project class.
    mine = _log_dir(tmp_path, "mine", "h0", delivered=True)
    owner_host = _FakeHost("h0", products=[_FakeItem("app", "acme")], log_dir=mine)
    bare_host = _FakeHost(
        "h1",
        products=[_FakeItem("their-app", "other")],
        log_dir=_log_dir(tmp_path, "bare", "h1", delivered=False, product="their-app"),
    )
    actions = _actions(_FakeCtx([owner_host, bare_host]))
    assert (await actions.get_logs(GetLogsOptions(require_product_logs=True))).is_ok

    # …and the OWNING host delivering nothing is still a failure that names it,
    # so the narrowed walk cannot degrade into no walk at all.
    (mine / "app" / "product" / "app.log").unlink()
    result = await actions.get_logs(GetLogsOptions(require_product_logs=True))
    assert not result.is_ok
    assert "h0" in result.msg


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_is_satisfied_by_a_haul(tmp_path):
    host = _FakeHost(
        "h0",
        products=[_FakeItem("app", "acme")],
        log_dir=_log_dir(tmp_path, "logs", "h0", delivered=True),
    )
    ctx = _FakeCtx([host])
    assert (await _actions(ctx).get_logs(GetLogsOptions(require_product_logs=True))).is_ok


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_with_product_false_is_refused():
    # A requirement that cannot be met is refused, not ignored: the haul it
    # requires is the step being skipped.
    ctx, hosts = _fake_ctx(n=1)
    result = await _actions(ctx).get_logs(
        GetLogsOptions(product_logs=False, require_product_logs=True)
    )
    assert not result.is_ok
    assert "require_product_logs" in result.msg
    assert hosts[0].calls == []


@pytest.mark.asyncio
async def test_get_logs_requirement_is_not_checked_after_a_failed_haul(tmp_path):
    # The haul's own failure is what returns — not a derived "no logs" verdict
    # that hides why nothing arrived. The host owns a product, so the require
    # walk WOULD have reached it had the haul succeeded.
    host = _FakeHost(
        "h0",
        products=[_FakeItem("app", "acme")],
        log_dir=_log_dir(tmp_path, "logs", "h0", delivered=False),
    )
    host.script("get_product_logs", _fail("transfer refused"))
    result = await _actions(_FakeCtx([host])).get_logs(GetLogsOptions(require_product_logs=True))
    assert not result.is_ok
    assert "transfer refused" in result.msg


# ── status / owns_products / is_clean ────────────────────────────────────


@pytest.mark.asyncio
async def test_status_tristate():
    # installed=2/2 products → INSTALLED; 1/2 → PARTIAL; 0/2 → UNINSTALLED.
    # Kills: deriving state from host-level is_installed booleans, which
    # cannot see a half-installed host (False == clean == half — the exact
    # ambiguity the tri-state exists to resolve).
    for installed_flags, expected in [
        ([True, True], InstallState.INSTALLED),
        ([True, False], InstallState.PARTIAL),
        ([False, False], InstallState.UNINSTALLED),
    ]:
        ctx, _ = _ctx_with_products("acme", installed_flags)
        state = await _actions(ctx).status(StatusOptions())
        assert state is expected, installed_flags


@pytest.mark.asyncio
async def test_status_is_partial_across_hosts_not_only_within_one():
    # One fully-installed host and one bare host is PARTIAL — a per-host
    # is_installed() reduction would call this INSTALLED-somewhere or clean.
    a = _FakeHost("h0", products=[_FakeItem("p", "acme", installed=True)])
    b = _FakeHost("h1", products=[_FakeItem("p", "acme", installed=False)])
    state = await _actions(_FakeCtx([a, b])).status(StatusOptions())
    assert state is InstallState.PARTIAL


@pytest.mark.asyncio
async def test_status_ignores_another_projects_products():
    # Kills: counting the whole fleet's products — another project's half-install
    # would drag this project to PARTIAL and its full install would fake ours.
    ctx, _ = _ctx_with_products("acme", [True, True], other_flags=[False, False])
    assert await _actions(ctx).status(StatusOptions()) is InstallState.INSTALLED


@pytest.mark.asyncio
async def test_status_with_no_owned_products_is_uninstalled():
    # Mirrors Host.is_installed's empty-products rule: nothing that could be
    # installed is not vacuously "installed".
    ctx, _ = _ctx_with_products("acme", [], other_flags=[True])
    assert await _actions(ctx).status(StatusOptions()) is InstallState.UNINSTALLED


@pytest.mark.asyncio
async def test_is_uninstalled_is_false_at_partial_not_only_at_installed():
    # THE BOUNDARY, and the reason this is not spelled `not is_installed()`: a
    # half-installed project is neither installed nor uninstalled, and a boolean
    # that answered True here would let a converge skip the teardown over
    # remnants still on the fleet.
    for flags, expected in [
        ([True, True], False),
        ([True, False], False),
        ([False, False], True),
    ]:
        ctx, _ = _ctx_with_products("acme", flags)
        assert await _actions(ctx).is_uninstalled() is expected, flags


@pytest.mark.asyncio
async def test_is_uninstalled_reads_status_rather_than_counting_again():
    # ONE AUTHORITY. A project whose install state comes from something otto
    # cannot see overrides status() and nothing else; a boolean built from its
    # own product walk would ignore that override entirely and answer for a
    # fleet the project has already said not to read.
    class _Opinionated(ProjectActions):
        async def status(self, opts):
            return InstallState.UNINSTALLED

    ctx, _ = _ctx_with_products("acme", [True, True])
    assert await _actions(ctx, cls=_Opinionated).is_uninstalled() is True


@pytest.mark.asyncio
async def test_is_uninstalled_hands_a_project_override_an_instance_of_its_own_class():
    """The probe honours a project's ``status``, with the project's own options class.

    The same seam as :meth:`cleanup`'s product half, and the same failure it
    had: ``self.status(StatusOptions())`` resolves through the MRO to the
    project's body, so handing over otto's instance raised ``AttributeError`` on
    the first field the project declared. The instance is rebuilt as the
    registered body's class now, and the project's own field takes its default --
    there is no ``--full`` equivalent for it to inherit, because
    ``is_uninstalled`` holds a bare ``StatusOptions``.

    Kills: handing over the base instance (``AttributeError`` on ``deep``).
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction

    seen = []

    @options
    class DeepStatus(StatusOptions):
        deep: Annotated[bool, typer.Option(help="Probe the device, not the file.")] = True

    with registering_project("prober"):

        @register_project_actions
        class Prober(ProjectActions):
            @instruction(options=DeepStatus)
            async def status(self, opts: DeepStatus):
                seen.append((type(opts).__name__, opts.deep))
                return InstallState.UNINSTALLED

    ctx, _ = _ctx_with_products("acme", [True, True])
    assert await _actions(ctx, cls=Prober).is_uninstalled() is True
    assert seen == [("DeepStatus", True)]


@pytest.mark.asyncio
async def test_is_uninstalled_refuses_a_status_override_with_a_required_field():
    """No default, nothing to build it from, so the probe refuses by name.

    ``is_uninstalled`` takes no arguments at all, so a required field on the
    project's ``status`` class has no possible source. Such a project overrides
    ``is_uninstalled`` itself -- which the message says.
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.instruction_catalog import ProjectInstructionError

    ran = []

    @options(kw_only=True)
    class DeepStatus(StatusOptions):
        deep: Annotated[bool, typer.Option(help="Probe the device, not the file.")]

    with registering_project("prober"):

        @register_project_actions
        class Prober(ProjectActions):
            @instruction(options=DeepStatus)
            async def status(self, opts: DeepStatus):
                ran.append(opts.deep)
                return InstallState.UNINSTALLED

    ctx, _ = _ctx_with_products("acme", [True, True])
    with pytest.raises(ProjectInstructionError) as caught:
        await _actions(ctx, cls=Prober).is_uninstalled()

    message = str(caught.value)
    assert "deep" in message
    assert "DeepStatus" in message
    assert "is_uninstalled" in message
    assert ran == []


def test_no_is_installed_boolean_on_project_actions():
    # DELIBERATE ASYMMETRY, and this is the note to whoever comes to "fix" it.
    # A host carries the is_installed/is_uninstalled pair because its answer is
    # per product; a project's is an aggregate over the fleet, and an aggregate is
    # where PARTIAL appears -- which False would bury alongside UNINSTALLED,
    # the exact ambiguity the tri-state status() exists to resolve.
    assert not hasattr(ProjectActions, "is_installed")


def test_owns_products_sees_only_this_projects_products():
    ctx, _ = _ctx_with_products("acme", [False], other_flags=[True])
    assert _actions(ctx).owns_products is True
    assert _actions(ctx, OTHER_REPO).owns_products is True
    assert _actions(ctx, fake_project("docs")).owns_products is False


def test_owns_products_is_false_for_an_empty_fleet():
    ctx, _ = _fake_ctx(n=0)
    assert _actions(ctx).owns_products is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_an_owned_product_is_installed():
    ctx, _ = _ctx_with_products("acme", [False, True])
    assert await _actions(ctx).is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_an_owned_dev_tool_is_installed():
    # Kills: an is_clean that only asks about products — a project's probe left on
    # the board is exactly what cleanup() removes and is_clean() must see.
    tool = _FakeItem("probe", "acme", installed=True)
    ctx, _ = _fake_ctx(n=1, dev_tools=[tool])
    assert await _actions(ctx).is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_ignores_another_projects_leftovers():
    host = _FakeHost(
        "h0",
        products=[_FakeItem("p", "other", installed=True)],
        dev_tools=[_FakeItem("probe", "other", installed=True)],
    )
    assert await _actions(_FakeCtx([host])).is_clean() is True


@pytest.mark.asyncio
async def test_is_clean_is_true_when_owned_products_and_tools_are_gone():
    host = _FakeHost(
        "h0",
        products=[_FakeItem("p", "acme", installed=False)],
        dev_tools=[_FakeItem("probe", "acme", installed=False)],
    )
    assert await _actions(_FakeCtx([host])).is_clean() is True


# ── registration ─────────────────────────────────────────────────────────


def test_register_project_actions_requires_init_import_context():
    # Kills: allowing ad-hoc registration with no attribution — the class
    # would be unkeyable and shadow every project.
    with pytest.raises(ValueError, match="init module"):
        register_project_actions(ProjectActions)


def test_register_twice_from_same_project_fails_loud():
    with registering_project("acme"):
        register_project_actions(ProjectActions)
        with pytest.raises(ValueError, match="acme"):
            register_project_actions(ProjectActions)


def test_two_projects_each_registering_is_the_intended_composition():
    class Mine(ProjectActions):
        pass

    class Theirs(ProjectActions):
        pass

    with registering_project("acme"):
        register_project_actions(Mine)
    with registering_project("other"):
        register_project_actions(Theirs)
    assert PROJECT_ACTIONS.get("acme").cls is Mine
    assert PROJECT_ACTIONS.get("other").cls is Theirs


def test_register_project_actions_returns_the_class_so_it_decorates():
    class Custom(ProjectActions):
        pass

    with registering_project("acme"):
        assert register_project_actions(Custom) is Custom


def test_registration_survives_a_repeat_run():
    # Twin of test_register_twice_from_same_project_fails_loud, and the reason
    # this file needs no isolation fixture of its own: if the root conftest's
    # _isolate_registries did not reach PROJECT_ACTIONS, the entry left by the
    # test above would make THIS registration the loud duplicate.
    with registering_project("acme"):
        register_project_actions(ProjectActions)
    assert PROJECT_ACTIONS.get("acme").cls is ProjectActions


def test_actions_for_prefers_registered_class_else_default():
    class Custom(ProjectActions):
        pass

    with registering_project("acme"):
        register_project_actions(Custom)
    ctx, _ = _fake_ctx(n=0)
    assert type(actions_for(PROJECT, ctx)) is Custom
    assert type(actions_for(fake_project("unregistered"), ctx)) is ProjectActions


def test_actions_for_hands_the_instance_a_view_bound_to_its_own_project():
    """THE ONE LINE that scopes a project's whole lifecycle (spec §7).

    Not ``actions.ctx is ctx`` any more, and the difference is the feature: an
    instance handed the plain context walks the union and stamps no owner —
    which, at the host layer, means *every* project's products. Nothing else in
    this file can catch that, because every test above builds its actions
    through the same view ``actions_for`` does.
    """
    ctx, _ = _fake_ctx(n=0)
    actions = actions_for(PROJECT, ctx)
    assert actions.project is PROJECT
    assert isinstance(actions.ctx, ProjectContextView)
    assert actions.ctx._project_name == PROJECT.name
    assert actions.ctx.hosts is ctx.hosts  # the SAME context underneath, not a copy


def test_hand_building_actions_around_a_plain_context_refuses():
    """The constructor's side door is shut, because the same spelled code turned destructive.

    ``ProjectActions(project, ctx)`` was legal AND owner-safe before the scope
    moved onto the view: the bodies passed ``owner=project.name`` themselves. Now
    they do not, so a hand-built instance over a plain context would walk the
    ambient union with ``owner=None`` — which the host layer reads as every
    owner's products, making its ``cleanup()`` a silent teardown of the
    neighbours. A docstring cannot be the only thing standing in front of that.
    """
    ctx = OttoContext(lab=Lab(name="t"))

    with pytest.raises(TypeError) as excinfo:
        ProjectActions(project=PROJECT, ctx=ctx)
    message = str(excinfo.value)
    assert "actions_for" in message  # the constructor to use instead
    assert "acme" in message
    assert "owner=None" in message  # WHY it is refused, not just that it is
    # The remedy names the real API, so following it works.
    assert "actions_for(project, ctx)" in message
    assert "ctx.for_project(project.name)" in message
    assert "for_repo" not in message

    # The two constructions that ARE the seam are untouched: the real context's
    # own view, and a double that borrows `for_project` to build one.
    assert isinstance(actions_for(PROJECT, ctx).ctx, ProjectContextView)
    fake, _ = _fake_ctx(n=0)
    assert isinstance(actions_for(PROJECT, fake).ctx, ProjectContextView)


# ── state vocabulary ─────────────────────────────────────────────────────


def test_a_cleanliness_row_is_unknown_exactly_when_it_carries_an_error():
    # THE INVARIANT `_verdict` LEANS ON. It raises the first UNKNOWN row's own
    # error with no arm for "unreadable, and yet nothing to raise" -- because
    # that shape cannot be built. The mirror half matters just as much: an
    # error on a row that DID answer is a measurement contradicting itself.
    for state, error in [
        (Cleanliness.UNKNOWN, None),
        (Cleanliness.CLEAN, RuntimeError("h9 never answered")),
        (Cleanliness.DIRTY, RuntimeError("h9 never answered")),
    ]:
        with pytest.raises(ValueError, match="UNKNOWN"):
            CleanlinessItem(kind=CleanlinessKind.PROJECT, name="acme", state=state, error=error)


def test_the_cleanliness_aggregate_keeps_a_dirty_row_over_an_unreadable_one():
    # An answer in hand is never discarded for a scan that fell short: once
    # something has been SEEN, the lab needs cleaning, and the host nobody
    # reached cannot make it clean again.
    dirty = CleanlinessItem(kind=CleanlinessKind.TUNNEL, name="h0-h1", state=Cleanliness.DIRTY)
    unknown = CleanlinessItem(
        kind=CleanlinessKind.TOOLCHAIN,
        name="h9",
        state=Cleanliness.UNKNOWN,
        error=RuntimeError("h9 never answered"),
    )
    assert CleanlinessReport([unknown, dirty]).overall is Cleanliness.DIRTY
    assert CleanlinessReport([dirty, unknown]).overall is Cleanliness.DIRTY
    assert CleanlinessReport([unknown]).overall is Cleanliness.UNKNOWN
    # A lab with nothing that could be left over has nothing left over.
    assert CleanlinessReport().overall is Cleanliness.CLEAN


def test_project_status_defaults_to_an_empty_per_project_map():
    status = ProjectStatus(overall=InstallState.UNINSTALLED)
    assert status.projects == {}
    assert ProjectStatus(
        overall=InstallState.PARTIAL, projects={"acme": InstallState.PARTIAL}
    ).projects


# ── project-instruction body registration ───────────────────────────────


class TestBodyRegistration:
    """Otto's six are declared when the module loads; registering a subclass adds its bodies."""

    def test_the_base_class_registered_all_six(self) -> None:
        from otto.instruction_catalog import PROJECT_INSTRUCTIONS

        names = {"install", "uninstall", "cleanup", "get-logs", "install-tools", "status"}
        assert names <= set(PROJECT_INSTRUCTIONS.names())
        for name in names:
            body = PROJECT_INSTRUCTIONS.get(name).body_for(ProjectActions)
            assert body is not None
            assert body.project is None

    def test_a_fresh_process_declares_the_six_at_import(self) -> None:
        """Pins ``declare()`` at import: importing the module is all the six need.

        A fresh subprocess import is how production meets the tables -- nothing
        else in this process has touched them -- so it is the one witness that
        the import alone declares otto's six, with no other call.

        A subprocess rather than ``importlib.reload``: reloading this module
        in-process rebinds its ``ProjectActions`` to a NEW class object that
        ``otto.project``'s own re-export (bound once, at package-import time)
        would never see again, splitting the class's identity for the rest of
        the test session. A fresh interpreter has no such history to corrupt.
        """
        import subprocess
        import sys

        script = (
            "import otto.project.actions as mod\n"
            "from otto.instruction_catalog import PROJECT_INSTRUCTIONS\n"
            "names = ['install', 'uninstall', 'cleanup', 'get-logs', 'install-tools', 'status']\n"
            "ok = all(\n"
            "    (b := PROJECT_INSTRUCTIONS.get(n).body_for(mod.ProjectActions)) is not None\n"
            "    and b.owner_class is mod.ProjectActions\n"
            "    and b.project is None\n"
            "    for n in names\n"
            ")\n"
            "print('OK' if ok else 'FAIL')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert result.stdout.strip() == "OK", result.stderr

    def test_a_registered_subclass_adds_its_override_and_its_new_instruction(self) -> None:
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.instruction_catalog import PROJECT_INSTRUCTIONS
        from otto.project import InstallOptions
        from otto.registry import registering_project

        @options
        class WidgetInstall(InstallOptions):
            variant: Annotated[str, typer.Option(help="v")] = "field"

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with registering_project("widget"):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction(options=WidgetInstall)
                async def install(self, opts: WidgetInstall):
                    return await super().install(opts)

                @instruction(options=DeployOpts, walk="reverse")
                async def deploy(self, opts: DeployOpts):
                    return Result(Status.Success)

        install = PROJECT_INSTRUCTIONS.get("install")
        assert install.body_for(Widget).options_cls is WidgetInstall
        assert install.body_for(Widget).project == "widget"
        deploy = PROJECT_INSTRUCTIONS.get("deploy")
        assert deploy.spec.walk == "reverse"
        assert deploy.spec.declared_by == "widget"

    def test_an_override_with_foreign_options_is_refused_at_registration(self) -> None:
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.instruction_catalog import ProjectInstructionError
        from otto.registry import registering_project

        @options
        class Foreign:
            ensure: Annotated[bool, typer.Option(help="e")] = False

        with (
            registering_project("widget"),
            pytest.raises(ProjectInstructionError, match="InstallOptions"),
        ):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction(options=Foreign)
                async def install(self, opts: Foreign):
                    return Result(Status.Success)

    def test_two_methods_on_one_class_claiming_one_name_are_refused(self) -> None:
        """A class holds ONE body per name, so the second declaration cannot be silent.

        The idempotence check that lets this module be re-imported also made a
        duplicate invisible: the second marked method matched the name already
        registered for the class and was skipped, so a typo'd
        ``@instruction("deploy")`` on a second method simply never ran. The
        refusal names the class, the name and BOTH methods, because "declared
        twice" without the second site is a grep the reader has to run.
        """
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.instruction_catalog import ProjectInstructionError
        from otto.registry import registering_project

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with (
            registering_project("widget"),
            pytest.raises(ProjectInstructionError, match=r"'deploy' twice") as caught,
        ):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy(self, opts: DeployOpts):
                    return Result(Status.Success)

                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy_again(self, opts: DeployOpts):
                    return Result(Status.Success)

        message = str(caught.value)
        assert "Widget" in message
        assert "deploy()" in message
        assert "deploy_again()" in message

    def test_a_duplicate_is_refused_before_anything_is_registered(self) -> None:
        """The scan is a pass of its own, so a refused class leaves no half-published name."""
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.instruction_catalog import PROJECT_INSTRUCTIONS, ProjectInstructionError
        from otto.registry import registering_project

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with registering_project("widget"), pytest.raises(ProjectInstructionError):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy(self, opts: DeployOpts):
                    return Result(Status.Success)

                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy_again(self, opts: DeployOpts):
                    return Result(Status.Success)

        assert "deploy" not in set(PROJECT_INSTRUCTIONS.names())


# ── a container that is not running (#676) ────────────────────────────────────


class _StoppedContainer(_FakeHost):
    """A container host whose stack is down: it answers `is_running` False."""

    async def is_running(self):
        return False


def _stopped_container_ctx():
    product = _FakeItem("app", PROJECT.name, installed=True)
    tool = _FakeItem("probe", PROJECT.name, installed=True)
    stopped = _StoppedContainer("h0.stack.app", products=[product], dev_tools=[tool])
    return _FakeCtx([stopped]), stopped, product, tool


@pytest.mark.asyncio
async def test_status_counts_a_stopped_container_s_products_as_not_installed():
    """Before #676 a product on a stopped stack read as installed: the container was
    never asked. Asking a stopped one would raise, so its products are not installed."""
    ctx, _, product, _ = _stopped_container_ctx()
    state = await _actions(ctx).status(StatusOptions())
    assert state is InstallState.UNINSTALLED
    assert product.calls == []


@pytest.mark.asyncio
async def test_a_stopped_container_is_clean():
    ctx, _, product, tool = _stopped_container_ctx()
    assert await _actions(ctx).is_clean()
    assert product.calls == tool.calls == []


@pytest.mark.asyncio
async def test_uninstall_and_cleanup_pass_over_a_stopped_container(caplog):
    ctx, stopped, _, _ = _stopped_container_ctx()
    actions = _actions(ctx)
    assert (await actions.uninstall(UninstallOptions())).is_ok
    assert (await actions.cleanup(CleanupOptions())).is_ok
    assert stopped.calls == []
    assert any("not running" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_install_on_a_stopped_container_is_still_the_host_s_to_refuse():
    """No auto-start: install reaches the host verb, which fails fast naming compose up."""
    ctx, stopped, _, _ = _stopped_container_ctx()
    await _actions(ctx).install(InstallOptions())
    assert [verb for verb, _ in stopped.calls] == ["install"]


@pytest.mark.asyncio
async def test_a_container_carrying_nothing_of_this_project_s_is_never_probed():
    """Probing costs a docker call per container; one with nothing of ours has no answer we need."""

    class _Probed(_FakeHost):
        probes = 0

        async def is_running(self):
            type(self).probes += 1
            return False

    other = _Probed("h0.stack.db", products=[_FakeItem("db", "someone-else", installed=True)])
    actions = _actions(_FakeCtx([other]))
    await actions.status(StatusOptions())
    await actions.is_clean()
    assert _Probed.probes == 0


@pytest.mark.asyncio
async def test_require_product_logs_names_a_stopped_container_as_the_reason(tmp_path):
    """A stopped container delivers no logs because it was passed over, not because
    its products wrote none: the requirement's failure says which, and how to start it."""
    stopped = _StoppedContainer(
        "h0.stack.app",
        products=[_FakeItem("app", PROJECT.name)],
        log_dir=_log_dir(tmp_path, "stopped", "h0.stack.app", delivered=False),
    )
    result = await _actions(_FakeCtx([stopped])).get_logs(GetLogsOptions(require_product_logs=True))
    assert not result.is_ok
    assert "h0.stack.app" in result.msg
    assert "not running" in result.msg
    assert "otto docker compose up" in result.msg
    assert stopped.calls == []
