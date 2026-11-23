"""Generate a synthetic project of a requested shape.

Deterministic by construction: the harness creates what it measures, so a
project-bearing import-budget surface stays as host-independent as the rest of the
table.

`dirs` scales independently of `files` because a stat-only walk has no per-file
audit signal — it is observable only per directory, via `os.scandir`. `top_level`
is separate because the tests directory's own files and its nested ones sit at
different depths of the corpus walk, and the realistic shape gives only the
top-level ones a class of tests.
"""

import json
import sys
from pathlib import Path

from tests._fixtures.paths import TECH1_GOLDEN
from tests._fixtures.project_scaffold import make_project

GENERATED_LIB_DIRS = ["pylib"]
"""The lib dirs every generated project declares. Single source of truth for
``_EXTRA`` below (which builds its ``libs = [...]`` line from this list rather
than hard-coding one) and for the tests that need to know where a project's
library code lives."""

_EXTRA = (
    "libs = [" + ", ".join(f'"{d}"' for d in GENERATED_LIB_DIRS) + "]\n"
    'init = ["{name}_instructions"]\n'
)

_LAB_SOURCE = """\
[[lab.sources]]
backend = "json"
paths = ["{lab}"]
"""

BUDGET_SSH_HOST = "budget-ssh"
"""The host id of the closed-port SSH host ``generate_project(ssh_lab_port=...)`` declares.

Hyphenated because a host id is a slug (``[a-z0-9-]``): an element named
``budget_ssh`` would be addressed as ``budget-ssh`` anyway."""


def _budget_ssh_lab(port: int) -> str:
    """A JSON lab holding one unix host at ``127.0.0.1`` whose SSH port is *port*.

    The host entry is the unix host shape of the bed cell's ``test3``, so the
    SSH path is measured on the same kind of host a real lab carries. Only the
    addresses differ: ``127.0.0.1`` for the host, so a connection is refused at
    once when the port is closed, and an interface address no tech1 host uses,
    so the merged lab keeps its IPs unique.
    """
    host = {
        "ip": "127.0.0.1",
        "os_type": "unix",
        "valid_terms": ["ssh", "telnet"],
        "valid_transfers": ["scp", "sftp", "ftp", "nc"],
        "is_virtual": True,
        "site": "lab-a",
        "rack": 1,
        "shelf": 4,
        "docker_capable": True,
        "creds": [
            {"login": "vagrant", "password": "vagrant"},
            {"login": "test", "password": "Password1"},
        ],
        "interfaces": {"eth2": {"ip": "192.168.1.250", "subnet": "192.168.1.0/24"}},
        "ssh_options": {"port": port},
    }
    # No top-level `labs` table: the element's own `labs` puts it in `unix`,
    # and a second source redeclaring `unix` would override tech1's entry for
    # it (a WARN on every run, and tech1's lab metadata lost).
    lab = {"elements": [{"name": BUDGET_SSH_HOST, "labs": ["unix"], "hosts": [host]}]}
    return json.dumps(lab, indent=4) + "\n"


_TEMPLATED_SOURCE = """\
[[inventories]]
name = "fleet"
backend = "json"
path = "templated/fleet.json"

[[lab.sources]]
backend = "jsonnet"
paths = ["templated/fleet.jsonnet"]
inventories = ["fleet"]
import_paths = ["templated/lib"]
"""

_TEMPLATED_ENTRY = """\
local lib = import 'fleet.libsonnet';
function(otto) {
  labs: { [lib.lab]: {} },
  elements: [
    {
      name: r.key,
      labs: [lib.lab],
      hosts: [{ inventory: { name: 'fleet', key: r.key }, os_type: 'unix' }],
    }
    for r in otto.inventories.fleet
  ],
}
"""

_RESERVATIONS_SOURCE = """\
[reservations]
backend = "json"

[reservations.json]
path = "{path}"
"""

RESERVATIONS_FILE = "reservations.json"
"""The JSON reservation file ``generate_project(reservations=True)`` declares, under the project.

It holds no rows: a surface that needs a reservation stores the answer in the
completion sidecar, and a backend that is ever read finds nobody holding
anything."""

_RESERVATIONS_BODY = json.dumps({"version": 1, "reservations": []}, indent=4) + "\n"

TEMPLATED_HOSTS = 4
"""How many hosts ``generate_project(templated_lab=True)``'s jsonnet source renders."""


def _templated_files() -> dict[str, str]:
    """The files of the generated project's one ``jsonnet`` lab source, by relative path.

    An entry, one library under its import root, and the ``fleet`` json
    inventory it renders a host per record of, into the ``templated`` lab.
    """
    records = {
        f"tmpl-{i}": {"ip": f"10.77.0.{i + 1}", "creds": [{"login": "u", "password": "p"}]}
        for i in range(TEMPLATED_HOSTS)
    }
    return {
        "templated/fleet.jsonnet": _TEMPLATED_ENTRY,
        "templated/lib/fleet.libsonnet": "{ lab: 'templated' }\n",
        "templated/fleet.json": json.dumps(records, indent=4) + "\n",
    }


_TEST_BODY = "def test_x():\n    pass\n"

_SUITE_BODY = """\
import pytest


class TestTop{i}:
    async def test_x(self):
        pass
"""

_REALISTIC_INIT = '''\
try:
    from otto.instructions import instruction
except ImportError:  # the decorator lived beside the run verb before otto.instructions
    from otto.cli.run import instruction
from otto.monitor.parsers import MetricParser  # noqa: F401  (real projects subclass parsers here)
from otto.result import CommandResult
from otto.utils import Status


@instruction()
async def noop() -> CommandResult:
    """Budget no-op: a real dispatch whose body does no work."""
    return CommandResult(status=Status.Success, value="", command="noop", retcode=0)


@instruction()
async def local_true() -> CommandResult:
    """Budget local-session run: a real dispatch that opens a persistent
    LocalHost session and runs one command through it, so the surface
    measuring this instruction sees what a session's own choke points
    cost — unlike ``noop``, which never opens a host session."""
    from otto.host.local_host import LocalHost

    h = LocalHost()
    try:
        return (await h.run("true")).only
    finally:
        await h.close()
'''


def generate_project(
    root: Path,
    *,
    files: int,
    dirs: int,
    top_level: int = 2,
    name: str = "genproject",
    realistic: bool = False,
    ssh_lab_port: int | None = None,
    templated_lab: bool = False,
    reservations: bool = False,
) -> Path:
    """Write a project under *root*; return the path for ``OTTOPATH``.

    *files* nested test files spread across *dirs* subdirectories, plus
    *top_level* test files directly in the tests dir.

    *realistic* shapes the project like a real one: top-level test-class files
    that import pytest, an init module that registers an instruction and imports a
    monitor parser, and a JSON lab source reading the bed's cell, as its frozen
    JSON file (:data:`~tests._fixtures.paths.TECH1_GOLDEN`), so budget surfaces
    see what real projects cost. ``realistic=False`` behaves exactly as before: a bare init
    module and plain top-level test functions, no lab.

    *ssh_lab_port* adds a second JSON lab source, ``budget_lab/lab.json``,
    holding one unix host (:data:`BUDGET_SSH_HOST`) at ``127.0.0.1`` with SSH
    on that port, in the ``unix`` lab. A caller that passes a closed port
    measures otto's SSH path with no real host: the connection is refused at
    once.

    *templated_lab* adds a ``jsonnet`` lab source rendering
    :data:`TEMPLATED_HOSTS` hosts into the ``templated`` lab from a ``fleet``
    json inventory, with one library under its import root.

    *reservations* declares ``[reservations]`` with the ``json`` backend, read
    from :data:`RESERVATIONS_FILE` under the project, which lists nobody.
    """
    # Through `make_project`, not a hand-rolled write: `.otto/settings.toml`
    # has exactly one spelling in the suite (tests/_fixtures/project_scaffold.py), and
    # `tests/unit/test_project_scaffold_policy.py` enforces it.
    extra = _EXTRA.format(name=name)
    init_body = ""
    if realistic:
        # The cell's frozen JSON, read in place: no render and no copy. The
        # release's import-budget delta generates this project with the TIP's
        # harness under the PREVIOUS release's otto (scripts/import_budget_delta.py),
        # which may have no `jsonnet` backend; and a file outside the fixture
        # root keeps the lab's reads out of the `workspace` counter, as the
        # checkout's `lab_data/tech1` directory did.
        extra += _LAB_SOURCE.format(lab=TECH1_GOLDEN)
        init_body = _REALISTIC_INIT
    project_files = {f"pylib/{name}_instructions.py": init_body}
    if ssh_lab_port is not None:
        extra += _LAB_SOURCE.format(lab=root / name / "budget_lab")
        project_files["budget_lab/lab.json"] = _budget_ssh_lab(ssh_lab_port)
    if templated_lab:
        extra += _TEMPLATED_SOURCE
        project_files.update(_templated_files())
    if reservations:
        extra += _RESERVATIONS_SOURCE.format(path=RESERVATIONS_FILE)
        project_files[RESERVATIONS_FILE] = _RESERVATIONS_BODY
    project = make_project(
        root / name,
        name=name,
        version="0.1.0",
        tests=["tests"],
        extra=extra,
        files=project_files,
        # Only a project with a host to dial pins its known-hosts file.
        known_hosts=root / "known_hosts" if ssh_lab_port is not None else None,
    )

    tests_root = project / "tests"
    tests_root.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        (tests_root / f"sub{d}").mkdir(exist_ok=True)
    for i in range(top_level):
        body = _SUITE_BODY.format(i=i) if realistic else _TEST_BODY
        (tests_root / f"test_top{i}.py").write_text(body)
    for i in range(files):
        # Nested files stay plain even under `realistic=True`: the completion
        # cache's static parse walks and parses them without executing them, so
        # §3.3 (of `2026-09-25-dispatch-startup-cost-design.md`)'s per-file
        # bound measures walking and parsing alone. A surface that resolves
        # test names (`otto test <name>`) collects the whole tree and so does
        # import them, each a one-line module with no imports of its own.
        subdir = tests_root / f"sub{i % dirs}"
        (subdir / f"test_{i}.py").write_text(_TEST_BODY)
    return project


def argv_writing_test_file_bytecode(entry: str) -> list[str]:
    """A child argv whose test-file imports write ``__pycache__`` beside them, as a real shell does.

    ``tests/conftest.py`` exports ``PYTHONPYCACHEPREFIX`` to every subprocess,
    so a child imports a generated project's test files without ever touching the
    project: the bytecode goes under the prefix. A real shell has no prefix, and
    the first import of a test file creates ``tests/__pycache__`` — which moves
    the mtime of a directory the completion cache keys on. Dropping the prefix
    for the whole child would also write bytecode into ``src/otto`` (the
    conftest's session guard fails on that), so the prefix is lifted only
    around each in-process ``pytest.main``: a pytest session is the only thing
    that imports a test file. The caller must also leave
    ``PYTHONDONTWRITEBYTECODE`` out of the child env.

    ``sys.argv[0]`` is set to ``otto``, as the console script's would be:
    click derives the completion env var (``_OTTO_COMPLETE``) from the program
    name, and under ``-c`` that name would be ``-c``.

    *entry* is the statement that runs the CLI, e.g. ``from otto._shim import
    main; main()``.
    """
    prelude = (
        "import sys\n"
        "sys.argv[0] = 'otto'\n"
        "import pytest\n"
        "_main = pytest.main\n"
        "def _main_writing_bytecode(*args, **kwargs):\n"
        "    saved, sys.pycache_prefix = sys.pycache_prefix, None\n"
        "    try:\n"
        "        return _main(*args, **kwargs)\n"
        "    finally:\n"
        "        sys.pycache_prefix = saved\n"
        "pytest.main = _main_writing_bytecode\n"
    )
    return [sys.executable, "-c", prelude + entry]
