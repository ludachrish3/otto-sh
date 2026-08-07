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
from collections.abc import Callable
from pathlib import Path

from otto.utils import wait_for
from tests.e2e._otto_subprocess import OTTO_BIN, PROJECT_ROOT, otto_subprocess_env

from ._target import ChaosTarget

BANNER = "cleaning up remote sessions"
_POLL = 0.05


def _otto_env(
    xdir: Path, target: ChaosTarget, extra_env: "dict[str, str] | None"
) -> "dict[str, str]":
    """The canonical otto child env, pointed at this chaos target's SUT repo.

    TERM is inherited so the child sees the same terminal type the suite runs
    under (the root conftest pins it to ``dumb``).
    """
    return otto_subprocess_env(
        xdir=xdir,
        sut_dirs=target.sut_dir,
        term=os.environ.get("TERM", "xterm-256color"),
        extra_env=extra_env,
    )


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
        text = ""
        found = ""

        def matched() -> bool:
            nonlocal text, found
            text = read()
            m = rx.search(text)
            if m:
                found = m.group(0)
                return True
            if self.proc.poll() is not None:
                # One last read: the process may have flushed on exit.
                text = read()
                m = rx.search(text)
                if m:
                    found = m.group(0)
                    return True
                raise AssertionError(
                    f"otto exited (rc={self.proc.returncode}) before {what} matched {pattern!r}.\n"
                    f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
                )
            return False

        wait_for(
            matched,
            timeout,
            interval=_POLL,
            on_timeout=lambda: (
                f"{what} never matched {pattern!r} within {timeout}s.\n"
                f"--- stderr ---\n{self.stderr_text()}\n--- {what} ---\n{text}"
            ),
        )
        return found

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
