"""Storage module for DB-agnostic lab/host repository pattern."""

from .factory import (
    create_host_from_dict as create_host_from_dict,
)
from .factory import (
    validate_host_dict as validate_host_dict,
)
from .json_repository import (
    JsonFileLabRepository as JsonFileLabRepository,
)
from .protocol import (
    LabRepository as LabRepository,
)
