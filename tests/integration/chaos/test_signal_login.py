"""SIGTERM during an interactive ``login``: terminal restored, exit 143.

In raw mode the local terminal does not generate SIGINT (^C forwards to
the remote as bytes) — SIGTERM is the interrupt that matters (chaos spec,
"Terminal restore on the force path"). Graceful: the login unwind's
``finally`` restores termios. Forced (teardown deadline 0): the graceful
unwind is abandoned, and restoration comes from plan 1's belt-and-suspenders
pair — the bridge task's inner ``finally`` (re-cancelled during
``asyncio.run`` finalization) and the force-exit hook
(``register_force_exit_hook`` → ``_restore_terminal``, after loop close).
Proven by mutation during review: the finalization path alone restores
termios even with the hook neutered, so this suite pins the end-to-end
observable — forced SIGTERM never strands a raw terminal — WITHOUT
discriminating which of the two paths did it; hook-mechanism isolation
lives in plan 1's unit tests.

Liveness marker: ``echo`` with a split literal, so the matched bytes can
only be command OUTPUT — the raw-mode echo of the typed line contains the
split form, not the joined one.
"""

import os
import re
import signal
import termios

import pytest

from tests.e2e.host._pty_driver import InteractiveOttoSession

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]

_READY = re.compile(rb"CHAOS-READY")


def _login_argv(target) -> "list[str]":
    return ["-l", target.lab, "host", target.host_id, "login"]


def _assert_canonical(master_fd: int) -> None:
    lflag = termios.tcgetattr(master_fd)[3]
    assert lflag & termios.ICANON, "termios not restored: ICANON still cleared (raw mode leaked)"
    assert lflag & termios.ECHO, "termios not restored: ECHO still cleared (raw mode leaked)"


def _sigterm_login(chaos_target, tmp_path, *, extra_env: "dict[str, str] | None") -> None:
    with InteractiveOttoSession(
        _login_argv(chaos_target),
        xdir=tmp_path,
        sut_dirs=chaos_target.sut_dir,
        extra_env=extra_env,
    ) as sess:
        sess.expect(b"Press Ctrl+] to disconnect", timeout=30)
        # otto prints the banner the moment it starts bridging, but the
        # remote login shell is still initializing (MOTD, profile scripts,
        # tcsetattr). Input typed into that window is flushed by the
        # shell's own tcsetattr and lost, so the round-trip echo never
        # comes back — tests/e2e/host/test_interact_e2e.py documents the
        # same race. Wait for the shell prompt before sending anything.
        sess.expect(re.compile(rb":~[$#] "), timeout=20)
        sess.sendline("echo CHAOS-$(echo READY)")
        sess.expect(_READY, timeout=60)  # session live: output round-tripped
        os.kill(sess.pid, signal.SIGTERM)
        rc = sess.wait(timeout=30)
        assert rc == 143
        _assert_canonical(sess._master_fd)  # master reflects the slave's termios


def test_sigterm_during_login_restores_terminal_and_exits_143(chaos_target, tmp_path) -> None:
    _sigterm_login(chaos_target, tmp_path, extra_env=None)


def test_forced_sigterm_during_login_still_restores_terminal(chaos_target, tmp_path) -> None:
    """Deadline 0 abandons the graceful unwind. Restoration then comes from
    plan 1's belt-and-suspenders pair — the bridge task's inner ``finally``
    at ``asyncio.run`` finalization, and the force-exit hook
    (``register_force_exit_hook`` → ``_restore_terminal``) after loop
    close. This asserts the observable outcome only (termios restored, exit
    143), not which path restored it — proven by mutation during review:
    the finalization path alone suffices even with the hook neutered.
    Hook-mechanism isolation lives in plan 1's unit tests."""
    _sigterm_login(chaos_target, tmp_path, extra_env={"OTTO_TEARDOWN_DEADLINE": "0"})
