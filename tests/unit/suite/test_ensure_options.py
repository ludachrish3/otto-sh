"""The ``ensure`` marker converges from the dispatched verb's options (spec §4.5).

An install body can only see flags registered for ``test``: the marker's
converge builds each body's options from the ``test`` verb's parsed flags, as
bound on the context, so a flag registered only for ``run`` takes its default.
"""

from otto import options
from otto.context import get_context
from otto.params import register_options
from otto.project import InstallOptions, orchestrator
from otto.project import actions as actions_module
from otto.result import Result
from otto.utils import Status
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


@options
class LabEnvOptions:
    """Registered for ``test``: ``otto test --lab-env`` sets it."""

    lab_env: str = "staging"


@options
class VariantOptions:
    """Registered for ``run`` only: ``otto test`` has no such flag."""

    variant: str = "field"


@options
class WidgetInstall(InstallOptions):
    """The install body's own options: it reads both flags by name."""

    lab_env: str = "staging"
    variant: str = "field"


_ENSURED_TEST = 'import pytest\n\n@pytest.mark.ensure("installed")\ndef test_x():\n    pass\n'


def test_a_test_flag_steers_the_install_and_a_run_only_flag_takes_its_default(
    pytester, otto_plugins, monkeypatch
) -> None:
    register_options(LabEnvOptions, verbs=["test"])
    register_options(VariantOptions, verbs=["run"])
    # An enclosing `otto run ... --variant lab` bound its own flags first; the
    # test run then binds what `otto test --lab-env prod` parsed. The run-only
    # `variant` must NOT reach the install body from the earlier binding.
    get_context().bind_verb_options("run", {"variant": "lab"})
    get_context().bind_verb_options("test", {"lab_env": "prod"})
    built: list[WidgetInstall] = []

    async def fake_ensure_installed(source):
        built.append(source.build(WidgetInstall))
        return Result(Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    [opts] = built
    assert opts.lab_env == "prod"
    assert opts.variant == "field"


def test_nothing_bound_converges_from_defaults(pytester, otto_plugins, monkeypatch) -> None:
    """A context that never dispatched a verb still converges: every body takes its defaults."""
    built: list[WidgetInstall] = []

    async def fake_ensure_installed(source):
        built.append(source.build(WidgetInstall))
        return Result(Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    assert [(o.lab_env, o.variant) for o in built] == [("staging", "field")]


def test_the_getting_started_variant_reaches_the_ensured_install(
    pytester, otto_plugins, monkeypatch
) -> None:
    """The worked example's shape: ``BedVariant`` is registered, ``BedInstall`` is not.

    ``otto test --variant lab`` binds ``BedVariant`` for ``test``; the ensure
    converge builds ``BedInstall`` (install's own options, which inherits
    ``BedVariant``) from those flags, so the body sees ``lab``.
    """
    import importlib

    from tests._fixtures.gs_example import import_gs_example

    import_gs_example()
    # Only its options classes and its register_options call are under test:
    # outside a repo's init import, register_project_actions refuses the
    # class, so it is stood in for by the identity.
    monkeypatch.setattr(actions_module, "register_project_actions", lambda cls: cls)
    actions = importlib.import_module("gs_example.actions")
    get_context().bind_verb_options("test", {"variant": "lab"})
    assert get_context().options(actions.BedVariant).variant == "lab"
    built = []

    async def fake_ensure_installed(source):
        built.append(source.build(actions.BedInstall))
        return Result(Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    [opts] = built
    assert (opts.variant, opts.ensure) == ("lab", False)
