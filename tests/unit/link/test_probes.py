"""Probe command builders and parsers for otto link check."""

import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from otto.check.fingerprint import one_command
from otto.link.probes import (
    PROBE_TIMEOUT_S,
    listener_command,
    parse_elapsed_ms,
    parse_ping,
    pick_backend,
    ping_command,
    timed_connect_command,
    timed_transfer_command,
)


@pytest.fixture
def echo_server() -> Iterator[int]:
    """A real loopback TCP echo server, for exercising client commands end to end.

    Plain blocking sockets in a background thread — this is test-double
    infrastructure, not product code, so it isn't the threads-plus-asyncio
    combination the repo avoids there.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()


IPUTILS = """\
PING 198.18.0.2 (198.18.0.2) 56(84) bytes of data.
64 bytes from 198.18.0.2: icmp_seq=1 ttl=64 time=100.4 ms
64 bytes from 198.18.0.2: icmp_seq=1 ttl=64 time=100.6 ms (DUP!)
64 bytes from 198.18.0.2: icmp_seq=3 ttl=64 time=100.2 ms
64 bytes from 198.18.0.2: icmp_seq=2 ttl=64 time=150.9 ms

--- 198.18.0.2 ping statistics ---
4 packets transmitted, 3 received, +1 duplicates, 25% packet loss, time 3004ms
rtt min/avg/max/mdev = 100.2/112.9/150.9/21.6 ms
"""

BUSYBOX = """\
PING 198.18.0.2 (198.18.0.2): 56 data bytes
64 bytes from 198.18.0.2: seq=0 ttl=64 time=0.123 ms
64 bytes from 198.18.0.2: seq=1 ttl=64 time=0.101 ms
64 bytes from 198.18.0.2: seq=1 ttl=64 time=0.109 ms (DUP!)

--- 198.18.0.2 ping statistics ---
3 packets transmitted, 2 packets received, 1 duplicates, 33% packet loss
round-trip min/avg/max = 0.101/0.111/0.123 ms
"""


class TestParsePing:
    def test_iputils_output_parses(self) -> None:
        stats = parse_ping(IPUTILS)
        assert stats is not None
        assert stats.transmitted == 4
        assert stats.seqs == [1, 3, 2]
        assert stats.rtts == [100.4, 100.2, 150.9]
        assert stats.duplicates == 1
        assert stats.received == 3
        assert stats.loss_pct == pytest.approx(25.0)
        assert stats.out_of_order == 1  # seq 2 arrived after seq 3

    def test_busybox_ping_output_parses(self) -> None:
        stats = parse_ping(BUSYBOX)
        assert stats is not None
        assert stats.transmitted == 3
        assert stats.seqs == [0, 1]
        assert stats.duplicates == 1
        assert stats.loss_pct == pytest.approx(100 / 3)
        assert stats.avg == pytest.approx(0.112)
        assert stats.sd == pytest.approx(0.011)

    def test_no_summary_is_none(self) -> None:
        assert parse_ping("ping: connect: Network is unreachable\n") is None

    def test_all_lost_has_no_average(self) -> None:
        stats = parse_ping("--- x ---\n5 packets transmitted, 0 received, 100% packet loss\n")
        assert stats is not None
        assert stats.loss_pct == 100.0
        assert stats.avg is None
        assert stats.sd is None


class TestCommands:
    def test_ping_command(self) -> None:
        assert (
            ping_command("198.18.0.2", count=10, interval=0.2)
            == "ping -c 10 -i 0.2 -W 2 198.18.0.2"
        )

    def test_backend_preference(self) -> None:
        assert pick_backend({"python3": True, "socat": True}) == "python3"
        assert pick_backend({"python3": False, "socat": True}) == "socat"
        assert pick_backend({"python3": False, "socat": False}) is None
        assert pick_backend({}) is None

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_listener_is_detached_tagged_and_namespaced(self, backend: str) -> None:
        cmd = listener_command(backend, 5205, tag="otto-check-abc123", netns="otto-check-abc123")
        assert cmd.startswith("ip netns exec otto-check-abc123 ")
        assert "otto-check-abc123" in cmd
        assert "5205" in cmd
        assert cmd.rstrip().endswith("&")

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_listener_binds_the_address_it_is_given(self, backend: str) -> None:
        cmd = listener_command(backend, 5205, tag="otto-check-abc123", bind="10.10.202.12")
        body = shlex.split(cmd)[3]
        if backend == "python3":
            program = shlex.split(body)[2]
            assert "s.bind(('10.10.202.12', 5205))" in program
        else:
            assert "TCP-LISTEN:5205,bind=10.10.202.12," in body

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_a_sandbox_listener_takes_every_address(self, backend: str) -> None:
        cmd = listener_command(backend, 5205, tag="otto-check-abc123", netns="otto-check-abc123")
        body = shlex.split(cmd)[7]
        if backend == "python3":
            assert "s.bind(('', 5205))" in shlex.split(body)[2]
        else:
            assert "TCP-LISTEN:5205,fork," in body

    def test_socat_clients_are_bounded_by_socats_own_timers(self) -> None:
        for cmd in (
            timed_connect_command("socat", "198.18.0.2", 5205),
            timed_transfer_command("socat", "198.18.0.2", 5299, 262144),
        ):
            assert f"socat -T {PROBE_TIMEOUT_S} -t {PROBE_TIMEOUT_S} - " in cmd
            assert "connect-timeout=5" in cmd
        quick = timed_connect_command("socat", "198.18.0.2", 5205, within=1)
        assert "socat -T 1 -t 1 - TCP:198.18.0.2:5205,connect-timeout=1 " in quick

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_client_commands_name_target(self, backend: str) -> None:
        assert "198.18.0.2" in timed_connect_command(backend, "198.18.0.2", 5205)
        cmd = timed_transfer_command(backend, "198.18.0.2", 5205, 262144)
        assert "262144" in cmd


class TestElapsed:
    @pytest.mark.parametrize(
        ("output", "ms"),
        [
            ("12.5\n", 12.5),
            ("noise\n1700000000.100000 1700000000.350000\n", 250.0),
        ],
    )
    def test_both_clock_shapes(self, output: str, ms: float) -> None:
        assert parse_elapsed_ms(output) == pytest.approx(ms)

    @pytest.mark.parametrize("output", ["", " \n", "x y\n"])
    def test_no_clock_is_none(self, output: str) -> None:
        assert parse_elapsed_ms(output) is None


class TestCommandsReadBack:
    """Every embedded program must survive a real shell round trip.

    A command that breaks when quoted into a remote shell is exactly the
    failure this feature exists to catch, so parse every built command with
    real bash (``bash -n``), extract the embedded payload through a stub
    interpreter that captures its argv, and run the client commands against
    a real echo server — rather than just asserting on substrings.
    """

    def test_ping_command_is_valid_shell(self) -> None:
        cmd = ping_command("198.18.0.2", count=3, interval=0.5)
        result = subprocess.run(
            ["bash", "-n", "-c", cmd], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    @pytest.mark.parametrize("netns", [None, "otto-check-abc123"])
    def test_listener_command_is_valid_shell(self, backend: str, netns: str | None) -> None:
        cmd = listener_command(backend, 5205, tag="otto-check-abc123", netns=netns)
        result = subprocess.run(
            ["bash", "-n", "-c", cmd], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_timed_connect_command_is_valid_shell(self, backend: str) -> None:
        cmd = timed_connect_command(backend, "198.18.0.2", 5205)
        result = subprocess.run(
            ["bash", "-n", "-c", cmd], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_timed_transfer_command_is_valid_shell(self, backend: str) -> None:
        cmd = timed_transfer_command(backend, "198.18.0.2", 5205, 262144)
        result = subprocess.run(
            ["bash", "-n", "-c", cmd], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_python3_echo_listener_program_compiles(self, tmp_path: Path) -> None:
        """The embedded echo-listener source must survive both quoting layers.

        The listener body is quoted twice (outer ``setsid bash -c``, inner
        ``python3 -c``). A stub ``python3`` on ``PATH`` captures the exact
        argv it receives, so real bash performs the unquoting rather than a
        reimplementation of it in the test — then the captured payload must
        compile as Python.
        """
        out_file = tmp_path / "captured.py"
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "python3"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "-c" ]; then printf %s "$2" > "$CAPTURE_OUT"; fi\n'
            "exit 0\n"
        )
        stub.chmod(0o755)

        cmd = listener_command("python3", 5205, tag="otto-check-abc123")
        # Run synchronously: the composed command backgrounds itself with a
        # trailing "&"; strip it so this call blocks until the stub exits.
        sync_cmd = cmd.rstrip().removesuffix("&")
        subprocess.run(
            ["bash", "-c", sync_cmd],
            check=True,
            env={
                **os.environ,
                "PATH": f"{stub_dir}:{os.environ['PATH']}",
                "CAPTURE_OUT": str(out_file),
            },
        )
        compile(out_file.read_text(), "<listener-echo>", "exec")

    def test_python3_listener_keeps_real_argv0_and_trails_the_tag(self, tmp_path: Path) -> None:
        """python3 must NOT be tagged via ``exec -a`` — see ``listener_command``'s

        docstring: renaming argv[0] breaks a relocatable CPython build's
        stdlib self-location. A stub ``python3`` captures its own argv[0]
        (as bash actually invoked it) and its trailing argument, so real
        bash — not a reimplementation here — proves both: argv[0] stays
        "python3", and the tag rides along as ``sys.argv[1]``.
        """
        argv0_file = tmp_path / "argv0"
        tag_file = tmp_path / "tag"
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "python3"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf %s "$0" > "$ARGV0_OUT"\n'
            'if [ "$1" = "-c" ]; then printf %s "$3" > "$TAG_OUT"; fi\n'
            "exit 0\n"
        )
        stub.chmod(0o755)

        cmd = listener_command("python3", 5205, tag="otto-check-abc123")
        assert "exec -a" not in cmd
        sync_cmd = cmd.rstrip().removesuffix("&")
        subprocess.run(
            ["bash", "-c", sync_cmd],
            check=True,
            env={
                **os.environ,
                "PATH": f"{stub_dir}:{os.environ['PATH']}",
                "ARGV0_OUT": str(argv0_file),
                "TAG_OUT": str(tag_file),
            },
        )
        assert Path(argv0_file.read_text()).name == "python3"
        assert tag_file.read_text() == "otto-check-abc123"

    @pytest.mark.parametrize("build", ["connect", "transfer"])
    def test_the_socat_clock_survives_the_check_sh_wrap(self, tmp_path: Path, build: str) -> None:
        """``check_root_run`` sends ``sh -c <cmd>``; ``sh`` may be dash, which has no clock.

        So the socat clients carry their own ``bash -c`` layer. Run the exact
        text ``check_root_run`` hands the host through a login shell, with a
        stub ``socat`` on ``PATH``, and the elapsed time must still parse.
        """
        if shutil.which("bash") is None:
            pytest.skip("bash not installed")
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "socat"
        stub.write_text("#!/bin/sh\nexec cat\n")  # an echo: what socat relays back
        stub.chmod(0o755)
        if build == "connect":
            cmd = timed_connect_command("socat", "127.0.0.1", 5205)
        else:
            cmd = timed_transfer_command("socat", "127.0.0.1", 5205, 4096)
        result = subprocess.run(
            ["bash", "-c", one_command(cmd)],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
        )
        elapsed = parse_elapsed_ms(result.stdout)
        assert elapsed is not None, result.stdout
        assert elapsed >= 0

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_timed_connect_command_runs_against_a_real_echo_server(
        self, backend: str, echo_server: int
    ) -> None:
        """Run the built client command, unmodified, against a real TCP echo server.

        This is the strongest check that ``shlex.quote`` produced something
        bash and the target interpreter both accept: real bash parses the
        command, the embedded program actually runs, and its own printed
        elapsed time parses back out. The listener side is exercised
        separately (compile + syntax checks) rather than through a live
        ``exec -a`` process — this dev VM's relocatable python3 build cannot
        resolve its own stdlib once ``exec -a`` renames argv[0], an
        environment quirk unrelated to the commands otto builds, and the
        echo server's protocol doesn't care which backend built the
        listener that would normally be on the other end.
        """
        if shutil.which(backend) is None:
            pytest.skip(f"{backend} not installed")
        cmd = timed_connect_command(backend, "127.0.0.1", echo_server)
        result = subprocess.run(["bash", "-c", cmd], check=True, capture_output=True, text=True)
        elapsed = parse_elapsed_ms(result.stdout)
        assert elapsed is not None
        assert elapsed >= 0

    @pytest.mark.parametrize("backend", ["python3", "socat"])
    def test_timed_transfer_command_runs_against_a_real_echo_server(
        self, backend: str, echo_server: int
    ) -> None:
        if shutil.which(backend) is None:
            pytest.skip(f"{backend} not installed")
        cmd = timed_transfer_command(backend, "127.0.0.1", echo_server, 4096)
        result = subprocess.run(["bash", "-c", cmd], check=True, capture_output=True, text=True)
        elapsed = parse_elapsed_ms(result.stdout)
        assert elapsed is not None
        assert elapsed >= 0


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A port that completes the TCP handshake (the kernel's backlog) and never answers."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture
def foreign_server() -> Iterator[int]:
    """A service that is not otto's echo: it greets every connection with a banner and hangs up."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()


def _stub_socat(tmp_path: Path, body: str) -> dict[str, str]:
    """Build an environment whose ``socat`` is a shell stub running *body* on the client's input."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "socat"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return {**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"}


class TestClientsTrustOnlyTheirOwnEcho:
    """A client that reached some OTHER service must fail, never read as a listener that is up."""

    def test_python3_connect_rejects_a_foreign_banner(self, foreign_server: int) -> None:
        if shutil.which("python3") is None:
            pytest.skip("python3 not installed")
        cmd = timed_connect_command("python3", "127.0.0.1", foreign_server)
        result = subprocess.run(["bash", "-c", cmd], check=False, capture_output=True, text=True)
        assert result.returncode != 0, result.stdout
        assert "echo answered b'S', not x" in result.stdout
        assert parse_elapsed_ms(result.stdout) is not None, "the clock still prints, as evidence"

    def test_socat_connect_rejects_a_foreign_banner(self, tmp_path: Path) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not installed")
        env = _stub_socat(tmp_path, "cat >/dev/null\necho SSH-2.0-OpenSSH_9.6")
        cmd = timed_connect_command("socat", "127.0.0.1", 5205)
        result = subprocess.run(
            ["bash", "-c", one_command(cmd)], check=False, capture_output=True, text=True, env=env
        )
        assert result.returncode != 0, result.stdout
        assert parse_elapsed_ms(result.stdout) is not None

    def test_socat_transfer_fails_on_a_short_echo(self, tmp_path: Path) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not installed")
        env = _stub_socat(tmp_path, "head -c 100")
        cmd = timed_transfer_command("socat", "127.0.0.1", 5299, 4096)
        result = subprocess.run(
            ["bash", "-c", one_command(cmd)], check=False, capture_output=True, text=True, env=env
        )
        assert result.returncode != 0, result.stdout
        assert "echoed 100 of 4096 bytes" in result.stdout
        assert parse_elapsed_ms(result.stdout) is not None

    @pytest.mark.parametrize("build", ["connect", "transfer"])
    def test_socat_scripts_parse_in_bash(self, build: str) -> None:
        """The socat clients' own ``bash -c`` layer, not just the outer word, must parse."""
        if shutil.which("bash") is None:
            pytest.skip("bash not installed")
        if build == "connect":
            cmd = timed_connect_command("socat", "198.18.0.2", 5205)
        else:
            cmd = timed_transfer_command("socat", "198.18.0.2", 5299, 262144)
        words = shlex.split(cmd)
        assert words[:2] == ["bash", "-c"]
        parsed = subprocess.run(
            ["bash", "-n", "-c", words[2]], check=False, capture_output=True, text=True
        )
        assert parsed.returncode == 0, parsed.stderr


class TestClientsEndOnTheirOwn:
    """A probe that stalls must end by itself, inside its bound, with its clock printed."""

    @pytest.mark.parametrize("build", ["connect", "transfer"])
    def test_python3_client_gives_up_at_its_deadline(self, build: str, silent_server: int) -> None:
        if shutil.which("python3") is None:
            pytest.skip("python3 not installed")
        if build == "connect":
            cmd = timed_connect_command("python3", "127.0.0.1", silent_server, within=0.5)
        else:
            cmd = timed_transfer_command("python3", "127.0.0.1", silent_server, 4096, within=0.5)
        started = time.monotonic()
        result = subprocess.run(
            ["bash", "-c", cmd], check=False, capture_output=True, text=True, timeout=10
        )
        took = time.monotonic() - started
        assert result.returncode != 0, result.stdout
        assert took < 5, f"the client ran {took:.1f}s past a 0.5s bound"
        elapsed = parse_elapsed_ms(result.stdout)
        assert elapsed is not None, result.stdout
        assert elapsed >= 450, "its own clock shows it ran to the bound"
        assert "timed out" in result.stdout
