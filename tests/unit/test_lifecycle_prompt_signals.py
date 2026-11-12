"""interruptible_prompt (#1003): a real signal at a real prompt, in a child process.

The tier-1 tests in tests/unit/test_lifecycle.py deliver the interrupt the way
that module always does, by calling ``_CommandRun._on_signal`` on the loop.
This proves the real path in a fresh interpreter: real handlers installed by a
real ``run_command``, a real ``typer.prompt`` waiting on a stdin that never
answers, and a real signal sent by PID the moment the child says it is about
to prompt. The signal is NOT held back until the read has started: a press
landing before it, while the prompt thread is still starting, is exactly the
case a signal handler raising out of the read would lose (the prompt would
then wait forever). Here the loop's own wakeup fd carries every delivery.

It also pins that ONE press is ONE delivery (the loop sees it once; a double
count would force the teardown), and that the process EXITS with the prompt's
thread still blocked in its read. The pty e2e tests cannot see the first
property, because a forced and a graceful interrupt both exit 128 + signum.

Placement follows tests/unit/test_lifecycle_sync_phase.py: real delivery
belongs to a subprocess, and this one is fast and bed-free, so every unit run
exercises it.
"""

import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from otto.utils import wait_for

_CHILD = r"""
import json, sys
import typer
from otto import lifecycle

ctrl = lifecycle._CommandRun(teardown_deadline=30.0)
loop_deliveries = []


def counting(signum):
    loop_deliveries.append(signum)
    lifecycle._CommandRun._on_signal(ctrl, signum)


ctrl._on_signal = counting  # what _main hands add_signal_handler
seen = {}


async def body():
    print("PROMPTING", flush=True)
    seen["answer"] = await lifecycle.interruptible_prompt(typer.prompt, "Name")


try:
    lifecycle.run_command(body(), _controller=ctrl)
except SystemExit as e:
    seen["code"] = e.code
seen["interrupted"] = ctrl.interrupted
seen["forced"] = ctrl.forced
seen["loop_deliveries"] = loop_deliveries
print("RESULT " + json.dumps(seen), flush=True)
"""

_REPEATS = 5
"""Each run signals at a different point of the prompt's start-up."""


def _signal_at_prompt(script: Path, log: Path, signum: int, *, stdout: "int | None" = None) -> int:
    """Run *script*, send *signum* once it logs PROMPTING, and return its exit status.

    stdin is a pipe nobody writes, so the prompt waits for ever. The child's
    stderr (and its stdout too, unless *stdout* says otherwise) goes to *log*.
    """
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=sink if stdout is None else stdout,
            stderr=sink,
        )
        try:
            wait_for(
                lambda: "PROMPTING" in log.read_text(),
                30,
                interval=0.01,
                on_timeout=lambda: f"child never reached its prompt:\n{log.read_text()}",
            )
            proc.send_signal(signum)
            return proc.wait(timeout=30)  # exits with the prompt's thread still reading
        finally:
            if proc.poll() is None:
                proc.kill()
            if proc.stdin is not None:
                proc.stdin.close()


@pytest.mark.parametrize(
    ("signum", "expected_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_one_signal_at_a_prompt_is_one_graceful_interrupt_and_the_process_exits(
    tmp_path: Path, signum: int, expected_code: int
) -> None:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    for attempt in range(_REPEATS):
        out = tmp_path / f"out{attempt}.txt"
        returncode = _signal_at_prompt(script, out, signum)
        text = out.read_text()
        # Not at a line start: the prompt line it interrupted never ended.
        found = re.search(r"RESULT (\{.*\})", text)
        assert found, text
        seen = json.loads(found.group(1))

        assert "answer" not in seen, text
        assert seen["code"] == expected_code, text
        assert seen["interrupted"] == signum, text
        assert seen["forced"] is False, text
        assert seen["loop_deliveries"] == [signum], text
        assert returncode == 0, text  # the child caught SystemExit itself


_SLOW_ECHO_CHILD = r"""
import atexit, functools, io, os, sys, threading, time
import typer
from otto import lifecycle

# The CI failure (#1026). Python flushes stdout (waiting for any write in
# progress) when the script ends, then runs atexit, then shuts down. A prompt
# thread that only STARTS its write after that flush, and is still inside it
# at shutdown, holds the BufferedWriter's lock while shutdown flushes stdout:
# CPython waits 1 s for it, then aborts. On a loaded runner the thread is
# simply slow. Here every step is ordered by an event, none by a sleep:
#   - the prompt starts writing only once the command can no longer race it:
#     waiting for quiet in _PromptHandoff.abandon (#1026's fix), or already
#     in atexit, past the script-end flush (435e90578, which does not wait);
#   - a command that waits lets the write finish at once, and exits after it;
#   - one that does not finds the write still holding stdout's lock when it
#     shuts down: the write only ends if the command waits, and atexit returns
#     only once the write has started. Shutdown then aborts.

may_write = threading.Event()
in_write = threading.Event()
waiting = threading.Event()  # the command waits for the prompt to be quiet


class SlowRaw(io.RawIOBase):
    first = True

    def writable(self):
        return True

    def write(self, data):
        if threading.current_thread().name == "otto-prompt" and SlowRaw.first:
            SlowRaw.first = False
            os.write(2, b"ECHOING\n")
            in_write.set()
            waiting.wait(30)  # never set without the fix: shutdown freezes us here
        return os.write(1, data)


sys.stdout = io.TextIOWrapper(io.BufferedWriter(SlowRaw()), write_through=False)


def at_exit():
    may_write.set()
    in_write.wait(30)


atexit.register(at_exit)

handoff = getattr(lifecycle, "_PromptHandoff", None)
if handoff is not None:
    waiting_for_quiet = handoff.abandon

    async def abandon(self):
        waiting.set()
        may_write.set()
        await waiting_for_quiet(self)

    handoff.abandon = abandon


@functools.wraps(typer.prompt)  # keeps typer's module, whose reader is swapped
def late_prompt(*args, **kwargs):
    may_write.wait()
    return typer.prompt(*args, **kwargs)


ctrl = lifecycle._CommandRun(teardown_deadline=30.0)


async def body():
    os.write(2, b"PROMPTING\n")
    await lifecycle.interruptible_prompt(late_prompt, "Name")


try:
    lifecycle.run_command(body(), _controller=ctrl)
except SystemExit as e:
    os.write(2, f"CODE {e.code}\n".encode())
"""


def test_an_interrupt_while_the_prompt_writes_its_question_exits_cleanly(tmp_path: Path) -> None:
    """The command waits for the question to be written before it exits (#1026).

    A daemon thread still inside a stdout write at interpreter shutdown holds
    the BufferedWriter's lock, and CPython aborts the exit with ``Fatal Python
    error: _enter_buffered_busy`` (exit -6) when shutdown flushes stdout. On a
    loaded CI runner the prompt thread is that slow; the child puts its write
    in that window (see its comments).
    """
    script = tmp_path / "child.py"
    script.write_text(_SLOW_ECHO_CHILD)
    log = tmp_path / "err.txt"
    returncode = _signal_at_prompt(script, log, signal.SIGTERM, stdout=subprocess.DEVNULL)
    text = log.read_text()
    assert "ECHOING" in text, text  # the write really happened after the interrupt
    assert "Fatal Python error" not in text, text
    assert "CODE 143" in text, text
    assert returncode == 0, text
