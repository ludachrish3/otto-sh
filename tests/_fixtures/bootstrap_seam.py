"""The one seam a unit test patches to hand otto its repos: the composition root.

``otto.bootstrap.get_repos`` and ``get_ordered_repos`` read ``bootstrap()`` at
call time, and so does ``OttoContext.scopes``. Patching ``bootstrap`` itself
is therefore the one patch every reader sees, with one result's repos,
walk order and errors.
"""

from typing import Any, cast

from otto.bootstrap import BootstrapError, BootstrapResult


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
