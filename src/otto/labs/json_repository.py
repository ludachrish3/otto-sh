"""Built-in ``"json"`` ``LabRepository`` backend: loads labs from ``lab.json`` files."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..host.factory import (
    create_host_from_dict,
    host_identity,
    validate_host_dict,
)
from ..models.lab import ElementKey, ElementSpec, LabEntrySpec
from .errors import (
    LabNotFoundError,
    LabRepositoryError,
)
from .protocol import HostSummary

if TYPE_CHECKING:
    from collections.abc import Iterable

    # Deferred: otto.config.lab imports this module (for the built-in "json"
    # backend), so a module-level import here would cycle when otto.labs is
    # the first thing imported. load_lab() below imports Lab locally at call
    # time instead, by which point both modules are fully initialized.
    from ..config.lab import Lab
    from ..inventory import Inventory

logger = logging.getLogger(__name__)

LAB_FILENAME = "lab.json"

# Known top-level sections of a lab.json object (v2): ``labs`` is the table of
# declared labs keyed by name, ``elements`` the array of element entries (each
# grouping host entries), ``links`` the array of declared data-plane routes.
_LAB_SECTIONS = frozenset({"labs", "elements", "links"})

_MIGRATION_HINT = (
    "top-level 'hosts' has moved: hosts are grouped under 'elements' (each with "
    "'name', optional 'id', 'labs' membership patterns, optional 'metadata', and "
    "'hosts'), and per-host 'labs' moved to the element (a host's 'resources' may "
    "stay on the host — a slot — or move to the element or the 'labs' table). "
    "See docs/guide/configuration/lab-config.md, "
    '"Migrating from the hosts array".'
)

_GLOB_CHARS = frozenset("*?[")

# WHY `from ..inventory import ...` is function-local at every use site below
# (three of them): this module is reached from every budgeted CLI surface
# (scripts/import_budget.py), and a module-scope import would put
# otto.inventory's nine modules on all ten of them for the overwhelmingly
# common process that never resolves a single reference. The edge is real and
# declared in tach.toml; only the timing is deferred, and the cost per call is
# a sys.modules lookup beside a pydantic validation.


def expand_lab_paths(paths: "Iterable[Path]") -> list[Path]:
    """Every lab file a list of ``paths`` entries names (spec §2.4).

    THE single home of the entry-to-files rule, read by the json backend, the
    completion-cache fingerprint and the ``otto init`` doctor: a directory
    contributes its ``lab.json``; a ``.json`` path is the file; an entry with
    a glob metacharacter is expanded relative to its non-glob prefix (sorted,
    files only, ``.json`` only). Entries that resolve to nothing are skipped —
    the composite's existence rule reports the consequence.

    Each FILE appears once, at its first-seen position, however many entries
    name it: ``paths = [d, d/"*.json"]`` — the main file by directory plus the
    split files by glob — is the documented layout written the natural way, and
    listing ``d/lab.json`` twice would trip the in-source duplicate rule
    (:func:`check_in_source_duplicates`) against the file itself. Identity is
    the RESOLVED path, so two spellings of one file collapse.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        """Append *candidate* unless some earlier entry already named that file."""
        key = candidate.resolve()
        if key in seen:
            logger.debug(f"lab path {candidate} already contributed by an earlier entry")
            return
        seen.add(key)
        found.append(candidate)

    for entry in paths:
        text = str(entry)
        if any(c in text for c in _GLOB_CHARS):
            parts = entry.parts
            first_glob = next(i for i, p in enumerate(parts) if any(c in p for c in _GLOB_CHARS))
            root = Path(*parts[:first_glob]) if first_glob else Path()
            pattern = str(Path(*parts[first_glob:]))
            matches = sorted(p for p in root.glob(pattern) if p.is_file() and p.suffix == ".json")
            if not matches:
                logger.debug(f"lab path glob {text!r} matched no .json file")
            for match in matches:
                add(match)
            continue
        candidate = entry if entry.suffix == ".json" else entry / LAB_FILENAME
        if candidate.is_file():
            add(candidate)
    return found


def parse_lab_sections(data: object, source: str) -> dict[str, Any]:
    """Validate a parsed ``lab.json`` object's section shape; return its sections.

    The single source of truth for the ``lab.json`` object contract — shared by
    the runtime loader (``JsonFileLabRepository._load_lab_file``) and the
    ``otto init`` doctor (``otto.cli.init._validate_lab``) so the doctor
    cannot drift from what otto actually accepts (there is no second validator
    to drift). *data* is the already-parsed JSON value; *source* names its
    origin (a file path) for error messages.

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
    """Validate every ``elements`` entry; duplicate ``(name, id)`` is an error."""
    seen: dict[ElementKey, int] = {}
    out: list[ElementSpec] = []
    for idx, entry in enumerate(raw):
        spec = _parse_element(entry, idx, source)
        if spec.key in seen:
            raise LabRepositoryError(
                f"Lab file '{source}': duplicate element {spec.key} at elements[{seen[spec.key]}] "
                f"and elements[{idx}] — one element, one entry"
            )
        seen[spec.key] = idx
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


@dataclass(frozen=True)
class _Document:
    """One parsed lab file of a source: its labs table, elements and raw links."""

    path: Path
    """The file these sections came from — what an error message names."""

    entries: dict[str, LabEntrySpec]
    """The file's ``labs`` table, keyed by lab name."""

    elements: list[ElementSpec]
    """The file's ``elements``, in declaration order."""

    links: list[Any]
    """The file's raw ``links`` entries, resolved later against loaded ids."""


class JsonFileLabRepository:
    """Load labs from ``lab.json`` files across a fixed set of search paths.

    A search path takes one of three forms: a **directory**, which is searched
    for a ``lab.json``; a path ending in **``.json``**, which *is* the lab
    file; or a **glob** (an entry containing ``*``, ``?`` or ``[``), which
    expands to the sorted ``.json`` files it matches. Any form that resolves to
    nothing is skipped silently, so a repository may draw on several sources
    and tolerate absent ones.

    Each lab file is a v2 JSON object with three optional sections — ``labs``
    (the table of declared labs, keyed by name, each with ``resources`` and
    ``metadata``), ``elements`` (entries carrying identity, ``labs``
    membership patterns, ``metadata`` and their host entries) and ``links``.
    A top-level ``hosts`` key is the v1 shape and fails with a migration
    message; top-level ``_``-prefixed keys are comment space and unknown
    sections fail loud.

    The files of ONE source compose by union (spec §2.4): an element in one
    file may join a lab declared in another, and a duplicate within the source
    — the same element ``(name, id)``, or the same lab declared twice — is a
    typo, not an override, and fails naming both files. The search paths are
    supplied once at construction — this is the built-in ``"json"`` backend,
    and :func:`otto.labs.build_lab_sources` feeds it the ``paths`` of the
    ``[[lab.sources]]`` entry that selected it.

    :meth:`load_lab` returns THIS source's contribution to a lab: the members
    whose element patterns :func:`re.fullmatch` the requested name, plus the
    lab's ``resources``/``metadata`` if this source declares it. The composite
    (:class:`otto.labs.composite.CompositeLabRepository`) merges contributions
    across sources.
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        """Configure the paths this repository draws lab data from.

        Each entry of *search_paths* is a directory (searched for a
        ``lab.json``), a path ending in ``.json`` (read directly as the lab
        file), or a glob (expanded to the ``.json`` files it matches).
        Entries that do not resolve to an existing file are skipped.
        """
        self.search_paths: list[Path] = list(search_paths or [])

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> "Lab":
        """Load this source's contribution to the lab called *name*.

        Members are the hosts of every element whose ``labs`` patterns
        fullmatch *name*; ``resources`` and ``metadata[name]`` are filled only
        when this source's ``labs`` table declares *name*.

        *inventory* is the process inventory referenced entries resolve
        against (spec 2026-08-28 host-inventory §6). The join happens HERE,
        once per entry, before the factory ever sees the dict; ``None`` means
        a referenced entry is an error naming the file, the element and the
        index.

        Raises
        ------
        LabNotFoundError
            If no lab file exists in any search path, or this source neither
            declares *name* nor holds an element matching it.
        LabRepositoryError
            If a lab.json is malformed, a host's data is invalid, or an entry
            references an inventory key that cannot be resolved.
        """
        from ..config.lab import Lab
        from ..inventory import resolve_host_entry  # lazy: see the note above

        try:
            docs = self._load_documents()
        except FileNotFoundError as e:
            raise LabNotFoundError(str(e)) from None

        declared: LabEntrySpec | None = None
        members: list[tuple[ElementSpec, Path]] = []  # (element, source file)
        all_elements: list[ElementSpec] = []
        all_links: list[Any] = []
        for doc in docs:
            if name in doc.entries:
                # Unique per source: _load_documents errors on a repeat.
                declared = doc.entries[name]
            members.extend((el, doc.path) for el in doc.elements if el.matches(name))
            all_elements.extend(doc.elements)
            all_links.extend(doc.links)
        # ``all_elements`` and ``all_links`` span ALL lab files so declared links
        # can resolve dangling (cross-lab) endpoints below.

        if declared is None and not members:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise LabNotFoundError(
                f"Lab '{name}' is neither declared in a 'labs' table nor matched by any "
                f"element's 'labs' patterns in:\n  {searched}"
            ) from None

        lab = Lab(name=name)
        if declared is not None:
            # The lab's own resources are DECLARED here, never read back off
            # its hosts (one of three levels since spec 2026-08-28
            # three-level-reservations).
            lab.resources = set(declared.resources)
            lab.metadata[name] = dict(declared.metadata)

        for element, path in members:
            for idx, host_data in enumerate(element.flatten()):
                _add_host(lab, host_data, element, idx, path, preferences, inventory)

        from ..link.derive import addressing_from_dict, resolve_declared_links

        # Only declared links consume `addressing`, and resolving an id costs a
        # full profile-merge + validation per record across EVERY lab file — so
        # a lab that declares no links (the default `otto init` writes, and the
        # common case) skips the walk entirely rather than paying for a map
        # nothing reads.
        #
        # Guard: the flattened records span ALL lab files, including entries
        # never validated (they belong to other labs) — skip shapes that can't
        # produce an id rather than crash link resolution on someone else's typo.
        # The requested lab's own hosts were already validated above, so any
        # exception here belongs to an unrelated lab's malformed record. No
        # `element` guard: a record whose element comes from its os_profile DOES
        # load, so skipping it would fail a link naming a host that plainly
        # exists — the except below covers the genuinely element-less record.
        addressing: dict[str, Any] = {}
        all_flat = [f for el in all_elements for f in el.flatten()] if all_links else []
        for h in all_flat:
            try:
                # Resolved inside the per-item try: a referenced entry has no
                # address of its own, so a link naming one is only derivable
                # once the record is joined on — and an entry belonging to
                # ANOTHER lab (whose key this process's inventory may not
                # hold) is skipped here exactly like any other unresolvable
                # record, rather than failing this lab's load.
                host_id, host_addressing = addressing_from_dict(
                    resolve_host_entry(h, inventory).host_data
                )
            except Exception as e:  # noqa: BLE001 — per-item resilience, see guard above
                # Log the reason: this now also fires for a WELL-FORMED record
                # whose os_profile / command_frame is registered by init modules
                # this process never loaded, and the downstream symptom is a
                # confusing "unknown host" for a host that is right there.
                logger.debug(f"Skipping unresolvable host record {h!r}: {e}")
                continue
            if host_id in addressing and addressing[host_id] != host_addressing:
                logger.warning(
                    "Duplicate host id %r across lab files with differing addressing; "
                    "keeping the first. Differentiate the element, element_id, or board/slot.",
                    host_id,
                )
                continue
            addressing[host_id] = host_addressing
        loaded_ids = set(lab.hosts)
        # ``all_links`` spans ALL lab files (like ``all_elements``), so a
        # typo'd link between two hosts of an UNRELATED lab must not break this
        # lab's load: ``resolve_declared_links`` skips entries touching no loaded
        # host, symmetric with the cross-lab host-record containment above. Links
        # touching this lab still fail loud with their original file index.
        try:
            declared_links = resolve_declared_links(
                all_links, addressing, source=LAB_FILENAME, loaded_ids=loaded_ids
            )
        except ValueError as e:
            raise LabRepositoryError(str(e)) from e
        # Membership: only links with >= 1 endpoint in this lab (guaranteed by
        # the skip above, restated here so the invariant is visible at the call site).
        lab.links = [
            link
            for link in declared_links
            if link.a.host in loaded_ids or link.b.host in loaded_ids
        ]

        logger.debug(f"Loaded lab '{name}' with {len(lab.hosts)} hosts")
        return lab

    def list_host_summaries(self, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Every host across the configured lab files, without building hosts.

        The :class:`~otto.labs.protocol.SupportsHostSummaries` fast path.
        Each id comes from :func:`~otto.host.factory.host_identity`, which
        applies the same profile merge and validation
        :func:`~otto.host.factory.create_host_from_dict` applies — so an id
        offered by completion is one that dispatches. Deriving ids by
        formatting the raw JSON instead would silently diverge (a float
        ``element_id``, or a profile that defaults ``board``/``slot``).

        ``labs`` is each element's patterns resolved against the labs THIS
        source declares; ``lab_patterns`` carries the patterns themselves, so
        the composite can re-resolve them against every source's declarations.

        A referenced entry is identified through its record, so a summary may
        carry an inventory-supplied ``ip``; without the inventory such an
        entry is skipped, like any other unresolvable record.

        Best-effort, like :meth:`list_labs`: a malformed file or host entry
        is skipped rather than raised — these feed completion, which must
        never crash the shell. Hosts listed in several lab files merge by
        id, unioning their ``labs``.
        """
        from ..inventory import InventoryError, resolve_host_entry  # lazy: see the note above

        by_id: dict[str, HostSummary] = {}

        try:
            docs = self._load_documents(best_effort=True)
        except FileNotFoundError:
            return []

        declared = sorted({n for doc in docs for n in doc.entries})
        for doc in docs:
            for element in doc.elements:
                labs = [n for n in declared if element.matches(n)]
                for flat in element.flatten():
                    try:
                        identity = host_identity(resolve_host_entry(flat, inventory).host_data)
                    except (ValueError, TypeError, InventoryError):
                        continue
                    existing = by_id.get(identity.id)
                    if existing is not None:
                        existing.labs.extend(n for n in labs if n not in existing.labs)
                        existing.lab_patterns.extend(
                            p for p in element.labs if p not in existing.lab_patterns
                        )
                        continue
                    by_id[identity.id] = HostSummary(
                        id=identity.id,
                        labs=list(labs),
                        lab_patterns=list(element.labs),
                        ip=identity.ip,
                        element=identity.element,
                        element_id=identity.element_id,
                        docker_capable=identity.docker_capable,
                    )

        return sorted(by_id.values(), key=lambda s: s.id)

    def list_labs(self) -> list[str]:
        """List the lab names DECLARED across the configured paths.

        The declared set, not the elements' membership patterns: a pattern is
        a regex, not a name (one with a quantifier or a group names nothing),
        and an undeclared lab does not exist (spec §2.1). Returns an empty list
        when no lab file exists. A malformed lab.json is skipped rather than
        raised, so listing stays best-effort.
        """
        try:
            docs = self._load_documents(best_effort=True)
        except FileNotFoundError:
            return []
        return sorted({n for doc in docs for n in doc.entries})

    def _load_documents(self, *, best_effort: bool = False) -> list[_Document]:
        """Parse every lab file: its path, labs table, elements and raw links.

        Within ONE source a duplicate is a typo, never an override: the same
        element ``(name, id)`` or the same lab declaration in two files fails
        naming both. *best_effort* (completion paths) skips a malformed or
        duplicating file with a debug log instead of raising.
        """
        docs: list[_Document] = []
        seen_elements: dict[ElementKey, Path] = {}
        seen_labs: dict[str, Path] = {}
        for lab_file in self._find_lab_files():
            try:
                sections = self._load_lab_file(lab_file)
                entries = parse_lab_entries(sections["labs"], str(lab_file))
                elements = parse_elements(sections["elements"], str(lab_file))
                check_in_source_duplicates(
                    entries,
                    elements,
                    lab_file,
                    seen_labs=seen_labs,
                    seen_elements=seen_elements,
                )
            except LabRepositoryError:
                if not best_effort:
                    raise
                logger.debug(
                    f"skipping lab file {lab_file} while enumerating (malformed or duplicating)"
                )
                continue
            docs.append(_Document(lab_file, entries, elements, sections["links"]))
        return docs

    def _find_lab_files(self) -> list[Path]:
        """Find all lab files across the configured search paths.

        Delegates the entry-to-files rule to :func:`expand_lab_paths` — the
        same helper ``CompiledLabSource.lab_files()`` reads, so the backend and
        the config side can never disagree about which files a source holds.

        Raises
        ------
        FileNotFoundError
            Internal signal (translated to LabNotFoundError by ``load_lab`` and
            swallowed by ``list_labs``) when no lab file is found.
        """
        found = expand_lab_paths(self.search_paths)

        if not found:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise FileNotFoundError(
                f"No lab data found in any search path (directories are searched "
                f"for {LAB_FILENAME}; .json entries are read directly; globs are "
                f"expanded):\n  {searched}"
            ) from None

        return found

    def _load_lab_file(self, lab_file: Path) -> dict[str, Any]:
        """Load one ``lab.json``: an object with ``labs``/``elements``/``links`` sections.

        Reads the file, then delegates the section-shape contract (object guard,
        ``_``-comment allowance — also tolerating a top-level ``$schema`` key,
        the editor-wiring idiom — v1 ``hosts`` rejection, unknown-section
        rejection, per-section type check) to :func:`parse_lab_sections` — the
        same helper the ``otto init`` doctor uses, so the two can never diverge.

        Raises
        ------
        LabRepositoryError
            If the file contains malformed JSON, its top-level value is not a
            JSON object, it carries the v1 ``hosts`` section or an unknown one,
            or a section's value has the wrong JSON type.
        """
        try:
            with lab_file.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise LabRepositoryError(f"Lab file '{lab_file}' contains malformed JSON: {e}") from e
        return parse_lab_sections(data, str(lab_file))


def _add_host(
    lab: "Lab",
    host_data: dict[str, Any],
    element: ElementSpec,
    idx: int,
    path: Path,
    preferences: dict[str, dict[str, Any]] | None,
    inventory: "Inventory | None",
) -> None:
    """Build one flattened host entry and add it to *lab*.

    A free function rather than the loop body it is called from: the
    ``try``/``except`` belongs outside the loop (``PERF203``), and the error
    text is the point — spec §9 wants the file, the element and the index of
    the host entry that failed, not a running count across elements.

    THE join site (spec §6): every entry passes through
    :func:`~otto.inventory.resolve_host_entry` here, so the factory only ever
    sees a resolved dict (it REFUSES an unresolved one) and no other layer
    has to know the inventory exists. *inventory* is REQUIRED, like its five
    siblings — a default would let a future call site drop it silently, which
    is the exact mistake the conformance suite's signature rule exists to
    catch in third-party backends.

    EVERY failure becomes a :class:`~otto.labs.errors.LabRepositoryError`
    carrying that context, with the original chained as ``__cause__`` — an
    :class:`~otto.inventory.errors.InventoryError` included, which is what
    puts the file, the element and the index in front of "key not found".
    """
    from ..inventory import resolve_host_entry  # lazy: see the note at the top of this module

    try:
        entry = resolve_host_entry(host_data, inventory)
        validate_host_dict(entry.host_data)
        host = create_host_from_dict(
            entry.host_data,
            preferences=preferences,
            lab_name=lab.name,
            element_metadata=element.metadata,
            element_resources=element.resources,
            inventory_ref=entry.ref,
        )
        lab.add_host(host)
    except Exception as e:
        # ``Exception``, not the validation errors alone: past the specs, the
        # factory runs SUT-REPO code — ``to_host`` plus the product and
        # dev-tool providers — which may raise anything at all. The composite
        # absorbs only LabNotFoundError, so a provider bug caught narrowly here
        # would surface as a raw traceback naming none of the hundred entries
        # it could have come from. Nothing is swallowed: the original is
        # chained and its text is quoted.
        raise LabRepositoryError(
            f"Lab file '{path}': element {element.name!r} hosts[{idx}] in lab {lab.name!r}: {e}"
        ) from e


def check_in_source_duplicates(
    entries: dict[str, LabEntrySpec],
    elements: list[ElementSpec],
    lab_file: Path,
    *,
    seen_labs: dict[str, Path],
    seen_elements: dict[ElementKey, Path],
) -> None:
    """Reject, then record, one file's declarations against its SOURCE's running state.

    Within one source a duplicate is a typo, never an override (spec §2.4): the
    same lab declared, or the same element ``(name, id)`` carried, by two of a
    source's files fails naming both. Across sources the same re-declaration is
    legal — that is the ``[[lab.sources]]`` override seam — so *seen_labs* and
    *seen_elements* must be reset per source and threaded across only that
    source's files.

    Checking and recording are one call deliberately: a caller that checked but
    forgot to record would silently accept every duplicate after the first.
    The recording happens only once both checks pass, so a rejected file never
    poisons the state for the files after it.

    Callers are ``JsonFileLabRepository._load_documents`` and the
    ``otto init`` doctor (``otto.cli.init._parse_lab_documents``), which is the
    whole point of it being public: the doctor must refuse exactly what the
    loader refuses, and it can only do that by running this code rather than a
    second copy of it.

    Raises
    ------
    LabRepositoryError
        Naming both files, on a duplicate lab declaration or element key.
    """
    _reject_duplicate_labs(entries, seen_labs, lab_file)
    _reject_duplicate_elements(elements, seen_elements, lab_file)
    seen_labs.update(dict.fromkeys(entries, lab_file))
    seen_elements.update({el.key: lab_file for el in elements})


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
    elements: list[ElementSpec], seen: dict[ElementKey, Path], lab_file: Path
) -> None:
    """Raise when an element of *elements* was already carried by another file."""
    for element in elements:
        if element.key in seen:
            raise LabRepositoryError(
                f"duplicate element {element.key} in {seen[element.key]} and "
                f"{lab_file} — one element, one entry"
            )
