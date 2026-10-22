"""Build a log line that relays text otto did not write: device output, an echoed command.

Every otto sink renders a log record as Rich markup: the console handler is a
``RichHandler`` built ``markup=True``, and ``console.log`` and ``verbose.log``
go through :class:`~otto.logger.formatters.RichFormatter`, which prints the
record through a capture console with markup on. That is right for otto's own
styling and wrong for what a device printed or for the command otto echoes:
any ``[word]`` Rich accepts as a tag (``[red]``, ``[link=x]``, ``[x:y]``,
``image:[sha256:abc]``) is DELETED from all three sinks, and a lone ``[/x]``
raises in the listener thread.

:func:`relay_line` keeps the two apart for every otto line that relays such
text. It is not a general logging API: a caller's own log messages carry the
caller's own markup and are the caller's to escape.
"""


def relay_line(prefix: str, text: str) -> str:
    r"""Return otto's markup *prefix* followed by *text*, whose brackets every sink keeps.

    *prefix* is otto's own markup and is used as is: a style that runs to the
    end of the line (``"[yellow]@host > | "``), a hand-escaped literal
    (``r"\[docker] compose down failed: "``), or ``""``. It must not carry
    foreign text.

    *text* is escaped (:func:`rich.markup.escape`), so its brackets reach the
    console and both log files as written. Rich still substitutes ``:emoji:``
    codes in a log sink, so the text is not guaranteed character for character;
    the brackets are what is kept. Escaped once and rendered
    once by each sink, so no sink shows the escape character. Start *text* at
    the first foreign value; otto prose after it is escaped too, harmlessly,
    since it carries no markup. A trailing backslash in *text* is kept single,
    so the result may be followed by plain text (a ``%``-format remainder) but
    never directly by a ``[`` tag.

    With ``prefix=""`` the result is one escaped value, for a ``%``-argument::

        logger.warning("%s: rm failed: %s", host.id, relay_line("", result.value))

    Args:
        prefix: otto's markup for the start of the line.
        text: the relayed text: device output, a command line, a hook's message.

    Returns:
        A markup string whose rendered text is the rendered *prefix* then *text*.
    """
    from rich.markup import escape  # function-scope: keeps `rich` off the startup graph

    # The sentinel: `escape` doubles a trailing backslash so it cannot escape
    # a tag that FOLLOWS the text -- and nothing follows here, so the doubled
    # backslash would print. Escaping one more character keeps the backslash
    # off the end, and it is sliced off again.
    return f"{prefix}{escape(text + '.')[:-1]}"
