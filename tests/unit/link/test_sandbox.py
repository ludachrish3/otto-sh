"""The throwaway netns sandbox otto link check tests netem in."""

from ipaddress import ip_address, ip_network

import pytest

from otto.check import SWEEP_MIN_AGE_S
from otto.link.sandbox import (
    SUBNET,
    SWEEP_LIST_COMMAND,
    SandboxProblem,
    StaleNamespace,
    fresh_sandbox,
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
    assert (sb.name, sb.veth, sb.peer) == ("otto-check-a1b2c3", "ocka1b2c3", "ocka1b2c3p")
    assert len(sb.peer) <= 15
    assert sandbox_from_name(sb.name) == sb


def test_random_tokens_differ() -> None:
    assert new_sandbox().name != new_sandbox().name


class TestEachSandboxHasItsOwnAddresses:
    """Two sandboxes on one host must never share a /30: the kernel would route both
    runs' probes into whichever namespace's route came first (seen on the bed)."""

    def test_the_first_block_is_the_old_fixed_pair(self) -> None:
        sb = new_sandbox("000000")
        assert (sb.root_ip, sb.ns_ip) == ("198.18.0.1", "198.18.0.2")

    def test_each_token_picks_a_30_of_the_benchmarking_range(self) -> None:
        for token in ("a1b2c3", "ffffff", "000001", "123456"):
            sb = new_sandbox(token)
            root, ns = ip_address(sb.root_ip), ip_address(sb.ns_ip)
            assert root in SUBNET
            assert ns in SUBNET
            assert int(root) % 4 == 1
            assert int(ns) == int(root) + 1

    def test_two_tokens_get_two_blocks(self) -> None:
        a, b = new_sandbox("a1b2c3"), new_sandbox("a1b2c4")
        assert ip_network(f"{a.root_ip}/30", strict=False) != ip_network(
            f"{b.root_ip}/30", strict=False
        )

    def test_setup_addresses_the_veth_pair_from_the_sandbox(self) -> None:
        sb = new_sandbox("a1b2c3")
        cmds = setup_commands(sb)
        assert f"ip addr add {sb.root_ip}/30 dev {sb.veth}" in cmds
        assert f"ip netns exec {sb.name} ip addr add {sb.ns_ip}/30 dev {sb.peer}" in cmds

    def test_a_name_otto_did_not_make_can_still_be_torn_down(self) -> None:
        sb = sandbox_from_name("otto-check-zzz")
        assert teardown_commands(sb)[-1] == "ip netns del otto-check-zzz 2>/dev/null || true"


_NOW = 1_790_000_000


class TestParseStale:
    def test_keeps_only_ours_with_ages_from_the_host_s_own_clock(self) -> None:
        out = (
            "otto-check-a1b2c3 (id: 0)\nblue\notto-check-ffffff\n"
            f"@now {_NOW}\n"
            f"@mtime {_NOW - 40} /var/run/netns/otto-check-a1b2c3\n"
            f"@mtime {_NOW - 3000} /var/run/netns/otto-check-ffffff\n"
        )
        assert parse_stale(out) == [
            StaleNamespace("otto-check-a1b2c3", 40),
            StaleNamespace("otto-check-ffffff", 3000),
        ]

    def test_no_clock_or_no_mtime_is_an_unknown_age(self) -> None:
        assert parse_stale("otto-check-a1b2c3\n") == [StaleNamespace("otto-check-a1b2c3", None)]
        out = f"otto-check-a1b2c3\notto-check-ffffff\n@now {_NOW}\n@mtime 5 /x/otto-check-ffffff"
        assert parse_stale(out) == [
            StaleNamespace("otto-check-a1b2c3", None),
            StaleNamespace("otto-check-ffffff", _NOW - 5),
        ]

    def test_the_listing_is_one_read_only_command_that_asks_the_host_for_its_clock(self) -> None:
        assert "date +%s" in SWEEP_LIST_COMMAND
        assert "stat -c '@mtime %Y %n' /var/run/netns/otto-check-*" in SWEEP_LIST_COMMAND


@pytest.mark.asyncio
async def test_sweep_removes_an_old_namespace_and_leaves_a_young_one() -> None:
    listing = (
        "otto-check-a1b2c3 (id: 0)\notto-check-b0b0b0 (id: 1)\n"
        f"@now {_NOW}\n"
        f"@mtime {_NOW - SWEEP_MIN_AGE_S - 1} /var/run/netns/otto-check-a1b2c3\n"
        f"@mtime {_NOW - 40} /var/run/netns/otto-check-b0b0b0\n"
    )
    host = ScriptedHost("test1").answer("ip netns list", listing)
    swept = await sweep_stale(host)
    assert swept.listed == ["otto-check-a1b2c3", "otto-check-b0b0b0"]
    assert swept.said == [
        (
            "swept leftover sandbox otto-check-a1b2c3 on test1 "
            "(earlier or concurrent run, created 45 min ago)"
        ),
        "left otto-check-b0b0b0 namespace on test1 (created 40 s ago — may be a running check)",
    ]
    assert host.sudo_commands == teardown_commands(sandbox_from_name("otto-check-a1b2c3"))


def _routed(sb, dev: str | None = None) -> ScriptedHost:
    """A host whose ``ip route get`` reaches *sb*'s namespace through *dev* (its own veth)."""
    return ScriptedHost("test1").answer(
        "ip route get", f"{sb.ns_ip} dev {dev or sb.veth} src {sb.root_ip} uid 0\n    cache\n"
    )


@pytest.mark.asyncio
async def test_open_runs_setup_checks_the_route_then_always_tears_down() -> None:
    sb = new_sandbox("a1b2c3")
    host = _routed(sb)
    async with open_sandbox(host, sb) as problem:
        assert problem is None
        host.commands.append("--inside--")
    inside = host.commands.index("--inside--")
    assert host.commands[:inside] == [*setup_commands(sb), f"ip route get {sb.ns_ip}"]
    assert host.commands[inside + 1 :] == teardown_commands(sb)


@pytest.mark.asyncio
async def test_failed_setup_yields_the_failure_and_still_tears_down() -> None:
    host = ScriptedHost("test1").answer(
        "ip netns add", "mount --make-shared /run/netns failed: Operation not permitted", ok=False
    )
    sb = new_sandbox("a1b2c3")
    async with open_sandbox(host, sb) as problem:
        assert problem is not None
        assert problem.clash is None
        assert "Operation not permitted" in (problem.result.value or "")
    assert host.commands == [setup_commands(sb)[0], *teardown_commands(sb)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "via"),
    [
        ("198.18.0.6 dev ockffffff src 198.18.0.5 uid 0\n    cache\n", "ockffffff"),
        ("RTNETLINK answers: Network is unreachable\n", "nothing otto could read"),
    ],
)
async def test_a_sandbox_address_routed_elsewhere_is_a_clash(said: str, via: str) -> None:
    sb = new_sandbox("000001")  # block 1: 198.18.0.4/30
    host = ScriptedHost("test1").answer("ip route get", said)
    async with open_sandbox(host, sb) as problem:
        assert isinstance(problem, SandboxProblem)
        assert problem.clash == (
            f"the sandbox's address 198.18.0.6 on test1 routes via {via}, not ock000001: "
            "another namespace there has the same /30 (another check?)"
        )
    assert host.commands[-3:] == teardown_commands(sb)


class TestAFreshSandboxAvoidsListedNamespaces:
    def test_a_draw_on_a_listed_namespace_s_30_is_drawn_again(self, monkeypatch) -> None:
        from otto.link import sandbox

        draws = iter(["008000", "a1b2c3"])  # 0x8000 is block 0 again, modulo 32768 blocks
        monkeypatch.setattr(sandbox, "_draw_token", lambda: next(draws))
        assert fresh_sandbox(["otto-check-000000", "blue"]) == new_sandbox("a1b2c3")

    def test_the_redraws_are_bounded(self, monkeypatch) -> None:
        from otto.link import sandbox

        drawn: list[str] = []

        def always_block_0() -> str:
            drawn.append("008000")
            return "008000"

        monkeypatch.setattr(sandbox, "_draw_token", always_block_0)
        assert fresh_sandbox(["otto-check-000000"]) == new_sandbox("008000")
        assert len(drawn) == sandbox._DRAWS


@pytest.mark.asyncio
async def test_teardown_runs_when_the_body_raises() -> None:
    host = ScriptedHost("test1")
    sb = new_sandbox("a1b2c3")
    with pytest.raises(RuntimeError, match="boom"):
        async with open_sandbox(host, sb):
            raise RuntimeError("boom")
    assert host.commands[-3:] == teardown_commands(sb)
