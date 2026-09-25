"""``otto host test2 logout`` and ``login --force`` against a console someone left logged in.

The ``otto`` CLI runs as a subprocess on a fresh pty
(:class:`~tests.e2e.host._pty_driver.InteractiveOttoSession`), so the verbs,
the bounded reset and the interactive console bridge all run for real. Each
test first leaves a shell logged in on the line over raw telnet and drops
the connection, which is exactly what a person who closed their terminal
leaves behind. The ``test2_console`` fixture proves the line is back at
``login:`` afterwards.
"""

import asyncio
import re
from pathlib import Path

import pytest

from otto.host.login_proxy import Cred
from tests._fixtures.labdata import host_data, host_data_console
from tests._fixtures.paths import PROJECT_ROOT
from tests.e2e.console._raw import login_and_abandon
from tests.e2e.host._pty_driver import InteractiveOttoSession

REPO1_DIR = PROJECT_ROOT / "tests" / "repo1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(180),
    pytest.mark.xdist_group("console_e2e"),
    pytest.mark.usefixtures("test2_console"),
]

_OPTS = host_data_console("test2")
_CREDS = [Cred(**c) for c in host_data("test2")["creds"]]
_CONSOLE_LOGIN = _CREDS[0]
"""The cred ``otto host test2 login`` logs in as: the first that applies to the term."""
_OTHER = next(c for c in _CREDS if c.login != _CONSOLE_LOGIN.login)
"""A second account, for the shell someone else left logged in."""


def _argv(*verb: str) -> list[str]:
    # ``--term`` is an option on the ``otto host`` callback: before the host id.
    return ["-l", "unix", "host", "--term", "console", "test2", *verb]


def _abandon_a_shell() -> None:
    asyncio.run(login_and_abandon(_OPTS.server, _OPTS.port, _OTHER.login, _OTHER.password))


def test_logout_resets_an_abandoned_shell(tmp_path: Path):
    _abandon_a_shell()
    with InteractiveOttoSession(
        _argv("logout"), xdir=tmp_path / "xdir", sut_dirs=REPO1_DIR
    ) as sess:
        sess.expect(b"reset: login prompt restored", timeout=60)
        assert sess.wait(timeout=20) == 0


def test_logout_on_a_line_already_at_login_says_so(tmp_path: Path):
    with InteractiveOttoSession(
        _argv("logout"), xdir=tmp_path / "xdir", sut_dirs=REPO1_DIR
    ) as sess:
        sess.expect(b"already at login prompt", timeout=60)
        assert sess.wait(timeout=20) == 0


def test_login_force_resets_then_bridges(tmp_path: Path):
    _abandon_a_shell()
    argv = _argv("login", "--force")
    with InteractiveOttoSession(argv, xdir=tmp_path / "xdir", sut_dirs=REPO1_DIR) as sess:
        sess.expect(b"reset: login prompt restored", timeout=60)
        sess.expect(b"(console). Press Ctrl+] to disconnect", timeout=60)
        sess.drain(0.5)
        sess.sendline("id -un")
        whoami = re.compile(rb"[\r\n]" + re.escape(_CONSOLE_LOGIN.login.encode()) + rb"\r?\n")
        sess.expect(whoami, timeout=20)
        sess.disconnect()
        sess.expect(b"disconnected from", timeout=30)
        assert sess.wait(timeout=30) == 0


def test_login_without_force_fails_loud_on_an_abandoned_shell(tmp_path: Path):
    _abandon_a_shell()
    with InteractiveOttoSession(_argv("login"), xdir=tmp_path / "xdir", sut_dirs=REPO1_DIR) as sess:
        sess.expect(b"already logged in", timeout=60)
        assert sess.wait(timeout=20) != 0
    # The abandoned shell is still there: the test2_console fixture fails
    # this test unless it is reset, so reset it the way the error says to.
    with InteractiveOttoSession(
        _argv("logout"), xdir=tmp_path / "xdir2", sut_dirs=REPO1_DIR
    ) as sess:
        sess.expect(b"reset: login prompt restored", timeout=60)
        assert sess.wait(timeout=20) == 0
