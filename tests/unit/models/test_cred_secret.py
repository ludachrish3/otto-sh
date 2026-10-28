"""A password is a ``SecretStr`` from the lab file to the host, and no repr shows one (#487)."""

import dataclasses

import pytest
from pydantic import SecretStr
from rich.console import Console

from otto.host import UnixHost
from otto.host.connections import LOGINLESS
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.logger.mode import LogMode
from otto.models.base import CredSpec

PLANTED = "PLANTED-SECRET-7f3a"


def _rich_render(obj: object) -> str:
    console = Console(record=True, width=200)
    console.print(obj)
    return console.export_text()


@pytest.mark.parametrize("pw", [PLANTED, ""])
def test_cred_built_from_a_plain_string_is_masked(pw):
    c = Cred(login="root", password=pw)  # plain str, as callers build it today
    assert isinstance(c.password, SecretStr)
    assert c.password.get_secret_value() == pw
    assert pw == "" or pw not in repr(c)


def test_cred_stays_frozen_and_equal_by_value():
    cred = Cred(login="root", password=PLANTED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cred.password = SecretStr("other")  # ty: ignore[invalid-assignment]
    assert cred == Cred(login="root", password=SecretStr(PLANTED))
    assert Cred(login="root", password="A") != Cred(login="root", password="B")
    assert Cred(login="root").password is None


def test_loginless_is_an_empty_secret():
    assert isinstance(LOGINLESS.password, SecretStr)
    assert LOGINLESS.password.get_secret_value() == ""


def test_credspec_repr_and_rich_never_show_password():
    s = CredSpec(login="root", password=PLANTED)
    assert isinstance(s.password, SecretStr)
    assert PLANTED not in repr(s)
    assert PLANTED not in _rich_render(s)
    assert PLANTED not in repr(s.to_cred())


def test_credspec_dump_never_shows_password():
    s = CredSpec(login="root", password=PLANTED)
    assert PLANTED not in str(s.model_dump())
    assert PLANTED not in s.model_dump_json()
    assert s.model_dump(mode="json")["password"] == "**********"
    kept = s.model_dump()["password"]  # python mode keeps the SecretStr, for the merges
    assert isinstance(kept, SecretStr)
    assert kept.get_secret_value() == PLANTED


def test_host_repr_never_shows_password():
    host = UnixHost(
        ip="10.0.0.1",
        element=Element("box"),
        creds=[Cred(login="user", password=PLANTED)],
        log=LogMode.QUIET,
    )
    assert isinstance(host.creds[0].password, SecretStr)
    assert PLANTED not in repr(host)
    assert PLANTED not in _rich_render(host)
