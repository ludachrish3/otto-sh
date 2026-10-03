"""Register short level-name aliases (WARN, CRIT) with the logging system.

Every stdlib level name is 5 characters or fewer once these two aliases are
registered (``DEBUG``/``ERROR`` are already 5; ``INFO``/``WARN``/``CRIT`` are
shorter) — letting log-file formatters use a fixed 5-wide level column
(``{levelname:<5}``) without truncating ``WARNING`` or ``CRITICAL`` or
overflowing the column for the shorter names.

Imported eagerly (not lazily) by ``otto.logger``'s package ``__init__`` since
it is stdlib-only and its side effect (``addLevelName``) must be live before
any formatter renders a level name.
"""

from logging import (
    CRITICAL,
    DEBUG,
    ERROR,
    INFO,
    WARNING,
    addLevelName,
)

LEVEL_ALIASES: dict[str, int] = {
    "WARN": WARNING,
    "CRIT": CRITICAL,
}
"""otto's short level-name aliases, and THE source of truth for them.

Registered below, and folded into :data:`LEVEL_NAMES`, which both
``otto.models.settings`` (``[logging.levels]``) and ``--log-level`` read, so
the names accepted stay the set otto actually understands: adding an alias
here makes it accepted everywhere without a second edit, and removing one
stops it validating instead of letting a config through that would later
crash ``setLevel``.
"""

_STDLIB_LEVELS: dict[str, int] = {
    "DEBUG": DEBUG,
    "INFO": INFO,
    "WARNING": WARNING,
    "ERROR": ERROR,
    "CRITICAL": CRITICAL,
}

_ALL_LEVELS: dict[str, int] = {**_STDLIB_LEVELS, **LEVEL_ALIASES}

LEVEL_NAMES: list[str] = sorted(
    _ALL_LEVELS, key=lambda name: (_ALL_LEVELS[name], name not in _STDLIB_LEVELS)
)
"""Every level name otto accepts, by severity, the full name before its alias.

THE vocabulary: ``[logging.levels]`` validates against it, ``--log-level``
refuses anything else and completes from it. Derived from
:data:`LEVEL_ALIASES`, so adding an alias makes it accepted everywhere.
"""

for _alias, _level in LEVEL_ALIASES.items():
    addLevelName(_level, _alias)
