"""``CompositeLabRepository`` — N ordered host sources behind one ``LabRepository``.

The combining layer for ``[[lab.sources]]`` (spec:
docs/superpowers/specs/2026-08-19-multi-source-lab-data-design.md §6). Sources
are consulted in declaration order; on a same-lab host-id collision the LATER
source's record wins wholesale, with a warning naming both sources — the
sanctioned way to test a data change locally before it lands in a global
database. This merge is deliberately NOT ``Lab.__add__``: that operator merges
DIFFERENT labs (``a+b``, same-ip dedup / different-ip error); this class merges
the SAME lab across sources, and the two rule sets must never blend.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import LabNotFoundError
from .protocol import HostSummary

if TYPE_CHECKING:
    from ..config.lab import Lab
    from .protocol import LabRepository

logger = logging.getLogger(__name__)


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
    ) -> "Lab":
        """Merge *name* across every source that knows it (spec §6.1).

        Raises
        ------
        LabNotFoundError
            If no source provides *name* (message names every source), or no
            sources are configured at all.
        LabRepositoryError
            Propagated from the first failing source — a broken backend is
            never silently dropped from the merge.
        """
        if not self.sources:
            raise LabNotFoundError(
                f"Lab {name!r} cannot be loaded: no repo declares a "
                "[[lab.sources]] entry in .otto/settings.toml"
            )
        loaded: "list[tuple[str, Lab]]" = []
        for source in self.sources:
            try:
                lab = source.repository.load_lab(name, preferences=preferences)
            except LabNotFoundError:  # a per-source protocol signal, not a failure
                continue
            loaded.append((source.label, lab))
        if not loaded:
            labels = ", ".join(s.label for s in self.sources)
            raise LabNotFoundError(f"Lab {name!r} not found in any configured source: {labels}")

        from ..host.remote_host import RemoteHost  # lazy: mirror Lab.add_host

        # Before the merge: ``merged`` aliases the first source's Lab, and the
        # host replacement below is in place — see _lab_level_extras.
        extras = self._lab_level_extras([lab for _, lab in loaded])
        first_label, merged = loaded[0]
        won_by: dict[str, str] = dict.fromkeys(merged.hosts, first_label)
        for label, lab in loaded[1:]:
            for host in lab.hosts.values():
                if isinstance(host, RemoteHost):
                    host._lab = merged  # noqa: SLF001 — same backlink repair Lab.__add__ performs
            for host_id, host in lab.hosts.items():
                if host_id in merged.hosts:
                    logger.warning(
                        "host %r in lab %r: %s overrides %s",
                        host_id,
                        name,
                        label,
                        won_by[host_id],
                    )
                merged.hosts[host_id] = host
                won_by[host_id] = label
            by_id = {link.id: link for link in merged.links}
            by_id.update({link.id: link for link in lab.links})
            merged.links = list(by_id.values())
        merged.resources = self._winning_resources(merged) | extras
        merged._assign_logical_indices()  # noqa: SLF001 — every lab-producing path restamps
        return merged

    @staticmethod
    def _winning_resources(merged: "Lab") -> set[str]:
        """Union of the resources carried by the hosts that WON the merge (spec §6.1).

        Recomputed, never unioned across sources: a plain union would keep a
        resource name an override deliberately dropped, and the reservation
        gate would then demand a resource that no longer exists.
        """
        winners: set[str] = set()
        for host in merged.hosts.values():
            winners |= set(getattr(host, "resources", ()) or ())
        return winners

    @staticmethod
    def _lab_level_extras(source_labs: "list[Lab]") -> set[str]:
        """Resources a backend attached to the LAB beyond its own hosts' (spec §6.1).

        They belong to the lab, not to any overridable host record, so they
        survive the merge. MUST be computed BEFORE the host merge: the merged
        lab aliases the first source's ``Lab`` and hosts are replaced in it in
        place, so afterwards that lab no longer reports the records its own
        ``resources`` were derived from — and an overridden host's resource
        would resurface here as a lab-level "extra", exactly the resurrection
        recomputing resources exists to prevent.
        """
        extras: set[str] = set()
        for lab in source_labs:
            host_derived: set[str] = set()
            for host in lab.hosts.values():
                host_derived |= set(getattr(host, "resources", ()) or ())
            extras |= lab.resources - host_derived
        return extras

    def list_labs(self) -> list[str]:
        """Sorted union across sources; a backend error propagates (spec §6.2)."""
        names: set[str] = set()
        for source in self.sources:
            names.update(source.repository.list_labs())
        return sorted(names)

    def list_host_summaries(self) -> list[HostSummary]:
        """Union by host id, later source wins, memberships unioned (spec §6.3).

        Best-effort by contract: this feeds shell completion, which must never
        crash or warn into the user's TAB — a failing source is skipped with a
        debug log, unlike ``load_lab``/``list_labs`` which stay loud.
        """
        from . import host_summaries  # lazy: package __init__ imports this module's siblings

        by_id: dict[str, HostSummary] = {}
        for source in self.sources:
            try:
                summaries = host_summaries(source.repository)
            except Exception as e:  # noqa: BLE001 — completion path, best-effort by contract
                logger.debug(f"skipping source {source.label} while summarizing hosts: {e}")
                continue
            for s in summaries:
                existing = by_id.get(s.id)
                labs = (
                    list(s.labs)
                    if existing is None
                    else existing.labs + [lab for lab in s.labs if lab not in existing.labs]
                )
                by_id[s.id] = HostSummary(
                    id=s.id,
                    labs=labs,
                    ip=s.ip,
                    element=s.element,
                    element_id=s.element_id,
                    docker_capable=s.docker_capable,
                )
        return sorted(by_id.values(), key=lambda s: s.id)
