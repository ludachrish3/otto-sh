"""The one stand-in for :class:`otto.config.repo.Repo` that unit tests build.

Why this exists: tests used to stand in for a ``Repo`` with a hand-built
``SimpleNamespace(name=..., docker_settings=..., ...)``. Each such double
carried only the attributes its author's code path read on the day it was
written, so the first patch that taught a NEW reader to look at repos broke it
for reasons unrelated to what the test tests. It happened twice:

* ``inventory_settings`` -- once ``otto.inventory.config`` began reading it,
  every double that reached inventory compilation needed a hand-pinned
  ``inventory_settings={}``.
* ``project_scope`` (2026-10-03) -- once ``get_repos`` was patched where it is
  defined, ``otto.config.scope``'s owner lookup read ``.project_scope`` off the
  namespaces in ``tests/unit/docker/test_deploy.py`` and ``test_cli.py``, and
  each got a hand-added ``project_scope=None``.

:func:`fake_repo` returns a REAL ``Repo``: every dataclass field is filled from
``Repo``'s own declared default (or a placeholder for the three that have
none), so a field added to ``Repo`` tomorrow is present on every double without
anyone touching a test. Only the disk read is skipped -- ``__post_init__``
(which parses ``.otto/settings.toml``) never runs.

The ``*_settings`` views (``inventory_settings``, ``creds_settings``,
``reservation_settings``) are properties over the raw ``settings`` table, as in
production; set them through ``settings={"inventory": {...}}``.
"""

import dataclasses
from pathlib import Path
from typing import Any

from otto.config.repo import Repo
from otto.config.version import Version

_FIELDS = {f.name: f for f in dataclasses.fields(Repo)}

_PROPERTIES = frozenset(n for n in dir(Repo) if isinstance(getattr(Repo, n), property))


def _placeholders(name: str) -> dict[str, Any]:
    """Values for the ``Repo`` fields that declare no default of their own."""
    return {
        "name": name,
        "sut_dir": Path("/nonexistent/otto-fake-repo") / name,
        "version": Version("0.0.0"),
    }


_UNPLACED = sorted(
    n
    for n, f in _FIELDS.items()
    if f.default is dataclasses.MISSING
    and f.default_factory is dataclasses.MISSING
    and n not in _placeholders("r")
)
if _UNPLACED:
    raise RuntimeError(
        f"Repo gained field(s) with no default: {_UNPLACED}; "
        "give each a placeholder in tests/_fixtures/fake_repo.py"
    )


def fake_repo(name: str = "r", **overrides: Any) -> Repo:
    """Build a real :class:`Repo` without reading disk, then apply *overrides*.

    *overrides* may name any ``Repo`` dataclass field, or a method to shadow on
    this one instance (``get_lab_panel=lambda: ...``). A property is refused --
    it is derived from ``settings``, so pass that instead -- and so is a name
    ``Repo`` does not have, so a double can never carry an attribute production
    never would.
    """
    repo = object.__new__(Repo)
    placeholders = _placeholders(name)
    for field_name, f in _FIELDS.items():
        if field_name in placeholders:
            value = placeholders[field_name]
        elif f.default_factory is not dataclasses.MISSING:
            value = f.default_factory()
        else:
            value = f.default
        object.__setattr__(repo, field_name, value)
    for key, value in overrides.items():
        if key in _PROPERTIES:
            raise TypeError(
                f"Repo.{key} is a property over the raw settings table; "
                f"pass settings={{...}} instead"
            )
        is_method = not key.startswith("_") and callable(getattr(Repo, key, None))
        if key not in _FIELDS and not is_method:
            raise TypeError(f"Repo has no attribute {key!r}")
        object.__setattr__(repo, key, value)
    return repo
