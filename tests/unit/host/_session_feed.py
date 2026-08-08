"""Explicit feeder ordering for the ShellSession test doubles.

The family's old shape — ``await asyncio.sleep(0.01)`` then ``feed()`` — was
wall-clock ordering between a feeder task and the code under test. It never
flaked only because ``MockSession.feed()`` writes into an
``asyncio.StreamReader``, which buffers a feed that arrives before the read
(an emergent property nothing enforces — review §3.4: one MockSession change
turns all 65 sites flaky at once). ``feed_after_write`` replaces the sleep
with the ordering those sites actually mean: "the session has WRITTEN its
next command and is therefore about to read output."

Baseline capture is EAGER (in the plain, synchronous outer call), which is
load-bearing: ``run_cmd()`` executes synchronously through ``_write()`` until
it blocks in ``_read_until_pattern``, so a feeder task's body only starts
running AFTER the write has happened. A baseline read inside the coroutine
would already include the write it means to wait for, and the wait would
never fire. Calling ``feed_after_write(...)`` BEFORE ``run_cmd()`` (the
family's universal shape — the returned coroutine goes straight into
``asyncio.create_task``) captures the pre-command count.

On a session that never writes, the wait expires LOUDLY with a named premise
failure instead of the old shape's silent feed-into-the-void (whose eventual
symptom was an unrelated-looking read timeout or hang).
"""

from collections.abc import Callable, Coroutine

from otto.utils import wait_for_async


class FeedAfterWriteMixin:
    """Mixin for ShellSession test doubles: every double records outbound
    writes in ``self.written`` and exposes ``feed()``; the mixin turns that
    pair into an explicit feeder-ordering primitive."""

    written: list[str]

    def feed(self, data: str) -> None:  # pragma: no cover - provided by the double
        raise NotImplementedError

    def feed_after_write(
        self,
        *chunks: str,
        timeout: float = 5.0,
        then: Callable[[], None] | None = None,
    ) -> Coroutine[None, None, None]:
        """Feed *chunks*, in order, once the session has written a NEW command.

        Returns a coroutine (pass it to ``asyncio.create_task``); the
        written-count baseline is captured HERE, synchronously, before the
        command under test runs — see the module docstring for why that
        eagerness is load-bearing. Only the first chunk is ordered; later
        chunks follow immediately (one command writes once, and its output
        may arrive in as many pieces as the test likes).

        ``then`` is called after the chunks under the same ordering, for the
        transport-death doubles whose event is not content — ``feed_eof``,
        ``feed_connection_lost``, ``feed_write_broken_pipe`` — which are
        typically passed with no chunks at all.
        """
        baseline = len(self.written)

        async def _feed() -> None:
            await wait_for_async(
                lambda: len(self.written) > baseline,
                timeout,
                interval=0.005,
                on_timeout=(
                    f"session never wrote a command past baseline {baseline} — "
                    f"feeder premise failed; written={self.written!r}"
                ),
            )
            for chunk in chunks:
                self.feed(chunk)
            if then is not None:
                then()

        return _feed()
