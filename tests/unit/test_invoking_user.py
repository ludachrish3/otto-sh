"""The invoking user's name: one answer for every site otto asks it of.

A uid with no passwd entry (``docker run -u 12345:12345``) and no login
name in the environment has no name: the local host still builds and runs,
and every site says what it uses in its place.
"""

import os
import pwd
from pathlib import Path

import pytest

from otto.utils import process_user

_LOGIN_ENV = ("LOGNAME", "USER", "LNAME", "USERNAME")


@pytest.fixture
def nameless_uid(monkeypatch):
    """The process's uid has no passwd entry and no login name is in the environment."""
    for name in _LOGIN_ENV:
        monkeypatch.delenv(name, raising=False)

    def no_entry(uid: int) -> pwd.struct_passwd:
        raise KeyError(f"getpwuid(): uid not found: {uid}")

    monkeypatch.setattr(pwd, "getpwuid", no_entry)


@pytest.mark.asyncio
async def test_the_local_host_builds_and_runs_for_a_uid_with_no_passwd_entry(nameless_uid):
    from otto.host.local_host import LocalHost

    assert process_user() is None
    host = LocalHost()
    try:
        result = await host.exec("true")
        assert result.retcode == 0
        assert host.configured_login_user() == ""
    finally:
        await host.close()


def test_labels_follow_the_environment_and_the_local_login_follows_the_euid(monkeypatch):
    from otto.coverage.get import _resolve_tester
    from otto.docker.compose import get_user_compose_project
    from otto.host.local_host import LocalHost
    from otto.reservations.identity import resolve_username

    monkeypatch.setenv("LOGNAME", "otto-named")
    monkeypatch.setenv("USER", "otto-named")
    monkeypatch.delenv("OTTO_COMPOSE_SUFFIX", raising=False)
    euid_name = pwd.getpwuid(os.geteuid()).pw_name
    assert euid_name != "otto-named"
    assert process_user() == euid_name
    assert LocalHost().configured_login_user() == euid_name
    assert resolve_username(None).username == "otto-named"
    assert get_user_compose_project(project_name="p") == "otto-p-otto-named"
    assert _resolve_tester(None, "e@x", Path("/nonexistent"))["name"] == "otto-named"


def test_a_nameless_uid_has_one_stated_stand_in_on_the_label_sites(nameless_uid, monkeypatch):
    from otto.docker.compose import get_user_compose_project
    from otto.reservations.identity import resolve_username

    monkeypatch.delenv("OTTO_COMPOSE_SUFFIX", raising=False)
    assert get_user_compose_project(project_name="p") == "otto-p-anon"
    assert resolve_username(None).username == "anon"
