"""Whether a project-owned instruction may run in this context."""

from typing import TYPE_CHECKING

from .errors import InstructionInactiveError

if TYPE_CHECKING:
    from ..run_context import OttoContext


def check_instruction_active(*, name: str, owner: "str | None", ctx: "OttoContext") -> None:
    """Refuse instruction *name* when its owning project is inactive in *ctx*.

    *owner* is ``INSTRUCTIONS.project(name)``, the project whose init import
    registered the instruction. ``None`` (otto's own, a project instruction,
    or one registered outside any project's init import) is never refused. The verdict is
    :func:`otto.config.scope.active`'s, so it needs the lab's scope verdicts:
    call this after the context is installed.
    """
    if owner is None:
        return
    from ..config.scope import inactive_reason

    reason = inactive_reason(owner, ctx)
    if reason is not None:
        raise InstructionInactiveError.from_record(instruction=name, owner=owner, reason=reason)
