"""Stub programs that record their argv, to prove a command hands a value over as one inert word.

A builder's command runs for real under ``sh -c``, with ``PATH`` led by a
directory of stubs. Each recording stub writes the argv it was given to its
own file under the test's ``tmp_path`` and exits at once, so nothing is
contacted and nothing waits on a timer; the test then reads back exactly
which words each program saw. A pass-through stub (``setsid``) runs the rest
of its argv in its own place, so the command after it is still observed.

The hostile values each carry a ``touch`` of a marker under ``tmp_path``: a
shell that expanded one would create the marker, so its absence is the
second half of every proof.
"""

import os
import shutil
import subprocess
from pathlib import Path

_RECORD = '#!/bin/sh\nf=$(mktemp "$ARGV_STUB_DIR/call.XXXXXX")\nprintf \'%s\\0\' "$0" "$@" > "$f"\n'
_PASS_THROUGH = '#!/bin/sh\nexec "$@"\n'


def hostile_values(marker: Path) -> list[str]:
    """Addresses that run ``touch <marker>`` if a shell splices them in unquoted."""
    return [
        f"1.2.3.4'; touch {marker}; '",
        f"$(touch {marker})",
        f"`touch {marker}`",
    ]


def stub_env(tmp_path: Path, record: list[str], *, pass_through: "list[str] | None" = None) -> dict:
    """Return an environment whose ``PATH`` finds the stubs first, then the real tools."""
    bindir = tmp_path / "stub-bin"
    calls = tmp_path / "stub-calls"
    bindir.mkdir()
    calls.mkdir()
    for name, text in [
        *((name, _RECORD) for name in record),
        *((name, _PASS_THROUGH) for name in pass_through or []),
    ]:
        stub = bindir / name
        stub.write_text(text)
        stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["ARGV_STUB_DIR"] = str(calls)
    return env


def run_sh(command: str, env: dict) -> "subprocess.CompletedProcess[str]":
    """Run *command* the way a host does, ``sh -c <command>``; the timeout is a hang guard only."""
    sh = shutil.which("sh")
    assert sh is not None, "sh not on PATH"
    return subprocess.run(
        [sh, "-c", command], capture_output=True, text=True, env=env, timeout=60, check=False
    )


def recorded(tmp_path: Path, program: str) -> list[list[str]]:
    """Every argv a recording stub named *program* received, argv[0] reduced to its basename.

    Order between calls is not kept; a test that needs one call asserts there was one.
    """
    calls = []
    for path in (tmp_path / "stub-calls").iterdir():
        argv = path.read_bytes().split(b"\0")[:-1]
        words = [word.decode() for word in argv]
        words[0] = Path(words[0]).name
        if words[0] == program:
            calls.append(words)
    return calls


SOCAT_REFUSAL = r"is not a valid IP address|has scope ID"
"""What a socat sink says when it refuses an address (``otto.host.shell_quote.socat_host``)."""

SOCAT_REFUSED = [
    *hostile_values(Path("pwned")),
    "1.2.3.4,fork",
    "evil:80",
    "a!!b",
    "a b",
    "[x]",
    "fe80::1%eth0;id",
    "",
]
"""Values every socat sink refuses: shell syntax, socat's own separators, a bad scope ID.

Nothing runs with them, so the marker path in the shell payloads is never touched."""
