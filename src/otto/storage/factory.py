from pathlib import Path
from typing import Any

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

    Dispatches on the ``osType`` field:

    - ``unix`` (the default when ``osType`` is absent) → :class:`UnixHost`
    - ``embedded`` → :class:`EmbeddedHost`

    Parameters
    ----------
    host_data : dict[str, Any]
        Dictionary containing host configuration. The accepted keys depend on
        ``osType``; see :func:`validate_host_dict` for the required set. Common
        keys: ``ip``, ``ne``, ``creds``, ``user``, ``board``, ``slot``,
        ``neId``, ``resources``, ``hop``, ``log``, ``log_stdout``, ``name``,
        ``osType``, ``osName``, ``osVersion``. Unix hosts additionally accept
        ``docker_capable``, ``toolchain``, and the ``*_options`` tables;
        embedded hosts accept ``telnet_options`` only.
    defaults : dict[str, dict[str, Any]] | None
        Optional repo-level option defaults, keyed by ``*_options`` table
        name. When supplied, each table is merged per-key beneath the
        host's own ``*_options`` (host keys win). ``None`` (the default)
        applies no defaults.

    Returns
    -------
    RemoteHost
        A :class:`UnixHost` or :class:`EmbeddedHost`, selected by ``osType``.

    Raises
    ------
    ValueError
        If ``osType`` is present but is neither ``unix`` nor ``embedded``, or
        if an embedded host declares ``docker_capable``.
    TypeError
        If required fields are missing or field types are incorrect.
    """
    os_type = host_data.get('osType', 'unix')
    if os_type == 'unix':
        return _create_unix_host(host_data, defaults)
    if os_type == 'embedded':
        return _create_embedded_host(host_data, defaults)
    raise ValueError(
        f"Unknown osType {os_type!r}; expected 'unix' or 'embedded'"
    )


def _create_unix_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
) -> UnixHost:
    """Build a :class:`UnixHost` (SSH/Telnet, bash shell) from a host dict."""

    # Only keep fields that are relevant to UnixHost init
    kwargs = { k: v for k, v in host_data.items() if k in UnixHost.__slots__ }

    # Ensure resources is a set
    resources = kwargs.get('resources', [])
    kwargs['resources'] = set(resources)

    # Convert toolchain dict to Toolchain instance
    if 'toolchain' in kwargs and isinstance(kwargs['toolchain'], dict):
        kwargs['toolchain'] = _build_toolchain(kwargs['toolchain'])

    # Convert each *_options dict to its dataclass instance, merging
    # repo-level defaults beneath the host's own values per-key.
    defaults = defaults or {}
    for opt_key, builder in _OPTIONS_BUILDERS.items():
        raw_host = kwargs.get(opt_key)
        host_table: dict[str, Any] = raw_host if isinstance(raw_host, dict) else {}
        default_table: dict[str, Any] = defaults.get(opt_key, {})
        if default_table or host_table:
            kwargs[opt_key] = builder({**default_table, **host_table})

    return UnixHost(**kwargs)


def _create_embedded_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
) -> EmbeddedHost:
    """Build an :class:`EmbeddedHost` (telnet RTOS shell) from a host dict.

    Embedded hosts speak telnet only, so ``telnet_options`` is the single
    per-protocol option table honored. A bare-metal/RTOS target cannot run
    Docker containers — a ``docker_capable: true`` entry is rejected outright.
    """
    if host_data.get('docker_capable'):
        raise ValueError(
            f"docker_capable is not supported on embedded hosts "
            f"(host {host_data.get('ne', '?')!r}) — a bare-metal/RTOS "
            f"target cannot run Docker containers"
        )

    # Only keep fields that are relevant to EmbeddedHost init
    kwargs = { k: v for k, v in host_data.items() if k in EmbeddedHost.__slots__ }

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
    # uses; merge repo-level defaults beneath the host's own values per-key.
    defaults = defaults or {}
    raw_host = kwargs.get('telnet_options')
    host_table: dict[str, Any] = raw_host if isinstance(raw_host, dict) else {}
    default_table: dict[str, Any] = defaults.get('telnet_options', {})
    if default_table or host_table:
        kwargs['telnet_options'] = _build_telnet_options({**default_table, **host_table})

    return EmbeddedHost(**kwargs)


def validate_host_dict(host_data: dict[str, Any]) -> None:
    """
    Validate host dictionary structure without creating a Host object.

    The required-field set depends on ``osType``:

    - ``unix`` (the default when ``osType`` is absent): ``ip``, ``creds``, ``ne``
    - ``embedded``: ``ip``, ``ne`` — ``creds`` is optional, since the RTOS
      telnet shell typically has no login step

    Parameters
    ----------
    host_data : dict[str, Any]
        Host data dictionary to validate

    Raises
    ------
    ValueError
        If ``osType`` is invalid, a required field is missing, a field has the
        wrong type, an embedded host declares ``docker_capable``, or an
        embedded host's ``transfer`` value is not ``console`` or ``tftp``.
    """
    os_type = host_data.get('osType', 'unix')
    if os_type not in ('unix', 'embedded'):
        raise ValueError(
            f"Field 'osType' must be 'unix' or 'embedded', got {os_type!r}"
        )

    required_fields = ['ip', 'ne'] if os_type == 'embedded' else ['ip', 'creds', 'ne']
    missing = [f for f in required_fields if f not in host_data]
    if missing:
        raise ValueError(f"Missing required host fields: {missing}")

    # Type validation
    if not isinstance(host_data['ip'], str):
        raise ValueError(f"Field 'ip' must be str, got {type(host_data['ip']).__name__}")
    if not isinstance(host_data['ne'], str):
        raise ValueError(f"Field 'ne' must be str, got {type(host_data['ne']).__name__}")
    if 'creds' in host_data and not isinstance(host_data['creds'], dict):
        raise ValueError(f"Field 'creds' must be dict, got {type(host_data['creds']).__name__}")

    if os_type == 'embedded' and host_data.get('docker_capable'):
        raise ValueError(
            f"docker_capable is not supported on embedded hosts "
            f"(host {host_data['ne']!r})"
        )

    if os_type == 'embedded' and 'transfer' in host_data:
        if host_data['transfer'] not in ('console', 'tftp'):
            raise ValueError(
                f"Field 'transfer' must be 'console' or 'tftp' for embedded "
                f"hosts, got {host_data['transfer']!r}"
            )

    if os_type == 'embedded' and 'filesystem' in host_data:
        fs = host_data['filesystem']
        if not isinstance(fs, str) or fs not in _FILESYSTEM_CLASSES:
            known = ', '.join(sorted(_FILESYSTEM_CLASSES))
            raise ValueError(
                f"Field 'filesystem' must be one of: {known} "
                f"(host {host_data['ne']!r} declared {fs!r})"
            )
