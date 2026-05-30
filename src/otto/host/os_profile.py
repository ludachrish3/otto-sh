"""Named OS profiles: a higher-level selector layered over the host base
classes.

The ``osType`` field in lab data names an :class:`OsProfile` rather than a bare
``unix`` / ``embedded`` family. A profile records which *base* host class to
build (:class:`~otto.host.unixHost.UnixHost` or
:class:`~otto.host.embeddedHost.EmbeddedHost`) plus a bundle of *default field
values* that the storage factory merges beneath each host's own fields. This
lets many hosts that share a characteristic bundle (e.g. a particular Zephyr
build's ``command_frame`` / ``filesystem`` / ``max_filename_len``) name that
bundle once instead of copy-pasting it into every ``hosts.json`` entry.

Profiles are authorable two ways, both feeding the same registry:

- **Data** — an ``[os_profiles.<name>]`` table in ``.otto/settings.toml`` (see
  :meth:`otto.configmodule.repo.Repo._parseOsProfiles`), registered at settings
  parse time.
- **Code** — :func:`register_os_profile` called from an init module listed in
  ``.otto/settings.toml`` (the same hook
  :func:`otto.host.command_frame.register_command_frame` uses), so third-party
  libraries can ship profiles. Init modules import *after* settings parse, so a
  code registration overrides a data table of the same name (last writer wins).

The registry mirrors ``command_frame._FRAME_CLASSES`` and
``embedded_filesystem._FILESYSTEM_CLASSES``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..logger import getOttoLogger

logger = getOttoLogger()

BaseFamily = Literal['unix', 'embedded']
"""The host base class an :class:`OsProfile` builds.

``unix`` → :class:`~otto.host.unixHost.UnixHost`;
``embedded`` → :class:`~otto.host.embeddedHost.EmbeddedHost`. No new base
classes are added by the profile layer — a profile only bundles defaults over
one of these existing families.
"""

_VALID_BASES: frozenset[str] = frozenset(('unix', 'embedded'))


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
    """The ``osType`` selector this profile is registered under."""

    base: BaseFamily
    """Which base host class the profile builds (``unix`` or ``embedded``)."""

    defaults: dict[str, Any] = field(default_factory=dict)
    """Raw field defaults merged beneath a host's own ``hosts.json`` fields."""


# Registry of profile name -> profile, mirroring
# ``command_frame._FRAME_CLASSES`` / ``embedded_filesystem._FILESYSTEM_CLASSES``.
_OS_PROFILES: dict[str, OsProfile] = {}


def _slots_for_base(base: str) -> frozenset[str]:
    """Return the settable field names for *base*'s host class.

    Lazily imports the host classes to avoid an import cycle (the host modules
    do not import this one). The returned set is the same ``__slots__`` the
    storage factory filters host dicts against, so a profile key that passes
    here will not be silently dropped at build time.
    """
    if base == 'unix':
        from .unixHost import UnixHost
        return frozenset(UnixHost.__slots__)
    from .embeddedHost import EmbeddedHost
    return frozenset(EmbeddedHost.__slots__)


def register_os_profile(
    name: str,
    base: str,
    defaults: dict[str, Any] | None = None,
) -> None:
    """Register an :class:`OsProfile` so lab data can select it by ``osType``.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    Re-registering a name replaces the previous profile (last writer wins);
    overriding a built-in (``unix`` / ``embedded`` / ``zephyr``) logs a warning.

    Parameters
    ----------
    name : str
        The ``osType`` string lab-data entries will use to select this profile.
    base : str
        ``'unix'`` or ``'embedded'`` — the host class the profile builds.
    defaults : dict[str, Any] | None
        Raw field defaults merged beneath each host's own fields. Keys are
        validated against the base class's fields.

    Raises
    ------
    ValueError
        If *base* is not ``'unix'`` or ``'embedded'``, or if a ``defaults`` key
        is not a field on the base class (a likely typo).
    """
    if base not in _VALID_BASES:
        raise ValueError(
            f"register_os_profile({name!r}): base must be one of "
            f"{sorted(_VALID_BASES)}, got {base!r}"
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
    host's ``osType`` to its base family and default bundle.

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
            f"Unknown osType {name!r}. Registered profiles: {known}. "
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


# Built-in profiles. ``unix`` and ``embedded`` carry no defaults, so they build
# their base class with its stock field defaults — keeping existing lab data
# (and an absent ``osType``, which defaults to ``unix``) byte-for-byte
# unchanged. ``zephyr`` names the stock Zephyr bundle explicitly; a non-Zephyr
# embedded OS registers its own profile rather than re-overriding Zephyr-isms
# on every host.
_BUILTIN_NAMES: frozenset[str] = frozenset(('unix', 'embedded', 'zephyr'))

register_os_profile('unix', base='unix')
register_os_profile('embedded', base='embedded')
register_os_profile(
    'zephyr',
    base='embedded',
    defaults={'osName': 'Zephyr', 'command_frame': 'zephyr', 'transfer': 'console'},
)
