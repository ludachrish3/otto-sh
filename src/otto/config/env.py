"""Environment variables that are needed before parsing CLI arguments."""

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

# Path-list env vars (e.g. OTTO_SUT_DIRS) historically accepted only commas,
# but the standard Unix convention is os.pathsep (':' on Linux). Accept both
# so users can use whichever feels natural — and so callers building lists
# via os.pathsep.join(...) work without surprises.
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")

LAB_ENV_VAR = "OTTO_LAB"
SUT_DIRS_ENV_VAR = "OTTO_SUT_DIRS"
FIELD_PRODUCT_ENV_VAR = "OTTO_FIELD_PRODUCTS"
LOG_DAYS_ENV_VAR = "OTTO_LOG_DAYS"
LOG_LVL_ENV_VAR = "OTTO_LOG_LEVEL"
LOG_RICH_ENV_VAR = "OTTO_LOG_RICH"
SSH_DEBUG_ENV_VAR = "OTTO_SSH_DEBUG"
XDIR_ENV_VAR = "OTTO_XDIR"
HOME_ENV_VAR = "OTTO_HOME"

DEFAULT_LOG_RETENTION_DAYS = 30

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings


def split_path_list(value: str) -> list[Path]:
    """Split a path-list env value (``OTTO_SUT_DIRS``) into its paths.

    Entries are separated by commas or :data:`os.pathsep`, either or both,
    and empty entries are dropped. Nothing else is touched: whitespace is
    part of an entry (``/a, /b`` names ``" /b"``), exactly as the
    completion-cache environment and the console-script shim split the same
    variable. The loader and ``otto init``'s "is this repo already on the
    list" check both read the variable through here, so they cannot disagree
    about what it names.
    """
    return [Path(entry) for entry in _PATH_LIST_SEP.split(value) if entry]


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
