"""Host-dict factory: build and validate ``RemoteHost`` instances from raw config dicts."""

import itertools
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from ..layout import validate_product_name
from ..registry import resolved
from .capability import (
    menu_capabilities,
    option_default_selectors,
    preference_selectors,
    select_option_defaults,
    select_preferences,
)
from .construction import HostContext, HostSpecInContext, _declared_default, _effective_default
from .dev_tool import apply_declared_dev_tools, apply_dev_tool_providers
from .element import Element
from .inventory_ref import InventoryRef, recording_fields
from .os_profile import (
    HOST_CLASSES,
    OsProfile,
    ProfileContext,
    constructing_with,
    resolve_os_profile,
)
from .product import (
    apply_declared_products,
    apply_product_providers,
    check_stage_collisions,
    cov_dir_of,
    stamp_cov_dir,
    validate_cov_dir,
)
from .remote_host import RemoteHost, make_host_id

if TYPE_CHECKING:
    from collections.abc import Sequence

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
# All but the last two are per-protocol. ``userland_options`` describes the DEVICE
# rather than a connection to it, and is here for the same layering reason: an
# os_profile can default a whole host class's answers while a single host pins
# one key inline. ``reboot_options`` bounds the ``reboot`` verb, not a protocol, and
# layers the same way.
OPTIONS_KEYS: frozenset[str] = frozenset(
    {
        "ssh_options",
        "telnet_options",
        "sftp_options",
        "scp_options",
        "ftp_options",
        "nc_options",
        "userland_options",
        "reboot_options",
    }
)


_NO_TABLE: dict[str, Any] = {}
"""An absent option table, as :func:`_merge_host_dict` overlays one (never written to)."""


def _overlay(
    tables: "Sequence[dict[str, Any]]",
    *,
    sources: "dict[str, int] | None",
    prefix: "str | None" = None,
) -> dict[str, Any]:
    """Overlay *tables*, lowest first: each table's keys replace the earlier tables'.

    The one precedence of :func:`_merge_host_dict`. With *sources*, also
    record which table each surviving key came from, by its position in
    *tables* (named ``<prefix>.<key>`` for an option table's key), which is
    how a recorded build attributes the merge (:func:`otto.host.provenance.note_merge`).
    """
    merged: dict[str, Any] = {}
    for rank, table in enumerate(tables):
        merged.update(table)
        if sources is not None:
            for name in table:
                sources[name if prefix is None else f"{prefix}.{name}"] = rank
    return merged


def _merge_host_dict(
    host_data: dict[str, Any],
    option_defaults: dict[str, dict[str, Any]] | None,
    profile: OsProfile,
    spec_cls: "type[HostSpec]",
    *,
    host_cls: "type[RemoteHost] | None" = None,
    sources: "dict[str, int] | None" = None,
) -> dict[str, Any]:
    """Precedence-merge profile defaults, host fields, and product option defaults into one dict.

    Scalars: host > profile, with one exception: when the host entry writes a
    capability list (``valid_transfers`` and so on) but not its pin, the
    profile's pin for that capability is dropped, so the entry's list is
    honoured as written and its first entry is the default. ``*_options``
    tables, per key, lowest→highest: profile default < host field < product
    ``[host_preferences]`` value. Only option keys the target spec declares
    are merged.

    A capability list neither the entry nor the profile writes is *host_cls*'s
    own default list when the class redeclared it and the spec did not, so a
    host class that redeclares only its list selects the same default here as
    when built directly.

    The layers are overlaid by :func:`_overlay`, in the order of
    :data:`~otto.host.provenance.PROFILE_RANK`,
    :data:`~otto.host.provenance.HOST_RANK` and
    :data:`~otto.host.provenance.PREFERENCE_RANK`. *sources*, when given (a
    recorded build), is filled with each merged value's layer.
    """
    defaults = profile.fields.defaults.thaw_json()
    for cap in menu_capabilities(getattr(spec_cls, "_host_family", None)):
        if cap.menu_field in host_data and cap.pin_field not in host_data:
            defaults.pop(cap.pin_field, None)
    merged = _overlay((defaults, host_data), sources=sources)
    if host_cls is not None:
        _fill_class_menus(merged, host_cls, spec_cls)

    option_defaults = option_defaults or {}
    opt_keys = OPTIONS_KEYS & set(spec_cls.model_fields)
    for key in opt_keys:
        p = defaults.get(key)
        h = host_data.get(key)
        d = option_defaults.get(key)
        if sources is not None:
            sources.pop(key, None)  # a table is attributed per key, below
        table = _overlay(
            (
                p if isinstance(p, dict) else _NO_TABLE,
                h if isinstance(h, dict) else _NO_TABLE,
                d if isinstance(d, dict) else _NO_TABLE,
            ),
            sources=sources,
            prefix=key,
        )
        if table:
            merged[key] = table
        else:
            merged.pop(key, None)
    return merged


def _fill_class_menus(
    merged: dict[str, Any], host_cls: "type[RemoteHost]", spec_cls: "type[HostSpec]"
) -> None:
    """Set each unwritten capability list in *merged* to *host_cls*'s own default list.

    Only where the class redeclared the list and the spec did not: a spec that
    declares its own default list keeps it, and a stock host's entry stays as
    written.

    This is the lab path's counterpart of the constructor, where the class's
    field default applies by itself; :func:`~otto.host.construction._effective_default`
    is the same idea for a scalar field.
    """
    for cap in menu_capabilities(getattr(spec_cls, "_host_family", None)):
        if cap.menu_field in merged:
            continue
        spec_default = spec_cls.model_fields[cap.menu_field].get_default(call_default_factory=True)
        own = _declared_default(host_cls, cap.menu_field, call_factory=True)
        if spec_default == cap.default_menu and own is not None and own != spec_default:
            merged[cap.menu_field] = own


def _attributing() -> bool:
    """Return whether this build records each value's layer (inside a recorded entry)."""
    if not recording_fields():
        return False
    from . import provenance

    return provenance.attributing()


def _note_merge(
    *,
    sources: dict[str, int],
    host_data: dict[str, Any],
    profile: OsProfile,
    preferences: "dict[str, dict[str, Any]]",
    host_id: str,
) -> None:
    """Note the final merge's winners, and offer the capability lists' selectors.

    The capability resolver runs after this, in contextual validation, and
    notes a ``[host_preferences]`` list that wins over the pin noted here
    (:func:`otto.host.provenance.preference_won`).
    """
    from . import provenance

    provenance.note_merge(
        sources=sources,
        host_data=host_data,
        defaults=profile.fields.defaults.thaw_json(),
        profile=profile.name,
        selectors=option_default_selectors(preferences, host_id),
    )
    provenance.preferences_offered(selectors=preference_selectors(preferences, host_id))


def _data(profiles: ProfileContext | None) -> ProfileContext:
    """Return the project data a resolution sees: *profiles*, or none."""
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
    loader resolves every entry (:func:`otto.inventory.resolve_view_entry`,
    or :func:`otto.inventory.resolve_host_entry` for ``default`` alone)
    before it reaches any factory function — this guard is for callers that
    hand-build dicts. A qualified reference is named ``name/key``.
    """
    value = host_data.get("inventory")
    if value is None:
        return
    from ..errors import describe_refused  # lazy: refusal path only
    from .inventory_ref import normalize_reference  # lazy: refusal path only

    try:
        reference = normalize_reference(value=value)
    except ValueError:
        reference = None
    view_resolver = "otto.inventory.resolve_view_entry(entry, view=view, element=element)"
    if reference is not None and reference.qualified:
        named = f"inventory reference {reference.name}/{reference.key}"
        call = view_resolver
    else:
        named = f"inventory key {describe_refused(value)}"
        call = f"otto.inventory.resolve_host_entry(entry, inventory) (or {view_resolver})"
    raise ValueError(
        f"host entry references {named} but was not resolved; call {call} first "
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
    profile = resolve_os_profile(name=selector, data=_data(profiles))
    entry = HOST_CLASSES.get(profile.base)
    spec_cls = resolved(entry.spec)
    # A recorded build (`otto lab explain`) collects each value's layer from
    # the merge that decides it, and notes the final merge's once, below.
    sources: "dict[str, int] | None" = {} if _attributing() else None
    host_cls = resolved(entry.cls)
    merged = _merge_host_dict(
        host_data, None, profile, spec_cls, host_cls=host_cls, sources=sources
    )
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
            sources = None if sources is None else {}
            merged = _merge_host_dict(
                host_data, option_defaults, profile, spec_cls, host_cls=host_cls, sources=sources
            )
            merged["os_type"] = selector
            spec = spec_cls.model_validate(merged)
    if sources is not None:
        _note_merge(
            sources=sources,
            host_data=host_data,
            profile=profile,
            preferences=preferences or {},
            host_id=host_id,
        )
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
    host_data: dict[str, Any], *, element: Element, profiles: ProfileContext | None = None
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
    *profiles* is the project data the ``os_type`` resolves through, as there.

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
    *,
    preferences: dict[str, dict[str, Any]] | None = None,
    lab_name: str | None = None,
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

    ``profiles`` is the project data profiles of the selected projects
    (:class:`~otto.host.os_profile.ProfileContext`); ``os_type`` resolves
    through the code profiles, then them, then the host classes and built-ins
    (:func:`~otto.host.os_profile.resolve_os_profile`). Omitted, no project data
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
        validate_cov_dir(cov_dir_of(product), f"host {host.id}: product {product.name!r}")
    check_stage_collisions(host)
    check_cov_dir_overlaps(host)


def check_cov_dir_overlaps(host: "RemoteHost | Any") -> None:
    """Refuse two products on THIS host whose ``cov_dir`` values are equal or nested.

    One product's counter reset (and cleanup's sweep) walks its whole
    ``cov_dir``, so a dir shared with, or containing, another product's would
    wipe that product's counters. The test is by path component, both ways
    round over every unordered pair, so the verdict never depends on
    declaration order and siblings that merely share a string prefix
    (``/var/cov/app`` and ``/var/cov/app_ref``) are fine. The same
    ``cov_dir`` on two hosts is fine too.
    """
    for p, q in itertools.combinations(host.products, 2):
        a, b = cov_dir_of(p), cov_dir_of(q)
        first, second = PurePosixPath(a), PurePosixPath(b)
        if first == second or second.is_relative_to(first) or first.is_relative_to(second):
            raise ValueError(
                f"host {host.id}: products {p.name!r} and {q.name!r} share cov_dir {a} / {b} "
                "(equal or nested): one product's counter reset would wipe the other's"
            )


def validate_host_dict(
    *, host_data: dict[str, Any], element: Element, profiles: ProfileContext | None = None
) -> None:
    """Validate a host dict without constructing the host.

    ``os_type`` must name a profile some layer resolves (project data only through
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
