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

# Define alias for levels with long names
addLevelName(WARNING, "WARN")
addLevelName(CRITICAL, "CRIT")
