"""A SUT repo exercising every completion source the shim must reproduce (spec §6)."""

from pathlib import Path

from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

CREDS = [{"login": "u", "password": "p"}]
HOSTS = [
    {
        "ip": "10.0.0.1",
        "element": "dut",
        "element_id": 1,
        "labs": ["east"],
        "creds": CREDS,
        "docker_capable": True,
    },
    {
        "ip": "10.0.0.2",
        "element": "dut",
        "element_id": 2,
        "labs": ["east", "west"],
        "creds": CREDS,
        "os_type": "shimos",
    },
    {"ip": "10.0.0.3", "element": "box", "labs": ["west"], "creds": CREDS, "os_type": "zephyr"},
]
"""The json backend derives the ids ``dut1``, ``dut2`` (element + element_id) and ``box``."""
LINKS = [{"endpoints": [{"host": "dut1"}, {"host": "dut2"}]}]
"""Host-only endpoints — the shape tests/unit/config/test_completion_link_ids.py:60-75 loads."""

SETTINGS = """\
libs = ["pylib"]
init = ["shimsut_init"]

[[lab.sources]]
backend = "json"
paths = ["lab"]
"""

INIT = '''
"""Registers a host class, a plugin group with a nested command, and an instruction."""

import enum
from pathlib import Path
from typing import Annotated

import typer

import otto
from otto.cli.run import instruction
from otto.host.os_profile import register_host_class
from otto.host.unix_host import UnixHost
from otto.result import CommandResult
from otto.utils import cli_exposed


class MyHost(UnixHost):
    @cli_exposed(help_="Blink the status LED.")
    async def blink(self, times: int = 1) -> CommandResult:
        return await self.exec(f"blink {times}")


register_host_class("shimos", MyHost)


class Kind(enum.Enum):
    fast = "fast"
    full = "full"


plug = typer.Typer(help="A plugin group.")
nest = typer.Typer(help="A nested group.")
plug.add_typer(nest, name="nest")


@nest.command("leaf")
def leaf(
    target: Annotated[Path, typer.Argument()] = Path(),
    kind: Annotated[Kind, typer.Option("--kind")] = Kind.fast,
    loud: Annotated[bool, typer.Option("--loud/--quiet")] = False,
) -> None:
    """A nested leaf."""


otto.register_cli_command("plug", plug)


@otto.options
class _Opts:
    level: Annotated[int, typer.Option("--level", help="How bright.")] = 1


@instruction(options=_Opts)
async def blink_all(opts: _Opts) -> None:
    """Blink every host."""
'''

SUITE = '''
from typing import Annotated

import pytest
import typer

from otto import options
from otto.suite import OttoSuite

pytestmark = pytest.mark.slow


@options
class _Options:
    depth: Annotated[int, typer.Option("--depth")] = 1


class TestShim(OttoSuite):
    """The differential fixture suite."""

    Options = _Options

    @pytest.mark.smoke
    async def test_one(self, suite_options: _Options) -> None:
        pass

    async def test_two(self) -> None:
        pass
'''

NESTED = "import pytest\n\n\n@pytest.mark.deep\ndef test_deep():\n    pass\n"

PYPROJECT = '[tool.pytest.ini_options]\nmarkers = ["smoke: quick", "slow: not quick"]\n'


def make_shim_repo(root: Path) -> Path:
    """Create the differential SUT repo under *root* and return it."""
    repo = make_sut_repo(
        root / "shimsut",
        name="shimsut",
        version="0.1.0",
        tests=["tests"],
        extra=SETTINGS,
        files={
            "pylib/shimsut_init/__init__.py": INIT,
            "tests/test_shim_suite.py": SUITE,
            "tests/sub/test_nested.py": NESTED,
            "pyproject.toml": PYPROJECT,
        },
    )
    write_lab_json(repo / "lab" / "lab.json", HOSTS, links=LINKS)
    return repo
