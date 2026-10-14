"""The seams a unit test uses to hand otto its repos: the composition root and its catalog.

``otto.bootstrap.get_repos`` and ``get_ordered_repos`` read ``bootstrap()`` at
call time, and so does a context built without ``bootstrap=`` (on its first
read of ``repos``, ``ordered_repos`` or its scope verdicts). Patching
``bootstrap`` itself is therefore the one patch every reader of the root
sees, with one result's repos, walk order and errors. A test that builds its
own context hands it ``bootstrap=fake_bootstrap_result(...)`` instead.

Below the root, ``otto.declared`` reads the composed-repos catalog the root
publishes. ``scope_for_repo`` takes the complete published result, else asks
the initiator, which calls ``bootstrap()``, so a patched ``bootstrap`` reaches
it. ``declared_for_host`` reads only what is published and never composes: a
test that hands a lab load its declared entries publishes its repos with
:func:`publish_repos`.
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


def publish_repos(repos, *, ordered=None) -> BootstrapResult:
    """Publish a result holding *repos* in the catalog, as a finished composition would; return it.

    *ordered* defaults to *repos* (nothing dependency-skipped). The root
    conftest's ``_restore_composition_state`` withdraws it after the test.
    """
    from otto.declared import publish_composed

    result = fake_bootstrap_result(repos, ordered=ordered)
    publish_composed(result, complete=True)
    return result


def install_composed(monkeypatch, result: BootstrapResult) -> BootstrapResult:
    """Make *result* the root's finished composition for this test, and return it.

    Two writes, as a finished ``bootstrap()`` makes them: the root's own cache
    (``otto.bootstrap._result``, which ``bootstrap()`` and ``is_bootstrapped``
    read) and the catalog below it (``publish_composed``, which
    ``declared_for_host`` reads). Setting the cache alone would leave the root
    bootstrapped with nothing published, a state product code never reaches.
    The monkeypatch undoes the cache; the root conftest's
    ``_restore_composition_state`` withdraws the publication.
    """
    from otto import bootstrap as bs
    from otto.declared import publish_composed

    monkeypatch.setattr(bs, "_result", result)
    publish_composed(result, complete=True)
    return result


def fail_composition(monkeypatch) -> None:
    """For this test, publish nothing and make every attempt to compose raise.

    The hostile condition a reader below the root meets when composition
    cannot run: a broken repo or an unreadable environment, as the catalog
    sees it.
    """
    from otto import declared

    def _unavailable():
        raise RuntimeError("composition failed in this process")

    monkeypatch.setattr(declared._composition, "result", None)
    monkeypatch.setattr(declared._composition, "in_progress", None)
    monkeypatch.setattr(declared._composition, "initiator", _unavailable)


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
