"""tests/_fixtures/process_group.py: a hung group is aborted children first."""

import sys
import textwrap
from pathlib import Path

from otto.utils import wait_for
from scripts.process_group import signal_group
from tests._fixtures.process_group import abort_group_in_turn, start_dumping_group

# A parent and the child it started, as an xdist controller and its worker,
# each waiting in a function named for which it is. The test pins the order
# they are aborted in, not the wake-up a closed pipe would cause.
_TREE = textwrap.dedent("""
    import subprocess, sys, time
    from pathlib import Path

    CHILD = '''
    import sys, time
    from pathlib import Path

    def the_child_waits():
        Path(sys.argv[1]).touch()
        time.sleep(600)

    the_child_waits()
    '''

    def the_parent_waits():
        subprocess.Popen([sys.executable, "-c", CHILD, sys.argv[1] + ".child"])
        Path(sys.argv[1]).touch()
        time.sleep(600)

    the_parent_waits()
""")


def test_a_child_dumps_while_its_parent_still_lives(tmp_path: Path) -> None:
    """The child is aborted before its parent, so no peer's death wakes it mid-dump.

    A parent aborted first closes its end of their pipe, and a thread the child
    has blocked on it (execnet's receiver, in an xdist worker) wakes and runs
    while faulthandler walks its frames: a dump that can segfault cut short (#852).
    """
    started = tmp_path / "started"
    out = tmp_path / "tree.out"
    with out.open("wb") as sink:
        proc = start_dumping_group(
            [sys.executable, "-c", _TREE, str(started)], env={}, cwd=tmp_path, sink=sink
        )
    try:
        wait_for(
            lambda: started.exists() and Path(f"{started}.child").exists(),
            30,
            on_timeout="the tree never started",
        )
        assert abort_group_in_turn(proc.pid) == []
        proc.wait()
        dumps = out.read_text()
        assert "in the_child_waits" in dumps
        assert "in the_parent_waits" in dumps
        assert dumps.index("in the_child_waits") < dumps.index("in the_parent_waits")
    finally:
        if proc.returncode is None:  # once reaped, its pgid may name another group
            signal_group(proc.pid)
            proc.wait()
