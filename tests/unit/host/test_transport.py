"""
Unit tests for HopTransport / SshHopTransport.

Tests verify tunnel caching, port-forward delegation, and cascade cleanup
without touching real SSH connections.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh import SSHClientConnection

from otto.host.transport import HopTransportTornDownError, SshHopTransport

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_conn() -> MagicMock:
    conn = MagicMock(spec=SSHClientConnection)
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    return conn


@pytest.fixture
def factory(mock_conn: MagicMock) -> AsyncMock:
    return AsyncMock(return_value=mock_conn)


@pytest.fixture
def transport(factory: AsyncMock) -> SshHopTransport:
    return SshHopTransport(factory)


# ---------------------------------------------------------------------------
# get_tunnel
# ---------------------------------------------------------------------------


class TestGetTunnel:
    @pytest.mark.asyncio
    async def test_calls_factory(
        self, transport: SshHopTransport, factory: AsyncMock, mock_conn: MagicMock
    ):
        result = await transport.get_tunnel()
        factory.assert_awaited_once()
        assert result is mock_conn

    @pytest.mark.asyncio
    async def test_caches_connection(self, transport: SshHopTransport, factory: AsyncMock):
        first = await transport.get_tunnel()
        second = await transport.get_tunnel()
        assert first is second
        factory.assert_awaited_once()


# ---------------------------------------------------------------------------
# forward_port
# ---------------------------------------------------------------------------


class TestForwardPort:
    @pytest.mark.asyncio
    async def test_delegates_to_connection(self, transport: SshHopTransport, mock_conn: MagicMock):
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=mock_listener)

        port = await transport.forward_port("10.0.0.5", 22)
        mock_conn.forward_local_port.assert_awaited_once_with("localhost", 0, "10.0.0.5", 22)
        assert port == 44444

    @pytest.mark.asyncio
    async def test_returns_local_port(self, transport: SshHopTransport, mock_conn: MagicMock):
        mock_listener = MagicMock()
        mock_listener.get_port.return_value = 55555
        mock_conn.forward_local_port = AsyncMock(return_value=mock_listener)

        assert await transport.forward_port("192.168.1.1", 8080) == 55555

    @pytest.mark.asyncio
    async def test_tracks_listeners(self, transport: SshHopTransport, mock_conn: MagicMock):
        """Distinct destinations each get their own listener, tracked for cleanup."""
        for expected_port in (11111, 22222):
            listener = MagicMock()
            listener.get_port.return_value = expected_port
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", expected_port)

        assert len(transport._port_forwards) == 2

    @pytest.mark.asyncio
    async def test_repeat_destination_reuses_one_listener(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """A destination that already has a forward does not get a second one.

        This is the whole fix: the netcat path forwards the same remote port
        once per file, and before caching each call built a listener that was
        held until ``close()`` — two descriptors per transfer, forever.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        ports = [await transport.forward_port("10.0.0.1", 9000) for _ in range(5)]

        assert ports == [44444] * 5
        mock_conn.forward_local_port.assert_awaited_once()
        assert len(transport._port_forwards) == 1

    @pytest.mark.asyncio
    async def test_same_port_on_a_different_host_is_a_different_forward(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """The key is the destination, not the port.

        No caller reaches this today — ``_build_hop_transport`` constructs a
        fresh transport per target host and ``ConnectionManager._forward_port``
        always passes its own ``_ip``, so no instance sees two destinations.
        Pinned anyway because ``forward_port`` takes ``dest_host`` as a
        parameter: the day anything shares a transport across hosts, keying on
        the port alone routes one host's traffic to another, silently.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        await transport.forward_port("10.0.0.1", 9000)
        await transport.forward_port("10.0.0.2", 9000)

        assert mock_conn.forward_local_port.await_count == 2
        assert len(transport._port_forwards) == 2

    @pytest.mark.asyncio
    async def test_unforward_closes_the_listener_and_frees_the_key(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Releasing must both close the socket and re-arm the cache.

        Closing without dropping the key hands the next caller a dead port;
        dropping without closing is the leak this whole change is about.
        """
        first, second = MagicMock(), MagicMock()
        first.get_port.return_value = 11111
        second.get_port.return_value = 22222

        mock_conn.forward_local_port = AsyncMock(return_value=first)
        assert await transport.forward_port("10.0.0.1", 9000) == 11111

        transport.unforward_port("10.0.0.1", 9000)
        first.close.assert_called_once()
        assert transport._port_forwards == {}

        mock_conn.forward_local_port = AsyncMock(return_value=second)
        assert await transport.forward_port("10.0.0.1", 9000) == 22222, (
            "forward_port handed back the released listener's port"
        )

    @pytest.mark.asyncio
    async def test_unforward_of_an_unknown_destination_is_a_no_op(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Callers release from a ``finally`` without knowing whether they took one.

        The netcat attempt can fail before ``forward_port``, and on a direct
        (untunneled) host it never forwards at all, so a release that raised
        would replace a real error with its own.
        """
        listener = MagicMock()
        listener.get_port.return_value = 11111
        mock_conn.forward_local_port = AsyncMock(return_value=listener)
        await transport.forward_port("10.0.0.1", 9000)

        transport.unforward_port("10.0.0.1", 9999)
        transport.unforward_port("10.0.0.2", 9000)
        transport.unforward_port("10.0.0.1", 9000)
        transport.unforward_port("10.0.0.1", 9000)

        listener.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_forward_binds_loopback_not_every_interface(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """The listener must not be reachable from off-box.

        Every caller connects to "localhost", so binding "" only ever bought
        off-box reachability nobody asked for. A forward has always lived
        until ``close()`` — this is not a new exposure the caching created,
        it is a standing one the caching made worth removing.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444
        mock_conn.forward_local_port = AsyncMock(return_value=listener)

        await transport.forward_port("10.0.0.1", 9000)

        bind_host = mock_conn.forward_local_port.await_args.args[0]
        assert bind_host == "localhost", (
            f"port forward bound {bind_host!r}; a cached forward reachable from "
            "off-box is a standing route to the destination port"
        )

    @pytest.mark.asyncio
    async def test_concurrent_callers_build_one_forward(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """Without the lock both callers miss the cache and one listener leaks.

        The loser's listener is never stored, so ``close()`` cannot reach it —
        exactly the orphaning ``_conn_lock`` exists to prevent for the tunnel.
        """
        listener = MagicMock()
        listener.get_port.return_value = 44444

        async def slow_forward(*_args: object) -> MagicMock:
            await asyncio.sleep(0.01)
            return listener

        mock_conn.forward_local_port = AsyncMock(side_effect=slow_forward)

        ports = await asyncio.gather(*(transport.forward_port("10.0.0.1", 9000) for _ in range(4)))

        assert ports == [44444] * 4
        mock_conn.forward_local_port.assert_awaited_once()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_connection(self, transport: SshHopTransport, mock_conn: MagicMock):
        await transport.get_tunnel()
        await transport.close()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closes_port_forwards(self, transport: SshHopTransport, mock_conn: MagicMock):
        listeners = []
        for port in (11111, 22222):
            listener = MagicMock()
            listener.get_port.return_value = port
            listener.close = MagicMock()
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", port)
            listeners.append(listener)

        await transport.close()
        for listener in listeners:
            listener.close.assert_called_once()
        assert transport._port_forwards == {}

    @pytest.mark.asyncio
    async def test_close_re_arms_the_forward_cache(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """``close()`` invalidates the cached forward without ending the transport.

        An emptied dict is not enough to assert: a mutant that moved entries to
        a side table, or that cleared only on the parent cascade, satisfies
        ``_port_forwards == {}`` and then hands out a closed listener's port
        forever. Assert the observable consequence instead.

        This also pins the half of ``close()``'s contract that the race fix
        must not break. A closed transport stays USABLE — measured against the
        pre-fix tree, a post-close ``get_tunnel`` calls the factory a second
        time — and ``ConnectionManager`` depends on it, as does
        tunnel_stability's monitor-loop test, which closes a host precisely so
        the next scan dials through a wedged sshd. An earlier cut of the fix
        made the transport terminal and deleted this test; the generation
        counter exists so both properties hold at once.
        """
        first, second = MagicMock(), MagicMock()
        first.get_port.return_value = 11111
        second.get_port.return_value = 22222

        mock_conn.forward_local_port = AsyncMock(return_value=first)
        assert await transport.forward_port("10.0.0.1", 9000) == 11111
        await transport.close()

        mock_conn.forward_local_port = AsyncMock(return_value=second)
        assert await transport.forward_port("10.0.0.1", 9000) == 22222, (
            "forward_port returned the closed listener's port after close()"
        )

    @pytest.mark.asyncio
    async def test_one_bad_listener_does_not_abort_the_teardown(
        self, transport: SshHopTransport, mock_conn: MagicMock
    ):
        """A raising ``listener.close()`` must not skip the rest of teardown.

        Unguarded, the first raise takes out every later listener, the tunnel
        teardown, and the parent cascade — the cleanup path of a leak fix
        becoming the largest leak in the file. Ordering makes this reachable:
        dict iteration is insertion-ordered, so the earliest forward decides
        whether the rest are closed.
        """
        listeners = []
        for port in (11111, 22222, 33333):
            listener = MagicMock()
            listener.get_port.return_value = port
            mock_conn.forward_local_port = AsyncMock(return_value=listener)
            await transport.forward_port("10.0.0.1", port)
            listeners.append(listener)
        listeners[0].close.side_effect = OSError("listener already gone")

        await transport.close()

        for listener in listeners[1:]:
            listener.close.assert_called_once()
        assert transport._port_forwards == {}
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cascades_to_parent(self):
        parent_conn = MagicMock(spec=SSHClientConnection)
        parent_conn.close = MagicMock()
        parent_conn.wait_closed = AsyncMock()
        parent = SshHopTransport(AsyncMock(return_value=parent_conn))

        child_conn = MagicMock(spec=SSHClientConnection)
        child_conn.close = MagicMock()
        child_conn.wait_closed = AsyncMock()
        child = SshHopTransport(AsyncMock(return_value=child_conn), parent=parent)

        await child.get_tunnel()
        await parent.get_tunnel()
        await child.close()

        child_conn.close.assert_called_once()
        parent_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent(self, transport: SshHopTransport, mock_conn: MagicMock):
        await transport.get_tunnel()
        await transport.close()
        await transport.close()  # should not raise
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_without_connect(self, factory: AsyncMock):
        """Closing a transport that was never used is a no-op."""
        transport = SshHopTransport(factory)
        await transport.close()  # should not raise
        factory.assert_not_awaited()


# Runaway guard for the teardown-latency control below: it catches a HANG, so
# it is generous on purpose and discriminates nothing. A tight value here would
# re-enter the wall-clock-bound class the 2026-08-08 sweep removed.
_TEARDOWN_RUNAWAY_S = 60.0


class TestCloseRacesWithInFlightWork:
    """``close()`` ends a GENERATION of resources, and must end it completely.

    It is deliberately not terminal — a closed transport stays usable, which
    ``ConnectionManager`` relies on and which tunnel_stability's monitor-loop
    test uses on purpose to force a reconnect (see
    ``test_close_re_arms_the_forward_cache``). So the property here is narrower
    and harder than "nothing is created after close": anything created FOR the
    generation that close ended must be released by whatever created it, while
    a later caller stays free to build afresh.

    Every other leak-prevention mechanism — the forward cache, the per-attempt
    release, the guarded teardown loop — treats ``close()`` as the backstop that
    catches what they missed, so a resource escaping it escapes everything.
    Four windows allow that, and each has its own test:

    - the factory await in ``get_tunnel`` (a whole SSH connection), which on a
      multi-hop chain also builds and connects the PARENT as a side effect;
    - the bind await in ``forward_port`` (a listener);
    - waiting on ``_conn_lock`` while another caller's handshake holds it;
    - waiting on ``_forward_lock`` while another caller's bind holds it.

    The last two are why both methods record the generation before any await
    rather than next to the check: a queued caller resumes after the bump and
    would otherwise read the new generation and find nothing wrong.

    Every test parks a real ``asyncio.Task`` inside its window and runs the
    whole of ``close()`` while it is parked, so the interleaving is
    deterministic rather than a timing hope — no sleeps, no retries. One
    caveat worth knowing before adding another: an ``AsyncMock`` ``wait_closed``
    never yields, so in most of these ``close()`` runs atomically and the
    parked task can only resume once it has returned. That makes them blind to
    WHERE inside ``close()`` the generation is bumped;
    ``test_the_generation_ends_before_close_can_yield`` supplies a
    ``wait_closed`` that really yields, and is the only test that pins it.

    Deliberately NOT fixed by making ``close()`` take a lock. Two reasons, and
    the tempting third one is false:

    - ``_forward_lock`` does not cover the cold window at all. That window is
      inside ``get_tunnel``, under ``_conn_lock``, so a ``close()`` waiting on
      the forward lock still misses it — and one that waited on ``_conn_lock``
      would block teardown for a full SSH connect timeout.
    - Teardown latency should not be coupled to arbitrary in-flight work.
      ``test_close_does_not_wait_on_a_bind`` pins that, and a faithful mutant
      (``close()`` acquiring and releasing ``_forward_lock``) reddens exactly
      it while leaving ``test_idempotent`` green.

    The false reason, recorded because an earlier version of this comment
    asserted it: that the parked task can only release the lock after
    ``close()`` returns, making it a true deadlock. It cannot. asyncssh's
    ``forward_local_port`` is local socket work — getaddrinfo, bind,
    create_server — so a cancelled bind unwinds its ``async with`` and releases
    the lock immediately. The property this control checks is real; that
    explanation for it was not.
    """

    @staticmethod
    def _conn() -> MagicMock:
        conn = MagicMock(spec=SSHClientConnection)
        conn.close = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn

    @pytest.mark.asyncio
    async def test_a_handshake_in_flight_across_close_leaves_no_live_connection(self):
        """The cold window: ``close()`` runs while the factory is still dialling.

        ``close()`` sees ``_conn is None`` and skips its teardown entirely, then
        ``get_tunnel`` resumes and assigns the finished connection. The result is
        a live SSH connection to the hop owned by a closed transport — the
        zombie-transport class, whose ``ResourceWarning`` surfaces on whichever
        test runs next.
        """
        reached, release = asyncio.Event(), asyncio.Event()
        conn = self._conn()
        conn.forward_local_port = AsyncMock(return_value=MagicMock())

        async def factory(**_):
            reached.set()
            await release.wait()
            return conn

        transport = SshHopTransport(factory)
        pending = asyncio.create_task(transport.forward_port("10.0.0.1", 23))
        await reached.wait()

        await transport.close()

        release.set()
        with pytest.raises(HopTransportTornDownError):
            await pending

        conn.close.assert_called_once()
        # Not just closed — never adopted. Without this an implementation that
        # stores the connection and then closes it satisfies the assertion
        # above while leaving a dead conn cached for the next caller to reuse.
        assert transport._conn is None

    @pytest.mark.asyncio
    async def test_a_bind_in_flight_across_close_strands_no_listener(self):
        """The warm window: ``close()`` runs while ``forward_local_port`` is awaited.

        The teardown loop cannot see a listener that does not exist yet, and the
        resuming ``forward_port`` stores it into the dict ``close()`` just
        cleared. Nothing will ever close it.
        """
        reached, release = asyncio.Event(), asyncio.Event()
        listener = MagicMock()
        listener.get_port.return_value = 12345
        listener.close = MagicMock()

        async def forward_local_port(*_args, **_kwargs):
            reached.set()
            await release.wait()
            return listener

        conn = self._conn()
        conn.forward_local_port = forward_local_port
        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        pending = asyncio.create_task(transport.forward_port("10.0.0.1", 23))
        await reached.wait()

        await transport.close()

        release.set()
        with pytest.raises(HopTransportTornDownError):
            await pending

        listener.close.assert_called_once()
        assert transport._port_forwards == {}

    @pytest.mark.asyncio
    async def test_close_does_not_wait_on_a_bind(self):
        """Teardown must not block on ``_forward_lock``.

        The negative control for the obvious fix: ``close()`` must not couple
        its latency to arbitrary in-flight work. A bind holds ``_forward_lock``
        for as long as its socket setup takes, and a ``close()`` that acquired
        the lock would queue behind it.

        NOT because that is a deadlock — see the class docstring; an earlier
        version of this one claimed it was, and it is false. The bound below is
        a runaway guard for a hang, so its value is deliberately generous and
        discriminates nothing.
        """
        reached, release = asyncio.Event(), asyncio.Event()

        async def forward_local_port(*_args, **_kwargs):
            reached.set()
            await release.wait()
            return MagicMock()

        conn = self._conn()
        conn.forward_local_port = forward_local_port
        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        pending = asyncio.create_task(transport.forward_port("10.0.0.1", 23))
        await reached.wait()

        try:
            await asyncio.wait_for(transport.close(), timeout=_TEARDOWN_RUNAWAY_S)
        finally:
            # Even on expiry: otherwise the parked bind is destroyed pending and
            # the asyncio leak detector fires on the one path where the signal
            # most needs to be clean.
            release.set()
            with contextlib.suppress(Exception):
                await pending

    @pytest.mark.asyncio
    async def test_the_generation_ends_before_close_can_yield(self):
        """The ordering ``close()`` depends on, pinned against a bump-last mutant.

        The other window tests cannot see this. They drive ``wait_closed`` with
        an ``AsyncMock``, which returns without ever yielding, so ``close()``
        runs atomically and the parked task can only resume once it has already
        returned — by which point the flag is set no matter WHERE in the method
        it was set. Moving ``self._generation += 1`` to the last line of
        ``close()`` therefore passes every one of them.

        Real teardown does yield: ``wait_closed()`` is a network round trip and
        ``await self._parent.close()`` recurses into another one. So this test
        resumes the bind from INSIDE ``close()``'s own await — the interleaving
        that actually happens — and the bump's position becomes observable.
        """
        bind_started, resume_bind, bind_finished = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        listener = MagicMock()
        listener.get_port.return_value = 12345
        listener.close = MagicMock()

        async def forward_local_port(*_args, **_kwargs):
            bind_started.set()
            await resume_bind.wait()
            return listener

        async def wait_closed():
            # Stand in for the real round trip: let the parked bind run to
            # completion before teardown returns, which is exactly what a
            # yielding await allows.
            resume_bind.set()
            await bind_finished.wait()

        conn = MagicMock(spec=SSHClientConnection)
        conn.close = MagicMock()
        conn.wait_closed = wait_closed
        conn.forward_local_port = forward_local_port

        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        async def do_forward():
            try:
                await transport.forward_port("10.0.0.1", 23)
            finally:
                bind_finished.set()

        pending = asyncio.create_task(do_forward())
        await bind_started.wait()

        await transport.close()

        with pytest.raises(HopTransportTornDownError):
            await pending

        listener.close.assert_called_once()
        assert transport._port_forwards == {}, (
            "a listener bound during close() was registered with a transport "
            "that had already swept its dict"
        )

    @pytest.mark.asyncio
    async def test_a_parent_built_during_the_cold_window_is_not_left_open(self):
        """The cold window on a MULTI-HOP chain also builds the parent.

        ``RemoteHost._build_hop_transport``'s factory assigns ``outer._parent``
        and opens the parent's SSH connection as a side effect, both inside the
        single await this window brackets (remote_host.py, "Build the parent
        SshHopTransport lazily on first use"). So ``close()`` reads
        ``_parent is None``, skips the cascade, and the factory then produces a
        live SSH session to the INTERMEDIATE hop owned by a transport that was
        never marked closed. Tearing down only the connection we were handed
        relocates the zombie one hop up instead of removing it.
        """
        reached, release = asyncio.Event(), asyncio.Event()
        parent_conn, child_conn = self._conn(), self._conn()
        child: SshHopTransport

        async def child_factory(**_):
            reached.set()
            await release.wait()
            child._parent = SshHopTransport(AsyncMock(return_value=parent_conn))
            await child._parent.get_tunnel()
            return child_conn

        child = SshHopTransport(child_factory)
        pending = asyncio.create_task(child.forward_port("10.0.0.1", 23))
        await reached.wait()

        await child.close()

        release.set()
        with pytest.raises(HopTransportTornDownError):
            await pending

        child_conn.close.assert_called_once()
        assert parent_conn.close.call_count == 1, (
            "the parent transport built during the window is still open — the "
            "zombie moved one hop up rather than being removed"
        )

    @pytest.mark.asyncio
    async def test_a_caller_queued_on_the_tunnel_lock_does_not_adopt_a_dead_generation(self):
        """The generation must be read before the lock, not inside it.

        An uncontended ``Lock.acquire()`` returns without yielding, so a lone
        caller reads the generation at what is effectively method entry. A
        CONTENDED one yields — so a second caller queued behind the first
        resumes after ``close()`` has already bumped, reads the NEW generation,
        and its check can never fire. It then adopts a live SSH connection onto
        a transport whose ``close()`` has returned: the original leak, reached by
        a different route.

        This is the window the abandoned ``_closed`` entry guard happened to
        cover, which is why the pivot to a generation counter had to move the
        capture rather than merely rename it.
        """
        first_call, release_first = asyncio.Event(), asyncio.Event()
        conns: list[MagicMock] = []

        async def factory(**_):
            conn = self._conn()
            conn.forward_local_port = AsyncMock(return_value=MagicMock())
            conns.append(conn)
            if len(conns) == 1:
                first_call.set()
                await release_first.wait()
            return conn

        transport = SshHopTransport(factory)
        first = asyncio.create_task(transport.get_tunnel())
        await first_call.wait()
        # Second caller queues on _conn_lock while the first holds it.
        second = asyncio.create_task(transport.get_tunnel())
        await asyncio.sleep(0)
        assert transport._conn_lock.locked()

        await transport.close()

        release_first.set()
        for task in (first, second):
            with pytest.raises(HopTransportTornDownError):
                await task

        assert transport._conn is None, (
            "a caller queued on _conn_lock adopted a connection built after "
            "close() returned — it read the generation the bump had already moved"
        )
        for index, conn in enumerate(conns):
            assert conn.close.call_count == 1, (
                f"connection {index} of {len(conns)} was built for a dead "
                "generation and never closed"
            )

    @pytest.mark.asyncio
    async def test_a_caller_queued_on_the_forward_lock_does_not_adopt_a_dead_generation(self):
        """``forward_port`` records the generation at ENTRY, and that matters.

        The sibling of the ``_conn_lock`` case, and the scenario
        ``forward_port``'s own comment cites — "the wait for ``_forward_lock``
        (which DOES yield when contended)". Nothing exercised it: a mutant that
        moves the capture inside the lock passes every other test in this class
        while stranding a listener, because the queued caller reads a generation
        the bump has already moved.
        """
        bind_started, release_bind = asyncio.Event(), asyncio.Event()
        listeners: list[MagicMock] = []

        async def forward_local_port(*_args, **_kwargs):
            listener = MagicMock()
            listener.get_port.return_value = 20000 + len(listeners)
            listener.close = MagicMock()
            listeners.append(listener)
            if len(listeners) == 1:
                bind_started.set()
                await release_bind.wait()
            return listener

        conn = self._conn()
        conn.forward_local_port = forward_local_port
        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        first = asyncio.create_task(transport.forward_port("10.0.0.1", 23))
        await bind_started.wait()
        # Distinct destination, so the cache cannot short-circuit it, and the
        # tunnel is warm — so this queues on _forward_lock and nowhere else.
        second = asyncio.create_task(transport.forward_port("10.0.0.1", 24))
        await asyncio.sleep(0)
        assert transport._forward_lock.locked()

        await transport.close()

        release_bind.set()
        for task in (first, second):
            with pytest.raises(HopTransportTornDownError):
                await task

        assert transport._port_forwards == {}, (
            "a caller queued on _forward_lock registered its listener with a "
            "transport that had already swept its dict"
        )
        for index, listener in enumerate(listeners):
            assert listener.close.call_count == 1, (
                f"listener {index} of {len(listeners)} was bound for a dead "
                "generation and never closed"
            )

    @pytest.mark.asyncio
    async def test_the_parent_reference_survives_an_abandoned_handshake(self):
        """Abandoning a handshake must not disinherit the parent.

        ``__init__`` documents that a parent passed to the constructor is
        "closed automatically when *this* transport is closed". An earlier cut
        of the abandon path cleared ``self._parent`` while cascading it, so a
        transport reused after a lost race would open a parent connection that
        its own ``close()`` no longer reached — a leak created by the leak fix,
        on the second lap.
        """
        parent_conns: list[MagicMock] = []

        async def parent_factory(**_):
            conn = self._conn()
            parent_conns.append(conn)
            return conn

        parent = SshHopTransport(parent_factory)
        reached, release = asyncio.Event(), asyncio.Event()
        parked = True

        async def child_factory(**_):
            nonlocal parked
            if parked:
                parked = False
                reached.set()
                await release.wait()
            # What the real factory does: resolve the parent's tunnel first.
            await parent.get_tunnel()
            return self._conn()

        child = SshHopTransport(child_factory, parent=parent)
        pending = asyncio.create_task(child.get_tunnel())
        await reached.wait()

        await child.close()

        release.set()
        with pytest.raises(HopTransportTornDownError):
            await pending

        # A closed transport is reusable, so this is a legitimate second lap.
        await child.get_tunnel()
        await child.close()

        assert parent_conns, "the parent was never dialled — test no longer exercises it"
        for index, conn in enumerate(parent_conns):
            assert conn.close.call_count == 1, (
                f"parent connection {index} of {len(parent_conns)} was left open; "
                "the cascade lost its parent reference"
            )

    @pytest.mark.asyncio
    async def test_teardown_defuses_the_asyncio_zombie_transport(self):
        """The stated reason ``_teardown_connection`` exists, asserted.

        asyncssh's ``wait_closed()`` returns before the asyncio transport's
        ``connection_lost`` fires, leaving ``_closing=False`` on a socket that is
        already gone; that zombie's ``__del__`` raises ``ResourceWarning`` on a
        closed loop and pytest's ``[unraisable]`` plugin escalates it into a
        failure of an unrelated later test. Nothing pinned the mitigation, so a
        bare ``conn.close()`` on either path passed the whole suite.
        """
        conn = self._conn()
        conn._transport = MagicMock()
        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        await transport.close()

        conn._transport.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_abandoned_connection_is_also_defused(self):
        """The abandon path needs it MORE, not less.

        A connection dropped there is dropped precisely because nobody will
        ever close it again, so if its asyncio transport is left armed the
        warning is guaranteed rather than merely possible.
        """
        reached, release = asyncio.Event(), asyncio.Event()
        conn = self._conn()
        conn._transport = MagicMock()

        async def factory(**_):
            reached.set()
            await release.wait()
            return conn

        transport = SshHopTransport(factory)
        pending = asyncio.create_task(transport.get_tunnel())
        await reached.wait()

        await transport.close()

        release.set()
        with pytest.raises(HopTransportTornDownError):
            await pending

        conn._transport.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_raising_wait_closed_still_defuses_the_transport(self):
        """The mitigation is in a ``finally`` on purpose.

        ``wait_closed()`` talks to a socket that may already be broken. If it
        raises and takes the ``asyncio_transport.close()`` with it, the zombie
        survives exactly in the case where the connection died badly — the case
        most likely to produce one.
        """
        conn = self._conn()
        conn._transport = MagicMock()
        conn.wait_closed = AsyncMock(side_effect=OSError("socket already gone"))
        transport = SshHopTransport(AsyncMock(return_value=conn))
        await transport.get_tunnel()

        with pytest.raises(OSError, match="socket already gone"):
            await transport.close()

        conn._transport.close.assert_called_once()
