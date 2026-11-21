"""``CompositeLabRepository`` — N ordered host sources behind one ``LabRepository``.

The combining layer for ``[[lab.sources]]`` (the multi-source lab data and
lab definition v2 specs, §6 of each). Sources are
consulted in declaration order; the LATER source wins wholesale at RECORD
granularity, with a warning naming both sources — the sanctioned way to test a
data change locally before it lands in a global database. Under v2 there are
two such records: the **element**, keyed by its slug (``host.element.slug``;
its hosts, membership and metadata replaced together), and the **``labs``
table entry** (its resources and metadata replaced together). Never a
field-level blend: a hybrid record — this source's hosts with that source's
metadata — is unrepresentable by design.

This class is also the single owner of lab EXISTENCE: a lab exists once some
source's ``list_labs()`` declares it, and only then (spec §2.1). Elements that
match a name nothing declares do not conjure a lab, and a declared lab that no
element matches is a definition mistake, not an empty lab — both are loud here
and nowhere else.

This merge is deliberately NOT ``Lab.__add__``: that operator merges DIFFERENT
labs (``a+b``, same-ip dedup / different-ip error); this class merges the SAME
lab across sources, and the two rule sets must never blend.
"""

# Specs: docs/superpowers/specs/2026-08-19-multi-source-lab-data-design.md §6 and
# docs/superpowers/specs/2026-08-27-lab-definition-v2-design.md §6.

import dataclasses
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from typing_extensions import final

from .composition import SourceContribution, compose_links, contribution_part
from .drops import Diagnostic, is_timeout
from .errors import LabNotFoundError, LabRepositoryError, LabUnavailableError
from .protocol import (
    HostSummary,
    LabScope,
    LabStatus,
    SourceRef,
    SupportsDeclaredLinks,
    SupportsLabStatus,
    SupportsOverridePolicy,
)
from .views import lab_scope_of, lab_statuses, load_contribution_through, load_lab_through

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from ..host.inventory_ref import MachineIdentity
    from ..inventory.protocol import Inventory
    from ..inventory.view import InventoryView
    from ..link.derive import DeclaredLinkEntry
    from ..templating.errors import RenderCancelled
    from .loader import Lab
    from .protocol import LabRepository

logger = logging.getLogger(__name__)

# An element's identity as this module keys it: ``host.element.slug``.
# Built from the HOSTS a source returned, not from the file's ``elements``
# entries — a backend need not have a file at all, and every backend's hosts
# carry the element. A local dict key, never a return value.
_ElementKey = str


@final
@dataclass
class LabSource:
    """One constructed host source: its user-facing label, its backend and its project."""

    label: str
    repository: "LabRepository"
    project_root: Path | None = None
    """The root of the project that declared the source, or ``None`` for none in particular.

    Every call to the source goes through the inventory view as this project
    sees it (:meth:`~otto.inventory.InventoryView.for_project`), so a project
    that refuses bare references (lab templating D1) refuses them in its own
    sources only."""


@final
@dataclass(frozen=True, kw_only=True)
class SourceUnavailable:
    """A scoped source whose status call failed: every lab in its scope is unavailable."""

    source: SourceRef
    """The source that failed."""

    scope: LabScope
    """The source's declared scope, which is every lab the failure reaches."""

    diagnostic: Diagnostic
    """The source's label and its error text."""

    timed_out: bool = False
    """Whether the status call was cancelled (``otto.labs.drops.is_timeout``), not a fault.

    Kept rather than the error itself: a report outlives the call, and an
    error's traceback frames can hold a backend's client and its credential.
    :meth:`cancellation` rebuilds the deadline from it, so a consumer tells a
    deadline from a fault through the chain rather than by the diagnostic's
    words."""

    def cancellation(self) -> "RenderCancelled | None":
        """Return a fresh cancellation for an outage that :attr:`timed_out`; ``None`` for a fault.

        The one rebuild of the deadline from the flag:
        :meth:`LabStatusReport.refusal` chains it as its error's ``__cause__``,
        and a consumer that reports outages (the ``otto init`` doctor) raises
        it rather than showing the deadline as the source's failure.
        """
        if not self.timed_out:
            return None
        from ..templating.errors import RenderCancelled  # lazy: a deadline only

        return RenderCancelled(self.diagnostic.reason)


@final
@dataclass(frozen=True, kw_only=True)
class SourceLinks:
    """One source's declared links, unresolved, and its scope.

    A row of :meth:`CompositeLabRepository.declared_links_in_view`. Rows
    compare by value; a row is not hashable: it holds a list.
    """

    __hash__ = None  # type: ignore[assignment]  # value equality over a mutable field; never a key

    source: SourceRef
    """The source."""

    entries: "list[DeclaredLinkEntry] | None"
    """Its ``links`` entries, or ``None`` for a source without the capability to list them.

    ``[]`` for a source whose listing failed (a drop records why)."""

    scope: LabScope | None
    """The source's declared lab scope (:func:`~otto.labs.lab_scope_of`), or ``None``."""


@final
@dataclass(frozen=True, kw_only=True)
class HostListing:
    """Every host a composite's sources summarize, and every lab they declare, from one pass.

    Built by :meth:`CompositeLabRepository.host_listing_in_view`, best-effort.
    Two listings compare by value. A listing is not hashable: it holds lists.
    """

    __hash__ = None  # type: ignore[assignment]  # value equality over mutable fields; never a key

    names: list[str]
    """Every lab some source declares now, sorted; a source that failed declares none."""

    summaries: list[HostSummary]
    """Every host summary, by id, its memberships resolved against :attr:`names`."""


@final
@dataclass(frozen=True, kw_only=True)
class LabStatusReport:
    """Every lab's availability across a composite's sources, decided once.

    Built by :meth:`CompositeLabRepository.lab_status_report`. The last
    declaring source's status wins wholesale; a lab inside the scope of a
    failed source (:attr:`unavailable_sources`) is unavailable with that
    source's diagnostic, whatever any source declares.

    Two reports compare by value. A report is not hashable: it holds lists
    and a dict.
    """

    __hash__ = None  # type: ignore[assignment]  # value equality over mutable fields; never a key

    labs: list[LabStatus]
    """Every declared lab's status, sorted by name."""

    declared_by: dict[str, list[SourceRef]]
    """Each declared lab's declaring sources, in source order (the last one owns the entry)."""

    unavailable_sources: list[SourceUnavailable]
    """Each scoped source whose status call failed, in source order."""

    def status(self, *, name: str) -> LabStatus | None:
        """Return lab *name*'s status, or ``None`` when no source declares it."""
        return next((s for s in self.labs if s.name == name), None)

    def refusal(self, *, name: str) -> LabUnavailableError | None:
        """Return the error that loading lab *name* must raise now, or ``None``.

        A failed source whose scope contains *name* comes first (a
        declaration cannot clear an outage), then the lab's own status. A
        cancelled outage's refusal is raised from its
        :meth:`SourceUnavailable.cancellation`.
        """
        outage = _outage_for(name=name, outages=self.unavailable_sources)
        if outage is not None:
            refusal = LabUnavailableError(name=name, diagnostic=outage.diagnostic, declared=False)
            refusal.__cause__ = outage.cancellation()
            return refusal
        status = self.status(name=name)
        if status is not None and status.unavailable is not None:
            return LabUnavailableError(name=name, diagnostic=status.unavailable, declared=True)
        return None

    def statuses_declared_in(self, *, project_root: Path) -> list[LabStatus]:
        """Return the status of each lab some source of the project at *project_root* declares.

        Sorted by name, in one pass over :attr:`labs`: a caller rendering
        every row reads each status here rather than asking :meth:`status`
        once per lab.
        """
        return [
            s
            for s in self.labs
            if any(ref.project_root == project_root for ref in self.declared_by.get(s.name, []))
        ]

    def labs_declared_in(self, *, project_root: Path) -> list[str]:
        """Return the labs some source of the project at *project_root* declares, sorted."""
        return [s.name for s in self.statuses_declared_in(project_root=project_root)]


@final
class CompositeLabRepository:
    """Merge N ``LabRepository`` backends, later sources overriding earlier.

    Satisfies ``LabRepository`` and ``SupportsHostSummaries``, so every
    downstream consumer (``labs.loader.load_lab``, completion, conformance)
    works unchanged, and the view capabilities
    (:class:`~otto.labs.SupportsInventoryView`,
    :class:`~otto.labs.SupportsHostSummariesInView`,
    :class:`~otto.labs.SupportsLabStatus`). The legacy methods are the view
    methods over :meth:`~otto.inventory.InventoryView.of_default`. Each source
    is called through the view as its own project sees it
    (:attr:`LabSource.project_root`). An EMPTY composite is valid: it lists
    no labs and ``load_lab`` fails loud with configuration guidance — the
    state of a process where no project declares a ``[[lab.sources]]`` entry.
    """

    def __init__(self, sources: "list[LabSource]") -> None:
        self.sources: "list[LabSource]" = list(sources)

    def load_lab(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> "Lab":
        """Merge *name* across every source that knows it (spec §6).

        :meth:`load_lab_in_view` over a view holding only *inventory* as
        ``default`` (:meth:`~otto.inventory.InventoryView.of_default`): every
        source resolves its bare references through the same inventory, and
        two sources resolving the same key against different records is the
        drift this layer exists to prevent.

        Raises
        ------
        LabNotFoundError
            As :meth:`load_lab_reported`.
        LabUnavailableError
            As :meth:`load_lab_reported`.
        LabRepositoryError
            As :meth:`load_lab_reported`.
        """
        from ..inventory.view import InventoryView  # lazy: keep the composite import-light

        return self.load_lab_in_view(
            name, preferences=preferences, view=InventoryView.of_default(inventory=inventory)
        )

    def load_lab_in_view(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        view: "InventoryView",
    ) -> "Lab":
        """Merge *name* across every source, each resolving references through *view*.

        Computes :meth:`lab_status_report` and loads through
        :meth:`load_lab_reported`.

        Raises
        ------
        LabNotFoundError
            As :meth:`load_lab_reported`.
        LabUnavailableError
            As :meth:`load_lab_reported`.
        LabRepositoryError
            As :meth:`load_lab_reported`, or from :meth:`lab_status_report`.
        """
        report = self.lab_status_report(view=view)
        return self.load_lab_reported(name, preferences=preferences, view=view, report=report)

    def load_lab_reported(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        view: "InventoryView",
        report: LabStatusReport,
    ) -> "Lab":
        """Merge *name* across every source that may contribute it, as *report* decided (spec §6).

        Availability first: the error :meth:`LabStatusReport.refusal` names
        is raised before any source is loaded. Then existence: *name* must be
        DECLARED by some source (:attr:`LabStatusReport.declared_by`). A
        source whose scope excludes *name* is skipped, so a scoped source that
        is down never fails a lab outside its scope. Each remaining source's
        contribution is loaded under the view of its own project
        (:func:`~otto.labs.load_contribution_through`, else
        :func:`~otto.labs.load_lab_through`) and merged in order — elements
        replaced wholesale by slug, the ``labs`` entry replaced wholesale by
        the last declaring source. An element from a source with an override
        policy (:class:`~otto.labs.SupportsOverridePolicy`) replaces another
        source's element only when the two hold the same machines
        (:func:`~otto.host.inventory_ref.same_machines`) or the policy
        declares the element. The declared links are then composed once,
        over the merged hosts (:func:`~otto.labs.composition.compose_links`),
        so a link may name a host another source declares; a source that
        holds only links still contributes them. A scoped source's link
        resolution fails as its load does: the lab is unavailable.

        Raises
        ------
        LabUnavailableError
            If *report* refuses *name*, or a scoped source fails while
            loading it or resolving its links (``declared=False``; *report*
            is not rewritten).
        LabNotFoundError
            If no source declares *name* (message names every source), no
            source provides it, or no sources are configured at all. The first
            two carry each source's own not-found text, one indented line
            apiece — for the json backend, the search paths it looked in.
        LabRepositoryError
            If *name* is declared but no element anywhere matches it, if two
            surviving elements produce the same host id, if an element from a
            source with an override policy would replace different machines
            the policy does not declare, if a declared link
            touching the lab does not resolve, or propagated from the first
            failing unscoped source — a broken backend is never silently
            dropped from the merge.
        """
        self._admit(name, report=report)
        declared_by = [ref.label for ref in report.declared_by.get(name, [])]
        loaded: "list[tuple[str, Lab]]" = []
        # Each source's answer, in source order: its part in the lab's links,
        # composed once the hosts are merged so no source's links resolve
        # against it alone, and the source's error rules they resolve under.
        answers: list[_Loaded] = []
        # The declared overrides of each source with an override policy.
        policies: dict[str, frozenset[str]] = {}
        # Each source's own not-found text, kept for the failure messages below.
        # Since R15 every load goes through this class, so a reason discarded
        # here is a reason the user never sees — and for the json backend that
        # reason is the ONLY place the searched paths are named. A mistyped
        # `paths` entry would otherwise be answered with "add a 'labs' table",
        # pointing at a file otto never found.
        reasons: list[str] = []
        # Where this load's recorded entries begin, when ``otto lab explain``
        # records the build: only the entries of winning elements are kept.
        since = _trace_position()
        from ..host.inventory_ref import NOT_RECORDING  # lazy: keep the composite import-light

        for source in self.sources:
            try:
                with NOT_RECORDING if since is None else _source_scope(source.label):
                    got = _load_from(source=source, name=name, preferences=preferences, view=view)
            except LabNotFoundError as e:  # a per-source protocol signal, not a failure
                reasons.append(f"{source.label}: {e}")
                continue
            if got is None:
                continue
            if got.lab is None:
                reasons.append(f"{source.label}: {got.not_found}")
            else:
                loaded.append((source.label, got.lab))
            answers.append(got)
            if isinstance(source.repository, SupportsOverridePolicy):
                policies[source.label] = source.repository.declared_overrides()
        if not declared_by:
            # Loading first buys the second sentence: "your elements DO match
            # this name, you just never declared it" is the actual mistake in
            # the migration case, and it is invisible from ``list_labs`` alone.
            labels = ", ".join(s.label for s in self.sources)
            raise LabNotFoundError(
                f"Lab {name!r} is not declared by any configured source ({labels}). "
                f"A lab exists only once a 'labs' table entry (or a backend's list_labs) "
                f"declares it"
                + (" — elements match it by pattern but nothing declares it." if loaded else ".")
                + _reason_block(reasons)
            )
        if not loaded:
            labels = ", ".join(s.label for s in self.sources)
            raise LabNotFoundError(
                f"Lab {name!r} not found in any configured source: {labels}"
                + _reason_block(reasons)
            )

        first_label, merged = loaded[0]
        # Two indexes over the SAME merge state: which element each surviving
        # host id came from, and which source currently owns each element.
        element_of: "dict[str, _ElementKey]" = {}
        won_by: "dict[_ElementKey, str]" = {}
        for host_id, host in merged.hosts.items():
            key = _element_key(host)
            element_of[host_id] = key
            won_by[key] = first_label
        lab_entry_from = first_label if first_label in declared_by else None
        for label, lab in loaded[1:]:
            _merge_elements(
                name=name,
                merged=merged,
                lab=lab,
                label=label,
                element_of=element_of,
                won_by=won_by,
                policy=policies.get(label),
            )
            if label in declared_by:
                if lab_entry_from is not None:
                    logger.warning("labs entry %r: %s overrides %s", name, label, lab_entry_from)
                # Wholesale: resources and metadata travel together, so an
                # override never leaves half of the previous declaration behind.
                merged.resources = set(lab.resources)
                merged.metadata[name] = dict(lab.metadata.get(name, {}))
                lab_entry_from = label
        if not merged.hosts:
            raise LabRepositoryError(
                f"Lab {name!r} is declared by {', '.join(declared_by)} but no element in any "
                f"source matches it — add a 'labs' pattern to an element or remove the declaration"
            )
        merged.links = compose_links(
            lab=merged, contributions=[a.part for a in answers], guard=_GuardOfPart(answers)
        )
        if since is not None:
            # Attribution follows the winning element: a host's values are the
            # winning source's, so a losing source's entries explain nothing.
            from ..host import provenance

            provenance.retain_winners(
                since=since, owners={hid: won_by[element_of[hid]] for hid in merged.hosts}
            )
        return merged

    def _admit(self, name: str, *, report: LabStatusReport) -> None:
        """Raise unless lab *name* may be loaded at all: *report*'s refusal, or no sources."""
        refusal = report.refusal(name=name)
        if refusal is not None:
            raise refusal
        if not self.sources:
            raise LabNotFoundError(
                f"Lab {name!r} cannot be loaded: no project declares a "
                "[[lab.sources]] entry in .otto/settings.toml"
            )

    def lab_status_report(self, *, view: "InventoryView") -> LabStatusReport:
        """Ask every source which labs it declares and whether each can be loaded now.

        Each source is asked through :func:`~otto.labs.lab_statuses` under
        the view of its own project. The last declaring source's status wins
        wholesale. A scoped source (:func:`~otto.labs.lab_scope_of`) whose
        status call fails with a :class:`LabRepositoryError` or an
        ``InventoryError`` (other than :class:`LabNotFoundError`) becomes a
        :class:`SourceUnavailable`, and every declared lab inside its scope is
        reported unavailable with its diagnostic.

        Raises
        ------
        LabRepositoryError
            If a scoped source reports a lab outside its scope, or
            propagated from an unscoped source's failure, as today.
        """
        statuses: dict[str, LabStatus] = {}
        declared_by: dict[str, list[SourceRef]] = {}
        outages: list[SourceUnavailable] = []
        for source in self.sources:
            scope = lab_scope_of(repository=source.repository)
            reported = _scoped_call(
                source=source,
                scope=scope,
                call=lambda source=source: lab_statuses(
                    repository=source.repository,
                    view=project_view(view=view, project_root=source.project_root),
                ),
            )
            if isinstance(reported, _SourceFailure):
                outages.append(
                    SourceUnavailable(
                        source=_source_ref(source),
                        scope=reported.scope,
                        diagnostic=reported.diagnostic,
                        timed_out=is_timeout(reported.error),
                    )
                )
                # The names it still lists are known, so they are shown, each
                # unavailable with its reason (``_status_under``), and the
                # statuses agree with ``list_labs``.
                reported = _names_of_down_source(source=source, scope=reported.scope)
            for status in reported:
                if scope is not None and not scope.contains(name=status.name):
                    raise LabRepositoryError(
                        f"source {source.label} reports lab {status.name!r}, which is outside "
                        "its declared scope"
                    )
                statuses[status.name] = status
                declared_by.setdefault(status.name, []).append(_source_ref(source))
        labs = [_status_under(statuses[name], outages=outages) for name in sorted(statuses)]
        return LabStatusReport(labs=labs, declared_by=declared_by, unavailable_sources=outages)

    def list_lab_status(self, *, view: "InventoryView") -> list[LabStatus]:
        """Return :attr:`LabStatusReport.labs` of :meth:`lab_status_report`."""
        return self.lab_status_report(view=view).labs

    def list_labs(self) -> list[str]:
        """Sorted union of every source's ``list_labs()``; a backend error propagates (spec §6.2).

        The required :class:`~otto.labs.LabRepository` method, which never asks
        a source for a status. otto itself reads the labs a composite declares
        through a view instead (:meth:`lab_status_report`, and completion's
        :meth:`host_listing_in_view`), so a ``jsonnet`` source's recorded
        names (diagnostics, see its ``list_labs``) reach no listing of otto's.
        """
        names: set[str] = set()
        for source in self.sources:
            names.update(source.repository.list_labs())
        return sorted(names)

    def list_host_summaries(self, *, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """:meth:`list_host_summaries_in_view` over a view holding *inventory* as ``default``."""
        from ..inventory.view import InventoryView  # lazy: keep the composite import-light

        return self.list_host_summaries_in_view(view=InventoryView.of_default(inventory=inventory))

    def list_host_summaries_in_view(self, *, view: "InventoryView") -> list[HostSummary]:
        """Union by host id, later source wins, memberships unioned (spec §6.3).

        :attr:`HostListing.summaries` of :meth:`host_listing_in_view`.
        """
        return self.host_listing_in_view(view=view).summaries

    def host_listing_in_view(self, *, view: "InventoryView") -> HostListing:
        """Every source's host summaries and declared labs, through *view*, in one pass.

        Each source is asked through the view of its own project for the labs
        it declares (:func:`~otto.labs.lab_statuses`, so every source's
        status read, its render included, is the one a listing makes) and for
        its host summaries (:func:`~otto.labs.host_summaries_in_view`).
        Summaries union by host id, the later source winning, and their
        memberships are RE-RESOLVED: a summary carries the membership
        patterns of its element, and a pattern declared in one source reaches
        a lab declared only in another, so the union of every source's names
        is the set they are matched against here. Resolving per source (as
        each backend must, seeing only its own declarations) would leave a
        host out of exactly the cross-source labs this class exists to
        compose.

        Best-effort by contract: this feeds shell completion, which must never
        crash or warn into the user's TAB. Each source's two calls are
        contained apart: a call that fails is skipped with a debug log and
        recorded as a drop (``otto.labs.drops.record_drop``; a refusal
        recorded its own), and never costs another source its names or its
        hosts, unlike :meth:`load_lab` and :meth:`lab_status_report`, which
        stay loud. A cancelled operation's error
        (``otto.labs.drops.is_timeout``) is not a source's failure: it
        propagates, so the operation stops here.
        """
        names = self.declared_names_in_view(view=view)
        by_id: dict[str, HostSummary] = {}
        for source in self.sources:
            for s in _source_summaries(source, view=view):
                by_id[s.id] = _merged_summary(s, by_id.get(s.id), names)
        return HostListing(names=names, summaries=sorted(by_id.values(), key=lambda s: s.id))

    def declared_names_in_view(self, *, view: "InventoryView") -> list[str]:
        """Every lab some source declares now, through *view*, sorted: the names alone.

        The one names read: each source's status read
        (:func:`~otto.labs.lab_statuses`, a template's render included)
        through the view of its own project, and nothing else, so no host
        is summarized and no legacy backend's lab is loaded.
        :meth:`host_listing_in_view` matches its summaries against these, and
        ``--lab`` completion offers them. Best-effort as it is: a source whose
        status call fails declares none (``_skip``), and a cancelled
        operation's error propagates.
        """
        declared: set[str] = set()
        for source in self.sources:
            declared.update(_declared_names(source, view=view))
        return sorted(declared)

    def declared_links_in_view(self, *, view: "InventoryView") -> list[SourceLinks]:
        """Every source's declared links, unresolved, through *view*, in source order.

        A source with the :class:`~otto.labs.SupportsDeclaredLinks` capability
        is asked through the view of its own project (:func:`project_view`),
        as :meth:`host_listing_in_view` asks it for its labs; any other source
        lists ``None``, its links being in no form the composite can read.
        Each row carries its source's lab scope. Best-effort, as
        :meth:`host_listing_in_view` is, and each source is contained apart:
        a failed listing is skipped (``_skip``) and lists ``[]``.
        """
        return [_source_links(source, view=view) for source in self.sources]


def _trace_position() -> "int | None":
    """Return where a live recording's entries stand (``otto lab explain``), else ``None``.

    Asks :func:`~otto.host.inventory_ref.recording_fields` first, so a load
    that records nothing never imports :mod:`otto.host.provenance`.
    """
    from ..host.inventory_ref import recording_fields  # lazy: keep the composite import-light

    if not recording_fields():
        return None
    from ..host import provenance

    return provenance.trace_position()


def _source_scope(label: str) -> "AbstractContextManager[None]":
    """Return the recorded scope that names source *label* (recorded loads only)."""
    from ..host import provenance

    return provenance.source_scope(label)


def _source_ref(source: LabSource) -> SourceRef:
    """Return *source* as a report names it."""
    return SourceRef(label=source.label, project_root=source.project_root)


def project_view(*, view: "InventoryView", project_root: "Path | None") -> "InventoryView":
    """Return *view* as the project at *project_root* sees it: THE view a source is called through.

    *view* itself for a source with no project (*project_root* ``None``), and
    for a :attr:`~otto.inventory.InventoryView.default_only` view: a legacy
    ``inventory=`` argument means one thing for every source, and re-pointing
    the view a :class:`~otto.inventory.RefusedDefault` stands for at another
    root would lose its refusal (lab templating D1). The composite calls each
    of its sources through it, with the source's
    :attr:`LabSource.project_root`, and ``otto lab render`` renders an entry
    through it, so a render and a load of one entry share one evaluation.
    """
    if project_root is None or view.default_only:
        return view
    return view.for_project(root=project_root)


@final
@dataclass(frozen=True, kw_only=True)
class _Loaded:
    """One source's answer for a lab: its hosts, if any, and its part in the lab's links."""

    lab: "Lab | None"
    """The source's hosts for the lab, or ``None`` when it holds none (a link-only source)."""

    not_found: str | None
    """Why the source holds no hosts for the lab, when :attr:`lab` is ``None``."""

    part: SourceContribution
    """The source's declared links, unresolved, or its own resolved ones."""

    guard: "_SourceGuard"
    """The source's error rules, which its links resolve under as its load did."""


def _load_from(
    *,
    source: LabSource,
    name: str,
    preferences: dict[str, dict[str, Any]] | None,
    view: "InventoryView",
) -> "_Loaded | None":
    """Load *source*'s contribution to lab *name*, or ``None`` when its scope excludes *name*.

    A source with the contribution capability
    (:func:`~otto.labs.load_contribution_through`) hands back its links
    unresolved; any other is loaded through :func:`~otto.labs.load_lab_through`
    and keeps the links it resolved itself. The load runs under the source's
    :class:`_SourceGuard`, which the answer keeps for its links.

    Raises
    ------
    LabNotFoundError
        Propagated: the source does not know *name*.
    LabUnavailableError
        A scoped source failed while loading (``declared=False``).
    LabRepositoryError
        Propagated from an unscoped source, as today.
    """
    scope = lab_scope_of(repository=source.repository)
    if scope is not None and not scope.contains(name=name):
        return None
    source_view = project_view(view=view, project_root=source.project_root)
    guard = _SourceGuard(source=source, scope=scope, name=name)
    return guard.run(
        lambda: _answer(
            source=source, name=name, preferences=preferences, view=source_view, guard=guard
        )
    )


def _answer(
    *,
    source: LabSource,
    name: str,
    preferences: dict[str, dict[str, Any]] | None,
    view: "InventoryView",
    guard: "_SourceGuard",
) -> _Loaded:
    """Return *source*'s answer for lab *name* through *view*, as its capabilities route it."""
    contribution = load_contribution_through(
        repository=source.repository, name=name, preferences=preferences, view=view
    )
    if contribution is not None:
        return _Loaded(
            lab=contribution.lab,
            not_found=contribution.not_found,
            part=contribution_part(label=source.label, contribution=contribution),
            guard=guard,
        )
    lab = load_lab_through(
        repository=source.repository, name=name, preferences=preferences, view=view
    )
    return _Loaded(
        lab=lab,
        not_found=None,
        part=SourceContribution(
            label=source.label,
            inputs=None,
            resolved_links=list(lab.links),
            host_ids=frozenset(lab.hosts),
        ),
        guard=guard,
    )


_T = TypeVar("_T")


@final
@dataclass(frozen=True, kw_only=True)
class _SourceFailure:
    """A scoped source's failure, as ``_scoped_call`` classifies it."""

    scope: LabScope
    """The failed source's scope."""
    diagnostic: Diagnostic
    """The source's label and the error text."""
    error: Exception
    """The error itself, chained as the ``__cause__`` of what the caller raises."""


def _scoped_call(
    *, source: LabSource, scope: LabScope | None, call: "Callable[[], _T]"
) -> "_T | _SourceFailure":
    """Return *call*'s result, or the failure of a source that declares a scope.

    The one decision of what a source failure is, for the status call, the
    load and the load's link resolution alike: a :class:`LabRepositoryError`
    or an ``InventoryError`` raised by a source that declares a *scope*. A
    :class:`LabNotFoundError` (a per-source protocol signal), a
    :class:`LabUnavailableError` (a failure already classified, as another
    source's whose addressing this call read) and any failure of an unscoped
    source propagate. The failure's diagnostic names the source by its label
    and states the error text.
    """
    from ..inventory.errors import InventoryError  # lazy: keep the composite import-light

    try:
        return call()
    except (LabNotFoundError, LabUnavailableError):
        raise
    except (LabRepositoryError, InventoryError) as e:
        if scope is None:
            raise
        return _SourceFailure(
            scope=scope, diagnostic=Diagnostic(where=source.label, reason=str(e)), error=e
        )


@final
@dataclass(frozen=True, kw_only=True)
class _SourceGuard:
    """One source's error rules while it contributes to lab :attr:`name`."""

    source: LabSource
    """The source."""

    scope: LabScope | None
    """Its declared scope, or ``None`` for an unscoped source."""

    name: str
    """The lab being loaded, which a failure makes unavailable."""

    def run(self, call: "Callable[[], _T]") -> _T:
        """Return *call*'s result; a scoped source's failure is the lab's unavailability.

        Raises
        ------
        LabUnavailableError
            :func:`_scoped_call` classified the failure as the scoped
            source's (``declared=False``), raised from the error itself.
        """
        got = _scoped_call(source=self.source, scope=self.scope, call=call)
        if isinstance(got, _SourceFailure):
            raise LabUnavailableError(
                name=self.name, diagnostic=got.diagnostic, declared=False
            ) from got.error
        return got


@final
class _GuardOfPart:
    """The :class:`~otto.labs.composition.ContributionGuard` of a load: each part's source's."""

    def __init__(self, answers: "list[_Loaded]") -> None:
        self._guards = {id(answer.part): answer.guard for answer in answers}

    def __call__(self, contribution: SourceContribution, call: "Callable[[], _T]", /) -> _T:
        """Return *call*'s result under the guard of the source *contribution* came from."""
        return self._guards[id(contribution)].run(call)


def _merge_elements(
    *,
    name: str,
    merged: "Lab",
    lab: "Lab",
    label: str,
    element_of: "dict[str, _ElementKey]",
    won_by: "dict[_ElementKey, str]",
    policy: "frozenset[str] | None",
) -> None:
    """Merge source *label*'s *lab* into *merged*, lab *name*, element by element, *label* winning.

    *element_of* (which element each surviving host id came from) and
    *won_by* (which source owns each element) are the merge state, updated
    here. *policy* is the source's declared overrides when it has an override
    policy (:class:`~otto.labs.SupportsOverridePolicy`), else ``None``.

    Raises
    ------
    LabRepositoryError
        Two surviving elements compose one host id, or an element of a source
        with a policy would replace different machines the policy does not
        declare.
    """
    from ..host.remote_host import RemoteHost  # lazy: mirror Lab.add_host

    for host in lab.hosts.values():
        if isinstance(host, RemoteHost):
            host._lab = merged  # noqa: SLF001 — same backlink repair Lab.__add__ performs
    incoming: "dict[_ElementKey, dict[str, Any]]" = {}
    for host_id, host in lab.hosts.items():
        incoming.setdefault(_element_key(host), {})[host_id] = host
    for key, hosts in incoming.items():
        if key in won_by:
            replaced = [h for h, k in element_of.items() if k == key]
            if policy is not None and key not in policy:
                _refuse_different_machines(
                    key=key,
                    name=name,
                    label=label,
                    owner=won_by[key],
                    replaced=_identities(merged, host_ids=replaced),
                    incoming=_identities(lab, host_ids=list(hosts)),
                )
            logger.warning("element %r in lab %r: %s overrides %s", key, name, label, won_by[key])
            # The whole element goes, not just the ids being restated:
            # a four-board chassis overridden by a one-board entry is
            # one board, not three stale ones plus the new one.
            for hid in replaced:
                merged._replace_host(host_id=hid, other=None)  # noqa: SLF001 — the merge owns this lab
                del element_of[hid]
        for host_id in hosts:
            if host_id in merged.hosts:
                raise LabRepositoryError(
                    f"host id {host_id!r} in lab {name!r}: element "
                    f"{element_of[host_id]!r} from {won_by[element_of[host_id]]} "
                    f"collides with element {key!r} from {label}"
                )
            merged._replace_host(host_id=host_id, other=lab)  # noqa: SLF001 — host and history together
            element_of[host_id] = key
        won_by[key] = label


def _identities(lab: "Lab", *, host_ids: "list[str]") -> "list[MachineIdentity]":
    """Return every machine identity *lab* holds under *host_ids*, in their order."""
    return [
        identity
        for host_id in host_ids
        for identity in lab._identity_history(host_id=host_id)  # noqa: SLF001 — the merge reads its labs
    ]


def _refuse_different_machines(
    *,
    key: str,
    name: str,
    label: str,
    owner: str,
    replaced: "list[MachineIdentity]",
    incoming: "list[MachineIdentity]",
) -> None:
    """Raise unless *incoming* holds the machines *replaced* did.

    *label*'s element *key* replaces *owner*'s in lab *name*, and *label*'s
    override policy does not declare *key*.

    Raises
    ------
    LabRepositoryError
        The two elements hold different machines
        (:func:`~otto.host.inventory_ref.same_machines`), naming both sources
        and both sets of machines, and how to declare the override.
    """
    from ..host.inventory_ref import same_machines, spell_machine

    if same_machines(replaced=replaced, incoming=incoming):
        return
    old = ", ".join(spell_machine(identity=i) for i in replaced)
    new = ", ".join(spell_machine(identity=i) for i in incoming)
    raise LabRepositoryError(
        f"element {key!r} in lab {name!r}: {label} would replace the machines of {owner} "
        f"({old}) with ({new}); declare overrides = [{key!r}] on {label} to mean it"
    )


def _names_of_down_source(*, source: LabSource, scope: LabScope) -> list[LabStatus]:
    """Return the labs a scoped source whose status call failed still lists, in its scope.

    Each is a plain :class:`LabStatus`; the report marks it unavailable with
    the outage's diagnostic. A listing that fails as the status did
    (``_scoped_call``) leaves the names unknown, so none is returned. A name
    outside *scope* is left out with a debug log: the source is down, and
    refusing it here would fail every lab.

    A source without the status capability
    (:class:`~otto.labs.SupportsLabStatus`, the test :func:`~otto.labs.lab_statuses`
    makes) is not asked: its failed status call already was its listing, and repeating it
    would only wait out the same failure again.
    """
    if not isinstance(source.repository, SupportsLabStatus):
        return []
    listed = _scoped_call(source=source, scope=scope, call=source.repository.list_labs)
    if isinstance(listed, _SourceFailure):
        return []
    outside = [n for n in listed if not scope.contains(name=n)]
    if outside:
        logger.debug(f"source {source.label} is down and lists labs outside its scope: {outside}")
    return [LabStatus(name=n) for n in listed if scope.contains(name=n)]


def _outage_for(*, name: str, outages: list[SourceUnavailable]) -> SourceUnavailable | None:
    """Return the first failed source whose scope contains lab *name*, or ``None``."""
    return next((o for o in outages if o.scope.contains(name=name)), None)


def _status_under(status: LabStatus, *, outages: list[SourceUnavailable]) -> LabStatus:
    """Return *status*, or the first outage's diagnostic when a failed scope contains the lab."""
    outage = _outage_for(name=status.name, outages=outages)
    if outage is None:
        return status
    return LabStatus(name=status.name, unavailable=outage.diagnostic)


def _reason_block(reasons: list[str]) -> str:
    """Render each source's not-found text as its own indented line, or ``""``.

    Appended to the composite's own verdict rather than replacing it: the first
    sentences are the ruling (existence lives here), these lines are the
    evidence each backend gathered — for the json one, the paths it searched.
    """
    if not reasons:
        return ""
    return "\n" + "\n".join(f"  {r}" for r in reasons)


def _declared_names(source: LabSource, *, view: "InventoryView") -> list[str]:
    """Return the labs *source* declares now through *view*, or ``[]`` if its status call fails.

    :func:`~otto.labs.lab_statuses` under the view of the source's project,
    best-effort as its caller (:meth:`CompositeLabRepository.host_listing_in_view`)
    is: a failure is skipped (``_skip``).
    """
    source_view = project_view(view=view, project_root=source.project_root)
    try:
        return [s.name for s in lab_statuses(repository=source.repository, view=source_view)]
    except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
        _skip(source, doing="listing labs", error=e)
        return []


def _source_summaries(source: LabSource, *, view: "InventoryView") -> list[HostSummary]:
    """Return *source*'s host summaries through *view*, or ``[]`` if they fail (``_skip``)."""
    from .summaries import host_summaries_in_view  # function-scope: only completion summarizes

    source_view = project_view(view=view, project_root=source.project_root)
    try:
        return host_summaries_in_view(repository=source.repository, view=source_view)
    except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
        _skip(source, doing="summarizing hosts", error=e)
        return []


def _source_links(source: LabSource, *, view: "InventoryView") -> SourceLinks:
    """Return *source*'s declared links through *view* (``None`` without the capability).

    A failed listing is skipped (``_skip``) and lists ``[]``.
    """
    entries: "list[DeclaredLinkEntry] | None" = None
    if isinstance(source.repository, SupportsDeclaredLinks):
        source_view = project_view(view=view, project_root=source.project_root)
        try:
            entries = source.repository.declared_link_entries(view=source_view)
        except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
            _skip(source, doing="listing links", error=e)
            entries = []
    return SourceLinks(
        source=_source_ref(source),
        entries=entries,
        scope=lab_scope_of(repository=source.repository),
    )


def _skip(source: LabSource, *, doing: str, error: Exception) -> None:
    """Skip *source*'s failed best-effort call, *doing*: a debug log and the skip rule.

    Free functions rather than loop bodies: the ``try``/``except`` belongs
    outside the loop (``PERF203``). ``otto.labs.drops.skip_failure``
    decides the rest: a cancelled operation's error is re-raised, a refusal
    recorded its own drop, anything else is one drop naming the source.
    """
    from .drops import skip_failure

    logger.debug(f"skipping source {source.label} while {doing}: {error}")
    skip_failure(where=f"source {source.label}", error=error)


def _element_key(host: Any) -> "_ElementKey":
    """Return the element slug *host* belongs to (spec §6).

    ``getattr`` rather than attribute access: only a
    :class:`~otto.host.remote_host.RemoteHost` carries an element at all
    (:class:`~otto.host.local_host.LocalHost` carries ``None``), and a
    backend is free to put one in a lab — an element-less host still has to
    key somewhere, and it keys under ``""``.
    """
    element = getattr(host, "element", None)
    return "" if element is None else element.slug


def _merged_summary(s: HostSummary, existing: HostSummary | None, names: list[str]) -> HostSummary:
    """Fold summary *s* onto *existing* (same id), re-resolving memberships.

    Later source wins the scalar fields (``inventory_ref`` among them: *s* is
    copied, not rebuilt); ``labs`` and ``lab_patterns`` union in first-seen
    order; then every declared name in *names* that a surviving pattern
    fullmatches joins ``labs``.
    """
    patterns = (
        list(s.lab_patterns)
        if existing is None
        else existing.lab_patterns + [p for p in s.lab_patterns if p not in existing.lab_patterns]
    )
    labs = (
        list(s.labs)
        if existing is None
        else existing.labs + [n for n in s.labs if n not in existing.labs]
    )
    try:
        labs += [n for n in names if n not in labs and any(re.fullmatch(p, n) for p in patterns)]
    except re.error as e:
        # A custom backend's pattern — the json one rejects an uncompilable
        # pattern at parse, but this field is open to any backend and this path
        # is completion, which must never raise into the user's TAB.
        logger.debug(f"skipping unusable lab patterns {patterns!r} for host {s.id!r}: {e}")
    return dataclasses.replace(s, labs=labs, lab_patterns=patterns, logins=list(s.logins))
