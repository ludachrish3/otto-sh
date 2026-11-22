"""Built-in ``"json"`` ``LabRepository`` backend: loads labs from ``lab.json`` files."""

import abc
import functools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from typing_extensions import final, override

from ..host.factory import (
    HostIdentity,
    create_host_from_dict,
    host_identity,
)
from ..host.inventory_ref import NOT_RECORDING, recording_fields
from ..host.os_profile import ProfileContext
from ..host.remote_host import describe_identity
from ..models.base import OttoModel, compact_validation_error
from ..models.lab import ElementSpec, LabEntrySpec
from ..utils import anchor_path, refuse_empty_entry
from .drops import Diagnostic
from .errors import (
    LabNotFoundError,
    LabRepositoryError,
)
from .protocol import (
    DeclaredLinkInputs,
    HostSummary,
    LabContribution,
    LabStatus,
    logins_of_host_data,
)
from .sources import JSON_FILE_RULE, LabSourceFacts

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from ..host.element import Element
    from ..host.inventory_ref import MachineIdentity
    from ..inventory.protocol import Inventory
    from ..inventory.resolve import ResolvedEntry
    from ..inventory.view import InventoryView
    from ..link.derive import DeclaredLinkEntry, HostAddressing
    from ..registry import Configured

    # Annotation only: load_lab() below imports Lab at call time, so a
    # backend loads the lab model only when it builds a lab.
    from .documents import SourceRead
    from .loader import Lab
    from .sources import LabSourceEnv

logger = logging.getLogger(__name__)

LAB_FILENAME = JSON_FILE_RULE.directory_file
"""The file a json directory entry contributes (:data:`~otto.labs.sources.JSON_FILE_RULE`)."""

# Known top-level sections of a lab.json object (v2): ``labs`` is the table of
# declared labs keyed by name, ``elements`` the array of element entries (each
# grouping host entries), ``links`` the array of declared data-plane routes.
_LAB_SECTIONS = frozenset({"labs", "elements", "links"})

_MIGRATION_HINT = (
    "top-level 'hosts' has moved: hosts are grouped under 'elements' (each with "
    "'name', optional 'id', 'labs' membership patterns, optional 'metadata', and "
    "'hosts'), and per-host 'labs' moved to the element (a host's 'resources' may "
    "stay on the host — a slot — or move to the element or the 'labs' table). "
    "See https://otto-sh.readthedocs.io/en/stable/configuration/lab/migrating.html#migrating-from-the-hosts-array"
)

# WHY `from ..inventory import ...` is function-local at every use site below:
# every command that loads a lab reaches this module, and a
# module-scope import would put otto.inventory's nine modules on each of them
# for the overwhelmingly common process that never resolves a single
# reference. The edge is real and
# declared in tach.toml; only the timing is deferred, and the cost per call is
# a sys.modules lookup beside a pydantic validation.


def parse_lab_sections(data: object, source: str) -> dict[str, Any]:
    """Validate a parsed ``lab.json`` object's section shape; return its sections.

    The single source of truth for the ``lab.json`` object contract, run by the
    one lab-document reader (:func:`otto.labs.documents.read_lab_document`)
    that the runtime loader, the ``otto init`` doctor and every other lab-file
    reader go through, so none of them can drift from what otto actually
    accepts (there is no second validator to drift). *data* is the
    already-parsed JSON value; *source* names its origin (a file path) for
    error messages.

    Returns ``{"labs": dict, "elements": list, "links": list}`` (absent
    sections empty). ``$schema`` and ``_``-prefixed keys are comment space.
    A top-level ``hosts`` key is the v1 shape and fails with the migration
    hint; any other unknown section fails naming it.

    Raises
    ------
    LabRepositoryError
        If *data* is not a JSON object, carries the v1 ``hosts`` section or an
        unknown one, or a section's value has the wrong JSON type.
    """
    if not isinstance(data, dict):
        raise LabRepositoryError(
            f"Lab file '{source}' must contain a JSON object with "
            f"'labs'/'elements'/'links' sections, got {type(data).__name__}"
        )
    # `$schema` is the standard editor-wiring key (VS Code / jsonls) — treated
    # as comment space alongside `_`-prefixed keys.
    # str(k) so a non-string key (possible: *data* is typed ``object``, and the
    # doctor hands us any parsed value) still sorts into the error message
    # instead of raising TypeError out of ``sorted`` below.
    keys = {
        str(k) for k in data if not (isinstance(k, str) and (k.startswith("_") or k == "$schema"))
    }
    if "hosts" in keys:
        raise LabRepositoryError(f"Lab file '{source}': {_MIGRATION_HINT}")
    unknown = keys - _LAB_SECTIONS
    if unknown:
        raise LabRepositoryError(
            f"Lab file '{source}' has unknown section(s) {sorted(unknown)}; "
            f"known sections: {sorted(_LAB_SECTIONS)}"
        )
    labs = data.get("labs", {})
    if not isinstance(labs, dict):
        raise LabRepositoryError(
            f"Lab file '{source}': section 'labs' must be a JSON object keyed by lab name, "
            f"got {type(labs).__name__}"
        )
    out: dict[str, Any] = {"labs": labs}
    for section in ("elements", "links"):
        value = data.get(section, [])
        if not isinstance(value, list):
            raise LabRepositoryError(
                f"Lab file '{source}': section '{section}' must be a JSON array, "
                f"got {type(value).__name__}"
            )
        out[section] = value
    return out


def parse_lab_entries(raw: dict[str, Any], source: str) -> dict[str, LabEntrySpec]:
    """Validate the ``labs`` table; keys are lab names, values ``LabEntrySpec``."""
    out: dict[str, LabEntrySpec] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise LabRepositoryError(f"Lab file '{source}': 'labs' key {name!r} is not a lab name")
        if name.startswith("_"):
            continue  # comment key inside the table
        try:
            out[name] = LabEntrySpec.model_validate(value)
        except ValidationError as e:
            raise LabRepositoryError(f"Lab file '{source}': labs[{name!r}] {e}") from e
    return out


def parse_elements(raw: list[Any], source: str) -> list[ElementSpec]:
    """Validate every ``elements`` entry; two entries whose names slug alike are an error."""
    seen_at: dict[str, int] = {}
    seen_name: dict[str, str] = {}
    out: list[ElementSpec] = []
    for idx, entry in enumerate(raw):
        spec = _parse_element(entry, idx, source)
        if spec.key in seen_at:
            first, first_name = seen_at[spec.key], seen_name[spec.key]
            if first_name == spec.name:
                detail = f"duplicate element {spec.name!r} at elements[{first}] and elements[{idx}]"
            else:
                detail = (
                    f"elements[{first}] {first_name!r} and elements[{idx}] {spec.name!r} "
                    f"both slug to {spec.key!r}"
                )
            raise LabRepositoryError(f"Lab file '{source}': {detail} — one element, one entry")
        seen_at[spec.key] = idx
        seen_name[spec.key] = spec.name
        out.append(spec)
    return out


def _parse_element(entry: object, idx: int, source: str) -> ElementSpec:
    """Validate one ``elements`` entry, naming *source* and its index on failure.

    A free function rather than the loop body it is called from: the
    ``try``/``except`` belongs outside the loop (``PERF203``).
    """
    if not isinstance(entry, dict):
        raise LabRepositoryError(
            f"Lab file '{source}': elements[{idx}] must be a JSON object, "
            f"got {type(entry).__name__}"
        )
    try:
        return ElementSpec.model_validate(entry)
    except ValidationError as e:
        raise LabRepositoryError(f"Lab file '{source}': elements[{idx}] {e}") from e


@dataclass(frozen=True, slots=True)
class SeenElement:
    """Where a source first carried an element slug, and how that file spelled it.

    The path alone is not enough. Identity is the slug, so ``ALT1`` in one file
    and ``Alt1`` in another are one element — and an error quoting only the
    second spelling sends the author hunting a string the first file never
    contains. Public because it is half of :func:`check_in_source_duplicates`'s
    signature, which the ``otto init`` doctor also calls.
    """

    path: Path
    """The lab file that first carried the element."""

    name: str
    """The element's name exactly as that file spelled it."""


@dataclass(frozen=True)
class LabDocument:
    """One parsed lab file of a source: its labs table, elements and raw links."""

    path: Path
    """The file these sections came from — what an error message names."""

    entries: dict[str, LabEntrySpec]
    """The file's ``labs`` table, keyed by lab name."""

    elements: list[ElementSpec]
    """The file's ``elements``, in declaration order."""

    links: list[Any]
    """The file's raw ``links`` entries, resolved later against loaded ids."""

    provenance: "Mapping[str, Any] | None" = None
    """Where a rendered document's elements came from (its ``_otto.provenance`` table).

    ``None`` for a hand-written lab file. Read from
    :attr:`~otto.labs.documents.LabDocumentSource.provenance`.
    """


class _DocumentLabRepository(abc.ABC):
    """Serve labs from one source's lab documents: the core every file-backed source shares.

    A subclass supplies the documents through its one seam,
    :meth:`_read_source`; everything a source answers (a lab's load, its
    host summaries, the declared labs and their status) is computed here from
    them, so two file-backed sources given the same documents answer alike.

    Each document is a v2 lab object with three optional sections — ``labs``
    (the table of declared labs, keyed by name, each with ``resources`` and
    ``metadata``), ``elements`` (entries carrying identity, ``labs``
    membership patterns, ``metadata`` and their host entries) and ``links``.
    A top-level ``hosts`` key is the v1 shape and fails with a migration
    message; top-level ``_``-prefixed keys are comment space and unknown
    sections fail loud.

    The documents of ONE source compose by union (spec §2.4): an element in
    one document may join a lab declared in another, and a duplicate within
    the source — the same element slug, or the same lab declared twice — is a
    typo, not an override, and fails naming both files.

    :meth:`load_contribution` returns THIS source's contribution to a lab:
    the members whose element patterns :func:`re.fullmatch` the requested
    name, plus the lab's ``resources``/``metadata`` if this source declares
    it, and the source's declared links unresolved. The composite
    (:class:`otto.labs.composite.CompositeLabRepository`) merges contributions
    across sources and then resolves every source's links once;
    :meth:`load_lab` resolves this source's links against this source alone.
    """

    def __init__(
        self,
        search_paths: list[Path] | None = None,
        *,
        profiles: ProfileContext | None = None,
    ) -> None:
        """Configure the paths this repository draws lab data from.

        *search_paths* are the source's ``paths`` entries, as the subclass
        reads them; a lab that is not found names them.

        *profiles* are the project data profiles the hosts' ``os_type`` resolves
        through (the source's :attr:`~otto.labs.sources.LabSourceEnv.profiles`);
        omitted, none.
        """
        self.search_paths: list[Path] = list(search_paths or [])
        self.profiles: ProfileContext = profiles if profiles is not None else ProfileContext.empty()

    def load_lab(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> "Lab":
        """Load this source's contribution to the lab called *name*.

        Members are the hosts of every element whose ``labs`` patterns
        fullmatch *name*; ``resources`` and ``metadata[name]`` are filled only
        when this source's ``labs`` table declares *name*.

        *inventory* is the process inventory referenced entries resolve
        against (spec 2026-08-28 host-inventory §6): :meth:`load_lab_in_view`
        over the view it stands for
        (:meth:`~otto.inventory.InventoryView.of_default`). ``None`` means a
        referenced entry is an error naming the file, the element and the
        index, and so does a qualified reference to any inventory but
        ``default``.

        Raises
        ------
        LabNotFoundError
            If no lab file exists in any search path, or this source neither
            declares *name* nor holds an element matching it.
        LabRepositoryError
            If a lab.json is malformed, a host's data is invalid, or an entry
            references an inventory key that cannot be resolved.
        """
        from ..inventory.view import InventoryView  # lazy: see the note above

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
        """Load this source's contribution to *name*, each reference resolved through *view*.

        The :class:`~otto.labs.protocol.SupportsInventoryView` capability, and
        what :meth:`load_lab` runs: :meth:`load_contribution`, with this
        source's own links resolved against this source alone
        (:func:`~otto.labs.composition.compose_links`). A link naming a host
        only another source holds is therefore an unknown host here; through
        the composite it resolves. *preferences* and the errors are
        :meth:`load_lab`'s.
        """
        from .composition import compose_links, contribution_part

        contribution = self.load_contribution(name, preferences=preferences, view=view)
        lab = contribution.lab
        if lab is None:
            raise LabNotFoundError(str(contribution.not_found))
        lab.links = compose_links(
            lab=lab,
            contributions=[contribution_part(label=type(self).__name__, contribution=contribution)],
        )
        return lab

    def load_contribution(
        self,
        name: str,
        *,
        preferences: dict[str, dict[str, Any]] | None = None,
        view: "InventoryView",
    ) -> LabContribution:
        """Return this source's contribution to *name*: its hosts, and its links unresolved.

        The :class:`~otto.labs.protocol.SupportsLabContribution` capability.
        The join happens HERE, once per entry, before the factory ever sees
        the dict (:func:`~otto.inventory.resolve_view_entry`): a bare
        reference resolves through ``default`` and a qualified one through
        the inventory it names. The link inputs come from the same read as
        the hosts and span the whole source (:func:`link_inputs_of`).

        A source that neither declares *name* nor holds an element matching
        it contributes no lab, with the reason, and still its links.

        Raises
        ------
        LabNotFoundError
            If no lab file exists in any search path.
        LabRepositoryError
            If a lab file is malformed, a host's data is invalid, or an entry
            references an inventory key that cannot be resolved.
        """
        from . import documents
        from .loader import Lab

        try:
            read = self._read_source(best_effort=False, view=view)
        except FileNotFoundError as e:
            raise LabNotFoundError(str(e)) from None
        docs = documents.accepted_documents(read, best_effort=False)
        links = link_inputs_of(read, view=view, profiles=self.profiles)

        declared: LabEntrySpec | None = None
        members: list[tuple[ElementSpec, LabDocument]] = []  # (element, its document)
        for doc in docs:
            if name in doc.entries:
                # Unique per source: the read refuses a repeat.
                declared = doc.entries[name]
            members.extend((el, doc) for el in doc.elements if el.matches(name))

        if declared is None and not members:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            return LabContribution(
                lab=None,
                not_found=(
                    f"Lab '{name}' is neither declared in a 'labs' table nor matched by any "
                    f"element's 'labs' patterns in:\n  {searched}"
                ),
                links=links,
            )

        lab = Lab(name=name)
        if declared is not None:
            # The lab's own resources are DECLARED here, never read back off
            # its hosts (one of three levels since spec 2026-08-28
            # three-level-reservations).
            lab.resources = set(declared.resources)
            lab.metadata[name] = dict(declared.metadata)

        for element, doc in members:
            runtime = element.to_element()
            for idx, host_data in enumerate(element.hosts):
                _add_host(lab, host_data, runtime, idx, doc, preferences, view, self.profiles)
        logger.debug(f"Loaded lab '{name}' with {len(lab.hosts)} hosts")
        return LabContribution(lab=lab, not_found=None, links=links)

    def declared_link_entries(self, *, view: "InventoryView") -> "list[DeclaredLinkEntry]":
        """Return every ``links`` entry of the source, for all of its labs, building no host.

        The :class:`~otto.labs.protocol.SupportsDeclaredLinks` capability:
        :func:`declared_link_entries` over a best-effort read, so a malformed
        file is left out, recorded as a drop as every best-effort read's
        problems are (:func:`~otto.labs.documents.accepted_documents`), and a
        source without lab files has none.
        """
        from .documents import accepted_documents

        try:
            read = self._read_source(best_effort=True, view=view)
        except FileNotFoundError:
            return []
        accepted_documents(read, best_effort=True)  # records the read's problems; never raises
        return declared_link_entries(read)

    def list_host_summaries(self, *, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Every host across the configured lab files, without building hosts.

        The :class:`~otto.labs.protocol.SupportsHostSummaries` fast path:
        :meth:`list_host_summaries_in_view` over the view *inventory* stands
        for (:meth:`~otto.inventory.InventoryView.of_default`). Without an
        inventory a referenced entry is skipped, like any other unresolvable
        record.
        """
        from ..inventory.view import InventoryView  # lazy: see the note above

        return self.list_host_summaries_in_view(view=InventoryView.of_default(inventory=inventory))

    def list_host_summaries_in_view(self, *, view: "InventoryView") -> list[HostSummary]:
        """Every host across the configured lab files, each reference resolved through *view*.

        The :class:`~otto.labs.protocol.SupportsHostSummariesInView` fast path.
        Each id comes from :func:`~otto.host.factory.host_identity`, which
        applies the same profile merge and validation
        :func:`~otto.host.factory.create_host_from_dict` applies, so the id is
        the one the constructed host has. Deriving ids by
        formatting the raw JSON instead would silently diverge (a profile that
        defaults ``board``/``slot`` supplies id parts the JSON never mentions).

        ``labs`` is each element's patterns resolved against the labs THIS
        source declares; ``lab_patterns`` carries the patterns themselves, so
        the composite can re-resolve them against every source's declarations.
        A lab's ``unavailable`` reason filters nothing here.

        A referenced entry is identified through its record
        (:func:`~otto.inventory.resolve_view_entry`), so a summary may carry an
        inventory-supplied ``ip``, and its ``inventory_ref`` names the
        inventory and key. An entry the view cannot resolve is skipped, like
        any other unresolvable record.

        Best-effort, like :meth:`list_labs`: a malformed file or host entry
        is skipped rather than raised — these feed completion, which must
        never crash the shell. Each skip is one drop
        (``otto.labs.drops.record_drop``); a bare reference the view refuses
        records its own, so the skip adds none.

        Two records deriving one id resolve by machine, the rule ``Lab.__add__``
        applies when labs merge (:func:`~otto.host.inventory_ref.first_conflict`
        against every identity already folded into that id). Either way the
        later record's labs and lab patterns fold into the first summary, whose
        other fields (``ip`` among them) stay the first record's, because
        ``load_lab`` of each lab loads its own machine. A different machine —
        two addresses, or two inventories (or two qualified keys of one)
        reusing one address — is also recorded as a drop naming both
        declarations, each time such a record is read.

        One shape folds here yet is refused by :meth:`load_lab`: two records
        that compose one id at the SAME ip and share a LAB name (element
        ``a-b`` and element ``a`` + board ``b``). Completion may therefore offer
        an id the lab refuses to load; the refusal, which names both
        declarations, is the honest answer at load.
        """
        from ..host.inventory_ref import MachineIdentity  # lazy: see the note above
        from .drops import record_drop

        by_id: dict[str, HostSummary] = {}
        # id -> every machine identity folded into it, each with its declaration
        folded: dict[str, list[_Declared]] = {}

        try:
            docs = self._load_documents(best_effort=True, view=view)
        except FileNotFoundError:
            return []

        declared = sorted({n for doc in docs for n in doc.entries})
        # Membership once per ELEMENT: its entries arrive together, in order.
        matched: ElementSpec | None = None
        labs: list[str] = []
        for found in _identified_entries(docs, view=view, profiles=self.profiles):
            element, identity, resolved = found.element, found.identity, found.resolved
            if element is not matched:
                matched, labs = element, [n for n in declared if element.matches(n)]
            incoming = _Declared(
                identity=MachineIdentity(ip=identity.ip, ref=resolved.ref),
                described=describe_identity(
                    element.name, identity.board, identity.slot, identity.ip
                ),
            )
            first = by_id.get(identity.id)
            if first is not None:
                # Every portion of an id joins with ``-``, so two records can
                # compose one id (``a-b`` vs ``a`` + board ``b``, or one
                # element restated across labs). Either way the record's labs
                # fold into the kept summary, since ``load_lab`` of each lab
                # loads its own machine. A different machine is also said,
                # naming both; only the same machine joins the identities kept.
                clash = _first_clash(incoming=incoming, seen=folded[identity.id])
                if clash is None:
                    folded[identity.id].append(incoming)
                else:
                    record_drop(
                        found.where(),
                        _summary_collision(host_id=identity.id, kept=clash, lost=incoming),
                    )
                first.labs.extend(n for n in labs if n not in first.labs)
                first.lab_patterns.extend(p for p in element.labs if p not in first.lab_patterns)
                continue
            folded[identity.id] = [incoming]
            by_id[identity.id] = HostSummary(
                id=identity.id,
                labs=list(labs),
                lab_patterns=list(element.labs),
                ip=identity.ip,
                docker_capable=identity.docker_capable,
                docker_priority=identity.docker_priority,
                os_type=str(resolved.host_data.get("os_type", "unix")),
                logins=logins_of_host_data(resolved.host_data),
                inventory_ref=resolved.ref,
            )

        return sorted(by_id.values(), key=lambda s: s.id)

    def list_labs(self) -> list[str]:
        """List the lab names DECLARED across the configured paths.

        The declared set, not the elements' membership patterns: a pattern is
        a regex, not a name (one with a quantifier or a group names nothing),
        and an undeclared lab does not exist (spec §2.1). Returns an empty list
        when no lab file exists. A malformed lab.json is skipped rather than
        raised, so listing stays best-effort. These are the names of
        :meth:`list_lab_status`, read once by the same walk.
        """
        from ..inventory.view import InventoryView  # lazy: see the note above

        view = InventoryView.of_default(inventory=None)
        return [status.name for status in self._lab_statuses(view=view)]

    def list_lab_status(self, *, view: "InventoryView") -> list[LabStatus]:
        """Return every lab :meth:`list_labs` names, with whether it can be used now.

        The :class:`~otto.labs.protocol.SupportsLabStatus` capability. A lab
        whose ``labs`` entry sets ``unavailable`` carries that reason, where
        ``<file>: lab '<name>'``; every other lab is available. It reads the
        lab files exactly as :meth:`list_labs` does: a lab's availability is
        what its document says, and *view* only reaches the documents' read.
        """
        return self._lab_statuses(view=view)

    def _lab_statuses(self, *, view: "InventoryView") -> list[LabStatus]:
        """Return the status of every declared lab, sorted by name: the one read of both listings.

        A malformed or duplicating file is skipped (``_load_documents`` with
        ``best_effort``).
        """
        try:
            docs = self._load_documents(best_effort=True, view=view)
        except FileNotFoundError:
            return []
        return lab_statuses_of(docs)

    def _load_documents(self, *, best_effort: bool, view: "InventoryView") -> list[LabDocument]:
        """Return the source's lab documents, each with its path, labs table, elements and links.

        Within ONE source a duplicate is a typo, never an override, so the
        same element slug or the same lab declaration in two documents fails
        naming both. The first problem of the read (:meth:`_read_source`) is
        raised as a :class:`~otto.labs.errors.LabRepositoryError`;
        *best_effort* (completion paths) instead skips each malformed or
        duplicating file, recorded as a drop (:func:`otto.labs.drops.record_drop`).

        Raises
        ------
        FileNotFoundError
            :meth:`_read_source`'s signal that the source holds no lab file.
        """
        from .documents import accepted_documents

        return accepted_documents(
            self._read_source(best_effort=best_effort, view=view), best_effort=best_effort
        )

    @abc.abstractmethod
    def _read_source(self, *, best_effort: bool, view: "InventoryView") -> "SourceRead":
        """Read the source's lab files into documents and problems: the one call of ``read_source``.

        The one seam a file-backed source supplies; a subclass that does not
        is refused at construction. *best_effort* says whether the caller
        raises the read's first problem or skips it, for a source whose read
        can stop early; *view* is the inventory view the caller resolves
        references through, for a source whose documents depend on it.

        Raises
        ------
        FileNotFoundError
            Internal signal (translated to LabNotFoundError by ``load_lab`` and
            swallowed by the listings) when the source holds no lab file.
        """


@final
class _JsonFileLabRepository(_DocumentLabRepository):
    """Load labs from ``lab.json`` files across a fixed set of search paths.

    A search path takes one of three forms: a **directory**, which is searched
    for a ``lab.json``; a path ending in **``.json``**, which *is* the lab
    file; or a **glob** (an entry containing ``*``, ``?`` or ``[``), which
    expands to the sorted ``.json`` files it matches
    (:data:`~otto.labs.sources.JSON_FILE_RULE`). Any form that resolves to
    nothing is skipped silently, so a repository may draw on several sources
    and tolerate absent ones.

    The search paths are supplied once at construction — this is the built-in
    ``"json"`` backend, and its factory feeds it the anchored ``paths`` of the
    ``[[lab.sources]]`` entry that selected it. Everything else is
    :class:`_DocumentLabRepository`'s.
    """

    @override
    def _read_source(self, *, best_effort: bool, view: "InventoryView") -> "SourceRead":
        """Read every lab file of the source (:func:`otto.labs.documents.read_source`).

        Neither argument is read: a json lab file is its own document, and
        its read never stops early.
        """
        del best_effort, view
        from .documents import read_source

        return read_source(self._find_lab_files())

    def _find_lab_files(self) -> list[Path]:
        """Find all lab files across the configured search paths.

        Delegates the entry-to-files rule to
        :func:`~otto.labs.documents.expand_lab_files` under
        :data:`~otto.labs.sources.JSON_FILE_RULE` — the function and the rule
        ``LabSourceState.lab_files()`` reads for a json source, so the backend
        and the config side can never disagree about which files a source
        holds.

        Raises
        ------
        FileNotFoundError
            When no lab file is found (:meth:`_read_source`'s signal).
        """
        from .documents import expand_lab_files

        found = expand_lab_files(self.search_paths, rule=JSON_FILE_RULE)

        if not found:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise FileNotFoundError(
                f"No lab data found in any search path (directories are searched "
                f"for {LAB_FILENAME}; .json entries are read directly; globs are "
                f"expanded):\n  {searched}{_template_hint(self.search_paths)}"
            ) from None

        return found


def _template_hint(search_paths: "list[Path]") -> str:
    """Return a line naming each Jsonnet template a json source's paths reach, or ``""`` for none.

    Asked only once no lab file is found, so it costs a found lab nothing. The
    templates are the files those paths hold under the ``jsonnet`` source's
    rule (:func:`~otto.labs.documents.expand_lab_files` with
    :data:`~otto.labs.sources.JSONNET_FILE_RULE`: a directory's
    ``lab.jsonnet``, a ``.jsonnet`` file named directly), which a json
    source never reads. A reader who converted the lab file to Jsonnet is
    told the route that reads it, in both places a json source comes from:
    a project's ``[[lab.sources]]`` entry and ``load_lab(search_paths=)``.
    """
    from .documents import expand_lab_files
    from .sources import JSONNET_FILE_RULE

    return "".join(
        f"\n{template} is a Jsonnet template, which a json lab source never reads: in a "
        f'project, declare a backend = "jsonnet" [[lab.sources]] entry for it (a '
        f"directory entry there reads its {JSONNET_FILE_RULE.directory_file}); in "
        f"load_lab(search_paths=), name the {JSONNET_FILE_RULE.suffix} file"
        for template in expand_lab_files(search_paths, rule=JSONNET_FILE_RULE)
    )


def _anchored_entries(
    value: "tuple[Path, ...]", info: ValidationInfo, *, backend: str
) -> "tuple[Path, ...]":
    """Anchor each relative entry of a path-list option to the declaring project's root.

    The one anchoring of a lab source's paths, a source config model's
    ``mode="after"`` validator: the root is the
    :class:`~otto.labs.sources.LabSourceEnv` in the validation context, and
    ``~`` is expanded (:func:`~otto.utils.anchor_path`). *backend* names the
    source in the error raised when no env is given.
    """
    env = (info.context or {}).get("env")
    if env is None:
        raise ValueError(
            f"a {backend} lab source is validated with its LabSourceEnv in the validation "
            'context (context={"env": env}); otto passes it when it prepares the source'
        )
    return tuple(anchor_path(p, env.project_root, quote=False) for p in value)


class JsonLabSourceConfig(OttoModel):
    """A ``backend = "json"`` source's options: the ``paths`` it reads lab files from.

    Each entry is a directory (its ``lab.json`` is read), a ``.json`` file, or
    a glob over ``.json`` files. A relative entry is anchored to the project
    root, and ``~`` is expanded.
    """

    model_config = ConfigDict(frozen=True)

    paths: tuple[Path, ...] = Field(min_length=1)
    """The directories, ``.json`` files and globs the source reads, anchored to the project root."""

    @field_validator("paths", mode="before")
    @classmethod
    def _no_empty_entry(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the project root."""
        return refuse_empty_entry(value, expected="a directory, a .json file or a glob")

    @field_validator("paths", mode="after")
    @classmethod
    def _anchor(cls, value: tuple[Path, ...], info: ValidationInfo) -> tuple[Path, ...]:
        """Anchor every relative entry to the root of the project that declares the source."""
        return _anchored_entries(value, info, backend="json")

    def prepared_facts(self) -> LabSourceFacts:
        """Return the entries this source reads; otto fingerprints their files for the cache."""
        return LabSourceFacts(file_inputs=self.paths)


def lab_statuses_of(docs: "list[LabDocument]") -> list[LabStatus]:
    """Return the status of every lab *docs* declare, sorted by name.

    A lab whose ``labs`` entry sets ``unavailable`` carries that reason, where
    ``<file>: lab '<name>'``; every other lab is available. The one reading of
    a document's statuses, for every file-backed source.
    """
    statuses = [
        LabStatus(
            name=name,
            unavailable=None
            if entry.unavailable is None
            else Diagnostic(where=f"{doc.path}: lab {name!r}", reason=entry.unavailable),
        )
        for doc in docs
        for name, entry in doc.entries.items()
    ]
    return sorted(statuses, key=lambda status: status.name)


def _json_lab_source(c: "Configured[JsonLabSourceConfig, LabSourceEnv]") -> _JsonFileLabRepository:
    """Build the built-in json source from its parsed configuration."""
    return _JsonFileLabRepository(search_paths=list(c.config.paths), profiles=c.env.profiles)


def _entry_where(*, origin: Path, element: ElementSpec, index: int) -> str:
    """Return how a drop names host entry *index* of *element* in the document from *origin*."""
    return f"{origin}: element {element.name!r} hosts[{index}]"


@final
@dataclass(frozen=True, kw_only=True)
class IdentifiedEntry:
    """One host entry of a lab document, resolved through a view and identified."""

    origin: Path
    """The document the entry was declared in."""

    element: ElementSpec
    """The element the entry belongs to."""

    index: int
    """The entry's index in :attr:`element`'s ``hosts``."""

    resolved: "ResolvedEntry"
    """The entry joined on its inventory record (:func:`~otto.inventory.resolve_view_entry`)."""

    identity: "HostIdentity"
    """The host's identity (:func:`~otto.host.factory.host_identity`), its id among it."""

    def where(self) -> str:
        """Return how a drop names this entry: its document, element and index."""
        return _entry_where(origin=self.origin, element=self.element, index=self.index)


def _identified_entries(
    docs: "list[LabDocument]", *, view: "InventoryView", profiles: ProfileContext
) -> "Iterator[IdentifiedEntry]":
    """Resolve and identify every host entry of *docs* through *view*, in declaration order.

    Lazily, one entry at a time, so a caller's own drops (a summary's
    collision) interleave with these in declaration order.

    Each id comes from :func:`~otto.host.factory.host_identity` under
    *profiles* (the source's), the profile merge and validation host
    construction applies. Best-effort: an element whose runtime form refuses
    it, or an entry that does not resolve or validate, is left out and
    recorded as a drop (:func:`otto.labs.drops.record_drop`); a bare
    reference the view refuses records its own.
    """
    from ..inventory.errors import InventoryError  # lazy: see the note above
    from ..inventory.resolve import resolve_view_entry
    from .drops import record_drop, skip_failure

    for doc in docs:
        for element in doc.elements:
            try:
                runtime = element.to_element()
            except ValueError as e:
                # The runtime Element re-checks what ElementSpec already
                # refused; if the two ever drift apart, the element is
                # skipped and SAID, never raised into the shell.
                record_drop(f"{doc.path}: element {element.name!r}", str(e))
                continue
            for index, entry in enumerate(element.hosts):
                try:
                    resolved = resolve_view_entry(entry, view=view, element=runtime)
                    identity = host_identity(resolved.host_data, element=runtime, profiles=profiles)
                except (ValueError, TypeError, InventoryError) as e:
                    # Skipped, never raised — but SAID: this is the one
                    # place a referenced entry silently fell out of
                    # completion (see otto.labs.drops). A ValidationError is
                    # rendered without its input, as the loader renders it.
                    # A refused bare reference already recorded its drop.
                    skip_failure(
                        where=_entry_where(origin=doc.path, element=element, index=index),
                        error=e,
                        reason=(
                            compact_validation_error(e)
                            if isinstance(e, ValidationError)
                            else str(e)
                        ),
                    )
                    continue
                yield IdentifiedEntry(
                    origin=doc.path,
                    element=element,
                    index=index,
                    resolved=resolved,
                    identity=identity,
                )


def _addressing(
    docs: "list[LabDocument]", *, view: "InventoryView", profiles: ProfileContext
) -> "dict[str, HostAddressing]":
    """Return the addressing of every host record of *docs*, by host id, without building a host.

    The map declared links resolve their endpoints against, so it spans every
    document, including entries of other labs that were never validated.
    Each record is joined on its inventory record through *view*, and its id
    comes from :func:`~otto.link.derive.addressing_from_dict` under
    *profiles* (the source's). A record that cannot produce an id is skipped
    (logged at debug): it may belong to an unrelated lab, whose typo must not
    break this one. Of two records with one id the first is kept; a warning
    says so when their addressing differs.
    """
    from ..inventory.resolve import resolve_view_entry  # lazy: see the note above
    from ..link.derive import addressing_from_dict

    addressing: dict[str, HostAddressing] = {}
    # ``to_element()`` once per ELEMENT, not once per host: one runtime
    # element is shared by its hosts everywhere else, and rebuilding it per
    # entry here would be the only place that is not true.
    all_entries: list[tuple[Element, dict[str, Any]]] = []
    for doc in docs:
        for el in doc.elements:
            runtime = el.to_element()
            all_entries.extend((runtime, h) for h in el.hosts)
    for runtime, h in all_entries:
        try:
            # Resolved inside the per-item try: a referenced entry has no
            # address of its own, so a link naming one is only derivable
            # once the record is joined on — and an entry belonging to
            # ANOTHER lab (whose key this process's inventories may not
            # hold) is skipped here exactly like any other unresolvable
            # record, rather than failing this lab's load.
            host_id, host_addressing = addressing_from_dict(
                resolve_view_entry(h, view=view, element=runtime).host_data,
                runtime,
                profiles=profiles,
            )
        except Exception as e:  # noqa: BLE001 — per-item resilience, see the docstring
            # Log the reason: this now also fires for a WELL-FORMED record
            # whose os_profile / command_frame is registered by init modules
            # this process never loaded, and the downstream symptom is a
            # confusing "unknown host" for a host that is right there.
            logger.debug(f"Skipping unresolvable host record {h!r}: {e}")
            continue
        held = addressing.get(host_id)
        if held is not None:
            if held != host_addressing:
                logger.warning(
                    "Duplicate host id %r across lab files with differing addressing; "
                    "keeping the first. Differentiate the element name, or set board/slot.",
                    host_id,
                )
            continue
        addressing[host_id] = host_addressing
    return addressing


def declared_link_entries(read: "SourceRead") -> "list[DeclaredLinkEntry]":
    """Return every ``links`` entry of every document of *read*, in file then index order.

    The one extraction of a source's declared links, for all of its labs at
    once: it needs no lab name and builds no host, reading the documents'
    data only. Each entry keeps its document and its index there, which its
    error names.
    """
    from ..link.derive import DeclaredLinkEntry

    return [
        DeclaredLinkEntry(origin=doc.path, index=index, entry=entry)
        for doc in read.documents
        for index, entry in enumerate(doc.links)
    ]


def link_inputs_of(
    read: "SourceRead", *, view: "InventoryView", profiles: ProfileContext
) -> DeclaredLinkInputs:
    """Return *read*'s declared links, with a thunk that computes their addressing.

    The entries are :func:`declared_link_entries`; the thunk is
    ``_addressing`` over the same documents through *view*, each id under
    *profiles* (the source's), so a dangling endpoint gets the id host
    construction gives its record. A load that never resolves a link never
    calls it.
    """
    return DeclaredLinkInputs(
        entries=declared_link_entries(read),
        addressing=functools.partial(_addressing, read.documents, view=view, profiles=profiles),
    )


@dataclass(frozen=True, kw_only=True)
class _Declared:
    """One summarized record's machine identity, and its declaration as a message names it."""

    identity: "MachineIdentity"
    described: str


def _first_clash(*, incoming: _Declared, seen: "list[_Declared]") -> "_Declared | None":
    """Return the first of *seen* that is a different machine from *incoming*, or ``None``.

    :func:`~otto.host.inventory_ref.first_conflict` decides it.
    """
    from ..host.inventory_ref import first_conflict

    clash = first_conflict(incoming=incoming.identity, seen=[s.identity for s in seen])
    return None if clash is None else next(s for s in seen if s.identity is clash)


def _summary_collision(*, host_id: str, kept: _Declared, lost: _Declared) -> str:
    """Return the drop text for *lost*, a different machine from *kept* under *host_id*.

    Two addresses keep today's text. One address names both identities as
    ``name/key``: two inventories, or two keys of one, reusing the address.
    """
    from ..host.inventory_ref import spell_ref

    if kept.identity.ip != lost.identity.ip:
        return (
            f"host id {host_id!r}: {kept.described} collides with {lost.described} — two "
            "declarations compose one id; rename one"
        )
    return (
        f"host id {host_id!r}: {kept.described} as {spell_ref(ref=kept.identity.ref)} collides "
        f"with {lost.described} as {spell_ref(ref=lost.identity.ref)} — two inventories, or "
        "two keys of one, name two machines at one address; rename one"
    )


def _add_host(
    lab: "Lab",
    host_data: dict[str, Any],
    element: "Element",
    idx: int,
    doc: LabDocument,
    preferences: dict[str, dict[str, Any]] | None,
    view: "InventoryView",
    profiles: ProfileContext,
) -> None:
    """Build one host entry of *element*, from the lab file *doc*, and add it to *lab*.

    A free function rather than the loop body it is called from: the
    ``try``/``except`` belongs outside the loop (``PERF203``), and the error
    text is the point — spec §9 wants the file, the element and the index of
    the host entry that failed, not a running count across elements.

    THE join site (spec §6): every entry passes through
    :func:`~otto.inventory.resolve_view_entry` here, so the factory only ever
    sees a resolved dict (it REFUSES an unresolved one) and no other layer
    has to know the inventory exists. *view* is REQUIRED, like its five
    siblings — a default would let a future call site drop it silently, which
    is the exact mistake the conformance suite's signature rule exists to
    catch in third-party backends.

    The factory is the single validation: it runs structural and contextual
    validation (power included) itself, so nothing validates the entry first.

    EVERY failure becomes a :class:`~otto.labs.errors.LabRepositoryError`
    carrying that context, with the original chained as ``__cause__`` — an
    :class:`~otto.inventory.errors.InventoryError` included, which is what
    puts the file, the element and the index in front of "key not found".

    In a recorded build (``otto lab explain``), the resolution and the build
    run in the entry's :func:`~otto.host.provenance.entry_scope`, so each
    value the merges decide is attributed to this entry, and the recording
    is told which fields the join supplied. Otherwise nothing is done for it.
    """
    from ..inventory.resolve import (
        resolve_view_entry,  # lazy: see the note at the top of this module
    )

    path = doc.path
    recorded = recording_fields()
    try:
        with _entry_scope(doc=doc, element=element, index=idx) if recorded else NOT_RECORDING:
            entry = resolve_view_entry(host_data, view=view, element=element)
            if recorded:
                _joined(supplied=entry.supplied)
            host = create_host_from_dict(
                entry.host_data,
                preferences=preferences,
                lab_name=lab.name,
                element=element,
                inventory_ref=entry.ref,
                profiles=profiles,
            )
            if recorded:
                _built(host_id=host.id)
    except ValidationError as e:
        # A resolved host dict can carry a referenced host's STORE creds
        # (spec 2026-09-06 creds-store §6.2), appended last when the entry
        # has none inline — the scaffold's own shape. ``str(ValidationError)``
        # would end in pydantic's ``input_value=`` repr of that dict, so a
        # model-level failure on an otherwise-fine entry prints the store's
        # password. ``compact_validation_error`` never reads ``input``.
        raise LabRepositoryError(
            f"Lab file '{path}': element {element.name!r} hosts[{idx}] in lab {lab.name!r}: "
            f"{compact_validation_error(e)}"
        ) from e
    except Exception as e:
        # ``Exception``, not the validation errors alone: past the specs, the
        # factory runs project code — ``to_host`` plus the product and
        # dev-tool providers — which may raise anything at all. The composite
        # absorbs only LabNotFoundError, so a provider bug caught narrowly here
        # would surface as a raw traceback naming none of the hundred entries
        # it could have come from. Nothing is swallowed: the original is
        # chained and its text is quoted.
        raise LabRepositoryError(
            f"Lab file '{path}': element {element.name!r} hosts[{idx}] in lab {lab.name!r}: {e}"
        ) from e
    else:
        # Its own step: a collision refusal already says ``in lab '<name>'`` and
        # names both declarations, so it takes the file/element/index prefix
        # without the lab framing the failures above carry.
        try:
            lab.add_host(host)
        except LabRepositoryError as e:
            raise LabRepositoryError(
                f"Lab file '{path}': element {element.name!r} hosts[{idx}]: {e}"
            ) from e


def _entry_scope(
    *, doc: LabDocument, element: "Element", index: int
) -> "AbstractContextManager[None]":
    """Return the recorded entry scope of *element*'s *index*-th host in *doc*.

    Only a live recording (:func:`~otto.host.inventory_ref.recording_fields`)
    calls it, so a build that explains nothing imports, keys and reads
    nothing for it. The scope carries the element's entry in *doc*'s rendered
    provenance table.
    """
    from ..host import provenance

    table = doc.provenance
    found = None if table is None else table.get(element.name)
    key = provenance.EntryKey(
        source=provenance.current_source(), origin=doc.path, element=element.name, index=index
    )
    return provenance.entry_scope(key, provenance=found if isinstance(found, Mapping) else None)


def _joined(*, supplied: "frozenset[str]") -> None:
    """Tell the recording which host fields the inventory join decided (recorded builds only)."""
    from ..host import provenance

    provenance.joined(supplied=supplied)


def _built(*, host_id: str) -> None:
    """Tell the recording which host the entry built (recorded builds only)."""
    from ..host import provenance

    provenance.built(host_id=host_id)


def parse_lab_document(
    sections: dict[str, Any],
    lab_file: Path,
    *,
    seen_labs: dict[str, Path],
    seen_elements: "dict[str, SeenElement]",
    provenance: "Mapping[str, Any] | None" = None,
) -> LabDocument:
    """Parse one lab file's validated *sections* into its document.

    The labs table and the elements are parsed, then the in-source duplicate
    rules run against the source's *seen_labs* / *seen_elements* (which this
    updates). The one source read (:func:`otto.labs.documents.read_source`)
    runs this for the runtime loader and the ``otto init`` doctor alike, so what
    one accepts the other accepts. *provenance* is a rendered document's
    origin data, kept on the document as :attr:`LabDocument.provenance`.

    Raises:
        LabRepositoryError: a malformed entry, or a duplicate within the source.
    """
    entries = parse_lab_entries(sections["labs"], str(lab_file))
    elements = parse_elements(sections["elements"], str(lab_file))
    check_in_source_duplicates(
        entries, elements, lab_file, seen_labs=seen_labs, seen_elements=seen_elements
    )
    return LabDocument(lab_file, entries, elements, sections["links"], provenance)


def check_in_source_duplicates(
    entries: dict[str, LabEntrySpec],
    elements: list[ElementSpec],
    lab_file: Path,
    *,
    seen_labs: dict[str, Path],
    seen_elements: dict[str, SeenElement],
) -> None:
    """Reject, then record, one file's declarations against its SOURCE's running state.

    Within one source a duplicate is a typo, never an override (spec §2.4): the
    same lab declared, or the same element slug carried, by two of a source's
    files fails naming both. Across sources the same re-declaration is
    legal — that is the ``[[lab.sources]]`` override seam — so *seen_labs* and
    *seen_elements* must be reset per source and threaded across only that
    source's files.

    Checking and recording are one call deliberately: a caller that checked but
    forgot to record would silently accept every duplicate after the first.
    The recording happens only once both checks pass, so a rejected file never
    poisons the state for the files after it.

    Its caller is :func:`parse_lab_document`, which the one source read
    (:func:`otto.labs.documents.read_source`) runs for both
    ``_JsonFileLabRepository._read_source`` and the ``otto init`` doctor
    (``otto.init.doctor._parse_lab_documents``): the doctor must refuse exactly
    what the loader refuses, and it can only do that by running this code
    rather than a second copy of it.

    Raises
    ------
    LabRepositoryError
        Naming both files, on a duplicate lab declaration or element key.
    """
    _reject_duplicate_labs(entries, seen_labs, lab_file)
    _reject_duplicate_elements(elements, seen_elements, lab_file)
    seen_labs.update(dict.fromkeys(entries, lab_file))
    seen_elements.update({el.key: SeenElement(lab_file, el.name) for el in elements})


def _reject_duplicate_labs(
    entries: dict[str, LabEntrySpec], seen: dict[str, Path], lab_file: Path
) -> None:
    """Raise when a lab *entries* declares was already declared by another file."""
    for name in entries:
        if name in seen:
            raise LabRepositoryError(
                f"lab {name!r} declared in both {seen[name]} and {lab_file} — "
                f"one source, one declaration (a second [[lab.sources]] entry is "
                f"the override seam)"
            )


def _reject_duplicate_elements(
    elements: list[ElementSpec], seen: dict[str, SeenElement], lab_file: Path
) -> None:
    """Raise when an element of *elements* was already carried by another file.

    The two forms mirror :func:`parse_elements` exactly (spec 2026-09-05 §6),
    with the two file paths standing where the two entry indices stand there:
    identical names read as a duplicate, and spellings that differ say what
    each file wrote and the slug they share.
    """
    for element in elements:
        first = seen.get(element.key)
        if first is None:
            continue
        if first.name == element.name:
            detail = f"duplicate element {element.name!r} in {first.path} and {lab_file}"
        else:
            detail = (
                f"{first.path} {first.name!r} and {lab_file} {element.name!r} "
                f"both slug to {element.key!r}"
            )
        raise LabRepositoryError(f"{detail} — one element, one entry")
