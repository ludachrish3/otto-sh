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
from ..host.remoteHost import RemoteHost
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


def create_host_from_dict(host_data: dict[str, Any]) -> RemoteHost:
    """
    Create appropriate Host subclass from dictionary.

    Parameters
    ----------
    host_data : dict[str, Any]
        Dictionary containing host configuration with keys:
        - ip (required): str
        - creds (required): dict[str, str]
        - ne (required): str
        - user (optional): str
        - board (optional): str
        - slot (optional): int
        - neId (optional): int
        - resources (optional): list[str] or set[str]
        - hop (optional): str (host ID of an intermediate hop)
        - log (optional): bool
        - log_stdout (optional): bool
        - name (optional): str
        - toolchain (optional): dict with sysroot, lcov, gcov paths
        - ssh_options (optional): dict mapped onto ``SshOptions`` fields
        - telnet_options (optional): dict mapped onto ``TelnetOptions`` fields
        - sftp_options (optional): dict mapped onto ``SftpOptions`` fields
        - scp_options (optional): dict mapped onto ``ScpOptions`` fields
        - ftp_options (optional): dict mapped onto ``FtpOptions`` fields
        - nc_options (optional): dict mapped onto ``NcOptions`` fields

    Returns
    -------
    Host
        RemoteHost using dict of host options

    Raises
    ------
    ValueError
        If required fields are missing or invalid
    TypeError
        If field types are incorrect
    """

    # Only keep fields that are relevant to RemoteHost init
    kwargs = { k: v for k, v in host_data.items() if k in RemoteHost.__slots__ }

    # Ensure resources is a set
    resources = kwargs.get('resources', [])
    kwargs['resources'] = set(resources)

    # Convert toolchain dict to Toolchain instance
    if 'toolchain' in kwargs and isinstance(kwargs['toolchain'], dict):
        kwargs['toolchain'] = _build_toolchain(kwargs['toolchain'])

    # Convert each *_options dict to its dataclass instance
    for opt_key, builder in _OPTIONS_BUILDERS.items():
        if opt_key in kwargs and isinstance(kwargs[opt_key], dict):
            kwargs[opt_key] = builder(kwargs[opt_key])

    # Determine which Host subclass to instantiate
    return RemoteHost(**kwargs)


def validate_host_dict(host_data: dict[str, Any]) -> None:
    """
    Validate host dictionary structure without creating a Host object.

    Parameters
    ----------
    host_data : dict[str, Any]
        Host data dictionary to validate

    Raises
    ------
    ValueError
        If validation fails
    """
    required_fields = ['ip', 'creds', 'ne']
    missing = [f for f in required_fields if f not in host_data]
    if missing:
        raise ValueError(f"Missing required host fields: {missing}")

    # Type validation
    if not isinstance(host_data['ip'], str):
        raise ValueError(f"Field 'ip' must be str, got {type(host_data['ip']).__name__}")
    if not isinstance(host_data['creds'], dict):
        raise ValueError(f"Field 'creds' must be dict, got {type(host_data['creds']).__name__}")
    if not isinstance(host_data['ne'], str):
        raise ValueError(f"Field 'ne' must be str, got {type(host_data['ne']).__name__}")
