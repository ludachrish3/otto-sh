"""Hostile text for the "printed literally" tests (#785), and the one way they check it.

:data:`HOSTILE` carries a markup tag Rich would delete, an ``:emoji:`` code Rich
would substitute, and a 300-character run that a console narrower than it
would wrap. A caller that prints it through ``otto.console.print_literal``
prints it as exactly one whole line.
"""

HOSTILE = "[red]x[/red] :smile: " + "p" * 300


HOSTILE_TOKEN = "[red]x[/red]:smile:" + "p" * 300
""":data:`HOSTILE` without its spaces, for a table cell that folds.

Rich drops a space that lands on a fold boundary (a limit
``tests/unit/test_console_literal.py::test_rich_fold_limits_are_pinned`` pins), so
a folded cell rejoins exactly only when it holds none."""


def assert_one_verbatim_line(output: str, expected: str) -> None:
    """Assert *expected* is exactly one whole line of *output*: not cut, wrapped or rewritten."""
    lines = output.splitlines()
    assert lines.count(expected) == 1, f"{expected!r} is not one whole line of:\n{output}"


def rejoin_column(output: str, index: int) -> str:
    """Concatenate column *index*'s body cells in a boxed table's *output*, in order.

    The body is every ``│`` line after the header separator (``├`` or ``┡``);
    each line's cell is stripped of its padding. A cell that folded over
    several lines therefore comes back as the one string it was, and a cell
    cut with "…" comes back with the "…" in it.
    """
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip()[:1] in ("├", "┡"))
    body = [line.strip() for line in lines[start + 1 :] if line.strip().startswith("│")]
    return "".join(line.strip("│").split("│")[index].strip() for line in body)
