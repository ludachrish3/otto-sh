"""Environment variables that are needed before parsing CLI arguments"""

import os
import re
from dataclasses import dataclass, field
from os import getenv
from pathlib import Path
from typing import (
    Any,
    Optional,
)

# Path-list env vars (e.g. OTTO_SUT_DIRS) historically accepted only commas,
# but the standard Unix convention is os.pathsep (':' on Linux). Accept both
# so users can use whichever feels natural — and so callers building lists
# via os.pathsep.join(...) work without surprises.
_PATH_LIST_SEP = re.compile(rf'[,{re.escape(os.pathsep)}]')

LAB_ENV_VAR           = 'OTTO_LAB'
SUT_DIRS_ENV_VAR      = 'OTTO_SUT_DIRS'
FIELD_PRODUCT_ENV_VAR = 'OTTO_FIELD_PRODUCTS'
LOG_DAYS_ENV_VAR      = 'OTTO_LOG_DAYS'
LOG_LVL_ENV_VAR       = 'OTTO_LOG_LEVEL'
LOG_RICH_ENV_VAR      = 'OTTO_LOG_RICH'
XDIR_ENV_VAR          = 'OTTO_XDIR'
FIELD_DEFAULT_ENV_VAR = 'OTTO_FIELD_DEFAULT'

DEFAULT_LOG_RETENTION_DAYS = 30


@dataclass(
    frozen=True,
)
class OttoEnv():
    """Otto environment variables"""

    sut_dirs: list[Path] = field(init=False)

    def __post_init__(self) -> None:

        sut_dirs = self.get_env_paths(SUT_DIRS_ENV_VAR)
        object.__setattr__(self, 'sut_dirs', sut_dirs)

    @classmethod
    def get_env_var(cls,
        var_name: str,
        default: Any = None,
    ) -> Optional[str]:

        return getenv(var_name, default)

    @classmethod
    def get_env_int(cls,
        var_name: str,
        default: Optional[int] = None,
    ) -> Optional[int]:

        value = cls.get_env_var(var_name=var_name, default=default)
        if value is not None:
            value = int(value)

        return value


    @classmethod
    def get_env_path(cls,
        env_var: str,
        default: Optional[Path] = None,
        must_exist: bool = True,
    ) -> Optional[Path]:

        path = cls.get_env_var(env_var)

        if path is None:
            path = default
        else:
            path = Path(path)

        cls.validate_path(path, must_exist=must_exist)

        return path

    @classmethod
    def get_env_paths(cls,
        env_var: str,
        must_exist: bool = True,
    ) -> list[Path]:

        paths: list[Path] = []

        pathStrings = cls.get_env_var(env_var)

        if pathStrings is None:
            paths = []
        else:
            paths = [Path(p) for p in _PATH_LIST_SEP.split(pathStrings) if p]

        for path in paths:
            cls.validate_path(path, must_exist=must_exist)

        return paths

    @classmethod
    def validate_path(cls,
        path: Path | None,
        must_exist: bool = True,
    ) -> None:

        # The path is None, so return now to avoid checking
        # whether the path exists
        if path is None:
            return

        path = Path(path)
        if not must_exist or path.exists():
            return

        raise FileNotFoundError(f'Path {path} does not exist')

