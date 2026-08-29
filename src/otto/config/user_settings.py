"""The user-level ``~/.otto/settings.toml`` (spec 2026-08-28 host-inventory §8).

otto had only per-repo settings; a machine inventory is a per-user fact, so
this file lives under ``otto.config.home.otto_home()`` (``OTTO_HOME``
relocates it; that module is not autodoc'd, hence the literal). Read once at
bootstrap by
:func:`otto.inventory.config.build_inventory`.
"""

from pathlib import Path

import tomli
from pydantic import ValidationError

from ..models.settings import UserSettingsModel
from .home import otto_home


def user_settings_path() -> Path:
    """Return ``otto_home() / "settings.toml"`` — pure, never created here.

    Pure in the same sense as ``otto_home()`` itself: a read-only
    code path may ask where the file *would* be without one appearing.
    """
    return otto_home() / "settings.toml"


def load_user_settings(path: "Path | None" = None) -> "UserSettingsModel | None":
    """Parse the user settings file; ``None`` when it is absent.

    *path* defaults to :func:`user_settings_path`, so ordinary callers ask for
    "this user's settings" with no arguments and tests can pin a file.

    Raises
    ------
    ValueError
        On a parse or validation error, naming the file. A broken user file is
        a configuration error, not "no inventory" — silently degrading to the
        latter is how a typo'd ``[inventory]`` becomes an unexplained
        "no inventory is configured" much later in the run.
    """
    p = path if path is not None else user_settings_path()
    if not p.is_file():
        return None
    try:
        data = tomli.loads(p.read_text())
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        raise ValueError(f"{p}: {e}") from e
    try:
        return UserSettingsModel.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{p}: {e}") from e
