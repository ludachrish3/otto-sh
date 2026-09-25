"""The throwaway netns sandbox otto link check tests netem in."""

import pytest

from otto.link.sandbox import (
    Sandbox,
    new_sandbox,
    open_sandbox,
    parse_stale,
    sandbox_from_name,
    setup_commands,
    sweep_stale,
    teardown_commands,
)
from tests.unit.check._fakes import ScriptedHost


def test_names_fit_the_15_char_netdev_limit_and_round_trip() -> None:
    sb = new_sandbox("a1b2c3")
    assert sb == Sandbox(name="otto-check-a1b2c3", veth="ocka1b2c3", peer="ocka1b2c3p")
    assert len(sb.peer) <= 15
    assert sandbox_from_name(sb.name) == sb


def test_random_tokens_differ() -> None:
    assert new_sandbox().name != new_sandbox().name


def test_parse_stale_keeps_only_ours() -> None:
    out = "otto-check-a1b2c3 (id: 0)\nblue\notto-check-ffffff\n"
    assert parse_stale(out) == ["otto-check-a1b2c3", "otto-check-ffffff"]


@pytest.mark.asyncio
async def test_sweep_removes_stale_namespaces_and_names_them() -> None:
    host = ScriptedHost("test1").answer("ip netns list", "otto-check-a1b2c3 (id: 0)\n")
    swept = await sweep_stale(host)
    assert swept == ["otto-check-a1b2c3"]
    assert host.sudo_commands[-3:] == teardown_commands(sandbox_from_name("otto-check-a1b2c3"))


@pytest.mark.asyncio
async def test_open_runs_setup_then_always_tears_down() -> None:
    host = ScriptedHost("test1")
    sb = new_sandbox("a1b2c3")
    async with open_sandbox(host, sb) as failure:
        assert failure is None
        host.commands.append("--inside--")
    inside = host.commands.index("--inside--")
    assert host.commands[:inside] == setup_commands(sb)
    assert host.commands[inside + 1 :] == teardown_commands(sb)


@pytest.mark.asyncio
async def test_failed_setup_yields_the_failure_and_still_tears_down() -> None:
    host = ScriptedHost("test1").answer(
        "ip netns add", "mount --make-shared /run/netns failed: Operation not permitted", ok=False
    )
    sb = new_sandbox("a1b2c3")
    async with open_sandbox(host, sb) as failure:
        assert failure is not None
        assert "Operation not permitted" in failure.value
    assert host.commands == [setup_commands(sb)[0], *teardown_commands(sb)]


@pytest.mark.asyncio
async def test_teardown_runs_when_the_body_raises() -> None:
    host = ScriptedHost("test1")
    sb = new_sandbox("a1b2c3")
    with pytest.raises(RuntimeError, match="boom"):
        async with open_sandbox(host, sb):
            raise RuntimeError("boom")
    assert host.commands[-3:] == teardown_commands(sb)
