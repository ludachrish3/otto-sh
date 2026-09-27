"""Generate a synthetic sut-dir repo of a requested shape.

Deterministic by construction: the harness creates what it measures, so a
repo-bearing import-budget surface stays as host-independent as the rest of the
table.

`dirs` scales independently of `files` because a stat-only walk has no per-file
audit signal — it is observable only per directory, via `os.scandir`. `top_level`
is separate because only top-level test files can register (`iter_test_files` is
non-recursive), which is the distinction the names cache section rests on.
"""

import json
import sys
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo

GENERATED_LIB_DIRS = ["pylib"]
"""The lib dirs every generated repo declares. Single source of truth for
``_EXTRA`` below (which builds its ``libs = [...]`` line from this list rather
than hard-coding one) and for the tests that need to know where a repo's
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
"""The host id of the closed-port SSH host ``generate_repo(ssh_lab_port=...)`` declares.

Hyphenated because a host id is a slug (``[a-z0-9-]``): an element named
``budget_ssh`` would be addressed as ``budget-ssh`` anyway."""


def _budget_ssh_lab(port: int) -> str:
    """A JSON lab holding one unix host at ``127.0.0.1`` whose SSH port is *port*.

    The host entry is the unix host shape of ``lab_data/tech1/lab.json``
    (``test3``), so the SSH path is measured on the same kind of host a real
    lab carries. Only the addresses differ: ``127.0.0.1`` for the host, so a
    connection is refused at once when the port is closed, and an interface
    address no tech1 host uses, so the merged lab keeps its IPs unique.
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
        "roles": ["docker"],
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


_TEST_BODY = "def test_x():\n    pass\n"

_SUITE_BODY = """\
import pytest

from otto.suite import OttoSuite


class TestTop{i}(OttoSuite):
    async def test_x(self):
        pass
"""

_REALISTIC_INIT = '''\
from otto.cli.run import instruction
from otto.monitor.parsers import MetricParser  # noqa: F401  (real repos subclass parsers here)
from otto.result import CommandResult
from otto.utils import Status


@instruction()
async def noop() -> CommandResult:
    """Budget no-op: a real dispatch whose body does no work."""
    return CommandResult(Status.Success, value="", command="noop", retcode=0)


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


def generate_repo(
    root: Path,
    *,
    files: int,
    dirs: int,
    top_level: int = 2,
    name: str = "genrepo",
    realistic: bool = False,
    ssh_lab_port: int | None = None,
) -> Path:
    """Write a sut-dir repo under *root*; return the path for ``OTTO_SUT_DIRS``.

    *files* nested test files spread across *dirs* subdirectories, plus
    *top_level* test files directly in the tests dir.

    *realistic* shapes the repo like a real one: top-level suite files that
    import pytest, an init module that registers an instruction and imports a
    monitor parser, and a JSON lab source, so budget surfaces see what real
    repos cost. ``realistic=False`` behaves exactly as before: a bare init
    module and plain top-level test functions, no lab.

    *ssh_lab_port* adds a second JSON lab source, ``budget_lab/lab.json``,
    holding one unix host (:data:`BUDGET_SSH_HOST`) at ``127.0.0.1`` with SSH
    on that port, in the ``unix`` lab. A caller that passes a closed port
    measures otto's SSH path with no real host: the connection is refused at
    once.
    """
    # Through `make_sut_repo`, not a hand-rolled write: `.otto/settings.toml`
    # has exactly one spelling in the suite (tests/_fixtures/sutrepo.py), and
    # `tests/unit/test_sutrepo_scaffold_policy.py` enforces it.
    extra = _EXTRA.format(name=name)
    init_body = ""
    if realistic:
        extra += _LAB_SOURCE.format(lab=PROJECT_ROOT / "tests" / "_fixtures" / "lab_data" / "tech1")
        init_body = _REALISTIC_INIT
    repo_files = {f"pylib/{name}_instructions.py": init_body}
    if ssh_lab_port is not None:
        extra += _LAB_SOURCE.format(lab=root / name / "budget_lab")
        repo_files["budget_lab/lab.json"] = _budget_ssh_lab(ssh_lab_port)
    repo = make_sut_repo(
        root / name,
        name=name,
        version="0.1.0",
        tests=["tests"],
        extra=extra,
        files=repo_files,
    )

    tests_root = repo / "tests"
    tests_root.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        (tests_root / f"sub{d}").mkdir(exist_ok=True)
    for i in range(top_level):
        body = _SUITE_BODY.format(i=i) if realistic else _TEST_BODY
        (tests_root / f"test_top{i}.py").write_text(body)
    for i in range(files):
        # Nested files are walked and parsed but never executed: they stay
        # plain even under `realistic=True`, so §3.3 (of
        # `2026-09-25-dispatch-startup-cost-design.md`)'s per-file bound measures
        # walking and parsing alone.
        subdir = tests_root / f"sub{i % dirs}"
        (subdir / f"test_{i}.py").write_text(_TEST_BODY)
    return repo


def argv_writing_test_file_bytecode(entry: str) -> list[str]:
    """A child argv whose test-file imports write ``__pycache__`` beside them, as a real shell does.

    ``tests/conftest.py`` exports ``PYTHONPYCACHEPREFIX`` to every subprocess,
    so a child imports a generated repo's test files without ever touching the
    repo: the bytecode goes under the prefix. A real shell has no prefix, and
    the first import of a test file creates ``tests/__pycache__`` — which moves
    the mtime of a directory the completion cache keys on. Dropping the prefix
    for the whole child would also write bytecode into ``src/otto`` (the
    conftest's session guard fails on that), so the prefix is lifted only
    around each test-file import. The caller must also leave
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
        "from otto.config.repo import Repo\n"
        "_import = Repo.import_test_file\n"
        "def _import_writing_bytecode(self, test_file):\n"
        "    saved, sys.pycache_prefix = sys.pycache_prefix, None\n"
        "    try:\n"
        "        _import(self, test_file)\n"
        "    finally:\n"
        "        sys.pycache_prefix = saved\n"
        "Repo.import_test_file = _import_writing_bytecode\n"
    )
    return [sys.executable, "-c", prelude + entry]
