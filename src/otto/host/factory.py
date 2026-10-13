"""Host-dict factory: build and validate ``RemoteHost`` instances from raw config dicts."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..layout import validate_product_name
from ..registry import resolved
from .capability import select_option_defaults, select_preferences
from .construction import HostContext, HostSpecInContext, _effective_default
from .dev_tool import apply_declared_dev_tools, apply_dev_tool_providers
from .element import Element
from .inventory_ref import InventoryRef
from .os_profile import (
    HOST_CLASSES,
    OsProfile,
    ProfileContext,
    constructing_with,
    resolve_os_profile,
)
from .product import apply_declared_products, apply_product_providers, stamp_cov_dir
from .remote_host import RemoteHost, make_host_id

if TYPE_CHECKING:
    from .specs import HostSpec

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
    profile: OsProfile,
    spec_cls: "type[HostSpec]",
) -> dict[str, Any]:
    """Precedence-merge profile defaults, host fields, and product option defaults into one dict.

    Scalars: host > profile. ``*_options`` tables, per key, lowest→highest:
    profile default < host field < product ``[host_preferences]`` value. Only
    option keys the target spec declares are merged.
    """
    defaults = profile.fields.defaults.thaw_json()
    merged: dict[str, Any] = {**defaults, **host_data}

    option_defaults = option_defaults or {}
    opt_keys = OPTIONS_KEYS & set(spec_cls.model_fields)
    for key in opt_keys:
        p = defaults.get(key)
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


def _data(profiles: ProfileContext | None) -> ProfileContext:
    """Return the repo data a resolution sees: *profiles*, or none."""
    return profiles if profiles is not None else ProfileContext.empty()


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

    docker_capable: bool
    """True when the host is docker-capable, as the entry (or its profile) sets it, else as
    the host class declares by default."""

    board: str | None
    """Validated board (a profile may default it) — an id portion, kept to name the host."""

    slot: int | None
    """Validated slot (a profile may default it) — an id portion, kept to name the host."""

    docker_priority: int = 0
    """The lab's rank of this host as a default docker parent (0 when undeclared)."""


def reject_unresolved_reference(host_data: dict[str, Any]) -> None:
    """Refuse a host dict that still carries a non-null ``inventory`` key.

    ``None`` (the field's default) references nothing — a host entry that
    never mentioned the inventory validates as ``{"inventory": None, ...}``
    under schema-legal round-tripping, and that is not a reference (R7). A
    referenced entry has no address of its own; validating it here would
    fail on ``ip`` with a message that points at the wrong thing. The lab
    loader resolves every entry (:func:`otto.inventory.resolve_host_entry`)
    before it reaches any factory function — this guard is for callers that
    hand-build dicts.
    """
    if host_data.get("inventory") is not None:
        raise ValueError(
            f"host entry references inventory key {host_data['inventory']!r} but was not "
            "resolved; call otto.inventory.resolve_host_entry(entry, inventory) first "
            "(the lab loader does this for every entry it builds)"
        )


def _check_preferences_shape(preferences: "dict[str, Any]") -> None:
    """Refuse a preferences table whose values are not selector-keyed tables."""
    for selector, entries in preferences.items():
        if not isinstance(entries, dict):
            raise ValueError(  # noqa: TRY004 — a configuration error, as every factory refusal
                f"preferences[{selector!r}] must be a table of capability lists and option "
                f"tables keyed by a host-id selector, e.g. {{'.*': {{'term': ['ssh']}}}}; got "
                f"{type(entries).__name__}"
            )


def _pipeline(
    host_data: dict[str, Any],
    *,
    element: Element,
    profiles: ProfileContext | None,
    preferences: "dict[str, dict[str, Any]] | None",
) -> HostSpecInContext:
    """Resolve, merge, validate structurally, identify and select preferences."""
    reject_unresolved_reference(host_data)
    selector = host_data.get("os_type", "unix")
    profile = resolve_os_profile(selector, data=_data(profiles))
    entry = HOST_CLASSES.get(profile.base)
    spec_cls = resolved(entry.spec)
    merged = _merge_host_dict(host_data, None, profile, spec_cls)
    merged["os_type"] = selector
    spec = spec_cls.model_validate(merged)
    # The id composes from the VALIDATED spec: a profile may default board or
    # slot, and a lax slot ("03", 3.0) reads as the int the built host keeps.
    host_id = make_host_id(element.name, spec.board, spec.slot)
    flat: "dict[str, list[str]] | None" = None
    if preferences is not None:
        _check_preferences_shape(preferences)
        flat = select_preferences(preferences, host_id)
        option_defaults = select_option_defaults(preferences, host_id)
        if option_defaults:
            merged = _merge_host_dict(host_data, option_defaults, profile, spec_cls)
            merged["os_type"] = selector
            spec = spec_cls.model_validate(merged)
    context = HostContext(
        element=element, entry=entry, profile=profile, host_id=host_id, preferences=flat
    )
    return HostSpecInContext(spec=spec, context=context)


def host_context(
    host_data: dict[str, Any],
    *,
    element: Element,
    profiles: ProfileContext | None = None,
    preferences: "dict[str, dict[str, Any]] | None" = None,
) -> HostSpecInContext:
    """Return *host_data*'s validated spec and the context it validates in.

    The supported route to a host without the factory:
    ``hc.spec.to_host(ready=hc.spec.validate_in(hc.context))``. *preferences* is
    the selector-keyed table :func:`create_host_from_dict` takes; ``None`` means
    no preferences (the record builds).
    """
    return _pipeline(
        host_data,
        element=element,
        profiles=profiles,
        preferences=preferences if preferences is not None else {},
    )


def host_identity(
    host_data: dict[str, Any], element: Element, *, profiles: ProfileContext | None = None
) -> HostIdentity:
    """Resolve a raw host dict's identity WITHOUT building the host.

    Applies the same ``os_profile`` merge and the same pydantic validation
    :func:`create_host_from_dict` applies, then composes the id through
    :func:`~otto.host.remote_host.make_host_id` — so the result is
    byte-identical to the constructed host's, which naive string-formatting
    of the raw dict is NOT: an ``os_profile`` that defaults ``board`` /
    ``slot`` supplies id parts the raw dict never mentions, so a raw-derived
    id would be missing them entirely. Completion that offered such an id
    would offer ids that do not dispatch.

    *element* is the host's element, the same object
    :func:`create_host_from_dict` takes — its name is the id's first part.
    *profiles* is the repo data the ``os_type`` resolves through, as there.

    It runs the factory's pipeline in no-preference mode: structural, then
    contextual, validation, with preferences unknown. No-preference mode
    refuses an entry that cannot build under any selection.

    Raises the same errors as :func:`validate_host_dict` (``ValueError``,
    including ``pydantic.ValidationError`` and
    :class:`~otto.host.HostValidationError`) — callers enumerating a whole
    fleet are expected to skip entries that fail.
    """
    hc = _pipeline(host_data, element=element, profiles=profiles, preferences=None)
    hc.spec.validate_in(hc.context)
    spec = hc.spec
    return HostIdentity(
        id=hc.context.host_id,
        ip=spec.ip,
        docker_capable=bool(_effective_default(spec, hc.context.entry.cls, "docker_capable")),
        board=spec.board,
        slot=spec.slot,
        docker_priority=int(getattr(spec, "docker_priority", 0)),
    )


def create_host_from_dict(
    host_data: dict[str, Any],
    preferences: dict[str, dict[str, Any]] | None = None,
    lab_name: str | None = None,
    *,
    element: Element,
    inventory_ref: InventoryRef | None = None,
    profiles: ProfileContext | None = None,
) -> RemoteHost:
    """Create the appropriate :class:`~otto.host.remote_host.RemoteHost` subclass from a host dict.

    ``os_type`` selects the profile / class / spec. ``preferences`` is the unified
    ``{selector: {capability_list | option_table}}`` table; for each host the
    factory cascades it by ``id`` into capability selections (resolved by
    contextual validation) and option-value defaults (merged per-key,
    product-wins). With ``preferences=None`` the result is identical to a bare
    host dict. A value that is not a table (a flat ``{capability: [...]}``
    map) is refused.

    The entry runs one pipeline: resolve the profile, merge, validate
    structurally, compute the id, select preferences, validate contextually
    (:meth:`~otto.host.HostSpec.validate_in`, which issues a readiness record),
    then construct from that record (:meth:`~otto.host.HostSpec.to_host`).

    ``lab_name`` is the lab the caller is loading, stamped onto
    :attr:`~otto.host.host.BaseHost.source_lab` before the product providers
    run — a provider may be gated on the host's lab, and a gate cannot read a
    stamp applied after it. It is a LOADER argument, deliberately separate from
    ``host_data``: the host specs forbid extras, so lab data cannot set it.
    Omitted, the host is left unattributed (``""``) rather than guessed at.

    ``element`` is the host's element — name, id, metadata, resources as one
    object (spec 2026-09-05 §2.6). A LOADER argument like ``lab_name``: the
    host spec forbids element keys on the entry. It becomes the host's
    :attr:`~otto.host.host.BaseHost.element` unchanged — the very
    instance passed here, so every host of one element shares it — and reaches
    construction through the validation context so the providers see it.

    ``inventory_ref`` is the provenance of an entry the loader resolved from an
    inventory record — a LOADER argument like ``element``; the host spec's own
    ``inventory`` field is the key and never reaches the factory.

    ``profiles`` is the repo data profiles of the selected repos
    (:class:`~otto.host.os_profile.ProfileContext`); ``os_type`` resolves
    through the code profiles, then them, then the host classes and built-ins
    (:func:`~otto.host.os_profile.resolve_os_profile`). Omitted, no repo data
    is seen. The host keeps the fields of the profile it was built from, so
    its console prompts come from that profile, across a connection rebuild
    and a copy too.
    """
    hc = _pipeline(
        host_data,
        element=element,
        profiles=profiles,
        preferences=preferences if preferences is not None else {},
    )
    ready = hc.spec.validate_in(hc.context)
    # The stock to_host scopes its own construction too; this scope covers an
    # override that does not reach them, and nesting the two is harmless.
    with constructing_with(str(hc.spec.os_type), hc.context.profile.fields):
        host = hc.spec.to_host(ready=ready)
    # Before the providers, not after: provider selection is allowed to depend
    # on which lab the host came from, and a stamp applied afterwards would be
    # invisible to exactly the code that needs it.
    host.source_lab = lab_name or ""
    host.inventory_ref = inventory_ref if inventory_ref is not None else InventoryRef()
    # Declared before providers, per seam: that order is what lets the provider
    # loops refuse a code instance whose name a settings entry already holds
    # (a name is defined in data OR in code). Both run after the source_lab
    # stamp above, because both gates read it.
    apply_providers(host)
    return host


def apply_providers(host: "RemoteHost | Any") -> None:
    """Attach declared + provider products and dev tools, then finish each product.

    The single ingest chokepoint: declared entries first per seam (so a
    provider instance reusing a declared name is refused, naming both; two
    providers with one name keep first-wins), then the kmodcov binding check
    (:func:`otto.host.kmod_tool_kind.check_kmodcov_bindings` — a host matching
    two ``kmodcov`` dev-tool entries is refused here, and so is one whose
    ``coverage = "module"`` product matches none), then the product-only
    finishing pass — the name must be a safe run-tree segment
    (:mod:`otto.layout`) and ``cov_dir`` becomes concrete. Called by
    ``create_host_from_dict`` and by the compose module for container hosts,
    so both families get the same products.
    """
    apply_declared_products(host)
    apply_product_providers(host)
    apply_declared_dev_tools(host)
    apply_dev_tool_providers(host)
    from .kmod_tool_kind import check_kmodcov_bindings

    check_kmodcov_bindings(host)
    for product in host.products:
        try:
            validate_product_name(product.name)
        except ValueError as e:
            raise ValueError(f"host {host.id}: {e}") from e
        stamp_cov_dir(product)
    check_stage_collisions(host)


def check_stage_collisions(host: "RemoteHost | Any") -> None:
    """Refuse two things on THIS host that stage one basename into one directory.

    The second ``put`` would land on the first's file, and whichever
    installed later would silently get the other's build — two ``demo.ko``
    from different trees, two ``app.tar`` images. The key is the RESOLVED
    staging directory plus the artifact's basename
    (:meth:`~otto.host.product.Product.resolved_stage_dir`), so the same
    basename under different directories is fine, and so is the same product
    on two hosts: a per-board build of one name is the normal shape, not a
    collision. Anything that places no file at all
    (:attr:`~otto.host.product.Product.stages_artifact`) is skipped — an
    ``llext`` extension and a pulled image have nothing to overwrite.

    BOTH seams are checked together. A ``kmod`` dev tool stages its ``.ko``
    through the very same ``host.load(dest_dir=...)`` a ``kmod`` product
    does, and by the time this runs both lists are populated — checking
    products alone would let a tool and a product overwrite each other in
    exactly the way this exists to stop. The message names each side's seam
    so the entry is findable.
    """
    seen: dict[tuple[str, str], str] = {}
    staged = [(p, "product") for p in host.products] + [
        (t, "dev tool") for t in getattr(host, "dev_tools", [])
    ]
    for item, seam in staged:
        if not getattr(item, "stages_artifact", False):
            continue
        artifact = getattr(item, "artifact", None)
        if artifact is None or not hasattr(item, "stage_key"):
            continue
        key = (item.stage_key(host), Path(artifact).name)
        first = seen.get(key)
        if first is not None:
            raise ValueError(
                f"host {host.id}: {first} and {seam} {item.name!r} both stage "
                f"{key[1]!r} into {key[0]} — one would overwrite the other; give one of them "
                "its own 'stage_dir'"
            )
        seen[key] = f"{seam} {item.name!r}"


def validate_host_dict(
    host_data: dict[str, Any], *, element: Element, profiles: ProfileContext | None = None
) -> None:
    """Validate a host dict without constructing the host.

    ``os_type`` must name a profile some layer resolves (repo data only through
    *profiles*, as in :func:`create_host_from_dict`); the profile's base spec
    validates the merged dict (``extra='forbid'``, required fields, typed
    coercion, family-specific field validators for ``command_frame`` /
    ``filesystem`` / ``transfer``). The spec is then
    validated in its context (*element* names the host, as in
    :func:`create_host_from_dict`), including the spec's own
    :meth:`~otto.host.HostSpec.check_in`, in no-preference mode: preferences
    are unknown here, and no-preference mode refuses an entry that cannot
    build under any selection.

    Raises
    ------
    ValueError
        If ``os_type`` names no registered profile (the text
        :func:`~otto.host.os_profile.resolve_os_profile` raises).
    pydantic.ValidationError
        On any structural problem (subclass of ``ValueError``).
    otto.host.HostValidationError
        On a contextual problem, naming the host id (subclass of ``ValueError``).
    """
    hc = _pipeline(host_data, element=element, profiles=profiles, preferences=None)
    hc.spec.validate_in(hc.context)
