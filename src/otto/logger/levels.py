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
    WARNING,
    addLevelName,
)

LEVEL_ALIASES: dict[str, int] = {
    "WARN": WARNING,
    "CRIT": CRITICAL,
}
"""otto's short level-name aliases, and THE source of truth for them.

Registered below, and read by ``otto.models.settings`` so the level names
``[logging.levels]`` accepts stay the set otto actually understands: adding an
alias here makes it configurable without a second edit, and removing one stops
it validating instead of letting a config through that would later crash
``setLevel``.
"""

for _alias, _level in LEVEL_ALIASES.items():
    addLevelName(_level, _alias)
