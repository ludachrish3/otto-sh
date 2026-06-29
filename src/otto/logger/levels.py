"""Register short level-name aliases (WARN, CRIT) with the logging system."""

from logging import (
    CRITICAL,
    WARNING,
    addLevelName,
)

# Define alias for levels with long names
addLevelName(WARNING, "WARN")
addLevelName(CRITICAL, "CRIT")
