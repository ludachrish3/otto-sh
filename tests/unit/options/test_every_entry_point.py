"""One install hook reads the same value whichever entry point started the work (§6 item 12).

The product below is the options guide's (``docs/setup/options-classes.md``):
its ``install`` reads ``otto.get_options(Acme).debug_level``. Each entry
point gives ``debug_level=3`` its own way -- a typed flag, an instance handed
to a library call, a binding on a context, a ``use_options`` block -- and the
hook must read 3 every time. Nothing between the entry point and the hook is
a double: the project's own ``install`` body, the real walk, the host's own
``install`` and, for ``otto test``, the real ``ensure`` converge inside a real
in-process pytest session. Only the transport is absent: the product touches
no host.

The registered classes are removed again after each test by the root
conftest's registry isolation.
"""

from typing import Any

import pytest

import otto
from otto.cli.run import instruction, run_app
from otto.context import OttoContext
from otto.host import Host, Product
from otto.instructions import run_instruction
from otto.params import register_options
from otto.project import InstallOptions, InstallState, ProjectActions, register_project_actions
from otto.registry import registering_project
from otto.result import Result
from otto.run_context import reset_context, set_context
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.run_projects import (  # noqa: F401 — fixtures
    _generated_modules_evicted,
    one_project,
    project_root,
)

dispatch = DispatchRunner()

PROJECT = "sut"
"""The one project's name: what ``one_project`` names it, and what owns the product."""


@otto.options
class Acme:
    """The project-wide options: ``--debug-level`` (and ``--variant``) on run and test."""

    debug_level: int = 0
    variant: str = "lab"


@otto.options
class Install(InstallOptions):
    """An install body's own class, offered by no ``otto test`` command.

    Only the ``otto test`` test registers it: under ``otto run install`` its
    fields would collide with ``Acme``'s flags of the same names.
    """

    debug_level: int = 0
    variant: str = "field"


class Agent(Product):
    """The guide's product: its ``install`` reads the debug level, and records it."""

    name = "agent"

    def __init__(self, seen: "list[int]") -> None:
        super().__init__()
        self.seen = seen
        self.owner = PROJECT

    async def stage(self, host: Host) -> Result:
        del host
        return Result(status=Status.Success)

    async def install(self, host: Host) -> Result:
        del host
        self.seen.append(otto.get_options(Acme).debug_level)
        return Result(status=Status.Success)

    async def uninstall(self, host: Host) -> Result:
        del host
        return Result(status=Status.Success)

    async def is_installed(self, host: Host) -> bool:
        del host
        return False


class _Uninstalled(ProjectActions):
    """The project's actions: otto's own install body, and a status of UNINSTALLED.

    The status is what makes an ``ensure`` converge install.
    """

    async def status(self, opts: Any) -> InstallState:
        del opts
        return InstallState.UNINSTALLED


def _register_project() -> None:
    """Register the project's actions with otto's own install body and its ``InstallOptions``."""
    with registering_project(PROJECT):
        register_project_actions(type("Sut", (_Uninstalled,), {}))


def _register_project_with_its_own_install_class() -> "list[Install]":
    """Register the project's actions with an install body under ``Install``.

    The body records the instance it was built and calls otto's own install.
    Returns the list it records into.
    """
    bodies: list[Install] = []

    with registering_project(PROJECT):

        @register_project_actions
        class Sut(_Uninstalled):
            @instruction(options=Install)
            async def install(self, opts: Install) -> Result:
                bodies.append(opts)
                return await super().install(opts)

    return bodies


@pytest.fixture
def acme(make_otto_context):
    """``Acme`` registered for run and test, and a lab whose one host carries the product.

    Returns ``(context, seen)``: the context to run under (not installed), and
    the list the product's install hook appends each read to.
    """
    register_options(Acme, verbs=["run", "test"])
    ctx = make_otto_context("test1")
    seen: list[int] = []
    for host in ctx.lab.hosts.values():
        host.products = [Agent(seen)]
    return ctx, seen


def _wire_project(monkeypatch) -> None:
    """Make the composition root answer the one project, as a lab-backed command sees it."""
    from tests._fixtures.bootstrap_seam import patch_bootstrap
    from tests._fixtures.fake_project import fake_project

    patch_bootstrap(
        monkeypatch, [fake_project(PROJECT, project_scope=None, root=f"/tmp/{PROJECT}")]
    )


_ENSURED_TEST = (
    "import pytest\n\n"
    '@pytest.mark.ensure("installed")\n'
    "class TestAgent:\n"
    "    def test_agent(self):\n"
    "        pass\n"
)


def _otto_test(ctx: OttoContext, tmp_path, args: "list[str]") -> Any:
    """Run ``otto test`` through the production dispatch seam under *ctx*, as ``otto_test_cli``."""
    import dataclasses

    from otto.cli import test as cli_test

    out = tmp_path / "otto-out"
    out.mkdir(exist_ok=True)
    ctx.policy = dataclasses.replace(ctx.policy, output_dir=out)
    token = set_context(ctx)
    try:
        return dispatch.invoke(
            cli_test.test_app,
            ["test", *args],
            spec_name="test",
            under_root=True,
            accepts_options=True,
        )
    finally:
        reset_context(token)


@pytest.mark.parametrize(
    "entry",
    [
        "otto run install",
        "otto test + ensure",
        "run_instruction",
        "otto.project.install",
        "use_options around host.install",
    ],
)
def test_the_install_hook_reads_the_given_value(
    entry, acme, monkeypatch, request, tmp_path
) -> None:
    """Sync, so the two command lines run their own loop as on the real CLI."""
    import asyncio

    ctx, seen = acme
    _register_project()
    if entry == "otto test + ensure":
        request.getfixturevalue("one_project")(files={"tests/test_agent.py": _ENSURED_TEST})
        result = _otto_test(ctx, tmp_path, ["TestAgent", "--debug-level", "3"])
        assert result.exit_code == 0, result.output
    else:
        _wire_project(monkeypatch)
    if entry == "otto run install":
        token = set_context(ctx)
        try:
            result = dispatch.invoke(
                run_app, ["install", "--debug-level", "3"], async_leaves=True, accepts_options=True
            )
        finally:
            reset_context(token)
        assert result.exit_code == 0, result.output
    elif entry == "run_instruction":
        given = run_instruction(ctx, name="install", opts=[Acme(debug_level=3)])
        assert asyncio.run(given).is_ok
    elif entry == "otto.project.install":
        from otto import project

        ctx.bind_verb_options(verb="run", kwargs={"debug_level": 3})
        token = set_context(ctx)
        try:
            assert asyncio.run(project.install()).is_ok
        finally:
            reset_context(token)
    elif entry == "use_options around host.install":
        (host,) = ctx.lab.hosts.values()

        async def installed() -> Result:
            with otto.use_options(Acme(debug_level=3)):
                return await host.install()

        assert asyncio.run(installed()).is_ok
    assert seen == [3]


def test_a_plugin_command_reads_the_given_value_too(acme, monkeypatch) -> None:
    """A plugin command covered by ``Acme`` reads the typed value, by the same preparation.

    Its body is the library's ``otto.project.install``, so the hook is reached
    as from any other entry point.
    """
    ctx, seen = acme
    _register_project()
    _wire_project(monkeypatch)
    register_options(Acme, verbs=["run", "test", "zdeploy"], overwrite=True)

    async def zdeploy() -> None:
        from otto import project

        assert (await project.install()).is_ok

    token = set_context(ctx)
    try:
        result = dispatch.invoke(
            zdeploy, ["--debug-level", "3"], spec_name="zdeploy", accepts_options=True
        )
    finally:
        reset_context(token)
    assert result.exit_code == 0, result.output
    assert seen == [3]


def test_under_otto_host_the_hook_reads_the_default(acme, monkeypatch) -> None:
    """``otto host <id> install`` offers no registered class's flags: the hook reads 0.

    Stage 3 gives host commands the run flags; until then a hook there reads
    defaults. Through the dispatch ``tests/unit/cli/test_host.py`` drives,
    with the real lab context.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from otto.cli.main import app
    from otto.inventory import InventoryView
    from otto.labs.loader import Lab
    from otto.session.lab import BuiltLab
    from tests._fixtures.bootstrap_seam import patch_bootstrap
    from tests.unit.cli.test_host import _make_host_with_session

    _, seen = acme
    host = _make_host_with_session([("", 0)])
    host.products = [Agent(seen)]
    patch_bootstrap(monkeypatch, [])
    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs: BuiltLab(
            lab=Lab(name="x"), view=InventoryView.of_default(inventory=None)
        ),
    )
    with (
        patch("otto.cli.invoke.ensure_cli_session"),
        patch("otto.logger.management.create_output_dir"),
        patch("otto.fleet.get_host", return_value=host),
    ):
        result = CliRunner().invoke(app, ["--lab", "x", "-R", "host", "router1", "install"])
    assert seen == [0], (result.output, result.exception)


def test_a_class_the_command_does_not_offer_reads_defaults_except_for_a_given_name(
    acme, request, tmp_path
) -> None:
    """``otto test`` offers ``Acme`` only; the ensured install body's class is ``Install``.

    Typed, ``--debug-level`` reaches ``Install`` by name. ``Acme.variant`` was
    parsed (``lab``) but not typed, so ``Install`` keeps its own ``field``.
    """
    ctx, seen = acme
    bodies = _register_project_with_its_own_install_class()
    request.getfixturevalue("one_project")(files={"tests/test_agent.py": _ENSURED_TEST})
    result = _otto_test(ctx, tmp_path, ["TestAgent", "--debug-level", "3"])
    assert result.exit_code == 0, result.output
    [body] = bodies
    assert isinstance(body, Install)
    assert (body.variant, body.debug_level) == ("field", 3)
    assert seen == [3]


@pytest.mark.parametrize("entry", ["otto run", "run_instruction"])
def test_a_project_bodys_options_are_built_once_and_the_validated_object_runs(
    entry, acme, monkeypatch
) -> None:
    """One preparation on both paths: the body class is built once; the body gets that object."""
    import asyncio

    built: list[Any] = []

    @otto.options
    class Counted(InstallOptions):
        marker: int = 0

        def __post_init__(self) -> None:
            built.append(self)

    bodies: list[Any] = []
    with registering_project(PROJECT):

        @register_project_actions
        class Sut(_Uninstalled):
            @instruction(options=Counted)
            async def install(self, opts: Counted) -> Result:
                bodies.append(opts)
                return await super().install(opts)

    del Sut
    ctx, _seen = acme
    _wire_project(monkeypatch)
    if entry == "otto run":
        token = set_context(ctx)
        try:
            result = dispatch.invoke(run_app, ["install"], async_leaves=True, accepts_options=True)
        finally:
            reset_context(token)
        assert result.exit_code == 0, result.output
    else:
        assert asyncio.run(run_instruction(ctx, name="install")).is_ok
    assert len(built) == 1
    assert len(bodies) == 1
    assert bodies[0] is built[0]
