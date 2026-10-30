"""Drive the loaded module on dut the way the tests do: write a line, read its count back."""

import shlex

from otto.lab import get_host

PARAMETERS = "/sys/module/qs_wordcount/parameters"


class ModuleCallError(RuntimeError):
    """Writing to or reading from the module's parameters failed on dut."""


async def count_words(line: str) -> int:
    """Write *line* to the module's ``line`` parameter on dut; return its ``words`` count.

    The write needs root, so it runs under sudo, inside ``sh -c`` so that the
    redirection is root's too. *line* reaches the module as data, never as shell.
    """
    host = get_host("dut")
    write = f"printf %s {shlex.quote(line)} > {PARAMETERS}/line"
    written = await host.exec(f"sh -c {shlex.quote(write)}", sudo=True)
    if not written.is_ok:
        raise ModuleCallError(
            f"writing {line!r} to {PARAMETERS}/line failed on dut: {written.value}"
        )
    read = await host.exec(f"cat {PARAMETERS}/words")
    if not read.is_ok:
        raise ModuleCallError(f"reading {PARAMETERS}/words failed on dut: {read.value}")
    try:
        return int(read.value)
    except ValueError:
        raise ModuleCallError(f"{PARAMETERS}/words held {read.value!r}, not a count") from None
