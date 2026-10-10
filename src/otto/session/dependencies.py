"""The dependency preflight: an active repo's unmet Python requirement refuses the run."""

from typing import TYPE_CHECKING

from .errors import DependencyRefusedError

if TYPE_CHECKING:
    from ..context import OttoContext


def check_dependencies(ctx: "OttoContext") -> list[str]:
    """Refuse when an ACTIVE repo declares a Python requirement this environment lacks.

    Runs once *ctx* exists with its scopes populated (it is passed explicitly,
    not read from the contextvar), because severity is an activation
    question and :func:`otto.config.scope.active`, the one authority, needs
    the lab's scope verdicts. The pre-lab projection would refuse a
    host-starved repo, which is simply inactive here. An eager import
    failure never reaches this check (``bootstrap()`` contained it, and
    :func:`~otto.session.check_repos` reported it). This check catches the lazy shape: an
    import inside an instruction body. The repos checked are the run's, read
    off *ctx* (:attr:`~otto.context.OttoContext.ordered_repos`), never a
    second read of the composition root.

    Returns the warnings: checks the preflight could not make, then each
    inactive repo's unmet requirement. Raises
    :class:`~otto.session.DependencyRefusedError` carrying the blocking
    requirements and those same warnings.
    """
    from ..config.scope import active
    from ..env.preflight import preflight

    result = preflight(ctx.ordered_repos)
    warnings = [str(w) for w in result.warnings]
    blocking = []
    for bad in result.unsatisfied:
        if active(bad.repo, ctx):
            blocking.append(bad)
            continue
        warnings.append(
            f"repo {bad.repo!r} requires {bad.requirement!r} — not satisfied in this "
            f"environment (found: {bad.found}), but {bad.repo} is inactive for this run "
            "— continuing without it"
        )
    if blocking:
        raise DependencyRefusedError(blocking, warnings)
    return warnings
