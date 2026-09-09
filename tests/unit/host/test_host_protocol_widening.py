"""The Host protocol names every public member BaseHost gives all families.

Spec 2026-09-09 host-field-single-home §3.8: app_shell, as_user, switch_user
and current_user were public in practice (every family answers them, the
docs teach them) but absent from the protocol; the protocol is the contract
the golden and the conformance asserter read, so it must name them.
"""

import pytest

from otto.host.host import BaseHost, Host

WIDENED = ("app_shell", "as_user", "switch_user")


@pytest.mark.parametrize("name", WIDENED)
def test_protocol_names_the_member_with_basehost_keyword_names(name):
    from otto.testing.conformance_host import _keyword_names

    assert name in vars(Host), f"Host protocol lacks {name}"
    assert _keyword_names(getattr(Host, name)) == _keyword_names(getattr(BaseHost, name))


def test_protocol_current_user_is_a_property_like_element():
    assert isinstance(vars(Host)["current_user"], property)
    assert isinstance(vars(BaseHost)["current_user"], property)


def test_basehost_as_user_default_is_an_async_context_manager_that_refuses():
    """The refusal has the REAL call shape: `async with host.as_user(...)`."""
    import asyncio

    from otto.host.local_host import LocalHost

    class Bare(BaseHost):  # a family without the posix mixin
        capabilities = LocalHost.capabilities

        async def _run_one(self, *a, **k):  # pragma: no cover - never called
            raise AssertionError

    inst = Bare.__new__(Bare)  # no __init__: only the method shape is under test
    cm = inst.as_user("root")
    assert hasattr(cm, "__aenter__"), "as_user() must return an async context manager"

    async def enter():
        async with cm:
            pass

    with pytest.raises(NotImplementedError, match="as_user is not supported on 'Bare'"):
        asyncio.run(enter())
