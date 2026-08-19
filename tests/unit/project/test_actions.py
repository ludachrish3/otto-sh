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

from otto.config.lab import Lab
from otto.context import OttoContext, ProjectContextView
from otto.project import (
    PROJECT_ACTIONS,
    Cleanliness,
    CleanlinessItem,
    CleanlinessKind,
    CleanlinessReport,
    InstallState,
    ProjectActions,
    ProjectStatus,
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
    result = await _actions(ctx).install()
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

    assert (await actions.install()).is_ok
    assert actions.owns_products is False
    assert ctx.scope_owners == ["acme", "acme"]


@pytest.mark.asyncio
async def test_default_install_reduces_first_host_failure():
    ctx, hosts = _fake_ctx(n=2)
    hosts[1].script("install", _fail("no space"))
    result = await _actions(ctx).install()
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
    result = await _actions(ctx).install()
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
    await _actions(ctx).uninstall()
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": True, "get_debug_logs": False, "owner": "acme"}),
    ]


@pytest.mark.asyncio
async def test_uninstall_forwards_get_product_logs_false():
    ctx, hosts = _fake_ctx(n=1)
    await _actions(ctx).uninstall(get_product_logs=False)
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
    result = await _actions(ctx).cleanup()
    assert result.is_ok
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": True, "get_debug_logs": False, "owner": "acme"}),
        ("uninstall_dev_tools", {"owner": "acme"}),
    ]
    assert mine.calls == []
    assert theirs.calls == []


@pytest.mark.asyncio
async def test_cleanup_forwards_get_product_logs_to_the_uninstall_half():
    # Kills: `await self.uninstall()` with the flag dropped on the floor.
    # `cleanup` is `uninstall` plus the tooling, so a caller that asked to skip
    # the log haul asked cleanup's uninstall half to skip it too — and hard-
    # wiring True passes every other cleanup test in this file, all of which
    # take the default.
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    result = await _actions(ctx).cleanup(get_product_logs=False)
    assert result.is_ok
    assert hosts[0].calls == [
        ("uninstall", {"get_product_logs": False, "get_debug_logs": False, "owner": "acme"}),
        ("uninstall_dev_tools", {"owner": "acme"}),
    ]


@pytest.mark.asyncio
async def test_cleanup_reports_a_failed_dev_tool_removal():
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall_dev_tools", _fail("busy"))
    result = await _actions(ctx).cleanup()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "busy" in result.msg


@pytest.mark.asyncio
async def test_cleanup_still_removes_dev_tools_after_a_failed_uninstall():
    # Best-effort teardown: a stranded product must not strand the tooling too.
    ctx, hosts = _fake_ctx(n=1, dev_tools=[_FakeItem("probe", "acme")])
    hosts[0].script("uninstall", _fail("busy"))
    result = await _actions(ctx).cleanup()
    assert not result.is_ok
    assert "busy" in result.msg  # the FIRST failure is what is reported
    assert hosts[0].calls[-1] == ("uninstall_dev_tools", {"owner": "acme"})


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
    assert (await actions.install_tools()).is_ok
    assert (await actions.cleanup()).is_ok
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
    result = await _actions(ctx).install_tools()
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
    result = await _actions(ctx).install_tools()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "exec format error" in result.msg


@pytest.mark.asyncio
async def test_install_tools_dev_false_touches_nothing():
    mine = _FakeItem("probe", "acme")
    ctx, hosts = _fake_ctx(n=1, dev_tools=[mine])
    assert (await _actions(ctx).install_tools(dev=False)).is_ok
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
    result = await _actions(ctx).install_tools(dev=False, toolchain=True)
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
    result = await _actions(ctx).install_tools(dev=True, toolchain=True)
    assert result.is_ok
    assert hosts[0].calls == [("install_dev_tools", {"owner": "acme"})]


# ── logs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_logs_dispatches_owner_scoped_product_haul():
    ctx, hosts = _fake_ctx(n=2)
    result = await _actions(ctx).get_logs()
    assert result.is_ok
    for h in hosts:
        assert h.calls == [("get_product_logs", {"owner": "acme"})]


@pytest.mark.asyncio
async def test_get_logs_product_false_gathers_nothing():
    # There is no debug half here on purpose (host debug logs belong to no
    # repo), so product=False leaves this action with nothing to do.
    ctx, hosts = _fake_ctx(n=1)
    assert (await _actions(ctx).get_logs(product=False)).is_ok
    assert hosts[0].calls == []


def _log_dir(base, name, *, delivered):
    """A host's log root, with ``product/`` populated or left empty."""
    root = base / name
    (root / "product").mkdir(parents=True)
    if delivered:
        (root / "product" / "app.log").write_text("hi", encoding="utf-8")
    return root


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
                log_dir=_log_dir(tmp_path, "full", delivered=True),
            ),
            _FakeHost(
                "h1",
                products=[_FakeItem("app", "acme")],
                log_dir=_log_dir(tmp_path, "empty", delivered=False),
            ),
        ]
    )
    result = await _actions(ctx).get_logs(require_product_logs=True)
    assert not result.is_ok
    assert "h1" in result.msg
    assert "h0" not in result.msg  # the host that DID deliver is not accused


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_only_asks_hosts_this_repo_owns(tmp_path):
    # Kills: demanding a haul from EVERY fleet host. A repo whose products live
    # on part of the fleet (firmware on the embedded target, say) can retrieve
    # everything it owns and still be failed — named after an innocent host it
    # never deploys to — which makes the flag unusable for that whole repo class.
    mine = _log_dir(tmp_path, "mine", delivered=True)
    owner_host = _FakeHost("h0", products=[_FakeItem("app", "acme")], log_dir=mine)
    bare_host = _FakeHost(
        "h1",
        products=[_FakeItem("their-app", "other")],
        log_dir=_log_dir(tmp_path, "bare", delivered=False),
    )
    actions = _actions(_FakeCtx([owner_host, bare_host]))
    assert (await actions.get_logs(require_product_logs=True)).is_ok

    # …and the OWNING host delivering nothing is still a failure that names it,
    # so the narrowed walk cannot degrade into no walk at all.
    (mine / "product" / "app.log").unlink()
    result = await actions.get_logs(require_product_logs=True)
    assert not result.is_ok
    assert "h0" in result.msg


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_is_satisfied_by_a_haul(tmp_path):
    host = _FakeHost(
        "h0",
        products=[_FakeItem("app", "acme")],
        log_dir=_log_dir(tmp_path, "logs", delivered=True),
    )
    ctx = _FakeCtx([host])
    assert (await _actions(ctx).get_logs(require_product_logs=True)).is_ok


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_with_product_false_is_refused():
    # A requirement that cannot be met is refused, not ignored: the haul it
    # requires is the step being skipped.
    ctx, hosts = _fake_ctx(n=1)
    result = await _actions(ctx).get_logs(product=False, require_product_logs=True)
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
        log_dir=_log_dir(tmp_path, "logs", delivered=False),
    )
    host.script("get_product_logs", _fail("transfer refused"))
    result = await _actions(_FakeCtx([host])).get_logs(require_product_logs=True)
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
        state = await _actions(ctx).status()
        assert state is expected, installed_flags


@pytest.mark.asyncio
async def test_status_is_partial_across_hosts_not_only_within_one():
    # One fully-installed host and one bare host is PARTIAL — a per-host
    # is_installed() reduction would call this INSTALLED-somewhere or clean.
    a = _FakeHost("h0", products=[_FakeItem("p", "acme", installed=True)])
    b = _FakeHost("h1", products=[_FakeItem("p", "acme", installed=False)])
    state = await _actions(_FakeCtx([a, b])).status()
    assert state is InstallState.PARTIAL


@pytest.mark.asyncio
async def test_status_ignores_another_repos_products():
    # Kills: counting the whole fleet's products — another repo's half-install
    # would drag this repo to PARTIAL and its full install would fake ours.
    ctx, _ = _ctx_with_products("acme", [True, True], other_flags=[False, False])
    assert await _actions(ctx).status() is InstallState.INSTALLED


@pytest.mark.asyncio
async def test_status_with_no_owned_products_is_uninstalled():
    # Mirrors Host.is_installed's empty-products rule: nothing that could be
    # installed is not vacuously "installed".
    ctx, _ = _ctx_with_products("acme", [], other_flags=[True])
    assert await _actions(ctx).status() is InstallState.UNINSTALLED


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
        async def status(self):
            return InstallState.UNINSTALLED

    ctx, _ = _ctx_with_products("acme", [True, True])
    assert await _actions(ctx, cls=_Opinionated).is_uninstalled() is True


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
