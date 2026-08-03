"""Privilege chaos: interrupt a block running as another user (``as_user``).

Cancels ``PosixPrivilege.as_user`` (``src/otto/host/privilege.py``, Task 3)
mid-body — inside the ``async with host.as_user("test")`` block's own sleep,
never mid-undo (mid-undo is unit-tested, see
``tests/unit/host/test_session.py::test_host_session_as_user_undo_survives_cancellation``
for the mocked-shell proof that a SECOND cancel landing during the undo
chain is held by ``otto.lifecycle.compensate`` until the chain finishes).
Confirms the shielded compensating undo restores the login user on the SAME
real ssh session on a live leased veggies bed host, and that the session
stays usable afterwards.

BedHygiene (autouse, ``tests/e2e/chaos/conftest.py``) asserts the leased
host is left clean; this module deliberately does NOT opt out (no
``no_hygiene_bracket``) -- it exercises the default (now-lazy, Task 1)
hygiene bracket, so a bracket regression here is itself signal.

Named-session twin: ``HostSession.as_user`` (``src/otto/host/session.py``,
shielded the same way at Task 3) IS reachable on a bed ``UnixHost`` --
``host.open_session(name)`` is proven live against a real ssh/telnet
``UnixHost`` in
``tests/integration/host/test_unix_host_integration.py::TestNamedSessionIntegration``
(no docker/embedded plumbing gap the way ``DockerContainerHost``/
``EmbeddedHost`` have for OTHER named-session edge cases). So this module
drives BOTH the default-session scenario and a named-session twin through an
auxiliary session opened on the SAME bed host, rather than resting solely on
the Task 3 unit test -- that unit test only proves the mid-undo shield
against a mocked shell, never restoration + usability on a real remote
shell, which is exactly the gap an e2e proof closes.

Deviation from the brief's literal transcription (live-bed rule: root-cause
first, never paper over): ``host.run(...)``/``session.run(...)`` always
return a :class:`~otto.result.Results` aggregate (``.value`` is a
``list[CommandResult]``, never a bare string) -- every other module in this
family (``test_login_proxy_e2e.py``, ``test_proxy_user_stability_integration.py``,
``test_unix_host_integration.py``) unwraps the single command via
``.only.value``/``.only.status``, so this module does the same rather than
the brief's bare ``.value``/``.status``, which would raise
``AttributeError``/always-false membership on a real ``Results``.

Session-continuity marker (review finding): ``whoami`` answering the login
user again is NOT, by itself, proof the shielded undo ran on the SAME shell
process -- ``_ensure_session`` rebuilding a dead session from scratch would
also seed a fresh shell with the login user, and that false positive would
look identical to a genuine restore. Both scenarios therefore export a
random per-test token into the shell's environment *before* entering
``as_user`` and read it back *after* the cancel + ``whoami`` check: a
rebuilt shell never ran that ``export`` and comes back empty, while the
original process still carries it no matter how many nested ``su``/``exit``
hops ran inside it.
"""

import asyncio
import uuid

import pytest

from otto.utils import Status
from tests._fixtures.tunnel_bed import build_bed_host
from tests.e2e.chaos._seed import offset_in

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


def test_cancel_inside_as_user_restores_login_user(chaos_bed, chaos_rng):
    """Interrupt a block running as another user: the shielded undo unwinds
    the switch, the SAME session answers as the login user afterwards, and
    the session stays usable (spec: privilege surface)."""

    async def scenario() -> None:
        host = build_bed_host(chaos_bed.element)
        try:
            login_user = (await host.run("whoami", timeout=30)).only.value.strip()

            # Identity nonce for THIS shell process only -- reproducibility
            # isn't needed (unlike chaos_rng's seeded offsets), just
            # uniqueness per test run. Exported BEFORE as_user so it lives in
            # the persistent shell's own environment, surviving any number of
            # nested su/exit hops -- a rebuilt session never ran this export.
            token = uuid.uuid4().hex
            await host.run(f"export OTTO_CHAOS_CONT={token}", timeout=30)

            switched = asyncio.Event()

            async def body() -> None:
                async with host.as_user("test"):
                    who = (await host.run("whoami", timeout=30)).only
                    assert who.value.strip() == "test", who.value
                    switched.set()
                    await asyncio.sleep(60)  # the cancel window

            task = asyncio.create_task(body())
            await asyncio.wait_for(switched.wait(), timeout=60)
            await asyncio.sleep(offset_in(chaos_rng, 0.05, 1.5))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            who_now = (await host.run("whoami", timeout=30)).only
            assert who_now.status == Status.Success
            assert who_now.value.strip() == login_user, (
                f"{chaos_bed.element}: session stranded as {who_now.value.strip()!r} "
                "after interrupted as_user"
            )
            cont = (await host.run("echo $OTTO_CHAOS_CONT", timeout=30)).only
            assert cont.value.strip() == token, (
                f"{chaos_bed.element}: continuity marker missing/mismatched "
                f"(want {token!r}, got {cont.value.strip()!r}) -- session was "
                "rebuilt, the undo never ran on the original shell"
            )
            ok = (await host.run("echo usable", timeout=30)).only
            assert ok.status == Status.Success
            assert "usable" in ok.value
        finally:
            await host.close()

    asyncio.run(scenario())


def test_cancel_inside_named_session_as_user_restores_login_user(chaos_bed, chaos_rng):
    """Named-session twin: the same choreography through a named session
    (``host.open_session`` + ``HostSession.as_user``) -- proving the Task 3
    shielded undo on a real remote shell, not just the mocked-shell unit
    proof (see the module docstring)."""

    async def scenario() -> None:
        host = build_bed_host(chaos_bed.element)
        try:
            session = await host.open_session("privilege_chaos")
            try:
                login_user = (await session.run("whoami", timeout=30)).only.value.strip()

                # Identity nonce for THIS named session's shell process only --
                # reproducibility isn't needed, just per-test uniqueness. See
                # the module docstring's "Session-continuity marker" note.
                token = uuid.uuid4().hex
                await session.run(f"export OTTO_CHAOS_CONT={token}", timeout=30)

                switched = asyncio.Event()

                async def body() -> None:
                    async with session.as_user("test"):
                        who = (await session.run("whoami", timeout=30)).only
                        assert who.value.strip() == "test", who.value
                        switched.set()
                        await asyncio.sleep(60)  # the cancel window

                task = asyncio.create_task(body())
                await asyncio.wait_for(switched.wait(), timeout=60)
                await asyncio.sleep(offset_in(chaos_rng, 0.05, 1.5))
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                who_now = (await session.run("whoami", timeout=30)).only
                assert who_now.status == Status.Success
                assert who_now.value.strip() == login_user, (
                    f"{chaos_bed.element}: named session stranded as "
                    f"{who_now.value.strip()!r} after interrupted as_user"
                )
                cont = (await session.run("echo $OTTO_CHAOS_CONT", timeout=30)).only
                assert cont.value.strip() == token, (
                    f"{chaos_bed.element}: named session continuity marker "
                    f"missing/mismatched (want {token!r}, got "
                    f"{cont.value.strip()!r}) -- session was rebuilt, the undo "
                    "never ran on the original shell"
                )
                ok = (await session.run("echo usable", timeout=30)).only
                assert ok.status == Status.Success
                assert "usable" in ok.value
            finally:
                await session.close()
        finally:
            await host.close()

    asyncio.run(scenario())
