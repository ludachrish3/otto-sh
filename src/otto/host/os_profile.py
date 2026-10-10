"""Host classes and the OS profiles layered over them.

The ``os_type`` field in lab data selects an :class:`OsProfile`; the value is
stamped onto the constructed host's ``os_type`` attribute as the profile
selector. A profile records which *base* registered host class to build (e.g.
:class:`~otto.host.unix_host.UnixHost` or
:class:`~otto.host.embedded_host.EmbeddedHost`) plus its
:class:`ProfileFields`: *default field values* the host factory merges beneath
each host's own fields, and the console prompts. This lets many hosts that
share a characteristic bundle (e.g. a particular Zephyr build's
``command_frame`` / ``filesystem`` / ``max_filename_len``) name that bundle
once instead of copy-pasting it into every ``lab.json`` entry.

``HOST_CLASSES`` / :func:`register_host_class` map a name to a concrete
:class:`~otto.host.remote_host.RemoteHost` subclass, its boundary
:class:`~otto.models.host.HostSpec`, and the class's own profile fields.
Built-in classes (``unix`` → ``UnixHost``, ``embedded`` → ``EmbeddedHost``,
``zephyr`` → ``ZephyrHost``) are registered at module load, by
:class:`~otto.registry.Ref`, so naming them imports no host class.

A name resolves through three layers, and the first layer holding it supplies
the WHOLE profile (:func:`resolve_os_profile`):

1. **Code** — :func:`register_os_profile`, called from an init module listed in
   ``.otto/settings.toml``, so third-party libraries can ship profiles.
2. **Repo data** — an ``[os_profiles.<name>]`` table in a repo's
   ``.otto/settings.toml``. Settings parsing keeps the table as data
   (:attr:`otto.config.repo.Repo.os_profiles`); it is never registered. A
   :class:`ProfileContext` built from the selected repos carries the tables
   to every reader, and the later repo in ``OTTO_SUT_DIRS`` wins a name two
   repos declare. Each table is checked after every init module has run
   (:func:`check_data_profiles`), so a table may name a host class an init
   module registers.
3. **Class or built-in** — a registered host class's name is its own profile
   (its :attr:`HostClassEntry.profile`), and ``busybox`` is a built-in
   defaults-only profile over ``unix`` (:data:`BUILTIN_PROFILES`).

**Registering a custom host class**

1. Subclass :class:`~otto.host.embedded_host.EmbeddedHost` or
   :class:`~otto.host.unix_host.UnixHost` (whichever family fits).
2. Call ``register_host_class('myos', MyHost)`` from an init module listed
   in ``.otto/settings.toml`` — the same hook
   :func:`otto.host.command_frame.register_command_frame` uses.
3. Optionally call ``register_os_profile('myos-v1', base='myos',
   defaults={...})``, or declare an ``[os_profiles.myos-v1]`` table, to layer a
   per-build data bundle over the class, selectable via ``os_type: myos-v1``.

:class:`~otto.host.embedded_host.ZephyrHost` is the in-tree worked example: it
subclasses :class:`~otto.host.embedded_host.EmbeddedHost`, declares Zephyr-
specific defaults, and is registered under ``"zephyr"`` at module load.
"""

import contextlib
import dataclasses
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from ..registry import FrozenMap, Ref, Registry, registration_boundary, resolved

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..models.host import HostSpec
    from ..models.settings import OsProfileSpec
    from .options import ConsoleOptions
    from .remote_host import RemoteHost

logger = logging.getLogger(__name__)

_H = TypeVar("_H")

BaseFamily = str
"""The name of a registered host class an :class:`OsProfile` builds.

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded``
(:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embedded_host.ZephyrHost`). Register more with
:func:`register_host_class`.
"""

UNIX_LOGIN_PROMPT = r"login: ?$"
"""What a getty prints, with or without a hostname prefix (``test2 login:``)."""

UNIX_PASSWORD_PROMPT = r"[Pp]assword: ?$"  # noqa: S105 — a regex pattern, not a credential
"""``login(1)``'s prompt; BusyBox and shadow spell the case differently."""


@dataclass(frozen=True)
class ProfileFields:
    """What a profile supplies a host: field defaults and the console prompts."""

    defaults: "FrozenMap[str, object]" = dataclasses.field(default_factory=FrozenMap)
    """Raw field defaults merged beneath a host's own ``lab.json`` fields.

    Held exactly as a ``lab.json`` entry would hold them (strings for
    ``command_frame`` / ``filesystem``, tables for the ``*_options``, plain
    scalars otherwise), frozen with :meth:`~otto.registry.FrozenMap.freeze_json`.
    The host factory thaws and merges them, then runs its usual
    string-to-instance coercion, so a profile never builds typed objects."""

    login_prompt: str | None = None
    """Regex the ``console`` term matches against the end of the line to
    recognise this OS's login prompt; ``None`` for an OS with no login
    (an RTOS shell)."""

    password_prompt: str | None = None
    """Regex for this OS's password prompt; ``None`` with ``login_prompt``."""


@dataclass(frozen=True)
class HostClassEntry:
    """The record ``HOST_CLASSES`` holds for one host class name."""

    cls: "type[RemoteHost] | Ref"
    """The host class; a ``Ref`` is imported at the entry's first lookup."""

    spec: "type[HostSpec] | Ref"
    """The boundary spec that validates the class's lab-dict shape."""

    profile: ProfileFields = ProfileFields()
    """The class's own profile, which ``os_type: <name>`` selects when no code
    or repo-data profile of that name exists."""


@dataclass(frozen=True)
class OsProfile:
    """A named profile over a base host class: the whole of what one ``os_type`` selects."""

    name: str
    """The ``os_type`` selector this profile answers to."""

    base: BaseFamily
    """Name of the registered host class the profile builds (e.g. ``unix``,
    ``embedded``, ``zephyr``, or a custom class registered via
    :func:`register_host_class`)."""

    fields: ProfileFields = ProfileFields()
    """The defaults and prompts the profile supplies."""


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


def _check_prompts(where: str, login_prompt: str | None, password_prompt: str | None) -> None:
    """Refuse a prompt that is not a valid regex; *where* names the registration."""
    for field_name, pattern in (
        ("login_prompt", login_prompt),
        ("password_prompt", password_prompt),
    ):
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{where}: {field_name} is not a valid regex: {exc}") from exc


def _validate_host_class(name: str, cls: type) -> None:
    """Refuse anything but a capable :class:`~otto.host.remote_host.RemoteHost` subclass."""
    from .capability_grid import HostCapabilities
    from .remote_host import RemoteHost

    if not (isinstance(cls, type) and issubclass(cls, RemoteHost)):
        raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
            f"register_host_class({name!r}): cls must be a RemoteHost subclass, got {cls!r}"
        )
    # isinstance, not hasattr: BaseHost carries the ClassVar ANNOTATION and no
    # value, which creates no attribute but would satisfy a name check on a
    # subclass — and a class declaring some other object under the name would
    # promise nothing the guide page or the conformance surfaces can read. Same
    # reasoning as ``transfer/registry.py``'s ``progress_granularity`` check.
    if not isinstance(getattr(cls, "capabilities", None), HostCapabilities):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see the RemoteHost check above)
            f"register_host_class({name!r}): cls.capabilities is missing; a host "
            f"class must declare what its verbs promise for user=, progress and "
            f"session identity (e.g. capabilities = HostCapabilities(...) — see "
            f"otto.host.capability_grid)."
        )


def _validate_host_spec(name: str, spec: object) -> None:
    """Refuse a spec that is not a :class:`~otto.models.host.HostSpec` subclass."""
    from ..models.host import HostSpec

    if not (isinstance(spec, type) and issubclass(spec, HostSpec)):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see _validate_host_class)
            f"register_host_class({name!r}): spec must be a HostSpec subclass, got {spec!r}"
        )


def _check_host_class_entry(name: str, entry: HostClassEntry) -> None:
    """``HOST_CLASSES``' one check, run at registration and again once its ``Ref`` fields resolve.

    A field still holding a :class:`~otto.registry.Ref` is skipped (it is
    checked when it resolves): the class must be a capable
    :class:`~otto.host.remote_host.RemoteHost` subclass, the spec a
    :class:`~otto.models.host.HostSpec` subclass, the profile's defaults
    fields of the class, and its prompts valid regexes.
    """
    profile = entry.profile
    if not isinstance(profile, ProfileFields):
        raise TypeError(
            f"register_host_class({name!r}): profile must be a ProfileFields, "
            f"got {type(profile).__name__}"
        )
    cls = entry.cls
    if not isinstance(cls, Ref):
        _validate_host_class(name, cls)
        unknown = sorted(k for k in profile.defaults if k not in _all_slots(cls))
        if unknown:
            raise ValueError(
                f"register_host_class({name!r}): unknown default field(s) for "
                f"{cls.__name__}: {unknown}"
            )
    if not isinstance(entry.spec, Ref):
        _validate_host_spec(name, entry.spec)
    _check_prompts(f"register_host_class({name!r})", profile.login_prompt, profile.password_prompt)


HOST_CLASSES: Registry[HostClassEntry] = Registry(
    "host class",
    entry=HostClassEntry,
    register_hint="otto.host.os_profile.register_host_class()",
    validate=lambda n, e, _p: _check_host_class_entry(n, e),
    check_resolved=_check_host_class_entry,
)
"""Every host class lab data can select, by name."""


def profile_vocabulary(base: str) -> frozenset[str]:
    """Return the field names a profile over host class *base* may default.

    Raises:
        ValueError: *base* names no registered host class.
        Exception: resolving a by-reference class failed (an import error, or
            the class's own resolution check); the caller wraps it.
    """
    return _all_slots(resolved(HOST_CLASSES.get(base).cls))


def check_os_profile(
    name: str,
    base: str,
    defaults: "Mapping[str, object] | None" = None,
    *,
    login_prompt: str | None = None,
    password_prompt: str | None = None,
) -> None:
    """Raise every ``ValueError`` a profile named *name* over *base* earns, and touch no registry.

    The one meaning check for a profile, wherever it comes from:
    :func:`register_os_profile` runs it, so does :func:`check_data_profiles`
    for every repo table, and so does :func:`resolve_os_profile` for a repo
    table it selects. *base* must name a registered host class that resolves
    (a class registered by reference is imported and checked here), every
    *defaults* key must be a field of that class, and each prompt must be a
    valid regex.
    """
    _check_profile_meaning(
        f"register_os_profile({name!r})", base, defaults, login_prompt, password_prompt
    )


def _check_profile_meaning(
    where: str,
    base: str,
    defaults: "Mapping[str, object] | None",
    login_prompt: str | None,
    password_prompt: str | None,
) -> None:
    """Run the checks of :func:`check_os_profile`, naming the profile *where* in each message.

    Looking up *base* may resolve a class registered by reference, which
    imports its module and runs the class's own checks. Any failure there is
    raised as a ``ValueError`` naming the base and the module that registered
    it (the cause chained), so every caller's ``ValueError`` containment holds
    and the error points at the class's author, not only at this profile.
    """
    if base not in HOST_CLASSES:
        known = ", ".join(HOST_CLASSES.names())
        raise ValueError(
            f"{where}: base must name a registered host class (one of {known}), got {base!r}"
        )
    try:
        vocabulary = profile_vocabulary(base)
    except Exception as exc:
        raise ValueError(
            f"{where}: base {base!r} (host class registered by {HOST_CLASSES.origin(base)}): "
            f"resolution failed: {type(exc).__name__}: {exc}"
        ) from exc
    unknown = [k for k in (defaults or {}) if k not in vocabulary]
    if unknown:
        raise ValueError(f"{where}: unknown default field(s) for base {base!r}: {sorted(unknown)}")
    _check_prompts(where, login_prompt, password_prompt)


def _check_code_profile(name: str, entry: OsProfile, _proposed: object) -> None:
    """``OS_PROFILES``' validate: the record is named for its key and passes the meaning check."""
    if entry.name != name:
        raise ValueError(
            f"register_os_profile({name!r}): the profile record is named {entry.name!r}"
        )
    check_os_profile(
        name,
        entry.base,
        entry.fields.defaults.thaw_json(),
        login_prompt=entry.fields.login_prompt,
        password_prompt=entry.fields.password_prompt,
    )


OS_PROFILES: Registry[OsProfile] = Registry(
    "os_type profile",
    entry=OsProfile,
    register_hint="otto.host.os_profile.register_os_profile()",
    validate=_check_code_profile,
)
"""The code profiles (:func:`register_os_profile`): the first layer a name resolves in."""


def _builtin_profiles() -> "FrozenMap[str, OsProfile]":
    """Return the built-in defaults-only profiles: ``busybox``, a unix host.

    What is here and what is NOT is the whole design. A BusyBox box is a unix
    host whose *userland* differs, and those differences are measured at runtime
    by :class:`~otto.host.userland.Userland` (elevation, timeout syntax, base64
    spelling, stat spelling, shell dialect). Probed answers must not be
    duplicated as declared defaults: a declaration in ``userland_options``
    skips the probe entirely, so a wrong guess here would be unfixable from the
    device itself — the profile carries none.

    That leaves the facts probing cannot discover, which gate whole code paths:

    ``has_bash=False``
        A stock BusyBox ships no bash. This is not cosmetic —
        :mod:`otto.tunnel.discovery` scans only ``has_bash`` hosts (it builds
        its process list from ``[h for h in lab.hosts.values() if h.has_bash]``),
        and detached command tagging goes through
        :func:`otto.host.daemon.launch_command`'s ``bash -c 'exec -a …'`` —
        ``exec -a`` is a bash builtin. Left at the unix default of
        ``True``, ``otto.tunnel.manage.resolve_chain`` would accept the host
        as a tunnel path member and then emit a bash-only launch command to a
        shell that cannot run it.

    ``command_frame="ash"``
        A truthful name for the shell. `AshFrame` overrides nothing —
        its rendered payloads (handshake, frame, recover, quiet_history) are
        byte-identical to `BashFrame`'s, measured both under real BusyBox ash
        across the version matrix (``tests/integration/busybox_bed/test_session_frame.py``)
        and directly against `BashFrame`'s output
        (``test_ash_inherits_bashs_marker_scheme_rather_than_restating_it`` in
        ``tests/unit/host/test_command_frame.py``). So this changes no bytes on
        the wire today; it labels the host correctly in diagnostics and gives a
        future ash-only divergence a home.

    ``transfer`` defaults to ``"shell"`` (:mod:`otto.host.transfer.shell`),
    the phase 4 backend built for exactly this device class: PUT, GET, and
    integrity verification all move bytes with nothing but command
    execution — no ``scp``, ``sftp-server``, or ``nc`` required on the
    device. That default exists *because* a real BusyBox device typically
    runs **dropbear** in place of OpenSSH — a separate project, not a
    BusyBox applet itself (measured: ``busybox-1.35.0-x86_64 --list`` names
    none of its 402 applets ``sshd``/``ssh``/``scp``/``sftp``/``dropbear``)
    — and dropbear ships no ``sftp-server`` (``docs/superpowers/specs/
    2026-08-11-busybox-host-support-design.md``, "The dropbear risk"). A
    2012-era dropbear (SHA-1-only key exchange, ``ssh-rsa``, ``hmac-sha1``)
    is measured on the bb1350 bed guest (``docs/architecture/subsystems/
    busybox-bed.md``): stock ``ssh_options`` negotiate it, and a per-host
    ``kex_algs`` or ``encryption_algs`` reaches the wire for a device that
    needs a narrower list; which algorithms need an ``ssh_options`` list at
    all is ``docs/architecture/ssh-algorithms.md``. ``scp``/``sftp``/``ftp``/
    ``nc`` stay in ``valid_transfers`` even so — a lab entry whose device
    runs a real OpenSSH-compatible server opts into one by pinning
    ``transfer`` itself.
    **Keeping ``scp`` here is what makes the refusal a question about the
    DEVICE rather than about this profile.**
    ``otto.host.transfer.scp.refuse_if_scp_is_absent`` declines a transfer
    only where the device answered that it has no ``scp`` applet, so a
    BusyBox box with a real ``scp`` installed alongside keeps working; a
    profile-level prune would refuse that host on the strength of its
    ``os_type`` and nothing else. The other three are unguarded and stay
    that way here: ``sftp``/``ftp``/``nc`` reach the device with no upfront
    probe, so their failure lands at transfer time on the real device
    rather than at the cheaper host-build time where a wrong
    ``command_frame`` or ``has_bash`` would be caught. ``shell`` as the
    *default* is what avoids that exposure for the common case.

    Naming a backend here is validated shallowly by design:
    :func:`register_os_profile`'s checks cover only that ``defaults``'s *keys*
    are fields on the base class, never that its *values* make sense — a
    typo'd or unregistered transfer name would register cleanly and only
    surface later, at host-build time, not here.
    ``TestBusyBoxProfile.test_busybox_names_the_shell_transfer_backend_and_it_is_registered``
    (``tests/unit/host/test_os_profile.py``) closes that gap for
    ``transfer`` the same way
    ``test_the_frame_the_profile_names_is_actually_registered`` closes it
    for ``command_frame``: asserting not just the name but that the named
    backend is actually registered in ``TRANSFER_BACKENDS``.
    """
    return FrozenMap(
        {
            "busybox": OsProfile(
                "busybox",
                "unix",
                ProfileFields(
                    FrozenMap.freeze_json(
                        {
                            "has_bash": False,
                            "command_frame": "ash",
                            "transfer": "shell",
                            "valid_transfers": ["shell", "scp", "sftp", "ftp", "nc"],
                        }
                    ),
                    login_prompt=UNIX_LOGIN_PROMPT,
                    password_prompt=UNIX_PASSWORD_PROMPT,
                ),
            )
        }
    )


BUILTIN_PROFILES: "FrozenMap[str, OsProfile]" = _builtin_profiles()
"""otto's defaults-only profiles, the last layer a name resolves in."""


def _builtin_host_classes() -> "list[tuple[str, HostClassEntry]]":
    """Return the built-in host classes and their specs, by reference.

    Neither the classes nor the pydantic specs are imported here: each is a
    :class:`~otto.registry.Ref`, imported on its first lookup, so listing the
    built-in ``os_type`` names costs no host or model import. ``unix``
    carries the getty prompts as its own profile; ``embedded`` and
    ``zephyr`` carry none (an RTOS shell has no login).
    """
    unix = ProfileFields(login_prompt=UNIX_LOGIN_PROMPT, password_prompt=UNIX_PASSWORD_PROMPT)
    return [
        (
            "unix",
            HostClassEntry(
                Ref("otto.host.unix_host:UnixHost"), Ref("otto.models.host:UnixHostSpec"), unix
            ),
        ),
        (
            "embedded",
            HostClassEntry(
                Ref("otto.host.embedded_host:EmbeddedHost"),
                Ref("otto.models.host:EmbeddedHostSpec"),
            ),
        ),
        (
            "zephyr",
            HostClassEntry(
                Ref("otto.host.embedded_host:ZephyrHost"),
                Ref("otto.models.host:EmbeddedHostSpec"),
            ),
        ),
    ]


_BUILTIN_CLASS_NAMES: frozenset[str] = frozenset(name for name, _ in _builtin_host_classes())
_BUILTIN_NAMES: frozenset[str] = _BUILTIN_CLASS_NAMES | frozenset(BUILTIN_PROFILES)


def _ref_names(ref: Ref, cls: type) -> bool:
    """Return whether *ref* names *cls* (compared by target; nothing is imported)."""
    return ref.target == f"{cls.__module__}:{cls.__qualname__}"


def _nearest_registered_spec(cls: type) -> "type[HostSpec] | None":
    """Return the spec registered for the nearest base of *cls* in its MRO.

    Compares each record's class with the bases, a ``Ref`` by its target, so
    nothing is imported but the spec of the record that matches.
    """
    records = HOST_CLASSES.raw_items()
    for base in cls.__mro__:
        for name, entry in records:
            held = entry.cls
            if held is base or (isinstance(held, Ref) and _ref_names(held, base)):
                return resolved(HOST_CLASSES.get(name).spec)
    return None


def _resolve_host_spec(
    name: str, cls: type, spec: "type[HostSpec] | Ref | None"
) -> "type[HostSpec] | Ref":
    """Return the spec :func:`register_host_class` would store for *cls*, or raise its refusal.

    An explicit *spec* class must be a :class:`~otto.models.host.HostSpec`
    subclass (a ``Ref`` is checked when it resolves); without one, the nearest
    base of *cls* with a registered spec supplies it. Registers nothing, so
    ``otto.testing.assert_host_registrable`` asks the same question without
    registering.

    Raises:
        ValueError: *spec* is not a ``HostSpec`` subclass, or it is ``None``
            and no base class of *cls* has a registered spec.
    """
    if spec is None:
        inherited = _nearest_registered_spec(cls)
        if inherited is None:
            raise ValueError(
                f"register_host_class({name!r}): no spec given and no base "
                f"class of {cls.__name__} has a registered spec. Pass spec=."
            )
        return inherited
    if not isinstance(spec, Ref):
        _validate_host_spec(name, spec)
    return spec


@registration_boundary
def register_host_class(
    name: str,
    cls: "type[RemoteHost] | Ref",
    *,
    spec: "type[HostSpec] | Ref | None" = None,
    profile: ProfileFields | None = None,
    overwrite: bool = False,
) -> None:
    """Register a host class (its boundary spec and its own profile) so lab data can select it.

    Call from an init module listed in ``.otto/settings.toml`` to ship a custom
    host subclass. ``os_type: <name>`` then selects the class with *profile*
    (its defaults and prompts) unless a code or repo-data profile of that
    name exists; registering a class registers no :class:`OsProfile`.

    Parameters
    ----------
    name : str
        The ``os_type`` selector to register under.
    cls : type | Ref
        A :class:`~otto.host.remote_host.RemoteHost` subclass, or a
        :class:`~otto.registry.Ref` naming one (checked when first looked up).
    spec : type | Ref | None
        The :class:`~otto.models.host.HostSpec` subclass that validates this
        class's lab-dict shape. When ``None``, defaults to the spec registered
        for the nearest base class in *cls*'s MRO — so a subclass that adds no
        fields needs none; add fields → register a ``HostSpec`` subclass. A
        class given by reference needs *spec*.
    profile : ProfileFields | None
        The class's own defaults and console prompts; none by default.
    overwrite : bool
        Replace an existing registration of *name* deliberately (e.g. a
        built-in); by default a taken name raises.

    Overriding a built-in name (``unix`` / ``embedded`` / ``zephyr`` /
    ``busybox``) logs a warning.

    Raises
    ------
    DuplicateRegistration
        If *name* is registered and *overwrite* is false.
    ValueError
        If *cls* is not a ``RemoteHost`` subclass or declares no
        :class:`~otto.host.capability_grid.HostCapabilities` under
        ``capabilities``; if *spec* is given but is not a ``HostSpec``
        subclass; if *spec* is ``None`` and no base class of *cls* has a
        registered spec; or if *profile* defaults a field the class lacks or
        holds a prompt that is not a valid regex.
    """
    if isinstance(cls, Ref):
        if spec is None:
            raise ValueError(
                f"register_host_class({name!r}): a class given by reference needs spec="
            )
        stored_spec: "type[HostSpec] | Ref" = spec
    else:
        # Checked before the spec lookup, which walks cls's MRO and so needs a
        # host class; the registry's own check runs the same rule again.
        _validate_host_class(name, cls)
        stored_spec = _resolve_host_spec(name, cls, spec)
    shadows = name in _BUILTIN_NAMES and (name in HOST_CLASSES or name in BUILTIN_PROFILES)
    HOST_CLASSES.register(
        name,
        HostClassEntry(cls, stored_spec, profile if profile is not None else ProfileFields()),
        overwrite=overwrite,
    )
    if shadows:
        logger.warning(f"register_host_class: overriding built-in host class {name!r}")


@registration_boundary
def register_os_profile(
    name: str,
    base: str,
    defaults: "Mapping[str, object] | None" = None,
    *,
    login_prompt: str | None = None,
    password_prompt: str | None = None,
    overwrite: bool = False,
) -> None:
    """Register a code :class:`OsProfile` so lab data can select it by ``os_type``.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows. A
    code profile is the first layer :func:`resolve_os_profile` reads, so it
    wins over a repo's ``[os_profiles]`` table and over a host class of the
    same name. Shadowing a built-in (``unix`` / ``embedded`` / ``zephyr`` /
    ``busybox``) logs a warning.

    Parameters
    ----------
    name : str
        The ``os_type`` string lab-data entries will use to select this profile.
    base : str
        Name of a registered host class (e.g. ``'unix'`` or ``'embedded'``).
    defaults : Mapping[str, object] | None
        Raw field defaults merged beneath each host's own fields. Keys are
        validated against the base class's fields.
    login_prompt : str | None
        Regex for this OS's login prompt (see :attr:`ProfileFields.login_prompt`).
    password_prompt : str | None
        Regex for this OS's password prompt (see
        :attr:`ProfileFields.password_prompt`).
    overwrite : bool
        Replace an existing code profile of *name* deliberately; by default a
        taken name raises.

    Raises
    ------
    DuplicateRegistration
        If a code profile named *name* exists and *overwrite* is false.
    ValueError
        Raised by :func:`check_os_profile`: if *base* is not a registered host
        class name; if a ``defaults`` key is not a field on the base class (a
        likely typo); or if ``login_prompt``/``password_prompt`` is not a
        valid regex.
    """
    shadows = name in _BUILTIN_NAMES and (name in BUILTIN_PROFILES or name in HOST_CLASSES)
    OS_PROFILES.register(
        name,
        OsProfile(
            name,
            base,
            ProfileFields(
                FrozenMap.freeze_json(dict(defaults or {})), login_prompt, password_prompt
            ),
        ),
        overwrite=overwrite,
    )
    if shadows:
        logger.warning(f"register_os_profile: overriding built-in profile {name!r}")


# --- repo data profiles ------------------------------------------------------


def _profile_from_spec(name: str, spec: "OsProfileSpec") -> OsProfile:
    """Return the record for one parsed ``[os_profiles.<name>]`` table (it carries no prompts)."""
    return OsProfile(name, spec.base, ProfileFields(FrozenMap.freeze_json(spec.defaults)))


@dataclass(frozen=True)
class ProfileContext:
    """The repo data profiles one set of selected repos declares.

    Built after init from the repos in ``OTTO_SUT_DIRS`` order
    (:meth:`from_repos`) and handed to every reader that resolves an
    ``os_type``; frozen and hashable, so a cache can key on it.
    """

    profiles: "FrozenMap[str, OsProfile]"
    """Every table by name, merged across the repos; a later repo wins a name."""

    owners: "FrozenMap[str, str]"
    """The repo whose table each name resolves to, named in messages."""

    @classmethod
    def empty(cls) -> "ProfileContext":
        """Return the context with no data profiles."""
        return cls(profiles=FrozenMap(), owners=FrozenMap())

    @classmethod
    def from_repos(cls, repos: "Sequence[Repo]") -> "ProfileContext":
        """Merge the ``[os_profiles]`` tables of *repos*, in the given order (later wins)."""
        profiles: dict[str, OsProfile] = {}
        owners: dict[str, str] = {}
        for repo in repos:
            for name, spec in repo.os_profiles.items():
                profiles[name] = _profile_from_spec(name, spec)
                owners[name] = repo.name
        return cls(profiles=FrozenMap(profiles), owners=FrozenMap(owners))

    def digest(self) -> str:
        """Return the sha256 of the profiles' canonical JSON, stable across processes."""
        canonical = {
            name: [
                p.base,
                p.fields.defaults.thaw_json(),
                p.fields.login_prompt,
                p.fields.password_prompt,
            ]
            for name, p in sorted(self.profiles.items())
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _check_data_profile(profile: OsProfile, *, owner: str) -> None:
    """Run the meaning check on one repo table, naming the table and its repo."""
    _check_profile_meaning(
        f"[os_profiles.{profile.name}] in repo {owner!r}",
        profile.base,
        profile.fields.defaults.thaw_json(),
        profile.fields.login_prompt,
        profile.fields.password_prompt,
    )


def check_data_profiles(repos: "Sequence[Repo]") -> None:
    """Check every ``[os_profiles]`` table of every repo in *repos*; idempotent.

    Run once every init module has run, so a table may name a host class an
    init module registers. Every table is checked, not only the one a name
    resolves to: a typo in a shadowed table is still a typo.

    Raises:
        ValueError: A table names an unregistered base or one whose host class
            does not resolve, defaults a field its base lacks, or holds a bad
            prompt; the message names the table and its repo.
    """
    for repo in repos:
        for name, spec in repo.os_profiles.items():
            _check_data_profile(_profile_from_spec(name, spec), owner=repo.name)


# --- the resolver -------------------------------------------------------------


def _layer_names(data: ProfileContext) -> list[str]:
    """Every name some layer resolves, built-ins first, each once."""
    names = [*HOST_CLASSES.names(), *BUILTIN_PROFILES, *OS_PROFILES.names(), *data.profiles]
    return list(dict.fromkeys(names))


def resolve_os_profile(name: str, *, data: ProfileContext) -> OsProfile:
    """Return the profile *name* selects: the first layer holding it supplies the whole profile.

    Code profiles first, then the repo data in *data* (checked as it is
    selected), then a host class's own profile or a built-in.

    Raises:
        ValueError: No layer holds *name* (the message lists every name and
            suggests a near miss), or the repo table it selects fails
            :func:`check_os_profile` (the message names the table and repo).
    """
    if name in OS_PROFILES:
        return OS_PROFILES.get(name)
    if name in data.profiles:
        profile = data.profiles[name]
        _check_data_profile(profile, owner=data.owners[name])
        return profile
    if name in HOST_CLASSES:
        return OsProfile(name, name, HOST_CLASSES.peek(name).profile)
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    import difflib

    names = _layer_names(data)
    close = difflib.get_close_matches(name, names, n=1)
    suggestion = f" Did you mean {close[0]!r}?" if close else ""
    raise ValueError(
        f"Unknown os_type profile {name!r}.{suggestion} Registered: {', '.join(names)}. "
        f"Custom entries can be added via otto.host.os_profile.register_os_profile()."
    )


def build_os_profile(name: str, *, data: ProfileContext | None = None) -> OsProfile:
    """Return the :class:`OsProfile` *name* selects, seeing repo data only through *data*.

    Raises:
        ValueError: As :func:`resolve_os_profile` does.
    """
    return resolve_os_profile(name, data=data if data is not None else ProfileContext.empty())


def get_os_profile(name: str, *, data: ProfileContext | None = None) -> OsProfile | None:
    """Return the :class:`OsProfile` *name* selects, or ``None`` when no layer holds it.

    Non-raising for an unknown name, so a caller can produce its own error
    (e.g. :func:`otto.host.factory.validate_host_dict`); a repo table it
    selects that fails its check still raises.
    """
    context = data if data is not None else ProfileContext.empty()
    if name not in _layer_names(context):
        return None
    return resolve_os_profile(name, data=context)


def registered_profile_names(*, data: ProfileContext | None = None) -> list[str]:
    """Return the sorted names every layer resolves, repo data only through *data*."""
    return sorted(_layer_names(data if data is not None else ProfileContext.empty()))


# --- host classes, read -------------------------------------------------------


def build_host_spec(name: str) -> "type[HostSpec]":
    """Return the ``HostSpec`` subclass registered under host-class *name* (raises on miss)."""
    if name not in HOST_CLASSES:
        known = ", ".join(sorted(HOST_CLASSES.names()))
        raise ValueError(
            f"No host spec registered for {name!r}. Registered: {known}. "
            f"Add one via register_host_class()."
        )
    return resolved(HOST_CLASSES.get(name).spec)


def registered_host_specs(*, builtins_only: bool = False) -> "dict[str, type[HostSpec]]":
    """Return the ``os_type`` → ``HostSpec`` subclass map of every registered host class.

    Names are many-to-one (``embedded`` and ``zephyr`` both resolve to
    :class:`~otto.models.host.EmbeddedHostSpec`). Used by the JSON Schema exporter;
    also reflects custom classes loaded via init modules. With *builtins_only*, restrict the
    result to the in-tree built-in types (``unix`` / ``embedded`` / ``zephyr``),
    excluding anything registered via init modules.
    """
    return {
        n: build_host_spec(n)
        for n in HOST_CLASSES.names()
        if not builtins_only or n in _BUILTIN_CLASS_NAMES
    }


def build_host_class(name: str) -> type:
    """Return the host class registered under *name* (raising on miss)."""
    return resolved(HOST_CLASSES.get(name).cls)


def get_host_class(name: str) -> type | None:
    """Return the host class registered under *name*, or ``None``.

    Non-raising counterpart to :func:`build_host_class`, for callers that
    produce their own error (e.g. :func:`otto.host.factory.validate_host_dict`).
    """
    return build_host_class(name) if name in HOST_CLASSES else None


# --- console prompts ----------------------------------------------------------

_CONSTRUCTION_PROFILE: "ContextVar[tuple[str, ProfileFields] | None]" = ContextVar(
    "otto_construction_profile", default=None
)
"""The ``os_type`` and profile fields of the host being constructed, set around its construction."""


@contextlib.contextmanager
def constructing_with(os_type: str, fields: ProfileFields | None) -> Iterator[None]:
    """Construct a host of *os_type* inside this block with *fields* as its resolved profile.

    :func:`otto.host.factory.create_host_from_dict` sets the selector and the
    fields of the profile it resolved; :func:`copy_host` sets the source
    host's. Only a host whose ``os_type`` is *os_type* takes them, so a host
    of another selector built inside the block (none is today) resolves its
    own. Reset on exit, so the next host built outside it resolves its own.
    """
    token = _CONSTRUCTION_PROFILE.set(None if fields is None else (os_type, fields))
    try:
        yield
    finally:
        _CONSTRUCTION_PROFILE.reset(token)


def construction_fields(os_type: str) -> ProfileFields | None:
    """Return the profile fields a host of *os_type* being built resolves its prompts from.

    The fields :func:`constructing_with` set for *os_type*, else those of the
    code, class or built-in profile *os_type* names (a directly constructed
    host sees no repo data), else ``None``.
    """
    held = _CONSTRUCTION_PROFILE.get()
    if held is not None and held[0] == os_type:
        return held[1]
    profile = get_os_profile(os_type)
    return profile.fields if profile is not None else None


def copy_host(host: _H, /, **changes: object) -> _H:
    """Return ``dataclasses.replace(host, **changes)``, keeping *host*'s resolved profile.

    ``dataclasses.replace`` skips ``init=False`` fields, so a copy would
    otherwise resolve its profile afresh by name and lose a repo data
    profile the original was built from. Every host copy in otto goes
    through here.
    """
    with constructing_with(getattr(host, "os_type", ""), getattr(host, "_profile_fields", None)):
        return dataclasses.replace(host, **changes)  # ty: ignore[invalid-argument-type]


def resolve_console_prompts(
    options: "ConsoleOptions", fields: ProfileFields | None
) -> "ConsoleOptions":
    """Fill ``login_prompt``/``password_prompt`` left ``None`` on *options* from *fields*.

    A host's own ``console_options`` win; the profile fields supply whatever
    they leave unset; no fields change nothing.
    """
    if fields is None:
        return options
    return dataclasses.replace(
        options,
        login_prompt=(
            options.login_prompt if options.login_prompt is not None else fields.login_prompt
        ),
        password_prompt=(
            options.password_prompt
            if options.password_prompt is not None
            else fields.password_prompt
        ),
    )


def _register_builtin_host_classes() -> None:
    """Register the built-in host classes, each by reference."""
    for name, entry in _builtin_host_classes():
        HOST_CLASSES.register(name, entry)


_register_builtin_host_classes()
