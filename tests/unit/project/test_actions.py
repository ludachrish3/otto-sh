"""Per-repo ``ProjectActions`` — owner-scoped defaults, tri-state status, registration.

NO LOCAL REGISTRY-ISOLATION FIXTURE HERE, deliberately. ``PROJECT_ACTIONS`` is
an ``otto.registry.Registry``, and the root conftest's autouse
``_isolate_registries`` discovers every ``Registry`` reachable from a loaded
``otto.*`` module dynamically — this one included, from the import above. The
provider seams (``_PRODUCT_PROVIDERS``/``_DEV_TOOL_PROVIDERS``) carry their own
``_isolate_provider_registry`` fixtures only because they are plain lists that
the root guard cannot see. Pinned by ``test_registration_survives_a_repeat_run``
below, which registers the same repo name a second time in the same process.
"""

from types import SimpleNamespace

import pytest

from otto import layout
from otto.config.lab import Lab
from otto.context import OttoContext, ProjectContextView
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
    actions_for,
    register_project_actions,
)
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status

REPO = SimpleNamespace(name="acme")
OTHER_REPO = SimpleNamespace(name="other")


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
    around ``ctx.for_repo(name)``, so what these tests exercise is the REAL
    :class:`~otto.context.ProjectContextView` sitting on top of this. The owner
    every host verb below records therefore comes from production code, not
    from a double reimplementing the injection it is meant to certify.

    ``_scope_owner`` is the view's other half — the universe binding — and is
    recorded rather than honoured (this double holds a list, not a lab, so
    there is nothing here to narrow). Recording it is what lets a test assert
    the project layer's walks are BOUND as well as stamped.
    """

    # The real seam, applied to the double: `for_repo` only wraps, so borrowing
    # OttoContext's own method keeps a second copy of the wiring out of the
    # tests that exist to certify it.
    for_repo = OttoContext.for_repo

    def __init__(self, hosts):
        self.hosts = list(hosts)
        self.scope_owners = []

    def all_hosts(self, _scope_owner=None):
        self.scope_owners.append(_scope_owner)
        return iter(self.hosts)

    async def do_for_all_hosts(self, method, *args, _scope_owner=None, **kwargs):
        """Apply *method* per host EXACTLY as the production seam does.

        The call shape is ``method(host, ...)`` -- the function object it was
        handed, applied to the host, with NO name lookup (``otto/context.py``'s
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


def _actions(ctx, repo=REPO, cls=ProjectActions):
    """Build actions the way production does — around ``ctx.for_repo(repo.name)``.

    ``actions_for`` is the only constructor a user's repo ever reaches, and the
    repo-scoped view it supplies is what carries the owner scope now that no
    ``ProjectActions`` body spells ``owner=``. Constructing around the plain
    double instead would exercise an unscoped instance nothing produces, and
    every ``{"owner": "acme"}`` assertion below would be measuring the absence
    of a seam rather than the seam.
    """
    return cls(repo=repo, ctx=ctx.for_repo(repo.name))


def _ctx_with_products(owner, installed_flags, other_flags=()):
    """One host carrying *owner*'s products plus another repo's, per flag list."""
    products = [_FakeItem(f"p{i}", owner, installed=f) for i, f in enumerate(installed_flags)]
    products += [_FakeItem(f"o{i}", "other", installed=f) for i, f in enumerate(other_flags)]
    host = _FakeHost("h0", products=products)
    return _FakeCtx([host]), host


# ── install ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_install_dispatches_owner_scoped_to_all_hosts():
    # Kills: forgetting the owner filter — repo A's actions would install
    # repo B's products (the exact cross-repo bleed the design forbids).
    ctx, hosts = _fake_ctx(n=2)
    result = await _actions(ctx).install(InstallOptions())
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("install", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_every_project_walk_is_bound_to_its_own_repos_universe():
    """The view's OTHER half: this layer's walks name their repo as well as stamp it.

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
    # THE spec §5 rule at the repo layer: host debug logs belong to no repo,
    # so a per-repo uninstall must never gather them (N repos would each sweep,
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
    # the owner scope (removing another repo's tools), or that removes tools
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


@pytest.mark.asyncio
async def test_cleanup_hands_a_repo_override_an_instance_of_its_own_class():
    """``cleanup``'s product half honours a repo's ``uninstall``, with the repo's class.

    THE HEADLINE USE CASE IS THE TRIGGER: a repo adds a flag to one of the
    six. ``cleanup``'s product half is ``self.uninstall(...)``, which resolves
    through the MRO to the repo's body -- and used to hand it an instance of
    OTTO's class, so the first read of the repo's own field raised
    ``AttributeError`` out of ``otto run cleanup`` and out of every
    ``ensure("clean")`` marker. The instance is rebuilt as the registered
    body's class now.

    Two claims, and the second is the one a name-matching rebuild would get
    wrong: the repo's own ``soft`` takes its DEFAULT, and ``product_logs``
    ARRIVES -- ``cleanup``'s caller asked to skip the log haul and the
    override has to hear it, which it does because the field is shared by
    declaring class.

    Kills: handing over the base instance (``AttributeError`` on ``soft``),
    and building the repo's class from defaults only (``product_logs`` comes
    back True).
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.project import actions as mod

    mod.register_project_instruction_bodies(ProjectActions, None)
    seen = []

    @options
    class SoftUninstall(UninstallOptions):
        soft: Annotated[bool, typer.Option(help="Quiesce before teardown.")] = True

    with registering_repo("softy"):

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
    ``CleanupOptions``; a required field on the repo's ``uninstall`` class was
    never asked for on ``otto run cleanup``'s command line and has no default
    to fall back on, so there is no value to pass and no way to invent one.

    REFUSED BY NAME, up front, rather than left to the constructor: a pydantic
    options class would raise ``ValidationError`` (rendered as a CLI
    ``BadParameter``) and a plain dataclass a bare ``TypeError``, and neither
    mentions ``cleanup`` -- which is the only place the repo can fix it.
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.instructions import ProjectInstructionError
    from otto.project import actions as mod

    mod.register_project_instruction_bodies(ProjectActions, None)
    ran = []

    @options(kw_only=True)
    class DrainUninstall(UninstallOptions):
        drain: Annotated[bool, typer.Option(help="Drain before teardown.")]

    with registering_repo("drainer"):

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
    assert "cleanup" in message  # names the method the repo must override too
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


# ── tools ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_tools_dispatches_the_owner_scoped_dev_tool_install():
    # Kills: dropping the owner scope (this repo's install_tools would place a
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
async def test_install_tools_toolchain_is_a_repo_level_noop_by_design():
    # Toolchain artifacts are HOST-global (one toolchain, all owners), so the
    # orchestrator places them once; a repo's actions own no part of that.
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
    # repo), so product=False leaves this action with nothing to do.
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
async def test_get_logs_require_product_logs_only_asks_hosts_this_repo_owns(tmp_path):
    # Kills: demanding a haul from EVERY fleet host. A repo whose products live
    # on part of the fleet (firmware on the embedded target, say) can retrieve
    # everything it owns and still be failed — named after an innocent host it
    # never deploys to — which makes the flag unusable for that whole repo class.
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
async def test_status_ignores_another_repos_products():
    # Kills: counting the whole fleet's products — another repo's half-install
    # would drag this repo to PARTIAL and its full install would fake ours.
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
    # half-installed repo is neither installed nor uninstalled, and a boolean
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
    # ONE AUTHORITY. A repo whose install state comes from something otto
    # cannot see overrides status() and nothing else; a boolean built from its
    # own product walk would ignore that override entirely and answer for a
    # fleet the repo has already said not to read.
    class _Opinionated(ProjectActions):
        async def status(self, opts):
            return InstallState.UNINSTALLED

    ctx, _ = _ctx_with_products("acme", [True, True])
    assert await _actions(ctx, cls=_Opinionated).is_uninstalled() is True


@pytest.mark.asyncio
async def test_is_uninstalled_hands_a_repo_override_an_instance_of_its_own_class():
    """The probe honours a repo's ``status``, with the repo's own options class.

    The same seam as :meth:`cleanup`'s product half, and the same failure it
    had: ``self.status(StatusOptions())`` resolves through the MRO to the
    repo's body, so handing over otto's instance raised ``AttributeError`` on
    the first field the repo declared. The instance is rebuilt as the
    registered body's class now, and the repo's own field takes its default --
    there is no ``--full`` equivalent for it to inherit, because
    ``is_uninstalled`` holds a bare ``StatusOptions``.

    Kills: handing over the base instance (``AttributeError`` on ``deep``).
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.project import actions as mod

    mod.register_project_instruction_bodies(ProjectActions, None)
    seen = []

    @options
    class DeepStatus(StatusOptions):
        deep: Annotated[bool, typer.Option(help="Probe the device, not the file.")] = True

    with registering_repo("prober"):

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
    repo's ``status`` class has no possible source. Such a repo overrides
    ``is_uninstalled`` itself -- which the message says.
    """
    from typing import Annotated

    import typer

    from otto import options
    from otto.cli.run import instruction
    from otto.instructions import ProjectInstructionError
    from otto.project import actions as mod

    mod.register_project_instruction_bodies(ProjectActions, None)
    ran = []

    @options(kw_only=True)
    class DeepStatus(StatusOptions):
        deep: Annotated[bool, typer.Option(help="Probe the device, not the file.")]

    with registering_repo("prober"):

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
    # per product; a repo's is an aggregate over the fleet, and an aggregate is
    # where PARTIAL appears -- which False would bury alongside UNINSTALLED,
    # the exact ambiguity the tri-state status() exists to resolve.
    assert not hasattr(ProjectActions, "is_installed")


def test_owns_products_sees_only_this_repos_products():
    ctx, _ = _ctx_with_products("acme", [False], other_flags=[True])
    assert _actions(ctx).owns_products is True
    assert _actions(ctx, OTHER_REPO).owns_products is True
    assert _actions(ctx, SimpleNamespace(name="docs")).owns_products is False


def test_owns_products_is_false_for_an_empty_fleet():
    ctx, _ = _fake_ctx(n=0)
    assert _actions(ctx).owns_products is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_an_owned_product_is_installed():
    ctx, _ = _ctx_with_products("acme", [False, True])
    assert await _actions(ctx).is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_an_owned_dev_tool_is_installed():
    # Kills: an is_clean that only asks about products — a repo's probe left on
    # the board is exactly what cleanup() removes and is_clean() must see.
    tool = _FakeItem("probe", "acme", installed=True)
    ctx, _ = _fake_ctx(n=1, dev_tools=[tool])
    assert await _actions(ctx).is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_ignores_another_repos_leftovers():
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
    # would be unkeyable and shadow every repo.
    with pytest.raises(ValueError, match="init module"):
        register_project_actions(ProjectActions)


def test_register_twice_from_same_repo_fails_loud():
    with registering_repo("acme"):
        register_project_actions(ProjectActions)
        with pytest.raises(ValueError, match="acme"):
            register_project_actions(ProjectActions)


def test_two_repos_each_registering_is_the_intended_composition():
    class Mine(ProjectActions):
        pass

    class Theirs(ProjectActions):
        pass

    with registering_repo("acme"):
        register_project_actions(Mine)
    with registering_repo("other"):
        register_project_actions(Theirs)
    assert PROJECT_ACTIONS.get("acme") is Mine
    assert PROJECT_ACTIONS.get("other") is Theirs


def test_register_project_actions_returns_the_class_so_it_decorates():
    class Custom(ProjectActions):
        pass

    with registering_repo("acme"):
        assert register_project_actions(Custom) is Custom


def test_registration_survives_a_repeat_run():
    # Twin of test_register_twice_from_same_repo_fails_loud, and the reason
    # this file needs no isolation fixture of its own: if the root conftest's
    # _isolate_registries did not reach PROJECT_ACTIONS, the entry left by the
    # test above would make THIS registration the loud duplicate.
    with registering_repo("acme"):
        register_project_actions(ProjectActions)
    assert PROJECT_ACTIONS.get("acme") is ProjectActions


def test_actions_for_prefers_registered_class_else_default():
    class Custom(ProjectActions):
        pass

    with registering_repo("acme"):
        register_project_actions(Custom)
    ctx, _ = _fake_ctx(n=0)
    assert type(actions_for(REPO, ctx)) is Custom
    assert type(actions_for(SimpleNamespace(name="unregistered"), ctx)) is ProjectActions


def test_actions_for_hands_the_instance_a_view_bound_to_its_own_repo():
    """THE ONE LINE that scopes a repo's whole lifecycle (spec §7).

    Not ``actions.ctx is ctx`` any more, and the difference is the feature: an
    instance handed the plain context walks the union and stamps no owner —
    which, at the host layer, means *every* repo's products. Nothing else in
    this file can catch that, because every test above builds its actions
    through the same view ``actions_for`` does.
    """
    ctx, _ = _fake_ctx(n=0)
    actions = actions_for(REPO, ctx)
    assert actions.repo is REPO
    assert isinstance(actions.ctx, ProjectContextView)
    assert actions.ctx._repo_name == REPO.name
    assert actions.ctx.hosts is ctx.hosts  # the SAME context underneath, not a copy


def test_hand_building_actions_around_a_plain_context_refuses():
    """The constructor's side door is shut, because the same spelled code turned destructive.

    ``ProjectActions(repo, ctx)`` was legal AND owner-safe before the scope
    moved onto the view: the bodies passed ``owner=repo.name`` themselves. Now
    they do not, so a hand-built instance over a plain context would walk the
    ambient union with ``owner=None`` — which the host layer reads as every
    owner's products, making its ``cleanup()`` a silent teardown of the
    neighbours. A docstring cannot be the only thing standing in front of that.
    """
    ctx = OttoContext(lab=Lab(name="t"))

    with pytest.raises(TypeError) as excinfo:
        ProjectActions(repo=REPO, ctx=ctx)
    message = str(excinfo.value)
    assert "actions_for" in message  # the constructor to use instead
    assert "acme" in message
    assert "owner=None" in message  # WHY it is refused, not just that it is

    # The two constructions that ARE the seam are untouched: the real context's
    # own view, and a double that borrows `for_repo` to build one.
    assert isinstance(actions_for(REPO, ctx).ctx, ProjectContextView)
    fake, _ = _fake_ctx(n=0)
    assert isinstance(actions_for(REPO, fake).ctx, ProjectContextView)


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
            CleanlinessItem(kind=CleanlinessKind.REPO, name="acme", state=state, error=error)


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


def test_project_status_defaults_to_an_empty_per_repo_map():
    status = ProjectStatus(overall=InstallState.UNINSTALLED)
    assert status.repos == {}
    assert ProjectStatus(overall=InstallState.PARTIAL, repos={"acme": InstallState.PARTIAL}).repos


# ── project-instruction body registration ───────────────────────────────


class TestBodyRegistration:
    """Importing the module registers otto's six; registering a subclass adds its bodies."""

    def test_the_base_class_registered_all_six(self) -> None:
        from otto.instructions import PROJECT_INSTRUCTIONS
        from otto.project import actions as mod

        # isolation may have rolled the table back
        mod.register_project_instruction_bodies(ProjectActions, None)
        names = {"install", "uninstall", "cleanup", "get-logs", "install-tools", "status"}
        assert names <= set(PROJECT_INSTRUCTIONS.names())
        for name in names:
            body = PROJECT_INSTRUCTIONS.get(name).body_for(ProjectActions)
            assert body is not None
            assert body.repo is None

    def test_import_time_call_is_what_actually_registers_the_six(self) -> None:
        """Pins the MODULE-LEVEL ``register_project_instruction_bodies(...)`` call.

        Every other test in this class calls ``register_project_instruction_bodies``
        itself before asserting, which would still pass with that module-level
        line deleted. A fresh subprocess import exercises the line the way
        production does -- nothing else in this process has already registered
        otto's six -- so a subprocess that never calls the function directly is
        the one witness that the import alone did the registering.

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
            "from otto.instructions import PROJECT_INSTRUCTIONS\n"
            "names = ['install', 'uninstall', 'cleanup', 'get-logs', 'install-tools', 'status']\n"
            "ok = all(\n"
            "    (b := PROJECT_INSTRUCTIONS.get(n).body_for(mod.ProjectActions)) is not None\n"
            "    and b.owner_class is mod.ProjectActions\n"
            "    and b.repo is None\n"
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
        from otto.instructions import PROJECT_INSTRUCTIONS
        from otto.project import InstallOptions
        from otto.project import actions as mod
        from otto.registry import registering_repo

        mod.register_project_instruction_bodies(ProjectActions, None)

        @options
        class WidgetInstall(InstallOptions):
            variant: Annotated[str, typer.Option(help="v")] = "field"

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with registering_repo("widget"):

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
        assert install.body_for(Widget).repo == "widget"
        deploy = PROJECT_INSTRUCTIONS.get("deploy")
        assert deploy.spec.walk == "reverse"
        assert deploy.spec.declared_by == "widget"

    def test_an_override_with_foreign_options_is_refused_at_registration(self) -> None:
        from typing import Annotated

        import typer

        from otto import options
        from otto.cli.run import instruction
        from otto.instructions import ProjectInstructionError
        from otto.project import actions as mod
        from otto.registry import registering_repo

        mod.register_project_instruction_bodies(ProjectActions, None)

        @options
        class Foreign:
            ensure: Annotated[bool, typer.Option(help="e")] = False

        with (
            registering_repo("widget"),
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
        from otto.instructions import ProjectInstructionError
        from otto.project import actions as mod
        from otto.registry import registering_repo

        mod.register_project_instruction_bodies(ProjectActions, None)

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with (
            registering_repo("widget"),
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
        from otto.instructions import PROJECT_INSTRUCTIONS, ProjectInstructionError
        from otto.project import actions as mod
        from otto.registry import registering_repo

        mod.register_project_instruction_bodies(ProjectActions, None)

        @options
        class DeployOpts:
            dry: Annotated[bool, typer.Option(help="d")] = False

        with registering_repo("widget"), pytest.raises(ProjectInstructionError):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy(self, opts: DeployOpts):
                    return Result(Status.Success)

                @instruction("deploy", options=DeployOpts, walk="forward")
                async def deploy_again(self, opts: DeployOpts):
                    return Result(Status.Success)

        assert "deploy" not in set(PROJECT_INSTRUCTIONS.names())
