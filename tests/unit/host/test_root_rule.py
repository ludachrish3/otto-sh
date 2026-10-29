"""Every site that asks "does this session already act as root" asks ``is_root``.

Each of them decides whether to sudo, or whether a login is privileged, from
a login name. A site that compared against ``"root"`` itself would keep
working until the rule changed, then silently disagree with the elevation
seam. :func:`~otto.host.identity.is_root` reads ``ROOT_LOGIN`` when called,
so swapping that one name for a login no real host has reaches every
is-root DECISION, however it imported the predicate, and each must follow
it. It does not reach the sites that name root as a VALUE (an ``as_user``
default, the identity a switch to root records): those bound
``ROOT_LOGIN`` at import, and the swap is not about them.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.check.fingerprint import _elevated, parse_fingerprint
from otto.host import identity
from otto.host.element import Element
from otto.host.kmod_tool_kind import elevates
from otto.host.unix_host import UnixHost
from otto.link.check import _privilege
from otto.link.manage import _root_run
from otto.logger.mode import LogMode
from otto.models.base import Cred
from otto.result import CommandResult, Result, Results
from otto.utils import Status

_RULED_ROOT = "superuser"


@pytest.fixture(autouse=True)
def _swapped_root_rule(monkeypatch):
    monkeypatch.setattr(identity, "ROOT_LOGIN", _RULED_ROOT)


class _Host:
    id = "h"

    def __init__(self, user: str) -> None:
        self.current_user = user
        self.sudo: list[bool] = []

    def configured_login_user(self) -> str:
        return self.current_user

    async def run(self, cmd, *, sudo, **_kw):
        self.sudo.append(sudo)
        return [SimpleNamespace(timed_out=False)]


def _unix_host(user: str) -> UnixHost:
    host = UnixHost(
        ip="10.0.0.1", element=Element("box"), creds=[Cred(login=user)], log=LogMode.QUIET
    )
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = user
    ok = Results.collect([CommandResult(command="", value="", status=Status.Success, retcode=0)])
    host.run = AsyncMock(return_value=ok)
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))
    host.rm = AsyncMock(return_value=Result(Status.Success))
    host._loaded_modules = AsyncMock(return_value=Result(Status.Success, value=["m"]))
    host.default_dest_dir = Path("/tmp")
    return host


@pytest.mark.parametrize(("user", "root"), [(_RULED_ROOT, True), ("root", False)])
@pytest.mark.asyncio
async def test_every_root_decision_follows_the_one_rule(user, root, tmp_path):
    fp = parse_fingerprint("h", "10.0.0.1", f"user={user}\n")
    assert fp.privileged is root
    assert (_privilege(fp) == "root") is root
    host = _Host(user)
    assert elevates(host) is not root
    await _elevated(host, "true", 1.0)
    await _root_run(host, "true")
    assert host.sudo == [not root, not root]

    unix = _unix_host(user)
    assert unix._acts_as_root() is root
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    await unix.load(ko)
    await unix.unload("m")
    assert [c.kwargs["sudo"] for c in unix.run.await_args_list] == [not root, not root]
