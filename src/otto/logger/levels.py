from logging import (
    CRITICAL,
    WARNING,
    addLevelName,
)

# Define alias for levels with long names
addLevelName(WARNING,  'WARN')
addLevelName(CRITICAL, 'CRIT')
