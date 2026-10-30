"""The user-level ``~/.otto/settings.toml`` (spec 2026-08-28 host-inventory §8).

otto had only per-project settings; a machine inventory is a per-user fact, so
this file lives under ``otto.config.home.otto_home()`` (``OTTO_HOME``
relocates it; that module is not autodoc'd, hence the literal). Read once at
bootstrap by
:func:`otto.inventory.config.build_inventory`.
"""

import contextlib
import os
import stat as _stat
from dataclasses import dataclass
from pathlib import Path

import tomli
from pydantic import ValidationError

from ..models.base import load_toml_settings
from ..models.settings import UserSettingsModel
from .home import otto_home

CACHE_TABLE = "cache"
"""The settings-file table (and :class:`UserSettingsModel` field) holding the cache setting."""

CACHE_ENABLED_KEY = "enabled"
"""The key inside ``[cache]`` that turns otto's caches on or off."""

# At most one entry, keyed on the unresolved default path (resolving would cost
# an lstat per component on every call): the file is read once per process
# however many callers ask, and a changed OTTO_HOME replaces the entry rather
# than adding one. A ValueError is never stored, so a broken file
# is reported on every ask; the None of an absent file is.
_memo: dict[Path, "UserSettingsModel | None"] = {}

# The one stat of the default path, taken by the load that reads it and kept
# even when the parse then fails (a broken file has a real identity too). The
# names digest and the shim's stored triple both read THIS observation, so they
# cannot disagree about the file and no second syscall is spent on it.
_stat_memo: dict[Path, "os.stat_result | None"] = {}


def clear_user_settings_memo() -> None:
    """Forget the memoized default-path read, so the next load reads the file again."""
    _memo.clear()
    _stat_memo.clear()


def _stat_file(p: Path) -> "os.stat_result | None":
    """Stat *p*, or ``None`` when it does not stat (absent, ELOOP, ...), as the shim does."""
    try:
        return p.stat()
    except OSError:
        return None


def user_settings_path() -> Path:
    """Return ``otto_home() / "settings.toml"`` — pure, never created here.

    Pure in the same sense as ``otto_home()`` itself: a read-only
    code path may ask where the file *would* be without one appearing.
    """
    return otto_home() / "settings.toml"


def load_user_settings(path: "Path | None" = None) -> "UserSettingsModel | None":
    """Parse the user settings file; ``None`` when it is absent.

    *path* defaults to :func:`user_settings_path`, so ordinary callers ask for
    "this user's settings" with no arguments and tests can pin a file. The
    default path is read once per process (see
    :func:`clear_user_settings_memo`); an explicit *path* always reads.

    Raises
    ------
    ValueError
        On a parse or validation error, naming the file. A broken user file is
        a configuration error, not "no inventory" — silently degrading to the
        latter is how a typo'd ``[inventory]`` becomes an unexplained
        "no inventory is configured" much later in the run.
    """
    if path is not None:
        return _read_user_settings(path, _stat_file(path))
    key = user_settings_path()
    if key not in _memo:
        _memo.clear()
        _stat_memo.clear()
        st = _stat_memo[key] = _stat_file(key)
        _memo[key] = _read_user_settings(key, st)
    return _memo[key]


def user_settings_stat() -> "os.stat_result | None":
    """Return the memoized stat of the default settings file; ``None`` when absent.

    The same observation :func:`load_user_settings` made (loading through the
    memo if nothing has yet), so a memo hit adds no syscall. A file that
    fails to parse still has its stat here.
    """
    key = user_settings_path()
    if key not in _stat_memo:
        with contextlib.suppress(ValueError):
            load_user_settings()
    return _stat_memo.get(key)


def _read_user_settings(p: Path, st: "os.stat_result | None") -> "UserSettingsModel | None":
    """Read and validate one settings file given its *st*, without the memo."""
    if st is None or not _stat.S_ISREG(st.st_mode):
        return None
    try:
        data = load_toml_settings(p.read_text())
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        raise ValueError(f"{p}: {e}") from e
    try:
        return UserSettingsModel.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{p}: {e}") from e


@dataclass(frozen=True)
class CacheSetting:
    """Whether otto's caches are on, and where that was decided."""

    on: bool
    source: str
    """``"default"``, ``"settings.toml: <path>"``, or the error text of an unreadable file."""


def cache_enabled() -> CacheSetting:
    """Resolve the ``[cache] enabled`` setting; on unless the user file says otherwise.

    An unreadable or invalid settings file resolves *off*, with the error as
    the source: a broken file must not leave caches the user meant to disable
    running. Not memoized itself — :func:`load_user_settings` is the
    per-process memo.
    """
    try:
        settings = load_user_settings()
    except ValueError as e:
        return CacheSetting(on=False, source=str(e))
    if settings is None or settings.cache is None:
        return CacheSetting(on=True, source="default")
    return CacheSetting(on=settings.cache.enabled, source=f"settings.toml: {user_settings_path()}")
