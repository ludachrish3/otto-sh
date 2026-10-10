"""The one seam a unit test patches to hand otto its repos: the composition root.

``otto.bootstrap.get_repos`` and ``get_ordered_repos`` read ``bootstrap()`` at
call time, and so does a context built without ``bootstrap=`` (on its first
read of ``repos``, ``ordered_repos`` or its scope verdicts). Patching
``bootstrap`` itself is therefore the one patch every reader sees, with one
result's repos, walk order and errors. A test that builds its own context
hands it ``bootstrap=fake_bootstrap_result(...)`` instead.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from otto.bootstrap import BootstrapError, BootstrapResult

if TYPE_CHECKING:
    from otto.config.scope import ProjectScope
    from otto.context import OttoContext


def fake_bootstrap_result(repos, *, ordered=None, errors=None) -> BootstrapResult:
    """A ``BootstrapResult`` holding *repos*; *ordered* defaults to *repos* (nothing skipped)."""
    return BootstrapResult(
        env=cast("Any", None),  # no reader of these results reads env
        repos=list(repos),
        errors=list(cast("list[BootstrapError]", errors or [])),
        ordered_repos=list(repos if ordered is None else ordered),
    )


def patch_bootstrap(monkeypatch, repos, *, ordered=None, errors=None) -> BootstrapResult:
    """Make ``otto.bootstrap.bootstrap()`` return one fake result, and return it."""
    result = fake_bootstrap_result(repos, ordered=ordered, errors=errors)
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: result)
    return result


def seed_scope_verdicts(ctx: "OttoContext", verdicts: "Mapping[str, ProjectScope]") -> None:
    """Make *verdicts* what ``otto.config.scope.scopes_of(ctx)`` returns, without resolving any.

    For a test that hands a context ready verdicts: it seeds the context's
    private cache, as a resolved read would have filled it.
    """
    ctx._scope_verdicts = dict(verdicts)  # a test seeding the context's own cache
    ctx._scopes_refusal = None
