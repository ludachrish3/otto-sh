"""Log or display text otto did not write, keeping it literal on every sink (#697).

A log record is rendered as Rich markup only when it opts in with
``extra={"markup": True}``; every other record reaches the console,
``console.log`` and ``verbose.log`` as written. Most lines that quote foreign
text therefore need nothing: they do not opt in. A line that combines otto's
own styling with foreign text -- a host's bold echo prefix and the command it
ran, its yellow output prefix and a device line -- must opt in for the style.
Then two things would alter the foreign part: a ``[word]`` Rich accepts as a
tag (``[red]``, ``[link=x]``, ``image:[sha256:abc]``) would be deleted, and a
lone ``[/x]`` would raise in the listener thread; and a colon-word such as
``root:x:0:0`` or ``:warning:`` would be read as an ``:emoji:`` code.

:func:`log_relayed` does both jobs in one place: it escapes the text and sets
``markup=True, emoji=False`` on the record, so a caller cannot relay text
without turning the emoji pass off. :func:`escape_relayed` is the pure escape,
for text that is displayed rather than logged. This is not a general logging
API: a caller who opts in to markup on its own escapes and opts out of emoji
itself.
"""

import logging


def escape_relayed(text: str) -> str:
    r"""Return *text* with its brackets escaped so Rich prints them as written.

    A trailing backslash is kept single, so the result may be followed by plain
    text but never directly by a ``[`` tag. Rich's ``:emoji:`` pass is a separate
    switch: a caller that renders the result must turn it off too.
    """
    from rich.markup import escape  # function-scope: keeps `rich` off the startup graph

    # The sentinel: `escape` doubles a trailing backslash so it cannot escape
    # a tag that FOLLOWS the text -- and nothing follows here, so the doubled
    # backslash would print. Escaping one more character keeps the backslash
    # off the end, and it is sliced off again.
    return escape(text + ".")[:-1]


def log_relayed(
    logger: logging.Logger,
    level: int,
    prefix: str,
    text: str,
    *,
    extra: dict[str, object] | None = None,
    per_line: bool = False,
) -> None:
    r"""Log otto's markup *prefix* followed by *text*, which every sink shows verbatim.

    *prefix* is otto's own markup and is used as is: a style that runs to the
    end of the line (``"[yellow]@host > | "``). It must not carry foreign text.
    A line with no styled prefix needs no helper: log it without opting in to
    markup and it is printed as written.

    *text* is escaped (:func:`escape_relayed`) and the record is sent with
    ``markup=True, emoji=False``, so its brackets and colon-words reach the
    console and both log files as written. Escaped once and rendered once by
    each sink, so no sink shows the escape character.

    Example::

        log_relayed(logger, logging.INFO, f"[bold]@{host.name} | ", command)

    Args:
        logger: the logger to emit on.
        level: the record's level.
        prefix: otto's markup for the start of the line.
        text: the relayed text: device output, a command line, a hook's message.
        extra: record attributes besides ``markup`` and ``emoji``, which the
            helper owns.
        per_line: when true, *prefix* starts every line of *text* and the lines
            form one multi-line record.
    """
    if not logger.isEnabledFor(level):
        return
    if per_line:
        message = "\n".join(f"{prefix}{escape_relayed(line)}" for line in text.splitlines())
    else:
        message = f"{prefix}{escape_relayed(text)}"
    logger.log(
        level,
        message,
        extra={**(extra or {}), "markup": True, "emoji": False},
        stacklevel=2,
    )
