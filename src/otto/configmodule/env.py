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

    sutDirs: list[Path] = field(init=False)

    def __post_init__(self) -> None:

        sutDirs = self.getEnvPaths(SUT_DIRS_ENV_VAR)
        object.__setattr__(self, 'sutDirs', sutDirs)

    @classmethod
    def getEnvVar(cls,
        varName: str,
        default: Any = None,
    ) -> Optional[str]:

        return getenv(varName, default)

    @classmethod
    def getEnvInt(cls,
        varName: str,
        default: Optional[int] = None,
    ) -> Optional[int]:

        value = cls.getEnvVar(varName=varName, default=default)
        if value is not None:
            value = int(value)

        return value


    @classmethod
    def getEnvPath(cls,
        envVar: str,
        default: Optional[Path] = None,
        mustExist: bool = True,
    ) -> Optional[Path]:

        path = cls.getEnvVar(envVar)

        if path is None:
            path = default
        else:
            path = Path(path)

        cls.validatePath(path, mustExist=mustExist)

        return path

    @classmethod
    def getEnvPaths(cls,
        envVar: str,
        mustExist: bool = True,
    ) -> list[Path]:

        paths: list[Path] = []

        pathStrings = cls.getEnvVar(envVar)

        if pathStrings is None:
            paths = []
        else:
            paths = [Path(p) for p in _PATH_LIST_SEP.split(pathStrings) if p]

        for path in paths:
            cls.validatePath(path, mustExist=mustExist)

        return paths

    @classmethod
    def validatePath(cls,
        path: Path | None,
        mustExist: bool = True,
    ) -> None:

        # The path is None, so return now to avoid checking
        # whether the path exists
        if path is None:
            return

        path = Path(path)
        if not mustExist or path.exists():
            return

        raise FileNotFoundError(f'Path {path} does not exist')

