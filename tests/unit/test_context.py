import asyncio
import logging
import re
from pathlib import Path

import pytest

from otto import bootstrap as bs
from otto.context import (
    LIBRARY_LAB_NAME,
    HostScope,
    OttoContext,
    get_context,
    reset_context,
    set_context,
    try_get_context,
)
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from tests._fixtures.chaos import ChaosPoints, Surface, sweep_cancellation
from tests._fixtures.fake_repo import fake_repo


class _FakeHost:
    """Minimal stand-in for a host: an id and an idempotent close()."""

    def __init__(self, host_id: str):
        self.id = host_id
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_hostscope_sweep_closes_every_registered_host_and_names_them():
    """Registration already means the host connected on this loop, so the sweep
    closes each one (close() on an idle host is a cheap no-op) and returns their ids."""
    scope = HostScope()
    a, b = _FakeHost("a"), _FakeHost("b")
    scope.register(a)
    scope.register(b)
    assert await scope.sweep() == ["a", "b"]
    assert (a.close_calls, b.close_calls) == (1, 1)


@pytest.mark.asyncio
async def test_hostscope_register_is_deduped():
    scope = HostScope()
    h = _FakeHost("a")
    scope.register(h)
    scope.register(h)
    assert await scope.sweep() == ["a"]
    assert h.close_calls == 1


@pytest.mark.asyncio
async def test_hostscope_isolates_errors():
    class _Boom(_FakeHost):
        async def close(self):
            raise RuntimeError("boom")

    boom = _Boom("boom")
    ok = _FakeHost("ok")
    scope = HostScope()
    scope.register(boom)
    scope.register(ok)
    assert await scope.sweep() == ["ok"]
    assert ok.close_calls == 1


@pytest.mark.asyncio
async def test_hostscope_sweep_skips_a_host_another_loop_now_owns():
    """A host closed on this loop and since claimed by another is that loop's to close."""
    other = asyncio.new_event_loop()
    try:
        moved = _FakeHost("moved")
        moved._owner_loop = other
        mine = _FakeHost("mine")
        mine._owner_loop = asyncio.get_running_loop()
        scope = HostScope()
        scope.register(moved)
        scope.register(mine)
        assert await scope.sweep() == ["mine"]
        assert moved.close_calls == 0
    finally:
        other.close()


from otto.config.lab import Lab


def _lab_with(*ne_names: str) -> Lab:
    """Build a Lab with real UnixHosts from available NE names in the test lab data."""
    from tests.conftest import make_host

    lab = Lab(name="t")
    for ne in ne_names:
        lab.add_host(make_host(ne))
    return lab


def test_get_host_unknown_id_raises_helpful_keyerror():
    import pytest

    ctx = OttoContext(lab=_lab_with("test1"))
    with pytest.raises(KeyError, match="Available"):
        ctx.get_host("does-not-exist")


def test_get_host_unknown_id_normal_lab_message_has_no_breadcrumb():
    """A normal (non-sentinel) lab's unknown-host message stays exactly what it
    was before the open_context breadcrumb was added — no library-only hint
    leaking into ordinary CLI/lab-backed errors."""
    import pytest

    ctx = OttoContext(lab=_lab_with("test1"))
    with pytest.raises(KeyError) as excinfo:
        ctx.get_host("does-not-exist")
    message = str(excinfo.value)
    assert message == ("\"No host 'does-not-exist' in lab 't'. Available: ['test1']\"")
    assert "open_context" not in message
    assert "no lab is loaded" not in message


def test_get_host_unknown_id_sentinel_lab_appends_open_context_breadcrumb():
    """The minimal library context installed by suite.run._session_context uses
    the sentinel lab name LIBRARY_LAB_NAME ("<library>"); get_host's unknown-host
    error must append a hint to wrap the call in ``async with
    otto.open_context(lab=...)`` — and ONLY for that sentinel lab."""
    import pytest

    from otto.config.lab import Lab
    from otto.context import LIBRARY_LAB_NAME

    assert LIBRARY_LAB_NAME == "<library>"
    ctx = OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))
    with pytest.raises(KeyError) as excinfo:
        ctx.get_host("does-not-exist")
    message = str(excinfo.value)
    assert "no lab is loaded" in message
    assert "async with otto.open_context(lab=...)" in message
    # The breadcrumb is an ADDITION, not a rewrite: the original wording is untouched.
    assert "No host 'does-not-exist' in lab '<library>'. Available: []" in message


def test_context_get_host_and_all_hosts_resolve_from_lab():
    # Use NEs that exist in tests/lab_data/tech1/lab.json with creds (Unix hosts):
    # test1, test2, test3, test4
    lab = _lab_with("test1", "test2", "test3")
    ctx = OttoContext(lab=lab)
    first_id = next(iter(lab.hosts))
    assert ctx.get_host(first_id) is lab.hosts[first_id]
    # Filter to only test1 and test2 — not test3. The trailing `.*` is
    # load-bearing: ids are FULLMATCHED (D6), so a bare alternation of the
    # element names selects nothing (and now raises).
    ids = {h.id for h in ctx.all_hosts(re.compile("(test1|test2).*"))}
    assert ids
    assert all("test1" in i or "test2" in i for i in ids)
    # "test3" should not appear in the filtered result
    assert not any("test3" in i for i in ids)


@pytest.mark.asyncio
async def test_context_all_hosts_registers_nothing_until_a_host_connects():
    """A host joins the scope of the loop it first connects on, not the one that listed it."""
    lab = _lab_with("test1")
    ctx = OttoContext(lab=lab)
    hosts = list(ctx.all_hosts())
    assert hosts
    assert ctx.scope_for(asyncio.get_running_loop())._hosts == []


def test_set_and_reset_context_round_trips():
    assert try_get_context() is None
    ctx = OttoContext(lab=_lab_with("test1"))
    token = set_context(ctx)
    try:
        assert get_context() is ctx
    finally:
        reset_context(token)
    assert try_get_context() is None


class _FakeRunHost(_FakeHost):
    def __init__(self, host_id: str):
        super().__init__(host_id)
        self.run_calls: list = []

    async def run(self, cmds, timeout=DEFAULT_COMMAND_TIMEOUT):
        self.run_calls.append((cmds, timeout))
        return f"ran:{self.id}"


@pytest.mark.asyncio
async def test_do_for_all_hosts_concurrent_captures_exceptions_per_host():
    lab = _lab_with("test1", "test2")
    ctx = OttoContext(lab=lab)
    ids = list(lab.hosts)

    async def flaky(host):
        if host.id == ids[0]:
            raise RuntimeError("boom")
        return "ok"

    results = await ctx.do_for_all_hosts(flaky)
    assert isinstance(results[ids[0]], RuntimeError)
    assert results[ids[1]] == "ok"


@pytest.mark.asyncio
async def test_do_for_all_hosts_serial_captures_exceptions():
    lab = _lab_with("test1", "test2")
    ctx = OttoContext(lab=lab)
    ids = list(lab.hosts)

    async def flaky(host):
        if host.id == ids[0]:
            raise RuntimeError("boom")
        return "ok"

    results = await ctx.do_for_all_hosts(flaky, concurrent=False)
    assert isinstance(results[ids[0]], RuntimeError)
    assert results[ids[1]] == "ok"


@pytest.mark.asyncio
async def test_run_on_all_hosts_normalizes_str_to_list():
    lab = Lab(name="t")
    h = _FakeRunHost("h1")
    # inject directly; no overrides => _apply_option_overrides returns it unchanged
    lab.hosts["h1"] = h
    ctx = OttoContext(lab=lab)
    results = await ctx.run_on_all_hosts("uname -a")
    # str normalized to a single-element list; timeout defaults to DEFAULT_COMMAND_TIMEOUT
    assert h.run_calls == [(["uname -a"], DEFAULT_COMMAND_TIMEOUT)]
    assert results["h1"] == "ran:h1"


def test_for_repo_is_a_facade_over_the_same_context_not_a_copy(tmp_path):
    """``for_repo`` narrows walks and nothing else — same lab, same scope, live flags.

    A view that COPIED the context would pass every scoping test in
    ``tests/unit/config/test_fleet_scoping.py`` and still be wrong twice over:
    hosts handed out by the view would register into a second set of per-loop
    :class:`~otto.context.HostScope` objects that nothing sweeps, and a flag set on the
    context after the view was built (``output_dir``, which the CLI stamps
    per-run) would never reach the repo acting under it.
    """
    lab = _lab_with("test1")
    ctx = OttoContext(lab=lab, dry_run=True)
    view = ctx.for_repo("acme")

    assert view.lab is ctx.lab
    loop = asyncio.new_event_loop()
    try:
        assert view.scope_for(loop) is ctx.scope_for(loop)  # ONE scope per loop, or hosts leak
    finally:
        loop.close()
    assert view.dry_run is True
    first_id = next(iter(lab.hosts))
    assert view.get_host(first_id) is lab.hosts[first_id]  # explicit targeting delegates

    ctx.output_dir = tmp_path  # a snapshot would go stale here
    assert view.output_dir == tmp_path


def test_context_runtime_flags_default_and_override():
    lab = _lab_with("test1")
    assert OttoContext(lab=lab).dry_run is False
    assert OttoContext(lab=lab).log_command_output is True
    assert OttoContext(lab=lab, dry_run=True).dry_run is True
    assert OttoContext(lab=lab, log_command_output=False).log_command_output is False
    assert OttoContext(lab=lab).cov_decision is None  # undecided: detected on first read
    assert OttoContext(lab=lab, cov_decision=True).cov is True
    assert OttoContext(lab=lab, cov_decision=False).cov is False


def test_bare_accessors_delegate_to_active_context():
    import otto.lab as cm
    from otto.context import OttoContext, reset_context, set_context

    lab = _lab_with("test1", "test2")
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)
    try:
        assert cm.get_lab() is lab
        assert {h.id for h in cm.all_hosts()} == set(lab.hosts)
        first = next(iter(lab.hosts))
        assert cm.get_host(first) is lab.hosts[first]
    finally:
        reset_context(token)


def test_admissible_ids_is_public_and_the_private_name_is_an_alias(monkeypatch):
    """The fleet of interest has a public name now (spec 2026-08-28
    three-level-reservations §5).

    The reservation gate reads the same set every fleet walk starts from, so it
    cannot be reached through an underscored method; the private spelling stays
    as an alias for one release rather than breaking any caller that has it.
    """
    monkeypatch.setattr("otto.bootstrap.get_ordered_repos", list)
    ctx = OttoContext(lab=_lab_with("test1"))

    assert ctx.admissible_ids() == {"test1"}
    # The private spelling is the alias, deliberately reached here by that name.
    assert ctx._admissible_ids(None) == ctx.admissible_ids(None)


def test_addhost_wires_lab_backref_and_survives_override_copy():
    import dataclasses

    lab = _lab_with("test1")
    host = next(iter(lab.hosts.values()))
    assert host._lab is lab
    copy = dataclasses.replace(host)  # *_options overrides use replace
    assert copy._lab is lab  # field must carry forward


@pytest.mark.asyncio
async def test_host_async_context_manager_closes_and_close_is_idempotent():
    lab = _lab_with("test1")
    host = next(iter(lab.hosts.values()))
    async with host as h:
        assert h is host
    # exiting the context called close(); a second close must be a harmless no-op
    await host.close()
    await host.close()


@pytest.mark.asyncio
async def test_base_host_async_cm_delegates_to_close():
    """BaseHost.__aenter__/__aexit__ must delegate to close() exactly once."""
    from otto.host.host import BaseHost

    class _MinimalHost(BaseHost):
        """Minimal BaseHost concrete subclass: counts the ``_close`` that ``close()`` wraps."""

        def __init__(self) -> None:
            self.close_calls = 0

        async def _close(self) -> None:
            self.close_calls += 1

    h = _MinimalHost()
    async with h as entered:
        assert entered is h
        assert h.close_calls == 0
    assert h.close_calls == 1


@pytest.mark.asyncio
async def test_open_context_sets_and_tears_down():
    import otto
    from otto.context import try_get_context

    lab = _lab_with("test1")
    assert try_get_context() is None
    async with otto.open_context(lab=lab) as ctx:
        assert try_get_context() is ctx
        list(ctx.all_hosts())  # hands hosts out; nothing connects, so nothing registers
    assert try_get_context() is None  # contextvar reset on exit


@pytest.mark.asyncio
async def test_open_context_with_a_lab_object_never_imports_or_calls_build_inventory(
    monkeypatch,
):
    """A `Lab` object skips inventory resolution entirely — the `else` arm never runs.

    Patched to EXPLODE rather than merely counting calls: a spy only proves
    our own test writes the call, not that ``open_context`` makes it. If
    ``build_inventory`` ran on the ``Lab``-object path, this fixture would
    already know it (this module is imported well after ``otto.inventory``
    elsewhere in the suite, so a plain ``sys.modules`` check would be
    unreliable here).
    """
    import otto
    import otto.inventory.config as inventory_config

    def _must_not_run(*args, **kwargs):
        raise AssertionError("build_inventory must not run for a Lab object")

    monkeypatch.setattr(inventory_config, "build_inventory", _must_not_run)

    lab = _lab_with("test1")
    async with otto.open_context(lab=lab) as ctx:
        assert ctx.lab is lab


@pytest.mark.asyncio
async def test_open_context_loads_the_lab_with_the_process_inventory(tmp_path, monkeypatch):
    """``open_context`` is the library entry point, so it resolves ``[inventory]`` too.

    Spec §6 names ``context.py`` among the callers that must hand the process
    inventory to the load. Without it a library user with a correct
    ``[inventory]`` table and a referenced host entry is told "no inventory is
    configured; declare [inventory] in ~/.otto/settings.toml" — an instruction
    they have already followed.

    Driven through the REAL resolution: ``OTTO_HOME`` (otto's own variable)
    points at a user settings file naming the worked-example fixture, so
    ``build_inventory`` reads a real file and the json backend a real
    inventory, rather than a patched seam that would pass with the threading
    still missing. The lab itself comes from a SUT repo's ``[[lab.sources]]``
    (a copy of the fixture's lab file), installed as the bootstrap result.
    """
    import shutil

    import otto
    from otto.config.repo import Repo
    from tests._fixtures.labdata import lab_data_dir
    from tests._fixtures.sutrepo import make_sut_repo

    fixture = lab_data_dir() / "tech1-inventory"
    sut = make_sut_repo(
        tmp_path / "repo", extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'
    )
    (sut / "lab").mkdir()
    shutil.copy(fixture / "lab.json", sut / "lab" / "lab.json")
    _install_result(monkeypatch, repos=[Repo(sut)])
    home = tmp_path / "home"
    home.mkdir()
    settings = home / "settings.toml"
    settings.write_text(  # sutrepo-exempt: the user-level ~/.otto file, not a SUT repo
        '[inventory]\nbackend = "json"\n'
        f'path = "{fixture / "inventory.json"}"\n'
        'supplies = ["ip", "interfaces", "is_virtual", "site", "rack", '
        '"shelf", "board", "os_name"]\n'
        f'\n[creds]\nbackend = "json"\npath = "{fixture / "creds.json"}"\n'
    )
    monkeypatch.setenv("OTTO_HOME", str(home))
    async with otto.open_context(lab="unix") as ctx:
        host = ctx.lab.hosts["test1"]
        assert host.inventory_ref.key == "test1"
        assert host.ip == "10.10.200.11"  # the record's address, not the lab file's


@pytest.mark.asyncio
async def test_run_on_all_hosts_accepts_option_overrides():
    """ctx.run_on_all_hosts/do_for_all_hosts accept *_options kwargs without error."""
    from otto.config.lab import Lab

    lab = Lab(name="t")
    h = _FakeRunHost("h1")
    lab.hosts["h1"] = h
    ctx = OttoContext(lab=lab)

    # ssh_options=None is a no-op override; just confirms the signature accepts it
    results = await ctx.run_on_all_hosts("uname -a", ssh_options=None, telnet_options=None)
    assert results["h1"] == "ran:h1"

    # do_for_all_hosts also accepts the override kwargs
    async def _noop(host):
        return "ok"

    results2 = await ctx.do_for_all_hosts(_noop, ssh_options=None, ftp_options=None)
    assert results2["h1"] == "ok"


def test_otto_context_output_dir_defaults_none_and_is_settable():
    # OttoContext requires a lab; use a minimal stand-in via the dataclass.
    ctx = OttoContext(lab=None)  # type: ignore[arg-type]
    assert ctx.output_dir is None
    ctx.output_dir = Path("/tmp/otto-run-xyz")
    assert ctx.output_dir == Path("/tmp/otto-run-xyz")


@pytest.mark.asyncio
async def test_hostscope_sweep_drains_registered_hosts():
    """A sweep closes AND forgets: a second sweep of the same scope (a loop
    swept twice) must not re-close hosts the first one closed."""
    scope = HostScope()
    h = _FakeHost("a")
    scope.register(h)
    assert await scope.sweep() == ["a"]
    assert h.close_calls == 1
    assert await scope.sweep() == []
    assert h.close_calls == 1


class _ScopedHost:
    """Standalone fake for ranked-sweep tests: records close order into a shared list."""

    def __init__(
        self,
        name: str,
        order: "list[str]",
        *,
        parent: "object | None" = None,
        fail: bool = False,
        yields: int = 0,
    ) -> None:
        self.id = name
        self._order = order
        self._fail = fail
        self._yields = yields
        if parent is not None:
            self.parent = parent

    async def close(self) -> None:
        for _ in range(self._yields):
            await asyncio.sleep(0)
        self._order.append(self.id)
        if self._fail:
            raise RuntimeError(f"{self.id}: close blew up")


@pytest.mark.asyncio
async def test_hostscope_closes_children_before_their_parent():
    """DockerContainerHost.close documents close-before-parent (its docker
    exec channel drains over the parent's still-open transport); the sweep
    must honor it. The child here closes SLOWER than its parent would, so a
    naive concurrent gather finishes the parent first."""
    order: "list[str]" = []
    parent = _ScopedHost("parent", order)
    child = _ScopedHost("child", order, parent=parent, yields=2)
    scope = HostScope()
    scope.register(child)
    scope.register(parent)
    assert await scope.sweep() == ["child", "parent"]
    assert order == ["child", "parent"]


@pytest.mark.asyncio
async def test_hostscope_ranks_a_three_level_parent_chain():
    order: "list[str]" = []
    top = _ScopedHost("top", order)
    mid = _ScopedHost("mid", order, parent=top, yields=1)
    leaf = _ScopedHost("leaf", order, parent=mid, yields=2)
    scope = HostScope()
    scope.register(top)
    scope.register(mid)
    scope.register(leaf)
    assert await scope.sweep() == ["leaf", "mid", "top"]
    assert order == ["leaf", "mid", "top"]


@pytest.mark.asyncio
async def test_hostscope_child_close_failure_still_closes_the_parent(caplog):
    """One host's close dying must be LOGGED (named) and must not stop the
    remaining ranks — silent swallowing is what this plan removes."""
    order: "list[str]" = []
    parent = _ScopedHost("parent", order)
    child = _ScopedHost("child", order, parent=parent, fail=True)
    scope = HostScope()
    scope.register(child)
    scope.register(parent)
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        closed = await scope.sweep()
    assert closed == ["parent"]
    assert order == ["child", "parent"]
    assert any("'child'" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_hostscope_sweep_chain():
    """Tier-1 sweep: one host's close dying (drop OR injected cancel) never
    skips the other hosts — per-rank gather captures per-host failures."""
    names = ["h1", "h2", "h3"]

    class _PointHost:
        def __init__(self, name: str, points: ChaosPoints) -> None:
            self.id = name
            self._points = points

        async def close(self) -> None:
            await self._points.point(self.id, surface=Surface.NETWORK)

    async def scenario(points: ChaosPoints) -> None:
        scope = HostScope()
        for name in names:
            scope.register(_PointHost(name, points))
        await scope.sweep()

    def oracle(points, outcome, exc_type, k) -> None:
        # Both variants: an injected failure inside ONE host's close is
        # indistinguishable from that close dying — it is captured, logged,
        # and the sweep continues. (Force-abandon cancels the sweep TASK,
        # which is a different mechanism and still aborts everything.)
        assert outcome is None, f"{exc_type.__name__} at host {k} escaped the sweep"
        assert points.executed == [n for i, n in enumerate(names) if i != k - 1]

    report = await sweep_cancellation(scenario, oracle)
    assert report.points == len(names)
    # Each host's close is a transport teardown: a command-failure cannot arise
    # at any of them, and this pins that the sweep skipped it on purpose.
    assert report.injected["command-failure"] == 0
    assert report.skipped["command-failure"] == len(names)
    for name in ("cancellation", "connection-dropped", "connection-reset", "timeout"):
        assert report.injected[name] == len(names), name


# ── ctx.cov: coverage awareness, detected lazily when nothing decided it ─────


class _CovRepo:
    """The one thing detection reads off a repo: its settings."""

    def __init__(self, coverage: "dict | None") -> None:
        self.settings = {"coverage": coverage} if coverage is not None else {}


@pytest.fixture
def cov_detection(monkeypatch):
    """Stub the repos and the fleet walk; each host carries one product.

    ``instrumented`` is each product's own ``instrumented()`` verdict, in host
    order; ``scans`` records every walk and the selector it was given.
    """
    from types import SimpleNamespace

    state = {"repos": [_CovRepo({"hosts": "test.*"})], "instrumented": [True], "scans": []}

    def _all_hosts(_self, pattern=None, *, include_containers=False, **_kw):
        state["scans"].append((pattern.pattern if pattern else None, include_containers))
        return [
            SimpleNamespace(
                id=f"h{i}", products=[SimpleNamespace(instrumented=lambda v=verdict: v)]
            )
            for i, verdict in enumerate(state["instrumented"])
        ]

    monkeypatch.setattr("otto.bootstrap.get_repos", lambda: state["repos"])
    monkeypatch.setattr(OttoContext, "all_hosts", _all_hosts)
    return state


def test_cov_is_detected_true_when_instrumented_and_configured(cov_detection):
    ctx = OttoContext(lab=_lab_with("test1"))
    assert ctx.cov is True
    # The [coverage].hosts selector, and containers included (a product can live in one).
    assert cov_detection["scans"] == [("test.*", True)]


def test_cov_is_false_when_nothing_is_instrumented(cov_detection):
    """``unknown`` (None) is not instrumented, exactly as otto test's scan counts it."""
    cov_detection["instrumented"] = [False, None]
    assert OttoContext(lab=_lab_with("test1")).cov is False


def test_cov_without_a_coverage_table_is_false_and_never_scans(cov_detection):
    """otto test's auto rule: no [coverage] table means off, whatever is built."""
    cov_detection["repos"] = [_CovRepo(None)]
    assert OttoContext(lab=_lab_with("test1")).cov is False
    assert cov_detection["scans"] == []


def test_cov_detection_runs_once_per_context(cov_detection):
    ctx = OttoContext(lab=_lab_with("test1"))
    assert [ctx.cov, ctx.cov, ctx.cov] == [True, True, True]
    assert len(cov_detection["scans"]) == 1
    assert ctx.cov_decision is True  # the detected answer is now the decision


def test_a_decision_wins_over_detection(cov_detection):
    """otto test stamps its resolved --cov/--no-cov/auto decision; nothing is scanned."""
    ctx = OttoContext(lab=_lab_with("test1"), cov_decision=False)
    assert ctx.cov is False
    assert cov_detection["scans"] == []


def test_cov_on_the_library_sentinel_lab_is_false_without_touching_repos(monkeypatch):

    def _boom():
        raise AssertionError("the sentinel lab must not reach the repos")

    monkeypatch.setattr("otto.bootstrap.get_repos", _boom)
    assert OttoContext(lab=Lab(name=LIBRARY_LAB_NAME)).cov is False


def test_cov_detection_failure_is_false_with_one_warning(cov_detection, caplog):
    """A broken [coverage].hosts selector must not kill a caller that only asked."""
    cov_detection["repos"] = [_CovRepo({"hosts": 5})]  # not a string: CoverageConfigError
    ctx = OttoContext(lab=_lab_with("test1"))
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        assert ctx.cov is False
        assert ctx.cov is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "coverage" in warnings[0].getMessage()


def test_cov_detection_with_unreachable_repos_is_false(monkeypatch):

    def _unreachable():
        raise RuntimeError("no bootstrap")

    monkeypatch.setattr("otto.bootstrap.get_repos", _unreachable)
    assert OttoContext(lab=_lab_with("test1")).cov is False


def test_cov_reads_through_a_repo_view(cov_detection):
    """The repo-scoped view delegates live, so instructions' self.ctx sees it too."""
    ctx = OttoContext(lab=_lab_with("test1"))
    assert ctx.for_repo("acme").cov is True


# --- open_context makes the CLI's decisions -------------------------------------
#
# The bootstrap result is INSTALLED, the way tests/unit/cli/test_bootstrap_gate.py
# installs it, so each test decides exactly which repos (and which load errors)
# open_context sees. The root conftest's `_restore_bootstrap_state` puts the
# previous result back afterwards.


def _install_result(monkeypatch, *, repos=(), errors=()):
    """Make *repos* and *errors* what ``bootstrap()`` returns for this test."""
    bs._reset()
    result = bs.BootstrapResult(
        env=None,
        repos=list(repos),
        errors=list(errors),
        warnings=[],
        ordered_repos=list(repos),  # as bootstrap() fills it, so the real preflight sees them
    )
    monkeypatch.setattr(bs, "_result", result)
    return result


def _host(element: str, ip: str) -> dict:
    """One inline lab-json host record, a member of lab ``merged``."""
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "resources": [element],
        "labs": ["merged"],
    }


@pytest.fixture
def installed_repos(monkeypatch):
    """Build a real SUT repo from settings + lab files and install it as bootstrap's result."""
    from otto.config.repo import Repo
    from tests._fixtures.labdata import write_lab_json
    from tests._fixtures.sutrepo import make_sut_repo

    def _install(root: Path, extra: str, labfiles: "dict[str, list[dict]]") -> Repo:
        sut = make_sut_repo(root, name=root.name, extra=extra)
        for rel, hosts in labfiles.items():
            write_lab_json(root / rel, hosts)
        repo = Repo(sut)
        _install_result(monkeypatch, repos=[repo])
        return repo

    return _install


def _broken_repo(name: str, *, lab_patterns: "list[str] | None" = None):
    """A discovered repo whose init failed, and its load error (its [project] parsed)."""

    from otto.config.scope import ProjectScopeConfig

    scope = (
        None
        if lab_patterns is None
        else ProjectScopeConfig(
            lab_patterns=[re.compile(p) for p in lab_patterns], host_patterns=[]
        )
    )
    sut_dir = Path(f"/repos/{name}")
    repo = fake_repo(name, sut_dir=sut_dir, project_scope=scope)
    return repo, bs.DependencyError(str(sut_dir), "dependency 'y' is not satisfied")


@pytest.mark.asyncio
async def test_open_context_builds_the_lab_build_lab_builds(tmp_path, installed_repos, monkeypatch):
    """The lab parity: sources, preferences and placeholders all come from the repos."""
    import otto
    from otto.docker import compose
    from otto.session import build_lab

    placeholder_labs: "list[Lab]" = []
    real_register = compose.register_declared_container_hosts

    def _spy(lab, repos):
        placeholder_labs.append(lab)
        return real_register(lab, repos)

    monkeypatch.setattr(compose, "register_declared_container_hosts", _spy)
    repo = installed_repos(
        tmp_path / "r1",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'
        '[host_preferences.".*".ssh_options]\nport = 2222\n',
        {"lab/lab.json": [_host("alt1", "10.0.0.1")]},
    )
    expected = build_lab([repo], ["merged"])
    async with otto.open_context(lab="merged") as ctx:
        assert set(ctx.lab.hosts) == set(expected.hosts)
        assert "alt1" in ctx.lab.hosts  # the repo's [[lab.sources]] record
        assert ctx.lab.hosts["alt1"].ssh_options.port == 2222  # the repo's [host_preferences]
        assert placeholder_labs[-1] is ctx.lab  # the declared containers were registered on it


@pytest.mark.asyncio
async def test_a_combined_lab_string_is_split_before_the_gate(monkeypatch):
    """'a+b': a repo matching only b is ACTIVE, so its load error is fatal.

    Unsplit, the one name ``a+b`` fullmatches no pattern, the repo reads as
    out of scope, and its error is merely demoted.
    """
    import otto
    from otto.session import RepoLoadError

    repo, error = _broken_repo("Repo2", lab_patterns=["b"])
    _install_result(monkeypatch, repos=[repo], errors=[error])
    with pytest.raises(RepoLoadError) as exc:
        async with otto.open_context(lab="a+b"):
            pass
    assert exc.value.errors == [error]
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_a_list_of_labs_is_split_like_repeated_lab_flags(monkeypatch):
    """``["a", "b+c"]`` is three labs, as ``--lab a --lab b+c`` is, for the gate and the build."""
    import otto
    from otto.session import projects

    real_check_repos = projects.check_repos
    seen: "dict[str, list[str]]" = {}

    def _check_repos(result, labs, selection):
        seen["gate"] = list(labs)
        return real_check_repos(result, labs, selection)

    def _build_lab(repos, labs):
        seen["build"] = list(labs)
        return _lab_with("test1")

    _install_result(monkeypatch)
    monkeypatch.setattr(projects, "check_repos", _check_repos)
    monkeypatch.setattr("otto.session.lab.build_lab", _build_lab)
    async with otto.open_context(lab=["a", "b+c"]):
        pass
    assert seen == {"gate": ["a", "b", "c"], "build": ["a", "b", "c"]}


@pytest.mark.asyncio
async def test_a_lab_object_still_runs_the_gate(monkeypatch):
    """Labs come from component_names; a broken active repo refuses."""
    import otto
    from otto.session import RepoLoadError

    repo, error = _broken_repo("Repo2", lab_patterns=["t"])
    _install_result(monkeypatch, repos=[repo], errors=[error])
    with pytest.raises(RepoLoadError) as exc:
        async with otto.open_context(lab=_lab_with("test1")):
            pass
    assert exc.value.errors == [error]
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_a_lab_objects_component_names_are_the_gates_labs(monkeypatch, caplog):
    """A repo out of scope for every component of the Lab is demoted, not fatal.

    With no labs at all the gate cannot prove the repo inactive, so this
    passes only if the gate was handed the Lab's ``component_names``.
    """
    import otto

    repo, error = _broken_repo("Repo2", lab_patterns=["elsewhere"])
    _install_result(monkeypatch, repos=[repo], errors=[error])
    lab = _lab_with("test1")
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        async with otto.open_context(lab=lab) as ctx:
            assert ctx.lab is lab
    assert "inactive for this run (not applicable to lab(s) [t])" in caplog.text


@pytest.mark.asyncio
async def test_a_demoted_error_is_logged_not_raised(caplog, monkeypatch):
    import otto

    repo, error = _broken_repo("Repo2")
    _install_result(monkeypatch, repos=[repo], errors=[error])
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        async with otto.open_context(lab=_lab_with("test1"), exclude_projects=["Repo2"]) as ctx:
            assert ctx.exclude_projects == ("repo2",)
    assert "inactive for this run (exclude_projects repo2)" in caplog.text


@pytest.mark.asyncio
async def test_the_selected_projects_reach_the_context(monkeypatch):
    """``include_projects`` is normalised and carried on the context, as the CLI carries ``-I``."""
    import otto

    repo, _ = _broken_repo("My_Repo")
    _install_result(monkeypatch, repos=[repo])
    async with otto.open_context(lab=_lab_with("test1"), include_projects=["My_Repo"]) as ctx:
        assert (ctx.include_projects, ctx.exclude_projects) == (("my-repo",), ())


@pytest.mark.asyncio
async def test_an_unknown_project_is_refused(monkeypatch):
    import otto
    from otto.session import ProjectSelectionError

    repo, _ = _broken_repo("Repo2")
    _install_result(monkeypatch, repos=[repo])
    with pytest.raises(ProjectSelectionError) as exc:
        async with otto.open_context(lab=_lab_with("test1"), include_projects=["nope"]):
            pass
    assert (exc.value.kind, exc.value.field, exc.value.names) == (
        "unknown",
        "include_projects",
        ["nope"],
    )
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_a_dependency_refusal_resets_the_context(monkeypatch):
    """The check runs with the context installed, and a refusal uninstalls it."""
    import otto
    from otto.session import DependencyRefusedError

    seen = []

    def _refuse(ctx):
        seen.append(try_get_context() is ctx)
        raise DependencyRefusedError([])

    _install_result(monkeypatch)
    monkeypatch.setattr("otto.session.dependencies.check_dependencies", _refuse)
    with pytest.raises(DependencyRefusedError):
        async with otto.open_context(lab=_lab_with("test1")):
            pass
    assert seen == [True]
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_dependency_warnings_are_logged(monkeypatch, caplog):
    import otto

    warning = "repo 'r' requires 'x' — not satisfied, but r is inactive for this run"
    _install_result(monkeypatch)
    monkeypatch.setattr("otto.session.dependencies.check_dependencies", lambda ctx: [warning])
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        async with otto.open_context(lab=_lab_with("test1")):
            pass
    assert warning in caplog.text


def test_open_context_has_no_search_paths():
    import inspect

    import otto

    assert "search_paths" not in inspect.signature(otto.open_context.__wrapped__).parameters


# --- open_context selects the product variant, as --field/--debug do ----------


@pytest.mark.asyncio
async def test_the_variant_is_set_for_the_lab_build_and_the_body(monkeypatch):
    """``variant="field"`` is active when the lab is built and inside the body."""
    import otto
    from otto.context import variant

    seen_at_build = []

    def _build_lab(repos, labs):
        seen_at_build.append(variant())
        return _lab_with("test1")

    _install_result(monkeypatch)
    monkeypatch.setattr("otto.session.lab.build_lab", _build_lab)
    async with otto.open_context(lab="test1", variant="field"):
        assert variant() == "field"
    assert seen_at_build == ["field"]
    assert variant() == "debug"


@pytest.mark.asyncio
async def test_the_variant_is_restored_when_the_body_raises(monkeypatch):
    import otto
    from otto.context import variant

    seen = []

    async def _body_raises():
        async with otto.open_context(lab=_lab_with("test1"), variant="field"):
            seen.append(variant())
            raise RuntimeError("body")

    _install_result(monkeypatch)
    with pytest.raises(RuntimeError, match="body"):
        await _body_raises()
    assert seen == ["field"]
    assert variant() == "debug"


@pytest.mark.asyncio
async def test_the_variant_is_restored_when_setup_refuses(monkeypatch):
    import otto
    from otto.context import variant
    from otto.session import ProjectSelectionError

    repo, _ = _broken_repo("Repo2")
    _install_result(monkeypatch, repos=[repo])
    with pytest.raises(ProjectSelectionError):
        async with otto.open_context(
            lab=_lab_with("test1"), include_projects=["nope"], variant="field"
        ):
            pass
    assert variant() == "debug"
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_the_variant_restores_the_previous_value_not_the_default(monkeypatch):
    """A caller's own ``set_variant`` is what an exit returns to."""
    import otto
    from otto.context import reset_variant, set_variant, variant

    _install_result(monkeypatch)
    token = set_variant("field")
    try:
        async with otto.open_context(lab=_lab_with("test1"), variant="debug"):
            assert variant() == "debug"
        assert variant() == "field"
    finally:
        reset_variant(token)


@pytest.mark.asyncio
async def test_no_variant_keeps_the_one_already_set(monkeypatch):
    import otto
    from otto.context import reset_variant, set_variant, variant

    _install_result(monkeypatch)
    token = set_variant("field")
    try:
        async with otto.open_context(lab=_lab_with("test1")):
            assert variant() == "field"
        assert variant() == "field"
    finally:
        reset_variant(token)


@pytest.mark.asyncio
async def test_an_invalid_variant_refuses_before_anything_runs(monkeypatch):
    import otto
    from otto.context import variant

    calls = []
    monkeypatch.setattr(bs, "bootstrap", lambda: calls.append("bootstrap"))
    with pytest.raises(ValueError, match="variant must be 'debug' or 'field'"):
        async with otto.open_context(lab=_lab_with("test1"), variant="release"):  # type: ignore[arg-type]
            pass
    assert calls == []
    assert variant() == "debug"
    assert try_get_context() is None


# --- open_context applies the reservation gate ----------------------------------


def _reservations_repo(tmp_path, holdings: "dict[str, list[str]]"):
    """A repo whose [reservations] is the JSON backend over *holdings* (user -> resources)."""
    import json

    path = tmp_path / "reservations.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "reservations": [
                    {"user": user, "resources": resources} for user, resources in holdings.items()
                ],
            }
        )
    )
    return fake_repo(
        "res",
        sut_dir=tmp_path,
        settings={"reservations": {"backend": "json", "json": {"path": str(path)}}},
    )


def _lab_needing(*resources):
    lab = _lab_with("test1")  # Lab is a mutable dataclass
    lab.resources = set(resources)
    return lab


def _as_user(monkeypatch, name: str) -> None:
    # getpass.getuser() reads LOGNAME first, then USER.
    monkeypatch.setenv("LOGNAME", name)
    monkeypatch.setenv("USER", name)


@pytest.mark.asyncio
async def test_open_context_refuses_an_unheld_resource_with_its_report(tmp_path, monkeypatch):
    import otto
    from otto.reservations import MissingReservationError

    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {"bob": ["rack1"]})])
    _as_user(monkeypatch, "alice")
    with pytest.raises(MissingReservationError) as exc:
        async with otto.open_context(lab=_lab_needing("rack1")):
            pass
    assert [m.resource for m in exc.value.report.missing] == ["rack1"]
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_open_context_passes_when_held_and_holder_changes_who_is_checked(
    tmp_path, monkeypatch
):
    import otto

    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {"bob": ["rack1"]})])
    _as_user(monkeypatch, "alice")
    async with otto.open_context(lab=_lab_needing("rack1"), holder="bob") as ctx:
        assert ctx.lab.name


@pytest.mark.asyncio
async def test_an_empty_holder_is_no_holder(tmp_path, monkeypatch, caplog):
    import otto

    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {"alice": ["rack1"]})])
    _as_user(monkeypatch, "alice")
    with caplog.at_level(logging.INFO, logger="otto.context"):
        # "" falls back to the invoking user (alice, who holds rack1), not a user named "".
        async with otto.open_context(lab=_lab_needing("rack1"), holder=""):
            pass
    assert "acting as" not in caplog.text


@pytest.mark.asyncio
async def test_a_holder_is_logged_as_the_identity_acted_as(tmp_path, monkeypatch, caplog):
    import otto

    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {"bob": ["rack1"]})])
    _as_user(monkeypatch, "alice")
    with caplog.at_level(logging.INFO, logger="otto.context"):
        async with otto.open_context(lab=_lab_needing("rack1"), holder="bob"):
            pass
    messages = [r.getMessage() for r in caplog.records]
    assert "reservations: acting as 'bob' (holder=)" in messages


@pytest.mark.asyncio
async def test_skip_builds_no_backend_and_logs_the_warning(tmp_path, monkeypatch, caplog):
    import otto
    from otto.reservations import factory

    calls = []

    def _never(*a, **k):
        calls.append(True)
        raise AssertionError("no backend may be built under skip")

    monkeypatch.setattr(factory, "build_backend", _never)
    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {})])
    with caplog.at_level(logging.WARNING):
        async with otto.open_context(lab=_lab_needing("rack1"), skip_reservation_check=True):
            pass
    assert calls == []
    assert "skipped" in caplog.text.lower()


@pytest.mark.asyncio
async def test_dry_run_is_gated_too(tmp_path, monkeypatch):
    import otto
    from otto.reservations import MissingReservationError

    _install_result(monkeypatch, repos=[_reservations_repo(tmp_path, {})])
    with pytest.raises(MissingReservationError):
        async with otto.open_context(lab=_lab_needing("rack1"), dry_run=True):
            pass


@pytest.mark.asyncio
async def test_no_reservations_table_is_a_no_op(monkeypatch):
    import otto

    _install_result(monkeypatch, repos=[fake_repo("plain")])
    async with otto.open_context(lab=_lab_needing("rack1")):
        pass


@pytest.mark.asyncio
async def test_an_unbuildable_backend_refuses_before_any_context(tmp_path, monkeypatch):
    import otto
    from otto.context import variant
    from otto.reservations import ReservationBackendError

    class _Boom:
        def __init__(self, **kwargs):
            raise TypeError("cannot connect")

    monkeypatch.setattr(
        "otto.reservations.registry.get_reservation_backend_class", lambda name: _Boom
    )
    repo = fake_repo("res", sut_dir=tmp_path, settings={"reservations": {"backend": "boom"}})
    _install_result(monkeypatch, repos=[repo])
    installed = []
    real_set_context = set_context
    monkeypatch.setattr(
        "otto.context.set_context", lambda ctx: installed.append(ctx) or real_set_context(ctx)
    )
    before = variant()
    with pytest.raises(ReservationBackendError):
        async with otto.open_context(lab=_lab_needing("rack1"), variant="field"):
            pass
    assert installed == []
    assert try_get_context() is None
    assert variant() == before


@pytest.mark.asyncio
async def test_a_backend_failing_at_query_time_uninstalls_the_context(tmp_path, monkeypatch):
    import otto
    from otto.context import variant
    from otto.reservations import ReservationBackendError

    repo = _reservations_repo(tmp_path, {})
    (tmp_path / "reservations.json").write_text("{not json")
    _install_result(monkeypatch, repos=[repo])
    installed = []
    real_set_context = set_context
    monkeypatch.setattr(
        "otto.context.set_context", lambda ctx: installed.append(ctx) or real_set_context(ctx)
    )
    before = variant()
    with pytest.raises(ReservationBackendError):
        async with otto.open_context(lab=_lab_needing("rack1"), variant="field"):
            pass
    assert len(installed) == 1
    assert try_get_context() is None
    assert variant() == before


def _owner_of_test1(host_pattern: str):
    """A repo whose [project] scope claims the hosts matching *host_pattern*."""
    from otto.config.scope import ProjectScopeConfig

    scope = ProjectScopeConfig(
        lab_patterns=[re.compile(".*")], host_patterns=[re.compile(host_pattern)]
    )
    return fake_repo("owner", sut_dir=Path("/repos/owner"), project_scope=scope)


def _lab_whose_test1_needs_slot():
    lab = _lab_with("test1")
    lab.hosts["test1"].resources = {"slot"}
    return lab


@pytest.mark.asyncio
async def test_exclude_projects_narrows_the_reservation_requirement(tmp_path, monkeypatch):
    """An excluded project's hosts leave play, so their resources leave the requirement.

    Without the exclusion the same run refuses on ``slot``, so the pass below is
    the switch's doing and not a requirement that was never there.
    """
    import otto
    from otto.reservations import MissingReservationError

    _as_user(monkeypatch, "alice")
    res = _reservations_repo(tmp_path, {"bob": ["slot"]})
    _install_result(monkeypatch, repos=[res, _owner_of_test1("test1")])

    with pytest.raises(MissingReservationError) as exc:
        async with otto.open_context(lab=_lab_whose_test1_needs_slot()):
            pass
    assert [m.resource for m in exc.value.report.missing] == ["slot"]

    async with otto.open_context(lab=_lab_whose_test1_needs_slot(), exclude_projects=["owner"]):
        pass
