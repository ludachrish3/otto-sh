"""``_history_restored`` must put the bed's history back, and say so when it can't.

The shell-history e2e module writes to a shared bed's ``~/.bash_history`` on
purpose and relies on this helper to undo it byte-for-byte. Two ways it could
quietly do harm are pinned here:

- a muddled answer to "does the file exist?" or a failed backup must stop the
  test BEFORE anything writes, because either would make the restore delete or
  overwrite the real file;
- a bad restore must always be reported -- failing the test when the body
  passed, and riding on the body's own exception (never replacing it) when the
  body failed.

The e2e module is bed-only; the helper is control flow over an injected host,
so it is tested here, on every hostless lane, against a stub that keeps the
"file" in memory.
"""

import logging
import sys

import pytest

from otto.utils import Status
from tests.e2e.host import test_shell_history_e2e as hist


class _Result:
    def __init__(self, value: str, status: Status = Status.Success) -> None:
        self.value = value
        self.status = status


class _FakeBed:
    """One history file and its backups, answered over a stubbed ``exec``.

    The "digest" of a file is its content: enough for the helper, which only
    compares digests for equality.
    """

    def __init__(
        self,
        history: str | None = "original",
        existed_answer: str | None = None,
        backup_fails: bool = False,
        restore: str = "works",
    ) -> None:
        self.history = history
        self.backups: dict[str, str] = {}
        self.existed_answer = existed_answer
        self.backup_fails = backup_fails
        self.restore = restore
        self.commands: list[str] = []

    async def exec(self, command: str) -> _Result:
        self.commands.append(command)
        if command.startswith("cat ~/.bash_history"):
            return _Result(self.history if self.history is not None else "<none>")
        if command.startswith("test -f"):
            if self.existed_answer is not None:
                return _Result(self.existed_answer)
            return _Result("yes" if self.history is not None else "no")
        if command.startswith("cp ~/.bash_history "):
            if self.backup_fails:
                return _Result("cp: No space left on device", Status.Failed)
            backup = command.split()[2]
            assert self.history is not None
            self.backups[backup] = self.history
            return _Result(self.history)
        if " ~/.bash_history; rm -f " in command or command.startswith("rm -f ~/.bash_history"):
            if self.restore == "raises":
                raise ConnectionError("connection lost")
            if self.restore == "corrupts":
                self.history = "corrupted"
            elif command.startswith("cp "):
                self.history = self.backups.pop(command.split()[1])
            else:
                self.history = None
            return _Result("")
        raise AssertionError(f"unexpected command: {command}")


@pytest.mark.asyncio
async def test_a_good_restore_puts_the_file_back_exactly() -> None:
    bed = _FakeBed()

    async with hist._history_restored(bed, "test1") as before:
        bed.history = "original + otto's commands"

    assert before == "original"
    assert bed.history == "original"


@pytest.mark.asyncio
async def test_a_bad_restore_fails_a_passing_body() -> None:
    bed = _FakeBed(restore="corrupts")

    with pytest.raises(AssertionError, match="failed to restore"):
        async with hist._history_restored(bed, "test1"):
            pass


def _notes_or_log(exc: BaseException, caplog: pytest.LogCaptureFixture) -> str:
    """Where :func:`hist._note_on` put its text on this interpreter."""
    if sys.version_info >= (3, 11):
        return "\n".join(getattr(exc, "__notes__", []))
    return caplog.text


@pytest.mark.asyncio
async def test_a_bad_restore_rides_on_a_failing_body_without_replacing_it(caplog) -> None:
    bed = _FakeBed(restore="corrupts")
    caplog.set_level(logging.ERROR)

    with pytest.raises(ValueError, match="the body's own failure") as raised:
        async with hist._history_restored(bed, "test1"):
            raise ValueError("the body's own failure")

    assert "failed to restore" in _notes_or_log(raised.value, caplog)


@pytest.mark.asyncio
async def test_a_restore_that_raises_never_replaces_the_body_failure(caplog) -> None:
    bed = _FakeBed(restore="raises")
    caplog.set_level(logging.ERROR)

    with pytest.raises(ValueError, match="the body's own failure") as raised:
        async with hist._history_restored(bed, "test1"):
            raise ValueError("the body's own failure")

    assert "connection lost" in _notes_or_log(raised.value, caplog)


@pytest.mark.asyncio
async def test_an_unclear_existence_answer_stops_before_anything_writes() -> None:
    """Neither "yes" nor "no" would otherwise pick the `rm` restore and delete the file."""
    bed = _FakeBed(existed_answer="bash: test: command not found")
    ran = False

    with pytest.raises(AssertionError, match="could not tell whether"):
        async with hist._history_restored(bed, "test1"):
            ran = True

    assert not ran
    assert bed.history == "original"
    assert not any(c.startswith("rm -f ~/.bash_history") for c in bed.commands)


@pytest.mark.asyncio
async def test_a_failed_backup_stops_before_anything_writes() -> None:
    """Without a good backup the restore would copy nothing over the real file."""
    bed = _FakeBed(backup_fails=True)
    ran = False

    with pytest.raises(AssertionError, match="could not back up"):
        async with hist._history_restored(bed, "test1"):
            ran = True

    assert not ran
    assert bed.history == "original"
    assert not any(c.startswith("cp /tmp/otto-history-backup-") for c in bed.commands)
