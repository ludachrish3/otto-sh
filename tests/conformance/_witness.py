"""Whether a process a contract's command started is still running: one observer, one cleanup.

The witness is DEF-WIT's (:mod:`tests._fixtures.witness_shell`): the stimulus
writes ``<pid> <starttime>`` to :attr:`Witness.path`, and the observer judges
that identity, so a reused pid never reads as the recorded process.

:func:`witness` chooses the observer ONCE, at entry, from where the cell's
host runs its commands:

* a hermetic cell (:data:`~tests.conformance._cells.RUNNER_LOCAL_KINDS`) runs
  them on the runner, so the hermetic helpers read the runner's ``/proc``
  (:func:`~tests._fixtures.witness_shell.observe`,
  :func:`~tests._fixtures.witness_shell.clean`);
* a bed cell on ``ssh`` or ``telnet`` opens ONE second host with
  :func:`~tests.conformance._entry.opened` for the context's life and runs the
  vocabulary's ``process_state_template`` through its ``run``, so the read
  never shares the session under test;
* a bed cell on ``console`` runs it through *host*'s own ``run``: the line
  takes one client, and a timeout's stop is inline, so the session is
  recovered by the time the timed-out call returns.

The cleanup goes through the same observer at exit, also on failure:
``witness_cleanup_template`` (or ``clean``) kills a running witness's process
and removes the file.
"""

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal

from otto.host.host import BaseHost
from otto.utils import WaitTimeoutError, wait_for_async
from tests._fixtures import witness_shell
from tests.conformance._cells import RUNNER_LOCAL_KINDS
from tests.conformance._entry import opened
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance._vocabulary import Vocabulary

logger = logging.getLogger(__name__)

_START_WAIT_S = 5.0
"""How long :meth:`Witness.state` waits for a stimulus to write its witness."""

_CONSOLE = "console"


@dataclasses.dataclass(frozen=True)
class Witness:
    """One witness file on the cell's host, and the one way to read it."""

    path: str
    """``/tmp/otto-cf-<purpose>-<uuid4 hex>`` on the host that runs the command."""

    label: str
    """The cell the witness is on, named in every failure."""

    _observe: "Callable[[], Awaitable[str]]"

    async def state(self) -> Literal["running", "stopped"]:
        """The witnessed process's state; raises for a missing witness or a failed observation.

        Waits up to :data:`_START_WAIT_S` for the witness to be written, so a
        background stimulus that has not written yet does not read as
        missing. Neither failure is ever reported as ``"stopped"``.
        """
        seen = witness_shell.NO_WITNESS

        async def written() -> bool:
            nonlocal seen
            seen = await self._observe()
            return seen != witness_shell.NO_WITNESS

        try:
            await wait_for_async(written, _START_WAIT_S, on_timeout="no witness")
        except WaitTimeoutError:
            raise AssertionError(
                f"no start witness: {self.label}: nothing was written to {self.path} "
                f"in {_START_WAIT_S}s"
            ) from None
        if seen == witness_shell.RUNNING:
            return "running"
        if seen == witness_shell.STOPPED:
            return "stopped"
        raise AssertionError(f"observation failed: {self.label}: {self.path} answered {seen!r}")


def _answer(value: str) -> str:
    """The one answer a remote observer printed, or the whole value when it is not one."""
    found = witness_shell.ANSWER_PATTERN.fullmatch(value.strip())
    return found.group(0) if found else value


async def _run_template(runner: BaseHost, template: str, path: str) -> str:
    """Run a ``{path}`` witness *template* through *runner*'s ``run``; its answer."""
    return _answer((await runner.run(template.format(path=path))).only.value)


@contextlib.asynccontextmanager
async def _remote_runner(resolved: ResolvedCell, host: BaseHost) -> "AsyncIterator[BaseHost]":
    """The host the remote observer runs on: *host* on a console, else one second host."""
    if resolved.cell.term == _CONSOLE:
        yield host
        return
    async with opened(resolved) as second:
        yield second


@contextlib.asynccontextmanager
async def _cleaned_up(cleanup: "Callable[[], Awaitable[None]]", path: str) -> "AsyncIterator[None]":
    """Run *cleanup* after the body; a cleanup error never replaces the body's own failure.

    When the body raised, a failing cleanup is logged and the body's error
    propagates; when the body succeeded, the cleanup's error is raised.
    """
    try:
        yield
    except BaseException:
        try:
            await cleanup()
        except Exception:
            logger.exception("witness cleanup of %s failed after the body failed", path)
        raise
    await cleanup()


@contextlib.asynccontextmanager
async def witness(
    resolved: ResolvedCell, host: BaseHost, *, purpose: str = "timeout"
) -> "AsyncIterator[Witness]":
    """One witness on *resolved*'s host, observed and cleaned up by the one observer it picks."""
    path = f"/tmp/otto-cf-{purpose}-{uuid.uuid4().hex}"
    if resolved.kind in RUNNER_LOCAL_KINDS:

        async def observe_here() -> str:
            return witness_shell.observe(path)

        async def clean_here() -> None:
            witness_shell.clean(path)

        async with _cleaned_up(clean_here, path):
            yield Witness(path, cell_label(resolved), observe_here)
        return

    words = resolved.vocabulary
    state, cleanup = words.process_state_template, words.witness_cleanup_template
    if state is None or cleanup is None:
        raise RuntimeError(
            f"{cell_label(resolved)}: its vocabulary has no witness observer, so no "
            f"contract on it may start a witnessed process"
        )
    async with _remote_runner(resolved, host) as runner:

        async def observe_remote() -> str:
            return await _run_template(runner, state, path)

        async def clean_remote() -> None:
            await _run_template(runner, cleanup, path)

        async with _cleaned_up(clean_remote, path):
            yield Witness(path, cell_label(resolved), observe_remote)


def fake_witnesses(words: Vocabulary) -> witness_shell.FakeWitnesses:
    """*words*' witness lines answered in memory: the one constructor every fake host calls."""
    return witness_shell.FakeWitnesses(
        long_running=words.pid_long_running_template,
        exec_long_running=words.exec_pid_long_running_template,
        running=words.witness_running_template,
        finished=words.witness_finished_template,
        observer=words.process_state_template,
        cleanup=words.witness_cleanup_template,
    )
