from pathlib import Path
from typing import Any, cast

from ..host.options import (
    FtpOptions,
    LocalPortForward,
    NcOptions,
    RemotePortForward,
    ScpOptions,
    SftpOptions,
    SocksForward,
    SshOptions,
    TelnetOptions,
)
from ..host.command_frame import build_command_frame
from ..host.embedded_filesystem import _FILESYSTEM_CLASSES, build_filesystem
from ..host.embeddedHost import EmbeddedHost
from ..host.os_profile import OsProfile, build_os_profile, get_os_profile, registered_profile_names
from ..host.remoteHost import RemoteHost
from ..host.unixHost import UnixHost
from ..host.toolchain import Toolchain


def _build_toolchain(raw: dict[str, Any]) -> Toolchain:
    """Build a :class:`Toolchain` from a (possibly partial) dict.

    Only the keys present in *raw* override the defaults, so
    ``{"sysroot": "/opt/arm"}`` gives a ``Toolchain`` with custom
    sysroot and default ``gcov``/``lcov`` paths.
    """
    kwargs: dict[str, Any] = {}
    for key in ('sysroot', 'lcov', 'gcov'):
        if key in raw:
            kwargs[key] = Path(raw[key])
    return Toolchain(**kwargs)


def _build_ssh_options(raw: dict[str, Any]) -> SshOptions:
    """Build an :class:`SshOptions` from a JSON-friendly dict.

    Nested structured forwards (``local_forwards``, ``remote_forwards``,
    ``socks_forwards``) are converted from list-of-dict into the
    corresponding frozen dataclasses. ``post_connect`` cannot be
    expressed in JSON and is silently ignored if present.
    """
    kwargs = {k: v for k, v in raw.items() if k != 'post_connect'}
    if 'local_forwards' in kwargs:
        kwargs['local_forwards'] = [LocalPortForward(**f) for f in kwargs['local_forwards']]
    if 'remote_forwards' in kwargs:
        kwargs['remote_forwards'] = [RemotePortForward(**f) for f in kwargs['remote_forwards']]
    if 'socks_forwards' in kwargs:
        kwargs['socks_forwards'] = [SocksForward(**f) for f in kwargs['socks_forwards']]
    return SshOptions(**kwargs)


def _build_telnet_options(raw: dict[str, Any]) -> TelnetOptions:
    kwargs = dict(raw)
    if 'login_prompt' in kwargs and isinstance(kwargs['login_prompt'], str):
        kwargs['login_prompt'] = kwargs['login_prompt'].encode()
    return TelnetOptions(**kwargs)


def _build_sftp_options(raw: dict[str, Any]) -> SftpOptions:
    return SftpOptions(**raw)


def _build_scp_options(raw: dict[str, Any]) -> ScpOptions:
    return ScpOptions(**raw)


def _build_ftp_options(raw: dict[str, Any]) -> FtpOptions:
    kwargs = dict(raw)
    if 'passive_commands' in kwargs and isinstance(kwargs['passive_commands'], list):
        kwargs['passive_commands'] = tuple(kwargs['passive_commands'])
    return FtpOptions(**kwargs)


def _build_nc_options(raw: dict[str, Any]) -> NcOptions:
    return NcOptions(**raw)


_OPTIONS_BUILDERS: dict[str, Any] = {
    'ssh_options': _build_ssh_options,
    'telnet_options': _build_telnet_options,
    'sftp_options': _build_sftp_options,
    'scp_options': _build_scp_options,
    'ftp_options': _build_ftp_options,
    'nc_options': _build_nc_options,
}

OPTIONS_KEYS: frozenset[str] = frozenset(_OPTIONS_BUILDERS)
"""Names of the per-protocol option tables accepted on host dicts and as
repo-level ``[host_defaults.<key>]`` tables."""


def create_host_from_dict(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
) -> RemoteHost:
    """
    Create the appropriate :class:`RemoteHost` subclass from a host dict.

    The ``osType`` field names a registered :class:`OsProfile`, which selects
    the base host class to build and carries a bundle of default field values:

    - ``unix`` (the default when ``osType`` is absent) → :class:`UnixHost`
    - ``embedded`` → :class:`EmbeddedHost`
    - any custom profile registered via ``register_os_profile`` or an
      ``[os_profiles.<name>]`` settings table → its declared base class

    Field precedence, highest to lowest:

    1. the host's own value in *host_data*;
    2. the profile's ``defaults``;
    3. repo-level ``*_options`` defaults from *defaults* (per-key, options only);
    4. the base class's stock dataclass default.

    Parameters
    ----------
    host_data : dict[str, Any]
        Dictionary containing host configuration. The accepted keys depend on
        the profile's base family; see :func:`validate_host_dict` for the
        required set. Common keys: ``ip``, ``ne``, ``creds``, ``user``,
        ``board``, ``slot``, ``neId``, ``resources``, ``hop``, ``log``,
        ``log_stdout``, ``name``, ``osType``, ``osName``, ``osVersion``. Unix
        hosts additionally accept ``docker_capable``, ``toolchain``, and the
        ``*_options`` tables; embedded hosts accept ``telnet_options`` only.
    defaults : dict[str, dict[str, Any]] | None
        Optional repo-level option defaults, keyed by ``*_options`` table
        name. When supplied, each table is merged per-key beneath the profile's
        and the host's own ``*_options`` (host keys win, then profile keys).
        ``None`` (the default) applies no repo-level defaults.

    Returns
    -------
    RemoteHost
        A :class:`UnixHost` or :class:`EmbeddedHost`, selected by the profile's
        base family.

    Raises
    ------
    ValueError
        If ``osType`` names no registered profile, or if an embedded host
        declares ``docker_capable``.
    TypeError
        If required fields are missing or field types are incorrect.
    """
    os_type = host_data.get('osType', 'unix')
    profile = build_os_profile(os_type)
    if profile.base == 'unix':
        return _create_unix_host(host_data, defaults, profile)
    return _create_embedded_host(host_data, defaults, profile)


def _create_unix_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
    profile: OsProfile | None = None,
) -> UnixHost:
    """Build a :class:`UnixHost` (SSH/Telnet, bash shell) from a host dict.

    The profile's ``defaults`` are layered beneath the host's own fields
    (host wins per-key); ``*_options`` tables merge three-deep so a single key
    can come from the host, the profile, or the repo defaults independently.
    """
    profile_defaults = profile.defaults if profile else {}

    # Layer profile defaults beneath the host's own fields, then keep only
    # fields relevant to UnixHost init. The *_options tables get a finer-grained
    # merge below, so this shallow layer only fixes the scalar/atomic fields.
    effective = {**profile_defaults, **host_data}
    kwargs = { k: v for k, v in effective.items() if k in UnixHost.__slots__ }

    # Ensure resources is a set
    resources = kwargs.get('resources', [])
    kwargs['resources'] = set(resources)

    # Convert toolchain dict to Toolchain instance
    if 'toolchain' in kwargs and isinstance(kwargs['toolchain'], dict):
        kwargs['toolchain'] = _build_toolchain(kwargs['toolchain'])

    # Convert each *_options dict to its dataclass instance, merging per-key:
    # repo defaults (lowest) < profile defaults < host's own values (highest).
    defaults = defaults or {}
    for opt_key, builder in _OPTIONS_BUILDERS.items():
        raw_default = defaults.get(opt_key)
        raw_profile = profile_defaults.get(opt_key)
        raw_host = host_data.get(opt_key)
        default_table: dict[str, Any] = raw_default if isinstance(raw_default, dict) else {}
        profile_table: dict[str, Any] = cast('dict[str, Any]', raw_profile) if isinstance(raw_profile, dict) else {}
        host_table: dict[str, Any] = cast('dict[str, Any]', raw_host) if isinstance(raw_host, dict) else {}
        merged: dict[str, Any] = {**default_table, **profile_table, **host_table}
        if merged:
            kwargs[opt_key] = builder(merged)

    kwargs['osType'] = profile.base if profile else 'unix'
    return UnixHost(**kwargs)


def _create_embedded_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
    profile: OsProfile | None = None,
) -> EmbeddedHost:
    """Build an :class:`EmbeddedHost` (telnet RTOS shell) from a host dict.

    The profile's ``defaults`` are layered beneath the host's own fields
    (host wins per-key). Embedded hosts speak telnet only, so ``telnet_options``
    is the single per-protocol option table honored — it merges three-deep
    (repo defaults < profile < host). A bare-metal/RTOS target cannot run Docker
    containers — a ``docker_capable: true`` entry (from the host or profile) is
    rejected outright.
    """
    profile_defaults = profile.defaults if profile else {}

    # Layer profile defaults beneath the host's own fields, then keep only
    # fields relevant to EmbeddedHost init. telnet_options is merged finer below.
    effective = {**profile_defaults, **host_data}

    if effective.get('docker_capable'):
        raise ValueError(
            f"docker_capable is not supported on embedded hosts "
            f"(host {effective.get('ne', '?')!r}) — a bare-metal/RTOS "
            f"target cannot run Docker containers"
        )

    kwargs = { k: v for k, v in effective.items() if k in EmbeddedHost.__slots__ }

    # Ensure resources is a set
    resources = kwargs.get('resources', [])
    kwargs['resources'] = set(resources)

    # Resolve the lab-data ``filesystem`` string to a typed instance. Absent
    # field defaults to NoFileSystem via the EmbeddedHost field default — no
    # action needed here.
    if 'filesystem' in kwargs and isinstance(kwargs['filesystem'], str):
        kwargs['filesystem'] = build_filesystem(kwargs['filesystem'])

    # Resolve the lab-data ``command_frame`` string to a typed instance.
    # Absent field defaults to ZephyrFrame via the EmbeddedHost field default
    # (the stock Zephyr 3.7/4.4 shell) — no action needed here.
    if 'command_frame' in kwargs and isinstance(kwargs['command_frame'], str):
        kwargs['command_frame'] = build_command_frame(kwargs['command_frame'])

    # telnet_options is the only per-protocol option table an embedded host
    # uses; merge per-key: repo defaults < profile defaults < host's own values.
    defaults = defaults or {}
    raw_profile = profile_defaults.get('telnet_options')
    raw_host = host_data.get('telnet_options')
    default_table: dict[str, Any] = defaults.get('telnet_options', {})
    profile_table: dict[str, Any] = cast('dict[str, Any]', raw_profile) if isinstance(raw_profile, dict) else {}
    host_table: dict[str, Any] = cast('dict[str, Any]', raw_host) if isinstance(raw_host, dict) else {}
    merged: dict[str, Any] = {**default_table, **profile_table, **host_table}
    if merged:
        kwargs['telnet_options'] = _build_telnet_options(merged)

    kwargs['osType'] = profile.base if profile else 'embedded'
    return EmbeddedHost(**kwargs)


def validate_host_dict(host_data: dict[str, Any]) -> None:
    """
    Validate host dictionary structure without creating a Host object.

    ``osType`` must name a registered :class:`OsProfile`; the profile's base
    family determines the required-field set and the family-specific checks.
    The checks run against the *effective* dict — the host's fields layered
    over the profile's ``defaults`` — so a value supplied by the profile (e.g.
    ``creds`` or ``filesystem``) satisfies/validates the same as a host field.

    - ``unix`` base (the default when ``osType`` is absent): ``ip``, ``creds``,
      ``ne`` required.
    - ``embedded`` base: ``ip``, ``ne`` required — ``creds`` is optional, since
      the RTOS telnet shell typically has no login step.

    Parameters
    ----------
    host_data : dict[str, Any]
        Host data dictionary to validate

    Raises
    ------
    ValueError
        If ``osType`` names no registered profile, a required field is missing,
        a field has the wrong type, an embedded host declares ``docker_capable``,
        or an embedded host's ``transfer`` value is not ``console`` or ``tftp``.
    """
    os_type = host_data.get('osType', 'unix')
    profile = get_os_profile(os_type)
    if profile is None:
        known = ', '.join(registered_profile_names())
        raise ValueError(
            f"Field 'osType' {os_type!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    base = profile.base

    # Validate against the host fields layered over the profile defaults, so a
    # profile-supplied required field (e.g. creds) counts as present.
    effective = {**profile.defaults, **host_data}

    required_fields = ['ip', 'ne'] if base == 'embedded' else ['ip', 'creds', 'ne']
    missing = [f for f in required_fields if f not in effective]
    if missing:
        raise ValueError(f"Missing required host fields: {missing}")

    # Type validation
    if not isinstance(effective['ip'], str):
        raise ValueError(f"Field 'ip' must be str, got {type(effective['ip']).__name__}")
    if not isinstance(effective['ne'], str):
        raise ValueError(f"Field 'ne' must be str, got {type(effective['ne']).__name__}")
    if 'creds' in effective and not isinstance(effective['creds'], dict):
        raise ValueError(f"Field 'creds' must be dict, got {type(effective['creds']).__name__}")

    if base == 'embedded' and effective.get('docker_capable'):
        raise ValueError(
            f"docker_capable is not supported on embedded hosts "
            f"(host {effective['ne']!r})"
        )

    if base == 'embedded' and 'transfer' in effective:
        if effective['transfer'] not in ('console', 'tftp'):
            raise ValueError(
                f"Field 'transfer' must be 'console' or 'tftp' for embedded "
                f"hosts, got {effective['transfer']!r}"
            )

    if base == 'embedded' and 'filesystem' in effective:
        fs = effective['filesystem']
        if not isinstance(fs, str) or fs not in _FILESYSTEM_CLASSES:
            known = ', '.join(sorted(_FILESYSTEM_CLASSES))
            raise ValueError(
                f"Field 'filesystem' must be one of: {known} "
                f"(host {effective['ne']!r} declared {fs!r})"
            )
