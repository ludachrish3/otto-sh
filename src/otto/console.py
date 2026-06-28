"""Shared Rich Console instance for the Otto application."""

import contextlib
import os

from rich.console import Console

CONSOLE = Console()

# Rich re-probes fds 0/1/2 for the terminal width on every render. An
# in-process pytest.main() run (otto test) leaves those descriptors in a
# non-tty state, so the probe fails and the console silently collapses to
# 80 columns mid-command. Pin the size now, while stdout is still the real
# terminal; if stdout isn't a tty (piped/redirected) leave Rich to auto-detect.
with contextlib.suppress(OSError):
    CONSOLE.size = os.get_terminal_size()
