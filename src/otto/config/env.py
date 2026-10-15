"""Environment variables that are needed before parsing CLI arguments."""

from pathlib import Path
from typing import TYPE_CHECKING

LAB_ENV_VAR = "OTTO_LAB"
SUT_DIRS_ENV_VAR = "OTTO_SUT_DIRS"
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
    """Construct the OTTO_* env settings and validate that every sut_dir exists.

    Raises FileNotFoundError (the historical OttoEnv() startup contract).
    """
    from ..models.settings import OttoEnvSettings  # lazy: avoid an import cycle

    env = OttoEnvSettings()
    for path in env.sut_dirs:
        validate_path(path, must_exist=True)
    return env
