"""``check_tunnel``'s cleanup on a simulated bed: the start-of-run sweep, the teardown, and a
host that stops answering or a check that is interrupted part way.
"""

import asyncio

import pytest

from otto.check import SWEEP_MIN_AGE_S, CheckHostUnreachableError, Verdict
from otto.host.errors import HostUnreachableError
from otto.tunnel.check import check_tunnel
from otto.tunnel.check_probes import SWEEP_COMMAND, EchoTag, echo_sentinel
from otto.tunnel.model import Tunnel, TunnelHop

from ._check_fakes import _LAUNCH_RE, bed
from ._check_helpers import (
    ABC,
    OLD_NOTE,
    OLD_S,
    PORT,
    _check_procs_left,
    _column,
    _rows,
    _throwaway_id,
)


class TestSweep:
    @pytest.mark.asyncio
    async def test_sweep_removes_only_the_check_s_own_leftovers(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        a, c = the_bed.hosts["a"], the_bed.hosts["c"]
        # A killed earlier run: its throwaway tunnel, the fwd echo naming it, a segment echo.
        old = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[old.id] = old
        old_fwd = c.plant(
            echo_sentinel(EchoTag("0ld0ld", old.id, "tcp", "fwd-echo", "c")), age_s=OLD_S
        )
        old_seg = a.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "a")), age_s=OLD_S)
        # A user's real tunnel on the same hosts: never touched.
        user = the_bed.plant_tunnel(["a", "c"], "tcp", 9000)
        user_pids = [p.pid for h in (a, c) for p in h.alive("otto-tunnel")]
        assert len(user_pids) == 2

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert the_bed.removes == [old.id, _throwaway_id("tcp")]
        assert user.id in the_bed.tunnels
        assert old_fwd in c.kills
        assert old_seg in a.kills
        kills = [pid for h in the_bed.hosts.values() for pid in h.kills]
        assert not set(user_pids) & set(kills)
        assert report.hops[2].swept == [
            (f"swept otto-check echo fwd-echo on c ({OLD_NOTE}); removed tunnel {old.id}")
        ]
        assert report.hops[0].swept == [f"swept otto-check echo segment on a ({OLD_NOTE})"]
        assert report.hops[1].swept == []
        # The sweep ran before this run started anything.
        first_launch = next(i for i, cmd in enumerate(c.commands) if "otto-check:v1:" in cmd)
        assert c.commands.index(SWEEP_COMMAND) < first_launch
        assert report.ok


class TestSweepByAge:
    """A leftover younger than ``SWEEP_MIN_AGE_S`` may be a running check's: it is left."""

    @pytest.mark.asyncio
    async def test_a_young_echo_is_left_and_its_tunnel_kept(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        theirs = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[theirs.id] = theirs
        pid = c.plant(echo_sentinel(EchoTag("y0ung0", theirs.id, "tcp", "fwd-echo", "c")), age_s=40)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid in c.procs
        assert pid not in c.kills
        assert theirs.id in the_bed.tunnels
        assert the_bed.removes == [_throwaway_id("tcp")]
        assert report.hops[2].swept == [
            "left otto-check echo fwd-echo on c (started 40 s ago — may be a running check)"
        ]
        assert report.ok

    @pytest.mark.asyncio
    async def test_an_echo_just_under_the_bound_is_left(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        a = the_bed.hosts["a"]
        pid = a.plant(
            echo_sentinel(EchoTag("y0ung0", "-", "udp", "segment", "a")), age_s=SWEEP_MIN_AGE_S
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert pid in a.procs
        assert report.hops[0].swept == [
            "left otto-check echo segment on a (started 45 min ago — may be a running check)"
        ]

    @pytest.mark.asyncio
    async def test_an_echo_whose_age_ps_cannot_say_is_swept(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        old = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[old.id] = old
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", old.id, "tcp", "fwd-echo", "c")), etime="?")

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid in c.kills
        assert the_bed.removes == [old.id, _throwaway_id("tcp")]
        assert report.hops[2].swept == [
            (
                "swept otto-check echo fwd-echo on c (earlier or concurrent run, age unknown); "
                f"removed tunnel {old.id}"
            )
        ]

    @pytest.mark.asyncio
    async def test_a_tunnel_a_younger_run_s_echo_names_is_left_even_beside_an_old_one(
        self, monkeypatch
    ) -> None:
        """Only a tunnel every echo of which is old goes; the old echo itself still goes."""
        the_bed = bed().install(monkeypatch)
        a, c = the_bed.hosts["a"], the_bed.hosts["c"]
        theirs = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[theirs.id] = theirs
        old = c.plant(
            echo_sentinel(EchoTag("0ld0ld", theirs.id, "tcp", "fwd-echo", "c")), age_s=OLD_S
        )
        young = a.plant(
            echo_sentinel(EchoTag("y0ung0", theirs.id, "tcp", "rev-echo", "a")), age_s=9
        )

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert theirs.id in the_bed.tunnels
        assert old in c.kills
        assert young in a.procs
        assert report.hops[2].swept == [
            (
                f"swept otto-check echo fwd-echo on c ({OLD_NOTE}); its tunnel {theirs.id} "
                "was left: a younger echo names it"
            )
        ]
        assert report.hops[0].swept == [
            "left otto-check echo rev-echo on a (started 9 s ago — may be a running check)"
        ]

    @pytest.mark.asyncio
    async def test_a_run_is_as_old_as_its_oldest_echo(self, monkeypatch) -> None:
        """A killed run's young echo (a segment echo it had just started) goes with the rest
        of that run: the run is judged by its oldest echo, on any host, as ``otto link check``
        judges one by its oldest process."""
        the_bed = bed().install(monkeypatch)
        a, c = the_bed.hosts["a"], the_bed.hosts["c"]
        theirs = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[theirs.id] = theirs
        old = c.plant(
            echo_sentinel(EchoTag("5tr4dl", theirs.id, "tcp", "fwd-echo", "c")), age_s=OLD_S
        )
        young = a.plant(echo_sentinel(EchoTag("5tr4dl", "-", "tcp", "segment", "a")), age_s=9)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert theirs.id not in the_bed.tunnels
        assert old in c.kills
        assert young in a.kills
        assert report.hops[2].swept == [
            f"swept otto-check echo fwd-echo on c ({OLD_NOTE}); removed tunnel {theirs.id}"
        ]
        assert report.hops[0].swept == [f"swept otto-check echo segment on a ({OLD_NOTE})"]

    @pytest.mark.asyncio
    async def test_a_run_with_an_echo_of_unknown_age_is_swept_whole(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        a, c = the_bed.hosts["a"], the_bed.hosts["c"]
        unknown = c.plant(echo_sentinel(EchoTag("5tr4dl", "-", "tcp", "segment", "c")), etime="?")
        young = a.plant(echo_sentinel(EchoTag("5tr4dl", "-", "tcp", "segment", "a")), age_s=9)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert unknown in c.kills
        assert young in a.kills
        assert report.hops[0].swept == [
            "swept otto-check echo segment on a (earlier or concurrent run, age unknown)"
        ]

    @pytest.mark.asyncio
    async def test_a_second_run_s_sweep_mid_run_leaves_this_run_alone(self, monkeypatch) -> None:
        """Two checks on the same hosts at once: the second's sweep takes nothing of the first's."""
        from otto.tunnel import check

        the_bed = bed().install(monkeypatch)
        resolved = _capture_resolved(monkeypatch)
        swept: list[dict[str, list[str]]] = []
        real_add = the_bed.add_tunnel

        async def add(lab, hosts, **kw):
            added = await real_add(lab, hosts, **kw)
            swept.append(await check._sweep(the_bed, resolved))
            return added

        monkeypatch.setattr(check, "add_tunnel", add)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        [said] = swept
        assert said["a"] == [
            "left otto-check echo rev-echo on a (started 5 s ago — may be a running check)"
        ]
        assert said["c"] == [
            "left otto-check echo fwd-echo on c (started 5 s ago — may be a running check)"
        ]
        assert the_bed.removes == [_throwaway_id("tcp")]
        assert _rows(report)["teardown"].verdict is Verdict.PASS
        assert report.ok
        assert _check_procs_left(the_bed) == {}


class TestTeardown:
    @pytest.mark.asyncio
    async def test_teardown_runs_even_when_a_payload_step_raises(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        a = the_bed.hosts["a"]
        a.raise_on = "head -c 1400"
        with pytest.raises(CheckHostUnreachableError, match="'a'"):
            await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert the_bed.removes == [_throwaway_id("tcp")]
        assert _check_procs_left(the_bed) == {}
        # After the probe that raised: this run's echoes killed, then every hop re-scanned.
        raised = next(i for i, cmd in enumerate(a.commands) if "head -c 1400" in cmd)
        after = a.commands[raised + 1 :]
        assert any(cmd.startswith("kill ") for cmd in after)
        for host in the_bed.hosts.values():
            assert host.commands[-1] == SWEEP_COMMAND, host.id

    @pytest.mark.asyncio
    async def test_a_surviving_echo_fails_teardown_by_name(self, monkeypatch) -> None:
        the_bed = bed(c={"unkillable": True}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        row = _rows(report)["teardown"]
        assert row.verdict is Verdict.FAIL
        survivors = list(the_bed.hosts["c"].alive())
        assert survivors
        for proc in survivors:
            assert f"pid {proc.pid}" in (row.detail or "")
        assert "on c" in (row.detail or "")
        assert not report.ok


class TestSweepReportsWhatRemovalDid:
    @pytest.mark.asyncio
    async def test_a_leftover_whose_tunnel_is_already_gone_says_so(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        gone = _throwaway_id("tcp", 61500)
        the_bed.hosts["c"].plant(
            echo_sentinel(EchoTag("0ld0ld", gone, "tcp", "fwd-echo", "c")), age_s=OLD_S
        )
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert report.hops[2].swept == [
            (
                f"swept otto-check echo fwd-echo on c ({OLD_NOTE}); "
                f"its tunnel {gone} was already gone"
            )
        ]

    @pytest.mark.asyncio
    async def test_a_leftover_tunnel_that_will_not_die_keeps_its_echo(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        old = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[old.id] = old
        the_bed.remove_survivors[old.id] = [("c", 4242)]
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", old.id, "tcp", "fwd-echo", "c")), age_s=OLD_S)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert pid in c.procs  # still there for the next sweep to retry
        assert pid not in c.kills
        assert report.hops[2].swept == [
            (
                f"kept otto-check echo fwd-echo on c ({OLD_NOTE}); "
                f"removed tunnel {old.id}; tunnel processes survived: pid 4242 on c"
            )
        ]

    @pytest.mark.asyncio
    async def test_an_unrelated_down_lab_host_does_not_pin_a_leftover(self, monkeypatch) -> None:
        """Removing a named leftover scans the whole lab, as ``otto tunnel remove`` does; a
        down host off this path cannot hold any of the leftover tunnel's processes here."""
        the_bed = bed().install(monkeypatch)
        old = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[old.id] = old
        the_bed.remove_unreachable[old.id] = ["zz"]
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", old.id, "tcp", "fwd-echo", "c")), age_s=OLD_S)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert pid in c.kills
        assert pid not in c.procs
        assert report.hops[2].swept == [
            (f"swept otto-check echo fwd-echo on c ({OLD_NOTE}); removed tunnel {old.id}")
        ]

    @pytest.mark.asyncio
    async def test_a_down_hop_on_the_path_keeps_the_leftover(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        old = Tunnel(protocol="tcp", service_port=61500, path=(TunnelHop("a"), TunnelHop("c")))
        the_bed.tunnels[old.id] = old
        the_bed.remove_unreachable[old.id] = ["zz", "b"]
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", old.id, "tcp", "fwd-echo", "c")), age_s=OLD_S)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert pid in c.procs
        assert pid not in c.kills
        assert report.hops[2].swept == [
            (
                f"kept otto-check echo fwd-echo on c ({OLD_NOTE}); "
                f"removed tunnel {old.id}; could not reach b"
            )
        ]


class TestUnreachableIsNeverAVerdict:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "says"),
        [
            (ConnectionError("b is down"), "b is down"),
            (HostUnreachableError("host 'b' timed out launching a tunnel process"), "'b'"),
        ],
    )
    async def test_a_hop_dropping_during_the_build_raises_after_cleanup(
        self, monkeypatch, error, says
    ) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.add_error = error
        with pytest.raises(CheckHostUnreachableError, match=says) as info:
            await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert "a → b → c" in str(info.value)
        assert the_bed.removes == [_throwaway_id("tcp")]
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            HostUnreachableError("host 'p' timed out probing container 'ctr'"),
            ConnectionError("host 'p' reset the connection"),
        ],
    )
    async def test_a_hop_dropping_while_the_path_resolves_raises(self, monkeypatch, error) -> None:
        the_bed = bed().install(monkeypatch)

        async def resolve(lab, hosts):
            raise error

        monkeypatch.setattr("otto.tunnel.check.resolve_chain", resolve)
        with pytest.raises(CheckHostUnreachableError, match="'p'"):
            await check_tunnel(the_bed, ABC, port=PORT)
        assert all(h.commands == [] for h in the_bed.hosts.values())

    @pytest.mark.asyncio
    async def test_a_hop_dropping_only_in_teardown_raises_and_the_rest_is_cleaned(
        self, monkeypatch
    ) -> None:
        the_bed = bed().install(monkeypatch)
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            the_bed.hosts["b"].fail_all = True
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr("otto.tunnel.check.remove_tunnel", remove)
        with pytest.raises(CheckHostUnreachableError, match="'b'"):
            await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert the_bed.hosts["a"].alive() == []
        assert the_bed.hosts["c"].alive() == []


class TestCleanupContracts:
    @pytest.mark.asyncio
    async def test_teardown_kills_only_this_run_s_echoes(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        b = the_bed.hosts["b"]
        other: list[int] = []
        real_add = the_bed.add_tunnel

        async def add(lab, hosts, **kw):
            # Another check run starts its echo after this run's sweep.
            other.append(b.plant(echo_sentinel(EchoTag("0ther0", "-", "tcp", "segment", "b"))))
            return await real_add(lab, hosts, **kw)

        monkeypatch.setattr("otto.tunnel.check.add_tunnel", add)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        [pid] = other
        assert pid in b.procs
        assert pid not in b.kills
        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.PASS
        assert str(pid) not in (teardown.detail or "")

    @pytest.mark.asyncio
    async def test_a_leaked_tunnel_keeps_its_echoes_for_the_next_sweep(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        tid = _throwaway_id("tcp")
        the_bed.remove_survivors[tid] = [("b", 777)]
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.FAIL
        assert "tunnel process pid 777 survived on b" in (teardown.detail or "")
        assert "left for the next check's sweep" in (teardown.detail or "")
        left = _check_procs_left(the_bed)
        assert sorted(left) == ["a", "c"]
        assert all(tid in token for tokens in left.values() for token in tokens)
        assert not [t for tokens in left.values() for t in tokens if ":segment:" in t]

    @pytest.mark.asyncio
    async def test_an_echo_that_never_started_builds_and_removes_nothing(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        the_bed.hosts["c"].answer(":fwd-echo:", "bind: address in use", ok=False)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        build = _rows(report)["build"]
        assert build.verdict is Verdict.FAIL
        assert build.detail == "could not start the fwd-echo echo on c"
        assert the_bed.adds == []
        assert the_bed.removes == []
        assert _rows(report)["teardown"].verdict is Verdict.PASS

    @pytest.mark.asyncio
    async def test_a_cancelled_check_still_tears_down(self, monkeypatch) -> None:
        """Ctrl-C mid-payload, then again mid-teardown: the teardown still completes."""
        the_bed = bed().install(monkeypatch)
        a = the_bed.hosts["a"]
        a.block_on = "head -c 1400"
        real_remove = the_bed.remove_tunnel
        task: list[asyncio.Task] = []

        async def remove(lab, tunnel_id):
            task[0].cancel()  # the second Ctrl-C lands inside the teardown
            await asyncio.sleep(0)
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr("otto.tunnel.check.remove_tunnel", remove)
        task.append(asyncio.create_task(check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")))
        await a.entered.wait()
        task[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task[0]
        assert the_bed.removes == [_throwaway_id("tcp")]
        assert _check_procs_left(the_bed) == {}


def _capture_resolved(monkeypatch) -> list:
    """Record the hops ``check_tunnel`` resolves, for a second run's sweep to scan."""
    from otto.tunnel import check

    captured: list = []
    real = check.resolve_chain

    async def resolve(lab, hosts):
        resolved = await real(lab, hosts)
        captured.extend(resolved)
        return resolved

    monkeypatch.setattr(check, "resolve_chain", resolve)
    return captured


class TestAConcurrentSweepIsNamed:
    """A second check's start-of-run sweep on the same hosts removes this run's tunnel
    and echoes mid-run. The teardown must say so, and ``proven`` must claim nothing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("when", ["after the build", "after the list"])
    async def test_a_second_run_s_sweep_mid_run_fails_teardown_and_proves_nothing(
        self, monkeypatch, when
    ) -> None:
        """This run has outlived the sweep's age bound (patched to 0 here), so the second
        run's sweep takes its echoes and tunnel: the detection must still name them."""
        from otto.tunnel import check

        monkeypatch.setattr("otto.check.sweep.SWEEP_MIN_AGE_S", 0)
        the_bed = bed().install(monkeypatch)
        resolved = _capture_resolved(monkeypatch)
        swept: list[dict[str, list[str]]] = []
        fired: list[bool] = []

        async def second_run_sweeps() -> None:
            fired.append(True)
            swept.append(await check._sweep(the_bed, resolved))

        real_add, real_remove = the_bed.add_tunnel, the_bed.remove_tunnel

        async def add(lab, hosts, **kw):
            added = await real_add(lab, hosts, **kw)
            if when == "after the build":
                await second_run_sweeps()
            return added

        async def remove(lab, tunnel_id):
            if when == "after the list" and not fired:
                await second_run_sweeps()
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "add_tunnel", add)
        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        # The second run really did remove this run's tunnel and both its echoes.
        [said] = swept
        assert any(_throwaway_id("tcp") in line for lines in said.values() for line in lines)
        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.FAIL
        assert teardown.detail == (
            f"tunnel {_throwaway_id('tcp')}, the rev-echo on a and the fwd-echo on c were gone "
            "before teardown — removed mid-run, most likely by another check's sweep on these "
            "hosts, and the payload rows above are not evidence"
        )
        assert (
            report.proven
            == "hop chain: not proven — its tunnel or echoes were gone before teardown"
        )
        assert not report.ok

    @pytest.mark.asyncio
    async def test_the_tunnel_gone_alone_is_named(self, monkeypatch) -> None:
        """A sweep killed between removing the tunnel and killing its echoes."""
        from otto.tunnel import check

        the_bed = bed().install(monkeypatch)
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            the_bed.tunnels.pop(tunnel_id, None)
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert _rows(report)["teardown"].detail == (
            f"tunnel {_throwaway_id('tcp')} was gone before teardown — removed mid-run, most "
            "likely by another check's sweep on these hosts, and the payload rows above are "
            "not evidence"
        )
        assert (
            report.proven
            == "hop chain: not proven — its tunnel or echoes were gone before teardown"
        )
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    async def test_an_echo_gone_alone_is_named(self, monkeypatch) -> None:
        from otto.tunnel import check

        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            [fwd] = [p for p in c.alive() if ":fwd-echo:" in p.token]
            c.procs.pop(fwd.pid)
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert the_bed.tunnels == {}
        assert _rows(report)["teardown"].detail == (
            "the fwd-echo on c was gone before teardown — it exited, or another check's "
            "sweep took it, and the payload rows above are not evidence"
        )
        assert (
            report.proven
            == "hop chain: not proven — its tunnel or echoes were gone before teardown"
        )

    @pytest.mark.asyncio
    async def test_a_full_proof_dest_s_echo_gone_is_named(self, monkeypatch) -> None:
        from otto.tunnel import check

        the_bed = bed("a", "b", "d").install(monkeypatch)
        d = the_bed.hosts["d"]
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            for proc in [p for p in d.alive() if ":fwd-echo:" in p.token]:
                d.procs.pop(proc.pid)
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(
            the_bed, [("a", None), ("b", None)], port=PORT, protocol="tcp", dest=("d", None)
        )
        assert _rows(report)["teardown"].detail == (
            "the fwd-echo on d was gone before teardown — it exited, or another check's "
            "sweep took it, and the payload rows above are not evidence"
        )
        assert report.proven == (
            "whole path to d: not proven — its tunnel or echoes were gone before teardown"
        )

    @pytest.mark.asyncio
    async def test_a_refused_build_is_not_a_concurrent_sweep(self, monkeypatch) -> None:
        """``add_tunnel`` refusing means there never was a tunnel to find at teardown."""
        the_bed = bed().install(monkeypatch)
        the_bed.add_error = ValueError("port 61000 is taken on b")
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert _rows(report)["teardown"].verdict is Verdict.PASS
        assert report.proven == "hop chain: not proven — see the failing rows"


class TestAScratchPortCollision:
    """Another run's echo already holds this run's scratch port: the row fails, naming the
    port, and the teardown does not blame a sweep for an echo that never ran."""

    HELD = "scratch port 61000 on {host} is held by something else (another check?)"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("host", "role", "ip", "row"),
        [
            ("c", "fwd-echo", "127.0.0.1", "build"),
            ("a", "rev-echo", "127.0.0.1", "build"),
            ("b", "segment", "10.0.0.2", "segment a → b"),
        ],
    )
    async def test_another_run_s_echo_on_the_scratch_port_fails_its_row(
        self, monkeypatch, host, role, ip, row
    ) -> None:
        the_bed = bed().install(monkeypatch)
        theirs = the_bed.hosts[host].plant(
            echo_sentinel(EchoTag("0th3r0", "tun-other", "tcp", role, host)),
            ip=ip,
            port=61000,
        )

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        rows = _rows(report)
        assert rows[row].verdict is Verdict.FAIL
        assert rows[row].detail == self.HELD.format(host=host)
        assert the_bed.adds == []
        assert rows["teardown"].verdict is Verdict.PASS, rows["teardown"].detail
        assert not _column(report).concurrently_swept
        assert report.proven == "hop chain: not proven — see the failing rows"
        # The other run's echo is its own business: still running, never killed.
        assert theirs in the_bed.hosts[host].procs
        assert _check_procs_left(the_bed) == {host: [the_bed.hosts[host].procs[theirs].token]}

    @pytest.mark.asyncio
    async def test_an_echo_no_listing_could_confirm_is_not_called_gone(self, monkeypatch) -> None:
        """No ``ss`` or ``netstat`` on c, and the fwd echo died at once: nothing proved it
        ever ran, so the teardown cannot call it removed mid-run."""
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        c.answer("ss -H", "sh: ss: not found", ok=False)
        c.answer(":fwd-echo:", "")  # the launch returns, and nothing keeps running

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.PASS, teardown.detail
        assert not _column(report).concurrently_swept


class TestASweepKillThatDoesNotTake:
    """A leftover the sweep's ``kill`` does not end (another user's echo, say) is never
    called swept: the hop is scanned again and the line says it is still there."""

    REFUSED = (
        "could not remove otto-check echo {role} pid {pid} on c "
        "(kill refused — started by another user?)"
    )

    @pytest.mark.asyncio
    async def test_a_segment_echo_that_survives_its_kill_is_named(self, monkeypatch) -> None:
        the_bed = bed(c={"unkillable": True}).install(monkeypatch)
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid in c.kills
        assert pid in c.procs
        assert report.hops[2].swept == [self.REFUSED.format(role="segment", pid=pid)]

    @pytest.mark.asyncio
    async def test_an_echo_of_an_already_gone_tunnel_that_survives_is_named(
        self, monkeypatch
    ) -> None:
        the_bed = bed(c={"unkillable": True}).install(monkeypatch)
        c = the_bed.hosts["c"]
        gone = _throwaway_id("tcp", 61500)
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", gone, "tcp", "fwd-echo", "c")), age_s=OLD_S)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid in c.procs
        assert report.hops[2].swept == [
            self.REFUSED.format(role="fwd-echo", pid=pid) + f"; its tunnel {gone} was already gone"
        ]

    @pytest.mark.asyncio
    async def test_only_the_hop_that_killed_something_is_scanned_again(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)
        seen: dict[str, int] = {}

        from otto.tunnel import check

        real = check._sweep

        async def sweep(lab, resolved):
            said = await real(lab, resolved)
            seen.update({h.id: h.commands.count(SWEEP_COMMAND) for h in the_bed.hosts.values()})
            return said

        monkeypatch.setattr(check, "_sweep", sweep)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert seen == {"a": 1, "b": 1, "c": 2}
        assert report.hops[2].swept == [f"swept otto-check echo segment on c ({OLD_NOTE})"]


class TestAHopWhosePsCannotList:
    """A ``ps`` that rejects ``-o`` (a busybox one built without it) lists nothing: the check
    says it could not look, never that nothing was there."""

    @pytest.mark.asyncio
    async def test_the_sweep_says_it_could_not_look(self, monkeypatch) -> None:
        the_bed = bed(c={"ps_fails": True}).install(monkeypatch)
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert report.hops[2].swept == ["could not list processes on c; sweep skipped"]
        assert pid not in c.kills
        assert report.hops[0].swept == []

    @pytest.mark.asyncio
    async def test_an_echo_ps_cannot_confirm_fails_its_row_without_blaming_a_collision(
        self, monkeypatch
    ) -> None:
        the_bed = bed(c={"ps_fails": True}).install(monkeypatch)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        row = _rows(report)["segment b → c"]
        assert row.verdict is Verdict.FAIL
        assert row.detail == "could not confirm the echo is this run's: ps on c lists nothing"
        assert "held by something else" not in (row.detail or "")
        assert the_bed.adds == []

    @pytest.mark.asyncio
    async def test_the_teardown_does_not_call_an_echo_it_could_not_look_for_gone(
        self, monkeypatch
    ) -> None:
        """``ps`` on c worked while the echoes started, then failed at teardown."""
        from otto.tunnel import check

        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            c.ps_fails = True
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.FAIL
        assert teardown.detail == (
            "could not list processes on c to confirm this run's echoes there are gone"
        )
        assert not _column(report).concurrently_swept
        assert "gone before teardown" not in (teardown.detail or "")

    @pytest.mark.asyncio
    async def test_a_kill_no_scan_could_confirm_is_not_called_swept(self, monkeypatch) -> None:
        """``ps`` on c listed the leftover, then failed on the scan after its kill."""
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)
        real = c._process

        def process(cmd: str) -> str | None:
            if cmd.startswith("kill "):
                c.ps_fails = True
            return real(cmd)

        c._process = process
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid in c.kills
        assert report.hops[2].swept == [
            f"could not confirm otto-check echo segment pid {pid} on c went: ps on c lists nothing"
        ]


class TestEchoesGoneAloneMayHaveExited:
    """Only the echoes vanished while the tunnel was still there to remove: they may have
    exited on their own, so the row does not pin it on a sweep."""

    @pytest.mark.asyncio
    async def test_both_echoes_gone_say_they_exited_or_were_taken(self, monkeypatch) -> None:
        from otto.tunnel import check

        the_bed = bed().install(monkeypatch)
        real_remove = the_bed.remove_tunnel

        async def remove(lab, tunnel_id):
            for host in the_bed.hosts.values():
                for proc in [p for p in host.alive() if "-echo:" in p.token]:
                    host.procs.pop(proc.pid)
            return await real_remove(lab, tunnel_id)

        monkeypatch.setattr(check, "remove_tunnel", remove)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        assert _rows(report)["teardown"].detail == (
            "the rev-echo on a and the fwd-echo on c were gone before teardown — they exited, "
            "or another check's sweep took them, and the payload rows above are not evidence"
        )


class TestTheOwnershipScanIsOneSample:
    """The scan that confirms an echo is this run's is one sample: a socat that lost its bind
    to another run's echo, but has not exited yet, still passes it. The probes then reach the
    other run's echo, and the teardown is what catches it."""

    @pytest.mark.asyncio
    async def test_an_echo_that_exits_just_after_its_scan_is_caught_at_teardown(
        self, monkeypatch
    ) -> None:
        the_bed = bed().install(monkeypatch)
        c = the_bed.hosts["c"]
        c.plant(
            echo_sentinel(EchoTag("0th3r0", "tun-other", "tcp", "fwd-echo", "c")),
            ip="127.0.0.1",
            port=61000,
        )
        ours: list[int] = []
        process, client = c._process, c._client

        def launch(cmd: str) -> str | None:
            found = _LAUNCH_RE.search(cmd)
            if found is not None and ":fwd-echo:" in cmd:
                # Its bind failed, but it is still listed for a moment.
                ours.append(c.plant(found["token"]))
                return ""
            return process(cmd)

        def probe(cmd: str) -> str | None:
            for pid in ours:
                c.procs.pop(pid, None)
            return client(cmd)

        c._process, c._client = launch, probe
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        rows = _rows(report)
        assert rows["build"].verdict is Verdict.PASS
        assert rows["teardown"].detail == (
            "the fwd-echo on c was gone before teardown — it exited, or another check's sweep "
            "took it, and the payload rows above are not evidence"
        )
        assert (
            report.proven
            == "hop chain: not proven — its tunnel or echoes were gone before teardown"
        )
        assert not report.ok


class TestAKilledEchoGetsTimeToExit:
    """A killed socat can still be listed for a moment while it exits: the scan after a kill
    is repeated, for a bounded time, before anything still listed counts as surviving."""

    @pytest.mark.asyncio
    async def test_a_leftover_slow_to_exit_is_swept(self, monkeypatch) -> None:
        the_bed = bed(c={"slow_exit_scans": 1}).install(monkeypatch)
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert pid not in c.procs
        assert report.hops[2].swept == [f"swept otto-check echo segment on c ({OLD_NOTE})"]

    @pytest.mark.asyncio
    async def test_this_run_s_echoes_slow_to_exit_pass_the_teardown(self, monkeypatch) -> None:
        the_bed = bed(c={"slow_exit_scans": 1}, a={"slow_exit_scans": 1}).install(monkeypatch)

        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        teardown = _rows(report)["teardown"]
        assert teardown.verdict is Verdict.PASS, teardown.detail
        assert _check_procs_left(the_bed) == {}

    @pytest.mark.asyncio
    async def test_an_unkillable_echo_is_still_refused_after_a_bounded_wait(
        self, monkeypatch
    ) -> None:
        from otto.tunnel import _tunnel_echoes, check

        the_bed = bed(c={"unkillable": True}).install(monkeypatch)
        c = the_bed.hosts["c"]
        pid = c.plant(echo_sentinel(EchoTag("0ld0ld", "-", "udp", "segment", "c")), age_s=OLD_S)
        waited: list[float] = []
        real = check._sweep

        async def sweep(lab, resolved):
            said = await real(lab, resolved)
            waited.append(the_bed.now)
            return said

        monkeypatch.setattr(check, "_sweep", sweep)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")

        assert report.hops[2].swept == [
            (
                f"could not remove otto-check echo segment pid {pid} on c "
                "(kill refused — started by another user?)"
            )
        ]
        [settled] = waited
        bound = _tunnel_echoes.KILL_SETTLE_S + _tunnel_echoes._READY_INTERVAL_S
        assert _tunnel_echoes.KILL_SETTLE_S <= settled <= bound
