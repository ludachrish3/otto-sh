"""Which layer supplied each of a host's values, recorded while a lab builds.

``otto lab explain`` answers "where did this value come from?" by recording
the build it explains, not by repeating it. Inside :func:`recording`, the
sites that decide a value note its winner as they decide it:

- the cred merges, store under record (``CredsOverlay``) and record under
  lab file (the inventory join), per identity and field
  (:func:`note_cred_merge`);
- the host factory's merge of profile defaults, the host dict and
  ``[host_preferences]`` option defaults, which reports each value's source
  as it overlays the layers (:func:`note_merge`);
- the capability resolver, when a ``[host_preferences]`` list decides the
  active term, transfer or impairer (:func:`preference_won`).

Each note lands on the host entry being built, which the lab loader opens
with :func:`entry_scope` and tells which fields the inventory join supplied
(:func:`joined`); the composite source keeps only the entries whose element
won (:func:`retain_winners`).

A trace is ONE shared mutable object, reached through context variables and
no threads. An asyncio task created during the build copies the context, so
it sees the same :class:`FieldTrace` and the same entry, and a note it makes
reaches them.

Outside :func:`recording` this module is not even imported: every site asks
:func:`otto.host.inventory_ref.recording_fields` first, which reads the
context variable :func:`recording` sets, in a module every lab build has
loaded already. A build that explains nothing loads, builds and notes
nothing for it.
"""

import contextlib
import enum
from collections.abc import Generator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typing_extensions import final

from .inventory_ref import FIELD_RECORDING


class FieldLayer(enum.Enum):
    """A layer of a host's configuration, lowest to highest as a host composes them.

    A higher layer's value replaces a lower one's where both state it: the
    store's creds lie under the record's, a profile default under every
    field the record or the entry states, and the entry's creds over the
    record's. A field the inventory supplies is the record's alone, so the
    record and the entry never state the same field but ``creds``. The user
    docs state the order on :doc:`/configuration/precedence`.

    The value is how ``otto lab explain`` names the layer;
    :meth:`label` names :attr:`LAB_DATA` for the kind of file it came from.
    """

    CREDS_STORE = "creds store"
    """The ``[creds]`` store, the lowest cred layer."""

    OS_PROFILE = "os profile"
    """A default of the host's ``os_type`` profile."""

    RECORD = "inventory record"
    """The host's record in an inventory, joined through its ``inventory`` reference."""

    LAB_DATA = "lab data"
    """The host entry itself, in a lab file or in a template's rendered document."""

    HOST_PREFERENCES = "host preferences"
    """A ``[host_preferences]`` selection or option default."""

    def label(self, *, rendered: bool) -> str:
        """Return how this layer is shown for an entry, *rendered* from a template or not.

        :attr:`LAB_DATA` is ``template`` for a rendered entry and ``lab file``
        otherwise; every other layer is its value.
        """
        if self is FieldLayer.LAB_DATA:
            return "template" if rendered else "lab file"
        return self.value


@final
@dataclass(frozen=True, kw_only=True)
class EntryKey:
    """One host entry of a lab source: which source, which file, which element, which entry."""

    source: str
    """The label of the lab source that read the entry (``sut/jsonnet#1``)."""

    origin: Path
    """The lab file (or Jsonnet entry file) the entry is in."""

    element: str
    """The name of the element the entry belongs to."""

    index: int
    """The entry's position in the element's ``hosts``."""


@final
@dataclass(frozen=True, kw_only=True)
class FieldWinner:
    """The layer that supplied one of a host entry's values."""

    entry: EntryKey
    """The entry the value belongs to."""

    field: str
    """The value's name, which is a host field (``ip``), an option
    (``ssh_options.port``) or a cred field (``creds.password``)."""

    layer: FieldLayer
    """The layer whose value the host got."""

    detail: "str | None"
    """What in the layer supplied it, when the layer holds several.

    A cred field names its credential identity, ``login`` plus scope
    (``root [ssh]``), never a value; a ``[host_preferences]`` value names its
    selector; an ``os_type`` profile default names the profile. ``None``
    otherwise."""


@final
@dataclass(eq=False, kw_only=True)
class EntryTrace:
    """What one host entry's build recorded: its key, its host, and each field's winner."""

    key: EntryKey
    """The entry."""

    provenance: "Mapping[str, Any] | None" = None
    """The entry's element as its rendered document's provenance table describes it
    (``instance``, ``role``, ``host``), or ``None``."""

    host_id: "str | None" = None
    """The id of the host the entry built, once it has built."""

    supplied: frozenset[str] = frozenset()
    """The host fields the inventory join decided (``ResolvedEntry.supplied``)."""

    preference_selectors: "dict[str, str]" = field(default_factory=dict, repr=False)
    """The selector of each ``[host_preferences]`` capability list offered to the host."""

    _winners: "dict[tuple[str, str | None], FieldWinner]" = field(default_factory=dict, repr=False)
    """Each winner by field and credential identity (``None`` for a field of no credential)."""

    @property
    def winners(self) -> "list[FieldWinner]":
        """Return every field's winner, ordered by field and then credential identity."""
        return [self._winners[k] for k in sorted(self._winners, key=_order)]

    def winner(self, *, field: str, scope: "str | None" = None) -> "FieldWinner | None":
        """Return the winner recorded for *field* (of the credential *scope*), or ``None``."""
        return self._winners.get((field, scope))


def _order(key: "tuple[str, str | None]") -> "tuple[str, str]":
    """Sort a winner key by field, then by credential identity (``None`` first)."""
    return (key[0], "" if key[1] is None else key[1])


@final
class FieldTrace:
    """Every host entry a recorded build attributed, in build order."""

    def __init__(self) -> None:
        self._entries: list[EntryTrace] = []

    @property
    def entries(self) -> "list[EntryTrace]":
        """Return the recorded entries, in build order."""
        return list(self._entries)

    def entry_for(self, *, host_id: str) -> "EntryTrace | None":
        """Return the first recorded entry that built *host_id*, or ``None``."""
        return next((e for e in self._entries if e.host_id == host_id), None)


_TRACE = FIELD_RECORDING
"""The trace a :func:`recording` collects into: the seam every recording site reads."""

_SOURCE: "ContextVar[str]" = ContextVar("otto_field_trace_source", default="")
"""The label of the lab source whose entries are being built (:func:`source_scope`)."""

_ENTRY: "ContextVar[EntryTrace | None]" = ContextVar("otto_field_trace_entry", default=None)
"""The host entry being built (:func:`entry_scope`), which a :func:`note` lands on."""


@contextlib.contextmanager
def recording() -> "Generator[FieldTrace, None, None]":
    """Record which layer supplies each value of every host built inside the block.

    Yields the :class:`FieldTrace` the notes collect into. Recording is
    synchronous, on the calling context; a block nested in another records
    into a trace of its own.
    """
    trace = FieldTrace()
    token = _TRACE.set(trace)
    try:
        yield trace
    finally:
        _TRACE.reset(token)


@contextlib.contextmanager
def source_scope(label: str) -> "Generator[None, None, None]":
    """Name the lab source *label* as the one whose entries the block builds."""
    token = _SOURCE.set(label)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def current_source() -> str:
    """Return the label of the lab source being loaded, or ``""`` outside :func:`source_scope`."""
    return _SOURCE.get()


@contextlib.contextmanager
def entry_scope(
    key: EntryKey, *, provenance: "Mapping[str, Any] | None" = None
) -> "Generator[None, None, None]":
    """Attribute every :func:`note` made in the block to the host entry *key*.

    *provenance* is the entry's element as its rendered document describes
    it. A no-op outside :func:`recording`.
    """
    trace = _TRACE.get()
    if trace is None:
        yield
        return
    entry = EntryTrace(key=key, provenance=provenance)
    trace._entries.append(entry)  # noqa: SLF001 — this module owns the trace
    token = _ENTRY.set(entry)
    try:
        yield
    finally:
        _ENTRY.reset(token)


def attributing() -> bool:
    """Return whether a note made now would be recorded: inside a recorded entry's build."""
    return _ENTRY.get() is not None


def joined(*, supplied: "frozenset[str]") -> None:
    """Record which host fields the inventory join decided for the entry being built.

    The lab loader calls it between the join and the build, so the factory's
    merge (:func:`note_merge`) attributes those fields to the record, or to
    the cred merges, and every other host field to the lab data.
    """
    entry = _ENTRY.get()
    if entry is not None:
        entry.supplied = supplied


def note(
    *,
    field: str,
    layer: FieldLayer,
    detail: "str | None" = None,
    scope: "str | None" = None,
    keep_prior: bool = False,
) -> None:
    """Record that *layer* supplied *field* of the entry being built; *detail* says what in it.

    *scope* is the credential identity a cred field belongs to: one login
    under two scopes is two credentials, each field with a winner of its own,
    and the identity is the winner's detail (so *detail* is not used with
    it). The later note of one field (and scope) replaces the earlier. With
    *keep_prior*, a field that already has a winner keeps it. A no-op outside
    a recorded entry.
    """
    entry = _ENTRY.get()
    if entry is None:
        return
    winners = entry._winners  # noqa: SLF001 — this module owns the trace
    slot = (field, scope)
    if keep_prior and slot in winners:
        return
    winners[slot] = FieldWinner(
        entry=entry.key, field=field, layer=layer, detail=detail if scope is None else scope
    )


PROFILE_RANK = 0
"""A merged value's source in :func:`note_merge`: the ``os_type`` profile's defaults."""

HOST_RANK = 1
"""A merged value's source in :func:`note_merge`: the host dict."""

PREFERENCE_RANK = 2
"""A merged value's source in :func:`note_merge`: a ``[host_preferences]`` option default."""


def note_merge(
    *,
    sources: "Mapping[str, int]",
    host_data: "Mapping[str, Any]",
    defaults: "Mapping[str, Any]",
    profile: str,
    selectors: "Mapping[str, Mapping[str, str]]",
) -> None:
    """Note the winner of each value the host factory's merge reports in *sources*.

    *sources* maps each merged value (``ip``, ``ssh_options.port``) to the
    layer it came from, as the merge overlaid them
    (:data:`PROFILE_RANK`, :data:`HOST_RANK`, :data:`PREFERENCE_RANK`), so
    the precedence is the merge's own. A host dict field is the record's
    when the inventory join supplied it (:func:`joined`), else the lab
    data's; a profile default names *profile*; an option default names its
    *selectors* entry. The creds the join decided were attributed by its
    cred merges; any other creds are attributed whole to the layer whose
    list the host got. A no-op outside a recorded entry.
    """
    entry = _ENTRY.get()
    if entry is None:
        return
    supplied = entry.supplied
    for name, rank in sources.items():
        top, _, option = name.partition(".")
        if top == "creds":
            continue
        if rank == PROFILE_RANK:
            note(field=name, layer=FieldLayer.OS_PROFILE, detail=profile)
        elif rank == HOST_RANK:
            layer = FieldLayer.RECORD if top in supplied else FieldLayer.LAB_DATA
            note(field=name, layer=layer)
        else:
            detail = selectors.get(top, {}).get(option)
            note(field=name, layer=FieldLayer.HOST_PREFERENCES, detail=detail)
    rank = sources.get("creds")
    if rank == HOST_RANK and "creds" not in supplied:
        note_cred_fields(creds=host_data.get("creds"), layer=FieldLayer.LAB_DATA)
    elif rank == PROFILE_RANK:
        note_cred_fields(creds=defaults.get("creds"), layer=FieldLayer.OS_PROFILE)


def preferences_offered(*, selectors: "Mapping[str, str]") -> None:
    """Record the selector of each ``[host_preferences]`` capability list offered to the entry."""
    entry = _ENTRY.get()
    if entry is not None:
        entry.preference_selectors = dict(selectors)


def preference_won(*, field: str) -> None:
    """Note that the ``[host_preferences]`` list for capability *field* decided its selection.

    Called by the capability resolver when an entry of a preference list is
    the active selection. The detail is the selector :func:`preferences_offered`
    recorded for *field*; a list nobody offered (a no-preference validation's
    probe) notes nothing. When the resolver falls back to the host's own pin,
    nothing is noted here, and the merge's note of the pin stands.
    """
    entry = _ENTRY.get()
    if entry is None:
        return
    selector = entry.preference_selectors.get(field)
    if selector is not None:
        note(field=field, layer=FieldLayer.HOST_PREFERENCES, detail=selector)


_CRED_IDENTITY_FIELDS = frozenset({"login", "protocols"})
"""A cred's identity (:func:`~otto.models.base.cred_identity`): a winner's detail, never a field."""


def _cred_field(name: str) -> str:
    """Return how cred field *name* is named as a host's value: ``creds.<name>``."""
    return f"creds.{name}"


def _layer_by_identity(creds: object) -> "dict[str, dict[str, Any]]":
    """Return the cred layer *creds* keyed as the merge keys it; ``{}`` for one it refuses.

    The keying is :func:`otto.models.base.cred_layer_by_identity`'s, the
    merge's own. A layer that is not a list, or that the keying refuses (an
    entry with no login, a duplicate identity), attributes nothing: the merge
    or the host's validation refuses it, so the host has no winner to explain.
    """
    from ..models import base  # the module, so the merge and this read one function

    if not isinstance(creds, list):
        return {}
    try:
        return base.cred_layer_by_identity(creds, name="attributed")
    except ValueError:
        return {}


def note_cred_merge(
    *, lower: object, higher: object, lower_layer: FieldLayer, higher_layer: FieldLayer
) -> None:
    """Note the winners of one cred merge (:func:`~otto.inventory.merge_creds`), higher over lower.

    Each field of each identity is a winner. A field the higher layer states
    is the higher layer's, replacing any earlier winner. A field only the
    lower layer states keeps the winner an earlier merge recorded for it, and
    is the lower layer's only when it has none: so a store password stays the
    store's through the record's merge and then the lab file's. Each
    winner's detail is the identity, never a value. A no-op outside a
    recorded entry.
    """
    if not attributing():
        return
    low = _layer_by_identity(lower)
    high = _layer_by_identity(higher)
    for identity, fields in low.items():
        over = high.get(identity, {})
        for name in fields:
            if name not in _CRED_IDENTITY_FIELDS and name not in over:
                note(field=_cred_field(name), layer=lower_layer, scope=identity, keep_prior=True)
    for identity, fields in high.items():
        for name in fields:
            if name not in _CRED_IDENTITY_FIELDS:
                note(field=_cred_field(name), layer=higher_layer, scope=identity)


def note_cred_fields(*, creds: object, layer: FieldLayer, keep_prior: bool = False) -> None:
    """Note each field *creds* states as *layer*'s.

    For creds that reach a host without a merge: an inline entry's own, a
    profile's default, or a record's passed through whole. With *keep_prior*
    a field an earlier merge recorded keeps its winner (a record's creds
    that the creds store's merge already attributed). A no-op outside a
    recorded entry.
    """
    if not attributing():
        return
    for identity, fields in _layer_by_identity(creds).items():
        for name in fields:
            if name not in _CRED_IDENTITY_FIELDS:
                note(field=_cred_field(name), layer=layer, scope=identity, keep_prior=keep_prior)


def built(*, host_id: str) -> None:
    """Record that the entry being built built the host *host_id*. A no-op outside one."""
    entry = _ENTRY.get()
    if entry is not None:
        entry.host_id = host_id


def trace_position() -> "int | None":
    """Return how many entries the recording holds now, or ``None`` outside :func:`recording`.

    The mark a load takes before its sources build, for :func:`retain_winners`.
    """
    trace = _TRACE.get()
    return None if trace is None else len(trace._entries)  # noqa: SLF001 — this module owns it


def retain_winners(*, since: int, owners: "Mapping[str, str]") -> None:
    """Keep, of the entries recorded after *since*, only those that built a surviving host.

    *owners* maps each host id the load kept to the label of the source whose
    element won it. An entry whose host was dropped, or that came from a
    source whose element lost, is forgotten. A no-op outside :func:`recording`.
    """
    trace = _TRACE.get()
    if trace is None:
        return
    entries = trace._entries  # noqa: SLF001 — this module owns the trace
    entries[since:] = [
        e
        for e in entries[since:]
        if e.host_id is not None and owners.get(e.host_id) == e.key.source
    ]
