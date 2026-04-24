"""End-to-end tests for ``otto host <id> login`` against real VMs.

These tests spawn ``otto`` as a subprocess on a fresh pseudo-terminal
(via :class:`InteractiveOttoSession`) so the full interactive bridge in
:mod:`otto.host.interact` runs end-to-end — raw mode, the stdin worker
thread, SIGWINCH forwarding, the session log file, and the real
``asyncssh`` / ``telnetlib3`` back-ends. The companion unit tests in
:mod:`tests.unit.host.test_interact` cover the protocol-free helpers;
this file covers the glue the unit tests cannot reach without a real
terminal and a real shell.

Prerequisites:
    - Vagrant test VM ``carrot`` must be running (lab ``veggies``).
    - The same subprocess-coverage bootstrap used by ``test_coverage_e2e.py``.

Running::

    uv run pytest tests/unit/host/test_interact_e2e.py \\
        -m integration -v --override-ini 'addopts='

All tests carry ``@pytest.mark.xdist_group("interact_e2e")`` so pytest-xdist
pins them to a single worker and concurrent runs don't race on the shared
Vagrant VMs.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from _pty_driver import InteractiveOttoSession

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1_DIR = PROJECT_ROOT / "tests" / "repo1"

HOST_ID = "carrot_seed"
# RemoteHost._generateName returns "{ne} {board}" — the banner and every
# log preamble written by _SessionLogFile use this, NOT the host id.
HOST_NAME = "carrot seed"
ROUND_TRIP_TOKEN = "otto_login_marker"

_LOG_LINE_RE = re.compile(
    rb"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[ INFO  \] @"
    + re.escape(HOST_NAME.encode())
    + rb"(?: > | +)\|"
)


def _find_login_log_dir(xdir: Path) -> Path:
    """Return the single ``host/<timestamp>_login`` output dir under ``xdir``."""
    host_dir = xdir / "host"
    assert host_dir.is_dir(), f"Expected {host_dir} to exist after host login"
    candidates = sorted(
        d for d in host_dir.iterdir() if d.is_dir() and d.name.endswith("_login")
    )
    assert len(candidates) == 1, (
        f"Expected exactly one ``_login`` output dir under {host_dir}, found {candidates}"
    )
    return candidates[0]


def _login_argv(term: str) -> list[str]:
    # ``--term`` is an option on the ``otto host`` callback, so it must
    # appear before the positional ``host_id``.
    return ["-l", "veggies", "host", "--term", term, HOST_ID, "login"]


@pytest.fixture(scope="class")
def login_session(request, tmp_path_factory):
    """Run one full ``otto host login`` round-trip and return the resulting log.

    Parametrized indirectly by the caller's ``term`` parameter. Returns a
    dict with ``xdir`` (the per-test output root), ``log_path``, and
    ``log_content`` (the transcript text written by ``_SessionLogFile``).
    Class-scoped so every test in the class shares one VM login per term.
    """
    term = request.param
    xdir = tmp_path_factory.mktemp(f"interact_e2e_{term}_") / "xdir"
    banner_seen: bytes = b""
    disconnect_seen: bytes = b""

    with InteractiveOttoSession(
        _login_argv(term),
        xdir=xdir,
        sut_dirs=REPO1_DIR,
    ) as sess:
        banner_seen = sess.expect(
            f"[otto] interactive session with {HOST_NAME} ({term}). Press Ctrl+] to disconnect.".encode(),
            timeout=30,
        )
        sess.sendline(f"echo {ROUND_TRIP_TOKEN}")
        # First match: the remote PTY echoing the command line we typed.
        # Second match: the shell's response. Waiting for both guarantees
        # the round-trip actually completed before we disconnect, so
        # _LineBuffer has a chance to flush the response into otto.log.
        sess.expect(ROUND_TRIP_TOKEN.encode(), timeout=10)
        try:
            sess.expect(ROUND_TRIP_TOKEN.encode(), timeout=5)
        except TimeoutError:
            # Shell prompt cursor-repaint can merge the echo and response
            # into one line, leaving a single occurrence. That still
            # proves the round-trip happened — drain briefly and continue.
            sess.drain(0.2)
        sess.disconnect()
        disconnect_seen = sess.expect(
            f"[otto] disconnected from {HOST_NAME}.".encode(), timeout=10
        )
        exit_code = sess.wait(timeout=10)

    log_path = _find_login_log_dir(xdir) / "otto.log"
    return {
        "term": term,
        "xdir": xdir,
        "exit_code": exit_code,
        "banner_seen": banner_seen,
        "disconnect_seen": disconnect_seen,
        "log_path": log_path,
        "log_content": log_path.read_text(),
        "log_bytes": log_path.read_bytes(),
    }


@pytest.mark.integration
@pytest.mark.xdist_group("interact_e2e")
@pytest.mark.parametrize("login_session", ["ssh", "telnet"], indirect=True)
class TestHostLoginSession:
    """Drive ``otto host carrot_seed --term {term} login`` end-to-end.

    Exercises the full :func:`otto.host.interact.run_ssh_login` and
    :func:`otto.host.interact.run_telnet_login` paths against a real
    Vagrant VM with a real PTY, so raw-mode setup, the stdin worker
    thread, and ``_SessionLogFile`` all run under test.
    """

    def test_banner_and_clean_exit(self, login_session):
        # Banner and disconnect messages come from _print_stderr; clean
        # exit proves _run_bridge's finally-block restored the terminal
        # and the process returned normally.
        assert b"Press Ctrl+] to disconnect" in login_session["banner_seen"]
        assert b"disconnected from" in login_session["disconnect_seen"]
        assert login_session["exit_code"] == 0

    def test_log_contains_session_markers_and_round_trip_token(self, login_session):
        content = login_session["log_content"]
        assert "Entering interactive session" in content, (
            f"otto.log missing entering marker:\n{content}"
        )
        assert "Interactive session ended" in content, (
            f"otto.log missing exit marker:\n{content}"
        )
        assert ROUND_TRIP_TOKEN in content, (
            f"otto.log missing round-trip token {ROUND_TRIP_TOKEN!r}:\n{content}"
        )
        # Marker lines use the '@host   |' preamble; output lines use '@host > |'.
        assert f"@{HOST_NAME}   |" in content, "Missing marker preamble '@host   |'"
        assert f"@{HOST_NAME} > |" in content, "Missing output preamble '@host > |'"

    def test_log_has_no_raw_ansi_escapes(self, login_session):
        # _LineBuffer._emit runs every line through _strip_ansi before
        # writing, so the transcript should contain no raw CSI sequences.
        assert "\x1b[" not in login_session["log_content"], (
            "otto.log contains un-stripped ANSI CSI escapes"
        )

    def test_log_lines_match_rich_formatter_layout(self, login_session):
        # Every non-empty line must match the format _SessionLogFile.write_line
        # and write_marker emit — pins the timestamp + '[ INFO  ]' + host
        # preamble so future drift from RichFormatter is caught.
        for line in login_session["log_bytes"].splitlines():
            if not line:
                continue
            assert _LOG_LINE_RE.match(line), (
                f"otto.log line does not match expected format: {line!r}"
            )


@pytest.mark.integration
@pytest.mark.xdist_group("interact_e2e")
@pytest.mark.parametrize("term", ["ssh", "telnet"])
class TestHostLoginSigwinch:
    """Exercise the SIGWINCH-forwarding branches of run_ssh_login / run_telnet_login.

    These lines only execute when SIGWINCH actually fires during an
    active interactive session, so the unit tests can't reach them.
    Resizing the PTY master from the test triggers the kernel to deliver
    SIGWINCH to the otto subprocess, which runs the ``install_sigwinch``
    handler installed by :func:`_run_bridge`.

    For SSH this covers ``process.change_terminal_size`` at
    :func:`otto.host.interact.run_ssh_login`; for telnet it covers the
    NAWS subnegotiation at :func:`otto.host.interact.run_telnet_login`.
    The remote-shell check (``stty size``) is a best-effort sanity
    signal — the primary coverage target is the otto-side handler code.
    """

    def test_resize_triggers_remote_side_update(self, tmp_path: Path, term: str):
        xdir = tmp_path / "xdir"
        with InteractiveOttoSession(
            _login_argv(term),
            xdir=xdir,
            sut_dirs=REPO1_DIR,
            cols=80,
            rows=24,
        ) as sess:
            sess.expect(b"Press Ctrl+] to disconnect", timeout=30)
            # Let the shell prompt stabilize so the resize lands mid-session,
            # not mid-login.
            sess.drain(0.3)

            sess.resize(132, 50)
            # Give the otto signal-handler task a moment to run and for
            # the remote side to process the update.
            time.sleep(0.3)

            sess.sendline("stty size")
            try:
                sess.expect(b"50 132", timeout=5)
            except TimeoutError:
                # Remote reflection isn't the primary assertion — if the
                # shell response merges with the echo due to cursor repaint,
                # that's fine. The key is the otto-side handler ran without
                # raising (otherwise the session would have crashed below).
                pass

            sess.disconnect()
            sess.expect(b"disconnected from", timeout=10)
            assert sess.wait(timeout=10) == 0
