"""The sudo form an ``exec(sudo=True)`` types: declared, or asked once with ``sudo -l "$SHELL"``.

:meth:`otto.host.userland.Userland.sudo_form` is declared per host or measured per login;
:meth:`~otto.host.privilege.PosixPrivilege._prepare_exec_elevation` settles it
on the first ``exec`` under sudo. The real-shell tests type the probe and the
exec into a real local shell with the stand-in ``sudo``
(:mod:`tests._fixtures.fake_elevation`): ``_exec_one`` is routed onto that
shell, so the lines, the password answer and the recovery are the session's.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from otto.host.command_frame import ExecInput
from otto.host.errors import PasswordRefusedError
from otto.host.login_proxy import Cred
from otto.host.option_specs import UserlandOptionsSpec
from otto.host.options import UserlandOptions
from otto.host.userland import SUDO_FORM_SHELL, SUDO_FORM_VALIDATE, Userland
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.fake_elevation import install_fake_elevation
from tests.unit.host._pty_session import pty_session
from tests.unit.host.test_privilege import (
    _answers_no,
    _host_with_userland,
    _sudo_parts,
)

_ADMIN = [Cred(login="admin", password="secret")]

_PROBE_TAIL = (
    '-l "${SHELL:-/bin/sh}" || if sudo -n -v </dev/null 2>/dev/null; '
    "then (exit 100); else (exit 101); fi"
)
"""The probe's argv tail and its same-frame timestamp check, as typed."""

_REFUSED_HELD, _REFUSED_UNHELD = 100, 101
"""The probe's statuses for a refused shell with, and without, a timestamp held."""


def _userland(sudo_form: str | None = None) -> Userland:
    return Userland(UserlandOptions(elevation="sudo", sudo_form=sudo_form), _answers_no)


def _answer(value: "bool | str | BaseException"):
    calls: list[int] = []

    async def probe() -> bool | None:
        calls.append(1)
        if isinstance(value, BaseException):
            raise value
        return value

    return probe, calls


# ---------------------------------------------------------------------------
# The value: declared wins, a measurement is kept, an unread probe is not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_form_is_never_asked():
    u = _userland(SUDO_FORM_VALIDATE)
    probe, calls = _answer(True)
    await u.resolve_sudo_form(login="admin", probe=probe)
    assert (u.sudo_form(login="admin"), calls, u.as_lab_json(sudo_login="admin")["sudo_form"]) == (
        SUDO_FORM_VALIDATE,
        [],
        "validate",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "form"), [(True, SUDO_FORM_SHELL), (False, SUDO_FORM_VALIDATE)])
async def test_an_answer_settles_the_form_once(answer, form):
    u = _userland()
    probe, calls = _answer(answer)
    await u.resolve_sudo_form(login="admin", probe=probe)
    await u.resolve_sudo_form(login="admin", probe=probe)
    assert (
        u.sudo_form(login="admin"),
        len(calls),
        u.as_lab_json(sudo_login="admin").get("sudo_form"),
    ) == (form, 1, form)


@pytest.mark.asyncio
async def test_an_unread_probe_answers_the_default_and_is_asked_again():
    u = _userland()
    probe, calls = _answer("sudo did not answer in time")
    await u.resolve_sudo_form(login="admin", probe=probe)
    assert (u.sudo_form(login="admin"), u.sudo_form_unread(login="admin")) == (
        SUDO_FORM_SHELL,
        "sudo did not answer in time",
    )
    assert "sudo_form" not in u.as_lab_json(sudo_login="admin")
    await u.resolve_sudo_form(login="admin", probe=probe)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_refused_probe_propagates_and_settles_nothing():
    u = _userland()
    probe, _ = _answer(PasswordRefusedError("refused"))
    with pytest.raises(PasswordRefusedError):
        await u.resolve_sudo_form(login="admin", probe=probe)
    assert not u.sudo_form_known(login="admin")
    assert u.sudo_form_unread(login="admin") == "the password was refused"
    with pytest.raises(RuntimeError, match="resolve_sudo_form"):
        _ = u.sudo_form(login="admin")


@pytest.mark.asyncio
async def test_concurrent_first_askers_ask_once():
    u = _userland()
    calls: list[int] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe() -> bool:
        calls.append(1)
        entered.set()
        await release.wait()
        return True

    askers = [
        asyncio.create_task(u.resolve_sudo_form(login="admin", probe=probe)) for _ in range(5)
    ]
    await entered.wait()
    release.set()
    await asyncio.gather(*askers)
    assert (len(calls), u.sudo_form(login="admin")) == (1, SUDO_FORM_SHELL)


@pytest.mark.parametrize("value", ["shell", "validate", None])
def test_the_spec_takes_the_two_forms(value):
    assert UserlandOptionsSpec(sudo_form=value).to_runtime().sudo_form == value


def test_the_spec_names_a_misspelled_form():
    with pytest.raises(ValidationError, match="sudo_form"):
        UserlandOptionsSpec(sudo_form="shel")


# ---------------------------------------------------------------------------
# The probe, through the host.
# ---------------------------------------------------------------------------


def _record_exec_one(host, results: "list[CommandResult | BaseException]"):
    seen: list[dict] = []
    # A copy: the list is a parametrize value, shared by every run of the test.
    results = list(results)

    async def _exec_one(cmd, timeout, log=LogMode.NORMAL, user=None, **kw):
        seen.append({"cmd": cmd, "log": log, "timeout": timeout, **kw})
        r = (
            results.pop(0)
            if results
            else CommandResult(status=Status.Success, value="", command=cmd, retcode=0)
        )
        if isinstance(r, BaseException):
            raise r
        return r

    host._exec_one = _exec_one
    return seen


def _ok(retcode: int = 0, *, timed_out: bool = False) -> CommandResult:
    status = Status.Success if retcode == 0 else Status.Failed
    return CommandResult(status=status, value="", command="", retcode=retcode, timed_out=timed_out)


@pytest.mark.asyncio
async def test_the_first_exec_asks_once_then_types_the_form_it_settled():
    host = await _host_with_userland("sudo", _ADMIN)
    seen = _record_exec_one(host, [_ok(0)])
    await host.exec("id -un; id -un", sudo=True)
    await host.exec("id -un", sudo=True)
    probe, first, second = seen
    assert _exec_prompt_line(probe["cmd"]) == _PROBE_TAIL
    assert probe["log"] is LogMode.NEVER
    assert probe["exec_input"] == ExecInput(stdin_eof=True, credential=True)
    shell = 'sudo -S "${SHELL:-/bin/sh}" -c'
    assert first["cmd"].endswith(f"{shell} 'exec </dev/null; id -un; id -un'")
    assert second["cmd"].endswith(f"{shell} 'exec </dev/null; id -un'")


def _exec_prompt_line(cmd: str) -> str:
    """The argv tail of an ``<&3 3<&- SUDO_PROMPT=... sudo -S <tail>`` line."""
    prefix = "<&3 3<&- "
    assert cmd.startswith(prefix), cmd
    return _sudo_parts(cmd[len(prefix) :])[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "form", "settled"),
    [
        ([_ok(0)], SUDO_FORM_SHELL, True),
        ([_ok(_REFUSED_HELD)], SUDO_FORM_VALIDATE, True),
        ([_ok(0, timed_out=True)], SUDO_FORM_SHELL, False),
        ([OSError("channel refused")], SUDO_FORM_SHELL, False),
    ],
)
async def test_what_the_probe_s_answer_settles(results, form, settled):
    """With a password: 0 is shell; a refusal while sudo holds a timestamp is validate."""
    host = await _host_with_userland("sudo", _ADMIN)
    _record_exec_one(host, results)
    await host._prepare_exec_elevation()
    userland = host._userland()
    assert (
        userland.sudo_form(login="admin"),
        "sudo_form" in userland.as_lab_json(sudo_login="admin"),
    ) == (form, settled)


@pytest.mark.asyncio
@pytest.mark.parametrize(("retcode", "form", "settled"), [(0, "shell", True), (1, "shell", False)])
async def test_without_a_password_only_a_yes_is_a_measurement(retcode, form, settled):
    """No password: a refusal may be sudo wanting the password otto lacks, so it is unread."""
    host = await _host_with_userland("sudo", [Cred(login="admin")])
    _record_exec_one(host, [_ok(retcode)])
    await host._prepare_exec_elevation()
    userland = host._userland()
    assert (
        userland.sudo_form(login="admin"),
        "sudo_form" in userland.as_lab_json(sudo_login="admin"),
    ) == (form, settled)
    assert (userland.sudo_form_unread(login="admin") is None) is settled


@pytest.mark.asyncio
async def test_a_passwordless_probe_reads_end_of_file_and_opens_no_descriptor():
    host = await _host_with_userland("sudo", [Cred(login="admin")])
    seen = _record_exec_one(host, [_ok(0)])
    await host._prepare_exec_elevation()
    assert _sudo_parts(seen[0]["cmd"])[1] == _PROBE_TAIL
    assert seen[0]["exec_input"] == ExecInput(stdin_eof=True, credential=False)
    assert seen[0]["expects"] == []


@pytest.mark.asyncio
async def test_a_root_session_asks_nothing_and_types_the_shell_form():
    host = await _host_with_userland("sudo", [Cred(login="root", password="rootpw")])
    seen = _record_exec_one(host, [])
    await host.exec("id", sudo=True)
    (only,) = seen
    assert "\"${SHELL:-/bin/sh}\" -c 'exec </dev/null; id'" in only["cmd"]


@pytest.mark.asyncio
@pytest.mark.parametrize("elevation", ["su"])
async def test_a_host_that_elevates_with_su_asks_nothing(elevation):
    host = await _host_with_userland(
        elevation, [Cred(login="admin"), Cred(login="root", password="x")]
    )
    seen = _record_exec_one(host, [])
    await host.exec("id", sudo=True)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_dry_run_asks_nothing(monkeypatch):
    from otto.host import privilege

    monkeypatch.setattr(privilege, "is_dry_run", lambda: True)
    host = await _host_with_userland("sudo", _ADMIN)
    seen = _record_exec_one(host, [])
    await host._prepare_exec_elevation()
    assert (seen, host._userland().sudo_form(login="admin")) == ([], SUDO_FORM_SHELL)
    assert "sudo_form" not in host._userland().as_lab_json(sudo_login="admin")


# ---------------------------------------------------------------------------
# The probe and the exec on a real shell with the stand-in sudo.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _on_a_real_shell(host, env):
    """Route *host*'s ``_exec_one`` onto one real local bash with *env*."""
    async with pty_session("bash", env=env) as s:

        async def _exec_one(cmd, timeout, log=LogMode.NORMAL, user=None, **kw):
            return await s.run_cmd(
                cmd=cmd, timeout=timeout, expects=kw.get("expects"), exec_input=kw.get("exec_input")
            )

        host._exec_one = _exec_one
        yield s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refuses_shell", "form", "second"),
    [(False, SUDO_FORM_SHELL, "root"), (True, SUDO_FORM_VALIDATE, None)],
)
async def test_an_unconfigured_host_probes_its_sudo_and_the_exec_follows(
    refuses_shell, form, second, tmp_path
):
    import getpass

    env = install_fake_elevation(
        tmp_path / "e", password="secret", keeps_timestamp=True, refuses_shell=refuses_shell
    )
    host = await _host_with_userland("sudo", _ADMIN)
    async with _on_a_real_shell(host, env) as s:
        r = await host.exec("id -un; id -un", sudo=True)
        reader = await host.exec("cat; echo otto-cf-eof", sudo=True)
        assert s.alive
    assert host._userland().sudo_form(login="admin") == form
    assert (r.retcode, r.value.splitlines()[-2:]) == (0, ["root", second or getpass.getuser()]), r
    # The stdin reader read nothing: the only output is its own marker.
    assert (reader.retcode, reader.value.splitlines()) == (0, ["otto-cf-eof"]), reader


@pytest.mark.asyncio
async def test_a_wrong_password_is_typed_once_and_leaves_nothing_pending(tmp_path):
    env = install_fake_elevation(tmp_path / "e", password="secret", keeps_timestamp=True)
    host = await _host_with_userland("sudo", [Cred(login="admin", password="wrong")])
    async with _on_a_real_shell(host, env) as s:
        with pytest.raises(PasswordRefusedError):
            await host.exec("true", sudo=True)
        after = await s.exec_("echo alive", timeout=5)
    assert not host._userland().sudo_form_known(login="admin")
    assert (after.timed_out, after.value) == (False, "alive")
    # The probe's one answer, and no second attempt by the exec in that call.
    assert _reads(tmp_path) == 1


@pytest.mark.asyncio
async def test_a_validate_host_on_a_sudo_without_timestamp_is_refused_as_declared(tmp_path):
    env = install_fake_elevation(tmp_path / "e", password="secret", keeps_timestamp=False)
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_VALIDATE)
    async with _on_a_real_shell(host, env) as s:
        r = await host.exec("id -un", sudo=True)
        assert s.alive
    assert (r.retcode, r.value.endswith("sudo: a password is required")) == (1, True), r


def _reads(tmp_path) -> int:
    """How many password lines the stand-in sudo read."""
    reads = tmp_path / "e" / "sudo" / "reads"
    return len(reads.read_text().splitlines()) if reads.exists() else 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("keeps_timestamp", "answers"), [(True, 1), (False, 2)])
async def test_the_first_exec_answers_at_most_once_for_the_probe_and_once_for_the_exec(
    keeps_timestamp, answers, tmp_path
):
    env = install_fake_elevation(tmp_path / "e", password="secret", keeps_timestamp=keeps_timestamp)
    host = await _host_with_userland("sudo", _ADMIN)
    async with _on_a_real_shell(host, env):
        r = await host.exec("id -un", sudo=True)
    assert (r.retcode, r.value.splitlines()[-1].endswith("root")) == (0, True), r
    assert _reads(tmp_path) == answers


@pytest.mark.asyncio
async def test_the_stand_in_refusing_the_session_s_shell_resolves_to_validate(tmp_path):
    env = install_fake_elevation(
        tmp_path / "e", password="secret", keeps_timestamp=True, refuses_shell=True
    )
    host = await _host_with_userland("sudo", _ADMIN)
    async with _on_a_real_shell(host, env | {"SHELL": "/bin/bash"}):
        r = await host.exec("id -un", sudo=True)
    assert host._userland().as_lab_json(sudo_login="admin")["sudo_form"] == SUDO_FORM_VALIDATE
    assert (r.retcode, r.value.splitlines()[-1]) == (0, "root"), r


@pytest.mark.asyncio
async def test_a_passwordless_host_whose_sudo_wants_one_is_unread_not_validate(tmp_path):
    env = install_fake_elevation(tmp_path / "e", password="secret", keeps_timestamp=True)
    host = await _host_with_userland("sudo", [Cred(login="admin")])
    async with _on_a_real_shell(host, env) as s:
        await host._prepare_exec_elevation()
        assert s.alive
    userland = host._userland()
    assert (
        userland.sudo_form(login="admin"),
        "sudo_form" in userland.as_lab_json(sudo_login="admin"),
    ) == (SUDO_FORM_SHELL, False)
    assert "no password" in (userland.sudo_form_unread(login="admin") or "")
    assert _reads(tmp_path) == 0


@pytest.mark.asyncio
async def test_a_nopasswd_probe_types_nothing_and_measures_shell(tmp_path):
    env = install_fake_elevation(
        tmp_path / "e", password="secret", keeps_timestamp=False, nopasswd=True
    )
    host = await _host_with_userland("sudo", _ADMIN)
    async with _on_a_real_shell(host, env) as s:
        r = await host.exec("cat; echo otto-cf-eof", sudo=True)
        after = await s.exec_("echo alive", timeout=5)
    assert host._userland().as_lab_json(sudo_login="admin")["sudo_form"] == SUDO_FORM_SHELL
    assert (r.retcode, r.value) == (0, "otto-cf-eof"), r
    assert (_reads(tmp_path), after.value) == (0, "alive")


# ---------------------------------------------------------------------------
# A refusal after the password: settled by ``sudo -n -v``, never a second answer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_with_no_timestamp_fails_the_exec_and_sends_nothing_more():
    host = await _host_with_userland("sudo", _ADMIN)
    seen = _record_exec_one(host, [_ok(_REFUSED_UNHELD)])
    with pytest.raises(
        PasswordRefusedError,
        match=r"(?s)sudo refused .*may not use sudo.*password is wrong.*sudo_form",
    ):
        await host.exec("id -un", sudo=True)
    (probe,) = seen  # one typed command: the check rides in the probe's own frame
    assert "|| if sudo -n -v </dev/null 2>/dev/null; then (exit 100)" in probe["cmd"]
    assert not host._userland().sudo_form_known(login="admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keeps_timestamp", "refuses_shell", "password", "form"),
    [
        (True, True, "secret", SUDO_FORM_VALIDATE),
        (False, True, "secret", None),
        (True, False, "wrong", None),
    ],
    ids=["denied-with-timestamp", "denied-without-timestamp", "single-attempt-wrong-password"],
)
async def test_a_single_attempt_sudo_gets_exactly_one_answer_in_the_call(
    keeps_timestamp, refuses_shell, password, form, tmp_path
):
    """A sudo that prompts once exits 1 on a wrong password and on "not allowed" alike."""
    env = install_fake_elevation(
        tmp_path / "e",
        password="secret",
        keeps_timestamp=keeps_timestamp,
        refuses_shell=refuses_shell,
        passwd_tries=1,
    )
    host = await _host_with_userland("sudo", [Cred(login="admin", password=password)])
    async with _on_a_real_shell(host, env | {"SHELL": "/bin/bash"}) as s:
        if form is None:
            with pytest.raises(PasswordRefusedError, match=r"userland_options\.sudo_form"):
                await host.exec("id -un", sudo=True)
        else:
            r = await host.exec("id -un", sudo=True)
            assert (r.retcode, r.value.splitlines()[-1]) == (0, "root"), r
        after = await s.exec_("echo alive", timeout=5)
    assert after.value == "alive"
    assert _reads(tmp_path) == 1
    userland = host._userland()
    assert userland.as_lab_json(sudo_login="admin").get("sudo_form") == form


# ---------------------------------------------------------------------------
# The measured form belongs to the login sudo was asked as.
# ---------------------------------------------------------------------------

_TWO_LOGINS = [Cred(login="admin", password="a-pw"), Cred(login="guest", password="g-pw")]


def _per_login_sudo(host, allows_shell: "dict[str, bool]"):
    """``_exec_one`` as a sudoers that answers per login: ``-l <shell>`` by *allows_shell*."""
    seen: list[tuple[str, str]] = []

    async def _exec_one(cmd, timeout, log=LogMode.NORMAL, user=None, **kw):
        login = host._acting_user(user)
        seen.append((login, cmd))
        await asyncio.sleep(0)
        probe = '-l "${SHELL:-/bin/sh}"' in cmd
        retcode = _REFUSED_HELD if probe and not allows_shell[login] else 0
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=retcode)

    host._exec_one = _exec_one
    return seen


def _probes(seen, login):
    return [c for who, c in seen if who == login and '-l "${SHELL:-/bin/sh}"' in c]


@pytest.mark.asyncio
async def test_one_login_s_answer_is_never_applied_to_another():
    host = await _host_with_userland("sudo", _TWO_LOGINS)
    seen = _per_login_sudo(host, {"admin": True, "guest": False})
    await host.exec("a; b", sudo=True, user="guest")
    await host.exec("a; b", sudo=True, user="admin")
    userland = host._userland()
    assert userland.sudo_form(login="guest") == SUDO_FORM_VALIDATE
    assert userland.sudo_form(login="admin") == SUDO_FORM_SHELL
    assert (len(_probes(seen, "guest")), len(_probes(seen, "admin"))) == (1, 1)
    admin_exec = [c for who, c in seen if who == "admin"][-1]
    assert admin_exec.endswith("""-c 'exec </dev/null; a; b'"""), admin_exec


@pytest.mark.asyncio
async def test_after_as_user_the_switched_login_is_the_one_asked():
    from types import SimpleNamespace

    host = await _host_with_userland("sudo", _TWO_LOGINS)
    seen = _per_login_sudo(host, {"admin": True, "guest": False})
    await host.exec("id", sudo=True)
    mgr = host._live_session_mgr()
    mgr._session = SimpleNamespace(current_user="guest")
    await host.exec("id", sudo=True)
    userland = host._userland()
    assert (userland.sudo_form(login="admin"), userland.sudo_form(login="guest")) == (
        SUDO_FORM_SHELL,
        SUDO_FORM_VALIDATE,
    )
    assert (len(_probes(seen, "admin")), len(_probes(seen, "guest"))) == (1, 1)


@pytest.mark.asyncio
async def test_concurrent_first_execs_ask_once_per_login():
    host = await _host_with_userland("sudo", _TWO_LOGINS)
    seen = _per_login_sudo(host, {"admin": True, "guest": False})
    await asyncio.gather(
        *(host.exec("id", sudo=True, user=u) for u in ("admin", "guest", "admin", "guest"))
    )
    assert (len(_probes(seen, "admin")), len(_probes(seen, "guest"))) == (1, 1)


@pytest.mark.asyncio
async def test_a_declared_form_holds_for_every_login():
    host = await _host_with_userland("sudo", _TWO_LOGINS, SUDO_FORM_VALIDATE)
    seen = _per_login_sudo(host, {"admin": True, "guest": True})
    await host.exec("id", sudo=True, user="guest")
    await host.exec("id", sudo=True, user="admin")
    assert _probes(seen, "admin") == _probes(seen, "guest") == []


@asynccontextmanager
async def _on_two_pooled_shells(host, env):
    """Route *host*'s ``_exec_one`` onto two real shells in turn, as a pool may hand them out."""
    async with pty_session("bash", env=env) as a, pty_session("bash", env=env) as b:
        turn = [a, b]

        async def _exec_one(cmd, timeout, log=LogMode.NORMAL, user=None, **kw):
            s = turn[0]
            turn.reverse()
            return await s.run_cmd(
                cmd=cmd, timeout=timeout, expects=kw.get("expects"), exec_input=kw.get("exec_input")
            )

        host._exec_one = _exec_one
        yield a, b


@pytest.mark.asyncio
async def test_the_probe_reads_the_timestamp_on_the_terminal_it_authenticated_on(tmp_path):
    """sudo keeps its timestamp per terminal: the refusal is settled in the probe's own frame."""
    env = install_fake_elevation(
        tmp_path / "e", password="secret", keeps_timestamp=True, refuses_shell=True, per_tty=True
    )
    host = await _host_with_userland("sudo", _ADMIN)
    async with _on_two_pooled_shells(host, env | {"SHELL": "/bin/bash"}):
        r = await host.exec("id -un", sudo=True)
    assert host._userland().as_lab_json(sudo_login="admin")["sudo_form"] == SUDO_FORM_VALIDATE
    assert (r.retcode, r.value.splitlines()[-1].endswith("root")) == (0, True), r
    # One answer for the probe, one for the exec on the other terminal.
    assert _reads(tmp_path) == 2
