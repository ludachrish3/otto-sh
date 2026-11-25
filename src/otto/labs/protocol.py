"""``LabRepository`` protocol — the DB-agnostic interface all lab-repository backends satisfy."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    runtime_checkable,
)

from typing_extensions import final

from ..host.inventory_ref import InventoryRef
from .drops import Diagnostic

if TYPE_CHECKING:
    # Annotation only: runtime code never constructs a Lab from this module,
    # so the string annotations below are read by static type-checking alone.
    from ..inventory.protocol import Inventory
    from ..inventory.view import InventoryView
    from ..link.derive import DeclaredLinkEntry, HostAddressing
    from .loader import Lab


@runtime_checkable
class LabRepository(Protocol):
    """DB-agnostic interface for loading labs.

    A backend is configured at construction time (the built-in JSON backend
    takes its ``search_paths`` in ``__init__``), then queried through the two
    methods below. Selection and construction happen in
    :func:`otto.labs.build_lab_sources`, one instance per ``[[lab.sources]]``
    entry.
    """

    def load_lab(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> "Lab":
        """Load a lab by name.

        Parameters
        ----------
        name : str
            Name of the lab to load.
        preferences : dict[str, dict[str, Any]] | None
            The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
            product-preference table forwarded to the factory, which matches each
            host's ``id`` and applies the result. ``None`` reproduces today's
            behavior.
        inventory : Inventory | None
            The process inventory referenced entries resolve against (spec
            2026-08-28 host-inventory §6); ``None`` means a referenced entry
            is an error.

        Returns
        -------
        Lab
            Fully constructed lab.

        Raises
        ------
        LabNotFoundError
            If no lab named ``name`` exists.
        LabRepositoryError
            If the backend fails to satisfy the query (I/O, parse, network).
        """
        ...

    def list_labs(self) -> list[str]:
        """List all lab names this backend can provide.

        Returns
        -------
        list[str]
            Lab names (every element a ``str``).
        """
        ...


@final
@dataclass(frozen=True)
class LoginSummary:
    """One login a host's record declares — identity only, never a secret.

    ``protocols`` is the cred's scope (empty = every protocol); ``proxy`` is
    whether reaching this login takes a login-proxy hop. That is exactly what
    ``otto host <id> <verb> --user <TAB>`` needs to offer the logins the verb
    accepts (``get``/``put`` take direct logins only; ``exec``/``login`` replay
    hops) and to narrow by ``--term``. Password, ``via``, ``params`` and the
    proxy key stay in the record.
    """

    login: str
    protocols: list[str] = field(default_factory=list)
    proxy: bool = False


@final
@dataclass(frozen=True, kw_only=True)
class HostSummary:
    """Identity and addressing for one host, without constructing it.

    What tab completion and tunnel path-narrowing need to *name* and *reach*
    a host. Deliberately small: identity and addressing only. Login *names*
    are identity (``logins``); passwords, proxy chains, interfaces,
    transports and options are a job for
    :meth:`~otto.labs.protocol.LabRepository.load_lab`, not for a second host
    model growing here.

    ``frozen=True`` blocks attribute rebinding, not mutation of ``labs`` (or
    ``lab_patterns``) — producers deliberately append to them while merging a
    host that appears in several labs, before the summary is handed out. (A consequence: a summary
    is not hashable. Key collections by ``.id``.)
    """

    id: str
    """The host's canonical id — byte-identical to the built host's ``.id``."""

    labs: list[str] = field(default_factory=list)
    """Lab names this host belongs to."""

    lab_patterns: list[str] = field(default_factory=list)
    """Membership patterns of the element this host belongs to (json backend);
    the composite re-resolves them against every source's declared labs so
    ``labs`` is complete across sources. Empty for backends that return
    concrete ``labs``."""

    ip: str = ""
    """Management address, or ``""`` when the backend does not expose one."""

    docker_capable: bool = False
    """Whether the host can host containers (drives ``otto docker --parent``)."""

    docker_priority: int = 0
    """The lab's rank of this host as a default docker parent; highest wins."""

    os_type: str | None = None
    """The entry's ``os_type`` selector as the backend recorded it — the json
    backend applies the factory's default (``"unix"``) when the entry names
    none — or ``None`` when the backend does not know it. Completion resolves
    it to a host class (``get_os_profile(os_type, data=...).base``, seeing the
    selected projects' data profiles) WHEN IT RESOLVES, so ``otto host <id> <TAB>``
    can offer that class's verbs without building the host; a selector no
    profile layer holds in the completing process resolves to nothing and the
    host keeps the union menu, exactly as one with
    no ``os_type`` does. A backend that implements
    :class:`SupportsHostSummaries` is expected to fill this in — the
    conformance suite compares it against the constructed host."""

    logins: list[LoginSummary] = field(default_factory=list)
    """The record's login identities (:class:`LoginSummary`), or ``[]`` when
    the backend does not fill them — then ``--user`` completes to nothing on
    that host, and nothing else changes."""

    inventory_ref: InventoryRef = field(default_factory=InventoryRef)
    """The qualified identity this summary was resolved from.

    The inventory's name and key, and whether the entry named the inventory
    (see :class:`~otto.host.inventory_ref.InventoryRef`). Empty for an inline
    host, and for a backend that does not fill it."""


def logins_of_creds(*, creds: "Iterable[Any]") -> list[LoginSummary]:
    """Summarise cred entries (``Cred`` or ``CredSpec``) to login identities, record order.

    Reads the three public identity fields every cred shape carries and
    nothing else, so a password can never ride along.
    """
    return [
        LoginSummary(
            login=str(c.login),
            protocols=[str(p) for p in (getattr(c, "protocols", None) or [])],
            proxy=getattr(c, "proxy", None) is not None,
        )
        for c in creds
    ]


def logins_of_host_data(host_data: "Mapping[str, Any]") -> list[LoginSummary]:
    """Login identities from a raw record's ``creds`` list — the fast path's data-only read.

    A backend calls this on the SAME record it handed
    :func:`~otto.host.factory.host_identity`, which already applied the host
    spec's validation; anything odd left is skipped rather than raised, as
    every summary field is.
    """
    out: list[LoginSummary] = []
    for entry in host_data.get("creds") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("login"), str):
            continue
        protocols = entry.get("protocols") or []
        out.append(
            LoginSummary(
                login=entry["login"],
                protocols=[str(p) for p in protocols] if isinstance(protocols, list) else [],
                proxy=entry.get("proxy") is not None,
            )
        )
    return out


@runtime_checkable
class SupportsHostSummaries(Protocol):
    """Optional capability: enumerate hosts without constructing them.

    A backend that can answer "which hosts exist, and what are their ids /
    labs / addresses" more cheaply than a full :meth:`LabRepository.load_lab`
    implements this; otto detects it structurally and uses it for tab
    completion and tunnel narrowing.

    Implementing it is OPTIONAL and purely an optimization: a backend that
    omits it still gets completion, because
    :func:`otto.labs.host_summaries` falls back to ``list_labs`` +
    ``load_lab``. The fallback is always correct, just slower.

    An implementation MUST agree with ``load_lab`` in three ways, all checked
    by :func:`otto.testing.assert_lab_repository_conforms`: every id
    byte-identical to the one ``load_lab`` reports (an id that does not
    round-trip is worse than no completion, because it offers the user
    something that will not dispatch); every host ``load_lab`` produces
    summarized, or completion silently stops offering it; and every FIELD
    equal to the built host's. The fields have defaults so this dataclass
    will let you omit them, but each drives a surface — see the class
    docstring above. Backends over raw records should reach
    for :func:`otto.host.factory.host_identity`, which applies the same
    profile merge and validation the host factory applies.
    """

    def list_host_summaries(self, *, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Every host this backend knows, across every lab.

        *inventory* is the process inventory referenced entries resolve
        against (spec 2026-08-28 host-inventory §6); ``None`` means a
        referenced entry cannot be identified and is skipped, like any other
        unresolvable record.
        """
        ...


@final
@dataclass(frozen=True, kw_only=True)
class LabStatus:
    """Whether one lab a source declares can be loaded now, and why not."""

    name: str
    """The lab's name."""

    unavailable: Diagnostic | None = None
    """Why the lab cannot be loaded, or ``None`` when it can."""

    @property
    def available(self) -> bool:
        """Return whether the lab can be loaded, which is when :attr:`unavailable` is ``None``."""
        return self.unavailable is None


@final
@dataclass(frozen=True, kw_only=True)
class LabScope:
    """The lab names a source can declare: exact :attr:`names` and literal :attr:`prefixes`."""

    names: frozenset[str] = frozenset()
    """Lab names the source may declare, exactly."""

    prefixes: frozenset[str] = frozenset()
    """Literal prefixes (not patterns) of the lab names the source may declare."""

    def contains(self, *, name: str) -> bool:
        """Return whether lab *name* is in this scope: an exact name, or one with a prefix."""
        return name in self.names or any(name.startswith(prefix) for prefix in self.prefixes)


@final
@dataclass(frozen=True, kw_only=True)
class SourceRef:
    """One lab source, as a report names it."""

    label: str
    """The source's label, e.g. ``json:/home/me/lab``."""

    project_root: Path | None
    """The root of the project that declared the source, or ``None`` for none."""


@runtime_checkable
class SupportsInventoryView(Protocol):
    """Optional capability: load a lab through the whole inventory view.

    A backend that implements it receives the
    :class:`~otto.inventory.InventoryView`, so a qualified ``{"name": ...,
    "key": ...}`` reference reaches the named inventory. A backend without it
    is called through :meth:`LabRepository.load_lab` with
    :attr:`~otto.inventory.InventoryView.bare_inventory`
    (:func:`otto.labs.load_lab_through` decides it).
    """

    def load_lab_in_view(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        view: "InventoryView",
    ) -> "Lab":
        """Load lab *name*, resolving every reference through *view*.

        *preferences* is :meth:`LabRepository.load_lab`'s. Resolve each entry
        with :func:`~otto.inventory.resolve_view_entry`, and raise as
        :meth:`LabRepository.load_lab` does.
        """
        ...


@runtime_checkable
class SupportsHostSummariesInView(Protocol):
    """Optional capability: :class:`SupportsHostSummaries` resolved through the inventory view.

    The same contract, and the same conformance rules; only the inventory
    argument is the whole view.
    """

    def list_host_summaries_in_view(self, *, view: "InventoryView") -> list[HostSummary]:
        """Every host this backend knows, across every lab, each reference resolved through *view*.

        Fill each summary's :attr:`HostSummary.inventory_ref` from the
        resolved entry. An entry the view cannot resolve is skipped.
        """
        ...


@runtime_checkable
class SupportsLabStatus(Protocol):
    """Optional capability: say which declared labs can be loaded now.

    A backend without it reports every :meth:`LabRepository.list_labs` name
    available (:func:`otto.labs.lab_statuses`).
    """

    def list_lab_status(self, *, view: "InventoryView") -> list[LabStatus]:
        """Return one :class:`LabStatus` per lab this backend declares."""
        ...


@runtime_checkable
class SupportsLabScope(Protocol):
    """Optional capability: declare which lab names this backend can declare at all.

    A scoped source is consulted only for names inside its scope, so its
    failure spares labs outside it. ``None`` means unscoped.
    """

    def lab_scope(self) -> LabScope | None:
        """Return this backend's :class:`LabScope`, or ``None`` when it is unscoped."""
        ...


@runtime_checkable
class SupportsOverridePolicy(Protocol):
    """Optional capability: replace another source's machines only where the backend says so.

    When an element of such a backend replaces an element an earlier source
    contributed to the lab, the composite compares the two elements'
    machines as a whole (:func:`~otto.host.inventory_ref.same_machines`:
    every machine of each is one of the other's). The same machines override as any
    element does, with the override warning; different ones are refused,
    naming both sources and both sets of machines, unless the element's slug
    is in :meth:`declared_overrides`. A backend without it overrides by
    today's rule, whatever it replaces.
    """

    def declared_overrides(self) -> frozenset[str]:
        """Return the element slugs whose replacement of different machines is intended."""
        ...


@final
@dataclass(frozen=True, kw_only=True)
class SkippedHostRecord:
    """A host record a source found and could not read, so no link can resolve against it.

    A source skips such a record rather than fail the load: it may belong to
    another lab. When a link names a host nothing addresses, otto reports the
    skipped records that could be that host instead of calling it unknown.
    """

    element: str
    """The name of the element the record belongs to.

    Host ids are ``<element>-<board>[-<slot>]``, and a record that cannot be
    read has no id, so a skipped record is matched to an endpoint by this name.
    """

    origin: Path
    """The document the record is in."""

    reason: str
    """Why the record could not be read.

    Shown to the user in the link's error, so it must not carry a secret from
    the record (a password, a token): describe the problem, not the value.
    """


def _no_skipped_records() -> "Sequence[SkippedHostRecord]":
    return ()


@final
@dataclass(frozen=True, kw_only=True)
class DeclaredLinkInputs:
    """A source's declared links, unresolved, and the addressing they resolve against.

    Source-wide: every ``links`` entry of every document of the source, for
    all of its labs at once, so a link reaching a host of another lab (a
    dangling end) still resolves.
    """

    entries: "list[DeclaredLinkEntry]"
    """Every raw ``links`` entry of the source, in document then index order."""

    addressing: "Callable[[], Mapping[str, HostAddressing]]"
    """Return the addressing of every host record of the source, by host id.

    Otto calls it at most once per load, and only when an attached link needs
    an endpoint: a link of this source, or a link of another source whose
    endpoint neither that source nor the merged lab addresses. A load whose
    links all resolve without it, or that has no links, never calls it.
    """

    skipped: "Callable[[], Sequence[SkippedHostRecord]]" = _no_skipped_records
    """Return the host records the source found and could not read. Optional.

    Otto calls it only when an attached link names an endpoint nothing
    addresses, to say that the host's record was skipped and why. A source
    that reports none gets the plain "unknown host" error.
    """


@final
@dataclass(frozen=True, kw_only=True)
class LabContribution:
    """One source's contribution to a lab: its hosts, and its declared links unresolved.

    A source that neither declares nor matches the lab has :attr:`lab`
    ``None`` and says why in :attr:`not_found`, but may still carry links (a
    source that holds only links) or the addressing another source's link
    resolves against.
    """

    lab: "Lab | None"
    """The source's hosts, resources and metadata for the lab; ``None`` if none.

    Its ``links`` are ignored: composition replaces them with the links it
    resolves from :attr:`links`.
    """

    not_found: str | None
    """Why the source contributes no hosts, when :attr:`lab` is ``None``."""

    links: DeclaredLinkInputs
    """The source's declared links and their addressing, resolved by the composite."""


@runtime_checkable
class SupportsLabContribution(Protocol):
    """Optional capability: contribute a lab's hosts and leave its links to the composite.

    The composite asks every such source for its contribution, merges the
    hosts, and then resolves every source's links once, over the composed
    lab, so a link may name a host another source declares. A backend
    without it is loaded through :func:`~otto.labs.load_lab_through` and
    keeps resolving its own links.
    """

    def load_contribution(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        view: "InventoryView",
    ) -> LabContribution:
        """Return this source's contribution to lab *name*, each reference resolved through *view*.

        Resolves no link. *preferences* and the errors are
        :meth:`SupportsInventoryView.load_lab_in_view`'s, except that a source
        that does not hold the lab returns a contribution with no lab rather
        than raising :class:`~otto.labs.LabNotFoundError`.
        """
        ...


@runtime_checkable
class SupportsDeclaredLinks(Protocol):
    """Optional capability: list a source's declared links without loading a lab.

    ``otto link`` completion reads it, through the operation's view, for
    every link id it offers. A source without it has the links of the files
    it lists read (:meth:`~otto.labs.sources.LabSourceState.lab_files`).
    """

    def declared_link_entries(self, *, view: "InventoryView") -> "list[DeclaredLinkEntry]":
        """Return every raw ``links`` entry of the source, best-effort, building no host.

        What the best-effort read left out is recorded as a drop
        (``otto.labs.drops.record_drop``), as a best-effort listing records it.
        """
        ...
