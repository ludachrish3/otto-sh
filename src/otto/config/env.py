"""Environment variables that are needed before parsing CLI arguments."""

from pathlib import Path
from typing import TYPE_CHECKING

LAB_ENV_VAR = "OTTO_LAB"
OTTOPATH_ENV_VAR = "OTTOPATH"
"""The list of project roots, separated by :data:`os.pathsep` like ``PATH``."""
OTTO_ENV_PREFIX = "OTTO_"
"""The prefix of every other variable that configures otto."""
FIELD_PRODUCT_ENV_VAR = "OTTO_FIELD_PRODUCTS"
LOG_DAYS_ENV_VAR = "OTTO_LOG_DAYS"
LOG_LVL_ENV_VAR = "OTTO_LOG_LEVEL"
LOG_RICH_ENV_VAR = "OTTO_LOG_RICH"
# OTTO_SSH_DEBUG is read only by the host layer: SSH_DEBUG_ENV_VAR lives in
# otto.host.connections, beside apply_ssh_debug_level.
XDIR_ENV_VAR = "OTTO_XDIR"
HOME_ENV_VAR = "OTTO_HOME"

DEFAULT_LOG_RETENTION_DAYS = 30

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings


def is_otto_env_var(name: str) -> bool:
    """Whether *name* configures otto: ``OTTOPATH`` or any ``OTTO_``-prefixed name.

    Every sanitizer that strips otto's configuration from an environment asks
    this, because ``OTTOPATH`` is the one variable the prefix does not catch.
    """
    return name == OTTOPATH_ENV_VAR or name.startswith(OTTO_ENV_PREFIX)


def validate_path(
    path: Path | None,
    must_exist: bool = True,
) -> None:
    """Validate that *path* exists when *must_exist* is ``True``.

    Raises ``FileNotFoundError`` if the path is set but does not exist on
    disk. A ``None`` path is always accepted (the env var was not set).
    """
    # The path is None, so return now to avoid checking
    # whether the path exists
    if path is None:
        return

    path = Path(path)
    if not must_exist or path.exists():
        return

    raise FileNotFoundError(f"Path {path} does not exist")


def load_otto_env() -> "OttoEnvSettings":
    """Construct the ``OTTO_*`` and ``OTTOPATH`` env settings; check every ``OTTOPATH`` root exists.

    Raises FileNotFoundError (the historical OttoEnv() startup contract).
    """
    from ..models.settings import OttoEnvSettings  # lazy: avoid an import cycle

    env = OttoEnvSettings()
    for path in env.ottopath:
        validate_path(path, must_exist=True)
    return env
