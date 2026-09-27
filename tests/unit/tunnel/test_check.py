"""``check_tunnel`` against a simulated bed (no real hosts).

The doubles live in :mod:`tests.unit.tunnel._check_fakes`: every echo the
check starts is a simulated process that the sweep scan sees, ``kill`` ends
and a probe client only hears back from when the bed would really answer.
"""

from collections.abc import Iterator

import pytest

from otto.check import CheckHostUnreachableError, UnmeasuredReason, Verdict
from otto.tunnel import _tunnel_echoes, _tunnel_rows
from otto.tunnel.carrier import CARRIERS, register_carrier
from otto.tunnel.check import PROTOCOLS, REFUSAL_HINT, check_tunnel, requested_protocols, row_order
from otto.tunnel.manage import PortBudget
from otto.tunnel.socat import NoFreePortError, SocatCarrier
from tests.conftest import active_context

from ._check_fakes import _PickRNG, bed, trips_output
from ._check_helpers import (
    ABC,
    PORT,
    SCRATCH,
    TCP_ROWS,
    _check_procs_left,
    _rows,
    _throwaway_id,
    _verdicts,
)


class TestProtocols:
    def test_requested_protocols(self) -> None:
        assert PROTOCOLS == ["tcp", "udp"]
        assert requested_protocols("both") == ["tcp", "udp"]
        assert requested_protocols("udp") == ["udp"]
        assert requested_protocols("TCP") == ["tcp"]

    @pytest.mark.asyncio
    async def test_an_unknown_protocol_is_a_value_error_before_any_contact(self) -> None:
        the_bed = bed()
        with pytest.raises(ValueError, match="'sctp'"):
            requested_protocols("sctp")
        with pytest.raises(ValueError, match="'sctp'"):
            await check_tunnel(the_bed, ABC, port=PORT, protocol="sctp")
        assert all(h.commands == [] for h in the_bed.hosts.values())

    def test_row_order(self) -> None:
        assert row_order("tcp", ["a", "b", "c"]) == TCP_ROWS
        udp = row_order("udp", ["a", "b"])
        assert udp[:3] == ["service port", "segment a → b", "build"]
        assert "fwd 65000 B" in udp
        assert "rev 65000 B" in udp

    @pytest.mark.asyncio
    async def test_both_runs_tcp_then_udp_in_that_order(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        assert [c.protocol for c in report.columns] == ["tcp", "udp"]
        assert [a["protocol"] for a in the_bed.adds] == ["tcp", "udp"]
        assert the_bed.removes == [_throwaway_id("tcp"), _throwaway_id("udp")]
        # Every TCP probe finished before the first UDP one started.
        clients = [c for c in the_bed.hosts["a"].commands if "TCP4:" in c or "UDP4:" in c]
        kinds = ["udp" if "UDP4:" in c else "tcp" for c in clients]
        assert kinds == sorted(kinds, key=["tcp", "udp"].index)
        assert "tcp" in kinds
        assert "udp" in kinds
        assert [r.feature for r in report.columns[0].results] == TCP_ROWS
        assert set(_verdicts(report, "tcp").values()) == {Verdict.PASS}
        assert set(_verdicts(report, "udp").values()) == {Verdict.PASS}
        assert report.ok
        assert report.proven == "hop chain: payload-verified (tcp, udp)"
        assert report.scratch_port == SCRATCH
        assert report.path == ["a", "b", "c"]
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    async def test_protocol_tcp_runs_one_column(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert [c.protocol for c in report.columns] == ["tcp"]
        assert report.protocols == ["tcp"]
        assert [a["protocol"] for a in the_bed.adds] == ["tcp"]
        assert not any("UDP4" in c for h in the_bed.hosts.values() for c in h.commands)
        assert report.proven == "hop chain: payload-verified (tcp)"

    @pytest.mark.asyncio
    async def test_no_two_host_commands_ever_overlap(self, monkeypatch) -> None:
        """socat's UDP fork listeners cross-deliver two new flows' first datagrams (#471)."""
        the_bed = bed().install(monkeypatch)
        await check_tunnel(the_bed, ABC, port=PORT)
        assert the_bed.peak == 1


@pytest.fixture
def tcp_only_carrier() -> Iterator[str]:
    class TcpOnly(SocatCarrier):
        supported_protocols = frozenset({"tcp"})

    register_carrier("tcp-only", TcpOnly)
    try:
        yield "tcp-only"
    finally:
        CARRIERS.unregister("tcp-only")


class TestRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("hosts", "kw", "tweak", "says"),
        [
            ([("a", None)], {}, {}, "at least 2 hosts"),
            ([("a", None), ("a", None)], {}, {}, "more than once"),
            (ABC, {}, {"b": {"has_bash": False}}, "has_bash=False"),
            (ABC, {"dest": ("b", None)}, {}, "--dest 'b' names a host already in the tunnel"),
            (ABC, {"carrier": "wireguard"}, {}, "Unknown carrier 'wireguard'"),
            ([("a", None), ("zz", None)], {}, {}, "unknown host 'zz'"),
            ([("a", "eth9"), ("b", None)], {}, {}, "no interface 'eth9'"),
            (ABC, {"dest": ("zz", None)}, {}, "unknown host 'zz'"),
        ],
    )
    async def test_a_refused_path_is_the_result_and_touches_no_host(
        self, monkeypatch, hosts, kw, tweak, says
    ) -> None:
        the_bed = bed(**tweak).install(monkeypatch)
        report = await check_tunnel(the_bed, hosts, port=PORT, **kw)
        assert report.refusal is not None
        assert says in report.refusal
        assert report.refusal_hint == REFUSAL_HINT
        assert "otto tunnel add --help" in REFUSAL_HINT
        assert "docs/cli/tunnel/add" in REFUSAL_HINT
        assert report.columns == []
        assert report.hops == []
        assert not report.ok
        assert all(h.commands == [] for h in the_bed.hosts.values())
        assert the_bed.adds == []
        assert the_bed.removes == []

    @pytest.mark.asyncio
    async def test_a_carrier_without_the_protocol_is_refused(
        self, monkeypatch, tcp_only_carrier
    ) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, carrier=tcp_only_carrier)
        assert report.refusal is not None
        assert "does not support protocol 'udp'" in report.refusal
        assert all(h.commands == [] for h in the_bed.hosts.values())
        # The same carrier checks TCP fine.
        tcp = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp", carrier=tcp_only_carrier)
        assert tcp.refusal is None
        assert the_bed.adds[0]["carrier"] == "tcp-only"


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_contacts_nothing_and_names_every_step(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        with active_context(dry_run=True):
            report = await check_tunnel(the_bed, ABC, port=PORT)
        assert all(h.commands == [] for h in the_bed.hosts.values())
        assert the_bed.adds == []
        assert the_bed.removes == []
        assert the_bed.lists == 0
        assert report.columns == []
        assert report.scratch_port is None
        plan = report.dry_run_plan
        steps = [
            "would fingerprint a, b, c (one probe command each)",
            (
                "would sweep leftover otto-check echoes older than 45 min on a, b, c, and "
                "remove the throwaway tunnels they name, scanning the lab for them the way "
                "`otto tunnel remove` does; a younger one may be a running check's, and "
                "would be left"
            ),
            "scratch service port at random from above every hop's ephemeral range",
            "segment echo on b at 10.0.0.2:<scratch> and send it 5 round trips of 1 B from a",
            "segment echo on c at 10.0.0.3:<scratch> and send it 5 round trips of 1 B from b",
            "fwd echo on c at 127.0.0.1",
            "would build a throwaway tunnel a → b → c",
            "5 round trips each of 1 B and 1400 B",
            "would remove the tunnel and every echo",
            "no --dest",
            "no device was contacted — nothing was measured",
        ]
        at = []
        for step in steps:
            hits = [i for i, line in enumerate(plan) if step in line]
            assert hits, f"no plan line says {step!r}: {plan}"
            at.append(hits[0])
        assert at == sorted(at), plan
        assert plan[-1] == "no device was contacted — nothing was measured"
        text = "\n".join(plan)
        assert "rev echo on a at 127.0.0.1" in text
        assert "tcp, udp" in text
        assert "carrier socat" in text
        assert "64 KiB (tcp)" in text
        assert "65000 B (udp)" in text
        assert f"--port {PORT}" in text

    @pytest.mark.asyncio
    async def test_dry_run_names_the_dest(self, monkeypatch) -> None:
        the_bed = bed("a", "b", "d").install(monkeypatch)
        with active_context(dry_run=True):
            report = await check_tunnel(the_bed, ABC[:2], port=PORT, dest=("d", None))
        assert any("--dest d" in line for line in report.dry_run_plan)
        assert any("takes 5 1 B trips from b" in line for line in report.dry_run_plan)
        assert any(
            "would fingerprint d (one probe command)" in line for line in report.dry_run_plan
        )
        assert not any("read-only" in line for line in report.dry_run_plan)
        assert report.dest == "d"
        assert all(h.commands == [] for h in the_bed.hosts.values())


class TestFingerprint:
    @pytest.mark.asyncio
    async def test_a_hop_without_socat_is_missing_tool_not_fail(self, monkeypatch) -> None:
        the_bed = bed(b={"missing": ["socat"]}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        for protocol in ["tcp", "udp"]:
            rows = _rows(report, protocol)
            assert rows["service port"].verdict is Verdict.PASS
            later = [r for name, r in rows.items() if name != "service port"]
            assert later
            for row in later:
                assert row.verdict is Verdict.UNMEASURED, row
                assert row.reason is UnmeasuredReason.MISSING_TOOL
                assert row.detail == "socat not found on b"
        assert report.failed() == []
        assert report.proven == "hop chain: not measured — socat not found on b"
        assert the_bed.adds == []
        assert not any("otto-check:v1:" in c for h in the_bed.hosts.values() for c in h.commands)

    @pytest.mark.asyncio
    async def test_a_missing_tunnel_tool_says_tunnel_add_would_refuse_too(
        self, monkeypatch
    ) -> None:
        """socat and bash are what `tunnel add` itself checks, so the claim holds for them."""
        the_bed = bed(b={"missing": ["socat"]}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        hints = {r.hint for name, r in _rows(report).items() if name != "service port"}
        assert hints == {
            (
                "`otto tunnel add` needs socat on every hop and would refuse this path too; "
                "install it, then check again"
            )
        }

    @pytest.mark.asyncio
    async def test_a_missing_probe_tool_does_not_blame_tunnel_add(self, monkeypatch) -> None:
        """cksum and friends are the check's own needs: `tunnel add` never looks for them."""
        the_bed = bed(b={"missing": ["cksum", "wc"]}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        assert rows["build"].verdict is Verdict.UNMEASURED
        assert rows["build"].detail == "cksum, wc not found on b"
        hints = {r.hint for name, r in rows.items() if name != "service port"}
        assert hints == {
            (
                "the check's probes need cksum, wc on every hop (`otto tunnel add` itself does "
                "not); install them, then check again"
            )
        }

    def test_a_mixed_gap_names_both_needs(self) -> None:
        hint = _tunnel_rows.missing_tool_hint(["wc", "bash"])
        assert hint == (
            "`otto tunnel add` needs bash on every hop and would refuse this path too; "
            "the check's probes need wc on every hop (`otto tunnel add` itself does not); "
            "install them, then check again"
        )

    @pytest.mark.asyncio
    async def test_each_hop_is_fingerprinted_and_labelled(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert [h.host_id for h in report.hops] == ["a", "b", "c"]
        assert [h.address for h in report.hops] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        hop = report.hops[0]
        assert hop.fingerprint is not None
        assert hop.fingerprint.versions["socat"] == "1.8.0.0"
        assert set(hop.range_labels) == {"kernel", "isa", "userland", "socat", "bash", "launcher"}
        assert hop.range_labels["kernel"] == "within"


class TestServicePort:
    @pytest.mark.asyncio
    async def test_a_held_service_port_fails_its_row_only(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        holder = f"LISTEN 0 128 0.0.0.0:{PORT} 0.0.0.0:*"
        the_bed.hosts["c"].held["tcp"] = [holder]
        report = await check_tunnel(the_bed, ABC, port=PORT)
        row = _rows(report, "tcp")["service port"]
        assert row.verdict is Verdict.FAIL
        assert row.measured == f"c: {holder}"
        assert "post-add verify" in (row.hint or "")
        assert "free the port or pick another" in (row.hint or "")
        assert "an otto tunnel" not in (row.detail or "")
        assert _rows(report, "udp")["service port"].verdict is Verdict.PASS
        others = {n: v for n, v in _verdicts(report, "tcp").items() if n != "service port"}
        assert set(others.values()) == {Verdict.PASS}
        assert report.failed() == [row]
        # The user's port was only looked at: nothing was bound or aimed at it.
        commands = [c for h in the_bed.hosts.values() for c in h.commands]
        assert not any(f":{PORT}," in c or f":{PORT} " in c for c in commands if "ss -H" not in c)

    @pytest.mark.asyncio
    async def test_an_otto_tunnel_on_the_service_port_is_named(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        user = the_bed.plant_tunnel(["a", "c"], "tcp", PORT)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["service port"]
        assert row.verdict is Verdict.FAIL
        assert "an otto tunnel already binds it" in (row.detail or "")
        assert user.id in (row.detail or "")
        assert user.id in the_bed.tunnels


class TestScratchPort:
    @pytest.mark.asyncio
    async def test_no_free_scratch_port_raises_no_free_port_error(self, monkeypatch) -> None:
        """Every port above the floor is taken: the documented ``NoFreePortError``, which
        the CLI turns into exit 1 with its message."""
        the_bed = bed().install(monkeypatch)

        async def full(resolved):
            return PortBudget(used=set(range(SCRATCH, 65536)), floor=SCRATCH)

        monkeypatch.setattr("otto.tunnel.check.probe_port_budget", full)
        with pytest.raises(NoFreePortError, match=f"no free port in \\[{SCRATCH}, 65535\\]"):
            await check_tunnel(the_bed, ABC, port=PORT)
        assert the_bed.adds == []

    @pytest.mark.asyncio
    async def test_the_scratch_port_is_above_the_floor_and_never_the_service_port(
        self, monkeypatch
    ) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=SCRATCH)  # --port sits on the floor
        assert report.scratch_port == SCRATCH + 1
        assert [a["port"] for a in the_bed.adds] == [SCRATCH + 1, SCRATCH + 1]
        launches = [c for h in the_bed.hosts.values() for c in h.commands if "otto-check:v1:" in c]
        assert launches
        assert all(f":{SCRATCH + 1},bind=" in c for c in launches)
        assert the_bed.removes == [
            _throwaway_id("tcp", SCRATCH + 1),
            _throwaway_id("udp", SCRATCH + 1),
        ]

    @pytest.mark.asyncio
    async def test_the_scratch_port_is_drawn_from_the_whole_budget_not_hardcoded_to_the_floor(
        self, monkeypatch
    ) -> None:
        """The scratch pick goes through the RNG seam, so it can land anywhere free.

        Two concurrent checks on the same hosts only avoid each other's
        scratch port if each draw can land anywhere free in the budget, not
        always on the lowest free port. Pinning the seam to a port far above
        the floor and getting it back proves ``check_tunnel`` really asks its
        module-level ``_scratch_rng`` rather than reimplementing
        ``pick_free_port``'s lowest-first search.
        """
        the_bed = bed().install(monkeypatch)
        far_port = SCRATCH + 2000
        monkeypatch.setattr("otto.tunnel.check._scratch_rng", _PickRNG(pick=far_port))
        report = await check_tunnel(the_bed, ABC, port=PORT)
        assert report.scratch_port == far_port
        assert [a["port"] for a in the_bed.adds] == [far_port, far_port]


class TestSegments:
    @pytest.mark.asyncio
    async def test_a_dead_segment_names_its_hop_pair_and_skips_the_rest(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.dead.add(("b", "c"))
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        assert rows["segment a → b"].verdict is Verdict.PASS
        dead = rows["segment b → c"]
        assert dead.verdict is Verdict.FAIL
        assert dead.detail == f"b → c: no echo from 10.0.0.3:{SCRATCH}"
        assert "trip 1 timeout" in (dead.output or "")
        assert any(f"TCP4:10.0.0.3:{SCRATCH}" in c for c in dead.commands)
        for name in TCP_ROWS[TCP_ROWS.index("build") : TCP_ROWS.index("teardown")]:
            assert rows[name].verdict is Verdict.SKIPPED, name
            assert "segment b → c" in (rows[name].hint or ""), name
        assert rows["teardown"].verdict is Verdict.PASS
        assert the_bed.adds == []
        assert _check_procs_left(the_bed) == {}
        assert report.proven == "hop chain: not proven — see the failing rows"

    @pytest.mark.asyncio
    async def test_a_segment_sends_every_trip_and_reports_their_median(self, monkeypatch) -> None:
        """Like the tunnel's own 1 B row it is compared with: five trips on one flow, warm."""
        the_bed = bed().install(monkeypatch)
        rtts = [1, 2, 3, 9, 4]
        said = [f"trip {i} ok {i}.000000 {i}.00{ms}000" for i, ms in enumerate(rtts, start=1)]
        the_bed.hosts["a"].answer(
            f"TCP4:10.0.0.2:{SCRATCH}", "\n".join([*said, "trips 5 matched 5"])
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        segment = rows["segment a → b"]
        assert segment.verdict is Verdict.PASS
        assert segment.measured == "5/5 echoed, median 3.0 ms round trip"
        [script] = segment.commands
        assert "i<=5;" in script
        assert rows["rtt"].measured == "through tunnel 1.0 ms; segments sum 4.0 ms"

    @pytest.mark.asyncio
    async def test_a_segment_that_drops_one_trip_fails(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.hosts["a"].answer(
            f"TCP4:10.0.0.2:{SCRATCH}", trips_output(5, clock=True, fail_at=5)
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        segment = _rows(report)["segment a → b"]
        assert segment.verdict is Verdict.FAIL
        assert segment.detail == f"a → b: 4 of 5 trips echoed back from 10.0.0.2:{SCRATCH}"
        assert _rows(report)["build"].verdict is Verdict.SKIPPED

    @pytest.mark.asyncio
    async def test_a_segment_without_a_clock_says_it_echoed(self, monkeypatch) -> None:
        the_bed = bed(a={"clock": False}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert _rows(report)["segment a → b"].measured == "5/5 echoed"
        assert _rows(report)["segment b → c"].measured == "5/5 echoed, median 1.0 ms round trip"

    @pytest.mark.asyncio
    async def test_the_segment_echo_listens_on_the_far_hop_s_own_address(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        await check_tunnel(the_bed, ABC, port=PORT, protocol="udp")
        seg = [c for c in the_bed.hosts["c"].commands if ":segment:" in c]
        assert len(seg) == 1
        assert f"UDP4-RECVFROM:{SCRATCH},bind=10.0.0.3" in seg[0]
        assert not any(":segment:" in c for c in the_bed.hosts["a"].commands)

    @pytest.mark.asyncio
    async def test_an_echo_that_never_listens_fails_its_row(self, monkeypatch) -> None:
        the_bed = bed(c={"silent_echoes": True}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["segment b → c"]
        assert row.verdict is Verdict.FAIL
        assert row.detail == f"the segment echo on c never listened on 10.0.0.3:{SCRATCH}"
        assert _rows(report)["build"].verdict is Verdict.SKIPPED

    @pytest.mark.asyncio
    async def test_an_echo_that_cannot_start_is_its_row_s_evidence(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.hosts["b"].answer("otto-check:v1:", "systemd-run: no such unit", ok=False)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["segment a → b"]
        assert row.verdict is Verdict.FAIL
        assert row.detail == "could not start the segment echo on b"
        assert row.output == "systemd-run: no such unit"


class TestBuild:
    @pytest.mark.asyncio
    async def test_a_build_failure_fails_build_and_skips_the_payloads(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        error = RuntimeError("host 'b' is missing socat (required for tunnels)")
        the_bed.add_error = error
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        build = rows["build"]
        assert build.verdict is Verdict.FAIL
        assert build.detail == str(error)
        assert build.output == str(error)
        for name in TCP_ROWS[TCP_ROWS.index("fwd 1 B") : TCP_ROWS.index("teardown")]:
            assert rows[name].verdict is Verdict.SKIPPED, name
        assert rows["teardown"].verdict is Verdict.PASS
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    async def test_the_echoes_are_up_and_tagged_before_the_tunnel_is_added(
        self, monkeypatch
    ) -> None:
        the_bed = bed().install(monkeypatch)
        seen: list[dict[str, list[str]]] = []
        real_add = the_bed.add_tunnel

        async def add(lab, hosts, **kw):
            seen.append(_check_procs_left(the_bed))
            return await real_add(lab, hosts, **kw)

        monkeypatch.setattr("otto.tunnel.check.add_tunnel", add)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        tid = _throwaway_id("tcp")
        [before] = seen
        assert [t for t in before["c"] if ":fwd-echo:" in t and tid in t]
        assert [t for t in before["a"] if ":rev-echo:" in t and tid in t]
        # Every segment echo is gone by then: the last hop's sits on the address
        # and scratch port the reverse ingress is about to bind.
        assert not [t for tokens in before.values() for t in tokens if ":segment:" in t]
        assert _rows(report)["build"].detail == f"carriers 61001/61002 · {tid}"
        assert _rows(report)["build"].measured is None


class TestPayloads:
    @pytest.mark.asyncio
    async def test_payloads_pass_and_a_short_echo_fails_with_its_numbers(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        c.answer("head -c 65536", "sent=65536 got=1000 mismatch\n1.0 1.2")
        c.answer(
            "head -c 1400",
            "trip 1 ok 1.0 1.001\ntrip 2 ok 2.0 2.001\ntrip 3 ok 3.0 3.001\n"
            "trip 4 mismatch got=12\ntrips 5 matched 3",
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        for name in ["fwd 1 B", "fwd 1400 B", "rev 1 B"]:
            assert rows[name].verdict is Verdict.PASS, name
            assert rows[name].measured == "5/5 echoed byte for byte"
        assert rows["fwd 64 KiB"].verdict is Verdict.PASS
        assert rows["fwd 64 KiB"].measured == "got 65536 of 65536 B"
        short = rows["rev 1400 B"]
        assert short.verdict is Verdict.FAIL
        assert short.measured == "3/5 echoed byte for byte"
        assert "trip 4 mismatch" in (short.output or "")
        bulk = rows["rev 64 KiB"]
        assert bulk.verdict is Verdict.FAIL
        assert bulk.measured == "got 1000 of 65536 B"
        # FWD enters at hop 0's address from hop 0; REV at the last hop's from the last hop.
        assert any(f"TCP4:10.0.0.1:{SCRATCH}" in cmd for cmd in rows["fwd 1 B"].commands)
        assert any(f"TCP4:10.0.0.3:{SCRATCH}" in cmd for cmd in rows["rev 1 B"].commands)
        assert report.proven == "hop chain: not proven — see the failing rows"
        assert not report.ok

    @pytest.mark.asyncio
    async def test_a_dropped_host_mid_payload_is_raised_host_named(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.hosts["c"].raise_on = "cksum"
        with pytest.raises(CheckHostUnreachableError, match="'c'"):
            await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")


class TestRtt:
    @pytest.mark.asyncio
    async def test_rtt_is_the_tunnel_median_next_to_the_segment_sum(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rtt = _rows(report)["rtt"]
        assert rtt.verdict is Verdict.PASS
        assert rtt.measured == "through tunnel 1.0 ms; segments sum 2.0 ms"

    @pytest.mark.asyncio
    async def test_no_clock_keeps_payload_verdicts(self, monkeypatch) -> None:
        old_bash = {"clock": False}
        the_bed = bed(a=old_bash, b=old_bash, c=old_bash).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        for protocol in ["tcp", "udp"]:
            rows = _rows(report, protocol)
            rtt = rows.pop("rtt")
            assert rtt.verdict is Verdict.UNMEASURED
            assert rtt.reason is UnmeasuredReason.NO_CLOCK
            assert {r.verdict for r in rows.values()} == {Verdict.PASS}, rows
        assert report.ok
        assert report.proven == "hop chain: payload-verified (tcp, udp)"


class TestList:
    @pytest.mark.asyncio
    async def test_a_degraded_list_fails_list(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.degraded = True
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["list"]
        assert row.verdict is Verdict.FAIL
        assert row.measured == "degraded (5/6)"
        assert _rows(report)["teardown"].verdict is Verdict.PASS


class TestRttWithoutAnEcho:
    @pytest.mark.asyncio
    async def test_a_clock_but_no_1_byte_echo_skips_rtt(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.hosts["a"].answer(
            # The fwd 1 B trips, aimed into the tunnel at a's own address.
            f"TCP4:10.0.0.1:{SCRATCH},connect-timeout=5 2>&1; }}; m=0; for ((i=1; i<=5; i++)); "
            "do p=$(tr -dc a-z0-9 </dev/urandom 2>/dev/null | head -c 1)",
            "trip 1 timeout\ntrips 5 matched 0",
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        rows = _rows(report)
        assert rows["fwd 1 B"].verdict is Verdict.FAIL
        assert rows["rtt"].verdict is Verdict.SKIPPED
        assert rows["rtt"].hint == "fwd 1 B did not echo"


class TestReadiness:
    @pytest.mark.asyncio
    async def test_a_listener_on_another_address_is_not_the_echo(self, monkeypatch) -> None:
        the_bed = bed(c={"silent_echoes": True}).install(monkeypatch)
        the_bed.hosts["c"].held["tcp"] = [f"LISTEN 0 5 10.9.9.9:{SCRATCH} 0.0.0.0:*"]
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["segment b → c"]
        assert row.detail == f"the segment echo on c never listened on 10.0.0.3:{SCRATCH}"

    @pytest.mark.asyncio
    async def test_an_echo_that_binds_late_but_within_the_deadline_is_waited_for(
        self, monkeypatch
    ) -> None:
        """A slow host (a systemd-run launch on a loaded VM) can take seconds to bind."""
        the_bed = bed(c={"bind_delay_s": 3.0}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert _rows(report)["segment b → c"].verdict is Verdict.PASS
        assert report.ok

    @pytest.mark.asyncio
    async def test_an_echo_that_never_binds_fails_after_the_deadline_with_its_listing(
        self, monkeypatch
    ) -> None:
        the_bed = bed(c={"bind_delay_s": 60.0}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["segment b → c"]
        assert row.verdict is Verdict.FAIL
        assert row.detail == f"the segment echo on c never listened on 10.0.0.3:{SCRATCH}"
        assert row.commands[-1].startswith("ss -Htln")
        # Only this one wait moved the bed's clock: about five seconds, 0.2 s at a time.
        assert 5.0 <= the_bed.now < 5.5
        listings = [cmd for cmd in the_bed.hosts["c"].commands if cmd.startswith("ss -Htln")]
        assert len(listings) >= 25

    @pytest.mark.parametrize(
        ("line", "bound"),
        [
            ("LISTEN 0 5 10.0.0.3:61000 0.0.0.0:*", True),
            ("LISTEN 0 5 *:61000 *:*", True),
            ("LISTEN 0 5 0.0.0.0:61000 0.0.0.0:*", True),
            ("UNCONN 0 0 [::]:61000 [::]:*", True),
            ("udp 0 0 :::61000 :::*", True),
            ("LISTEN 0 5 10.0.0.9:61000 0.0.0.0:*", False),
            ("LISTEN 0 5 10.0.0.3:61001 0.0.0.0:*", False),
        ],
    )
    def test_bound_at_matches_the_address_or_a_wildcard(self, line, bound) -> None:
        assert _tunnel_echoes._bound_at(line, "10.0.0.3", 61000) is bound
