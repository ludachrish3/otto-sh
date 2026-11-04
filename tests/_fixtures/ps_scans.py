"""The three ``ps`` scans' long-argv cases, written once for procps and BusyBox.

Each case plants a harmless ``sleep`` under an argv[0] longer than 200
characters and asks one scan to find it whole. procps cuts ``args`` to
``COLUMNS`` on a pipe unless it is given ``-ww``, and BusyBox rejects ``-w``
and must take the fallback leg, so the same three cases run under
``tests/unit/host/test_ps_scan_width.py`` (the host's procps) and
``tests/busybox/test_applet_contracts.py`` (the pinned BusyBox builds).
"""

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from otto.host.daemon import parse_ps_output, ps_scan_command
from otto.link import check_live
from otto.tunnel.check_probes import CHECK_PREFIX, SWEEP_COMMAND
from otto.utils import wait_for

_DAEMON_PREFIX = "otto-widthprobe"


@dataclass(frozen=True)
class PsScanCase:
    """One scan, the argv[0] it must find, and how to read what it found."""

    command: str
    argv0: str
    findings: Callable[[str, int], list[str]]
    """``(scan stdout, planted pid)`` → what the scan reports for that pid."""
    expected: list[str]


def _tokens(prefix: str) -> Callable[[str, int], list[str]]:
    def read(out: str, pid: int) -> list[str]:
        return [p.token for p in parse_ps_output(out, prefix) if p.pid == pid]

    return read


def _link_tags(out: str, pid: int) -> list[str]:
    return [p.tag for p in check_live._parse_scan(out) if p.pid == pid]


_DAEMON_ARGV0 = f"{_DAEMON_PREFIX}:" + "v" * 200
_TUNNEL_ARGV0 = f"{CHECK_PREFIX}:" + "v" * 200

PS_SCAN_CASES = {
    "daemon": PsScanCase(
        ps_scan_command(_DAEMON_PREFIX), _DAEMON_ARGV0, _tokens(_DAEMON_PREFIX), [_DAEMON_ARGV0]
    ),
    "tunnel-sweep": PsScanCase(
        SWEEP_COMMAND, _TUNNEL_ARGV0, _tokens(CHECK_PREFIX), [_TUNNEL_ARGV0]
    ),
    "link-sweep": PsScanCase(
        check_live.SWEEP_SCAN, "p" * 200 + "-otto-check-abc123", _link_tags, ["otto-check-abc123"]
    ),
}


def run_scan(command: str, env: Mapping[str, str]) -> str:
    """Run *command* under ``/bin/sh`` with exactly *env*; return its stdout."""
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
        env=dict(env),
    ).stdout


def assert_the_scan_reads_the_argv_whole(case: PsScanCase, env: Mapping[str, str]) -> None:
    """Plant *case*'s argv[0], run its scan under *env*, and assert it is found whole."""
    planted = subprocess.Popen(["/bin/bash", "-c", f'exec -a "{case.argv0}" sleep 30'])
    cmdline = Path(f"/proc/{planted.pid}/cmdline")
    try:
        # Until bash execs, the pid's argv is bash's own, which holds argv0 quoted:
        # wait for the exec so the scan sees the planted argv, not that.
        wait_for(
            lambda: cmdline.read_bytes().startswith(case.argv0.encode() + b"\0"),
            10,
            on_timeout=f"pid {planted.pid} never exec'd under the planted argv[0]",
        )
        out = run_scan(case.command, env)
    finally:
        planted.kill()
        planted.wait(timeout=10)
    assert case.findings(out, planted.pid) == case.expected, out
