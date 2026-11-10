"""The chaos guest bed's hostless pins: boot wait, process scan, the shared shell proof.

Nothing here touches test4 or the bed; every probe is monkeypatched.
"""

import asyncio

import pytest

from otto.utils import WaitTimeoutError
from tests.e2e.chaos import _bed

_PS_LINE = (
    "530366 07:11:31 otto-tunnel:v1:tun-4e9f7db3a376-15130:udp:15130:49152:fwd:ingress:0::"
    "test1%2Ctest2 UDP4-LISTEN:15130,bind=10.10.200.11,fork,reuseaddr "
    "TCP4:10.10.200.12:49152"
)


def test_wait_for_new_boot_rides_out_probe_failures_then_returns_the_new_id(monkeypatch) -> None:
    """A guest mid-reboot refuses the probe; that is "not yet", never a failure."""
    answers = iter([OSError("down"), OSError("still down"), "b"])

    def _boot_id() -> str:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(_bed, "chaos_guest_boot_id", _boot_id)
    monkeypatch.setattr(_bed, "_BOOT_POLL_S", 0.01)
    assert _bed.wait_for_new_boot("a", timeout=5.0) == "b"


def test_wait_for_new_boot_times_out_when_the_boot_id_never_changes(monkeypatch) -> None:
    monkeypatch.setattr(_bed, "chaos_guest_boot_id", lambda: "a")
    monkeypatch.setattr(_bed, "_BOOT_POLL_S", 0.01)
    with pytest.raises(WaitTimeoutError):
        _bed.wait_for_new_boot("a", timeout=0.1)


def test_observe_guest_tunnel_processes_decodes_the_sentinel_output(monkeypatch) -> None:
    """The scan is the production sentinel parser over the guest's ps, not a pgrep.

    The stub runs a loop of its own, as the real hopped probe does, so an
    observer that is itself awaited inside a loop fails here the way it failed
    on the bed (``asyncio.run() cannot be called from a running event loop``).
    """
    from otto.tunnel.discovery import DISCOVERY_PS_COMMAND

    seen: list[str] = []

    async def _ps_output() -> str:
        return f"{_PS_LINE}\nnoise that is not a sentinel line\n"

    def _probe_text(cmd: str, **_kw) -> str:
        seen.append(cmd)
        return asyncio.run(_ps_output())

    monkeypatch.setattr(_bed, "chaos_guest_probe_text", _probe_text)
    found = _bed.observe_guest_tunnel_processes()
    assert seen == [DISCOVERY_PS_COMMAND]
    assert [o.parsed.tunnel.service_port for o in found] == [15130]


def _raise(*_a, **_k):
    raise ConnectionError("no route")


def test_both_guest_beds_fail_loud_through_the_one_shell_proof(monkeypatch) -> None:
    """Each bed names its guest and `make qemu-restart` when its probe dies."""
    monkeypatch.setattr(_bed, "busybox_probe_text", _raise)
    with pytest.raises(RuntimeError, match=r"bb1350.*test1.*make qemu-restart"), _bed.busybox_bed():
        pass

    monkeypatch.setattr(_bed, "chaos_guest_probe_text", _raise)
    with (
        pytest.raises(RuntimeError, match=r"ubu2404.*test4.*chaos-qemu-ubu2404.*make qemu-restart"),
        _bed.chaos_guest_bed(),
    ):
        pass


def test_chaos_guest_bed_asserts_the_three_tools(monkeypatch) -> None:
    replies = {
        "echo CHAOS-GUEST-READY": "CHAOS-GUEST-READY",
        "command -v socat bash tc": "/a\n/b\n",
    }
    monkeypatch.setattr(_bed, "chaos_guest_probe_text", lambda cmd, **_k: replies[cmd])
    with pytest.raises(AssertionError, match="socat bash tc"), _bed.chaos_guest_bed():
        pass


@pytest.mark.parametrize("seconds", [75, None])
def test_set_shutdown_hold_needs_no_sudo(monkeypatch, seconds) -> None:
    seen: list[str] = []
    monkeypatch.setattr(_bed, "chaos_guest_probe_text", lambda cmd, **_k: seen.append(cmd) or "")
    _bed.set_shutdown_hold(seconds)
    assert len(seen) == 1
    assert "sudo" not in seen[0]
    assert _bed.CHAOS_GUEST.hold_file in seen[0]
