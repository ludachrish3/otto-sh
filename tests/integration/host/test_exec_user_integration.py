"""The headline contract of the exec-CLI-verb spec, on the live bed:
``exec(user=...)`` reaches EVERY user, and ``exec(sudo=True, user=...)``
answers the elevation prompt with that user's OWN password.

Companion to the unit pins in ``tests/unit/host/test_session_exec_pooled.py``
(routing) and ``tests/unit/host/test_unix_host.py`` (ambient identity): those
prove where the call GOES with a stub shell; only the bed proves the user it
actually lands as, and only the bed can tell a right password from a wrong
one. Spec: ``docs/superpowers/specs/2026-09-21-exec-cli-verb-design.md`` §4.1,
§9.

Two accounts, deliberately different in kind (both provisioned on every bed
VM — see ``provision_test_vm``):

- ``root``: reachable only as a PROXY login (``sudo su`` from vagrant), so a
  raw exec channel could never authenticate as it. This is the case that
  proves the promotion off the raw channel end to end — on ssh, where the
  raw channel exists, AND on telnet, where it never did.
- ``test``: a plain bash account with a real password, whose ``sudo``
  therefore challenges. Before the fix, the prompt was answered with the
  SESSION's password (vagrant's), and sudo replied ``Sorry, try again``.
  ``test`` need not be in sudoers for that to be the assertion: what is
  pinned is that the PASSWORD was accepted, not that a uid came back.

Containment mirrors ``test_proxy_user_stability_integration.py``: the
``sudo-su-shell`` proxy is registered at module scope with ``overwrite=True``
and the creds are inline, so only the VM's IP is read from ``tech1/lab.json``.

Carries no ``stability`` marker, so it rides the ordinary Unix lab lanes
(``make coverage-unix`` / ``nox -s tests_unix`` / ``tests_all``, all of which
select ``integration and not embedded and not stability``) rather than a
``--count`` soak leg.
"""

import pytest

from otto import register_login_proxy
from otto.host.factory import create_host_from_dict
from otto.utils import Status
from tests._fixtures.labdata import element_for, host_data

pytestmark = pytest.mark.timeout(60)

_NE = "test1"


async def _sudo_su_shell(io, ctx):
    # Root-mediated for the same reason the stability module gives: vagrant is
    # passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\n")


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)


_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "root", "proxy": "sudo-su-shell", "via": "vagrant"},
    {"login": "test", "password": "Password1"},
]


def _host(term: str):
    """A ``test1`` UnixHost on *term*, carrying the two extra accounts inline."""
    return create_host_from_dict(
        {
            "ip": host_data(_NE)["ip"],
            "creds": [dict(c) for c in _CREDS],
            "term": term,
        },
        element=element_for(_NE),
    )


@pytest.mark.parametrize("term", ["ssh", "telnet"])
@pytest.mark.asyncio
async def test_exec_user_lands_on_a_proxy_login(term: str) -> None:
    """``exec("whoami", user="root")`` returns ``root``, on both terms.

    ``whoami`` rather than ``id``: the answer IS the identity, so a command
    that ran as vagrant cannot pass by accident. On ssh this also pins the
    promotion off the raw exec channel — that channel authenticates as the
    direct cred and can never replay the ``sudo su`` hop, so a regression
    there comes back ``vagrant`` with a perfectly successful status.
    """
    host = _host(term)
    try:
        result = await host.exec("whoami", user="root")
        assert result.status == Status.Success, f"exec failed: {result.value!r}"
        assert result.value.strip().splitlines()[-1] == "root", result.value
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_exec_sudo_user_answers_with_that_users_own_password() -> None:
    """``exec(sudo=True, user="test")`` must not be told it typed the wrong password.

    ``test`` has a password of its own and is NOT assumed to be a sudoer, so
    the assertion is about the challenge, not the outcome: sudo may still
    refuse the account, but it must never come back ``Sorry, try again`` —
    that string is sudo saying the password it was handed (pre-fix: the
    session user's) did not belong to the user running the command.
    """
    host = _host("ssh")
    try:
        result = await host.exec("id", sudo=True, user="test")
        assert "Sorry, try again" not in result.value, (
            f"sudo was handed the wrong user's password: {result.value!r}"
        )
    finally:
        await host.close()
