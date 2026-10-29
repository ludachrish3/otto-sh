"""The inner session's stdout names the JUnit file where it ends up.

A run writes its JUnit file under a temporary name. pytest's own "generated
xml file" banner already names the final file (``JUnitPromotion`` moves it
first; see ``test_run_sessions``), so the stream only swaps the final path into
any line that still names the temporary one, and drops such a line when the
file is not kept.
"""

import io

from otto.suite.run import _SessionStdout

_FINAL = "/tmp/otto-gs/xdir/otto-output/test/20261025_101112_123/junit.xml"
_PARTIAL = "/tmp/otto-gs/xdir/otto-output/test/20261025_101112_123/.junit.xml.123456.partial"


def _through_stream(text: str, *, kept: bool = True) -> str:
    """What reaches the real stream when pytest writes *text* as ``line`` does."""
    real = io.StringIO()
    stream = _SessionStdout(real, partial=_PARTIAL, final=_FINAL, kept=lambda: kept, silent=False)
    for line in text.splitlines(keepends=True):
        stream.write(line.removesuffix("\n"))
        if line.endswith("\n"):
            stream.write("\n")
    return real.getvalue()


def test_a_line_that_names_the_partial_file_names_the_final_one():
    line = f"wrote {_PARTIAL} -- see it\n"

    assert _through_stream(line) == f"wrote {_FINAL} -- see it\n"


def test_a_line_that_names_the_partial_file_of_a_run_not_kept_is_dropped_with_its_newline():
    text = f"before\n--- generated xml file: {_PARTIAL} ---\nafter\n"

    assert _through_stream(text, kept=False) == "before\nafter\n"
