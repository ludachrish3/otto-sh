"""Lab-state vocabulary for project-level actions (pure data, import-light).

TWO AXES, AND THEY ARE NOT THE SAME QUESTION. :class:`InstallState` answers
"are this lab's PRODUCTS on it?"; :class:`Cleanliness` answers "is anything
``cleanup`` removes still there?" — dev tools, toolchain tools, netem
impairments, otto tunnels. A lab can be fully INSTALLED and filthy, or fully
UNINSTALLED and still wearing a tunnel, which is why neither axis is derived
from the other.

Deliberately free of otto imports: every layer above — per-repo actions, the
orchestrator, instructions, fixtures — reads this vocabulary, and a leaf with no
edges can be imported from any of them without a cycle to hold apart.

>>> from otto.project.state import InstallState, ProjectStatus
>>> status = ProjectStatus(overall=InstallState.PARTIAL, repos={"app": InstallState.PARTIAL})
>>> status.overall
<InstallState.PARTIAL: 'partial'>
>>> ProjectStatus(overall=InstallState.UNINSTALLED).repos
{}
"""

import enum
from dataclasses import dataclass, field


class InstallState(enum.Enum):
    """Tri-state install answer for one repo or for the lab as a whole.

    THE MIDDLE MEMBER IS WHY THIS IS NOT A BOOLEAN. A half-installed lab and a
    clean one are the same answer to ``is_installed()`` -- False -- and acting
    on that answer reinstalls on top of remnants. ``PARTIAL`` is the
    error-recovery signal ``ensure_installed(recover_partial=True)`` keys on:
    tear down first, then install fresh.
    """

    INSTALLED = "installed"
    PARTIAL = "partial"
    UNINSTALLED = "uninstalled"


@dataclass
class ProjectStatus:
    """Lab-level aggregate plus the per-repo detail behind it.

    Both halves are reported because they answer different questions: the
    aggregate decides whether a fixture converges, while the per-repo map says
    WHICH repo is the odd one out. A repo that is not counted (see the
    orchestrator's counted-repo rule) is absent from :attr:`repos` entirely
    rather than present with a made-up state.
    """

    overall: InstallState
    """The lab's answer across every counted repo."""

    repos: "dict[str, InstallState]" = field(default_factory=dict)
    """Per-repo state, keyed by repo name; counted repos only."""


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

    REPO = "repo"
    """One repo's own products and dev tools (``ProjectActions.is_clean``)."""

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
    """Repo name, host id, link id, or tunnel id — whatever the row is about."""

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
    order ``cleanup`` performs them — repos, toolchain tools, impairments,
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
