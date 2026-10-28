"""A crash shows its locals and no password: the CLI and the log file, from real failures.

Traceback locals stay on in the CLI and in the logs. otto keeps a password a
``SecretStr`` in its own frames, and the libraries that receive the raw value
are shown by location only
(``otto.logger.formatters.suppressed_traceback_packages``).
Every failure here is the real library's, on loopback, never a stand-in: a
frame defined in a test sits under ``tests/``, where no suppression of the
library could hide it.

The planted password lives in a module constant, never on a source line near
a frame, since Rich prints the source around every frame it shows.
"""

import logging
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable

import pytest

from otto.config.env import is_otto_env_var
from otto.logger import install, management
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.loopback_servers import Offered, closed_port
from tests._fixtures.project_scaffold import make_project

PLANTED = "PLANTED-SECRET-7f3a"

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]

_RUN_TIMEOUT = 120
"""Seconds one ``otto`` subprocess may take: a wedged exit fails the test, never the lane."""

_MARKER = "a calling frame local"
"""A local of the frame that calls the library, which must still render."""


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


# ---------------------------------------------------------------------------
# The crash through the real entry(): printed once, with locals, exit 1
# ---------------------------------------------------------------------------

_INIT = """\
import os

from pydantic import SecretStr

from otto.cli.registry import cli_command
from otto.host.connections import ssh_connect

# Both come from the test's environment: the password is on no source line
# Rich could print around a frame, and the port is held closed by the test.
SECRET = SecretStr(os.environ["ACME_PLANTED_PASSWORD"])
CLOSED_PORT = int(os.environ["ACME_CLOSED_PORT"])
MARKER = "a calling frame local"


async def dial(marker_local):
    await ssh_connect(
        "127.0.0.1", password=SECRET, port=CLOSED_PORT, known_hosts=None, client_keys=[]
    )


@cli_command(name="dial")
async def dial_command() -> None:
    await dial(MARKER)
"""


@pytest.fixture
def workspace(tmp_path):
    lab_data = tmp_path / "lab_data"
    lab_data.mkdir()
    write_lab_json(
        lab_data / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "host1",
                "labs": ["test_lab"],
                "creds": [{"login": "admin", "password": "pass"}],
            }
        ],
    )
    project = make_project(
        tmp_path / "sut",
        name="acme",
        extra=(
            'libs = ["lib"]\ninit = ["acme_crash_init"]\n\n'
            '[[lab.sources]]\nbackend = "json"\npaths = ["../lab_data"]'
        ),
        files={"lib/acme_crash_init.py": _INIT},
    )
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env["OTTOPATH"] = str(project)
    env["OTTO_HOME"] = str(tmp_path / "home")
    env["OTTO_XDIR"] = str(tmp_path / "xdir")
    return tmp_path, env


def test_cli_crash_hides_library_locals_and_exits_1(workspace):
    """asyncssh's ``connect`` holds the password when the dial is refused; the crash hides it."""
    tmp_path, env = workspace
    with closed_port() as port:
        p = subprocess.run(
            [*_ENTRY, "--lab", "test_lab", "-R", "dial"],
            env={**env, "ACME_PLANTED_PASSWORD": PLANTED, "ACME_CLOSED_PORT": str(port)},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUN_TIMEOUT,
        )
    logs = sorted((tmp_path / "xdir").glob("*/*/verbose.log"))
    assert len(logs) == 1, f"expected one run directory\n{p.stdout}\n{p.stderr}"
    files = {name: (logs[0].parent / name).read_text() for name in ("console.log", "verbose.log")}

    assert p.returncode == 1, p.stderr
    assert p.stderr.count("Traceback") == 1, f"the crash is not printed once:\n{p.stderr}"
    assert "ConnectionRefusedError" in p.stderr, p.stderr
    assert "asyncssh" in p.stderr, f"the library's frames are not even located:\n{p.stderr}"
    assert re.search(rf"marker_local\s*=\s*'{_MARKER}'", p.stderr), (
        f"the calling frame's locals did not render:\n{p.stderr}"
    )
    assert PLANTED not in p.stdout + p.stderr, f"the crash shows the password:\n{p.stderr}"
    for name, text in files.items():
        assert "uncaught ConnectionRefusedError ended the command" in text, name
        assert PLANTED not in text, f"{name} shows the password:\n{text}"


# ---------------------------------------------------------------------------
# Each protocol's authentication failure names no password
# ---------------------------------------------------------------------------


def _manager(term: str, **options: object):
    from otto.host.connections import ConnectionManager
    from otto.models.base import Cred

    return ConnectionManager(
        ip="127.0.0.1",
        creds=[Cred(login="admin", password=PLANTED)],
        term=term,
        name="h1",
        **options,
    )


async def _ssh_auth(offered: Offered) -> None:
    from otto.host.options import SshOptions
    from tests._fixtures.loopback_servers import refusing_ssh_server

    async with refusing_ssh_server(offered) as port:
        options = SshOptions(port=port, client_keys=[], preferred_auth="password")
        await _manager("ssh", ssh_options=options).ssh()


async def _telnet_login(offered: Offered) -> None:
    from otto.host.options import TelnetOptions
    from otto.host.session import TelnetSession
    from tests._fixtures.loopback_servers import refusing_telnet_login

    async with refusing_telnet_login(offered) as port:
        options = TelnetOptions(
            port=port,
            echo_negotiation_timeout=0.1,
            extra={"connect_minwait": 0.0, "connect_maxwait": 0.1},
        )
        client = await _manager("telnet", telnet_options=options).telnet()
        session = TelnetSession(
            client.reader, client.writer, _owned_client=client, init_timeout=0.5
        )
        await session._ensure_initialized()


async def _ftp_login(offered: Offered) -> None:
    from otto.host.options import FtpOptions
    from tests._fixtures.loopback_servers import refusing_ftp_server

    async with refusing_ftp_server("admin", offered) as port:
        await _manager("ssh", ftp_options=FtpOptions(port=port)).ftp()


async def _su(offered: Offered) -> None:
    from otto.host import login_proxy
    from otto.models.base import Cred
    from tests._fixtures.fake_shell import ModelIO, ShellModel

    io = ModelIO(ShellModel(user="admin", password="the-right-one", challenges=True))
    hop = Cred(login="mysql", password=PLANTED, proxy="su", via="admin")
    try:
        await login_proxy.run_proxy(io, hop, via=Cred(login="admin"), host_id="h1")
    finally:
        offered.passwords.extend(text.rstrip("\n") for text in io.shell.writes)


_AUTH_FAILURES: dict[str, Callable[[Offered], Awaitable[None]]] = {
    "ssh_auth": _ssh_auth,
    "telnet_login": _telnet_login,
    "ftp_login": _ftp_login,
    "su": _su,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(_AUTH_FAILURES))
async def test_auth_failure_messages_carry_no_password(path, tmp_path, capsys, monkeypatch):
    """otto's own login path for each protocol, refused, renders no password anywhere.

    The password reaches the server, so the refusal is the real one, and
    neither the CLI's crash print nor the log file shows it: not in a
    frame's locals, not in a message. telnetlib3 does not authenticate;
    otto's telnet login and shell handshake do, so that is the path driven.
    """
    from otto.host import login_proxy

    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)
    offered = Offered()
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    try:
        await _drive(_AUTH_FAILURES[path], offered, _MARKER)
    except Exception as exc:
        failure = exc
        logging.getLogger("acme.product").exception("the login failed")
    else:
        raise AssertionError(f"{path}: the login was expected to be refused")
    management.reset()

    assert PLANTED in offered.passwords, f"{path}: the server was offered {offered.passwords}"
    _assert_shown_without_password(path, "verbose.log", (out / "verbose.log").read_text(), failure)
    from otto.cli.main import render_crash

    render_crash(failure)
    _assert_shown_without_password(path, "cli", capsys.readouterr().err, failure)


def _assert_shown_without_password(path: str, name: str, text: str, failure: BaseException) -> None:
    assert type(failure).__name__ in text, f"{name} never rendered the failure:\n{text}"
    assert re.search(rf"marker_local\s*=\s*'{_MARKER}'", text), (
        f"{name} lost the calling frame's locals:\n{text}"
    )
    assert PLANTED not in text, f"{path}: {name} shows the password:\n{text}"


async def _drive(
    login: Callable[[Offered], Awaitable[None]], offered: Offered, marker_local: object
) -> None:
    """The calling frame: its local must render while the library's frames are hidden."""
    await login(offered)


def test_a_crash_renders_at_the_width_otto_console_pinned(capsys, monkeypatch):
    """An in-process crash is as wide as otto's console, not a fresh 80-column probe."""
    from rich.console import ConsoleDimensions

    from otto.cli.main import render_crash
    from otto.console import CONSOLE

    monkeypatch.setattr(CONSOLE, "size", ConsoleDimensions(137, 40))
    try:
        _crash()
    except ValueError as exc:
        render_crash(exc)
    widths = {len(line) for line in capsys.readouterr().err.splitlines() if line.startswith("╭")}
    assert widths == {137}, widths


def _crash() -> None:
    raise ValueError("a crash")
