"""Real :class:`~otto.config.scope.ProjectScope` verdicts, built once for several modules.

More than one test module needs a resolver verdict to hand to the activation
predicates, and each of them wants the SAME construction: the fully-populated
frozen dataclass, never a stub. A ``SimpleNamespace`` stand-in would let
``excluded`` and ``universe`` disagree — precisely the pair
:func:`~otto.config.scope.unusable_scope` reads — and would then happily
certify a predicate that consulted the wrong one. Sharing the factory here is
what keeps those modules from re-rolling the dataclass, each with its own
silent drift.
"""

from otto.config.scope import ProjectScope


def verdict(name, *, declared=True, excluded=False, universe=("h0",), lab_patterns=None):
    """A real resolver verdict — excluded empties the applicability fields.

    *lab_patterns* defaults to the single pattern ``<name>-lab``, which is what
    every caller before it existed got. Pass a longer tuple for a repo that
    declares SEVERAL — the ordinary case in a multi-lab tree, and the one that
    makes a rendered ``-l a / -l b / -l c`` hint long enough to reach a
    terminal's wrap column. A default that can only ever produce one pattern
    silently caps how long any message built from it can be, which is enough
    to make a width assertion untriggerable.
    """
    return ProjectScope(
        repo_name=name,
        declared=declared,
        config=None,
        applicable_labs=frozenset() if excluded else frozenset({"bench"}),
        universe=frozenset() if excluded else frozenset(universe),
        excluded=excluded,
        sut_dir=f"/repos/{name}",
        loaded_labs=("bench",),
        lab_patterns=(f"{name}-lab",) if lab_patterns is None else tuple(lab_patterns),
        host_patterns=(".*",),
    )
