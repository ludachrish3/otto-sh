"""The per-call host overrides are one table, and every entry forwards all of it.

``HostOverrides`` names the overrides once; ``otto.fleet``'s four entries and
``OttoContext``'s three take ``**overrides`` and forward them, and
``do_for_all_hosts`` splits them from a method's own keywords by the same
table. A new override added to the table reaches every entry without a
second edit — which is what these tests drive, key by key.
"""

import inspect
from unittest.mock import AsyncMock

import pytest

from otto import fleet
from otto.context import OttoContext, reset_context, set_context
from otto.host.options import SshOptions
from otto.labs.loader import Lab
from otto.run_context import HOST_OVERRIDE_KEYS, HostOverrides, _apply_option_overrides
from tests.unit.fleet.test_fleet import make_host


@pytest.fixture
def ctx():
    lab = Lab(name="one-table")
    lab.hosts = {"test1": make_host("test1")}
    context = OttoContext(lab=lab)
    token = set_context(context)
    yield context
    reset_context(token)


def test_the_keys_are_the_table_s_annotations():
    assert frozenset(HostOverrides.__annotations__) == HOST_OVERRIDE_KEYS
    assert {"term", "transfer", "ssh_options", "userland_options"} <= HOST_OVERRIDE_KEYS


@pytest.mark.parametrize("key", sorted(HOST_OVERRIDE_KEYS))
def test_every_fleet_entry_accepts_every_override_by_keyword(key):
    """No entry restates the list: each takes ``**overrides`` (or ``**kwargs``)."""
    for entry in (fleet.all_hosts, fleet.get_host, fleet.do_for_all_hosts, fleet.run_on_all_hosts):
        params = inspect.signature(entry).parameters
        assert key not in params, f"{entry.__name__} spells {key} out instead of forwarding it"
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(HOST_OVERRIDE_KEYS))
async def test_do_for_all_hosts_keeps_every_override_from_the_method(ctx, key):
    """An override shapes the walk; the method sees only its own keywords."""
    method = AsyncMock(return_value="ran")
    value = SshOptions(port=2222) if key == "ssh_options" else None
    results = await fleet.do_for_all_hosts(method, "arg", extra_kw=1, **{key: value})
    assert results == {"test1": "ran"}
    (call,) = method.await_args_list
    assert call.args[1:] == ("arg",)
    assert call.kwargs == {"extra_kw": 1}


def test_a_set_override_reaches_the_host_through_each_entry(ctx):
    override = SshOptions(port=2222)
    assert fleet.get_host("test1", ssh_options=override).ssh_options is override
    (host,) = fleet.all_hosts(ssh_options=override)
    assert host.ssh_options is override


def test_an_unknown_override_is_refused_by_name(ctx):
    with pytest.raises(TypeError, match="ssh_optionz"):
        _apply_option_overrides(ctx.lab.hosts["test1"], ssh_optionz=1)  # type: ignore[call-arg]
