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


class FailingHost(RecordingHost):
    async def _close(self):
        await super()._close()
        raise RuntimeError("boom")


class SlowHost(RecordingHost):
    async def _close(self):
        try:
            await asyncio.sleep(30)
        finally:  # cancelled by the deadline: still release the local shell
            await LocalHost._close(self)
'''
"""Source of a ``fakehost`` module: ``LocalHost`` doubles that log and record each close.

A test writes it with ``pytester.makepyfile(fakehost=FAKE_HOSTS)``. The hosts
claim the running loop on first use, as every host does, so they register
with that loop with no test wiring. ``CLOSED`` gets ``(host id, id(loop),
loop closed?)`` for every close.
"""


def run_inner(pytester, plugins, **files):
    """Write *files* into *pytester*'s directory and run one inner session under *plugins*."""
    pytester.makepyfile(**files)
    return pytester.runpytest_inprocess(*INNER_ARGS, plugins=plugins)
