from typing import Any

from ..host.os_profile import (
    build_host_class,
    build_host_spec,
    build_os_profile,
    get_os_profile,
    registered_profile_names,
)
from ..host.product import apply_product_providers
from ..host.remote_host import RemoteHost
from ..models.host import HostSpec

# Names of the per-protocol option tables accepted on host dicts and as
# repo-level ``[host_defaults.<key>]`` tables. Kept here (and imported by
# ``configmodule.repo``) as the canonical option-key set.
OPTIONS_KEYS: frozenset[str] = frozenset({
    'ssh_options',
    'telnet_options',
    'sftp_options',
    'scp_options',
    'ftp_options',
    'nc_options',
})


def _merge_host_dict(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None,
    profile: Any,
    spec_cls: type[HostSpec],
) -> dict[str, Any]:
    """Precedence-merge profile defaults, repo defaults, and host fields into a
    single dict (the M1 merge), ready for ``spec_cls.model_validate``.

    Scalars: host field > profile default. ``*_options`` tables: per-key
    host > profile > repo-default. Only option keys the target spec actually
    declares are merged, so a repo-wide ``ssh_options`` default is never
    injected onto a host family that has no such field.
    """
    merged: dict[str, Any] = {**profile.defaults, **host_data}

    defaults = defaults or {}
    opt_keys = OPTIONS_KEYS & set(spec_cls.model_fields)
    for key in opt_keys:
        d = defaults.get(key)
        p = profile.defaults.get(key)
        h = host_data.get(key)
        table: dict[str, Any] = {
            **(d if isinstance(d, dict) else {}),
            **(p if isinstance(p, dict) else {}),
            **(h if isinstance(h, dict) else {}),
        }
        if table:
            merged[key] = table
        else:
            merged.pop(key, None)
    return merged


def create_host_from_dict(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
) -> RemoteHost:
    """Create the appropriate :class:`RemoteHost` subclass from a host dict.

    ``os_type`` names a registered :class:`~otto.host.os_profile.OsProfile`,
    which selects the base host class and carries a bundle of default field
    values. The profile's base resolves to a ``(host_class, host_spec)`` pair;
    the merged dict (host > profile > repo defaults, per-key for ``*_options``)
    is validated once by the spec (``extra='forbid'``, typed, with field-name
    suggestions on typos) and the spec builds the runtime host.

    Field precedence, highest to lowest: the host's own value; the profile's
    ``defaults``; repo-level ``*_options`` defaults (options only); the runtime
    class's stock default.

    Raises
    ------
    ValueError
        If ``os_type`` names no registered profile.
    pydantic.ValidationError
        If a field is missing, mistyped, misplaced, or unknown (a subclass of
        ``ValueError``).
    """
    selector = host_data.get('os_type', 'unix')
    profile = build_os_profile(selector)
    cls = build_host_class(profile.base)
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, defaults, profile, spec_cls)
    merged['os_type'] = selector
    spec = spec_cls.model_validate(merged)
    host = spec.to_host(cls)
    apply_product_providers(host)
    return host


def validate_host_dict(host_data: dict[str, Any]) -> None:
    """Validate a host dict without constructing the host.

    ``os_type`` must name a registered profile; the profile's base spec
    validates the merged dict (``extra='forbid'``, required fields, typed
    coercion, family-specific field validators for ``command_frame`` /
    ``filesystem`` / ``transfer`` / ``docker_capable``).

    Raises
    ------
    ValueError
        If ``os_type`` names no registered profile.
    pydantic.ValidationError
        On any structural problem (subclass of ``ValueError``).
    """
    selector = host_data.get('os_type', 'unix')
    profile = get_os_profile(selector)
    if profile is None:
        known = ', '.join(registered_profile_names())
        raise ValueError(
            f"Field 'os_type' {selector!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, None, profile, spec_cls)
    merged['os_type'] = selector
    spec_cls.model_validate(merged)
