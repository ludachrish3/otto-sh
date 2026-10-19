"""The seams a unit test uses to hand otto its projects: the composition root and its catalog.

``otto.declared.get_projects`` and ``get_ordered_projects`` take the complete
published result, else the in-progress one, else ask the initiator, which calls
``bootstrap()`` at call time, and a context built without ``bootstrap=`` calls
it (on its first read of ``projects``, ``ordered_projects`` or its scope verdicts).
A patched ``bootstrap`` is therefore reached only when nothing is published: a
result already published bypasses it. A test that builds its own context hands
it ``bootstrap=fake_bootstrap_result(...)`` instead.

Below the root, ``otto.declared`` reads the composed-projects catalog the root
publishes. ``scope_for_project`` reads in the same order (complete result,
in-progress result, initiator), so a patched ``bootstrap`` reaches it only
when nothing is published. ``declared_for_host`` reads only what is published and never composes: a
test that hands a lab load its declared entries publishes its projects with
:func:`publish_projects`.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from otto.bootstrap import BootstrapError, BootstrapResult

if TYPE_CHECKING:
    from otto.config.scope import ProjectScope
    from otto.context import OttoContext


def fake_bootstrap_result(projects, *, ordered=None, errors=None) -> BootstrapResult:
    """A ``BootstrapResult`` holding *projects*; *ordered* defaults to them (nothing skipped)."""
    return BootstrapResult(
        env=cast("Any", None),  # no reader of these results reads env
        projects=list(projects),
        errors=list(cast("list[BootstrapError]", errors or [])),
        ordered_projects=list(projects if ordered is None else ordered),
    )


def publish_projects(projects, *, ordered=None) -> BootstrapResult:
    """Publish a result holding *projects* in the catalog, as a composition would; return it.

    *ordered* defaults to *projects* (nothing dependency-skipped). The root
    conftest's ``_restore_composition_state`` withdraws it after the test.
    """
    from otto.declared import publish_composed

    result = fake_bootstrap_result(projects, ordered=ordered)
    publish_composed(result, complete=True)
    return result


def install_composed(monkeypatch, result: BootstrapResult) -> BootstrapResult:
    """Make *result* the root's finished composition for this test, and return it.

    One write, as a finished ``bootstrap()`` makes it: the complete result on
    the catalog's record in ``otto.declared``, which ``bootstrap()``,
    ``is_bootstrapped``, ``get_projects`` and ``declared_for_host`` all read. The
    monkeypatch undoes it.
    """
    from otto import declared

    monkeypatch.setattr(declared._composition, "result", result)
    monkeypatch.setattr(declared._composition, "in_progress", None)
    return result


def fail_composition(monkeypatch) -> None:
    """For this test, publish nothing and make every attempt to compose raise.

    The hostile condition a reader below the root meets when composition
    cannot run: a broken project or an unreadable environment, as the catalog
    sees it.
    """
    from otto import declared

    def _unavailable():
        raise RuntimeError("composition failed in this process")

    monkeypatch.setattr(declared._composition, "result", None)
    monkeypatch.setattr(declared._composition, "in_progress", None)
    monkeypatch.setattr(declared._composition, "initiator", _unavailable)


def patch_bootstrap(monkeypatch, projects, *, ordered=None, errors=None) -> BootstrapResult:
    """Make ``otto.bootstrap.bootstrap()`` return one fake result, and return it."""
    result = fake_bootstrap_result(projects, ordered=ordered, errors=errors)
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: result)
    return result


def seed_scope_verdicts(ctx: "OttoContext", verdicts: "Mapping[str, ProjectScope]") -> None:
    """Make *verdicts* what ``otto.config.scope.scopes_of(ctx)`` returns, without resolving any.

    For a test that hands a context ready verdicts: it seeds the context's
    private cache, as a resolved read would have filled it.
    """
    ctx._scope_verdicts = dict(verdicts)  # a test seeding the context's own cache
    ctx._scopes_refusal = None
