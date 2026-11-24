"""The netcat rung scripts, run for real under ``/bin/sh`` with stand-in tools.

Each script is run as built, with ``PATH`` scoped to a temp directory that
holds stand-ins for its tool and links to the helpers. Output is read as one
merged stream, which is what a pty session hands otto; a second run with
stderr apart proves every complaint went to ``/dev/null``.
"""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from otto.host.transfer.nc_scripts import (
    TOOL_FAILED,
    netstat_listener_script,
    netstat_port_script,
    proc_listener_script,
    proc_port_script,
    python_port_command,
    ss_listener_script,
    ss_port_script,
)
from tests._fixtures.nc_listings import (
    COMPLAINT,
    NETSTAT_HEADER,
    OTHER,
    PORT,
    SS_HEADER,
    TCP_HEADER,
    netstat_listing,
    one_port,
    ss_listing,
    tcp_row,
)

_HELPERS = ("sort", "tr", "sed", "grep", "cat", "printf")
_SS_LISTING = ss_listing(PORT)
_NETSTAT_LISTING = netstat_listing(PORT)


def _bin_dir(tmp_path: Path, stand_ins: dict[str, str]) -> Path:
    """A PATH directory: links to the helpers plus executable stand-ins."""
    d = tmp_path / "bin"
    d.mkdir()
    for name in _HELPERS:
        real = shutil.which(name)
        if real is not None:
            (d / name).symlink_to(real)
    for name, body in stand_ins.items():
        tool = d / name
        tool.write_text("#!/bin/sh\n" + body)
        tool.chmod(0o755)
    return d


def _printing(stdout: str, *, status: int = 0, stderr: str = "") -> str:
    """A stand-in body that prints *stdout* (and *stderr*) and exits *status*."""
    body = f"cat <<'EOF'\n{stdout}EOF\n"
    if stderr:
        body = f"echo '{stderr}' >&2\n" + body
    return body + f"exit {status}\n"


def _run(script: str, path: Path) -> str:
    """Run *script* with one merged stream, and assert stderr alone is empty."""
    merged = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": str(path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        check=False,
    )
    apart = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": str(path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert apart.stderr == "", f"stderr leaked: {apart.stderr!r}"
    return merged.stdout


_PORT_TOOLS = [
    pytest.param("ss", ss_port_script, id="ss"),
    pytest.param("netstat", netstat_port_script, id="netstat"),
]
_LISTENER_TOOLS = [
    pytest.param("ss", ss_listener_script, id="ss"),
    pytest.param("netstat", netstat_listener_script, id="netstat"),
]
_LISTINGS = {"ss": _SS_LISTING, "netstat": _NETSTAT_LISTING}
_HEADERS = {"ss": SS_HEADER, "netstat": NETSTAT_HEADER}


class TestAMissingToolGivesTheSentinel:
    @pytest.mark.parametrize(("tool", "build"), _PORT_TOOLS)
    def test_port_script(self, tool, build, tmp_path):
        out = _run(build(base_port=PORT, reserved=""), _bin_dir(tmp_path, {}))
        assert out == f"{TOOL_FAILED}\n"

    @pytest.mark.parametrize(("tool", "build"), _LISTENER_TOOLS)
    def test_listener_script(self, tool, build, tmp_path):
        out = _run(build(PORT), _bin_dir(tmp_path, {}))
        assert out == f"{TOOL_FAILED}\n"

    def test_proc_scripts_over_an_empty_directory(self, tmp_path):
        net = tmp_path / "net"
        net.mkdir()
        path = _bin_dir(tmp_path, {})
        assert _run(proc_port_script(base_port=PORT, reserved="", proc_net=str(net)), path) == (
            f"{TOOL_FAILED}\n"
        )
        assert _run(proc_listener_script(PORT, proc_net=str(net)), path) == f"{TOOL_FAILED}\n"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_proc_scripts_over_an_unreadable_tcp(self, tmp_path):
        net = tmp_path / "net"
        net.mkdir()
        tcp = net / "tcp"
        tcp.write_text(TCP_HEADER + tcp_row(0, PORT))
        tcp.chmod(0)
        path = _bin_dir(tmp_path, {})
        try:
            port_out = _run(proc_port_script(base_port=PORT, reserved="", proc_net=str(net)), path)
            count_out = _run(proc_listener_script(PORT, proc_net=str(net)), path)
        finally:
            tcp.chmod(0o644)
        assert port_out == f"{TOOL_FAILED}\n"
        assert count_out == f"{TOOL_FAILED}\n"

    def test_python_without_an_interpreter(self, tmp_path):
        script = python_port_command("python")
        path = _bin_dir(tmp_path, {})
        merged = subprocess.run(
            ["/bin/sh", "-c", script],
            env={"PATH": str(path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            check=False,
        )
        assert merged.returncode != 0
        assert merged.stdout == ""


class TestAToolThatFailsAfterPrinting:
    @pytest.mark.parametrize(("tool", "build"), _PORT_TOOLS)
    def test_port_script(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_LISTINGS[tool], status=1)})
        assert _run(build(base_port=PORT, reserved=""), path) == f"{TOOL_FAILED}\n"

    @pytest.mark.parametrize(("tool", "build"), _LISTENER_TOOLS)
    def test_listener_script(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_LISTINGS[tool], status=1)})
        assert _run(build(PORT), path) == f"{TOOL_FAILED}\n"


class TestAToolThatSucceedsWithNothing:
    @pytest.mark.parametrize(("tool", "build"), _PORT_TOOLS)
    def test_port_script_takes_the_base_port(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_HEADERS[tool])})
        assert _run(build(base_port=PORT, reserved=""), path) == f"{PORT}\n"

    @pytest.mark.parametrize(("tool", "build"), _LISTENER_TOOLS)
    def test_listener_script_counts_zero(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_HEADERS[tool])})
        assert _run(build(PORT), path) == "0\n"


class TestAToolThatComplainsAndStillAnswers:
    @pytest.mark.parametrize(("tool", "build"), _PORT_TOOLS)
    def test_port_script_skips_the_listed_port(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_LISTINGS[tool], stderr=COMPLAINT)})
        out = _run(build(base_port=PORT, reserved=""), path)
        assert one_port(out) != PORT

    @pytest.mark.parametrize(("tool", "build"), _LISTENER_TOOLS)
    def test_listener_script_counts_the_listed_port(self, tool, build, tmp_path):
        path = _bin_dir(tmp_path, {tool: _printing(_LISTINGS[tool], stderr=COMPLAINT)})
        assert _run(build(PORT), path) == "1\n"


class TestABackslashInTheListing:
    def test_does_not_cut_the_listing_short(self, tmp_path):
        """Dash's ``echo`` reads ``\\c`` as "stop here"; the listing is printed unchanged."""
        listing = (
            SS_HEADER
            + 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("a\\cb",pid=1,fd=3))\n'
            + f"LISTEN 0 128 0.0.0.0:{PORT} 0.0.0.0:*\n"
        )
        path = _bin_dir(tmp_path, {"ss": _printing(listing)})
        assert _run(ss_listener_script(PORT), path) == "1\n"


class TestProcWithoutIpv6:
    def _net(self, tmp_path: Path, *, v6_port: int | None = None) -> Path:
        net = tmp_path / "net"
        net.mkdir()
        (net / "tcp").write_text(TCP_HEADER + tcp_row(0, PORT) + tcp_row(1, 22))
        if v6_port is not None:
            (net / "tcp6").write_text(TCP_HEADER + tcp_row(0, v6_port, v6=True))
        return net

    def test_port_script_skips_a_listed_port(self, tmp_path):
        net = self._net(tmp_path)
        out = _run(
            proc_port_script(base_port=PORT, reserved="", proc_net=str(net)),
            _bin_dir(tmp_path, {}),
        )
        assert one_port(out) != PORT

    def test_listener_script_counts(self, tmp_path):
        net = self._net(tmp_path)
        path = _bin_dir(tmp_path, {})
        assert _run(proc_listener_script(PORT, proc_net=str(net)), path) == "1\n"
        assert _run(proc_listener_script(OTHER, proc_net=str(net)), path) == "0\n"

    def test_a_tcp6_listener_is_skipped_and_counted(self, tmp_path):
        net = self._net(tmp_path, v6_port=OTHER)
        path = _bin_dir(tmp_path, {})
        out = _run(proc_port_script(base_port=OTHER, reserved="", proc_net=str(net)), path)
        assert one_port(out) != OTHER
        assert _run(proc_listener_script(OTHER, proc_net=str(net)), path) == "1\n"


@pytest.fixture
def dual_stack_port():
    """A port held by one dual-stack listener (``[::]``, ``IPV6_V6ONLY=0``)."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"no IPv6 socket on this machine: {exc}")
    try:
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", 0))
        except OSError as exc:
            pytest.skip(f"no dual-stack listener on this machine: {exc}")
        sock.listen(1)
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _real_rungs() -> list:
    rungs = [pytest.param("proc", id="proc")]
    rungs += [pytest.param(t, id=t) for t in ("ss", "netstat") if shutil.which(t)]
    return rungs


def _recording(tmp_path: Path, tool: str) -> tuple[str, Path]:
    """A ``PATH`` whose *tool* is the real one, its stdout also appended to a log.

    The log holds what the tool printed in the very run being judged, so a bad
    count is shown beside the listing that produced it.
    """
    real = shutil.which(tool)
    real_cat = shutil.which("cat")
    assert real is not None
    assert real_cat is not None
    wrap = tmp_path / "recording"
    wrap.mkdir()
    log = tmp_path / f"{tool}.log"
    out = tmp_path / f"{tool}.out"
    (wrap / tool).write_text(
        "#!/bin/sh\n"
        f'"{real}" "$@" > "{out}"; s=$?\n'
        f'"{real_cat}" "{out}"; "{real_cat}" "{out}" >> "{log}"\n'
        "exit $s\n"
    )
    (wrap / tool).chmod(0o755)
    return f"{wrap}{os.pathsep}{os.environ.get('PATH', '')}", log


class TestADualStackListener:
    @pytest.mark.parametrize("rung", _real_rungs())
    def test_is_counted_once_and_skipped(self, rung, dual_stack_port, tmp_path):
        # The proc rung reads its listing with cat.
        path, log = _recording(tmp_path, "cat" if rung == "proc" else rung)
        listener = {
            "ss": ss_listener_script,
            "netstat": netstat_listener_script,
            "proc": proc_listener_script,
        }[rung]
        port_script = {
            "ss": ss_port_script,
            "netstat": netstat_port_script,
            "proc": proc_port_script,
        }[rung]
        count = subprocess.run(
            ["/bin/sh", "-c", listener(dual_stack_port)],
            env={"PATH": path},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        assert count == "1\n", (
            f"counted {count!r} for port {dual_stack_port} from this listing:\n"
            + (log.read_text() if log.exists() else "<the tool printed nothing>")
        )
        free = subprocess.run(
            ["/bin/sh", "-c", port_script(base_port=dual_stack_port, reserved="")],
            env={"PATH": path},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            check=False,
        ).stdout
        assert one_port(free) != dual_stack_port
