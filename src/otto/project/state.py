"""Lab-state vocabulary for project-level actions (pure data, import-light).

TWO AXES, AND THEY ARE NOT THE SAME QUESTION. :class:`InstallState` answers
"are this lab's PRODUCTS on it?"; :class:`Cleanliness` answers "is anything
``cleanup`` removes still there?" — dev tools, toolchain tools, netem
impairments, otto tunnels. A lab can be fully INSTALLED and filthy, or fully
UNINSTALLED and still wearing a tunnel, which is why neither axis is derived
from the other.

Deliberately free of otto imports: every layer above — per-project actions, the
orchestrator, instructions, fixtures — reads this vocabulary, and a leaf with no
edges can be imported from any of them without a cycle to hold apart.

>>> from otto.project.state import InstallState, ProjectStatus
>>> status = ProjectStatus(overall=InstallState.PARTIAL, projects={"app": InstallState.PARTIAL})
>>> status.overall
<InstallState.PARTIAL: 'partial'>
>>> ProjectStatus(overall=InstallState.UNINSTALLED).projects
{}
"""

import enum
from dataclasses import dataclass, field


class InstallState(enum.Enum):
    """Tri-state install answer for one project or for the lab as a whole.

    THE MIDDLE MEMBER IS WHY THIS IS NOT A BOOLEAN. A half-installed lab and a
    clean one are the same answer to ``is_installed()`` -- False -- and acting
    on that answer reinstalls on top of remnants. ``PARTIAL`` is the
    error-recovery signal ``ensure_installed(recover_partial=True)`` keys on:
    tear down first, then install fresh.
    """

    INSTALLED = "installed"
    PARTIAL = "partial"
    UNINSTALLED = "uninstalled"


@dataclass(frozen=True)
class ProjectScopeSummary:
    """What per-project scoping decided about one project, in display terms.

    The rendering projection of :class:`otto.config.scope.ProjectScope` --
    sorted, stringly, and without the compiled patterns -- so that a status
    row can be printed from the report alone. It is a projection rather than
    the verdict itself for two reasons. This module is deliberately free of
    otto imports (see the module docstring). And the verdict's ``universe`` is
    a resolve-time snapshot that a live fleet walk must never iterate: a
    display type, reachable only from a finished report, is one no walk can
    pick it up from by accident.

    >>> from otto.project.state import ProjectScopeSummary
    >>> ProjectScopeSummary(applicable=False, loaded_labs=("bench", "floor")).universe
    ()
    """

    applicable: bool
    """Whether any loaded lab applies to this project.

    False is D3's exclusion -- the project DECLARED labs and none of them was
    loaded -- which is why it is reported rather than implied by an empty
    :attr:`applicable_labs`. An undeclared project applies to everything (the
    whole-lab fallback), so silence and exclusion must not render alike.
    """

    usable: bool = True
    """Whether this run's walks acted on the project at all -- D3's skip, either way.

    NOT a synonym for :attr:`applicable`, and the pair is what makes the two
    skipped rows tellable apart. A project can apply to a loaded lab and still be
    left out, because its ``host_patterns`` match no host there; its fleet is
    as empty as an excluded project's, but its ``-l`` and its ``lab_patterns`` are
    right and a row blaming the labs would send the reader to change them. So
    ``applicable and not usable`` is exactly the host-starved case, and
    ``not applicable`` is exactly the excluded one.

    Computed once, from :func:`~otto.config.scope.unusable_scope` -- the same
    predicate the walks skip on and the current project aborts on -- rather than
    re-derived here from the other fields, which would put D3's condition in a
    third place.
    """

    loaded_labs: "tuple[str, ...]" = ()
    """Every lab this run loaded, in load order -- the "not applicable to WHAT" half.

    Carried even when the project is applicable, because the row that needs it
    most is the one that has nothing else to say: "not applicable" without the
    labs reads as a bug in otto rather than as the wrong ``-l``.
    """

    applicable_labs: "tuple[str, ...]" = ()
    """The loaded labs this project does apply to, sorted (empty when it applies to none)."""

    universe: "tuple[str, ...]" = ()
    """Host ids the project targeted AT RESOLVE TIME, sorted -- display only.

    A snapshot, never a walk list: fleet iteration re-derives membership per
    host so a container that joins after resolution is scoped correctly. It is
    here because ``status --full`` is where an operator asks "which hosts does
    this project even mean", and answering that from anything else would mean a
    second membership rule.
    """

    host_patterns: "tuple[str, ...]" = ()
    """The declared ``host_patterns``, as written (empty when undeclared).

    Carried for the row that has nothing else to say: a project left out because
    its patterns matched no host is unactionable without the patterns that
    did not match, exactly as an excluded project's row is unactionable without
    the loaded labs.
    """

    skipped: bool = False
    """Whether the dependency pass left this project out of the run, so no walk acted on it.

    The project parsed, so its declaration still counts toward the fleet of
    interest, but it registered nothing. Derived by the orchestrator: the project
    is among the parsed projects and absent from the walk order. Its row is shown
    so that a declaration narrowing the fleet is never invisible in the display
    that explains the fleet.
    """


@dataclass
class ProjectStatus:
    """Lab-level aggregate plus the per-project detail behind it.

    Both halves are reported because they answer different questions: the
    aggregate decides whether a fixture converges, while the per-project map says
    WHICH project is the odd one out. A project that is not counted (see the
    orchestrator's counted-project rule) is absent from :attr:`projects` entirely
    rather than present with a made-up state.
    """

    overall: InstallState
    """The lab's answer across every counted project."""

    projects: "dict[str, InstallState]" = field(default_factory=dict)
    """Per-project state, keyed by project name; counted and applicable projects only."""

    scoping: "dict[str, ProjectScopeSummary]" = field(default_factory=dict)
    """Per-project scoping verdict, keyed by project name, in ``OTTOPATH`` order.

    A SEPARATE MAPPING FROM :attr:`projects`, not a richer value in it, because a
    project this run does not apply to has no install state at all -- otto never
    asked it, and asking would have contacted hosts outside its fleet of
    interest. Folding the two would force either a fabricated state or an
    ``InstallState | None`` that every existing reader would have to learn.

    Every parsed project has a row, a project the dependency pass skipped included
    (marked :attr:`ProjectScopeSummary.skipped`), because a skipped project's declaration
    still narrows the fleet.

    Empty ONLY when nothing was resolved -- a library context, a run with no
    project roots. Not the whole-lab fallback, which is a different thing and does
    populate this: an undeclared project still gets a verdict (it applies to every
    loaded lab and targets every host), so a lab where no project declared
    ``[project]`` reports a row per project rather than nothing at all.
    """


class Cleanliness(enum.Enum):
    """Tri-state cleanliness answer for one thing ``cleanup`` would take off.

    THE THIRD MEMBER BELONGS TO THE DISPLAY, and it is the whole reason this is
    not a boolean. :func:`otto.project.orchestrator.is_clean` may never answer UNKNOWN — a
    converge decision made on a state nobody read is the fabrication the
    dry-run contract exists to prevent, so it raises instead. A *report* has
    the opposite duty: ``otto run status --full`` must render every fact the
    lab surrendered and MARK the rest, because a display that dies on one
    unreachable host shows nothing about the twelve that answered.
    """

    CLEAN = "clean"
    DIRTY = "dirty"
    UNKNOWN = "unknown"


class CleanlinessKind(enum.Enum):
    """Which of ``cleanup``'s steps a :class:`CleanlinessItem` answers for.

    The values are slugs, not sentences: the wording of a section heading is
    the renderer's business, and two surfaces already want different lengths of
    it.
    """

    PROJECT = "project"
    """One project's own products and dev tools (``ProjectActions.is_clean``)."""

    TOOLCHAIN = "toolchain"
    """One host's toolchain tools — host-global, so named by host id."""

    IMPAIRMENT = "impairment"
    """One lab link's netem state, named by link id."""

    TUNNEL = "tunnel"
    """One discovered otto tunnel, or the scan itself when it fell short."""


@dataclass(frozen=True)
class CleanlinessItem:
    """One named thing ``cleanup`` acts on, and what could be learned about it."""

    kind: CleanlinessKind
    """Which cleanup step this row answers for."""

    name: str
    """Project name, host id, link id, or tunnel id — whatever the row is about."""

    state: Cleanliness
    """What was learned. UNKNOWN means nobody managed to look."""

    detail: str = ""
    """A short phrase for the renderer to put beside :attr:`state`.

    Never a repeat of :attr:`name` — the row already carries it — and never
    the only place a reason appears when :attr:`error` also holds one.
    """

    error: "BaseException | None" = None
    """The refusal :func:`otto.project.orchestrator.is_clean` raises for an UNKNOWN row.

    HELD RATHER THAN THROWN, because the two consumers of these items have
    opposite duties on a non-fact: the boolean must refuse to answer (a
    converge would otherwise cleanup on something nobody measured), while the
    display must print the row and carry on. Carrying the exception here is
    what lets ONE probe serve both — the alternative is a second copy of the
    presence logic, which is exactly the mirrored code this package's split
    surfaces exist to prevent.

    Its type is the answer, not decoration: an unreachable host, a ``tc`` that
    is not installed and a dry run's declined read are three different classes,
    and re-raising the real one is what sends the operator to the right place.
    """

    def __post_init__(self) -> None:
        """Pin UNKNOWN and :attr:`error` to each other, in both directions.

        An UNKNOWN row with no error would make :func:`otto.project.orchestrator.is_clean`
        choose between fabricating an answer and raising something it invented;
        an error on a CLEAN or DIRTY row is a measurement contradicting itself.
        Neither is a shape any builder should be able to hand out.
        """
        if (self.state is Cleanliness.UNKNOWN) != (self.error is not None):
            raise ValueError(
                f"cleanliness row {self.name!r}: UNKNOWN carries the error is_clean would "
                f"raise for it, and no other state carries one (state={self.state.value}, "
                f"error={self.error!r})"
            )


@dataclass
class CleanlinessReport:
    """Every cleanliness row the lab surrendered, in ``cleanup``'s own order.

    Rows are grouped by :attr:`CleanlinessItem.kind` and the groups run in the
    order ``cleanup`` performs them — projects, toolchain tools, impairments,
    tunnels — so a renderer can print a section heading on the row where the
    kind changes without sorting anything first.
    """

    items: "list[CleanlinessItem]" = field(default_factory=list)
    """One row per thing asked; an axis that answered clean still gets a row."""

    @property
    def overall(self) -> Cleanliness:
        """The lab's answer — DIRTY beats UNKNOWN beats CLEAN.

        A DEFINITIVE DIRTY ANSWER IS NEVER DISCARDED for a scan that fell
        short. Once anything has been SEEN, the lab needs cleaning and no host
        that failed to answer can make it clean again — so a report holding one
        dirty row and one unreadable row says "dirty", not "unknown".

        No rows at all is CLEAN: a lab with nothing that could be left over has
        nothing left over. (The install axis reduces an empty lab the other
        way, to UNINSTALLED, for the mirrored reason — nothing that could be
        installed is not installed.)
        """
        states = {item.state for item in self.items}
        if Cleanliness.DIRTY in states:
            return Cleanliness.DIRTY
        if Cleanliness.UNKNOWN in states:
            return Cleanliness.UNKNOWN
        return Cleanliness.CLEAN


def combine_install_states(results: "dict[str, InstallState]") -> InstallState:
    """Fold each counted project's state into the lab's: the ``status`` combiner.

    Zero counted projects is UNINSTALLED, not a vacuous INSTALLED. All INSTALLED
    is INSTALLED, all UNINSTALLED is UNINSTALLED, anything mixed -- or any
    PARTIAL -- is PARTIAL.

    THE CHECK ORDER IS LOAD-BEARING: ``all()`` is vacuously true over an empty
    sequence, so whichever of the two ``all()`` arms is asked first is the
    answer for a lab with no counted projects. UNINSTALLED first IS the zero-project
    rule -- swapping the arms leaves every other case unchanged and silently
    reports an empty lab as fully installed.
    """
    states = list(results.values())
    if all(state is InstallState.UNINSTALLED for state in states):
        return InstallState.UNINSTALLED
    if all(state is InstallState.INSTALLED for state in states):
        return InstallState.INSTALLED
    return InstallState.PARTIAL
