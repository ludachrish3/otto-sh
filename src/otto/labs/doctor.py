"""Lab-file doctor warnings: advisory checks that need every declaration in view.

Pure — takes parsed documents, returns strings — so ``otto init`` (the only
caller) stays the single place that reads a repo's lab files. Warnings, not
problems: a shared file may serve projects that declare different labs, so a
dead pattern is advice, and the disjoint-resource overlap (spec 2026-08-27
lab-definition-v2 §8.3) is the author's call to make once it is pointed out.

Every check here needs the WHOLE picture — a pattern is only dead once no
file declares a lab it matches, and two labs only fail to contend once both
their declarations are in hand — which is why they live in the doctor and not
in the per-file parsers.
"""

import re

from ..models.lab import ElementSpec, LabEntrySpec

Documents = list[tuple[str, dict[str, LabEntrySpec], list[ElementSpec]]]
"""``(source, labs table, elements)`` per file."""


def lab_warnings(documents: Documents) -> list[str]:
    """Return every advisory finding across *documents*, in report order.

    Dead membership patterns first (per file, per element, per pattern), then
    one finding per pair of labs that share an element, BOTH declare at least
    one resource, and declare no resource in common. An empty list means the
    declarations are coherent; nothing here ever makes a repo invalid.
    """
    declared: dict[str, LabEntrySpec] = {}
    for _, entries, _ in documents:
        declared.update(entries)
    names = sorted(declared)
    out: list[str] = []
    members: dict[str, list[str]] = {n: [] for n in names}
    for source, _, elements in documents:
        for element in elements:
            out.extend(_dead_patterns(source, element, names))
            for name in names:
                if element.matches(name):
                    members[name].append(element.name)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            shared = sorted(set(members[first]) & set(members[second]))
            if shared and _cannot_contend(declared[first], declared[second]):
                out.append(_disjoint_resources(first, second, shared))
    return out


def _cannot_contend(first: LabEntrySpec, second: LabEntrySpec) -> bool:
    """Whether reserving one of two labs would leave the other free.

    Only a pair that BOTH reserve something can fail to contend. A lab with an
    empty ``resources`` declares that it reserves nothing — a legal, deliberate
    declaration (it is what the README ``otto init`` scaffolds calls "a
    perfectly good declaration") — so there is no reservation for a shared
    element to protect and nothing to warn about. Without this guard
    ``set().isdisjoint(set())`` is ``True`` and N resource-less labs sharing an
    element emit N(N-1)/2 warnings advising a fix for a problem they do not
    have.
    """
    return bool(first.resources and second.resources) and first.resources.isdisjoint(
        second.resources
    )


def _dead_patterns(source: str, element: ElementSpec, names: list[str]) -> list[str]:
    """Return one warning per *element* pattern that no declared lab fullmatches.

    Names the file, the element and the pattern (spec §9): a pattern is the
    only thing the author can act on, and the declared set tells them whether
    they mistyped it or forgot the ``labs`` entry.

    ``fullmatch`` mirrors :meth:`otto.models.lab.ElementSpec.matches` and must
    keep mirroring it: a check any looser would stay silent about a pattern
    that joins nothing at load (``"uni"`` against a declared ``"unix"``).
    """
    return [
        f"{source}: element {element.name!r} labs pattern {pattern!r} matches no "
        f"declared lab (declared: {names or 'none'})"
        for pattern in element.labs
        if not any(re.fullmatch(pattern, name) for name in names)
    ]


def _disjoint_resources(first: str, second: str, shared: list[str]) -> str:
    """Return the overlap warning for two labs that share elements but no resource.

    On v1 two labs sharing a host shared that host's resources automatically,
    so reserving either contended correctly. Lab-level resources are declared,
    never derived (spec §8.1), so that safety net is gone and this is the
    check that replaces it.
    """
    return (
        f"labs {first!r} and {second!r} share element(s) {shared} but declare "
        f"disjoint resources — reserving one will not contend with the other; "
        f"declare a shared resource identifier, or make one a sub-lab of the "
        f"other (e.g. {first}.{second})"
    )
