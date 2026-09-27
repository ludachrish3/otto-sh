"""``check_tunnel`` with ``--dest`` on a simulated bed: a full proof at a dest that runs socat,
a split proof otherwise.
"""

import pytest

from otto.check import CheckHostUnreachableError, UnmeasuredReason, Verdict
from otto.tunnel.check import check_tunnel, row_order
from otto.tunnel.check_probes import SWEEP_COMMAND, EchoTag, echo_sentinel, handshake_script
from tests.conftest import active_context

from ._check_fakes import HANDSHAKE_MARK, bed
from ._check_helpers import (
    AB,
    DEST,
    EMBEDDED,
    HANDSHAKE_ONLY,
    LAST_SEGMENT,
    NO_ORACLE,
    OLD_NOTE,
    OLD_S,
    PORT,
    SCRATCH,
    _check_procs_left,
    _column,
    _dest_bed,
    _rows,
    _throwaway_id,
    _verdicts,
)


class TestDestDecision:
    @pytest.mark.asyncio
    async def test_a_has_bash_false_dest_is_never_fingerprinted(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        assert the_bed.hosts["d"].commands == []
        assert report.dest_proof == "split"
        assert report.destination is not None
        assert report.destination.host_id == "d"
        assert report.destination.address == "10.0.0.3"
        assert report.destination.fingerprint is None
        assert report.destination.swept == []
        assert [h.host_id for h in report.hops] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_dest_without_socat_is_a_split_proof(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, missing=["socat"], serves=[PORT])
        d = the_bed.hosts["d"]
        leftover = d.plant(echo_sentinel(EchoTag("0ld0ld", "-", "tcp", "segment", "d")))
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert report.dest_proof == "split"
        # Fingerprinted once, and nothing else: never swept, never given an echo.
        [fingerprint] = d.commands
        assert 'echo "kernel=' in fingerprint
        assert leftover in d.procs
        assert report.destination is not None
        assert report.destination.fingerprint is not None
        assert report.destination.fingerprint.tools["socat"] is False
        assert report.destination.range_labels["kernel"] == "within"
        assert [a["dest"] for a in the_bed.adds] == [None]
        assert list(_rows(report))[-1] == LAST_SEGMENT
        assert report.proven == "hop chain: payload-verified; last segment to d: TCP handshake only"

    @pytest.mark.asyncio
    async def test_a_dest_with_socat_but_no_bash_is_a_split_proof(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, missing=["bash"], serves=[PORT])
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert report.dest_proof == "split"
        assert len(the_bed.hosts["d"].commands) == 1


class TestDestFullProof:
    @pytest.mark.asyncio
    async def test_dest_with_socat_is_payload_verified_end_to_end(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        a, b, d = (the_bed.hosts[h] for h in "abd")
        report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        assert report.dest_proof == "full"
        assert report.proven == "whole path payload-verified to d (tcp, udp)"
        assert report.ok
        for protocol in ["tcp", "udp"]:
            rows = _rows(report, protocol)
            # The last leg is a segment like any other; there is no split row.
            assert list(rows)[:3] == ["service port", "segment a → b", "segment b → d"]
            assert LAST_SEGMENT not in rows
            assert set(_verdicts(report, protocol).values()) == {Verdict.PASS}, rows
        # The tunnel is built with --dest, and the fwd echo sits at the dest's own address.
        assert [x["dest"] for x in the_bed.adds] == [DEST, DEST]
        fwd = [c for c in d.commands if ":fwd-echo:" in c]
        assert len(fwd) == 2
        assert all(",bind=10.0.0.3" in c and f":{SCRATCH}," in c for c in fwd)
        assert not any(":fwd-echo:" in c for c in b.commands)
        rev = [c for c in a.commands if ":rev-echo:" in c]
        assert all(",bind=127.0.0.1" in c for c in rev)
        # The segment into the dest: its echo on d at d's address, its trip from b.
        seg = _rows(report, "tcp")["segment b → d"]
        assert any(f"TCP4:10.0.0.3:{SCRATCH}" in c for c in seg.commands)
        assert any(f"TCP4:10.0.0.3:{SCRATCH}" in c for c in b.commands)
        # The dest is fingerprinted, labelled, swept first, torn down and re-scanned.
        assert report.destination is not None
        assert report.destination.fingerprint is not None
        assert set(report.destination.range_labels) == {
            "kernel",
            "isa",
            "userland",
            "socat",
            "bash",
            "launcher",
        }
        first_launch = next(i for i, cmd in enumerate(d.commands) if "otto-check:v1:" in cmd)
        assert d.commands.index(SWEEP_COMMAND) < first_launch
        assert d.commands[-1] == SWEEP_COMMAND
        assert _check_procs_left(the_bed) == {}
        # The scratch port is picked free on the dest too.
        assert the_bed.budget_hosts == [["a", "b", "d"]]
        # --port is only looked at: no handshake, nothing aimed at it.
        commands = [c for h in the_bed.hosts.values() for c in h.commands]
        assert not any(HANDSHAKE_MARK in c for c in commands)
        assert _rows(report, "tcp")["rtt"].measured == "through tunnel 1.0 ms; segments sum 2.0 ms"

    @pytest.mark.asyncio
    async def test_a_dead_leg_into_the_dest_names_its_hop_pair(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        the_bed.dead.add(("b", "d"))
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        row = _rows(report)["segment b → d"]
        assert row.verdict is Verdict.FAIL
        assert row.detail == f"b → d: no echo from 10.0.0.3:{SCRATCH}"
        assert _rows(report)["build"].verdict is Verdict.SKIPPED
        assert the_bed.adds == []
        assert report.proven == "whole path to d: not proven — see the failing rows"

    @pytest.mark.asyncio
    async def test_the_dest_is_swept_and_its_survivors_named(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        d = the_bed.hosts["d"]
        old = d.plant(echo_sentinel(EchoTag("0ld0ld", "-", "tcp", "segment", "d")), age_s=OLD_S)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert old in d.kills
        assert report.destination is not None
        assert report.destination.swept == [f"swept otto-check echo segment on d ({OLD_NOTE})"]
        d.unkillable = True
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.FAIL
        assert "survived on d" in (teardown.detail or "")

    @pytest.mark.asyncio
    async def test_a_leaked_tunnel_keeps_the_dest_s_echo_too(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        tid = _throwaway_id("tcp", hops="ab")
        the_bed.remove_survivors[tid] = [("b", 777)]
        await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        left = _check_procs_left(the_bed)
        assert sorted(left) == ["a", "d"]
        assert all(tid in token for tokens in left.values() for token in tokens)

    @pytest.mark.parametrize("dest", [{}, {"missing": ["socat"]}])
    @pytest.mark.asyncio
    async def test_the_service_port_row_looks_only_where_the_tunnel_listens(
        self, monkeypatch, dest
    ) -> None:
        """The real tunnel binds --port on the first and last hop; the dest's is its service."""
        the_bed = _dest_bed(monkeypatch, **dest)
        the_bed.hosts["d"].held["tcp"] = [f"LISTEN 0 128 0.0.0.0:{PORT} 0.0.0.0:*"]
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        row = _rows(report)["service port"]
        assert row.verdict is Verdict.PASS
        assert [line.split(":")[0] for line in (row.output or "").splitlines()] == ["a", "b"]
        # A holder on the last hop still fails it, dest or not.
        the_bed.hosts["b"].held["tcp"] = [f"LISTEN 0 128 0.0.0.0:{PORT} 0.0.0.0:*"]
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert _rows(report)["service port"].verdict is Verdict.FAIL


class TestDestSplitProof:
    @pytest.mark.asyncio
    async def test_dest_without_socat_splits_the_proof(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        a, b, d = (the_bed.hosts[h] for h in "abd")
        report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        assert report.proven == (
            "hop chain: payload-verified; last segment to d: TCP handshake only; UDP not measured"
        )
        assert report.ok
        for protocol in ["tcp", "udp"]:
            names = [r.feature for r in _column(report, protocol).results]
            assert names == [*row_order(protocol, ["a", "b"]), LAST_SEGMENT]
            chain = {n: v for n, v in _verdicts(report, protocol).items() if n != LAST_SEGMENT}
            assert set(chain.values()) == {Verdict.PASS}
        # The hop chain: built without --dest, delivering to the fwd echo on the last hop.
        assert [x["dest"] for x in the_bed.adds] == [None, None]
        assert all(",bind=127.0.0.1" in c for c in b.commands if ":fwd-echo:" in c)
        assert [c for c in b.commands if ":fwd-echo:" in c]
        # TCP: a handshake from the last hop to --port itself, never the scratch port.
        tcp = _rows(report, "tcp")[LAST_SEGMENT]
        assert tcp.verdict is Verdict.PASS
        assert tcp.detail == HANDSHAKE_ONLY
        assert tcp.commands == [handshake_script("10.0.0.3", PORT)]
        assert tcp.commands[0] in b.commands
        assert not any(HANDSHAKE_MARK in c for c in a.commands)
        # UDP: nothing to check a reply against.
        udp = _rows(report, "udp")[LAST_SEGMENT]
        assert udp.verdict is Verdict.UNMEASURED
        assert udp.reason is UnmeasuredReason.NO_REPLY_ORACLE
        assert udp.detail == NO_ORACLE
        assert udp.commands == []
        # The only contact with the dest is that handshake: no command ever ran on it.
        assert d.commands == []
        everything = [c for h in the_bed.hosts.values() for c in h.commands]
        assert not any("10.0.0.3" in c for c in everything if c != tcp.commands[0])
        assert the_bed.budget_hosts == [["a", "b"]]

    @pytest.mark.asyncio
    async def test_a_refused_handshake_fails_the_last_segment_with_its_output(
        self, monkeypatch
    ) -> None:
        the_bed = _dest_bed(monkeypatch, has_bash=False)  # nothing listens on --port
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        row = _rows(report)[LAST_SEGMENT]
        assert row.verdict is Verdict.FAIL
        assert row.detail == f"b → d: no TCP handshake with 10.0.0.3:{PORT}"
        assert "Connection refused" in (row.output or "")
        assert "handshake failed" in (row.output or "")
        assert report.failed() == [row]
        assert (
            report.proven == "hop chain: payload-verified; last segment to d: TCP handshake failed"
        )

    @pytest.mark.parametrize(
        ("protocol", "said"),
        [
            ("tcp", "last segment to d: TCP handshake only"),
            ("udp", "last segment to d: UDP not measured"),
        ],
    )
    @pytest.mark.asyncio
    async def test_proven_names_only_the_requested_protocols(
        self, monkeypatch, protocol, said
    ) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol=protocol, dest=DEST)
        assert report.proven == f"hop chain: payload-verified; {said}"

    @pytest.mark.asyncio
    async def test_a_hop_missing_a_tool_leaves_the_last_segment_unmeasured(
        self, monkeypatch
    ) -> None:
        the_bed = bed("a", "b", "d", b={"missing": ["socat"]}, d=EMBEDDED).install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        row = _rows(report)[LAST_SEGMENT]
        assert list(_rows(report))[-1] == LAST_SEGMENT
        assert row.verdict is Verdict.UNMEASURED
        assert row.reason is UnmeasuredReason.MISSING_TOOL
        assert report.proven == (
            "hop chain: not measured — socat not found on b; last segment to d: TCP not measured"
        )
        assert the_bed.hosts["d"].commands == []


class TestDestDryRun:
    @pytest.mark.asyncio
    async def test_a_has_bash_false_dest_plans_the_split_proof(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        with active_context(dry_run=True):
            report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        text = "\n".join(report.dry_run_plan)
        assert "has_bash=False" in text
        assert "split proof" in text
        assert "full proof" not in text
        assert f"TCP handshake to d at 10.0.0.3:{PORT}" in text
        assert "no-reply-oracle" in text
        assert report.dry_run_plan[-1] == "no device was contacted — nothing was measured"
        assert all(h.commands == [] for h in the_bed.hosts.values())

    @pytest.mark.asyncio
    async def test_a_dest_with_bash_plans_a_decision_by_its_fingerprint(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        with active_context(dry_run=True):
            report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        text = "\n".join(report.dry_run_plan)
        assert "would fingerprint d" in text
        assert "decided by" in text
        assert "full proof" in text
        assert "fwd echo on d at 10.0.0.3:<scratch>" in text
        assert "split proof" in text
        assert "UDP" not in text
        assert all(h.commands == [] for h in the_bed.hosts.values())


class TestAnUnreachableDestIsNeverAVerdict:
    @pytest.mark.asyncio
    async def test_a_dest_down_at_its_fingerprint_raises_never_splits(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch)
        the_bed.hosts["d"].fail_all = True
        with pytest.raises(CheckHostUnreachableError, match="'d'"):
            await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        assert the_bed.adds == []

    @pytest.mark.asyncio
    async def test_a_dest_dropping_in_teardown_raises_and_the_hops_are_cleaned(
        self, monkeypatch
    ) -> None:
        the_bed = _dest_bed(monkeypatch)
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            the_bed.hosts["d"].fail_all = True
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr("otto.tunnel.check.remove_tunnel", remove)
        with pytest.raises(CheckHostUnreachableError, match="'d'"):
            await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert the_bed.hosts["a"].alive() == []
        assert the_bed.hosts["b"].alive() == []

    @pytest.mark.asyncio
    async def test_a_dest_dropping_at_its_fwd_echo_raises_and_nothing_is_left(
        self, monkeypatch
    ) -> None:
        the_bed = _dest_bed(monkeypatch)
        the_bed.hosts["d"].raise_on = ":fwd-echo:"
        with pytest.raises(CheckHostUnreachableError, match="'d'"):
            await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert the_bed.adds == []
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    async def test_a_last_hop_dropping_at_the_handshake_raises(self, monkeypatch) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        the_bed.hosts["b"].raise_on = HANDSHAKE_MARK
        with pytest.raises(CheckHostUnreachableError, match="'b'"):
            await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert _check_procs_left(the_bed) == {}
        assert the_bed.hosts["d"].commands == []


class TestTheLastSegmentWhenAHopMissesATool:
    @pytest.mark.asyncio
    async def test_a_tool_missing_off_the_last_hop_still_shakes_hands(self, monkeypatch) -> None:
        the_bed = bed("a", "b", "d", a={"missing": ["socat"]}, d=EMBEDDED).install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        tcp = _rows(report, "tcp")
        assert list(tcp)[-1] == LAST_SEGMENT
        assert tcp[LAST_SEGMENT].verdict is Verdict.PASS
        assert tcp[LAST_SEGMENT].detail == HANDSHAKE_ONLY
        assert tcp[LAST_SEGMENT].commands == [handshake_script("10.0.0.3", PORT)]
        assert tcp[LAST_SEGMENT].commands[0] in the_bed.hosts["b"].commands
        assert tcp["build"].reason is UnmeasuredReason.MISSING_TOOL
        udp = _rows(report, "udp")[LAST_SEGMENT]
        assert udp.verdict is Verdict.UNMEASURED
        assert udp.reason is UnmeasuredReason.NO_REPLY_ORACLE
        assert udp.detail == NO_ORACLE
        assert udp.commands == []
        assert report.proven == (
            "hop chain: not measured — socat not found on a; "
            "last segment to d: TCP handshake only; UDP not measured"
        )
        assert the_bed.hosts["d"].commands == []

    @pytest.mark.asyncio
    async def test_a_last_hop_without_socat_leaves_the_handshake_unmeasured(
        self, monkeypatch
    ) -> None:
        the_bed = bed("a", "b", "d", b={"missing": ["socat"]}, d=EMBEDDED).install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, dest=DEST)
        tcp = _rows(report, "tcp")[LAST_SEGMENT]
        assert tcp.verdict is Verdict.UNMEASURED
        assert tcp.reason is UnmeasuredReason.MISSING_TOOL
        assert tcp.detail == "socat not found on b"
        assert tcp.commands == []
        assert not any(HANDSHAKE_MARK in c for c in the_bed.hosts["b"].commands)
        udp = _rows(report, "udp")[LAST_SEGMENT]
        assert udp.reason is UnmeasuredReason.NO_REPLY_ORACLE
        assert udp.detail == NO_ORACLE
        assert the_bed.hosts["d"].commands == []
