"""The inventory parser over CAPTURED output, and the runner's elevation rule.

Fixtures are real `ss`/`netstat`/`/proc` output. A blank owner is `owner
unknown`, never a parse failure; loopback-only binds are dropped and counted;
the runner tries elevation once and falls back to plain, never twice.
"""

import shlex
import subprocess
from pathlib import Path

import pytest

from otto.host.errors import UnsupportedOnUserlandError
from otto.host.privilege import _SUDO_PROMPT
from otto.host.survey.inventory import (
    _INVENTORY_BODY,
    INVENTORY_SCRIPT,
    Inventory,
    Listener,
    is_loopback,
    parse_inventory,
    run_inventory,
)
from otto.result import CommandResult, NotRunResult
from otto.utils import Status

_FIX = Path(__file__).parent / "_fixtures"


def _load(name: str) -> str:
    return (_FIX / name).read_text()


def _tcp(port, owner=None, address="0.0.0.0"):
    return Listener(transport="tcp", address=address, port=port, owner=owner)


def _udp(port, owner=None, address="0.0.0.0"):
    return Listener(transport="udp", address=address, port=port, owner=owner)


def test_ss_rows_carry_owners_and_loopback_is_dropped_and_counted():
    """Mutation: keep loopback rows and nginx on 127.0.0.1:80 becomes an 'other listener'."""
    inv = parse_inventory(_load("ss_tulnp.txt"), elevated=True)
    assert inv.tool == "ss"
    assert inv.elevated is True
    assert inv.loopback_dropped == 2  # chronyd 127.0.0.1:323, nginx 127.0.0.1:80
    assert inv.unparsed == 0
    assert inv.listeners == [
        _udp(161, "snmpd"),
        _tcp(22, "sshd"),
        _tcp(2222, "sshd"),
        _tcp(21, "vsftpd", address="*"),
        _tcp(22, "sshd", address="[::]"),
    ]


def test_gnu_netstat_blank_owner_is_unknown_not_a_parse_failure():
    inv = parse_inventory(_load("netstat_gnu.txt"), elevated=False)
    assert inv.tool == "netstat"
    assert inv.unparsed == 0
    assert inv.listeners == [_tcp(22), _tcp(2121, "vsftpd"), _tcp(22, address="::"), _udp(161)]


def test_netstat_listen_with_no_pid_column_and_no_trailing_space_is_not_an_owner():
    """Mutation: match LISTEN only when followed by whitespace and owner becomes 'LISTEN'."""
    text = (
        "OTTO_TOOL=netstat\n"
        "tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN"
    )
    inv = parse_inventory(text, elevated=False)
    assert inv.listeners == [_tcp(22)]
    assert inv.unparsed == 0


def test_busybox_netstat_without_root_reports_unknown_owners():
    inv = parse_inventory(_load("netstat_busybox_noowner.txt"), elevated=False)
    assert inv.listeners == [_tcp(23), _udp(69)]
    assert inv.unparsed == 0


def test_busybox_netstat_as_root_names_the_applet_owner():
    """Mutation: strip the PID/ prefix wrongly and `91/busybox` becomes owner '91'."""
    inv = parse_inventory(_load("netstat_busybox_root.txt"), elevated=True)
    assert inv.listeners == [_tcp(23, "telnetd"), _tcp(2323, "busybox"), _udp(69, "udpsvd")]


def test_proc_walk_decodes_hex_ports_and_joins_inodes_to_comm():
    """Mutation: read state 01 as a listener and the ESTABLISHED row to 10.10.10.10 appears."""
    inv = parse_inventory(_load("proc_walk.txt"), elevated=True)
    assert inv.tool == "proc"
    assert inv.loopback_dropped == 1
    assert inv.listeners == [_tcp(22, "sshd"), _tcp(2222, "sshd", address="::"), _udp(161, "snmpd")]


def test_unrecognised_lines_are_counted_not_fatal():
    text = (
        "OTTO_TOOL=ss\nNetid State\ngarbage line here\n"
        'tcp LISTEN 0 1 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
    )
    inv = parse_inventory(text, elevated=False)
    assert inv.unparsed == 1
    assert inv.listeners == [_tcp(22, "sshd")]


def test_missing_tool_marker_is_an_error_not_an_empty_inventory():
    inv = parse_inventory("bash: ss: command not found\n", elevated=False)
    assert inv.error.startswith("inventory: unrecognised output")
    assert inv.listeners == []


@pytest.mark.parametrize(
    ("address", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("*", False),
        ("::", False),
        ("[::]", False),
        (":::", False),
        ("10.0.0.5", False),
    ],
)
def test_is_loopback(address, loopback):
    assert is_loopback(address) is loopback


def test_the_script_prefers_ss_then_netstat_then_proc_and_is_one_command():
    assert _INVENTORY_BODY.startswith("if command -v ss ")
    assert "elif command -v netstat " in _INVENTORY_BODY
    assert "OTTO_TOOL=proc" in _INVENTORY_BODY
    assert "\n" not in INVENTORY_SCRIPT.replace(" \\\n", " ")


def test_the_script_is_one_shell_word_carrying_the_body():
    """``sh -c '<body>'`` and nothing else: three words, the third the whole script."""
    words = shlex.split(INVENTORY_SCRIPT)
    assert words == ["sh", "-c", _INVENTORY_BODY], words


_ELEVATED = {
    # The two textual prefixes PosixPrivilege._elevate builds, verbatim.
    "sudo": f"sudo -S -p '{_SUDO_PROMPT}' {INVENTORY_SCRIPT}",
    "su": "su -c " + shlex.quote(INVENTORY_SCRIPT),
}


@pytest.mark.parametrize("mechanism", list(_ELEVATED))
def test_the_elevated_command_parses_as_one_command(mechanism):
    """What ``PosixPrivilege._elevate`` builds must survive bash's parser.

    Both elevation forms are TEXTUAL PREFIXES (privilege.py), so a bare
    multi-statement body lands in sudo's argument list, where ``if`` is a
    literal word and the later ``then`` has no matching ``if`` -- bash then
    refuses the whole input line, sentinels included, and the caller hangs to
    its own timeout (mutation: unwrap INVENTORY_SCRIPT and the sudo case
    fails with "syntax error near unexpected token `then'").

    ``bash -n`` parses without executing, so no elevation is ever attempted
    and this test never contacts a host.
    """
    parsed = subprocess.run(
        ["bash", "-n", "-c", _ELEVATED[mechanism]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert parsed.returncode == 0, (mechanism, parsed.stderr)


def test_the_inner_script_is_posix_sh():
    """The body runs under ``sh``, not bash: ``sh -n`` must accept it as written."""
    parsed = subprocess.run(
        ["sh", "-n", "-c", _INVENTORY_BODY],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr


class _Run:
    """A scripted `run`: one queued CommandResult per call, records the sudo flag."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[bool] = []

    async def __call__(self, cmd, *, sudo=False):
        assert cmd == INVENTORY_SCRIPT
        self.calls.append(sudo)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text):
    return CommandResult(status=Status.Success, value=text, command="x", retcode=0)


def _fail(text, retcode=1):
    return CommandResult(status=Status.Failed, value=text, command="x", retcode=retcode)


@pytest.mark.asyncio
async def test_elevated_run_is_tried_once_then_plain_never_twice():
    """Mutation: retry elevation and `calls` gains a second True."""
    run = _Run(_fail("sudo: a password is required"), _ok(_load("netstat_gnu.txt")))
    inv = await run_inventory(run, elevate=True)
    assert run.calls == [True, False]
    assert inv.elevated is False
    assert inv.tool == "netstat"
    assert inv.error == ""


@pytest.mark.asyncio
async def test_no_elevation_means_one_plain_run():
    run = _Run(_ok(_load("ss_tulnp.txt")))
    inv = await run_inventory(run, elevate=False)
    assert run.calls == [False]
    assert inv.elevated is False


@pytest.mark.asyncio
async def test_a_successful_elevated_run_is_marked_elevated():
    run = _Run(_ok(_load("ss_tulnp.txt")))
    inv = await run_inventory(run, elevate=True)
    assert run.calls == [True]
    assert inv.elevated is True


@pytest.mark.asyncio
async def test_a_failed_plain_run_is_an_error_inventory_with_the_detail():
    run = _Run(_fail("sh: syntax error", retcode=2))
    inv = await run_inventory(run, elevate=False)
    assert inv == Inventory(
        tool="",
        elevated=False,
        listeners=[],
        loopback_dropped=0,
        unparsed=0,
        error="inventory failed: exit 2: sh: syntax error",
    )


@pytest.mark.asyncio
async def test_an_exception_from_the_wire_is_an_error_inventory():
    run = _Run(ConnectionError("Network disconnected"))
    inv = await run_inventory(run, elevate=False)
    assert inv.error == "inventory failed: ConnectionError: Network disconnected"


@pytest.mark.asyncio
async def test_a_declined_dry_run_is_an_error_inventory_and_never_raises():
    """Mutation: read `.value` outside the try and a declined dry run raises instead of erroring."""
    declined = NotRunResult(status=Status.NotRun, command="x", retcode=-1, host_name="box")
    run = _Run(declined)
    inv = await run_inventory(run, elevate=False)
    assert inv == Inventory(
        tool="",
        elevated=False,
        listeners=[],
        loopback_dropped=0,
        unparsed=0,
        error="inventory failed: not run: declined",
    )
    assert inv.error.startswith("inventory failed: not run")


@pytest.mark.asyncio
async def test_a_userland_elevation_refusal_propagates_instead_of_falling_back():
    """A refusal means the resolution and the host disagree — an error, not a quiet plain run.

    Elevation is asked for only when the userland resolved ``sudo``/``su`` AND
    the cred has a password to answer it, so ``UnsupportedOnUserlandError`` is
    the two disagreeing, which the operator has to see. A sudo the TARGET
    refuses (non-zero retcode / NotRun) still falls back — that is the test
    above, and it is unchanged.

    Mutation: swallow it in the broad handler and this falls back to a plain
    run, reporting an unelevated inventory as if nothing went wrong.
    """
    run = _Run(UnsupportedOnUserlandError("no sudo and no su on this userland"))
    with pytest.raises(UnsupportedOnUserlandError):
        await run_inventory(run, elevate=True)
    assert run.calls == [True]
