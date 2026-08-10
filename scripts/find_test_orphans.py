#!/usr/bin/env python3
"""List local daemons stranded by a test run, so a stopped gate can be checked.

Why this exists rather than a one-liner: the obvious one-liner is wrong. The
natural reach is ``pgrep -f "sshd .*pytest"``, which matches **the shell
command containing that pattern** — your own probe, and anything else quoting
it. Run interactively it reports itself and reads as a hit. Worse, the check
that gets used after stopping a gate run is usually ``pgrep -f "nox|pytest"``,
which cannot match a stranded daemon at all: a listening sshd's argv is
``sshd: /usr/sbin/sshd -D -e -f …``, so it matches on the *path* of its config
file, not on being a test process.

Both failure modes are real. An sshd from ``popen-gw3/chaos0`` lived on the dev
VM for two days across two "verified no orphans" checks that could not have
seen it either way.

So this keys on the process NAME (``comm``) and requires a pytest tmp path in
the arguments — a shell quoting the same string has ``comm`` of ``bash`` and is
never a hit.

Daemons spawned by the tier-2 chaos fixture now carry ``PR_SET_PDEATHSIG``
(see ``tests/integration/chaos/_sshd.py``), so the kernel reaps them when the
worker is killed and this should print nothing. It stays because that guard
covers one spawner: anything else that starts a long-lived child from a test is
still exposed, and a probe nobody can run is how the first one survived.

Usage::

    python scripts/find_test_orphans.py           # list, exit 1 if any found
    python scripts/find_test_orphans.py --quiet   # exit status only
"""

import argparse
import shutil
import subprocess
import sys

# Process names worth checking for. Keyed on `comm`, which is the executable's
# name — not a substring of somebody's command line.
_DAEMON_COMMS = frozenset({"sshd", "nc", "socat", "ncat", "telnetd", "vsftpd", "python3"})

# A hit also has to reference a pytest-owned temp directory. `python3` is in
# the list above because a test helper can daemonise as one, but it needs this
# second condition or every interpreter on the box matches.
_TEST_PATH_MARKERS = ("pytest-of-", "/pytest-")


def find_orphans() -> "list[tuple[int, str, str]]":
    """Return ``(pid, comm, args)`` for every live test-spawned daemon."""
    ps = shutil.which("ps") or "/bin/ps"
    out = subprocess.run(  # noqa: S603 — resolved `ps` with a fixed argv, no shell
        [ps, "-eo", "pid=,comm=,args="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:  # noqa: PLR2004 — pid, comm, args
            continue
        pid, comm, args = parts
        if comm not in _DAEMON_COMMS:
            continue
        if not any(marker in args for marker in _TEST_PATH_MARKERS):
            continue
        found.append((int(pid), comm, args))
    return found


def main() -> int:
    """Print any stranded test daemons; exit non-zero when some are found."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="exit status only")
    args = parser.parse_args()

    orphans = find_orphans()
    if not args.quiet:
        if not orphans:
            print("no test-spawned daemons running")
        else:
            print(f"{len(orphans)} test-spawned daemon(s) still running:")
            for pid, comm, argv in orphans:
                print(f"  {pid:>8}  {comm:<8}  {argv[:110]}")
            # Deliberately not offering to kill them: a live run has daemons
            # too, and this cannot tell those apart from strays. Kill by the
            # PIDs printed above, never by pattern.
            print("\nIf no test run is in progress, kill these by PID.")
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
