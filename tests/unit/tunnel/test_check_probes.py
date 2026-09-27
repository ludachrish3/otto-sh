"""Pure probe builders and parsers for ``otto tunnel check``."""

import os
import shlex
import shutil
import subprocess
import time

import pytest

from otto.check.clock import SOCAT_CONNECT_S
from otto.host.daemon import launch_command
from otto.tunnel import check_probes
from otto.tunnel.check_probes import (
    BULK_SIZE,
    HANDSHAKE_DRAIN_S,
    EchoTag,
    bulk_script,
    echo_argv,
    echo_launch_command,
    echo_sentinel,
    handshake_script,
    parse_bulk,
    parse_echo_sentinel,
    parse_trips,
    trips_script,
)

TAG = EchoTag(
    run="a1b2c3",
    tunnel_id="tun-0123456789ab-61001",
    protocol="tcp",
    role="fwd-echo",
    host_id="test1",
)

_NO_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")


class TestEchoSentinel:
    def test_echo_sentinel_round_trips(self) -> None:
        token = echo_sentinel(TAG)
        assert parse_echo_sentinel(token) == TAG

    def test_parse_echo_sentinel_rejects_other_prefixes(self) -> None:
        assert parse_echo_sentinel("otto-tunnel:v1:a:b:c:d:e") is None

    def test_parse_echo_sentinel_rejects_garbage(self) -> None:
        assert parse_echo_sentinel("not-a-token-at-all") is None
        assert parse_echo_sentinel("") is None


class TestEchoArgv:
    def test_echo_argv_per_protocol(self) -> None:
        assert echo_argv("tcp", "127.0.0.1", 61001) == [
            "socat",
            "-b",
            "65535",
            "TCP4-LISTEN:61001,bind=127.0.0.1,fork,reuseaddr",
            "PIPE",
        ]
        assert echo_argv("udp", "127.0.0.1", 61001) == [
            "socat",
            "-b",
            "65535",
            "-T",
            "30",
            "UDP4-RECVFROM:61001,bind=127.0.0.1,fork",
            "PIPE",
        ]

    def test_echo_launch_uses_the_tunnel_launcher(self) -> None:
        got = echo_launch_command(TAG, "tcp", "127.0.0.1", 61001)
        want = launch_command(echo_sentinel(TAG), echo_argv("tcp", "127.0.0.1", 61001))
        assert got == want


def _all_scripts() -> list[str]:
    return [
        trips_script("tcp", "127.0.0.1", 61001, 1400),
        trips_script("udp", "127.0.0.1", 61001, 1400),
        bulk_script("tcp", "127.0.0.1", 61001, BULK_SIZE["tcp"]),
        bulk_script("udp", "127.0.0.1", 61001, BULK_SIZE["udp"]),
        handshake_script("127.0.0.1", 61001),
    ]


class TestScriptShape:
    @_NO_BASH
    def test_every_script_parses_in_bash(self) -> None:
        for s in _all_scripts():
            _bash, _dash_c, body = shlex.split(s)
            result = subprocess.run(
                ["bash", "-n", "-c", body],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, (s, result.stderr)

    def test_every_script_is_one_word_for_sh_c(self) -> None:
        for s in _all_scripts():
            words = shlex.split(s)
            assert len(words) == 3
            assert words[0] == "bash"
            assert words[1] == "-c"

    def test_handshake_script_sends_nothing(self) -> None:
        """socat's stdin is ``/dev/null``: EOF at once, so nothing is sent and FIN goes first."""
        _bash, _dash_c, body = shlex.split(handshake_script("127.0.0.1", 61001))
        socat = body.split(" & ", 1)[0]
        assert socat.startswith("socat ")
        assert " - TCP4:127.0.0.1:61001," in socat
        assert socat.endswith(" </dev/null 2>&1 >/dev/null")
        assert f"connect-timeout={SOCAT_CONNECT_S}" in socat

    def test_handshake_script_drains_both_ways(self) -> None:
        """Bidirectional (no ``-u``), so socat reads what the device says before it closes;
        the replies go to ``/dev/null`` and socat's stderr stays as evidence."""
        _bash, _dash_c, body = shlex.split(handshake_script("127.0.0.1", 61001))
        socat = body.split(" & ", 1)[0].split()
        assert "-u" not in socat
        assert "-U" not in socat
        assert socat[1:3] == ["-t", str(HANDSHAKE_DRAIN_S)]
        # stderr is duplicated onto stdout BEFORE stdout goes to /dev/null
        assert socat[-2:] == ["2>&1", ">/dev/null"]

    def test_handshake_cutoff_is_connect_plus_drain(self) -> None:
        s = handshake_script("127.0.0.1", 61001)
        assert f"read -t {SOCAT_CONNECT_S + HANDSHAKE_DRAIN_S} " in s


# ---------------------------------------------------------------------------
# End-to-end: run the generated scripts under a REAL bash against a fake
# `socat` on PATH. The stub lives entirely in tmp_path (never the scratchpad)
# and is driven by two env vars: FAKE_MODE picks its behaviour, FAKE_LOG is
# where it records its own invocation (and, for the "silent" mode, its own
# pid — logged before it execs into `sleep`, so the pid it logs is the pid
# the exec'd sleep keeps, letting the no-orphan test check it directly).
# "talk" saves its stdin to FAKE_LOG.stdin and answers on stdout and stderr;
# "refused" prints socat's refusal for FAKE_TARGET and exits 1.
# ---------------------------------------------------------------------------

_SOCAT_STUB = """#!/usr/bin/env bash
echo "args=$*" >> "$FAKE_LOG"
case "$FAKE_MODE" in
  echo) exec cat ;;
  dead) exit 1 ;;
  silent) echo "pid=$$" >> "$FAKE_LOG"; exec sleep 60 ;;
  talk) cat > "$FAKE_LOG.stdin"; echo "DEVICE-BANNER"; echo "W stub talked" >&2 ;;
  refused) echo "E connect(5, AF=2 $FAKE_TARGET, 16): Connection refused" >&2; exit 1 ;;
esac
"""


def _write_stub(bindir) -> None:
    bindir.mkdir(exist_ok=True)
    stub = bindir / "socat"
    stub.write_text(_SOCAT_STUB)
    stub.chmod(0o755)


def _stub_env(tmp_path, mode: str, log_path) -> dict:
    """PATH with tmp_path's fake ``socat`` ahead of the real PATH; every other
    tool (bash, tr, head, mktemp, wc, cksum, rm) still resolves normally."""
    bindir = tmp_path / "bin"
    _write_stub(bindir)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_LOG"] = str(log_path)
    env["FAKE_MODE"] = mode
    return env


def _restricted_env(tmp_path, tools: list[str], mode: str, log_path) -> dict:
    """A PATH containing ONLY *tools* (by symlink) plus the fake ``socat`` —
    nothing else, so a tool NOT listed genuinely cannot be found."""
    bindir = tmp_path / "restricted_bin"
    bindir.mkdir(exist_ok=True)
    for name in tools:
        real = shutil.which(name)
        assert real is not None, f"{name} is required by this test's harness and not installed"
        (bindir / name).symlink_to(real)
    _write_stub(bindir)
    return {
        "PATH": str(bindir),
        "FAKE_LOG": str(log_path),
        "FAKE_MODE": mode,
        "HOME": os.environ.get("HOME", "/tmp"),
    }


def _run(s: str, env: dict, *, timeout: float = 30, stdin: str = "") -> subprocess.CompletedProcess:
    """Run a returned script string exactly the way a host does: ``sh -c <it>``.

    ``sh`` itself is resolved from THIS process's own PATH and invoked by
    absolute path: a test that hands *env* a deliberately restricted PATH
    (``_restricted_env``) is restricting what the SCRIPT can find, not
    subprocess's own executable lookup. *stdin* is what the caller's stdin
    holds, so a script that lets it through to socat is caught.
    """
    sh = shutil.which("sh")
    assert sh is not None, "sh not on PATH"
    return subprocess.run(
        [sh, "-c", s],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


class TestEndToEndWithFakeSocat:
    """Runs the actual generated scripts under bash — the product code, not a
    hand-copied fragment — against a same-process fake ``socat``. This is the
    live generator-length proof (the echo-stub bulk case below: a real
    ``tr -dc a-z0-9 | head -c size`` payload really does round-trip as
    exactly *size* bytes) plus the fix-round-1 regressions: an early stop on
    a dead client, no orphaned process after teardown, and a false "match"
    that a missing ``cksum`` can no longer produce.
    """

    @_NO_BASH
    def test_echo_stub_trips_all_match(self, tmp_path) -> None:
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "echo", log)
        s = trips_script("tcp", "127.0.0.1", 61001, 32, trips=3)
        result = _run(s, env)
        assert result.returncode == 0, result.stderr
        parsed = parse_trips(result.stdout, 3)
        assert parsed.matched == 3

    @_NO_BASH
    def test_echo_stub_trips_leave_stderr_empty(self, tmp_path) -> None:
        """The script's own ``trap '' PIPE`` is inherited by the nonce
        generator's ``tr``: an ignored signal stays ignored across ``exec``, so
        when ``head -c`` closes the pipe ``tr`` gets EPIPE instead of a silent
        SIGPIPE death and prints ``tr: write error: Broken pipe`` — once per
        trip. A host merges stderr into the output, so on the bed every trips
        transcript carried five of those lines (two per trip on CentOS 7's
        coreutils) around the real evidence."""
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "echo", log)
        s = trips_script("tcp", "127.0.0.1", 61001, 32, trips=3)
        result = _run(s, env)
        assert parse_trips(result.stdout, 3).matched == 3
        assert result.stderr == ""

    @_NO_BASH
    def test_trips_without_tr_says_nonce_instead_of_timing_out(self, tmp_path, monkeypatch) -> None:
        """Silencing the generator's stderr must not hide a generator that
        produced nothing: a short nonce is its own verdict, reported at once,
        never a ``timeout`` that blames the tunnel after a full wait."""
        monkeypatch.setattr(check_probes, "PROBE_TIMEOUT_S", 2)
        log = tmp_path / "log.txt"
        env = _restricted_env(tmp_path, ["bash", "head"], "echo", log)
        s = trips_script("tcp", "127.0.0.1", 61001, 32, trips=3)
        result = _run(s, env, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "trip 1 nonce got=0" in result.stdout
        assert "timeout" not in result.stdout
        assert "trips 3 matched 0" in result.stdout

    @_NO_BASH
    def test_echo_stub_bulk_matches_with_sent_equal_got(self, tmp_path) -> None:
        """The live generator-length proof: a real payload from ``bulk_script``
        round-trips through a real bash pipeline as exactly *size* bytes."""
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "echo", log)
        s = bulk_script("tcp", "127.0.0.1", 61001, 4096)
        result = _run(s, env)
        assert result.returncode == 0, result.stderr
        parsed = parse_bulk(result.stdout)
        assert parsed.match is True
        assert parsed.sent == 4096
        assert parsed.got == 4096

    @_NO_BASH
    def test_dead_stub_trips_prints_summary_without_hanging(self, tmp_path) -> None:
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "dead", log)
        s = trips_script("tcp", "127.0.0.1", 61001, 32, trips=3)
        result = _run(s, env, timeout=10)
        assert result.returncode == 0
        assert result.returncode != 141, "141 = bash killed by SIGPIPE (rc 128+13)"
        parsed = parse_trips(result.stdout, 3)
        assert parsed.matched == 0
        assert "trips 3 matched 0" in result.stdout

    @_NO_BASH
    def test_silent_stub_stops_after_first_timeout_and_leaves_no_orphan(
        self, tmp_path, monkeypatch
    ) -> None:
        """Covers two fix-round-1 items in one real run: the loop stops at the
        first timed-out trip (rather than repeating ``trips`` full timeouts),
        and the coprocess client is gone — not orphaned — once the script
        returns.

        ``PROBE_TIMEOUT_S`` is monkeypatched down for this one test so its
        real wait is ~2s instead of the product's 15s: :func:`trips_script`
        reads the module global at CALL time (an f-string, not a frozen
        default), so this shortens the generated script's own ``read -t``
        without adding any new parameter to the product.
        """
        monkeypatch.setattr(check_probes, "PROBE_TIMEOUT_S", 2)
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "silent", log)
        s = trips_script("tcp", "127.0.0.1", 61001, 32, trips=3)
        result = _run(s, env, timeout=10)
        assert result.returncode == 0
        parsed = parse_trips(result.stdout, 3)
        assert parsed.matched == 0
        assert "trip 1 timeout" in result.stdout
        assert "trip 2" not in result.stdout, "must stop at the first non-ok trip"

        lines = log.read_text().splitlines()
        pid_lines = [line for line in lines if line.startswith("pid=")]
        assert pid_lines, f"stub never logged its pid: {lines}"
        pid = int(pid_lines[0].removeprefix("pid="))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    @_NO_BASH
    def test_bulk_without_cksum_is_not_a_match(self, tmp_path) -> None:
        """Fix-round-1 item 4: a host missing ``cksum`` must never false-pass."""
        log = tmp_path / "log.txt"
        env = _restricted_env(tmp_path, ["bash", "mktemp", "head", "wc", "tr", "rm"], "echo", log)
        s = bulk_script("tcp", "127.0.0.1", 61001, 512)
        result = _run(s, env)
        assert result.returncode == 0, result.stderr
        parsed = parse_bulk(result.stdout)
        assert parsed.match is False


class TestHandshakeWithFakeSocat:
    """The real :func:`handshake_script` under bash against a fake ``socat``.

    The fake stands in for socat's ``-`` address: whatever reaches its stdin
    is what socat would send the device, and whatever it prints on stdout is
    what the device said. A caller stdin full of ``LEAK`` makes a script that
    forgets ``</dev/null`` visible.
    """

    @_NO_BASH
    def test_nothing_reaches_the_device_and_its_replies_are_discarded(self, tmp_path) -> None:
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "talk", log)
        result = _run(handshake_script("127.0.0.1", 61001), env, stdin="LEAK\n")
        assert result.returncode == 0, result.stderr
        sent = tmp_path / "log.txt.stdin"
        assert sent.exists(), "the fake socat never ran"
        assert sent.read_text() == ""
        assert "DEVICE-BANNER" not in result.stdout
        assert "W stub talked" in result.stdout, "socat's stderr is the evidence and is kept"
        assert result.stdout.splitlines()[-1] == "handshake ok"
        assert "drain cut off" not in result.stdout

    @_NO_BASH
    def test_a_refused_connect_fails_with_socats_own_words(self, tmp_path) -> None:
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "refused", log)
        env["FAKE_TARGET"] = "127.0.0.1:61001"
        result = _run(handshake_script("127.0.0.1", 61001), env, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "E connect(5, AF=2 127.0.0.1:61001, 16): Connection refused",
            "handshake failed",
        ]

    @_NO_BASH
    def test_a_device_that_never_stops_is_cut_off_and_still_ok(self, tmp_path, monkeypatch) -> None:
        """socat's ``-t`` is re-armed by every read, so only the watchdog bounds this.

        The fake never exits (a device that keeps talking keeps real socat
        alive the same way). With connect and drain shrunk to 1 s each, the
        script must return near their 2 s sum, call it ``handshake ok`` (a
        socat alive past its connect-timeout has connected), and leave no
        socat behind.
        """
        monkeypatch.setattr(check_probes, "SOCAT_CONNECT_S", 1)
        monkeypatch.setattr(check_probes, "HANDSHAKE_DRAIN_S", 1)
        log = tmp_path / "log.txt"
        env = _stub_env(tmp_path, "silent", log)
        start = time.monotonic()
        result = _run(handshake_script("127.0.0.1", 61001), env, timeout=10)
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr
        assert 1.5 < elapsed < 6, elapsed
        assert result.stdout.splitlines() == [
            "still sending after 2s: drain cut off",
            "handshake ok",
        ]
        pid_lines = [line for line in log.read_text().splitlines() if line.startswith("pid=")]
        assert pid_lines, "the fake socat never logged its pid"
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_lines[0].removeprefix("pid=")), 0)


class TestParseTrips:
    def test_parse_trips_all_matched_with_clock(self) -> None:
        lines = [f"trip {i} ok 1790339724.826219 1790339724.830219" for i in range(1, 6)]
        lines.append("trips 5 matched 5")
        output = "\n".join(lines)
        result = parse_trips(output, 5)
        assert result.matched == 5
        assert len(result.rtts_ms) == 5
        for rtt in result.rtts_ms:
            assert rtt == pytest.approx(4.0, abs=0.01)

    def test_parse_trips_no_clock_keeps_matches(self) -> None:
        output = "\n".join(["trip 1 ok  "] * 5)
        result = parse_trips(output, 5)
        assert result.matched == 5
        assert result.rtts_ms == []

    def test_parse_trips_mismatch_and_timeout(self) -> None:
        output = (
            "trip 1 ok 1790339724.826219 1790339724.830219\ntrip 2 mismatch got=0\ntrip 3 timeout"
        )
        result = parse_trips(output, 3)
        assert result.matched == 1

    def test_parse_trips_closed_counts_as_not_matched(self) -> None:
        output = "trip 1 ok 1790339724.826219 1790339724.830219\ntrip 2 closed got=0 \n"
        result = parse_trips(output, 3)
        assert result.matched == 1

    def test_parse_trips_caps_matched_at_trips(self) -> None:
        output = "\n".join(["trip 1 ok  "] * 8)
        result = parse_trips(output, 3)
        assert result.matched == 3

    def test_parse_trips_empty_output(self) -> None:
        result = parse_trips("", 5)
        assert result.matched == 0
        assert result.rtts_ms == []
        assert result.said == ""

    def test_parse_trips_garbage_output(self) -> None:
        result = parse_trips("not even close to the expected shape\n\xff\xfe", 5)
        assert result.matched == 0
        assert result.rtts_ms == []


class TestParseBulk:
    def test_parse_bulk_match_with_timing(self) -> None:
        output = "sent=65536 got=65536 match\n1790339724.826219 1790339725.026219\n"
        result = parse_bulk(output)
        assert result.match is True
        assert result.got == 65536
        assert result.elapsed_ms == pytest.approx(200.0, abs=0.01)

    def test_parse_bulk_mismatch_without_timing(self) -> None:
        output = "sent=65000 got=8192 mismatch\n\n"
        result = parse_bulk(output)
        assert result.match is False
        assert result.got == 8192
        assert result.elapsed_ms is None

    def test_parse_bulk_word_match_but_size_mismatch_is_not_trusted(self) -> None:
        """The script only ever writes ``match`` when sizes already agree, but the
        parser's own ``sent == got > 0`` guard must hold even if that invariant
        is ever violated (e.g. a corrupted transcript)."""
        output = "sent=100 got=50 match\n1790339724.826219 1790339724.926219\n"
        result = parse_bulk(output)
        assert result.match is False

    def test_parse_bulk_empty_output(self) -> None:
        result = parse_bulk("")
        assert result.match is False
        assert result.got == 0
        assert result.sent == 0
        assert result.elapsed_ms is None
        assert result.said == ""

    def test_parse_bulk_garbage_output(self) -> None:
        result = parse_bulk("this is not a bulk transcript at all\n***\n")
        assert result.match is False
        assert result.got == 0
        assert result.sent == 0
        assert result.elapsed_ms is None
