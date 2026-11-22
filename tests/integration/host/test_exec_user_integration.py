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
and the creds are inline, so only the VM's IP is read from ``tech1/lab.jsonnet``.

Carries no ``stability`` marker, so it rides the ordinary Unix lab lanes
(``make coverage-unix`` / ``nox -s tests_unix`` / ``tests_all``, all of which
select ``integration and not embedded and not stability``) rather than a
``--count`` soak leg.
"""

import pytest

from otto import register_login_proxy
from otto.host.errors import PasswordRefusedError
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


def _host(term: str, ne: str = _NE, userland_options: "dict[str, str] | None" = None):
    """A *ne* (default ``test1``) UnixHost on *term*, carrying the two extra accounts inline.

    *userland_options* is the host entry's table of that name, when given.
    """
    entry: dict = {"ip": host_data(ne)["ip"], "creds": [dict(c) for c in _CREDS], "term": term}
    if userland_options is not None:
        entry["userland_options"] = userland_options
    return create_host_from_dict(entry, element=element_for(ne))


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
        assert result.value == "root", f"{result.value!r}"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_exec_sudo_user_answers_with_that_users_own_password() -> None:
    """``exec(sudo=True, user="test")`` must not be told it typed the wrong password.

    ``test`` has a password of its own and is a sudoer on only some lab VMs
    (the Vagrantfile's ``sudo_policy``), so the assertion is about the
    challenge, not the outcome: sudo may still refuse the account where it
    is not one, but it must never come back ``Sorry, try again`` —
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


# The sudo form of `exec(sudo=True)` against the lab's password sudo, run as
# `test` (password Password1). The Vagrantfile's `sudo_policy`: on test1 `test`
# is a sudoer with sudo's default timestamp, on test3 a sudoer with
# `timestamp_timeout=0`, on test2 not a sudoer. Every password typed here is
# the right one: a wrong one counts toward the account's lockout.


@pytest.mark.parametrize("ne", ["test1", "test3"])
@pytest.mark.asyncio
async def test_an_unconfigured_host_probes_to_the_shell_form_and_elevates_the_whole_string(
    ne: str,
) -> None:
    host = _host("ssh", ne)
    try:
        both = await host.exec("id -un; id -un", sudo=True, user="test")
        bash = await host.exec("[[ a == a ]] && echo {x,y}", sudo=True, user="test")
        assert bash.value.splitlines()[-1].endswith("x y"), f"{bash.value!r}"
        # Settled as a measurement, not the unread default.
        assert host._userland().as_lab_json(sudo_login="test").get("sudo_form") == "shell"
        assert both.status == Status.Success, f"{both.value!r}"
        assert both.value.splitlines()[-2:] == ["root", "root"], f"{both.value!r}"
        reader = await host.exec("cat; echo otto-sudo-eof", sudo=True, user="test")
        assert reader.status == Status.Success, f"{reader.value!r}"
        # The reader read end of file: it returned at once, its marker last.
        assert reader.value.splitlines()[-1] == "otto-sudo-eof", f"{reader.value!r}"
    finally:
        await host.close()


@pytest.mark.parametrize(("ne", "ok"), [("test1", True), ("test3", False)])
@pytest.mark.asyncio
async def test_the_validate_form_needs_a_sudo_that_keeps_its_timestamp(ne: str, ok: bool) -> None:
    """Configured ``validate``: runs on test1; on test3 sudo refuses the ``sudo -n`` step."""
    host = _host("ssh", ne, userland_options={"sudo_form": "validate"})
    try:
        result = await host.exec("id -un", sudo=True, user="test")
        if ok:
            assert result.status == Status.Success, f"{result.value!r}"
            assert result.value.splitlines()[-1] == "root", f"{result.value!r}"
        else:
            assert result.retcode == 1, f"{result!r}"
            assert result.value.endswith("sudo: a password is required"), f"{result.value!r}"
        after = await host.exec("echo alive", user="test")
        assert after.value == "alive", f"{after.value!r}"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_a_login_that_is_not_a_sudoer_is_refused_once_with_nothing_pending() -> None:
    """test2: ``test`` is not a sudoer, so the probe is refused and ``sudo -n -v`` holds nothing.

    That is unread, never a measured form: the exec fails as a refused
    password naming ``userland_options.sudo_form``, with no second password.
    """
    host = _host("ssh", "test2")
    try:
        with pytest.raises(PasswordRefusedError, match=r"userland_options\.sudo_form") as refused:
            await host.exec("id -un", sudo=True, user="test")
        assert "Sorry, try again" not in str(refused.value)
        assert "sudo_form" not in host._userland().as_lab_json(sudo_login="test")
        after = await host.exec("echo alive", user="test")
        assert after.value == "alive", f"{after.value!r}"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_one_login_s_sudo_answer_is_not_another_s() -> None:
    """test2: ``test``'s refusal leaves ``vagrant`` (a NOPASSWD sudoer) to be asked as itself.

    ``vagrant`` is asked after ``test`` was refused, measures the ``shell``
    form, and its compound command runs whole as root.
    """
    host = _host("ssh", "test2")
    try:
        with pytest.raises(PasswordRefusedError):
            await host.exec("id -un", sudo=True, user="test")
        both = await host.exec("id -un; id -un", sudo=True)
        userland = host._userland()
        assert userland.as_lab_json(sudo_login="vagrant").get("sudo_form") == "shell"
        assert "sudo_form" not in userland.as_lab_json(sudo_login="test")
        assert both.value.splitlines()[-2:] == ["root", "root"], f"{both.value!r}"
    finally:
        await host.close()
