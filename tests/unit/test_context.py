import asyncio
import logging
import re
from pathlib import Path

import pytest

from otto.context import (
    HostScope,
    OttoContext,
    get_context,
    reset_context,
    set_context,
    try_get_context,
)
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from tests._fixtures.chaos import ChaosPoints, Surface, sweep_cancellation


class _FakeHost:
    """Minimal stand-in for a RemoteHost: has _connected and an idempotent close()."""

    def __init__(self, host_id: str, connected: bool = True):
        self.id = host_id
        self._is_connected = connected
        self.close_calls = 0

    @property
    def _connected(self) -> bool:
        return self._is_connected

    async def close(self) -> None:
        self.close_calls += 1
        self._is_connected = False


@pytest.mark.asyncio
async def test_hostscope_closes_only_connected_hosts():
    scope = HostScope()
    live = _FakeHost("a", connected=True)
    idle = _FakeHost("b", connected=False)
    scope.register(live)
    scope.register(idle)
    async with scope:
        pass
    assert live.close_calls == 1
    assert idle.close_calls == 0


@pytest.mark.asyncio
async def test_hostscope_register_is_deduped():
    scope = HostScope()
    h = _FakeHost("a")
    scope.register(h)
    scope.register(h)
    async with scope:
        pass
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
    async with scope:
        pass
    assert ok.close_calls == 1


@pytest.mark.asyncio
async def test_hostscope_closes_hosts_without_connected_attr():
    """A host lacking the RemoteHost-private ``_connected`` (e.g. a
    DockerContainerHost / LocalHost, which are BaseHosts) must still be closed by
    the scope rather than crash with AttributeError."""

    class _NoConnFlag:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    h = _NoConnFlag()
    assert not hasattr(h, "_connected")
    scope = HostScope()
    scope.register(h)
    async with scope:
        pass
    assert h.close_calls == 1


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


def test_context_all_hosts_registers_into_scope():
    lab = _lab_with("test1")
    ctx = OttoContext(lab=lab)
    hosts = list(ctx.all_hosts())
    assert hosts
    assert all(h in ctx.scope._hosts for h in hosts)


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
    hosts handed out by the view would register into a second
    :class:`~otto.context.HostScope` that nothing closes, and a flag set on the
    context after the view was built (``output_dir``, which the CLI stamps
    per-run) would never reach the repo acting under it.
    """
    lab = _lab_with("test1")
    ctx = OttoContext(lab=lab, dry_run=True)
    view = ctx.for_repo("acme")

    assert view.lab is ctx.lab
    assert view.scope is ctx.scope  # ONE lifecycle scope, or hosts leak
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


def test_bare_accessors_delegate_to_active_context():
    import otto.config as cm
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
        """Minimal BaseHost concrete subclass: tracks close() calls."""

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
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
        list(ctx.all_hosts())  # registers into ctx.scope
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
    still missing.
    """
    import otto
    from tests._fixtures.labdata import lab_data_dir

    fixture = lab_data_dir() / "tech1-inventory"
    home = tmp_path / "home"
    home.mkdir()
    settings = home / "settings.toml"
    settings.write_text(  # sutrepo-exempt: the user-level ~/.otto file, not a SUT repo
        '[inventory]\nbackend = "json"\n'
        f'path = "{fixture / "inventory.json"}"\n'
        f'creds_file = "{fixture / "creds.json"}"\n'
        'supplies = ["ip", "interfaces", "is_virtual", "site", "rack", '
        '"shelf", "board", "os_name"]\n'
    )
    monkeypatch.setenv("OTTO_HOME", str(home))
    async with otto.open_context(lab="unix", search_paths=[fixture]) as ctx:
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
async def test_hostscope_exit_drains_registered_hosts():
    """Scope exit sweeps AND forgets: a second enter/exit cycle (run_command
    is invoked multiple times per command in suite/run.py) must not re-close
    hosts swept by the first."""

    class _NoConnFlag:
        """Host without _connected attr; close() is unconditionally called if not drained."""

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    scope = HostScope()
    h = _NoConnFlag()
    assert not hasattr(h, "_connected")
    scope.register(h)
    async with scope:
        pass
    assert h.close_calls == 1
    async with scope:
        pass
    assert h.close_calls == 1


def test_set_and_reset_cli_context_pair():
    from otto.context import reset_cli_context, set_cli_context

    baseline = try_get_context()
    ctx = OttoContext(lab=_lab_with())
    set_cli_context(ctx)
    assert try_get_context() is ctx
    reset_cli_context()
    assert try_get_context() is baseline
    reset_cli_context()  # idempotent: second reset is a no-op
    assert try_get_context() is baseline


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
    async with scope:
        pass
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
    async with scope:
        pass
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
        async with scope:
            pass
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
        async with scope:
            pass

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


def test_hostscope_rebuild_connections_hits_every_host_with_the_hook():
    scope = HostScope()
    calls: "list[str]" = []

    class _WithHook:
        def __init__(self, name: str) -> None:
            self.id = name

        def rebuild_connections(self) -> None:
            calls.append(self.id)

    class _WithoutHook:
        id = "plain"

    scope.register(_WithHook("a"))
    scope.register(_WithoutHook())
    scope.register(_WithHook("b"))
    scope.rebuild_connections()
    assert calls == ["a", "b"]
