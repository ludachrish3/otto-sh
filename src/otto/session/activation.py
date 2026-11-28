"""Whether a project-owned instruction or command may run in this context."""

from typing import TYPE_CHECKING

from .errors import InstructionInactiveError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..config.project import Project
    from ..config.scope import InactiveReason, ScopeSelection
    from ..run_context import OttoContext


def owner_refusal(
    owner: "str | None",
    *,
    projects: "Sequence[Project]",
    ctx: "OttoContext | None" = None,
    selection: "ScopeSelection | None" = None,
) -> "InactiveReason | None":
    """Say why *owner*'s instruction or command may not run, or ``None`` when it may.

    The one judgement of a project-owned entry at dispatch. ``None`` for no
    owner (otto's own) and for an owner that takes part. An owner that does
    not take part is refused for its reason, unless a project that does take
    part requires it (:func:`otto.session.projects.required_by_active`).

    *projects* is the full list the run parsed, and participation is decided
    once over it, by :func:`otto.config.scope.participating` with exactly one of
    *ctx* (the lab is built) or *selection* (only ``-l``, ``-I`` and ``-E`` are
    known). That one answer gives both the owner's reason and the projects
    whose requirements count, so a project that is host-starved once the lab
    is built stops exempting what it requires. *owner* may be spelled any way;
    it is matched PEP 503 normalised.
    """
    if owner is None:
        return None
    from ..config.scope import participating
    from ..models.dependencies import normalize_name
    from .projects import required_by_active

    taking_part = participating(projects, ctx=ctx, selection=selection)
    name = normalize_name(owner)
    for dropped in taking_part.dropped:
        if dropped.reason.normalized == name:
            if required_by_active(owner, active=taking_part.kept, projects=projects):
                return None
            return dropped.reason
    return None


def check_instruction_active(*, name: str, owner: "str | None", ctx: "OttoContext") -> None:
    """Refuse instruction *name* when its owning project does not take part in *ctx*.

    *owner* is ``INSTRUCTIONS.project(name)``, the project whose init import
    registered the instruction. ``None`` (otto's own, a project instruction,
    or one registered outside any project's init import) is never refused.
    Neither is an owner that a participating project requires, directly or
    through a chain of required dependencies. The verdict is judged with the
    lab, over every project *ctx* parsed, so it needs the lab's scope
    verdicts: call this after the context is installed.
    """
    refuse_owner(name, owner, projects=lambda: ctx.unscoped.projects, ctx=ctx)


def refuse_owner(
    name: str,
    owner: "str | None",
    *,
    projects: "Callable[[], Sequence[Project]]",
    ctx: "OttoContext | None" = None,
    selection: "ScopeSelection | None" = None,
) -> None:
    """Raise :class:`InstructionInactiveError` for *name* when *owner* may not run.

    The one refusal both dispatch tiers raise, over :func:`owner_refusal`'s
    verdict: with *ctx* once the lab is built, with *selection* before it.
    *projects* returns every parsed project. It is read only when there is
    an owner to judge, so otto's own entries never read the project list.
    """
    if owner is None:
        return
    reason = owner_refusal(owner, projects=projects(), ctx=ctx, selection=selection)
    if reason is not None:
        raise InstructionInactiveError.from_record(instruction=name, owner=owner, reason=reason)
