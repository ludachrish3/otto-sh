"""Two projects whose classes give ``otto run`` and ``otto test`` every kind of flag they carry.

The world (:func:`options_golden_world`), written to a temporary directory as
:func:`tests._fixtures.shim_project.make_shim_project` writes its project:

- project ``a`` (``lab_patterns = ["alpha.*"]``, lab ``alpha1``) registers
  ``ARunTest`` for ``["run", "test"]``, ``ARun`` for ``["run"]`` and ``ATest``
  for ``["test"]`` (``ATest``'s ``--a-token`` hides its default). It has three
  standalone instructions: ``a-plain`` with no class, ``a-owned`` with its own
  ``options=`` class ``AOwn``, and ``a-annotated`` with a parameter annotated
  with the registered ``ARun``;
- project ``b`` (``lab_patterns = ["beta.*"]``, lab ``beta1``) registers
  ``BRunTest`` for ``["run", "test"]`` and overrides the project instruction
  ``install`` with a body class of its own, ``BInstall`` (which inherits otto's
  ``InstallOptions``).

:data:`ORIGINS` says where each flag of the world comes from, by parameter name;
a parameter it does not name is otto's own.
"""

import dataclasses
import os
from pathlib import Path

from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project
from tests._fixtures.warm_tab import cli_env

_A_INIT = '''
"""Project a: a class for each verb and for both, and three standalone instructions."""

import enum
from typing import Annotated

import typer

import otto
from otto.instructions import instruction


class Flavour(enum.Enum):
    mild = "mild"
    hot = "hot"


@otto.options
class ARunTest:
    a_flavour: Annotated[Flavour, typer.Option("--a-flavour", "-F", help="a's flavour.")] = (
        Flavour.mild
    )
    a_loud: Annotated[bool, typer.Option(help="a speaks up.")] = False


otto.register_options(ARunTest, verbs=["run", "test"])


@otto.options
class ARun:
    a_run_level: Annotated[int, typer.Option(help="a's run level.")] = 1


otto.register_options(ARun, verbs=["run"])


@otto.options
class ATest:
    a_token: Annotated[str, typer.Option(help="a's test token.", show_default=False)] = "s3cret"


otto.register_options(ATest, verbs=["test"])


@otto.options
class AOwn:
    a_count: Annotated[int, typer.Option("--a-count", "-c", help="How many.")] = 2


@instruction()
async def a_plain(times: Annotated[int, typer.Option(help="How often.")] = 1) -> None:
    """An instruction with no class."""


@instruction(options=AOwn)
async def a_owned(opts: AOwn) -> None:
    """An instruction with its own class."""


@instruction()
async def a_annotated(
    run: ARun, label: Annotated[str, typer.Option(help="A label.")] = "x"
) -> None:
    """An instruction with a parameter annotated with a registered class."""
'''

_B_INIT = '''
"""Project b: a class for both verbs, and an install body with a class of its own."""

from typing import Annotated

import typer

import otto
from otto.instructions import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result


@otto.options
class BRunTest:
    b_level: Annotated[int, typer.Option(help="b's level.")] = 0


otto.register_options(BRunTest, verbs=["run", "test"])


@otto.options
class BInstall(InstallOptions):
    b_variant: Annotated[str, typer.Option(help="b's install variant.")] = "field"


@register_project_actions
class BActions(ProjectActions):
    @instruction(options=BInstall)
    async def install(self, opts: BInstall) -> Result:
        """Install as otto would, with b's variant."""
        return await super().install(opts)
'''

ORIGINS: "dict[str, tuple[str, str | None]]" = {
    "a_flavour": ("run_and_test_class", "a"),
    "a_loud": ("run_and_test_class", "a"),
    "a_run_level": ("run_class", "a"),
    "a_token": ("test_class", "a"),
    "a_count": ("own_class", "a"),
    "times": ("own_parameter", "a"),
    "label": ("own_parameter", "a"),
    "b_level": ("run_and_test_class", "b"),
    "b_variant": ("body_class", "b"),
    "ensure": ("body_class", None),
    "recover_partial": ("body_class", None),
}
"""Each world flag's parameter name: its origin and the project that wrote it.

``ensure`` and ``recover_partial`` are otto's ``InstallOptions``, the body
class every project's ``install`` carries, so no project wrote them."""

OWN = ("own_parameter", None)
"""The origin of a parameter :data:`ORIGINS` does not name: otto's own."""

COMMANDS = {
    "test": ["test"],
    "run a-plain": ["run", "a-plain"],
    "run a-owned": ["run", "a-owned"],
    "run a-annotated": ["run", "a-annotated"],
    "run install": ["run", "install"],
}
"""The commands whose flags the world gives, each by the words that name it."""


@dataclasses.dataclass(frozen=True)
class World:
    """The written world: its root, and the environment an ``otto`` of it runs under."""

    root: Path
    env: "dict[str, str]"


def _project(root: Path, name: str, pattern: str, lab: str, init: str) -> Path:
    project = make_project(
        root / name,
        name=name,
        version="0.1.0",
        extra=(
            f'libs = ["pylib"]\ninit = ["{name}_init"]\n\n'
            f'[project]\nlab_patterns = ["{pattern}"]\n\n'
            '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'
        ),
        files={f"pylib/{name}_init.py": init},
    )
    hosts = [
        {
            "ip": "10.0.0.1",
            "element": f"{name}box",
            "labs": [lab],
            "creds": [{"login": "u", "password": "p"}],
        }
    ]
    write_lab_json(project / "lab" / "lab.json", hosts)
    return project


def options_golden_world(root: Path) -> World:
    """Write the world of the module docstring under *root*; return it.

    Its environment is :func:`tests._fixtures.warm_tab.cli_env` over both
    projects, with its home under *root*. No run has seeded the cache yet.
    """
    a = _project(root, "a", "alpha.*", "alpha1", _A_INIT)
    b = _project(root, "b", "beta.*", "beta1", _B_INIT)
    return World(root=root, env=cli_env(f"{a}{os.pathsep}{b}", root / "home"))
