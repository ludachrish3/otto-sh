"""The ``ensure`` marker converges from the dispatched verb's options (spec §4.5).

The marker's converge builds each install body's options from the option
values in force: the ``test`` command's recorded flags, where only a typed
flag reaches a class the command did not offer, with every ``use_options``
block around the fixture laid over them. A flag registered only for ``run``
is not among them, so it takes its default.
"""

import pytest

from otto import options
from otto.context import get_context
from otto.params import register_options
from otto.project import InstallOptions, InstallState, orchestrator
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
    get_context().bind_verb_options(verb="run", kwargs={"variant": "lab"})
    get_context().bind_verb_options(verb="test", kwargs={"lab_env": "prod"})
    built: list[WidgetInstall] = []

    async def fake_ensure_installed(*, opts):
        built.append(opts.build(WidgetInstall))
        return Result(status=Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    [opts] = built
    assert opts.lab_env == "prod"
    assert opts.variant == "field"


def test_nothing_bound_converges_from_defaults(pytester, otto_plugins, monkeypatch) -> None:
    """A context that never dispatched a verb still converges: every body takes its defaults."""
    built: list[WidgetInstall] = []

    async def fake_ensure_installed(*, opts):
        built.append(opts.build(WidgetInstall))
        return Result(status=Status.Success)

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
    # outside a project's init import, register_project_actions refuses the
    # class, so it is stood in for by the identity.
    monkeypatch.setattr(actions_module, "register_project_actions", lambda cls: cls)
    actions = importlib.import_module("gs_example.actions")
    get_context().bind_verb_options(verb="test", kwargs={"variant": "lab"})
    assert get_context().options(actions.BedVariant).variant == "lab"
    built = []

    async def fake_ensure_installed(*, opts):
        built.append(opts.build(actions.BedInstall))
        return Result(status=Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    [opts] = built
    assert (opts.variant, opts.ensure) == ("lab", False)


@options
class GuardedInstall(InstallOptions):
    """The second project's install class: it rejects the ``test`` flag's value."""

    lab_env: str = "staging"

    def __post_init__(self) -> None:
        if self.lab_env == "bad":
            raise ValueError("lab_env must not be bad")


def test_a_later_projects_bad_value_is_a_setup_error_before_any_body(
    pytester, otto_plugins, monkeypatch
) -> None:
    """Both projects' install classes are built before the first install body runs."""
    from types import SimpleNamespace

    from otto.project.state import ProjectStatus

    register_options(LabEnvOptions, verbs=["test"])
    get_context().bind_verb_options(verb="test", kwargs={"lab_env": "bad"})
    ran: list[str] = []

    async def first_install(opts):
        del opts
        ran.append("a")
        return Result(status=Status.Success)

    async def second_install(opts):
        del opts
        ran.append("b")
        return Result(status=Status.Success)

    def body(cls):
        return SimpleNamespace(method_name="install", options_cls=cls)

    walk = [
        (SimpleNamespace(name="a"), SimpleNamespace(install=first_install), body(InstallOptions)),
        (SimpleNamespace(name="b"), SimpleNamespace(install=second_install), body(GuardedInstall)),
    ]

    async def uninstalled(*, opts):
        del opts
        return ProjectStatus(overall=InstallState.UNINSTALLED, projects={}, scoping={})

    monkeypatch.setattr(orchestrator, "status", uninstalled)
    monkeypatch.setattr(orchestrator, "_lab", lambda: (get_context(), []))
    monkeypatch.setattr(orchestrator, "_walk_order", lambda *_a, **_k: walk)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(errors=1)
    assert ran == []


@options
class DebugInstall(InstallOptions):
    """Registered for ``test``: ``otto test --debug-level 3`` sets it, and it is install's class."""

    debug_level: int = 0


def test_an_ensured_install_hook_reads_the_test_flag_and_the_bodys_own_instance(
    pytester, otto_plugins, monkeypatch
) -> None:
    """The real converge hands ``--debug-level 3`` to the install body and its hook.

    The options guide promises it: a test marked ``ensure("installed")`` run
    with ``--debug-level 3`` installs the product with that value. The hook
    reads it with ``otto.get_options``, and gets the body's own ``opts``.
    """
    from types import SimpleNamespace

    import otto
    from otto.project.state import ProjectStatus

    register_options(DebugInstall, verbs=["test"])
    get_context().bind_verb_options(verb="test", kwargs={"debug_level": 3})
    seen: list[tuple[int, bool]] = []

    def hook(opts) -> None:
        read = otto.get_options(DebugInstall)
        seen.append((read.debug_level, read is opts))

    async def install(opts):
        hook(opts)
        return Result(status=Status.Success)

    walk = [
        (
            SimpleNamespace(name="a"),
            SimpleNamespace(install=install),
            SimpleNamespace(method_name="install", options_cls=DebugInstall),
        )
    ]

    async def uninstalled(*, opts):
        del opts
        return ProjectStatus(overall=InstallState.UNINSTALLED, projects={}, scoping={})

    monkeypatch.setattr(orchestrator, "status", uninstalled)
    monkeypatch.setattr(orchestrator, "_lab", lambda: (get_context(), []))
    monkeypatch.setattr(orchestrator, "_walk_order", lambda *_a, **_k: walk)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    assert seen == [(3, True)]


@options
class LabEnv2:
    """Offered by the recorded ``test`` command: its parsed value is ``lab-default``."""

    lab_env: str = "lab-default"


@pytest.mark.parametrize(("given", "expected"), [((), "staging"), (("lab_env",), "lab-default")])
def test_an_unset_test_flag_does_not_reach_an_install_bodys_field_and_a_typed_one_does(
    pytester, otto_plugins, monkeypatch, given, expected
) -> None:
    """``WidgetInstall`` is not offered: it takes a recorded value only when it was typed."""
    from otto.params import RecordedOptions
    from otto.run_context import record_options

    record_options(
        get_context(),
        RecordedOptions(
            group="test",
            parsed={"lab_env": "lab-default"},
            given=frozenset(given),
            offered=(LabEnv2,),
        ),
    )
    built: list[WidgetInstall] = []

    async def fake_ensure_installed(*, opts):
        built.append(opts.build(WidgetInstall))
        return Result(status=Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    run_inner(pytester, otto_plugins, test_e=_ENSURED_TEST).assert_outcomes(passed=1)
    assert [o.lab_env for o in built] == [expected]
