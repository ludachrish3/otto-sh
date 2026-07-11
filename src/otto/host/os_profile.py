"""Named OS profiles: a higher-level selector layered over the host base classes.

The ``os_type`` field in lab data selects an :class:`OsProfile`; the value is
stamped onto the constructed host's ``os_type`` attribute as the profile selector.
A profile records which *base* registered host class to build (e.g.
:class:`~otto.host.unix_host.UnixHost` or
:class:`~otto.host.embedded_host.EmbeddedHost`) plus a bundle of *default field
values* that the host factory merges beneath each host's own fields. This
lets many hosts that share a characteristic bundle (e.g. a particular Zephyr
build's ``command_frame`` / ``filesystem`` / ``max_filename_len``) name that
bundle once instead of copy-pasting it into every ``lab.json`` entry.

Profiles are authorable two ways, both feeding the same registry:

- **Data** — an ``[os_profiles.<name>]`` table in ``.otto/settings.toml``
  (validated by ``SettingsModel`` and registered by ``Repo._register_os_profiles``),
  registered at settings parse time.
- **Code** — :func:`register_os_profile` called from an init module listed in
  ``.otto/settings.toml`` (the same hook
  :func:`otto.host.command_frame.register_command_frame` uses), so third-party
  libraries can ship profiles. Init modules import *after* settings parse, so a
  code registration overrides a data table of the same name (last writer wins).

The registry mirrors ``command_frame.FRAME_CLASSES`` and
``embedded_filesystem.FILESYSTEM_CLASSES``.

A companion registry — ``HOST_CLASSES`` / :func:`register_host_class` — maps
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
   selectable via ``os_type: myos-v1`` in ``lab.json``.

:class:`~otto.host.embedded_host.ZephyrHost` is the in-tree worked example: it
subclasses :class:`~otto.host.embedded_host.EmbeddedHost`, declares Zephyr-
specific defaults, and is registered under ``"zephyr"`` at module load.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..registry import Registry, caller_module

if TYPE_CHECKING:
    from ..models.host import HostSpec

logger = logging.getLogger(__name__)

BaseFamily = str
"""The name of a registered host class an :class:`OsProfile` builds.

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded``
(:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embedded_host.ZephyrHost`). Register more with
:func:`register_host_class`.
"""

# Registry of host-class name -> class, mirroring ``OS_PROFILES`` /
# ``command_frame.FRAME_CLASSES``. Populated for built-ins at module load.
# Registration here is always last-writer-wins (see register_host_class) —
# unlike the other backend registries, re-registering a name is documented,
# tested behavior, not a mistake to catch loudly.
HOST_CLASSES: Registry[type] = Registry(
    "host class", register_hint="otto.host.os_profile.register_host_class()"
)

# Registry of host-class name -> its boundary HostSpec subclass, populated for
# built-ins at module load alongside ``HOST_CLASSES``. Kept as a plain dict
# (not a Registry): it has no independent register_*/build_* public wrapper of
# its own — it is always written in lockstep with HOST_CLASSES from inside
# register_host_class, and tests reach into it directly via monkeypatch.setitem.
_HOST_SPECS: "dict[str, type[HostSpec]]" = {}


@dataclass(frozen=True)
class OsProfile:
    """A named bundle of host defaults over a base family.

    The ``defaults`` dict holds *raw* values exactly as a ``lab.json`` entry
    would (strings for ``command_frame`` / ``filesystem``, dicts for the
    ``*_options`` tables, plain scalars otherwise). The host factory merges
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
    """Raw field defaults merged beneath a host's own ``lab.json`` fields."""


# Registry of profile name -> profile, mirroring
# ``command_frame.FRAME_CLASSES`` / ``embedded_filesystem.FILESYSTEM_CLASSES``.
# Registration here is always last-writer-wins (see register_os_profile) — a
# re-registration is documented, tested behavior, not a mistake to catch loudly.
OS_PROFILES: Registry[OsProfile] = Registry(
    "os_type profile", register_hint="otto.host.os_profile.register_os_profile()"
)


def _all_slots(cls: type) -> frozenset[str]:
    """All settable field names of *cls*, gathered across its MRO.

    A ``@dataclass(slots=True)`` subclass may not repeat inherited slot names
    (Python 3.11+ adds only *new* fields to the subclass ``__slots__``), so a
    single-class ``__slots__`` lookup can miss inherited fields. The union over
    the MRO is what the host factory filters host/profile dicts against.
    """
    names: set[str] = set()
    for klass in cls.__mro__:
        names.update(getattr(klass, "__slots__", ()))
    return frozenset(names)


def register_host_class(
    name: str,
    cls: type,
    spec: "type[HostSpec] | None" = None,
) -> None:
    """Register a host class (and its boundary spec) so lab data can select it by ``os_type``.

    Mirrors :func:`otto.host.command_frame.register_command_frame`. Call from an
    init module listed in ``.otto/settings.toml`` to ship a custom host
    subclass. otto registers its own built-ins through this same call.

    Parameters
    ----------
    name : str
        The ``os_type`` selector to register under.
    cls : type
        A :class:`~otto.host.remote_host.RemoteHost` subclass.
    spec : type | None
        The :class:`~otto.models.host.HostSpec` subclass that validates this
        class's lab-dict shape. When ``None``, defaults to the spec registered
        for the nearest base class in *cls*'s MRO — so a subclass that adds no
        fields needs none; add fields → register a ``HostSpec`` subclass.

    Registering a class also registers a trivial same-named :class:`OsProfile`
    (``base=name``, empty ``defaults``), so ``os_type: name`` resolves with no
    extra config. Re-registering replaces the prior class and spec.

    Raises
    ------
    ValueError
        If *cls* is not a ``RemoteHost`` subclass; if *spec* is given but is not
        a ``HostSpec`` subclass; or if *spec* is ``None`` and no base class of
        *cls* has a registered spec.
    """
    from .remote_host import RemoteHost

    if not (isinstance(cls, type) and issubclass(cls, RemoteHost)):
        raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
            f"register_host_class({name!r}): cls must be a RemoteHost subclass, got {cls!r}"
        )
    if spec is None:
        spec = _nearest_registered_spec(cls)
        if spec is None:
            raise ValueError(
                f"register_host_class({name!r}): no spec given and no base "
                f"class of {cls.__name__} has a registered spec. Pass spec=."
            )
    else:
        from ..models.host import HostSpec

        if not (isinstance(spec, type) and issubclass(spec, HostSpec)):
            raise ValueError(
                f"register_host_class({name!r}): spec must be a HostSpec subclass, got {spec!r}"
            )
    if name in _BUILTIN_NAMES and name in HOST_CLASSES:
        logger.warning(f"register_host_class: overriding built-in host class {name!r}")
    # Last-writer-wins by design (see docstring) — always overwrite rather
    # than raise on re-registration.
    #
    # Three global writes with no rollback: all validation happened above, so
    # only an interpreter-level failure (KeyboardInterrupt at the wrong
    # instant) could leave the trio half-written. Registration runs at
    # bootstrap in one thread; accepting that window keeps this simple.
    HOST_CLASSES.register(name, cls, overwrite=True, origin=caller_module())
    _HOST_SPECS[name] = spec
    # Auto-register a selector profile so os_type:<name> works immediately.
    OS_PROFILES.register(
        name, OsProfile(name=name, base=name, defaults={}), overwrite=True, origin=caller_module()
    )


def _nearest_registered_spec(cls: type) -> "type[HostSpec] | None":
    """Return the spec registered for the nearest base of *cls* in its MRO."""
    by_class = {HOST_CLASSES.get(n): _HOST_SPECS[n] for n in _HOST_SPECS}
    for base in cls.__mro__:
        if base in by_class:
            return by_class[base]
    return None


def build_host_spec(name: str) -> "type[HostSpec]":
    """Return the ``HostSpec`` subclass registered under host-class *name* (raises on miss)."""
    try:
        return _HOST_SPECS[name]
    except KeyError:
        known = ", ".join(sorted(_HOST_SPECS))
        raise ValueError(
            f"No host spec registered for {name!r}. Registered: {known}. "
            f"Add one via register_host_class()."
        ) from None


def registered_host_specs(*, builtins_only: bool = False) -> "dict[str, type[HostSpec]]":
    """Return a shallow copy of the ``os_type`` → ``HostSpec`` subclass registry.

    Names are many-to-one (``embedded`` and ``zephyr`` both resolve to
    :class:`~otto.models.host.EmbeddedHostSpec`). Used by the JSON Schema exporter;
    also reflects custom classes loaded via init modules. With *builtins_only*, restrict the
    result to the in-tree built-in types (``unix`` / ``embedded`` / ``zephyr``),
    excluding anything registered via init modules.
    """
    if builtins_only:
        return {n: s for n, s in _HOST_SPECS.items() if n in _BUILTIN_NAMES}
    return dict(_HOST_SPECS)


def build_host_class(name: str) -> type:
    """Return the host class registered under *name* (raising on miss)."""
    return HOST_CLASSES.get(name)


def get_host_class(name: str) -> type | None:
    """Return the host class registered under *name*, or ``None``.

    Non-raising counterpart to :func:`build_host_class`, for callers that
    produce their own error (e.g. :func:`otto.host.factory.validate_host_dict`).
    """
    return HOST_CLASSES.get(name) if name in HOST_CLASSES else None


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
    if base not in HOST_CLASSES:
        known = ", ".join(HOST_CLASSES.names())
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

    if name in _BUILTIN_NAMES and name in OS_PROFILES:
        logger.warning(f"register_os_profile: overriding built-in profile {name!r}")

    # Last-writer-wins by design (see docstring) — always overwrite rather
    # than raise on re-registration.
    OS_PROFILES.register(
        name,
        OsProfile(name=name, base=base, defaults=defaults),
        overwrite=True,
        origin=caller_module(),
    )


def build_os_profile(name: str) -> OsProfile:
    """Return the :class:`OsProfile` registered under *name*.

    Used by :func:`otto.host.factory.create_host_from_dict` to resolve a
    host's ``os_type`` to its base family and default bundle.

    Raises
    ------
    ValueError
        If *name* is not registered. The error lists the registered names so a
        typo is diagnosable from the message alone.
    """
    return OS_PROFILES.get(name)


def get_os_profile(name: str) -> OsProfile | None:
    """Return the registered :class:`OsProfile` for *name*, or ``None``.

    Non-raising counterpart to :func:`build_os_profile`, used by
    :func:`otto.host.factory.validate_host_dict` so validation can produce
    its own error message.
    """
    return OS_PROFILES.get(name) if name in OS_PROFILES else None


def registered_profile_names() -> list[str]:
    """Return the sorted names of all currently registered profiles."""
    return sorted(OS_PROFILES.names())


# Built-in host classes. ``unix`` and ``embedded`` carry no profile defaults —
# they build their base class with its stock field defaults, keeping existing
# lab data (and an absent ``os_type``, which defaults to ``unix``) byte-for-byte
# unchanged. ``zephyr`` maps to :class:`~otto.host.embedded_host.ZephyrHost`,
# which re-declares the Zephyr-specific defaults on the class itself. Registering
# each class also auto-registers a same-named trivial :class:`OsProfile`, so
# ``os_type: <name>`` resolves with no extra config.
_BUILTIN_NAMES: frozenset[str] = frozenset(("unix", "embedded", "zephyr"))


def _register_builtin_host_classes() -> None:
    """Register the built-in host classes and their boundary specs.

    Imported lazily to avoid an import cycle (the host/spec modules do not
    import this one at module top).
    """
    from ..models.host import EmbeddedHostSpec, UnixHostSpec
    from .embedded_host import EmbeddedHost, ZephyrHost
    from .unix_host import UnixHost

    register_host_class("unix", UnixHost, UnixHostSpec)
    register_host_class("embedded", EmbeddedHost, EmbeddedHostSpec)
    register_host_class("zephyr", ZephyrHost, EmbeddedHostSpec)


_register_builtin_host_classes()
