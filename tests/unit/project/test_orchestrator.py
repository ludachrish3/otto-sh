"""The orchestrator — cross-repo composition, one debug sweep, and converge.

Everything here is about what the LAB does, not what one repo does: the walk
order (dependencies first to build, dependents first to tear down), what a
failure stops and what it does not, and the host-global steps that belong to no
repo at all (the debug sweep, the toolchain tools).

THE TWO LOOKUPS ARE MONKEYPATCHED WHERE THE ORCHESTRATOR LOOKS THEM UP.
:func:`otto.project.orchestrator._lab` imports ``get_ordered_repos`` and
``get_context`` inside the call (the package's circular-import idiom), so the
attribute read happens on ``otto.config`` / ``otto.context`` at call time and
patching those two names is what the module actually sees.

No registry-isolation fixture here, deliberately: the tests register recording
``ProjectActions`` subclasses, and the root conftest's autouse
``_isolate_registries`` reaches ``PROJECT_ACTIONS`` dynamically (the same
reasoning as ``test_actions.py``). They register into it directly rather than
through ``register_project_actions`` — the attribution seam is
``test_actions.py``'s subject, and a test that wires two labs in a row needs
``overwrite=``.
"""

from types import SimpleNamespace

import pytest

from otto import project
from otto.project import PROJECT_ACTIONS, InstallState, ProjectActions
from otto.result import CommandNotRunError, Result
from otto.utils import Status

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


class _FakeCtx:
    """OttoContext double — the two dispatch seams the orchestrator uses."""

    def __init__(self, hosts):
        self.hosts = list(hosts)

    def all_hosts(self):
        return iter(self.hosts)

    async def do_for_all_hosts(self, method, *args, **kwargs):
        """Apply *method* per host EXACTLY as the production seam does.

        Two properties, both load-bearing. The call shape is ``method(host,
        ...)`` -- the function object it was handed, applied to the host, with
        NO name lookup (``otto/context.py``'s ``method(h, *args, **kwargs)``).
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


def _recording_actions(events, flags, failing=None, state=None, dirty=()):
    """A ``ProjectActions`` subclass recording ``(repo, verb)`` and the flags it got.

    *failing* is one ``(repo, verb)`` pair that answers Failed; *state* scripts
    ``status()``; *dirty* names the repos whose ``is_clean()`` answers False.
    Questions (``status``/``is_clean``) are NOT recorded — ``events`` is the
    list of things the lab was made to DO.
    """

    class _Recording(ProjectActions):
        async def _note(self, verb, **kwargs):
            events.append((self.repo.name, verb))
            flags.append((self.repo.name, verb, kwargs))
            if failing == (self.repo.name, verb):
                return Result(Status.Failed, msg=f"{verb} refused")
            return Result(Status.Success)

        async def install(self):
            return await self._note("install")

        async def uninstall(self, get_product_logs=True):
            return await self._note("uninstall", get_product_logs=get_product_logs)

        async def cleanup(self, get_product_logs=True):
            return await self._note("cleanup", get_product_logs=get_product_logs)

        async def get_logs(self, product=True, require_product_logs=False):
            return await self._note(
                "get_logs", product=product, require_product_logs=require_product_logs
            )

        async def install_tools(self, dev=True, toolchain=False):
            return await self._note("install_tools", dev=dev, toolchain=toolchain)

        async def status(self):
            return state if state is not None else await super().status()

        async def is_clean(self):
            return self.repo.name not in dirty

    return _Recording


def _wire_lab(monkeypatch, repo_names, ctx):
    """Point the orchestrator's two lookups at *repo_names* (in walk order) and *ctx*."""
    ordered = [SimpleNamespace(name=name) for name in repo_names]
    monkeypatch.setattr("otto.config.get_ordered_repos", lambda: ordered)
    monkeypatch.setattr("otto.context.get_context", lambda: ctx)
    return ordered


def _wire(monkeypatch, repos, hosts=0, **actions_kwargs):
    """A lab of *repos* (topological order) with recording actions and *hosts* fleet hosts."""
    events, flags = [], []
    cls = _recording_actions(events, flags, **actions_kwargs)
    for name in repos:
        PROJECT_ACTIONS.register(name, cls, overwrite=True, origin="test")
    fleet = [_FakeHost(f"h{i}", events) for i in range(hosts)]
    ctx = _FakeCtx(fleet)
    ordered = _wire_lab(monkeypatch, repos, ctx)
    return SimpleNamespace(events=events, flags=flags, hosts=fleet, ctx=ctx, ordered=ordered)


def _verbs(events, *names):
    """The recorded event names, narrowed to *names* when given."""
    return [e[1] for e in events if not names or e[1] in names]


# ── install ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_walks_repos_in_topological_order(monkeypatch):
    # Kills: any walk that ignores the resolved order — a dependent installed
    # before the dependency it needs.
    lab = _wire(monkeypatch, repos=["base", "app"])  # base is the dependency
    assert (await project.install()).is_ok
    assert lab.events == [("base", "install"), ("app", "install")]


@pytest.mark.asyncio
async def test_install_is_fail_fast(monkeypatch):
    # Kills: a best-effort install — installing a dependent on top of a
    # dependency that is known not to be there.
    lab = _wire(monkeypatch, repos=["base", "app"], failing=("base", "install"))
    result = await project.install()
    assert ("app", "install") not in lab.events
    assert not result.is_ok
    assert result.status is Status.Failed  # the repo's own status, not a generic one
    assert "base" in result.msg  # kills: dropping WHICH repo failed
    assert "install refused" in result.msg


# ── uninstall ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uninstall_walks_reverse_and_is_best_effort(monkeypatch):
    # Kills BOTH: forward-order teardown (dependents must come down first) and
    # fail-fast teardown (a failed repo must not strand the rest).
    lab = _wire(monkeypatch, repos=["base", "app"], failing=("app", "uninstall"))
    result = await project.uninstall()
    assert [e[0] for e in lab.events if e[1] == "uninstall"] == ["app", "base"]
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_uninstall_does_not_mutate_the_cached_repo_order(monkeypatch):
    # get_ordered_repos() hands back bootstrap's OWN list (it aliases the
    # resolution). Kills: an in-place ``repos.reverse()``, which would leave
    # every later caller — including the next orchestrator verb — walking
    # backwards.
    lab = _wire(monkeypatch, repos=["base", "app"])
    before = list(lab.ordered)
    await project.uninstall()
    assert lab.ordered == before


@pytest.mark.asyncio
async def test_uninstall_forwards_get_product_logs_to_every_repo(monkeypatch):
    lab = _wire(monkeypatch, repos=["base", "app"])
    await project.uninstall(get_product_logs=False)
    assert [f[2] for f in lab.flags] == [{"get_product_logs": False}] * 2


# ── the single debug sweep ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_sweep_runs_once_per_host_after_all_uninstalls(monkeypatch):
    # THE spec §5 rule. Kills: per-repo debug gathering (N sweeps, each
    # overwriting the last), and pre-teardown sweeping (Chris reversed that —
    # teardown activity is what debug logs exist to capture).
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)
    assert (await project.uninstall()).is_ok
    debug_events = [e for e in lab.events if e[1] == "get_debug_logs"]
    assert len(debug_events) == 2  # once per host, not once per repo on every host
    last_uninstall = max(i for i, e in enumerate(lab.events) if e[1] == "uninstall")
    first_debug = min(i for i, e in enumerate(lab.events) if e[1] == "get_debug_logs")
    assert first_debug > last_uninstall


@pytest.mark.asyncio
async def test_uninstall_get_debug_logs_false_skips_the_sweep(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    await project.uninstall(get_debug_logs=False)
    assert "get_debug_logs" not in _verbs(lab.events)


@pytest.mark.asyncio
async def test_uninstall_reports_a_failed_debug_sweep(monkeypatch):
    # Kills: firing the sweep and dropping its answer — a lost log set is the
    # frustration this whole surface exists to prevent.
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    lab.hosts[0].script("get_debug_logs", Result(Status.Failed, msg="transfer refused"))
    result = await project.uninstall()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "transfer refused" in result.msg


@pytest.mark.asyncio
async def test_debug_sweep_runs_even_when_a_repo_failed_to_uninstall(monkeypatch):
    # Best-effort all the way down: the repo that would not tear down is
    # exactly the one whose debug logs are wanted.
    lab = _wire(monkeypatch, repos=["app"], hosts=1, failing=("app", "uninstall"))
    result = await project.uninstall()
    assert ("h0", "get_debug_logs") in lab.events
    assert not result.is_ok
    assert "app" in result.msg  # the FIRST failure is what is reported


# ── cleanup ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_walks_reverse_then_sweeps_then_removes_the_toolchain(monkeypatch):
    # The toolchain is HOST-global (one per host, shared by every owner), so it
    # is removed once at the end rather than by any repo. Kills: a per-repo
    # toolchain removal, which would take a neighbour's tooling with it.
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)
    assert (await project.cleanup()).is_ok
    assert _verbs(lab.events) == [
        "cleanup",
        "cleanup",
        "get_debug_logs",
        "get_debug_logs",
        "remove_toolchain_tools",
        "remove_toolchain_tools",
    ]
    assert [e[0] for e in lab.events if e[1] == "cleanup"] == ["app", "base"]


@pytest.mark.asyncio
async def test_cleanup_removes_the_toolchain_even_when_a_repo_failed(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1, failing=("app", "cleanup"))
    result = await project.cleanup()
    assert ("h0", "remove_toolchain_tools") in lab.events
    assert not result.is_ok
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_cleanup_reports_a_failed_toolchain_removal(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    lab.hosts[0].script("remove_toolchain_tools", Result(Status.Failed, msg="read-only fs"))
    result = await project.cleanup()
    assert not result.is_ok
    assert "h0" in result.msg
    assert "read-only fs" in result.msg


@pytest.mark.asyncio
async def test_cleanup_get_debug_logs_false_still_removes_the_toolchain(monkeypatch):
    # Kills: hanging the host-global removal off the debug-log flag — two
    # unrelated host-global steps that happen to sit next to each other.
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.cleanup(get_debug_logs=False)).is_ok
    assert _verbs(lab.events) == ["cleanup", "remove_toolchain_tools"]


@pytest.mark.asyncio
async def test_cleanup_forwards_get_product_logs(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"])
    await project.cleanup(get_product_logs=False)
    assert lab.flags == [("app", "cleanup", {"get_product_logs": False})]


# ── get_logs ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_logs_walks_every_repo_then_sweeps_debug_once(monkeypatch):
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)
    assert (await project.get_logs()).is_ok
    assert _verbs(lab.events) == ["get_logs", "get_logs", "get_debug_logs", "get_debug_logs"]


@pytest.mark.asyncio
async def test_get_logs_is_best_effort_across_repos(monkeypatch):
    # A repo whose haul failed must not cost the others theirs.
    lab = _wire(monkeypatch, repos=["base", "app"], failing=("base", "get_logs"))
    result = await project.get_logs()
    assert ("app", "get_logs") in lab.events
    assert not result.is_ok
    assert "base" in result.msg


@pytest.mark.asyncio
async def test_get_logs_debug_false_skips_the_sweep(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.get_logs(debug=False)).is_ok
    assert _verbs(lab.events) == ["get_logs"]


@pytest.mark.asyncio
async def test_get_logs_forwards_require_product_logs(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"])
    await project.get_logs(require_product_logs=True)
    assert lab.flags == [("app", "get_logs", {"product": True, "require_product_logs": True})]


@pytest.mark.asyncio
async def test_get_logs_require_product_logs_with_product_false_is_refused(monkeypatch):
    # Kills: leaning on the per-repo refusal, which never fires in a lab with
    # no repos — the requirement would be parsed and silently unenforceable.
    lab = _wire(monkeypatch, repos=[], hosts=1)
    result = await project.get_logs(product=False, require_product_logs=True)
    assert not result.is_ok
    assert "require_product_logs" in result.msg
    assert lab.events == []  # nothing ran, including the debug sweep


# ── install_tools ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_tools_walks_forward_and_is_fail_fast(monkeypatch):
    lab = _wire(monkeypatch, repos=["base", "app"], failing=("base", "install_tools"))
    result = await project.install_tools()
    assert _verbs(lab.events) == ["install_tools"]  # 'app' never started
    assert not result.is_ok
    assert "base" in result.msg


@pytest.mark.asyncio
async def test_install_tools_default_leaves_the_toolchain_alone(monkeypatch):
    # The asymmetric default is the point: toolchain artifacts are large and
    # rarely needed, so asking for them is deliberate.
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.install_tools()).is_ok
    assert _verbs(lab.events) == ["install_tools"]
    # toolchain=False is the repo's own default, never forwarded: the toolchain
    # is host-global, so no repo is ever ASKED to place it.
    assert lab.flags == [("app", "install_tools", {"dev": True, "toolchain": False})]


@pytest.mark.asyncio
async def test_install_tools_toolchain_runs_the_host_global_sweep(monkeypatch):
    # THE host-global half: no repo places toolchain tools (one toolchain per
    # host, shared by every owner), so if the orchestrator does not sweep,
    # ``install_tools(toolchain=True)`` is a silent end-to-end no-op.
    lab = _wire(monkeypatch, repos=["app"], hosts=2)
    assert (await project.install_tools(toolchain=True)).is_ok
    assert _verbs(lab.events) == [
        "install_tools",
        "install_toolchain_tools",
        "install_toolchain_tools",
    ]


@pytest.mark.asyncio
async def test_install_tools_dev_false_toolchain_true_still_sweeps(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.install_tools(dev=False, toolchain=True)).is_ok
    # The repo is still not asked for toolchain work — the sweep below IS the
    # toolchain half, and asking both would place it twice.
    assert lab.flags == [("app", "install_tools", {"dev": False, "toolchain": False})]
    assert ("h0", "install_toolchain_tools") in lab.events


@pytest.mark.asyncio
async def test_install_tools_reports_a_failed_toolchain_sweep(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    lab.hosts[0].script("install_toolchain_tools", Result(Status.Failed, msg="no space"))
    result = await project.install_tools(toolchain=True)
    assert not result.is_ok
    assert "h0" in result.msg
    assert "no space" in result.msg


@pytest.mark.asyncio
async def test_install_tools_failed_dev_walk_never_starts_the_toolchain(monkeypatch):
    # Fail-fast, mirroring the host verb: the toolchain is not placed on top of
    # tooling that is known to be missing.
    lab = _wire(monkeypatch, repos=["app"], hosts=1, failing=("app", "install_tools"))
    result = await project.install_tools(toolchain=True)
    assert not result.is_ok
    assert "install_toolchain_tools" not in _verbs(lab.events)


# ── status ───────────────────────────────────────────────────────────────


def _state_actions(state):
    """A registered subclass that answers *state* — an opinion, held without products."""

    class _Scripted(ProjectActions):
        async def status(self):
            return state

    return _Scripted


def _wire_status(monkeypatch, repos):
    """Lab whose repos map name -> (scripted state | None, installed product flags).

    ``None`` means the repo registers nothing and gets otto's default actions,
    so its state is computed from the products the fleet actually carries.
    """
    products = []
    for name, (state, flags) in repos.items():
        if state is not None:
            PROJECT_ACTIONS.register(name, _state_actions(state), overwrite=True, origin="test")
        products += [_FakeItem(name, installed=flag) for flag in flags]
    ctx = _FakeCtx([_FakeHost("h0", [], products=products)])
    _wire_lab(monkeypatch, list(repos), ctx)


@pytest.mark.asyncio
async def test_status_uncounted_repo_rule_and_empty_aggregate(monkeypatch):
    # A default-actions repo owning no products must not drag the aggregate;
    # zero counted repos aggregate to UNINSTALLED (no vacuous INSTALLED).
    _wire_status(monkeypatch, {"docs": (None, [])})
    st = await project.status()
    assert st.overall is InstallState.UNINSTALLED
    assert st.repos == {}


@pytest.mark.asyncio
async def test_status_ignores_an_uncounted_repo_beside_a_counted_one(monkeypatch):
    _wire_status(monkeypatch, {"docs": (None, []), "app": (None, [True, True])})
    st = await project.status()
    assert st.overall is InstallState.INSTALLED
    assert "docs" not in st.repos
    assert st.repos == {"app": InstallState.INSTALLED}


@pytest.mark.asyncio
async def test_status_counts_registered_subclass_even_without_products(monkeypatch):
    # "A registered subclass opted into having an opinion."
    _wire_status(monkeypatch, {"app": (InstallState.UNINSTALLED, [])})
    st = await project.status()
    assert st.repos == {"app": InstallState.UNINSTALLED}
    assert st.overall is InstallState.UNINSTALLED


@pytest.mark.asyncio
async def test_status_aggregate_is_partial_when_repos_disagree(monkeypatch):
    # Kills: an aggregate read off the first (or last) repo, and one that only
    # sees PARTIAL when some repo is itself PARTIAL.
    _wire_status(monkeypatch, {"base": (None, [True]), "app": (None, [False])})
    st = await project.status()
    assert st.overall is InstallState.PARTIAL
    assert st.repos == {"base": InstallState.INSTALLED, "app": InstallState.UNINSTALLED}


@pytest.mark.asyncio
async def test_status_aggregate_follows_a_single_partial_repo(monkeypatch):
    _wire_status(monkeypatch, {"app": (None, [True, False])})
    assert (await project.status()).overall is InstallState.PARTIAL


@pytest.mark.asyncio
async def test_status_aggregate_is_uninstalled_when_every_counted_repo_is(monkeypatch):
    _wire_status(monkeypatch, {"base": (None, [False]), "app": (None, [False])})
    assert (await project.status()).overall is InstallState.UNINSTALLED


# ── is_clean ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_clean_is_true_when_every_repo_and_host_is_clean(monkeypatch):
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)
    assert await project.is_clean() is True
    assert _verbs(lab.events) == ["toolchain_tools_absent"] * 2


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_repo_has_leftovers(monkeypatch):
    _wire(monkeypatch, repos=["base", "app"], hosts=1, dirty=("app",))
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_host_toolchain_tool_remains(monkeypatch):
    # Kills: an is_clean that only asks the repos. Toolchain tools belong to no
    # repo, so nobody but the orchestrator can see them left behind.
    lab = _wire(monkeypatch, repos=["app"], hosts=2)
    lab.hosts[1].toolchain_absent = False
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_asks_a_repo_that_status_does_not_count(monkeypatch):
    # An uncounted repo (no products, no registered actions) can still own DEV
    # TOOLS — ``owns_products`` cannot see them. Kills: reusing status()'s
    # counted-repo filter here, which would report a lab with a probe still on
    # the board as clean.
    tool = _FakeItem("tools", installed=True)
    ctx = _FakeCtx([_FakeHost("h0", [], dev_tools=[tool])])
    _wire_lab(monkeypatch, ["tools"], ctx)
    assert await project.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_surfaces_a_hosts_refusal_instead_of_answering(monkeypatch):
    # do_for_all_hosts captures exceptions AS VALUES. A dry run's refusal read
    # as "not clean" would send a converge into a cleanup nobody can run, and a
    # transport failure read the same way is a fact nobody established.
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    lab.hosts[0].script("toolchain_tools_absent", CommandNotRunError("test -e /d/gdb", "h0"))
    with pytest.raises(CommandNotRunError):
        await project.is_clean()


# ── ensure_* ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_installed_converges_from_each_state(monkeypatch):
    for state, expected in [
        (InstallState.INSTALLED, []),
        (InstallState.UNINSTALLED, ["install"]),
        (InstallState.PARTIAL, ["uninstall", "install"]),
    ]:
        lab = _wire(monkeypatch, repos=["app"], state=state)
        result = await project.ensure_installed()
        assert _verbs(lab.events) == expected, state
        assert result.is_ok


@pytest.mark.asyncio
async def test_ensure_installed_already_installed_is_a_skip_not_a_fresh_install(monkeypatch):
    _wire(monkeypatch, repos=["app"], state=InstallState.INSTALLED)
    result = await project.ensure_installed()
    assert result.status is Status.Skipped  # "nothing to converge", not "installed it"


@pytest.mark.asyncio
async def test_ensure_installed_recover_partial_false_skips_the_uninstall(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], state=InstallState.PARTIAL)
    await project.ensure_installed(recover_partial=False)
    assert _verbs(lab.events) == ["install"]


@pytest.mark.asyncio
async def test_ensure_installed_stops_when_the_recovery_teardown_fails(monkeypatch):
    # Kills: installing on top of remnants that are KNOWN to be stranded —
    # which reproduces the PARTIAL state ensure_installed was called to fix.
    lab = _wire(
        monkeypatch,
        repos=["app"],
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
        lab = _wire(monkeypatch, repos=["app"], state=state)
        assert (await project.ensure_uninstalled()).is_ok
        assert _verbs(lab.events) == expected, state


@pytest.mark.asyncio
async def test_ensure_clean_cleans_only_a_dirty_lab(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.ensure_clean()).status is Status.Skipped
    assert "cleanup" not in _verbs(lab.events)

    lab = _wire(monkeypatch, repos=["app"], hosts=1, dirty=("app",))
    assert (await project.ensure_clean()).is_ok
    assert "cleanup" in _verbs(lab.events)


@pytest.mark.asyncio
async def test_install_ensure_flag_delegates_to_the_converge(monkeypatch):
    # The CLI's ``install --ensure`` is this argument, not a second code path.
    lab = _wire(monkeypatch, repos=["app"], state=InstallState.PARTIAL)
    await project.install(ensure=True)
    assert _verbs(lab.events) == ["uninstall", "install"]

    lab = _wire(monkeypatch, repos=["app"], state=InstallState.PARTIAL)
    await project.install(ensure=True, recover_partial=False)
    assert _verbs(lab.events) == ["install"]

    # …and without it, install is the plain walk: no status is consulted, so a
    # lab that is already INSTALLED is installed again.
    lab = _wire(monkeypatch, repos=["app"], state=InstallState.INSTALLED)
    await project.install()
    assert _verbs(lab.events) == ["install"]


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
        "status",
        "is_clean",
        "ensure_installed",
        "ensure_uninstalled",
        "ensure_clean",
    ):
        assert callable(getattr(project, name)), name
