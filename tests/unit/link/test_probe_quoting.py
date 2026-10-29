"""``otto link check``'s probe commands take an address as data, in shell, socat and Python.

The commands are run for real under ``sh -c`` against recording stubs
(:mod:`tests._fixtures.argv_stub`) for ``socat``, ``python3``, ``ping`` and
``ip``; a stub records its argv and exits, so nothing is contacted. The
python3 clients' source is parsed, and the address must be one string
literal in it.
"""

import ast
import shlex

import pytest

from otto.link.probes import (
    listener_command,
    ping_command,
    timed_connect_command,
    timed_transfer_command,
)
from tests._fixtures.argv_stub import (
    SOCAT_REFUSAL,
    SOCAT_REFUSED,
    hostile_values,
    recorded,
    run_sh,
    stub_env,
)

TAG = "otto-check-abc123"


def _python_hostile(marker) -> list[str]:
    """Values that would run Python if spliced between quotes in the source."""
    return [
        *hostile_values(marker),
        f"x', 0) or open({str(marker)!r}, 'w') and ('",
        f'x"); open({str(marker)!r}, "w"); ("',
    ]


def _the_literal(program: str, value: str) -> ast.Constant:
    """The one string literal in *program* equal to *value*; fails if there is not exactly one."""
    tree = ast.parse(program)
    found = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == value]
    assert len(found) == 1, program
    return found[0]


def _connect_target(program: str) -> ast.expr:
    """The first element of the ``(host, port)`` tuple handed to ``socket.create_connection``."""
    for node in ast.walk(ast.parse(program)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_connection":
            target = node.args[0]
            assert isinstance(target, ast.Tuple)
            return target.elts[0]
    raise AssertionError("no create_connection call")


def test_the_socat_clients_hand_socat_the_address_as_one_word(tmp_path):
    """A valid address still crosses two shells intact; a bracketed IPv6 needs the quoting."""
    env = stub_env(tmp_path, ["socat"])
    values = ["router_1.lab.", "fe80::1%eth0"]
    for value in values:
        run_sh(timed_connect_command("socat", value, 5205), env)
        run_sh(timed_transfer_command("socat", value, 5205, 64), env)
    got = sorted(call[-1] for call in recorded(tmp_path, "socat"))
    want = ["TCP:router_1.lab.:5205,connect-timeout=5", "TCP:[fe80::1%eth0]:5205,connect-timeout=5"]
    assert got == sorted(want * 2)


@pytest.mark.parametrize("build", [timed_connect_command, timed_transfer_command])
def test_the_python3_clients_take_the_address_as_one_string_literal(tmp_path, build):
    marker = tmp_path / "pwned"
    values = _python_hostile(marker)
    env = stub_env(tmp_path, ["python3"])
    args = {timed_connect_command: [5205], timed_transfer_command: [5205, 64]}[build]
    for value in values:
        run_sh(build("python3", value, *args), env)
    calls = recorded(tmp_path, "python3")
    assert len(calls) == len(values)
    targets = []
    for call in calls:
        assert call[1] == "-c"
        assert len(call) == 3  # the program is one word
        target = _connect_target(call[2])
        assert isinstance(target, ast.Constant)
        _the_literal(call[2], target.value)
        targets.append(target.value)
    assert sorted(targets) == sorted(values)
    assert not marker.exists()


def test_the_python3_listener_binds_the_address_as_one_string_literal(tmp_path):
    """Pinned: the listener already wrote ``repr(bind)``; a regression would splice it raw."""
    marker = tmp_path / "pwned"
    for value in _python_hostile(marker):
        cmd = listener_command("python3", 5205, tag=TAG, bind=value)
        # setsid bash -c '<body>'; the body is python3 -c '<program>' <tag>
        body = shlex.split(cmd)[3]
        program = shlex.split(body)[2]
        _the_literal(program, value)


def test_the_socat_listener_hands_socat_its_bind_as_one_word(tmp_path):
    env = stub_env(tmp_path, ["socat"], pass_through=["setsid"])
    # The listener backgrounds itself; `wait` holds sh until the stub has recorded.
    run_sh(f"{listener_command('socat', 5205, tag=TAG, bind='fe80::1%eth0')} wait", env)
    [call] = recorded(tmp_path, "socat")
    assert call[1:] == ["TCP-LISTEN:5205,bind=[fe80::1%eth0],fork,reuseaddr", "PIPE"]


@pytest.mark.parametrize("value", SOCAT_REFUSED)
def test_the_socat_sinks_refuse_what_is_not_an_address(value):
    for build in [
        lambda: timed_connect_command("socat", value, 5205),
        lambda: timed_transfer_command("socat", value, 5205, 64),
        lambda: listener_command("socat", 5205, tag=TAG, bind=value or "x y"),
    ]:
        with pytest.raises(ValueError, match=SOCAT_REFUSAL):
            build()


def test_ping_takes_the_target_as_one_operand(tmp_path):
    marker = tmp_path / "pwned"
    values = hostile_values(marker)
    env = stub_env(tmp_path, ["ping"])
    for value in values:
        run_sh(ping_command(value, count=1, interval=0.2), env)
    got = sorted(call[-1] for call in recorded(tmp_path, "ping"))
    assert got == sorted(values)
    assert not marker.exists()
    with pytest.raises(ValueError, match="would be read as an option"):
        ping_command("-f", count=1, interval=0.2)


def test_the_listener_netns_is_one_word(tmp_path):
    marker = tmp_path / "pwned"
    env = stub_env(tmp_path, ["ip"])
    value = f"$(touch {marker})"
    run_sh(f"{listener_command('socat', 5205, tag=TAG, netns=value)} wait", env)
    [call] = recorded(tmp_path, "ip")
    assert call[:5] == ["ip", "netns", "exec", value, "setsid"]
    assert not marker.exists()
