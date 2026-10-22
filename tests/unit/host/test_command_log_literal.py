"""A host's command echo and output reach every sink exactly as written (#697).

Driven through the real fan-out: :func:`otto.logger.management.init_cli_logging`
plus :func:`~otto.logger.management.create_output_dir` install the same console
handler, queue, listener and :class:`~otto.logger.formatters.RichFormatter` the
CLI uses, and a :class:`~otto.host.local_host.LocalHost` runs a real shell
command. The echo and output lines opt in to Rich markup for otto's styled
host prefix, so a bracket in device output that Rich accepts as a tag
(``[red]``, ``[link=x]``, ``[x:y]``) would be deleted from the console and
from both log files, and a lone ``[/x]`` could raise, if the device's text
were not escaped.

Each fact is its own short output line: the console wraps at its width, and a
wrapped fact would be split across two rendered lines.
"""

import logging
import re

import pytest

from otto.host.local_host import LocalHost
from otto.logger import management

# Each is printed by the device on a line of its own and must come back verbatim.
_LINES = [
    "a [red]b[/red] c",
    "d [link=x]e[/link] f",
    "g [x:y] h",
    "image:[sha256:abc]",
    "i [bold] j",
    "k [/x] l",
    r"m \[n] o",
    "root:x:0:0 :warning: y",
]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _command() -> str:
    # printf '%s\n' with each line single-quoted: the shell passes the text
    # through untouched, so what reaches the log is exactly _LINES.
    quoted = " ".join("'" + line.replace("'", "'\\''") + "'" for line in _LINES)
    return f"printf '%s\\n' {quoted}"


async def _run_and_read(tmp_path, monkeypatch, host_name: str | None = None) -> dict[str, str]:
    # The console's text is read from a buffer of its own rather than capsys:
    # the console handler prints on the listener thread, and pytest's live-log
    # handler swaps sys.stdout out from under capsys while it emits on the
    # test's thread, so a line printed in that window would miss capsys.
    import io

    from otto.console import CONSOLE

    console = io.StringIO()
    monkeypatch.setattr(CONSOLE, "_file", console)
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    host = LocalHost()
    if host_name is not None:
        host.name = host_name
    try:
        result = (await host.run(_command())).only
    finally:
        await host.close()
    assert result.retcode == 0, result
    management.reset()  # drain the listener into every sink
    return {
        "console": _ANSI.sub("", console.getvalue()),
        "console.log": (out / "console.log").read_text(),
        "verbose.log": (out / "verbose.log").read_text(),
    }


@pytest.mark.asyncio
async def test_device_output_with_brackets_reaches_every_sink_verbatim(tmp_path, monkeypatch):
    sinks = await _run_and_read(tmp_path, monkeypatch)
    for sink, text in sinks.items():
        for line in _LINES:
            assert f"@localhost > | {line}" in text, (sink, line, text)


@pytest.mark.asyncio
async def test_the_command_echo_keeps_its_brackets_in_every_sink(tmp_path, monkeypatch):
    sinks = await _run_and_read(tmp_path, monkeypatch)
    for sink, text in sinks.items():
        # The echo is long enough to wrap on the console; its opening and a
        # bracketed token near the start are enough to prove nothing was eaten.
        assert "@localhost   | printf '%s\\n' 'a [red]b[/red] c'" in text, (sink, text)


@pytest.mark.asyncio
async def test_a_bracketed_host_name_keeps_its_brackets_in_the_styled_prefix(tmp_path, monkeypatch):
    """The host name is otto-side text in a markup prefix: escaped, so ``[x]`` is not a tag."""
    sinks = await _run_and_read(tmp_path, monkeypatch, host_name="[x]")
    for sink, text in sinks.items():
        assert "@[x] > | a [red]b[/red] c" in text, (sink, text)
        assert "@[x]   | printf" in text, (sink, text)


@pytest.mark.asyncio
async def test_escaping_leaves_no_stray_backslash_in_any_sink(tmp_path, monkeypatch):
    """Escaped once, rendered once: no sink shows the escape character itself."""
    sinks = await _run_and_read(tmp_path, monkeypatch)
    for sink, text in sinks.items():
        assert "\\[" not in text.replace(r"m \[n] o", ""), (sink, text)


@pytest.mark.asyncio
async def test_otto_still_styles_the_host_prefix(tmp_path):
    """The escaping covers the foreign text only: otto's own style is still markup.

    Read off the records themselves, parsed the way every sink parses them
    (:meth:`rich.text.Text.from_markup`): the whole line carries otto's style,
    and the plain text is the device's line verbatim. A sink's capture console
    is not a terminal, so its ANSI output cannot show the style; the parse can.
    """
    from rich.text import Text

    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    keep = _Keep(logging.DEBUG)
    logging.getLogger().addHandler(keep)
    host = LocalHost()
    try:
        await host.run("printf '%s\\n' 'a [red]b[/red] c'")
    finally:
        await host.close()
        logging.getLogger().removeHandler(keep)
    rendered = {}
    for record in records:
        if getattr(record, "host", None) is host:
            text = Text.from_markup(record.getMessage())
            rendered[text.plain] = {span.style for span in text.spans}
    assert rendered["@localhost > | a [red]b[/red] c"] == {"yellow"}, rendered
    assert rendered["@localhost   | printf '%s\\n' 'a [red]b[/red] c'"] == {"bold"}, rendered
