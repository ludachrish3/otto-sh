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
    - Vagrant test VM ``test1`` must be running (lab ``unix``).
    - The same subprocess-coverage bootstrap used by ``test_coverage_e2e.py``.

Running::

    uv run pytest tests/e2e/host/test_interact_e2e.py \\
        -m integration -v --override-ini 'addopts='

Tests are split into two xdist groups — ``interact_e2e_ssh`` and
``interact_e2e_telnet`` — so pytest-xdist keeps the ssh and telnet
parametrizations on separate workers. Both drive the *same* VM, so the
pool lease (see :func:`leased_test1`) is what actually keeps them from
racing each other and the rest of the suite on it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.paths import PROJECT_ROOT
from tests.e2e.host._pty_driver import InteractiveOttoSession

REPO1_DIR = PROJECT_ROOT / "tests" / "repo1"

# One source of truth: the element leased and the host driven can never drift.
ELEMENT = "test1"
HOST_ID = ELEMENT
# UnixHost._generate_name returns "{ne} [board]" — the banner and every
# log preamble written by _SessionLogFile use this, NOT the host id. The
# bed's Unix hosts carry no board, so the name is the bare element.
HOST_NAME = ELEMENT
ROUND_TRIP_TOKEN = "otto_login_marker"

_LOG_LINE_RE = re.compile(
    rb"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[INFO \] @"
    + re.escape(HOST_NAME.encode())
    + rb"(?: > | +)\|"
)


def _find_login_log_dir(xdir: Path) -> Path:
    """Return the single ``host/<timestamp>_login`` output dir under ``xdir``."""
    host_dir = xdir / "host"
    assert host_dir.is_dir(), f"Expected {host_dir} to exist after host login"
    candidates = sorted(d for d in host_dir.iterdir() if d.is_dir() and d.name.endswith("_login"))
    assert len(candidates) == 1, (
        f"Expected exactly one ``_login`` output dir under {host_dir}, found {candidates}"
    )
    return candidates[0]


def _login_argv(term: str) -> list[str]:
    # ``--term`` is an option on the ``otto host`` callback, so it must
    # appear before the positional ``host_id``.
    return ["-l", "unix", "host", "--term", term, HOST_ID, "login"]


@pytest.fixture(scope="class")
def leased_test1(tmp_path_factory) -> Iterator[str]:
    """Hold the Unix-pool lease on test1 for the whole class.

    ``otto host <id> login`` is the *human-facing* bridge: it hands over a
    real interactive shell and deliberately does NOT neutralize ``HISTFILE``
    (a person's own login must keep recording their history). So every
    session this module opens appends what it types — ``echo
    otto_login_marker``, up to three ``stty size`` probes — to test1's
    ``~/.bash_history``
    when bash flushes at exit.

    Harmless on its own, but ``test_shell_history_e2e`` digests that exact
    file before and after its measurement window to prove otto stays out of
    it, and the bed caps the file at ``HISTFILESIZE`` lines, so a single
    concurrent append rotates lines out and moves the sha256 of the whole
    file. That module leases its host; before this fixture existed this one
    did not, and living in its own ``xdist_group`` under ``-n auto --dist
    loadgroup`` is precisely what let the two run at once.

    Leasing a *named* host rather than whichever is free — the
    single-candidate idiom ``tests/e2e/chaos/test_docker_chaos.py`` uses for
    test3 — because ``HOST_ID`` and ``HOST_NAME`` are baked into the banner
    and log-preamble assertions here. Blocks until test1 is free.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, [ELEMENT]) as element:
        yield element


@pytest.fixture(scope="class")
def login_session(request, tmp_path_factory, leased_test1):
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
            f"[otto] interactive session with {HOST_NAME} ({term}). Press Ctrl+] to disconnect.".encode(),  # noqa: E501 — long banner string with hostname interpolation
            timeout=30,
        )
        # otto prints the banner the moment it starts bridging, but the
        # remote login shell is still initializing (MOTD, profile scripts,
        # tcsetattr). Input typed into that window is flushed by the shell's
        # tcsetattr and lost, so the round-trip echo never comes back. Wait
        # for the shell prompt — it only appears once the remote shell has
        # finished its terminal setup and is reading stdin.
        sess.expect(re.compile(rb":~[$#] "), timeout=20)
        sess.sendline(f"echo {ROUND_TRIP_TOKEN}")
        # First match: the remote PTY echoing the command line we typed.
        # Second match: the shell's response. Waiting for both guarantees
        # the round-trip actually completed before we disconnect, so
        # _LineBuffer has a chance to flush the response into session.log.
        try:
            first_seen = sess.expect(ROUND_TRIP_TOKEN.encode(), timeout=10)
        except TimeoutError as exc:
            pytest.fail(
                f"round-trip over {term}: the command echo (first token "
                f"occurrence) never came back — input is not reaching the "
                f"remote shell.\n{exc}"
            )
        try:
            sess.expect(ROUND_TRIP_TOKEN.encode(), timeout=10)
        except TimeoutError as exc:
            # Shell prompt cursor-repaint CAN merge echo and response into
            # a single occurrence — but only by mangling the echo, in which
            # case the first expect consumed the RESPONSE (what it matched
            # carries no clean `echo <token>`) and the round trip is
            # already proven; drain briefly and continue. If the first
            # match really was the echo, a missing second occurrence means
            # the shell never answered: input reached the remote but no
            # output came back, and silently continuing would hand every
            # dependent test a half-dead transcript.
            if b"echo " + ROUND_TRIP_TOKEN.encode() in first_seen:
                pytest.fail(
                    f"round-trip over {term}: the shell response (second "
                    f"token occurrence) never arrived; the echo was seen, "
                    f"so input reached the remote but nothing came back.\n{exc}"
                )
            # The else-arm's premise is itself assertable: a RESPONSE is the
            # token at line start (expect consumed through the token, so
            # first_seen always ends with it — check what precedes). If the
            # single occurrence was neither a clean echo nor a line-start
            # response, nothing proves the round trip — fail classified
            # rather than continue silent.
            token = ROUND_TRIP_TOKEN.encode()
            if not first_seen[: -len(token)].endswith((b"\r", b"\n")):
                pytest.fail(
                    f"round-trip over {term}: single token occurrence and it "
                    f"was not a line-start response — cannot prove the round "
                    f"trip completed.\n{first_seen!r}"
                )
            sess.drain(0.2)
        sess.disconnect()
        disconnect_seen = sess.expect(f"[otto] disconnected from {HOST_NAME}.".encode(), timeout=10)
        exit_code = sess.wait(timeout=10)

    log_path = _find_login_log_dir(xdir) / "session.log"
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
@pytest.mark.parametrize(
    "login_session",
    [
        pytest.param("ssh", marks=pytest.mark.xdist_group("interact_e2e_ssh")),
        pytest.param("telnet", marks=pytest.mark.xdist_group("interact_e2e_telnet")),
    ],
    indirect=True,
)
class TestHostLoginSession:
    """Drive ``otto host test1 --term {term} login`` end-to-end.

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
            f"session.log missing entering marker:\n{content}"
        )
        assert "Interactive session ended" in content, (
            f"session.log missing exit marker:\n{content}"
        )
        assert ROUND_TRIP_TOKEN in content, (
            f"session.log missing round-trip token {ROUND_TRIP_TOKEN!r}:\n{content}"
        )
        # Marker lines use the '@host   |' preamble; output lines use '@host > |'.
        assert f"@{HOST_NAME}   |" in content, "Missing marker preamble '@host   |'"
        assert f"@{HOST_NAME} > |" in content, "Missing output preamble '@host > |'"

    def test_log_has_no_raw_ansi_escapes(self, login_session):
        # _LineBuffer._emit runs every line through _strip_ansi before
        # writing, so the transcript should contain no raw CSI sequences.
        assert "\x1b[" not in login_session["log_content"], (
            "session.log contains un-stripped ANSI CSI escapes"
        )

    def test_log_lines_match_rich_formatter_layout(self, login_session):
        # Every non-empty line must match the format _SessionLogFile.write_line
        # and write_marker emit — pins the timestamp + '[INFO ]' + host
        # preamble so future drift from RichFormatter is caught.
        for line in login_session["log_bytes"].splitlines():
            if not line:
                continue
            assert _LOG_LINE_RE.match(line), (
                f"session.log line does not match expected format: {line!r}"
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    "term",
    [
        pytest.param("ssh", marks=pytest.mark.xdist_group("interact_e2e_ssh")),
        pytest.param("telnet", marks=pytest.mark.xdist_group("interact_e2e_telnet")),
    ],
)
class TestHostLoginSigwinch:
    """Exercise the SIGWINCH-forwarding branches of run_ssh_login / run_telnet_login.

    These lines only execute when SIGWINCH actually fires during an
    active interactive session, so the unit tests can't reach them.
    Resizing the PTY master delivers SIGWINCH to the otto subprocess ONLY
    because the driver's shim made this PTY otto's controlling terminal —
    without that (the driver's original state) the kernel signals nobody
    and this test covers nothing, which is why the precondition is asserted
    by name below. The handler is installed by :func:`_run_bridge`.

    For SSH this covers ``process.change_terminal_size`` at
    :func:`otto.host.interact.run_ssh_login`; for telnet it covers the
    NAWS subnegotiation at :func:`otto.host.interact.run_telnet_login`.
    The remote-shell check (``stty size``) is the assertion that the
    resize reached the remote side — probed with retries because the
    first probe can race the push landing.
    """

    def test_resize_triggers_remote_side_update(self, tmp_path: Path, term: str, leased_test1):
        # Opens its own session rather than reusing `login_session`, so it has
        # to take the same lease itself — see `leased_test1` for why.
        xdir = tmp_path / "xdir"
        with InteractiveOttoSession(
            _login_argv(term),
            xdir=xdir,
            sut_dirs=REPO1_DIR,
            cols=80,
            rows=24,
        ) as sess:
            sess.expect(b"Press Ctrl+] to disconnect", timeout=30)
            # The load-bearing precondition, asserted by name: without a
            # controlling terminal the kernel delivers resize SIGWINCH to
            # nobody, and everything below this line covers nothing (the
            # pre-shim driver's permanent, silent state).
            assert sess.foreground_pgid == os.getpgid(sess.pid), (
                "driver child has no controlling terminal — the kernel "
                "cannot deliver resize SIGWINCH"
            )
            # Let the shell prompt stabilize so the resize lands mid-session,
            # not mid-login.
            sess.drain(0.3)

            sess.resize(132, 50)
            # The SIGWINCH forwarders leave no local artifact on success
            # (they debug-log only on failure), so the remote's own report
            # is the ONE observable that the resize landed: probe
            # `stty size` until it reflects the new geometry. The first
            # probe can race the window-change/NAWS push and report the
            # stale 24x80 — each retry sends a fresh probe, and the
            # expect's per-attempt timeout owns the pacing (a fused
            # probe-response loop, not an interval poll). On exhaustion
            # this FAILS naming the backend: it is the only assertion
            # that the resize actually reached the remote side.
            last_expiry: TimeoutError | None = None
            for _ in range(3):
                sess.sendline("stty size")
                try:
                    sess.expect(b"50 132", timeout=5)
                    break
                except TimeoutError as exc:
                    last_expiry = exc
            else:
                # for/else: the failure is bound to LOOP EXHAUSTION, not to
                # a sentinel element of the iterable a future edit can drop.
                pytest.fail(
                    f"resize never reached the remote shell over {term}: "
                    f"three 'stty size' probes never reported the new "
                    f"geometry (expected '50 132').\n{last_expiry}"
                )

            sess.disconnect()
            sess.expect(b"disconnected from", timeout=10)
            assert sess.wait(timeout=10) == 0
