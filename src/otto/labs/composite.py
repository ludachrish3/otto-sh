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

from .drops import Diagnostic
from .errors import LabNotFoundError, LabRepositoryError, LabUnavailableError
from .protocol import HostSummary, LabScope, LabStatus, SourceRef, SupportsLabStatus
from .views import lab_scope_of, lab_statuses, load_lab_through

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..inventory.protocol import Inventory
    from ..inventory.view import InventoryView
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
        declaration cannot clear an outage), then the lab's own status.
        """
        outage = _outage_for(name=name, outages=self.unavailable_sources)
        if outage is not None:
            return LabUnavailableError(name=name, diagnostic=outage.diagnostic, declared=False)
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
        contribution is loaded through :func:`~otto.labs.load_lab_through`
        under the view of its own project and merged in order — elements
        replaced wholesale by slug, the ``labs`` entry replaced wholesale by
        the last declaring source.

        Raises
        ------
        LabUnavailableError
            If *report* refuses *name*, or a scoped source fails while
            loading it (``declared=False``; *report* is not rewritten).
        LabNotFoundError
            If no source declares *name* (message names every source), no
            source provides it, or no sources are configured at all. The first
            two carry each source's own not-found text, one indented line
            apiece — for the json backend, the search paths it looked in.
        LabRepositoryError
            If *name* is declared but no element anywhere matches it, if two
            surviving elements produce the same host id, or propagated from
            the first failing unscoped source — a broken backend is never
            silently dropped from the merge.
        """
        self._admit(name, report=report)
        declared_by = [ref.label for ref in report.declared_by.get(name, [])]
        loaded: "list[tuple[str, Lab]]" = []
        # Each source's own not-found text, kept for the failure messages below.
        # Since R15 every load goes through this class, so a reason discarded
        # here is a reason the user never sees — and for the json backend that
        # reason is the ONLY place the searched paths are named. A mistyped
        # `paths` entry would otherwise be answered with "add a 'labs' table",
        # pointing at a file otto never found.
        reasons: list[str] = []
        for source in self.sources:
            try:
                lab = _load_from(source=source, name=name, preferences=preferences, view=view)
            except LabNotFoundError as e:  # a per-source protocol signal, not a failure
                reasons.append(f"{source.label}: {e}")
                continue
            if lab is not None:
                loaded.append((source.label, lab))
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

        from ..host.remote_host import RemoteHost  # lazy: mirror Lab.add_host

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
            for host in lab.hosts.values():
                if isinstance(host, RemoteHost):
                    host._lab = merged  # noqa: SLF001 — same backlink repair Lab.__add__ performs
            incoming: "dict[_ElementKey, dict[str, Any]]" = {}
            for host_id, host in lab.hosts.items():
                incoming.setdefault(_element_key(host), {})[host_id] = host
            for key, hosts in incoming.items():
                if key in won_by:
                    logger.warning(
                        "element %r in lab %r: %s overrides %s", key, name, label, won_by[key]
                    )
                    # The whole element goes, not just the ids being restated:
                    # a four-board chassis overridden by a one-board entry is
                    # one board, not three stale ones plus the new one.
                    for hid in [h for h, k in element_of.items() if k == key]:
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
            if label in declared_by:
                if lab_entry_from is not None:
                    logger.warning("labs entry %r: %s overrides %s", name, label, lab_entry_from)
                # Wholesale: resources and metadata travel together, so an
                # override never leaves half of the previous declaration behind.
                merged.resources = set(lab.resources)
                merged.metadata[name] = dict(lab.metadata.get(name, {}))
                lab_entry_from = label
            by_id = {link.id: link for link in merged.links}
            by_id.update({link.id: link for link in lab.links})
            merged.links = list(by_id.values())
        if not merged.hosts:
            raise LabRepositoryError(
                f"Lab {name!r} is declared by {', '.join(declared_by)} but no element in any "
                f"source matches it — add a 'labs' pattern to an element or remove the declaration"
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
                    repository=source.repository, view=_source_view(view=view, source=source)
                ),
            )
            if isinstance(reported, _SourceFailure):
                outages.append(
                    SourceUnavailable(
                        source=_source_ref(source),
                        scope=reported.scope,
                        diagnostic=reported.diagnostic,
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
        """Sorted union across sources; a backend error propagates (spec §6.2).

        Never asks a source for a status: the ``--lab`` completer calls this
        outside the host deadline.
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

        Memberships are also RE-RESOLVED: a summary carries the membership
        patterns of its element, and a pattern declared in one source reaches a
        lab declared only in another, so the union of every source's
        ``list_labs()`` is the set they are matched against here. Resolving
        per source (as each backend must, seeing only its own declarations)
        would leave a host out of exactly the cross-source labs this class
        exists to compose.

        Each source is summarized through :func:`~otto.labs.host_summaries_in_view`
        under the view of its own project.

        Best-effort by contract: this feeds shell completion, which must never
        crash or warn into the user's TAB — a failing source is skipped with a
        debug log, unlike ``load_lab``/``list_labs`` which stay loud.
        """
        from .summaries import host_summaries_in_view  # function-scope: only completion summarizes

        declared: set[str] = set()
        for source in self.sources:
            declared.update(_declared_names(source))
        names = sorted(declared)
        by_id: dict[str, HostSummary] = {}
        for source in self.sources:
            try:
                summaries = host_summaries_in_view(
                    repository=source.repository, view=_source_view(view=view, source=source)
                )
            except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
                from .drops import is_refusal, record_drop

                logger.debug(f"skipping source {source.label} while summarizing hosts: {e}")
                if not is_refusal(e):  # a refusal recorded its own drop
                    record_drop(f"source {source.label}", str(e))
                continue
            for s in summaries:
                by_id[s.id] = _merged_summary(s, by_id.get(s.id), names)
        return sorted(by_id.values(), key=lambda s: s.id)


def _source_ref(source: LabSource) -> SourceRef:
    """Return *source* as a report names it."""
    return SourceRef(label=source.label, project_root=source.project_root)


def _source_view(*, view: "InventoryView", source: LabSource) -> "InventoryView":
    """Return *view* as *source*'s project sees it.

    *view* itself for a source with no project, and for a
    :attr:`~otto.inventory.InventoryView.default_only` view: a legacy
    ``inventory=`` argument means one thing for every source, and re-pointing
    the view a :class:`~otto.inventory.RefusedDefault` stands for at another
    root would lose its refusal (lab templating D1).
    """
    if source.project_root is None or view.default_only:
        return view
    return view.for_project(root=source.project_root)


def _load_from(
    *,
    source: LabSource,
    name: str,
    preferences: dict[str, dict[str, Any]] | None,
    view: "InventoryView",
) -> "Lab | None":
    """Load *source*'s contribution to lab *name*, or ``None`` when its scope excludes *name*.

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
    lab = _scoped_call(
        source=source,
        scope=scope,
        call=lambda: load_lab_through(
            repository=source.repository,
            name=name,
            preferences=preferences,
            view=_source_view(view=view, source=source),
        ),
    )
    if isinstance(lab, _SourceFailure):
        raise LabUnavailableError(
            name=name, diagnostic=lab.diagnostic, declared=False
        ) from lab.error
    return lab


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

    The one decision of what a source failure is, for the status call and the
    load alike: a :class:`LabRepositoryError` or an ``InventoryError`` raised
    by a source that declares a *scope*. A :class:`LabNotFoundError` (a
    per-source protocol signal) and any failure of an unscoped source
    propagate, as today. The failure's diagnostic names the source by its
    label and states the error text.
    """
    from ..inventory.errors import InventoryError  # lazy: keep the composite import-light

    try:
        return call()
    except LabNotFoundError:
        raise
    except (LabRepositoryError, InventoryError) as e:
        if scope is None:
            raise
        return _SourceFailure(
            scope=scope, diagnostic=Diagnostic(where=source.label, reason=str(e)), error=e
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


def _declared_names(source: LabSource) -> list[str]:
    """Return the labs *source* declares, or ``[]`` if it fails.

    A free function rather than the loop body it is called from: the
    ``try``/``except`` belongs outside the loop (``PERF203``). Best-effort like
    its caller — this feeds completion, never ``load_lab``, which stays loud.
    """
    try:
        return list(source.repository.list_labs())
    except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
        logger.debug(f"skipping source {source.label} while listing labs: {e}")
        return []


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
