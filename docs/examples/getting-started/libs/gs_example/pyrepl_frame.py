"""A command frame for the ``python3`` REPL — the page's stand-in for a local application."""

# doc: begin pyrepl-frame
import re

from otto.host.command_frame import CommandFrame, SessionMarkers

_HELPER = """
def __otto(cmd, begin, end):
    print(begin)
    rc = 0
    try:
        try:
            r = eval(cmd, globals())
            if r is not None:
                print(r)
        except SyntaxError:
            exec(cmd, globals())
    except SystemExit:
        raise
    except BaseException as e:
        print(e)
        rc = 1
    print(f"{end}{rc}__")

"""


class PyReplFrame(CommandFrame):
    """One REPL line per command; exit code 0 unless the line raised.

    The handshake silences ``sys.ps1``/``sys.ps2`` (so prompts never reach
    the parser), defines the helper once (idempotent — the readiness probe
    is resent until it lands), and prints the READY token. Single-line
    commands only: this is an example frame, not a Python driver.
    """

    type_name = "pyrepl"
    streams_output_live = False

    def handshake(self, m: SessionMarkers) -> str:
        """Silence the prompts, define the helper, then emit the readiness token."""
        return f"import sys; sys.ps1 = ''; sys.ps2 = ''\n{_HELPER}print({m.ready!r})\n"

    def frame(self, cmd: str, m: SessionMarkers) -> str:
        """Hand ``cmd`` to the helper as one REPL line, with the sentinels to print."""
        return f"__otto({cmd!r}, {m.begin!r}, {m.end_prefix!r})\n"

    def recover(self, m: SessionMarkers) -> str:
        """Re-synchronization probe the REPL must *execute* to satisfy."""
        # Echo-proof: the echoed text carries `+ str(0) +`, never the digit form.
        return f"print({m.recover!r} + str(0) + '__')\n"

    def recover_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        """Match only the executed form of :meth:`recover` — token, digits, ``__``."""
        return re.compile(re.escape(m.recover) + r"(\d+)__")

    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        """Match the helper's closing line, capturing the exit code it carries."""
        return re.compile(re.escape(m.end_prefix) + r"(\d+)__")

    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        """Report whether ``data`` is the chunk carrying the BEGIN sentinel."""
        stripped = data.rstrip("\r\n")
        return stripped == m.begin or stripped.endswith(m.begin)

    def parse_output(
        self,
        buffer: str,
        cmd: str,  # noqa: ARG002 -- the base class names it; the override must match
        m: SessionMarkers,
    ) -> str:
        """Return the text the command printed, between the two sentinels."""
        begin = buffer.rfind(m.begin)  # the LAST marker skips an echoed frame
        start = begin + len(m.begin) if begin != -1 else 0
        while start < len(buffer) and buffer[start] in ("\r", "\n"):
            start += 1
        end_match = self.end_pattern(m).search(buffer, start)
        end = end_match.start() if end_match else len(buffer)
        return buffer[start:end].rstrip("\r\n").replace("\r", "")

    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        """Return the code the helper printed, or ``-1`` when the line never closed."""
        match = self.end_pattern(m).search(buffer)
        return int(match.group(1)) if match else -1

    def restore_interactive(self) -> str | None:
        """Put the prompts the handshake silenced back, before a human gets the shell."""
        return "import sys; sys.ps1 = '>>> '; sys.ps2 = '... '"


# doc: end pyrepl-frame
