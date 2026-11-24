"""The netcat rung scripts under each BusyBox version's applets.

Every row reads one merged stream, which is what a pty session hands otto, so
a tool that complains on stderr and still answers must still give one clean
line. Rows that rely on a stand-in or on a tool's absence run through dash
over ``ROWS`` (dash resolves names through ``PATH``; the system row's own
shell does not, see ``test_applet_contracts.py``'s module docstring), and again
under each pinned artifact's own ``busybox sh`` over ``ARTIFACT_ROWS``. Each
stand-in touches a witness file, and the row asserts it ran.

BusyBox ships no ``ss``: its rows use a stand-in printing canned iproute2
output, so they test the filter under each version's helpers.
"""

import socket
from pathlib import Path

import pytest

from otto.host.transfer.nc_scripts import (
    TOOL_FAILED,
    netstat_listener_script,
    netstat_port_script,
    proc_listener_script,
    proc_port_script,
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
    one_port,
    ss_listing,
    tcp_row,
)

from ._rows import ARTIFACT_ROWS, ROWS, binary_for, run_in_busybox_sh, run_via_dash, stand_in_applet

_HELPERS = ["cat", "grep", "tr", "sed", "sort", "printf"]


def _drive(driver: str, release, applets: Path, script: str):
    if driver == "dash":
        return run_via_dash(applets, script, merge_stderr=True)
    return run_in_busybox_sh(release, applets, script, merge_stderr=True)


def _rows_for_each_driver():
    """Dash over every row, and each pinned artifact under its own shell."""
    dash = [pytest.param(r.values[0], "dash", id=f"dash-{r.id}", marks=r.marks) for r in ROWS]
    ash = [pytest.param(r.values[0], "ash", id=f"ash-{r.id}", marks=r.marks) for r in ARTIFACT_ROWS]
    return dash + ash


DRIVEN = _rows_for_each_driver()


@pytest.fixture
def held_port():
    """A port held by one IPv4 listener on loopback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _applets(applet_dir, release, extra=()):
    return applet_dir(release, [*_HELPERS, *extra])


def _witnessed(applets: Path, name: str, body: str) -> Path:
    witness = applets.parent / f"{applets.name}-{name}.ran"
    stand_in_applet(applets, name, f": > '{witness}'\n{body}")
    return witness


def _cat(text: str) -> str:
    return f"cat <<'EOF'\n{text}EOF\n"


# --- netstat, the real applet ---------------------------------------------


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_netstat_port_scan_skips_a_held_port(release, driver, applet_dir, held_port):
    applets = _applets(applet_dir, release, ["netstat"])
    out = _drive(driver, release, applets, netstat_port_script(base_port=held_port, reserved=""))
    assert one_port(out.stdout) != held_port


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_netstat_listener_counts_a_held_port(release, driver, applet_dir, held_port):
    applets = _applets(applet_dir, release, ["netstat"])
    out = _drive(driver, release, applets, netstat_listener_script(held_port))
    assert out.stdout == "1\n"


# --- netstat that complains about a missing tcp6, then answers ---------------


def _complaining_netstat(applets: Path, release) -> Path:
    return _witnessed(
        applets,
        "netstat",
        f"echo '{COMPLAINT}' >&2\nexec '{binary_for(release)}' netstat \"$@\"\n",
    )


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_a_complaining_netstat_port_scan_gives_one_clean_port(
    release, driver, applet_dir, held_port
):
    applets = _applets(applet_dir, release)
    witness = _complaining_netstat(applets, release)
    out = _drive(driver, release, applets, netstat_port_script(base_port=held_port, reserved=""))
    assert witness.exists()
    assert one_port(out.stdout) != held_port


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_a_complaining_netstat_listener_gives_one_clean_count(
    release, driver, applet_dir, held_port
):
    applets = _applets(applet_dir, release)
    witness = _complaining_netstat(applets, release)
    out = _drive(driver, release, applets, netstat_listener_script(held_port))
    assert witness.exists()
    assert out.stdout == "1\n"


# --- ss, a stand-in (BusyBox ships none) ----------------------------------


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_ss_port_scan_skips_a_listed_port(release, driver, applet_dir):
    applets = _applets(applet_dir, release)
    witness = _witnessed(applets, "ss", _cat(ss_listing(PORT)))
    out = _drive(driver, release, applets, ss_port_script(base_port=PORT, reserved=""))
    assert witness.exists()
    assert one_port(out.stdout) != PORT


@pytest.mark.busybox
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_ss_listener_counts_a_listed_port(release, driver, applet_dir):
    applets = _applets(applet_dir, release)
    witness = _witnessed(applets, "ss", _cat(ss_listing(PORT)))
    out = _drive(driver, release, applets, ss_listener_script(PORT))
    assert witness.exists()
    assert out.stdout == "1\n"


# --- /proc over a synthetic directory, tcp6 present and absent -------------


def _proc_net(tmp_path: Path, *, v6_port: int | None) -> Path:
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(TCP_HEADER + tcp_row(0, PORT) + tcp_row(1, 22))
    if v6_port is not None:
        (net / "tcp6").write_text(TCP_HEADER + tcp_row(0, v6_port, v6=True))
    return net


@pytest.mark.busybox
@pytest.mark.parametrize("v6", [False, True], ids=["no-tcp6", "tcp6"])
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_proc_port_scan_skips_listed_ports(release, driver, v6, applet_dir, tmp_path):
    applets = _applets(applet_dir, release)
    net = _proc_net(tmp_path, v6_port=OTHER if v6 else None)
    start = OTHER if v6 else PORT
    out = _drive(
        driver, release, applets, proc_port_script(base_port=start, reserved="", proc_net=str(net))
    )
    assert one_port(out.stdout) != start


@pytest.mark.busybox
@pytest.mark.parametrize("v6", [False, True], ids=["no-tcp6", "tcp6"])
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_proc_listener_counts(release, driver, v6, applet_dir, tmp_path):
    applets = _applets(applet_dir, release)
    net = _proc_net(tmp_path, v6_port=OTHER if v6 else None)
    listed = _drive(driver, release, applets, proc_listener_script(PORT, proc_net=str(net)))
    other = _drive(driver, release, applets, proc_listener_script(OTHER, proc_net=str(net)))
    assert listed.stdout == "1\n"
    assert other.stdout == ("1\n" if v6 else "0\n")


# --- missing tool and zero count, per rung ---------------------------------

_PORT_RUNGS = {
    "ss": lambda net: ss_port_script(base_port=PORT, reserved=""),
    "netstat": lambda net: netstat_port_script(base_port=PORT, reserved=""),
    "proc": lambda net: proc_port_script(base_port=PORT, reserved="", proc_net=str(net)),
}
_LISTENER_RUNGS = {
    "ss": lambda net: ss_listener_script(PORT),
    "netstat": lambda net: netstat_listener_script(PORT),
    "proc": lambda net: proc_listener_script(PORT, proc_net=str(net)),
}


@pytest.mark.busybox
@pytest.mark.parametrize("rung", ["ss", "netstat", "proc"])
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_a_missing_tool_gives_the_sentinel(release, driver, rung, applet_dir, tmp_path):
    applets = _applets(applet_dir, release)
    empty = tmp_path / "empty-net"
    empty.mkdir()
    port = _drive(driver, release, applets, _PORT_RUNGS[rung](empty))
    count = _drive(driver, release, applets, _LISTENER_RUNGS[rung](empty))
    assert port.stdout == f"{TOOL_FAILED}\n"
    assert count.stdout == f"{TOOL_FAILED}\n"


@pytest.mark.busybox
@pytest.mark.parametrize("rung", ["ss", "netstat", "proc"])
@pytest.mark.parametrize(("release", "driver"), DRIVEN)
def test_an_empty_listing_is_nothing_in_use(release, driver, rung, applet_dir, tmp_path):
    applets = _applets(applet_dir, release)
    witness = None
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(TCP_HEADER)
    if rung == "ss":
        witness = _witnessed(applets, "ss", _cat(SS_HEADER))
    elif rung == "netstat":
        witness = _witnessed(
            applets,
            "netstat",
            _cat(NETSTAT_HEADER),
        )
    port = _drive(driver, release, applets, _PORT_RUNGS[rung](net))
    count = _drive(driver, release, applets, _LISTENER_RUNGS[rung](net))
    if witness is not None:
        assert witness.exists()
    assert port.stdout == f"{PORT}\n"
    assert count.stdout == "0\n"
