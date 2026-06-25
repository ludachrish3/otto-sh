from typing import Any

from ..host.capability import select_option_defaults, select_preferences
from ..host.os_profile import (
    build_host_class,
    build_host_spec,
    build_os_profile,
    get_os_profile,
    registered_profile_names,
)
from ..host.product import apply_product_providers
from ..host.remote_host import RemoteHost, make_host_id
from ..models.host import HostSpec

# Names of the per-protocol option tables accepted on host dicts and in
# ``[host_preferences."<selector>"]`` blocks. Kept here (and imported by
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
    option_defaults: dict[str, dict[str, Any]] | None,
    profile: Any,
    spec_cls: type[HostSpec],
) -> dict[str, Any]:
    """Precedence-merge profile defaults, host fields, and product option
    defaults into one dict for ``spec_cls.model_validate``.

    Scalars: host > profile. ``*_options`` tables, per key, lowest→highest:
    profile default < host field < product ``[host_preferences]`` value. Only
    option keys the target spec declares are merged.
    """
    merged: dict[str, Any] = {**profile.defaults, **host_data}

    option_defaults = option_defaults or {}
    opt_keys = OPTIONS_KEYS & set(spec_cls.model_fields)
    for key in opt_keys:
        p = profile.defaults.get(key)
        h = host_data.get(key)
        d = option_defaults.get(key)
        table: dict[str, Any] = {
            **(p if isinstance(p, dict) else {}),
            **(h if isinstance(h, dict) else {}),
            **(d if isinstance(d, dict) else {}),
        }
        if table:
            merged[key] = table
        else:
            merged.pop(key, None)
    return merged


def create_host_from_dict(
    host_data: dict[str, Any],
    preferences: dict[str, dict[str, Any]] | None = None,
) -> RemoteHost:
    """Create the appropriate :class:`~otto.host.remote_host.RemoteHost` subclass from a host dict.

    ``os_type`` selects the profile / class / spec. ``preferences`` is the unified
    ``{selector: {capability_list | option_table}}`` table; for each host the
    factory cascades it by ``id`` into capability selections (forwarded to
    ``to_host``) and option-value defaults (merged per-key, product-wins). With
    ``preferences=None`` the result is identical to a bare host dict.
    """
    selector = host_data.get('os_type', 'unix')
    profile = build_os_profile(selector)
    cls = build_host_class(profile.base)
    spec_cls = build_host_spec(profile.base)

    flat_prefs: dict[str, list[str]] | None = None
    option_defaults: dict[str, dict[str, Any]] | None = None
    if preferences:
        host_id = make_host_id(
            host_data['element'], host_data.get('element_id'),
            host_data.get('board'), host_data.get('slot'),
        )
        flat_prefs = select_preferences(preferences, host_id)
        option_defaults = select_option_defaults(preferences, host_id)

    merged = _merge_host_dict(host_data, option_defaults, profile, spec_cls)
    merged['os_type'] = selector
    spec = spec_cls.model_validate(merged)
    host = spec.to_host(cls, preferences=flat_prefs)
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
