"""Tolerant readers of a repo's raw ``.otto/settings.toml``.

Every reader returns ``None`` rather than raising on an absent or unparsable
file: that is what lets the doctor and the scaffolder fall back to the
conventional layout on a repo otto has not scaffolded yet.
"""

from pathlib import Path
from typing import Any

import tomli

from ..config.repo import TOML_SETTINGS_PATH


def settings_path(root: Path) -> Path:
    """Return where *root*'s settings file lives (it may not exist)."""
    return root / TOML_SETTINGS_PATH


def settings_data(root: Path) -> dict[str, Any] | None:
    """Return the raw parsed ``.otto/settings.toml`` (``None`` if absent/unparseable).

    Returning ``None`` (rather than raising) is what lets every doctor check
    fall back to the conventional layout on a repo otto has not scaffolded
    yet; the settings area itself reports the parse error through
    :func:`otto.config.repo.validate_settings`. Decoded as UTF-8, as TOML
    requires and that check reads it, whatever the locale: an undecodable
    file is unreadable here too, never a ``UnicodeDecodeError`` out of a
    reader the doctor relies on not to raise.
    """
    path = settings_path(root)
    if not path.is_file():
        return None
    try:
        return tomli.loads(path.read_bytes().decode())
    except (tomli.TOMLDecodeError, UnicodeDecodeError, OSError):
        return None


def settings_paths(root: Path) -> dict[str, list[Path]] | None:
    """Parse ``.otto/settings.toml`` and anchor its ``tests``/``libs`` lists to *root*.

    Returns ``None`` when the settings file is absent or fails to parse, so
    callers fall back to the conventional path instead of erroring.

    Applies phase 1 anchoring via :func:`otto.utils.anchor_path`: ``~`` expands
    to the user's home, and whatever is still relative afterwards is anchored
    to *root*. Host data is NOT one of these lists — it is declared as
    ``[[lab.sources]]`` entries, not read here.

    Both keys are always present: a value that is not a list reads as ``[]``
    (the settings area reports it), so callers index without a ``KeyError``.
    An entry that cannot be anchored (``~nosuchuser/...``) is left out, for
    the same reason and with the same reporter.
    """
    data = settings_data(root)
    if data is None:
        return None
    resolved: dict[str, list[Path]] = {}
    for key in ("tests", "libs"):
        values = data.get(key, [])
        anchored = [_anchored(str(v), root) for v in values] if isinstance(values, list) else []
        resolved[key] = [path for path in anchored if path is not None]
    return resolved


def _anchored(value: str, root: Path) -> Path | None:
    """Anchor *value* to *root*, or ``None`` when it cannot be (``~nosuchuser/...``)."""
    from ..utils import anchor_path

    try:
        return anchor_path(Path(value), root)
    except ValueError:
        return None


def existing_settings_name(root: Path) -> str | None:
    """Return the ``name`` an existing settings file records, if any.

    Error-tolerant: an absent or unparsable file, or a non-string ``name``,
    yields ``None`` so the caller falls back to the directory name.
    """
    data = settings_data(root)
    name = data.get("name") if data is not None else None
    return name if isinstance(name, str) and name else None


def declared_init(root: Path) -> list[str] | None:
    """Return the init modules the settings declare.

    ``[]`` when ``init`` is omitted or empty — a repo with no init modules,
    which is legitimate. ``None`` when there is no readable settings file, or
    ``init`` is not a list of strings: the settings area reports that error.
    """
    data = settings_data(root)
    if data is None:
        return None
    modules = data.get("init", [])
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        return None
    return list(modules)
