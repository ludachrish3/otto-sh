"""Named OS profiles: a higher-level selector layered over the host base
classes.

The ``os_type`` field in lab data selects an :class:`OsProfile`; the value is
stamped onto the constructed host's ``os_type`` attribute as the profile selector.
A profile records which *base* registered host class to build (e.g.
:class:`~otto.host.unix_host.UnixHost` or
:class:`~otto.host.embedded_host.EmbeddedHost`) plus a bundle of *default field
values* that the storage factory merges beneath each host's own fields. This
lets many hosts that share a characteristic bundle (e.g. a particular Zephyr
build's ``command_frame`` / ``filesystem`` / ``max_filename_len``) name that
bundle once instead of copy-pasting it into every ``hosts.json`` entry.

Profiles are authorable two ways, both feeding the same registry:

- **Data** — an ``[os_profiles.<name>]`` table in ``.otto/settings.toml``
  (parsed by ``Repo._parse_os_profiles``), registered at settings parse time.
- **Code** — :func:`register_os_profile` called from an init module listed in
  ``.otto/settings.toml`` (the same hook
  :func:`otto.host.command_frame.register_command_frame` uses), so third-party
  libraries can ship profiles. Init modules import *after* settings parse, so a
  code registration overrides a data table of the same name (last writer wins).

The registry mirrors ``command_frame._FRAME_CLASSES`` and
``embedded_filesystem._FILESYSTEM_CLASSES``.

A companion registry — ``_HOST_CLASSES`` / :func:`register_host_class` — maps
a name to a concrete :class:`~otto.host.remote_host.RemoteHost` subclass.
Built-in classes (``unix`` → ``UnixHost``, ``embedded`` → ``EmbeddedHost``,
``zephyr`` → ``ZephyrHost``) are registered at module load. An
:class:`OsProfile` names one of these via its ``base`` field, and registering a
class auto-registers a same-named trivial profile, so ``os_type: <name>``
resolves with no extra config.

**Registering a custom host class**

To ship a host subclass from an external repo:

1. Subclass :class:`~otto.host.embedded_host.EmbeddedHost` or
   :class:`~otto.host.unix_host.UnixHost` (whichever family fits).
2. Call ``register_host_class('myos', MyHost)`` from an init module listed
   in ``.otto/settings.toml`` — the same hook
   :func:`otto.host.command_frame.register_command_frame` uses.
3. Optionally call ``register_os_profile('myos-v1', base='myos',
   defaults={...})`` to layer a per-build data bundle (e.g. a specific
   ``command_frame``, ``max_filename_len``, or ``os_name``) over the class,
   selectable via ``os_type: myos-v1`` in ``hosts.json``.

:class:`~otto.host.embedded_host.ZephyrHost` is the in-tree worked example: it
subclasses :class:`~otto.host.embedded_host.EmbeddedHost`, declares Zephyr-
specific defaults, and is registered under ``"zephyr"`` at module load.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logger import get_otto_logger

logger = get_otto_logger()

BaseFamily = str
"""The name of a registered host class an :class:`OsProfile` builds.

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded``
(:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embedded_host.ZephyrHost`). Register more with
:func:`register_host_class`.
"""

# Registry of host-class name -> class, mirroring ``_OS_PROFILES`` /
# ``command_frame._FRAME_CLASSES``. Populated for built-ins at module load.
_HOST_CLASSES: dict[str, type] = {}


@dataclass(frozen=True)
class OsProfile:
    """A named bundle of host defaults over a base family.

    The ``defaults`` dict holds *raw* values exactly as a ``hosts.json`` entry
    would (strings for ``command_frame`` / ``filesystem``, dicts for the
    ``*_options`` tables, plain scalars otherwise). The storage factory merges
    them beneath the host's own fields and runs its existing string→instance
    coercion, so the profile never has to build typed objects itself.
    """

    name: str
    """The ``os_type`` selector this profile is registered under."""

    base: BaseFamily
    """Name of the registered host class the profile builds (e.g. ``unix``,
    ``embedded``, ``zephyr``, or a custom class registered via
    :func:`register_host_class`)."""

    defaults: dict[str, Any] = field(default_factory=dict)
    """Raw field defaults merged beneath a host's own ``hosts.json`` fields."""


# Registry of profile name -> profile, mirroring
# ``command_frame._FRAME_CLASSES`` / ``embedded_filesystem._FILESYSTEM_CLASSES``.
_OS_PROFILES: dict[str, OsProfile] = {}


def _all_slots(cls: type) -> frozenset[str]:
    """All settable field names of *cls*, gathered across its MRO.

    A ``@dataclass(slots=True)`` subclass may not repeat inherited slot names
    (Python 3.11+ adds only *new* fields to the subclass ``__slots__``), so a
    single-class ``__slots__`` lookup can miss inherited fields. The union over
    the MRO is what the storage factory filters host/profile dicts against.
    """
    names: set[str] = set()
    for klass in cls.__mro__:
        names.update(getattr(klass, '__slots__', ()))
    return frozenset(names)


def register_host_class(name: str, cls: type) -> None:
    """Register a host class so lab data can select it by ``os_type``.

    Mirrors :func:`otto.host.command_frame.register_command_frame`. Call from
    an init module listed in ``.otto/settings.toml`` to ship a custom host
    subclass. Registering a class also registers a trivial same-named
    :class:`OsProfile` (``base=name``, empty ``defaults``), so ``os_type: name``
    resolves with no extra config. Re-registering replaces the prior class.

    Raises
    ------
    ValueError
        If *cls* is not a :class:`~otto.host.remote_host.RemoteHost` subclass.
    """
    from .remote_host import RemoteHost
    if not (isinstance(cls, type) and issubclass(cls, RemoteHost)):
        raise ValueError(
            f"register_host_class({name!r}): cls must be a RemoteHost "
            f"subclass, got {cls!r}"
        )
    if name in _BUILTIN_NAMES and name in _HOST_CLASSES:
        logger.warning(
            f"register_host_class: overriding built-in host class {name!r}"
        )
    _HOST_CLASSES[name] = cls
    # Auto-register a selector profile so os_type:<name> works immediately.
    _OS_PROFILES[name] = OsProfile(name=name, base=name, defaults={})


def build_host_class(name: str) -> type:
    """Return the host class registered under *name* (raising on miss)."""
    try:
        return _HOST_CLASSES[name]
    except KeyError:
        known = ', '.join(sorted(_HOST_CLASSES))
        raise ValueError(
            f"Unknown host class {name!r}. Registered: {known}. "
            f"Add one via register_host_class()."
        ) from None


def get_host_class(name: str) -> type | None:
    """Return the host class registered under *name*, or ``None``.

    Non-raising counterpart to :func:`build_host_class`, for callers that
    produce their own error (e.g. :func:`otto.storage.factory.validate_host_dict`).
    """
    return _HOST_CLASSES.get(name)


def _slots_for_base(base: str) -> frozenset[str]:
    """Return the settable field names for the host class named *base*."""
    return _all_slots(build_host_class(base))


def register_os_profile(
    name: str,
    base: str,
    defaults: dict[str, Any] | None = None,
) -> None:
    """Register an :class:`OsProfile` so lab data can select it by ``os_type``.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    Re-registering a name replaces the previous profile (last writer wins);
    overriding a built-in (``unix`` / ``embedded`` / ``zephyr``) logs a warning.

    Parameters
    ----------
    name : str
        The ``os_type`` string lab-data entries will use to select this profile.
    base : str
        Name of a registered host class (e.g. ``'unix'`` or ``'embedded'``).
    defaults : dict[str, Any] | None
        Raw field defaults merged beneath each host's own fields. Keys are
        validated against the base class's fields.

    Raises
    ------
    ValueError
        If *base* is not a registered host class name, or if a ``defaults`` key
        is not a field on the base class (a likely typo).
    """
    if base not in _HOST_CLASSES:
        known = ', '.join(sorted(_HOST_CLASSES))
        raise ValueError(
            f"register_os_profile({name!r}): base must name a registered "
            f"host class (one of {known}), got {base!r}"
        )

    defaults = dict(defaults or {})
    slots = _slots_for_base(base)
    unknown = [k for k in defaults if k not in slots]
    if unknown:
        raise ValueError(
            f"register_os_profile({name!r}): unknown default field(s) for "
            f"base {base!r}: {sorted(unknown)}"
        )

    if name in _BUILTIN_NAMES and name in _OS_PROFILES:
        logger.warning(
            f"register_os_profile: overriding built-in profile {name!r}"
        )

    _OS_PROFILES[name] = OsProfile(name=name, base=base, defaults=defaults)


def build_os_profile(name: str) -> OsProfile:
    """Return the :class:`OsProfile` registered under *name*.

    Used by :func:`otto.storage.factory.create_host_from_dict` to resolve a
    host's ``os_type`` to its base family and default bundle.

    Raises
    ------
    ValueError
        If *name* is not registered. The error lists the registered names so a
        typo is diagnosable from the message alone.
    """
    try:
        return _OS_PROFILES[name]
    except KeyError:
        known = ', '.join(sorted(_OS_PROFILES))
        raise ValueError(
            f"Unknown os_type {name!r}. Registered profiles: {known}. "
            f"Custom profiles can be added via register_os_profile() or an "
            f"[os_profiles.<name>] table in .otto/settings.toml."
        ) from None


def get_os_profile(name: str) -> OsProfile | None:
    """Return the registered :class:`OsProfile` for *name*, or ``None``.

    Non-raising counterpart to :func:`build_os_profile`, used by
    :func:`otto.storage.factory.validate_host_dict` so validation can produce
    its own error message.
    """
    return _OS_PROFILES.get(name)


def registered_profile_names() -> list[str]:
    """Return the sorted names of all currently registered profiles."""
    return sorted(_OS_PROFILES)


# Built-in host classes. ``unix`` and ``embedded`` carry no profile defaults —
# they build their base class with its stock field defaults, keeping existing
# lab data (and an absent ``os_type``, which defaults to ``unix``) byte-for-byte
# unchanged. ``zephyr`` maps to :class:`~otto.host.embedded_host.ZephyrHost`,
# which re-declares the Zephyr-specific defaults on the class itself. Registering
# each class also auto-registers a same-named trivial :class:`OsProfile`, so
# ``os_type: <name>`` resolves with no extra config.
_BUILTIN_NAMES: frozenset[str] = frozenset(('unix', 'embedded', 'zephyr'))


def _register_builtin_host_classes() -> None:
    """Register the built-in host classes. Imported lazily to avoid an import
    cycle (the host modules do not import this one at module top)."""
    from .unix_host import UnixHost
    from .embedded_host import EmbeddedHost, ZephyrHost
    register_host_class('unix', UnixHost)
    register_host_class('embedded', EmbeddedHost)
    register_host_class('zephyr', ZephyrHost)


_register_builtin_host_classes()
