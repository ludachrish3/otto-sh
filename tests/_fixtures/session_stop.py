"""The shell route's cancel stop as it was before it stopped anything, for controls."""


async def old_mark_only(self, *, restore, disposable=False):
    """The cancel arm as it was: mark the session for the next use, write nothing.

    Patched over ``ShellSession._stop_interrupted_command`` by the controls
    that prove a check can see a command the stop left running.
    """
    self._needs_recovery = True
