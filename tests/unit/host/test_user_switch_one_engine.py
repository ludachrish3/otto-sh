"""``run(user=X)`` and ``exec(user=X)`` reach the one switch engine for the same X.

A user with no credential entry has no ssh login of their own, so neither
verb can connect as them: both switch to them, through
:func:`otto.host.login_proxy.perform_switch`, on every term and on the
local host. The route planner answers "no direct login" for such a user
rather than refusing the call.
"""

import pytest

from otto.host import login_proxy
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred, LoginProxyError
from tests.unit.host.conftest import unix_host_over_scripted

_UNKNOWN = "otto-no-cred-user"
"""A user no credential entry names."""


class _SwitchReachedError(Exception):
    """Raised by the patched engine: the switch to :attr:`user` was attempted."""

    def __init__(self, user: str) -> None:
        super().__init__(user)
        self.user = user


async def _engine_reached(_io, _creds, user, *_args, **_kwargs):
    raise _SwitchReachedError(user)


async def _host(term: str):
    if term == "local":
        return LocalHost()
    host, _ = await unix_host_over_scripted(
        term, elevation="sudo", creds=[Cred(login="admin", password="pw")]
    )
    return host


async def _issue(host, verb: str):
    if verb == "run":
        return await host.run("id -un", timeout=5, user=_UNKNOWN)
    return await host.exec("id -un", timeout=5, user=_UNKNOWN)


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["run", "exec"])
@pytest.mark.parametrize("term", ["ssh", "telnet", "local"])
async def test_both_verbs_switch_to_a_user_with_no_credential_through_one_engine(
    term, verb, monkeypatch
):
    monkeypatch.setattr(login_proxy, "perform_switch", _engine_reached)
    host = await _host(term)
    try:
        with pytest.raises(_SwitchReachedError) as reached:
            await _issue(host, verb)
    except LoginProxyError as refused:  # pragma: no cover - the defect this pins
        pytest.fail(f"{term} {verb}(user=) refused before switching: {refused}")
    finally:
        await host.close()
    assert reached.value.user == _UNKNOWN
