"""The hop dial passes the address as data: one literal argument, never script text.

Each arm's command is run for real under ``sh -c`` against recording stubs
(:mod:`tests._fixtures.argv_stub`): ``timeout`` for the bash arm, which wraps
the whole ``bash -c``, and ``nc`` for the applet arm. A stub records its argv
and exits, so nothing is dialed.
"""

import pytest

from otto.host.survey.dial import HOP_DIAL_BASH, dial_via_hop, hop_dial_script
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.argv_stub import hostile_values, recorded, run_sh, stub_env


class _Userland:
    def __init__(self, dialect: str) -> None:
        self.shell_dialect = dialect
        self.timeout_style = "none"

    def has_applet(self, name: str) -> str:
        assert name == "nc"
        return "present"


def _cases(tmp_path):
    marker = tmp_path / "pwned"
    return marker, hostile_values(marker)


def test_the_bash_arm_hands_bash_the_address_as_its_first_positional_parameter(tmp_path):
    marker, values = _cases(tmp_path)
    env = stub_env(tmp_path, ["timeout"])
    for value in values:
        script = hop_dial_script(value, 23, timeout=2.0, userland=None)
        assert script is not None
        run_sh(script, env)
    calls = recorded(tmp_path, "timeout")
    assert sorted(call[6] for call in calls) == sorted(values)
    for call in calls:
        # timeout 2 bash -c <script> _ <ip> <port>: the script is the constant, untouched.
        assert call[:4] == ["timeout", "2", "bash", "-c"]
        assert call[5:] == ["_", call[6], "23"]
        assert call[4] == HOP_DIAL_BASH.format(r=1, n=256)
        assert '"$1"' in call[4]
    assert not marker.exists()


def test_the_nc_arm_hands_nc_the_address_as_one_operand(tmp_path):
    marker, values = _cases(tmp_path)
    env = stub_env(tmp_path, ["nc"])
    for value in values:
        script = hop_dial_script(value, 23, timeout=2.0, userland=_Userland("ash"))
        assert script is not None
        run_sh(script, env)
    calls = recorded(tmp_path, "nc")
    assert sorted(calls) == sorted(["nc", "-z", "-w", "2", value, "23"] for value in values)
    assert not marker.exists()


def test_the_nc_arm_refuses_an_address_nc_would_read_as_an_option():
    with pytest.raises(ValueError, match="would be read as an option"):
        hop_dial_script("-e/bin/sh", 23, timeout=2.0, userland=_Userland("ash"))


@pytest.mark.asyncio
async def test_a_refused_address_is_not_checkable_and_runs_nothing():
    ran: list[str] = []

    async def run(cmd: str) -> CommandResult:
        ran.append(cmd)
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    out = await dial_via_hop(run, "hop1", "-e/bin/sh", 23, timeout=2.0, userland=_Userland("ash"))
    assert out.state == "not-checkable"
    assert "hop hop1" in out.detail
    assert ran == []
