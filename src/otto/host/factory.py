"""Host-dict factory: build and validate ``RemoteHost`` instances from raw config dicts."""

from dataclasses import dataclass
from typing import Any

from ..models.host import HostSpec
from .capability import select_option_defaults, select_preferences
from .dev_tool import apply_dev_tool_providers
from .os_profile import (
    build_host_class,
    build_host_spec,
    build_os_profile,
    get_os_profile,
    registered_profile_names,
)
from .product import apply_product_providers
from .remote_host import RemoteHost, make_host_id

# Names of the option tables accepted on host dicts and in
# ``[host_preferences."<selector>"]`` blocks. Kept here as the canonical
# option-key set; ``models.settings`` mirrors it and a drift test holds the two
# in lockstep.
#
# Membership is what makes a table merge PER KEY across the profile / host /
# product layers. A table left out still reaches the host — the plain dict
# merge carries it — but the layers replace each other wholesale instead of
# blending, so a product default and a host's own table cannot coexist.
#
# All but the last are per-protocol. ``userland_options`` describes the DEVICE
# rather than a connection to it, and is here for the same layering reason: an
# os_profile can default a whole host class's answers while a single host pins
# one key inline.
OPTIONS_KEYS: frozenset[str] = frozenset(
    {
        "ssh_options",
        "telnet_options",
        "sftp_options",
        "scp_options",
        "ftp_options",
        "nc_options",
        "userland_options",
    }
)


def _merge_host_dict(
    host_data: dict[str, Any],
    option_defaults: dict[str, dict[str, Any]] | None,
    profile: Any,
    spec_cls: type[HostSpec],
) -> dict[str, Any]:
    """Precedence-merge profile defaults, host fields, and product option defaults into one dict.

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


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Who a host dict *is*, resolved without constructing the host.

    The fields every caller needs to enumerate or address a host — nothing
    that requires transports, creds, or sessions.
    """

    id: str
    """Byte-identical to the ``.id`` of the host this dict would build."""

    ip: str
    """Validated management address (a profile may supply it, so read it here)."""

    element: str
    """Validated element name (feeds positional-handle synthesis)."""

    element_id: int | None
    """Validated element index, or None."""

    docker_capable: bool
    """Whether the host declares (or its profile defaults) docker capability."""


def host_identity(host_data: dict[str, Any]) -> HostIdentity:
    """Resolve a raw host dict's identity WITHOUT building the host.

    Applies the same ``os_profile`` merge and the same pydantic validation
    :func:`create_host_from_dict` applies, then composes the id through
    :func:`~otto.host.remote_host.make_host_id` — so the result is
    byte-identical to the constructed host's, which naive string-formatting
    of the raw dict is NOT: a JSON ``3.0`` element_id formats as ``"3.0"``
    where the host reports ``3``, and a profile that defaults ``board`` /
    ``slot`` / ``element_id`` / ``element`` is invisible to the raw dict
    entirely. Completion that offered a raw-derived id would offer ids that
    do not dispatch.

    Raises the same errors as :func:`validate_host_dict` (``ValueError``,
    including ``pydantic.ValidationError``) — callers enumerating a whole
    fleet are expected to skip entries that fail.
    """
    selector = host_data.get("os_type", "unix")
    profile = build_os_profile(selector)
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, None, profile, spec_cls)
    merged["os_type"] = selector
    spec = spec_cls.model_validate(merged)
    return HostIdentity(
        id=make_host_id(spec.element, spec.element_id, spec.board, spec.slot),
        ip=spec.ip,
        element=spec.element,
        element_id=spec.element_id,
        docker_capable=bool(getattr(spec, "docker_capable", False)),
    )


def create_host_from_dict(
    host_data: dict[str, Any],
    preferences: dict[str, dict[str, Any]] | None = None,
    lab_name: str | None = None,
    *,
    element_metadata: dict[str, Any] | None = None,
) -> RemoteHost:
    """Create the appropriate :class:`~otto.host.remote_host.RemoteHost` subclass from a host dict.

    ``os_type`` selects the profile / class / spec. ``preferences`` is the unified
    ``{selector: {capability_list | option_table}}`` table; for each host the
    factory cascades it by ``id`` into capability selections (forwarded to
    ``to_host``) and option-value defaults (merged per-key, product-wins). With
    ``preferences=None`` the result is identical to a bare host dict.

    ``lab_name`` is the lab the caller is loading, stamped onto
    :attr:`~otto.host.host.BaseHost.source_lab` before the product providers
    run — a provider may be gated on the host's lab, and a gate cannot read a
    stamp applied after it. It is a LOADER argument, deliberately separate from
    ``host_data``: the host specs forbid extras, so lab data cannot set it.
    Omitted, the host is left unattributed (``""``) rather than guessed at.

    ``element_metadata`` is the element's opaque table — a LOADER argument like
    ``lab_name`` (the file layer hoists it; the host spec forbids it on the
    entry), copied per host and stamped before the providers run.
    """
    selector = host_data.get("os_type", "unix")
    profile = build_os_profile(selector)
    cls = build_host_class(profile.base)
    spec_cls = build_host_spec(profile.base)

    flat_prefs: dict[str, list[str]] | None = None
    option_defaults: dict[str, dict[str, Any]] | None = None
    if preferences:
        # Match selectors against the id the host will actually REPORT, not a
        # raw-dict rendering of it — they diverge under profile-defaulted
        # identity fields and non-int element_ids (see host_identity). Costs
        # one extra validation pass, and only when preferences exist.
        host_id = host_identity(host_data).id
        flat_prefs = select_preferences(preferences, host_id)
        option_defaults = select_option_defaults(preferences, host_id)

    merged = _merge_host_dict(host_data, option_defaults, profile, spec_cls)
    merged["os_type"] = selector
    spec = spec_cls.model_validate(merged)
    host = spec.to_host(cls, preferences=flat_prefs)
    # Before the providers, not after: provider selection is allowed to depend
    # on which lab the host came from, and a stamp applied afterwards would be
    # invisible to exactly the code that needs it.
    host.source_lab = lab_name or ""
    host.element_metadata = dict(element_metadata or {})
    apply_product_providers(host)
    apply_dev_tool_providers(host)
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
    selector = host_data.get("os_type", "unix")
    profile = get_os_profile(selector)
    if profile is None:
        known = ", ".join(registered_profile_names())
        raise ValueError(
            f"Field 'os_type' {selector!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, None, profile, spec_cls)
    merged["os_type"] = selector
    spec_cls.model_validate(merged)
