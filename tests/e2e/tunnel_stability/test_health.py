"""Host-down health detection (spec §4): phantom host (unreachable from the
start) and SIGSTOP wedge (was up, went down mid-life, recovers). No VM is
powered off anywhere; no partition rules are installed."""

import asyncio
import contextlib
import json
import time
from pathlib import Path

import pytest

from otto.config.lab import Lab
from otto.host.login_proxy import Cred
from otto.host.options import SshOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.tunnel import add_tunnel, discover_tunnels, remove_tunnel
from otto.tunnel.discovery import _TUNNEL_HOST_TIMEOUT
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import build_bed_host, cli_sut_dir, run_tunnel_cli
from tests.e2e.tunnel_stability._harness import (
    ARM_SECONDS,
    EXIT,
    INGRESS,
    PORT_PHANTOM_CHAIN,
    PORT_PHANTOM_REAL,
    PORT_SIGSTOP,
    SOAK_CYCLES,
    arm_auto_cont,
    assert_gone,
    assert_sshd_responsive,
    cancel_auto_cont,
    soak_timeout,
    sshd_listener_pid,
)

pytestmark = [
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("link_tunnels_e2e"),
    pytest.mark.timeout(soak_timeout(per_cycle=120.0, base=300.0)),
]

PHANTOM_IP = "10.10.200.99"  # spec §8: must stay outside bed VM allocations
PHANTOM_ID = "phantom"
_SCAN_BUDGET = _TUNNEL_HOST_TIMEOUT + 10.0  # boundedness, measured not assumed


def build_phantom_host() -> UnixHost:
    """A REAL UnixHost at a black-hole ip: real transport, real connect
    timeout (bounded locally at 5s so a phantom scan costs seconds, not the
    full discovery budget)."""
    creds = [Cred(**c) for c in host_data("carrot")["creds"]]
    return UnixHost(
        ip=PHANTOM_IP,
        element="phantom",
        creds=creds,
        term="ssh",
        transfer="scp",
        log=LogMode.QUIET,
        ssh_options=SshOptions(connect_timeout=5),
    )


async def _assert_black_hole() -> None:
    """A live host at PHANTOM_IP is a loud config error, never a false pass."""
    try:
        await asyncio.wait_for(asyncio.open_connection(PHANTOM_IP, 22), timeout=3)
    except (OSError, asyncio.TimeoutError):
        return
    raise AssertionError(
        f"{PHANTOM_IP} answered tcp/22 — the phantom ip is allocated; pick a new one"
    )


@pytest.mark.asyncio
async def test_phantom_host_health_cycle(tunnel_lab, reap_tunnels) -> None:
    """CYCLES x { add-through-phantom fails loud + rolls back; discovery on
    the mixed lab stays bounded, names the phantom, keeps the real tunnel ok;
    remove reports the phantom unreachable }. Cycled because repeated
    timed-out connects are the classic transport/fd leak (the watermark
    fixture is watching)."""
    await _assert_black_hole()
    phantom = build_phantom_host()
    assert phantom.id == PHANTOM_ID, f"phantom host id {phantom.id!r} != {PHANTOM_ID!r}"
    tunnel_lab.add_host(phantom)

    real_chain = [(INGRESS, None), (EXIT, None)]
    for cycle in range(SOAK_CYCLES):
        added = await add_tunnel(tunnel_lab, real_chain, port=PORT_PHANTOM_REAL, protocol="udp")
        reap_tunnels.append(added.tunnel.id)

        # (a) add through the phantom: loud, named, fully rolled back. The
        # loud shape is either the tunnel layer's host-named RuntimeError
        # (probe/tool-check timeout) or the transport's address-named OSError
        # (fast connect failure) — both name the culprit; pin the naming, not
        # the class.
        with pytest.raises(Exception, match=rf"{PHANTOM_ID}|{PHANTOM_IP}"):
            await add_tunnel(
                tunnel_lab,
                [(INGRESS, None), (PHANTOM_ID, None)],
                port=PORT_PHANTOM_CHAIN,
                protocol="udp",
            )
        rollback_check = await discover_tunnels(tunnel_lab)
        assert not any(
            d.tunnel.service_port == PORT_PHANTOM_CHAIN for d in rollback_check.tunnels
        ), f"cycle {cycle}: failed add left processes behind"

        # (b) discovery: bounded, phantom named, real tunnel unaffected.
        started = time.monotonic()
        discovery = await discover_tunnels(tunnel_lab)
        elapsed = time.monotonic() - started
        assert elapsed < _SCAN_BUDGET, f"cycle {cycle}: scan took {elapsed:.1f}s"
        assert PHANTOM_ID in discovery.unreachable, (
            f"cycle {cycle}: unreachable {discovery.unreachable!r}"
        )
        found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
        assert found is not None, f"cycle {cycle}: real tunnel missing from discovery"
        assert found.status == "ok", f"cycle {cycle}: real tunnel not ok: {found.status!r}"

        # (c) remove: phantom reported, real tunnel reaped clean.
        report = await remove_tunnel(tunnel_lab, added.tunnel.id)
        assert PHANTOM_ID in report.unreachable, (
            f"cycle {cycle}: remove unreachable {report.unreachable!r}"
        )
        assert report.survivors == [], f"cycle {cycle}: survivors {report.survivors!r}"
        reap_tunnels.remove(added.tunnel.id)


# --- SIGSTOP wedge: was up, went down mid-life, recovers (spec §4) -----------

_WEDGED_SCAN_BUDGET = _TUNNEL_HOST_TIMEOUT + 15.0


def _fresh_two_host_lab() -> Lab:
    """New host objects => new connections => the wedge actually bites (a
    pooled pre-wedge connection would falsely show the host healthy)."""
    lab = Lab(name="tunnel_sigstop_probe")
    for ne in ("carrot", "tomato"):
        lab.add_host(build_bed_host(ne, ssh_options=SshOptions(connect_timeout=5)))
    return lab


def _cli_sut_pinned_to_ssh(tmp_path: Path) -> Path:
    """``cli_sut_dir`` verbatim, then pin every host's active term to ``ssh``.

    tomato's raw lab data lists ``valid_terms: ["telnet", "ssh"]`` (telnet
    first — it's documented as a telnet/netcat test host). With no pin,
    otto's capability resolver picks ``valid_terms[0]`` — so a CLI process
    loading the UNPATCHED lab.json would run tunnel discovery against tomato
    over TELNET (a separate daemon our SIGSTOP never touches), making the
    "CLI sees the wedge" assertion pass for the wrong reason regardless of
    whether sshd is actually stopped. Pinning ``term`` in the host dict
    (`UnixHostSpec.term`, resolved ahead of `valid_terms[0]`) forces the same
    SSH connection this test wedges."""
    sut = cli_sut_dir(tmp_path)
    lab_json_path = sut / "lab_data" / "lab.json"
    lab_data = json.loads(lab_json_path.read_text())
    for host in lab_data["hosts"]:
        host["term"] = "ssh"
    lab_json_path.write_text(json.dumps(lab_data))
    return sut


@pytest.mark.asyncio
async def test_sigstop_wedge_uncertain_then_recovers(
    tunnel_lab, reap_tunnels, tmp_path: Path
) -> None:
    tomato_ip = host_data("tomato")["ip"]
    control = tunnel_lab.hosts[EXIT]  # established connection; survives the STOP
    added = await add_tunnel(
        tunnel_lab, [(INGRESS, None), (EXIT, None)], port=PORT_SIGSTOP, protocol="udp"
    )
    reap_tunnels.append(added.tunnel.id)
    cli_sut = _cli_sut_pinned_to_ssh(tmp_path)

    pid = await sshd_listener_pid(control)
    stopped = False
    succeeded = False
    try:
        # Arm auto-recovery BEFORE stopping: a failed teardown cannot wedge the bed.
        arm_tag = await arm_auto_cont(control, pid)
        stop_result = await control.exec(f"sudo -n kill -STOP {pid}", timeout=15, log=LogMode.QUIET)
        assert stop_result.is_ok, f"kill -STOP failed: {stop_result.value!r}"
        stopped = True

        # Fresh lab: tomato unreachable, tunnel 'uncertain', scan bounded.
        wedged_lab = _fresh_two_host_lab()
        try:
            started = time.monotonic()
            discovery = await discover_tunnels(wedged_lab)
            elapsed = time.monotonic() - started
            assert elapsed < _WEDGED_SCAN_BUDGET, f"wedged scan took {elapsed:.1f}s"
            assert EXIT in discovery.unreachable, f"unreachable: {discovery.unreachable!r}"
            found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
            assert found is not None, "tunnel vanished during the wedge"
            assert found.health == "uncertain", (
                f"expected 'uncertain' (unknown, not missing), got {found.health!r} "
                f"/ status {found.status!r}"
            )

            # Recovery contract (a): the uncertainty is plainly visible to a
            # human running `otto tunnel list`, not just to the library API.
            # The CLI's own scan pays the wedged host's full connect timeout,
            # so this call is budgeted generously (see the report for the
            # measured duration).
            cli_started = time.monotonic()
            stdout = run_tunnel_cli(cli_sut, "list")
            cli_elapsed = time.monotonic() - cli_started
            assert added.tunnel.id in stdout, (
                f"tunnel id missing from CLI list (took {cli_elapsed:.1f}s):\n{stdout}"
            )
            tunnel_line = next(line for line in stdout.splitlines() if added.tunnel.id in line)
            assert tunnel_line.rstrip().endswith("?"), (
                f"CLI list row for {added.tunnel.id!r} missing the uncertainty "
                f"suffix (took {cli_elapsed:.1f}s): {tunnel_line!r}"
            )

            # Recovery contract (b): remove attempted THROUGH the wedged lab
            # completes WITHOUT raising, reaps the reachable (INGRESS) side,
            # and NAMES the wedged host — an incomplete reap is reported,
            # never silent.
            report = await remove_tunnel(wedged_lab, added.tunnel.id)
            assert EXIT in report.unreachable, (
                f"partial remove did not name {EXIT!r}: unreachable={report.unreachable!r}"
            )
        finally:
            await asyncio.gather(
                *(h.close() for h in wedged_lab.hosts.values()), return_exceptions=True
            )

        # Recover, then prove it through ANOTHER fresh lab.
        cont_result = await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        assert cont_result.is_ok, f"kill -CONT failed: {cont_result.value!r}"
        stopped = False
        await assert_sshd_responsive(tomato_ip)

        # The id is STILL discoverable — only the exit host's processes
        # survived the partial reap above (tomato was unreachable when we
        # tried to kill through it), which is what makes the completing
        # remove below meaningful rather than a no-op.
        recovered_lab = _fresh_two_host_lab()
        try:
            discovery = await discover_tunnels(recovered_lab)
            found = next((d for d in discovery.tunnels if d.tunnel.id == added.tunnel.id), None)
            assert found is not None, "tunnel vanished after recovery"
            assert found.uncertain is False, (
                f"expected certainty post-recovery (both hosts reachable), "
                f"got uncertain={found.uncertain!r}"
            )
            assert len(found.present) == 2, (
                f"expected only the exit host's 2 surviving processes, "
                f"got {len(found.present)}: {found.present!r}"
            )
            assert found.status.startswith("degraded ("), (
                f"expected a half-reaped 'degraded (' status, got {found.status!r}"
            )

            # Completing remove: the survivors are finally gone.
            report = await remove_tunnel(recovered_lab, added.tunnel.id)
            assert report.survivors == [], (
                f"survivors after completing remove: {report.survivors!r}"
            )
            await assert_gone(recovered_lab, added.tunnel.id)
        finally:
            await asyncio.gather(
                *(h.close() for h in recovered_lab.hosts.values()), return_exceptions=True
            )
        reap_tunnels.remove(added.tunnel.id)
        succeeded = True
    finally:
        if stopped:  # test body failed mid-wedge: recover NOW, loudly if we can't
            with contextlib.suppress(Exception):
                await control.exec(f"sudo -n kill -CONT {pid}", timeout=15, log=LogMode.QUIET)
        try:
            await assert_sshd_responsive(tomato_ip)
        except Exception as exc:
            raise AssertionError(
                f"tomato sshd is NOT responsive after the SIGSTOP test — the armed "
                f"auto-CONT fires within {ARM_SECONDS}s; if it doesn't, run "
                f"'sudo kill -CONT {pid}' on test2 or 'make vm-health': {exc!r}"
            ) from exc
        if succeeded:
            # Recovery is fully proven (both the explicit CONT above AND this
            # probe succeeded, AND every assertion in the wedge/recovery body
            # passed) — the armed safety net has no job left to do. Cancel it
            # so a later `make stability-tunnel COUNT=N` iteration's own STOP
            # window can never be un-stuck by THIS run's orphaned sleeper.
            # On any failure path (probe failed, CONT failed, or an earlier
            # assertion failed) `succeeded` stays False and the timer is left
            # armed — it IS the safety net for exactly that case.
            await cancel_auto_cont(control, arm_tag)
