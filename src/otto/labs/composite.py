"""``CompositeLabRepository`` — N ordered host sources behind one ``LabRepository``.

The combining layer for ``[[lab.sources]]`` (specs:
docs/superpowers/specs/2026-08-19-multi-source-lab-data-design.md §6 and
docs/superpowers/specs/2026-08-27-lab-definition-v2-design.md §6). Sources are
consulted in declaration order; the LATER source wins wholesale at RECORD
granularity, with a warning naming both sources — the sanctioned way to test a
data change locally before it lands in a global database. Under v2 there are
two such records: the **element** ``(name, id)`` (its hosts, membership and
metadata replaced together) and the **``labs`` table entry** (its resources and
metadata replaced together). Never a field-level blend: a hybrid record — this
source's hosts with that source's metadata — is unrepresentable by design.

This class is also the single owner of lab EXISTENCE: a lab exists once some
source's ``list_labs()`` declares it, and only then (spec §2.1). Elements that
match a name nothing declares do not conjure a lab, and a declared lab that no
element matches is a definition mistake, not an empty lab — both are loud here
and nowhere else.

This merge is deliberately NOT ``Lab.__add__``: that operator merges DIFFERENT
labs (``a+b``, same-ip dedup / different-ip error); this class merges the SAME
lab across sources, and the two rule sets must never blend.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import LabNotFoundError, LabRepositoryError
from .protocol import HostSummary

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..inventory import Inventory
    from .protocol import LabRepository

logger = logging.getLogger(__name__)

# An element's identity as this module keys it: ``(host.element, host.element_id)``.
# Built from the HOSTS a source returned, not from the file's ``elements``
# entries — a backend need not have a file at all, and every backend's hosts
# carry the pair. A local dict key, never a return value.
_ElementKey = tuple[str, int | None]


@dataclass
class LabSource:
    """One constructed host source: its user-facing label and its backend."""

    label: str
    repository: "LabRepository"


class CompositeLabRepository:
    """Merge N ``LabRepository`` backends, later sources overriding earlier.

    Satisfies ``LabRepository`` and ``SupportsHostSummaries``, so every
    downstream consumer (``config.lab.load_lab``, completion, conformance)
    works unchanged. An EMPTY composite is valid: it lists no labs and
    ``load_lab`` fails loud with configuration guidance — the state of a
    process where no repo declares a ``[[lab.sources]]`` entry.
    """

    def __init__(self, sources: "list[LabSource]") -> None:
        self.sources: "list[LabSource]" = list(sources)

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> "Lab":
        """Merge *name* across every source that knows it (spec §6).

        Existence first: *name* must be DECLARED by some source's
        ``list_labs()``. Then each source's contribution is merged in order —
        elements replaced wholesale by ``(element, element_id)``, the ``labs``
        entry replaced wholesale by the last declaring source.

        *inventory* is forwarded to EVERY source: a process has exactly one
        inventory (spec §8), and two sources resolving the same key against
        different records is the drift this layer exists to prevent.

        Raises
        ------
        LabNotFoundError
            If no source declares *name* (message names every source), no
            source provides it, or no sources are configured at all. The first
            two carry each source's own not-found text, one indented line
            apiece — for the json backend, the search paths it looked in.
        LabRepositoryError
            If *name* is declared but no element anywhere matches it, if two
            surviving elements produce the same host id, or propagated from
            the first failing source — a broken backend is never silently
            dropped from the merge.
        """
        if not self.sources:
            raise LabNotFoundError(
                f"Lab {name!r} cannot be loaded: no repo declares a "
                "[[lab.sources]] entry in .otto/settings.toml"
            )
        declared_by = [s.label for s in self.sources if name in s.repository.list_labs()]
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
                lab = source.repository.load_lab(name, preferences=preferences, inventory=inventory)
            except LabNotFoundError as e:  # a per-source protocol signal, not a failure
                reasons.append(f"{source.label}: {e}")
                continue
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
                        del merged.hosts[hid]
                        del element_of[hid]
                for host_id, host in hosts.items():
                    if host_id in merged.hosts:
                        raise LabRepositoryError(
                            f"host id {host_id!r} in lab {name!r}: element "
                            f"{element_of[host_id]!r} from {won_by[element_of[host_id]]} "
                            f"collides with element {key!r} from {label}"
                        )
                    merged.hosts[host_id] = host
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
        merged._assign_logical_indices()  # noqa: SLF001 — every lab-producing path restamps
        return merged

    def list_labs(self) -> list[str]:
        """Sorted union across sources; a backend error propagates (spec §6.2)."""
        names: set[str] = set()
        for source in self.sources:
            names.update(source.repository.list_labs())
        return sorted(names)

    def list_host_summaries(self, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Union by host id, later source wins, memberships unioned (spec §6.3).

        Memberships are also RE-RESOLVED: a summary carries the membership
        patterns of its element, and a pattern declared in one source reaches a
        lab declared only in another, so the union of every source's
        ``list_labs()`` is the set they are matched against here. Resolving
        per source (as each backend must, seeing only its own declarations)
        would leave a host out of exactly the cross-source labs this class
        exists to compose.

        Best-effort by contract: this feeds shell completion, which must never
        crash or warn into the user's TAB — a failing source is skipped with a
        debug log, unlike ``load_lab``/``list_labs`` which stay loud.
        """
        from . import host_summaries  # lazy: package __init__ imports this module's siblings

        declared: set[str] = set()
        for source in self.sources:
            declared.update(_declared_names(source))
        names = sorted(declared)
        by_id: dict[str, HostSummary] = {}
        for source in self.sources:
            try:
                summaries = host_summaries(source.repository, inventory=inventory)
            except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
                logger.debug(f"skipping source {source.label} while summarizing hosts: {e}")
                continue
            for s in summaries:
                by_id[s.id] = _merged_summary(s, by_id.get(s.id), names)
        return sorted(by_id.values(), key=lambda s: s.id)


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
    """Return the ``(element, element_id)`` pair *host* belongs to (spec §6).

    ``getattr`` rather than attribute access: only a
    :class:`~otto.host.remote_host.RemoteHost` carries ``element`` at all
    (:class:`~otto.host.local_host.LocalHost` has no such field), and a
    backend is free to put one in a lab — an element-less host still has to
    key somewhere, and it keys under ``("", None)``.
    """
    return (getattr(host, "element", "") or "", getattr(host, "element_id", None))


def _merged_summary(s: HostSummary, existing: HostSummary | None, names: list[str]) -> HostSummary:
    """Fold summary *s* onto *existing* (same id), re-resolving memberships.

    Later source wins the scalar fields; ``labs`` and ``lab_patterns`` union in
    first-seen order; then every declared name in *names* that a surviving
    pattern fullmatches joins ``labs``.
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
    return HostSummary(
        id=s.id,
        labs=labs,
        lab_patterns=patterns,
        ip=s.ip,
        element=s.element,
        element_id=s.element_id,
        docker_capable=s.docker_capable,
    )
