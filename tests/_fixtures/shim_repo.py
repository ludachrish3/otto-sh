"""A SUT repo exercising every completion source the shim must reproduce (spec §6)."""

from pathlib import Path

from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

CREDS = [{"login": "u", "password": "p"}]
DUT1_CREDS = [
    *CREDS,
    {"login": "root", "password": "r", "proxy": "su"},
    {"login": "tel", "password": "t", "protocols": ["telnet"]},
    {"login": "u", "password": "p2", "protocols": ["telnet"]},
]
"""dut1 carries the two cred shapes `--user` completion must tell apart: a proxied login
(dropped by `get`/`put`'s direct flavour) and a telnet-scoped login (dropped by --term ssh);
`u` also appears twice, once unscoped and once telnet-scoped (different password per
protocol — `cred_identity` differs by scope) — a login scoped to several protocols is
offered once."""
HOSTS = [
    {
        "ip": "10.0.0.1",
        "element": "dut1",
        "labs": ["east"],
        "creds": DUT1_CREDS,
        "docker_capable": True,
        "docker_priority": 10,
    },
    {
        "ip": "10.0.0.2",
        "element": "dut2",
        "labs": ["east", "west"],
        "creds": CREDS,
        "os_type": "shimos",
        "docker_capable": True,
    },
    {"ip": "10.0.0.3", "element": "box", "labs": ["west"], "creds": CREDS, "os_type": "zephyr"},
    {
        "ip": "10.0.0.4",
        "element": "dut3",
        "labs": ["west"],
        "creds": CREDS,
        "docker_capable": True,
        "docker_priority": 0,
    },
]
"""The json backend derives the ids ``dut1``, ``dut2``, ``box`` and ``dut3`` from the element
names. The docker parents shape the default-parent rule: ``east`` has two capable hosts and
dut1's priority 10 wins it; ``west``'s dut2 and dut3 tie at priority 0, so the rule leaves
``west`` without a default parent."""
LINKS = [{"endpoints": [{"host": "dut1"}, {"host": "dut2"}]}]
"""Host-only endpoints — the shape tests/unit/config/test_completion_link_ids.py:60-75 loads."""

SETTINGS = """\
libs = ["pylib"]
init = ["shimsut_init"]

[[lab.sources]]
backend = "json"
paths = ["lab"]

[[docker.images]]
name = "api"
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.composes]]
name = "core"
path = "docker/compose.yml"
services = ["api", "db"]

[[docker.use_cases]]
name = "integration"
composes = ["core"]
"""

INIT = '''
"""Registers a host class, a nested plugin group, an instruction and test options."""

import enum
from pathlib import Path
from typing import Annotated

import typer

import otto
from otto.instructions import instruction
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


@otto.options
class _ShimTestOpts:
    depth: Annotated[int, typer.Option("--depth")] = 1


otto.register_options(_ShimTestOpts, verbs=["test"])
'''

SUITE = '''
import pytest

pytestmark = pytest.mark.slow


class TestShim:
    """The differential fixture class."""

    @pytest.mark.smoke
    async def test_one(self, ctx) -> None:
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
            "docker/Dockerfile": "FROM scratch\n",
            "docker/compose.yml": "services: {}\n",
        },
    )
    write_lab_json(repo / "lab" / "lab.json", HOSTS, links=LINKS)
    return repo
