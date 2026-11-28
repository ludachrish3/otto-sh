"""A world of projects that take part in an invocation or not, for the options-scope tests.

The world (:func:`scope_world`):

- project ``a`` (``lab_patterns = ["alpha.*"]``; every project here has
  ``host_patterns = [".*"]``) registers :class:`AFlags` for ``"flash"``,
  and requires ``lib``;
- project ``b`` (``lab_patterns = ["beta.*"]``) registers ``BFlags`` for ``"flash"`` by the
  lazy string form (:data:`B_FLAGS`): its module, :data:`B_FLAGS_MODULE`, is written to the
  test's ``tmp_path`` and imported only when something resolves the registration;
- project ``lib`` (``lab_patterns = ["gamma.*"]``), which ``a`` requires, registers
  :class:`LibFlags` for ``"flash"``;
- :class:`Free` is registered for ``"flash"`` outside any project, so it always applies.

The fixture plugin commands (``tests/_fixtures/plugin_commands.py``) are registered as
accepting. :meth:`ScopeWorld.override_install` gives a project an ``install`` body with an
options class of its own, so ``otto run install`` carries that project's body flags. The
labs ``alpha1`` and ``beta1`` are declared by :func:`recording_repository`, which the world
uses as its lab sources, and load with one host each, so the project a lab selects has a
host in play: the projects' flags follow the ``-l`` names, never a loaded lab.

:func:`recording_repository` stands in for every project's lab sources and records every
read of them, so a test can say that nothing read a lab source before the preamble did.
"""

import dataclasses
import importlib
import re
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any

import pytest
import typer

import otto
from otto.config.scope import ProjectScopeConfig
from otto.params import register_options
from otto.registry import registering_project
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.fake_project import fake_project

B_FLAGS_MODULE = "scope_world_b_flags"
"""The module holding ``BFlags``, generated per test, imported only by a resolution."""

B_FLAGS = f"{B_FLAGS_MODULE}:BFlags"
"""Project ``b``'s lazy registration."""

_B_FLAGS_SOURCE = '''\
"""Project b's options class, registered by its import path."""

from typing import Annotated

import typer

import otto


@otto.options
class BFlags:
    b_level: Annotated[int, typer.Option(envvar="B_LEVEL", help="b's level.")] = 0
'''

ENV = {"OTTO_LAB": "", "COLUMNS": "200", "A_LEVEL": None, "B_LEVEL": None, "LIB_LEVEL": None}
"""The environment every invocation of the world runs under: no lab, no level set."""


@otto.options
class AFlags:
    a_level: Annotated[int, typer.Option(envvar="A_LEVEL", help="a's level.")] = 0


@otto.options
class LibFlags:
    lib_level: Annotated[int, typer.Option(envvar="LIB_LEVEL", help="lib's level.")] = 0


@otto.options
class Free:
    free: Annotated[int, typer.Option(help="A level no project owns.")] = 0


def _scope(*patterns: str, hosts: "tuple[str, ...]" = (".*",)) -> ProjectScopeConfig:
    """A ``[project]`` table: ``lab_patterns`` *patterns*, ``host_patterns`` *hosts*.

    Every host of a matched lab by default; ``hosts=()`` declares a project
    that takes part in a lab yet targets no host there.
    """
    return ProjectScopeConfig(
        lab_patterns=[re.compile(p) for p in patterns],
        host_patterns=[re.compile(h) for h in hosts],
    )


def _requires(name: str) -> Any:
    from otto.config.dependencies import ResolvedDependency
    from otto.config.version import Version

    return ResolvedDependency(
        name=name,
        normalized=name,
        constraint="",
        required=True,
        status="satisfied",
        provider_version=Version("1.0.0"),
    )


@dataclasses.dataclass
class ScopeWorld:
    """The world one test runs in: its projects, and the seams to change it."""

    monkeypatch: pytest.MonkeyPatch
    a: Any
    b: Any
    lib: Any

    @property
    def projects(self) -> "list[Any]":
        return [self.a, self.b, self.lib]

    def install(self, *, errors: "list[Any] | None" = None) -> None:
        """Make the world's projects (and *errors*) the composition root's result."""
        install_composed(self.monkeypatch, fake_bootstrap_result(self.projects, errors=errors))

    def fail_to_load(self, project: Any) -> None:
        """Make *project*'s init import fail, as bootstrap reports it."""
        from otto import bootstrap as bs

        error = bs.BootstrapError(
            project.root, source=f"{project.name}_init", cause=ImportError("broken")
        )
        self.install(errors=[error])

    def b_flags(self) -> type:
        """Import and return ``BFlags`` (this imports :data:`B_FLAGS_MODULE`)."""
        return importlib.import_module(B_FLAGS_MODULE).BFlags

    def register(self, cls: "type | str", verbs: "list[str]", *, project: "str | None") -> None:
        """Register *cls* for *verbs* as *project* registers it (``None``: no project)."""
        if project is None:
            register_options(cls, verbs=verbs)
            return
        with registering_project(project):
            register_options(cls, verbs=verbs)

    def override_install(self, options_cls: type, *, project: str) -> None:
        """Make *project* override ``install`` with a body whose class is *options_cls*.

        *options_cls* inherits ``otto.project.InstallOptions``, as every
        ``install`` body's class does; the body calls up to otto's own.
        """
        from otto.instructions import instruction
        from otto.project import ProjectActions, register_project_actions

        class Actions(ProjectActions):
            async def install(self, opts: Any) -> Any:  # pragma: no cover — the walk is a stand-in
                return await super().install(opts)

            install.__annotations__["opts"] = options_cls
            install = instruction(options=options_cls)(install)

        Actions.__name__ = Actions.__qualname__ = f"{project.capitalize()}Actions"
        with registering_project(project):
            register_project_actions(Actions)

    def reads(self, *classes: "type | Callable[[], type]") -> "list[dict[str, Any]]":
        """Make each fixture body also read ``otto.get_options`` of *classes*; return the reads.

        A callable is called in the body for the class (``world.b_flags``), so
        nothing imports it before the body runs. Each read is keyed by class name.
        """
        seen: "list[dict[str, Any]]" = []
        read = _reader(seen, classes)
        record = plugin_commands._record_call

        def recorded(ctx: typer.Context, command: str, **kwargs: Any) -> None:
            read()
            record(ctx, command, **kwargs)

        self.monkeypatch.setattr(plugin_commands, "_record_call", recorded)
        return seen

    def install_reads(self, *classes: "type | Callable[[], type]") -> "list[dict[str, Any]]":
        """Make each ``otto run <name>`` of a project instruction read *classes*; return the reads.

        The run's walk (``otto.project.orchestrator.run_project_instruction``)
        is replaced by one read of ``otto.get_options`` of each of *classes*,
        as a hook reads them, inside the command's preparation: no body runs.
        The reads are :meth:`reads`' shape.
        """
        from otto.project import orchestrator

        seen: "list[dict[str, Any]]" = []
        read = _reader(seen, classes)

        async def walk(name: str, kwargs: Any, *, source: Any = None) -> None:
            read()

        self.monkeypatch.setattr(orchestrator, "run_project_instruction", walk)
        return seen

    def skip_install_bodies(self) -> None:
        """Make the walk that builds a project instruction's body classes build none."""
        from otto.project import orchestrator

        self.monkeypatch.setattr(
            orchestrator, "project_instruction_body_options", lambda *a, **k: []
        )


def _reader(
    seen: "list[dict[str, Any]]", classes: "tuple[type | Callable[[], type], ...]"
) -> "Callable[[], None]":
    """Return a call that appends to *seen* one read of ``otto.get_options`` of each of *classes*.

    The one read :meth:`ScopeWorld.reads` and :meth:`ScopeWorld.install_reads`
    make. A callable is called for its class at the read, so nothing imports
    it before then; each read is keyed by class name.
    """

    def read() -> None:
        found = [c if isinstance(c, type) else c() for c in classes]
        seen.append({cls.__name__: otto.get_options(cls) for cls in found})

    return read


def _evict_b_flags() -> None:
    sys.modules.pop(B_FLAGS_MODULE, None)


@pytest.fixture
def scope_world(monkeypatch, tmp_path, recording_repository) -> "Generator[ScopeWorld]":
    """The world of the module docstring, with the fixture plugin commands accepting.

    Its lab sources are :func:`recording_repository`'s, so ``alpha1`` and
    ``beta1`` are declared labs wherever a line asks the sources.
    """
    source = tmp_path / "scope_world_modules"
    source.mkdir()
    (source / f"{B_FLAGS_MODULE}.py").write_text(_B_FLAGS_SOURCE)
    monkeypatch.syspath_prepend(str(source))
    _evict_b_flags()
    world = ScopeWorld(
        monkeypatch=monkeypatch,
        a=fake_project(
            "a",
            root=Path("/projects/a"),
            project_scope=_scope("alpha.*"),
            dependencies=[_requires("lib")],
        ),
        b=fake_project("b", root=Path("/projects/b"), project_scope=_scope("beta.*")),
        lib=fake_project("lib", root=Path("/projects/lib"), project_scope=_scope("gamma.*")),
    )
    world.install()
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    with plugin_commands.registered(accepts_options=True):
        world.register(AFlags, ["flash"], project="a")
        world.register(B_FLAGS, ["flash"], project="b")
        world.register(LibFlags, ["flash"], project="lib")
        world.register(Free, ["flash"], project=None)
        try:
            yield world
        finally:
            _evict_b_flags()


UNKNOWN_LAB = "Lab {name!r} is not declared by the world's source"
"""What :class:`RecordingRepository` says of a name it does not declare."""


class RecordingRepository:
    """A lab source that declares ``alpha1`` and ``beta1`` and records every read of it.

    Every load is recorded first. A declared lab loads with one host,
    ``<lab>-host``, so a project whose ``lab_patterns`` match the lab has a
    host in play and a scoped command runs rather than refusing an empty
    scope. Any other name is refused as a source refuses an undeclared lab
    (``LabNotFoundError``, the words :data:`UNKNOWN_LAB` gives).
    """

    labs = ("alpha1", "beta1")

    def __init__(self) -> None:
        self.reads: "list[str]" = []

    def load_lab(self, name: Any, **_: Any) -> Any:
        from otto.labs.errors import LabNotFoundError
        from tests._fixtures.fleet import _lab

        self.reads.append(f"load_lab {name}")
        if name not in self.labs:
            raise LabNotFoundError(UNKNOWN_LAB.format(name=name))
        return _lab((f"{name}-host", name))

    def list_labs(self) -> "list[str]":
        self.reads.append("list_labs")
        return list(self.labs)


@pytest.fixture
def recording_repository(monkeypatch) -> RecordingRepository:
    """Every project's lab sources, replaced by one :class:`RecordingRepository`.

    Building the sources is a read too (``"build"``), whatever asks for them.
    """
    repository = RecordingRepository()

    def build(**_: Any) -> RecordingRepository:
        repository.reads.append("build")
        return repository

    monkeypatch.setattr("otto.labs.sources.build_lab_sources", build)
    return repository
