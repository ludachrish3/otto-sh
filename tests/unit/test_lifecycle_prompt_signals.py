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
        with out.open("wb") as sink:
            # stdin is a pipe nobody writes: the prompt waits for ever.
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.PIPE,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_for(
                    lambda out=out: "PROMPTING" in out.read_text(),
                    30,
                    interval=0.01,
                    on_timeout=lambda out=out: (
                        f"child never reached its prompt:\n{out.read_text()}"
                    ),
                )
                proc.send_signal(signum)
                proc.wait(timeout=30)  # exits with the prompt's thread still reading
            finally:
                if proc.poll() is None:
                    proc.kill()
                if proc.stdin is not None:
                    proc.stdin.close()
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
        assert proc.returncode == 0, text  # the child caught SystemExit itself
