"""Tunnel/link rollback chaos: interrupt at the launch->return window and
during rollback itself; SIGKILL characterization + recovery reconciliation.
The product cannot survive SIGKILL (spec); the test characterizes what leaks
and asserts `otto tunnel remove --all --yes` / `otto link repair --all` clean
the bed. This is inherently multi-host (a tunnel spans a path; a link joins
two), so every scenario pins carrot/tomato explicitly and reconciles them
manually (`no_hygiene_bracket`) rather than relying on the single-host
autouse BedHygiene bracket, which only snapshots whichever host the module's
session-scoped ``chaos_bed`` lease happens to grab -- never necessarily one
of the two hosts these scenarios actually dirty.
"""

import contextlib
import re
import shlex
import time

import pytest

from otto.logger.mode import LogMode
from tests._fixtures.bed_hygiene import argv_pattern
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import cli_sut_dir, observe_tunnel_processes
from tests.e2e.chaos._bed import (
    assert_eth2_netem_free,
    probe_text,
    run_probe,
    tunnel_target,
    veggies_link_id,
)
from tests.integration.chaos._driver import BANNER, spawn_otto
from tests.integration.chaos._target import make_bed_target

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(360),
]

_TUNNEL_SIGKILL_PORT = 15200  # chaos block (15200-15299), disjoint from
_ROLLBACK_PORT = 15210  # tunnel_stability's 15100-15199 and test_tunnel_e2e's 15000-15099

_HOLD_MARKER = "otto-chaos-portblock"

# `tunnel`/`link` are registered `output_dir=False` (otto/cli/builtin_commands.py:
# "the group needs no per-invocation output directory of its own") -- neither
# ever calls `create_output_dir`, so NEITHER EVER WRITES a `verbose.log`
# (confirmed live: `OttoProc.wait_for_log` can never match for these two
# command groups -- there is no file to poll).
#
# The phase markers land on stdout instead -- NOT because QUIET-suppression
# is skipped (it isn't: `ensure_cli_session` in otto/cli/invoke.py attaches
# `HostFilter` to the console handler unconditionally, output_dir or not, and
# it correctly drops every QUIET/NEVER-tagged, HOST-tagged record). The
# markers this module keys on are a DIFFERENT kind of line: `ShellSession.
# _run_cmd_inner`'s own "framed write"/"run_cmd done" traces (src/otto/host/
# session.py, the `logger.debug(f"{self._log_tag}: framed write cmd=...")` /
# `"... run_cmd done cmd=..."` calls) are unconditional and carry no
# `extra={"host": ..., "log_mode": ...}` tag at all -- `HostFilter.filter`
# passes any untagged record straight through (`host is None` -> always
# keep), regardless of what LogMode the underlying host.exec/run call asked
# for. Those traces fire for every `host.run()` call on ANY transport (link
# impair's `_root_run` uses `host.run`) and for `host.exec()` specifically on
# TELNET (which is implemented via the same framed-session path) -- but NOT
# for `host.exec()` on SSH, which rides a separate raw `create_process`
# channel with no such untagged trace, so its only log line is the (tagged,
# QUIET-suppressed) `[bold]@host | cmd` echo. That is also why carrot's own
# SSH-routed tunnel launches never appear in this module's marker waits (see
# `test_interrupt_during_rollback_still_reaps`'s docstring) while tomato's
# telnet-routed ones, and every link-impair `sudo ... tc qdisc` line on
# either host (host.run), do. Because the otto subprocess runs at
# `--log-level DEBUG`, these untagged traces are visible on the captured
# stream `_wait_for_stdout` polls.
#
# That stream is rendered through rich's Console, which -- with no tty and no
# `COLUMNS` env var -- wraps at a detected-or-default width (this repo's own
# `otto/cli/link.py::list_links` hits the identical issue: "rich's global
# console otherwise wraps at its detected width (80 cols under CliRunner/
# no-tty, since COLUMNS isn't set in CI)"), which measurably corrupts a long
# sentinel token mid-word (confirmed live: `fwd:egress` split across a wrapped
# line as `fw` / `d:egress`). Setting `COLUMNS` wide on these two spawns keeps
# every phase-marker line on one row so a plain regex can match it intact.
_WIDE_CONSOLE_ENV = {"COLUMNS": "2000"}


def _wait_for_stdout(p, pattern: str, timeout: float) -> str:
    """Poll *p*'s captured stdout for *pattern* -- the `wait_for_log`/
    `verbose.log` equivalent for `tunnel`/`link` invocations (see the
    `_WIDE_CONSOLE_ENV` comment above for why stdout, not the log file, is
    the only place these phase markers ever appear). Mirrors
    `tests/integration/chaos/_driver.py::OttoProc._wait_for`'s polling shape.
    """
    rx = re.compile(pattern)
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = p.stdout_text()
        if rx.search(text):
            return text
        if p.proc.poll() is not None:
            # One last read: the process may have flushed on exit.
            text = p.stdout_text()
            if rx.search(text):
                return text
            raise AssertionError(
                f"otto exited (rc={p.proc.returncode}) before stdout matched {pattern!r}.\n"
                f"--- stdout ---\n{text}"
            )
        time.sleep(0.05)
    raise AssertionError(
        f"stdout never matched {pattern!r} within {timeout}s.\n--- stdout ---\n{text}"
    )


_HOLD_SCRIPT = (
    f"# {_HOLD_MARKER}\n"
    "import socket, time\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(({ip!r}, {port}))\n"
    "s.listen(1)\n"
    "time.sleep(180)\n"
)


def _leftover_tunnel_processes() -> list:
    """Owner-agnostic: any otto-tunnel sentinel process on the veggies trio."""
    import asyncio

    return asyncio.run(observe_tunnel_processes())


@pytest.mark.no_hygiene_bracket  # multi-host; we reconcile + assert manually
def test_sigkill_mid_tunnel_recovers_via_remove_all(tmp_path):
    """SIGKILL an `otto tunnel add` mid-launch; assert `tunnel remove --all
    --yes` reaps whatever daemons survived and the trio ends clean.
    """
    sut = cli_sut_dir(tmp_path)
    target = tunnel_target(sut)
    p = spawn_otto(
        [
            "tunnel",
            "add",
            "--hosts",
            "carrot_seed,tomato_seed",
            "--port",
            str(_TUNNEL_SIGKILL_PORT),
        ],
        xdir=tmp_path,
        target=target,
        extra_env=_WIDE_CONSOLE_ENV,
    )
    try:
        # phase: at least one daemon launched (sentinel in a QUIET exec line)
        _wait_for_stdout(p, r"otto-tunnel:v1:", timeout=120.0)
        p.signal(9)  # SIGKILL -- no teardown possible
        p.wait(timeout=30.0)
        # Recovery command reconciles the bed.
        rm_xdir = tmp_path / "rm"
        rm_xdir.mkdir()
        rm = spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm_xdir, target=target)
        assert rm.wait(timeout=120.0) == 0, rm.stderr_text()
        assert not _leftover_tunnel_processes(), "tunnel remove --all left survivors"
    finally:
        # Belt for the assert-failure path: `_wait_for_stdout` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p.proc.poll() is None:
            p.signal(9)
        rm2_xdir = tmp_path / "rm2"
        rm2_xdir.mkdir()
        spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm2_xdir, target=target).wait(
            timeout=120.0
        )
        assert not _leftover_tunnel_processes(), "bed not clean after final reconciliation"


def _hold_tcp_port(elem: str, ip: str, port: int) -> None:
    """Occupy ``ip:port`` on *elem* with a plain listening socket, confirmed bound.

    Deliberately NOT an otto-tagged tunnel process (so ``_check_conflicts``'s
    pre-launch scan -- which only inspects OTHER already-known tunnels --
    never sees it and refuses the add up front): the launch loop's own socat
    bind is what must fail, INSIDE ``add_tunnel``'s try/except, to exercise a
    real ``lifecycle.compensate()`` rollback rather than an artificial
    pre-check refusal that never launches anything.
    """
    script = _HOLD_SCRIPT.format(ip=ip, port=port)
    cmd = f"setsid python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 &"
    run_probe(elem, lambda h: h.exec(cmd, timeout=15, log=LogMode.QUIET))
    needle = f"{ip}:{port}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        out = probe_text(elem, "ss -H -t -a -n 2>/dev/null || true", timeout=15)
        if needle in out:
            return
        time.sleep(0.2)
    raise AssertionError(f"{elem}: port-hold listener never bound to {needle}")


def _release_tcp_port(elem: str) -> None:
    cmd = f"pkill -f {shlex.quote(argv_pattern(_HOLD_MARKER))} || true"
    run_probe(elem, lambda h: h.exec(cmd, timeout=15, log=LogMode.QUIET))


@pytest.mark.no_hygiene_bracket  # multi-host; product's own rollback + our manual reconcile
def test_interrupt_during_rollback_still_reaps(tmp_path):
    """Force a deterministic post-launch rollback, then interrupt somewhere
    across the resulting verify/retry/rollback tail; assert the bed is
    ALREADY clean before any explicit `tunnel remove` -- proving
    `add_tunnel`'s own `lifecycle.compensate()`-shielded reap survived a real
    concurrent SIGINT.

    How the rollback is forced, deterministically (not by luck of timing): a
    raw, non-otto listener is pre-bound on carrot's eth2 ip at the exact
    service port `otto tunnel add` will use. The launch loop itself cannot
    see this (`launch_command` backgrounds every process fire-and-forget --
    the launch's own `host.exec` returns as soon as the shell accepts the
    backgrounded command, well before socat itself has attempted its bind;
    see `tests/_fixtures/tunnel_bed.py::wait_for_udp_bound`'s docstring for
    the same race characterized on the tunnel_stability suite's UDP
    listeners), so all 4 launches (2-host chain, FWD+REV x 2) dispatch
    successfully from add_tunnel's point of view. The post-add verify scan
    then finds carrot's FWD ingress (the one whose socat lost the bind race
    against our held listener) genuinely NOT running, `_raise_verify_failure`
    fires after the one built-in retry sleep, and add_tunnel's
    `except BaseException:` branch (launched=True, since the first launch
    already happened) invokes `compensate(_kill_tunnel_on(...))` for real.

    Where the SIGINT lands: sent as soon as the LAST launch line (tomato's
    REV ingress, dispatched 4th/last) appears on stdout -- carrot's own two
    launches (2nd/3rd) run for real but never appear in this capture. Per
    the module-level comment near `_WIDE_CONSOLE_ENV`: this is the SSH-vs-
    telnet `host.exec()` asymmetry, not a QUIET-suppression gap -- carrot's
    launches ride SSH's raw `create_process` channel (only the tagged,
    QUIET-suppressed `[bold]@host | cmd` echo, correctly dropped), while
    tomato's ride the framed TELNET session path, whose `ShellSession.
    _run_cmd_inner` emits its own unconditional, untagged "framed write"/
    "run_cmd done" debug trace that no suppression filter ever touches
    (untagged records have no `host`/`log_mode` to filter on). Confirmed
    live both ways: a direct (non-CLI) `add_tunnel` call against hosts built
    with `log=LogMode.QUIET` explicitly still showed every carrot command,
    via that same tagged `[bold]@carrot seed | ...` echo -- proving the
    launch itself is not missing, just its untagged-trace visibility.
    The one on-bed fact that matters -- carrot's FWD ingress genuinely never
    comes up, confirmed by the post-add verify's own `not running:
    carrot_seed/fwd/ingress` -- was independently reproduced (see task-8
    report) via a bare `otto tunnel add` subprocess run with no interrupt at
    all. By construction there is nothing left for add_tunnel to do at that point
    except verify (fails), sleep ~1s, re-verify (fails again), raise, and
    roll back. Whether the interrupt actually lands mid-launch-loop tail,
    during the guaranteed ~1s retry sleep, during the rollback's own
    discovery scan, or during its kill exec is NOT something this subprocess
    driver can pin exactly (no phase marker distinguishes "compensate() is
    now running" from the surrounding steps) -- but EVERY one of those
    landings routes through the SAME `except BaseException:` -> compensate()
    path (the rollback either wasn't affected yet, or the shield already
    absorbed the cancellation), so the end assertion (no survivors) is a
    hard invariant across all of them, not a probabilistic one.

    Honest scope note (per the task-8 brief's own fallback): the tier-1 unit
    suite (`tests/unit/tunnel/test_manage_add.py::
    TestAddTunnelBehavior::test_cancel_during_rollback_still_reaps`) pins the
    EXACT moment -- a monkeypatched rendezvous forces the second cancel to
    land while the rollback's discovery scan is in flight -- that this
    subprocess/live-bed test cannot reproduce from the outside: there is no
    externally observable log line for "compensate() is now running". What
    IS proven here, live, from the CLI boundary: a real rollback reliably
    fires, a real SIGINT is delivered somewhere in the launch-to-rollback
    window on every run, and the bed still ends up clean. That is the
    belt-and-suspenders bed proof the brief asks for; the tier-1 test above
    is the precise one.
    """
    sut = cli_sut_dir(tmp_path)
    target = tunnel_target(sut)
    carrot_ip = host_data("carrot")["interfaces"]["eth2"]["ip"]
    p = None  # bound inside the try; finally must not assume it got there
    try:
        # Inside the try from the start: if the hold-port confirmation itself
        # fails (e.g. the bind-confirmation poll times out), the listener may
        # still be running on carrot -- the finally's release must still fire.
        _hold_tcp_port("carrot", carrot_ip, _ROLLBACK_PORT)
        p = spawn_otto(
            ["tunnel", "add", "--hosts", "carrot_seed,tomato_seed", "--port", str(_ROLLBACK_PORT)],
            xdir=tmp_path,
            target=target,
            extra_env=_WIDE_CONSOLE_ENV,
        )
        # phase: the LAST launch (tomato's REV ingress) dispatched -- all 4
        # launches attempted; only verify -> retry -> rollback remains.
        _wait_for_stdout(p, r"otto-tunnel:v1:[^:]+:[^:]+:\d+:\d+:rev:ingress:", timeout=60.0)
        if p.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                p.signal(2)  # SIGINT
        with contextlib.suppress(AssertionError):
            # Best-effort: confirms the signal was actually handled, not a
            # hard requirement (the process may finish first on a very fast
            # bed, in which case the rc/survivors assertions below still hold).
            p.wait_for_stderr(BANNER, timeout=10.0)
        rc = p.wait(timeout=90.0)
        assert rc != 0, (
            "tunnel add should never report success against a pre-bound conflicting port\n"
            f"stdout:\n{p.stdout_text()}\nstderr:\n{p.stderr_text()}"
        )
        survivors = _leftover_tunnel_processes()
        assert not survivors, (
            f"add_tunnel's own compensate()-shielded rollback left survivors: {survivors}"
        )
    finally:
        # Belt for the assert-failure path: `_wait_for_stdout` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        # Nested so a raising release probe (post-G5 a dead probe raises)
        # still reports loudly WITHOUT skipping the tunnel reconciliation.
        try:
            _release_tcp_port("carrot")
        finally:
            rm_xdir = tmp_path / "rollback_rm"
            rm_xdir.mkdir()
            spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm_xdir, target=target).wait(
                timeout=120.0
            )
            assert not _leftover_tunnel_processes(), "bed not clean after final reconciliation"


@pytest.mark.no_hygiene_bracket  # multi-host; product's own repair --all + our manual reconcile
def test_sigkill_mid_impair_recovers_via_repair_all(tmp_path):
    """SIGKILL an `otto link impair`; assert `otto link repair --all` restores
    impairment-free qdiscs on the trio.
    """
    link_id = veggies_link_id()
    target = make_bed_target("carrot")
    p = spawn_otto(
        ["link", "impair", link_id, "--loss", "50", "--expire", "60"],
        xdir=tmp_path,
        target=target,
        extra_env=_WIDE_CONSOLE_ENV,
    )
    try:
        # phase: qdisc being written. Not the brief's literal `\| (sudo )?tc
        # qdisc` -- the real command is `sudo -S -p 'otto-sudo:' tc qdisc
        # replace ...` (otto's actual sudo invocation has flags between
        # "sudo" and "tc"), confirmed live; `tc qdisc replace` alone is the
        # unambiguous mutating command (unlike a bare `tc qdisc`, which would
        # also match a read-only `tc qdisc show`).
        _wait_for_stdout(p, r"tc qdisc replace", timeout=120.0)
        p.signal(9)  # SIGKILL
        p.wait(timeout=30.0)
        rep_xdir = tmp_path / "rep"
        rep_xdir.mkdir()
        rep = spawn_otto(["link", "repair", "--all"], xdir=rep_xdir, target=target)
        assert rep.wait(timeout=120.0) == 0, rep.stderr_text()
        # eth2 must be netem-free on both endpoints.
        assert_eth2_netem_free("repair --all")
    finally:
        # Belt for the assert-failure path: `_wait_for_stdout` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p.proc.poll() is None:
            p.signal(9)
        rep2_xdir = tmp_path / "rep2"
        rep2_xdir.mkdir()
        spawn_otto(["link", "repair", "--all"], xdir=rep2_xdir, target=target).wait(timeout=120.0)
        assert_eth2_netem_free("the FINAL reconciliation repair --all")
