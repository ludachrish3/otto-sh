"""Run an in-process inner pytest session the way ``otto test`` does."""

from otto.suite.run import ASYNCIO_LOOP_ARGS

INNER_ARGS = [
    # A nested in-process session: pytest-playwright's session-wide call
    # wrapper rejects re-entry, so `-p no:playwright` disables it. The inner
    # session runs with otto test's own loop-scope args (ASYNCIO_LOOP_ARGS),
    # not a stand-in, so it measures the real contract.
    "-p",
    "no:cacheprovider",
    "-p",
    "no:playwright",
    "-p",
    "no:randomly",
    *ASYNCIO_LOOP_ARGS,
    # pytest holds captured logs back unless a test fails; the inner session
    # prints them live so a test can match otto's own log lines.
    "-o",
    "log_cli=true",
    "-o",
    "log_cli_level=DEBUG",
]


FAKE_HOSTS = '''
import asyncio
import logging

from otto.host.local_host import LocalHost

CLOSED = []
FINISHED = []
_log = logging.getLogger("fakehost")


class RecordingHost(LocalHost):
    """A local shell that records the loop its close ran on."""

    def __init__(self, name):
        super().__init__()
        self.id = name
        self.rebuild_connections()

    async def _close(self):
        loop = asyncio.get_running_loop()
        CLOSED.append((self.id, id(loop), loop.is_closed()))
        _log.info("recorded close of %s, loop closed: %s", self.id, loop.is_closed())
        await super()._close()
        FINISHED.append(self.id)


class FailingHost(RecordingHost):
    async def _close(self):
        await super()._close()
        raise RuntimeError("boom")


class SlowHost(RecordingHost):
    """Hangs in its close, and runs its loop's teardown deadline out from inside it.

    It first waits for the hosts named in *after* to finish closing, then steps
    the loop's clock past any deadline: the expiry lands after them however
    slow the machine, where a short real deadline bet they would close in
    time (#705). The clock stays stepped: the loop is this class's, and the
    graces the expiry schedules are measured on it.
    """

    def __init__(self, name, after=()):
        super().__init__(name)
        self.after = list(after)

    async def _close(self):
        try:
            while not set(self.after) <= set(FINISHED):
                await asyncio.sleep(0)
            loop = asyncio.get_running_loop()
            real = loop.time
            loop.time = lambda: real() + 3600.0
            await asyncio.Event().wait()  # never set: only the deadline ends it
        finally:  # cancelled by the deadline: still release the local shell
            await LocalHost._close(self)
'''
"""Source of a ``fakehost`` module: ``LocalHost`` doubles that log and record each close.

A test writes it with ``pytester.makepyfile(fakehost=FAKE_HOSTS)``. The hosts
claim the running loop on first use, as every host does, so they register
with that loop with no test wiring. ``CLOSED`` gets ``(host id, id(loop),
loop closed?)`` for every close as it starts, ``FINISHED`` the id of every
close that completed.
"""


def run_inner(pytester, plugins, **files):
    """Write *files* into *pytester*'s directory and run one inner session under *plugins*.

    The inner session gets otto's library floor first, as ``otto test`` does
    when it installs logging. Under ``log_cli_level=DEBUG`` alone, aiosqlite's
    worker thread logs at DEBUG after an archive's ``close()`` has returned,
    and that line can still be inside the inner live-log handler when inner
    teardown closes its capture (#613). The root conftest hands the floored
    loggers back after the test.
    """
    from otto.logger import management

    management.apply_library_levels()
    pytester.makepyfile(**files)
    return pytester.runpytest_inprocess(*INNER_ARGS, plugins=plugins)
