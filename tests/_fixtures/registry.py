"""Duck hosts for the loop registry's tests: the leaf sees only HostRegistration records."""

import asyncio
from collections.abc import Awaitable, Callable

from otto import invocation as inv


class DuckHost:
    """A host stand-in whose close can block, fail, refuse cancellation or run a callback.

    Its ownership check and its abandon callback can raise, as a defective host's might.
    With ``release_on_cancel`` a cancelled close does what ``BaseHost.close``'s
    ``finally`` does (clear the owner and remove its own record, at its generation)
    and then runs ``after_cancel``, before the cancellation propagates.
    """

    def __init__(
        self,
        name: str,
        *,
        parent: "DuckHost | None" = None,
        block: bool = False,
        fail: "BaseException | None" = None,
        refuse_cancel: bool = False,
        on_close: "Callable[[], Awaitable[None]] | None" = None,
        owned_by_fails: "Exception | None" = None,
        abandon_fails: "Exception | None" = None,
        release_on_cancel: bool = False,
        after_cancel: "Callable[[], None] | None" = None,
    ) -> None:
        self.id = name
        self.parent = parent
        self.block = block
        self.fail = fail
        self.refuse_cancel = refuse_cancel
        self.on_close = on_close
        self.owned_by_fails = owned_by_fails
        self.abandon_fails = abandon_fails
        self.release_on_cancel = release_on_cancel
        self.after_cancel = after_cancel
        self.generation = 0
        self.owner: "asyncio.AbstractEventLoop | None" = None
        self.closes_started = 0
        self.closes_finished = 0
        self.cancels_refused = 0
        self.abandon_calls: list[int] = []
        self.abandoned: list[int] = []
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.gate = asyncio.Event()

    def record(self, loop: asyncio.AbstractEventLoop) -> inv.HostRegistration:
        return inv.HostRegistration(
            key=id(self),
            display_id=self.id,
            depends_on=[] if self.parent is None else [id(self.parent)],
            generation=self.generation,
            close=self.close,
            owned_by=self._owned_by,
            abandon=self._abandon,
        )

    def claim(self, loop: asyncio.AbstractEventLoop) -> None:
        self.owner = loop
        self.generation += 1
        inv.register(loop, self.record(loop))

    async def close(self) -> None:
        self.closes_started += 1
        self.entered.set()
        loop, generation = self.owner, self.generation
        if self.on_close is not None:
            await self.on_close()
        if self.block:
            if self.refuse_cancel:
                while not self.gate.is_set():
                    await self._wait_refusing_cancel()
            else:
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    if self.release_on_cancel:
                        self._release(loop, generation)
                        if self.after_cancel is not None:
                            self.after_cancel()
                    raise
        if self.fail is not None:
            raise self.fail
        self.closes_finished += 1
        self.finished.set()
        self._release(loop, generation)

    def _release(self, loop: "asyncio.AbstractEventLoop | None", generation: int) -> None:
        """Unown and unregister, unless the host reconnected since *generation*."""
        if self.generation == generation:
            self.owner = None
            if loop is not None:
                inv.unregister(loop, id(self), generation)

    async def _wait_refusing_cancel(self) -> None:
        """Wait for the gate once; a cancel is counted and swallowed."""
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancels_refused += 1

    def _owned_by(self, loop: asyncio.AbstractEventLoop) -> bool:
        if self.owned_by_fails is not None:
            raise self.owned_by_fails
        return self.owner is loop

    def _abandon(self, loop: asyncio.AbstractEventLoop, generation: int) -> bool:
        """Record the call, then drop the host if *generation* and the owner still match."""
        self.abandon_calls.append(generation)
        if self.abandon_fails is not None:
            raise self.abandon_fails
        if generation != self.generation or self.owner not in (loop, None):
            return False
        self.abandoned.append(generation)
        self.owner = None
        self.generation += 1
        return True


def registered_ids(loop: asyncio.AbstractEventLoop) -> list[str]:
    """The display ids registered on *loop*, in registration order ([] when none)."""
    return next((s.display_ids for s in inv.open_registrations() if s.loop is loop), [])


def register_duck(host: object, loop: asyncio.AbstractEventLoop) -> None:
    """Register a bare double (``id`` and ``close()`` only) as a record on *loop*."""
    inv.register(
        loop,
        inv.HostRegistration(
            key=id(host),
            display_id=getattr(host, "id", repr(host)),
            depends_on=[],
            generation=0,
            close=host.close,  # type: ignore[attr-defined]
            owned_by=lambda lp: lp is loop,
            abandon=lambda lp, generation: False,  # a bare double has nothing to drop
        ),
    )
