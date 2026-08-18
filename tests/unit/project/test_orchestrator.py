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
from otto.project import PROJECT_ACTIONS, InstallState, ProjectActions
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

    def __init__(self, hosts, lab=None):
        self.hosts = list(hosts)
        self.lab = _fake_lab() if lab is None else lab

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


def _wire(
    monkeypatch,
    repos,
    hosts=0,
    *,
    repair=None,
    reap=None,
    states=(),
    discovery=None,
    links=(),
    **actions_kwargs,
):
    """A lab of *repos* (topological order) with recording actions and *hosts* fleet hosts."""
    events, flags = [], []
    cls = _recording_actions(events, flags, **actions_kwargs)
    for name in repos:
        PROJECT_ACTIONS.register(name, cls, overwrite=True, origin="test")
    fleet = [_FakeHost(f"h{i}", events) for i in range(hosts)]
    ctx = _FakeCtx(fleet, lab=_fake_lab(links))
    ordered = _wire_lab(monkeypatch, repos, ctx)
    calls = _wire_infra(
        monkeypatch, events, repair=repair, reap=reap, states=states, discovery=discovery
    )
    return SimpleNamespace(
        events=events, flags=flags, hosts=fleet, ctx=ctx, ordered=ordered, infra=calls
    )


def _verbs(events, *names):
    """The recorded event names, narrowed to *names* when given."""
    return [e[1] for e in events if not names or e[1] in names]


# ── install ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_walks_repos_in_topological_order(monkeypatch):
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
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)  # base is the dependency
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
    # touch the repo walk. Fail-fast means the FAILURE STOPS EVERYTHING after
    # it, not just the next repo.
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2, failing=("base", "install"))
    result = await project.install()
    assert lab.events == [("base", "install")]
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
async def test_cleanup_walks_reverse_sweeps_untools_then_repairs_and_reaps_last(monkeypatch):
    # The toolchain is HOST-global (one per host, shared by every owner), so it
    # is removed once at the end rather than by any repo. Kills: a per-repo
    # toolchain removal, which would take a neighbour's tooling with it.
    #
    # THE LAST TWO ARE THE ORDER THAT MATTERS MOST, and this is the only place
    # it is asserted: a tunnel can BE the access path to a host, so reaping it
    # before the repo walk, the sweep or the toolchain removal severs the
    # connection the rest of cleanup still needs. Resetting impairments first
    # only improves that path, which is why it comes second-to-last rather
    # than last. Kills: either step hoisted anywhere earlier, and the two
    # swapped.
    lab = _wire(monkeypatch, repos=["base", "app"], hosts=2)
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
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    await project.cleanup()
    assert [(name, obj is lab.ctx.lab) for name, obj in lab.infra] == [
        ("repair_all", True),
        ("remove_all_tunnels", True),
    ]


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
    assert _verbs(lab.events) == [
        "cleanup",
        "remove_toolchain_tools",
        "repair_all",
        "remove_all_tunnels",
    ]


@pytest.mark.asyncio
async def test_cleanup_forwards_get_product_logs(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"])
    await project.cleanup(get_product_logs=False)
    assert lab.flags == [("app", "cleanup", {"get_product_logs": False})]


# ── cleanup: the lab's own infrastructure ────────────────────────────────
#
# Impairments and tunnels belong to no repo, exactly like the debug sweep and
# the toolchain above: nothing in a repo's products or dev tools put them
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
        repos=["app"],
        hosts=1,
        repair=RepairAllReport(failures=["core: host 'r1' unreachable"]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "core" in result.msg
    assert "unreachable" in result.msg
    # …and the reap still ran: cleanup is best-effort all the way down.
    assert ("lab", "remove_all_tunnels") in lab.events


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
        repos=["app"],
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
        repos=["app"],
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
        repos=["app"],
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
        repos=["app"],
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
async def test_cleanup_a_repo_failure_outranks_a_refused_impairment(monkeypatch):
    # The decline is `is_ok`, so it must never displace a real failure in the
    # reported result. Kills: a reduction that returns the last non-Success.
    _wire(
        monkeypatch,
        repos=["app"],
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
        repos=["app"],
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
        repos=["app"],
        hosts=1,
        reap=RemovedReport(removed_ids=[], killed={}, unreachable=["h9"], survivors=[]),
    )
    result = await project.cleanup()
    assert not result.is_ok
    assert "h9" in result.msg


@pytest.mark.asyncio
async def test_cleanup_no_reset_impairments_skips_only_that_step(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.cleanup(reset_impairments=False)).is_ok
    assert "repair_all" not in _verbs(lab.events)
    assert ("lab", "remove_all_tunnels") in lab.events


@pytest.mark.asyncio
async def test_cleanup_no_remove_tunnels_skips_only_that_step(monkeypatch):
    lab = _wire(monkeypatch, repos=["app"], hosts=1)
    assert (await project.cleanup(remove_tunnels=False)).is_ok
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
    ctx = _FakeCtx([], lab=fake_lab)
    ctx.dry_run = True
    _wire_lab(monkeypatch, [], ctx)
    monkeypatch.setattr("otto.context.try_get_context", lambda: ctx)

    result = await project.cleanup()

    assert result.status is Status.NotRun  # not Success, and not a fabricated failure
    assert "dry run" in result.msg


@pytest.mark.asyncio
async def test_cleanup_dry_run_preview_of_the_impairment_reset_is_not_a_repair(monkeypatch):
    # repair_all under a dry run files its previews in `planned` and leaves
    # `repaired` empty. Kills: counting `planned` as work done.
    _wire(
        monkeypatch,
        repos=[],
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
        repos=[],
        hosts=0,
        reap=RemovedReport([], {}, [], [], plan=DryRunPlan(["scan 0 has_bash host(s)"], ["what"])),
    )
    result = await project.cleanup(reset_impairments=False)
    assert result.status is Status.NotRun
    assert "dry run" in result.msg


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


@pytest.mark.asyncio
async def test_status_is_immune_to_impairments_and_tunnels(monkeypatch):
    """INSTALL STATE IS ABOUT PRODUCTS AND NOTHING ELSE (the repo owner's rule).

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
    assert report.repos == {"app": InstallState.INSTALLED}
    assert calls == []


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


def _state(*, impairable=True, direction=FlowDirection.A_TO_B, **shape):
    """One link's read state, with *shape* applied to *direction* and the other clean.

    THE DIRECTION IS A PARAMETER because the two land on different hosts and
    are read separately: an impairment applied with ``--from`` the b end shows
    up in exactly one of these two cells, and a check that consulted only the
    first would report that lab clean.
    """
    link = _link()
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
    lab = _wire(monkeypatch, repos=["app"], hosts=1, states=[_state(), _state(impairable=False)])
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
        repos=["app"],
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
        repos=["app"],
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
    _wire(monkeypatch, repos=["app"], hosts=1, states=[_state(foreign=True)])
    assert await project.is_clean() is True


@pytest.mark.asyncio
async def test_is_clean_is_false_while_a_tunnel_is_live(monkeypatch):
    # The second half of the agreement rule, and the one with no other witness:
    # a lab dirty ONLY in tunnels was clean to every surface before this.
    _wire(
        monkeypatch,
        repos=["app"],
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
        _wire(monkeypatch, repos=["app"], hosts=1, states=[state])
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
        repos=["app"],
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
        _wire(monkeypatch, repos=["app"], hosts=1, discovery=discovery)
        with pytest.raises(RuntimeError):
            await project.is_clean()


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
