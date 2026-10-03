"""Whether a repo-owned instruction may run in this context."""

from typing import TYPE_CHECKING

from .errors import InstructionInactiveError

if TYPE_CHECKING:
    from ..context import OttoContext


def check_instruction_active(name: str, owner: "str | None", ctx: "OttoContext") -> None:
    """Refuse instruction *name* when its owning repo is inactive in *ctx*.

    *owner* is the instruction's ``registered_by``. ``None`` (first-party, or
    registered by hand) is never refused. The verdict is
    :func:`otto.config.scope.active`'s, so it needs the lab's scope verdicts:
    call this after the context is installed.
    """
    if owner is None:
        return
    from ..config.scope import active, switched_off

    if active(owner, ctx):
        return
    from ..models.dependencies import normalize_name

    project = normalize_name(owner)
    if switched_off(owner, ctx):
        raise InstructionInactiveError(name, owner, project=project, reason="excluded")
    # Not switched off, yet inactive: `active()` reaches that verdict only by
    # finding an unusable ProjectScope under the repo's declared name, so the
    # lookup cannot miss.
    scope = ctx.scopes[owner]
    raise InstructionInactiveError(
        name,
        owner,
        project=project,
        reason="out_of_lab_scope" if scope.excluded else "host_starved",
        loaded_labs=list(scope.loaded_labs),
        lab_patterns=list(scope.lab_patterns),
        host_patterns=list(scope.host_patterns),
    )
