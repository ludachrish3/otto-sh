"""WARN/CRIT level-name aliases and the fixed-width level column they enable.

``otto.logger`` eagerly wires short aliases for the two long stdlib level
names (``WARNING`` → ``WARN``, ``CRITICAL`` → ``CRIT``) so every level name
is ≤5 characters — letting the file-sink formatter use a fixed-width column
without truncating or overflowing it.
"""

import logging

import otto.logger  # noqa: F401 — import triggers the eager `from . import levels` wiring
from otto.logger.formatters import RichFormatter


def test_warning_level_name_aliased_to_warn():
    assert logging.getLevelName(logging.WARNING) == "WARN"


def test_critical_level_name_aliased_to_crit():
    assert logging.getLevelName(logging.CRITICAL) == "CRIT"


def test_settings_accepts_exactly_names_logging_can_act_on():
    """``[logging.levels]`` must not validate a name ``setLevel`` would reject.

    The settings model's accepted set and the aliases THIS module registers are
    two halves of one contract: a name the model lets through goes straight to
    ``Logger.setLevel``, so a set that drifted ahead of the registrations would
    accept a config and then crash the CLI with ``Unknown level``. Asserted
    against ``logging`` itself rather than against a second hand-written list,
    which would just be a third copy to drift.
    """
    from otto.models.settings import _LOG_LEVEL_NAMES

    for name in _LOG_LEVEL_NAMES:
        assert isinstance(logging.getLevelName(name), int), (
            f"[logging.levels] accepts {name!r}, but logging does not know it"
        )


def test_every_alias_this_module_registers_is_configurable():
    """The other direction: a new alias here is usable in ``[logging.levels]``.

    ``otto.models.settings`` builds its accepted set FROM ``LEVEL_ALIASES``, so
    this holds by construction — pinned anyway, because the day someone
    re-hand-copies the set (the shape this replaced) it goes quietly false.
    """
    from otto.logger.levels import LEVEL_ALIASES
    from otto.models.settings import _LOG_LEVEL_NAMES

    assert set(LEVEL_ALIASES) <= _LOG_LEVEL_NAMES


def test_file_formatter_aligns_message_column_across_all_levels():
    """The message must start at the same column for every level name.

    All five level names (DEBUG/INFO/WARN/ERROR/CRIT) are ≤5 characters once
    the aliases are wired, so a fixed-width ``{levelname:<5}`` column keeps
    the message text aligned regardless of which level fired.
    """
    formatter = RichFormatter()
    formatter.rich = False
    levels = [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ]

    message_columns: list[int] = []
    for level in levels:
        record = logging.LogRecord(
            name="otto",
            level=level,
            pathname=__file__,
            lineno=1,
            msg="the message",
            args=None,
            exc_info=None,
        )
        formatted = formatter.format(record)
        message_columns.append(formatted.index("the message"))

    assert len(set(message_columns)) == 1, (
        f"message column differs across levels: {message_columns}"
    )


# ── one record, several handlers: the formatter must not consume it ──────────


def _record(msg):
    return logging.LogRecord(
        name="otto",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_formatting_a_record_twice_renders_the_same_text():
    """``console.log`` and ``verbose.log`` format the SAME record object, in turn.

    ``otto.logger.management.setup_output_dir`` puts the console handler and
    both file handlers behind one ``QueueListener``, and ``logging`` hands each
    of them the same :class:`~logging.LogRecord` instance. ``_stylize`` writes
    its RENDERED output back onto ``record.msg``, so without a restore the
    second file handler renders text the first one already rendered — and rich
    markup does not survive being rendered twice.

    THE ESCAPED BRACKET IS THE CASE THAT BITES. Otto escapes brackets it wants
    taken literally (``otto.project.orchestrator._literal``); the first pass
    turns ``\\[bench]`` into ``[bench]``, and the second pass reads that bare
    bracket as a style tag and DELETES it. The symptom is ``console.log``
    carrying the lab list and ``verbose.log`` silently missing it — one log
    file telling the truth and the other not, which is worse than either being
    wrong on its own.

    Asserted as "the two renders are equal" rather than against a literal, so
    it stays true if the prefix format ever changes.
    """
    formatter = RichFormatter()
    formatter.rich = False
    record = _record(r"lab(s) \[bench, floor] here")

    first = formatter.format(record)
    second = formatter.format(record)

    assert "[bench, floor]" in first
    assert second == first


def test_formatting_leaves_the_record_as_it_found_it():
    """The narrower half: a formatter is a reader, and must not mutate its input.

    Pinned separately from the render-twice cell because they fail for
    different reasons — this one catches a restore that runs on the happy path
    only, which the equality above would not notice if ``_stylize`` ever
    started raising.
    """
    formatter = RichFormatter()
    formatter.rich = False
    original = r"lab(s) \[bench] here"
    record = _record(original)

    formatter.format(record)

    assert record.msg == original
