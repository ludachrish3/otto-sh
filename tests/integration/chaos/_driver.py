"""Spawn a real ``otto`` subprocess and observe it: logs, stderr, signals, exit.

Phase markers, not timing (chaos spec, Tier 2): callers wait for a line in
the run's ``verbose.log`` (the file sink flushes per record, but a
QueueListener thread sits between ``logger.*()`` and the file — hence
polling) or for the interrupt banner on stderr (an unbuffered direct
write from ``_on_signal``), then deliver the signal.

Third-signal behavior is deliberately not exercised anywhere in this
suite: after ``_main`` removes its handlers, a third SIGINT during
``asyncio.run`` finalization surfaces as a bare KeyboardInterrupt (SIGTERM:
SIG_DFL kill) — a timing window no marker can gate on.
"""

import dataclasses
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ._target import ChaosTarget

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OTTO_BIN = Path(sys.executable).parent / "otto"
_COVERAGERC = PROJECT_ROOT / ".coveragerc"
_COV_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

BANNER = "cleaning up remote sessions"
_POLL = 0.05


def _otto_env(
    xdir: Path, target: ChaosTarget, extra_env: "dict[str, str] | None"
) -> "dict[str, str]":
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "OTTO_XDIR": str(xdir),
        "OTTO_SUT_DIRS": str(target.sut_dir),
        "COVERAGE_PROCESS_START": str(_COVERAGERC),
        "PYTHONPATH": f"{_COV_BOOTSTRAP}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    if extra_env:
        env.update(extra_env)
    return env


@dataclasses.dataclass
class OttoProc:
    proc: "subprocess.Popen[bytes]"
    xdir: Path
    stdout_path: Path
    stderr_path: Path

    @property
    def pid(self) -> int:
        return self.proc.pid

    def signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def wait(self, timeout: float) -> int:
        return self.proc.wait(timeout=timeout)

    def stdout_text(self) -> str:
        return self.stdout_path.read_text(errors="replace")

    def stderr_text(self) -> str:
        return self.stderr_path.read_text(errors="replace")

    def _wait_for(self, read: "Callable[[], str]", pattern: str, timeout: float, what: str) -> str:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = read()
            m = rx.search(text)
            if m:
                return m.group(0)
            if self.proc.poll() is not None:
                # One last read: the process may have flushed on exit.
                text = read()
                m = rx.search(text)
                if m:
                    return m.group(0)
                raise AssertionError(
                    f"otto exited (rc={self.proc.returncode}) before {what} matched {pattern!r}.\n"
                    f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
                )
            time.sleep(_POLL)
        raise AssertionError(
            f"{what} never matched {pattern!r} within {timeout}s.\n"
            f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
        )

    def wait_for_stderr(self, pattern: str, timeout: float) -> str:
        return self._wait_for(self.stderr_text, pattern, timeout, "stderr")

    def _log_text(self) -> str:
        return "\n".join(
            p.read_text(errors="replace") for p in sorted(self.xdir.rglob("verbose.log"))
        )

    def wait_for_log(self, pattern: str, timeout: float) -> str:
        return self._wait_for(self._log_text, pattern, timeout, "verbose.log")

    def assert_no_process_group(self) -> None:
        """After exit, nothing may remain in otto's group (start_new_session ⇒ pgid == pid)."""
        assert self.proc.poll() is not None, "call wait() before assert_no_process_group()"
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return
        raise AssertionError(
            f"orphaned local children remain in otto's process group (pgid {self.pid})"
        )


def spawn_otto(
    argv: "list[str]",
    *,
    xdir: Path,
    target: ChaosTarget,
    extra_env: "dict[str, str] | None" = None,
) -> OttoProc:
    """Start ``otto -l <lab> --log-level DEBUG *argv`` with stdout/stderr to files."""
    stdout_path = xdir / "otto-stdout.txt"
    stderr_path = xdir / "otto-stderr.txt"
    cmd = [str(OTTO_BIN), "-l", target.lab, "--log-level", "DEBUG", *argv]
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            cwd=PROJECT_ROOT,
            env=_otto_env(xdir, target, extra_env),
            start_new_session=True,
        )
    return OttoProc(proc=proc, xdir=xdir, stdout_path=stdout_path, stderr_path=stderr_path)
