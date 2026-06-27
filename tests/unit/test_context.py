import re
from pathlib import Path

import pytest

from otto.context import HostScope, OttoContext, get_context, reset_context, set_context, try_get_context


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


from otto.configmodule.lab import Lab


def _lab_with(*ne_names: str) -> Lab:
    """Build a Lab with real UnixHosts from available NE names in the test lab data."""
    from tests.conftest import make_host
    lab = Lab(name="t")
    for ne in ne_names:
        lab.add_host(make_host(ne))
    return lab


def test_get_host_unknown_id_raises_helpful_keyerror():
    import pytest
    ctx = OttoContext(lab=_lab_with("carrot"))
    with pytest.raises(KeyError, match="Available"):
        ctx.get_host("does-not-exist")


def test_context_get_host_and_all_hosts_resolve_from_lab():
    # Use NEs that exist in tests/lab_data/tech1/hosts.json with creds (Unix hosts):
    # carrot, tomato, pepper, basil
    lab = _lab_with("carrot", "tomato", "pepper")
    ctx = OttoContext(lab=lab)
    first_id = next(iter(lab.hosts))
    assert ctx.get_host(first_id) is lab.hosts[first_id]
    # Filter to only carrot and tomato — not pepper
    ids = {h.id for h in ctx.all_hosts(re.compile("carrot|tomato"))}
    assert ids and all("carrot" in i or "tomato" in i for i in ids)
    # "pepper" should not appear in the filtered result
    assert not any("pepper" in i for i in ids)


def test_context_all_hosts_registers_into_scope():
    lab = _lab_with("carrot")
    ctx = OttoContext(lab=lab)
    hosts = list(ctx.all_hosts())
    assert hosts and all(h in ctx.scope._hosts for h in hosts)


def test_set_and_reset_context_round_trips():
    assert try_get_context() is None
    ctx = OttoContext(lab=_lab_with("carrot"))
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

    async def run(self, cmds, timeout=None):
        self.run_calls.append((cmds, timeout))
        return f"ran:{self.id}"


@pytest.mark.asyncio
async def test_do_for_all_hosts_concurrent_captures_exceptions_per_host():
    lab = _lab_with("carrot", "tomato")
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
    lab = _lab_with("carrot", "tomato")
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
    assert h.run_calls == [(["uname -a"], None)]   # str normalized to a single-element list
    assert results["h1"] == "ran:h1"


def test_context_runtime_flags_default_and_override():
    lab = _lab_with("carrot")
    assert OttoContext(lab=lab).dry_run is False
    assert OttoContext(lab=lab).log_command_output is True
    assert OttoContext(lab=lab, dry_run=True).dry_run is True
    assert OttoContext(lab=lab, log_command_output=False).log_command_output is False


def test_async_typer_command_enters_and_exits_scope():
    from otto.context import OttoContext, reset_context, set_context
    from otto.utils import async_typer_command

    ctx = OttoContext(lab=_lab_with("carrot"))
    fake = _FakeHost("sentinel", connected=True)
    ctx.scope.register(fake)
    token = set_context(ctx)
    try:
        async def _cmd():
            return "ok"
        result = async_typer_command(_cmd)()
        assert result == "ok"
        assert fake.close_calls == 1   # wrapper entered ctx.scope; exit swept the connected host
    finally:
        reset_context(token)


def test_async_typer_command_runs_without_active_context():
    from otto.utils import async_typer_command

    async def _cmd():
        return 42
    assert async_typer_command(_cmd)() == 42   # no context set → still runs, no scope


def test_bare_accessors_delegate_to_active_context():
    import otto.configmodule as cm
    from otto.context import OttoContext, reset_context, set_context

    lab = _lab_with("carrot", "tomato")
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
    lab = _lab_with("carrot")
    host = next(iter(lab.hosts.values()))
    assert host._lab is lab
    copy = dataclasses.replace(host)            # *_options overrides use replace
    assert copy._lab is lab                     # field must carry forward


@pytest.mark.asyncio
async def test_host_async_context_manager_closes_and_close_is_idempotent():
    lab = _lab_with("carrot")
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

    lab = _lab_with("carrot")
    assert try_get_context() is None
    async with otto.open_context(lab=lab) as ctx:
        assert try_get_context() is ctx
        list(ctx.all_hosts())          # registers into ctx.scope
    assert try_get_context() is None   # contextvar reset on exit


@pytest.mark.asyncio
async def test_run_on_all_hosts_accepts_option_overrides():
    """ctx.run_on_all_hosts/do_for_all_hosts accept *_options kwargs without error."""
    from otto.configmodule.lab import Lab

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
    ctx.output_dir = Path('/tmp/otto-run-xyz')
    assert ctx.output_dir == Path('/tmp/otto-run-xyz')
