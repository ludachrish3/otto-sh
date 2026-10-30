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

from ..models.base import compact_validation_error, load_toml_settings
from ..models.settings import CacheSettingsSpec, UserSettingsModel
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
# cannot disagree about the file and no second syscall is spent on it. A path
# that cannot be a settings file at all (see _stat_file) is recorded as None:
# the cache is then off, so nothing is stored for the shim to compare.
_stat_memo: dict[Path, "os.stat_result | None"] = {}


def clear_user_settings_memo() -> None:
    """Forget the memoized default-path read, so the next load reads the file again."""
    _memo.clear()
    _stat_memo.clear()


def _stat_file(p: Path) -> "os.stat_result | None":
    """Stat settings file *p*: ``None`` when it is absent, a :class:`ValueError` when it is broken.

    Absent is ``ENOENT`` or ``ENOTDIR`` only. Any other stat error (``ELOOP``,
    ``EACCES``, ...) or a path that is not a regular file (a directory, a
    FIFO, which reading would block on) is a file the user must fix, named in
    one line, never silently "no settings". Not ``corpus_snapshot.stat``: that
    maps every ``OSError`` to ``None``, and this split needs the errno.
    """
    try:
        st = p.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise ValueError(f"{p}: {e.strerror or e}") from e
    if not _stat.S_ISREG(st.st_mode):
        raise ValueError(f"{p}: not a regular file")
    return st


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
        On a parse or validation error, or a path that cannot be read as a
        settings file (a directory, a FIFO, an ``EACCES`` stat), naming the
        file. A broken user file is a configuration error, not "no
        inventory" — silently degrading to the latter is how a typo'd
        ``[inventory]`` becomes an unexplained "no inventory is configured"
        much later in the run.
    """
    if path is not None:
        return _read_user_settings(path, _stat_file(path))[0]
    key = user_settings_path()
    if key not in _memo:
        clear_user_settings_memo()
        _stat_memo[key] = None  # what stays recorded when _stat_file raises
        st = _stat_memo[key] = _stat_file(key)
        _memo[key] = _read_user_settings(key, st)[0]
    return _memo[key]


def user_settings_stat() -> "os.stat_result | None":
    """Return the memoized stat of the default settings file; ``None`` when absent.

    The same observation :func:`load_user_settings` made (loading through the
    memo if nothing has yet), so a memo hit adds no syscall. A file that
    fails to parse still has its stat here; a path that cannot be a settings
    file at all (see :func:`load_user_settings`) is ``None``, and the cache is
    then off.
    """
    key = user_settings_path()
    if key not in _stat_memo:
        with contextlib.suppress(ValueError):
            load_user_settings()
    return _stat_memo.get(key)


def _read_user_settings(
    p: Path, st: "os.stat_result | None"
) -> "tuple[UserSettingsModel | None, str]":
    """Read and validate one settings file given its *st*, without the memo.

    Returns the model and the exact text it was validated from (``None`` and
    ``""`` when the file is absent): the one parse and validation behind both
    every load and :func:`set_cache_enabled`'s edit. The bytes are decoded
    without newline translation, so a CRLF file reaches the editor as CRLF.
    """
    if st is None:
        return None, ""
    try:
        text = p.read_bytes().decode()
        data = load_toml_settings(text)
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        raise ValueError(f"{p}: {e}") from e
    try:
        return UserSettingsModel.model_validate(data), text
    except ValidationError as e:
        raise ValueError(f"{p}: {compact_validation_error(e)}") from e


@dataclass(frozen=True)
class CacheSetting:
    """Whether otto's caches are on, and where that was decided."""

    on: bool
    source: str
    """``"default"``, ``"settings.toml: <path>"``, or the error text of an unreadable file."""


def _cache_on(settings: "UserSettingsModel | None") -> bool:
    """Whether a validated settings model leaves the caches on; the default is the model field's."""
    cache = settings.cache if settings is not None and settings.cache is not None else None
    return (cache or CacheSettingsSpec()).enabled


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
        return CacheSetting(on=_cache_on(settings), source="default")
    return CacheSetting(on=_cache_on(settings), source=f"settings.toml: {user_settings_path()}")


def set_cache_enabled(enabled: bool) -> CacheSetting:
    """Set ``[cache] enabled`` in the user settings file in place; return the new setting.

    The current file is validated first: a broken one raises its
    :class:`ValueError` (see :func:`load_user_settings`) and stays untouched,
    so a verb never rewrites a file it could not read. Turning the cache off
    creates the home, the file and the ``[cache]`` table as needed; turning it
    on only edits an existing ``[cache]`` table, since no table already means
    on (a bare ``[cache]`` table gets ``enabled = true`` written). Nothing is
    written when the setting already is what is asked and is spelled as a
    boolean (``enabled = "false"`` is rewritten). The edit keeps every other
    byte of the file (comments, order, spelling, line endings) and the file's
    permission bits, validates the edited document as a
    :class:`~otto.models.settings.UserSettingsModel`, and replaces the file
    atomically. The memo is dropped whether or not anything was written, so
    the returned :func:`cache_enabled` is what the next caller sees, even
    after an edit made behind the memo's back.
    """
    import tomlkit  # only the verb edits TOML; every read goes through tomli

    from ..utils import atomic_write_bytes

    path = user_settings_path()
    st = _stat_file(path)  # ValueError propagates: nothing is written
    current, text = _read_user_settings(path, st)
    doc = tomlkit.parse(text)
    table = doc.get(CACHE_TABLE)
    # The state is the validated model's (pydantic coerces "false" to off). A coerced
    # string is rewritten as a real boolean even when the state already matches, and
    # a bare [cache] table gets the key written.
    raw = table.unwrap().get(CACHE_ENABLED_KEY) if table is not None else None
    if not (_cache_on(current) == enabled and (table is None or isinstance(raw, bool))):
        if table is None:
            table = tomlkit.table()
            doc[CACHE_TABLE] = table
        table[CACHE_ENABLED_KEY] = enabled
        new_text = tomlkit.dumps(doc)
        UserSettingsModel.model_validate(load_toml_settings(new_text))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, new_text.encode(), preserve=st)
    # Written or not, the memo may predate an edit made behind its back.
    clear_user_settings_memo()
    return cache_enabled()
