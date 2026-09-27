"""Import contracts: what an entry point must never load, and who loaded it if it did.

Every command pays, on each run, for every module its import graph reaches,
and on an NFS venv each module file is a round trip. The per-verb import cost
was cut edge by edge (#455); this table keeps the cuts cut. Each row maps an
ENTRY (an import statement, run as the first thing a fresh interpreter does)
to the modules it must NEVER load, directly or transitively.

When a row fails, the message prints, for each forbidden module found, the
chain of frames that first imported it, outermost first: the last otto frame
in that chain is the edge to cut. The code-shape rule sanctions the fix: a
function-scope import in the one function that uses the dependency, a
``TYPE_CHECKING`` import with quoted annotations for a type, or a lazy-table
entry in a package ``__init__``.

The DIRECT form of the same bans (a heavy import at the top of a CLI module,
an eager import in a lazy package ``__init__``) is ast-grep's job
(``.ast-grep/rules/cli-command-no-module-scope-heavy-import.yml`` and
``lazy-package-init-stays-lazy.yml``, run by ``make lint-arch``), which names
the offending line without running anything. This table catches what a
per-file rule cannot see: a harmless-looking import that itself reaches a
heavy module. The file-operation ceilings in ``tests/unit/import_budget/``
remain the cost backstop; these rows name the edge.

To add a contract, add a row. A row belongs here when it is a pure
"importing X never loads Y" check; a guard that also asserts behaviour (a call
still works, an access DOES load something) stays with the code it tests.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# Nothing a verb's `--help` needs, and so nothing its command module may load
# at import: each was cut from a command module's import graph with a measured
# saving, and each belongs to a command body (running, reading the lab,
# reporting, validating options, drawing a table).
_NO_COMMAND_MODULE_LOADS = [
    "asyncio",
    "asyncssh",
    "pydantic",
    "rich.table",
    "otto.config.fleet",
    "otto.config.lab",
    "otto.config.repo",
    "otto.context",
    "otto.coverage.merge",
    "otto.coverage.reporter",
    "otto.coverage.store",
    "otto.coverage.tree",
    "otto.docker.build",
    "otto.docker.compose",
    "otto.host.host",
    "otto.host.remote_host",
    "otto.host.session",
    "otto.host.unix_host",
    "otto.models.monitor",
]

IMPORT_CONTRACTS: dict[str, list[str]] = {
    # --- Each gated verb's command module (its `--help` imports it). ---
    # The root command tree: no help reads a repo or validates options.
    "import otto.cli.main": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.run": _NO_COMMAND_MODULE_LOADS,
    # The verb menu comes from the resolved host's class, not this module.
    "import otto.cli.host": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.reservation": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.docker": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.schema": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.monitor": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.test": _NO_COMMAND_MODULE_LOADS,
    # Only building or fetching a report can meet a coverage error, and the
    # missing-tool error lives with the host errors.
    "import otto.cli.cov": [
        *_NO_COMMAND_MODULE_LOADS,
        "otto.coverage.errors",
        "otto.coverage.renderer.spa_renderer",
        "otto.host.errors",
        "otto.logger",
    ],
    # --- Tracked verbs whose module-top edges were cut. ---
    "import otto.cli.cache": ["rich.table"],
    "import otto.cli.inventory": ["rich.table"],
    "import otto.cli.link": ["otto.config.fleet", "rich.table"],
    "import otto.cli.tunnel": ["otto.config.fleet", "rich.table"],
    # --- Shared modules every command imports. ---
    # The command tree imports otto.config for every `--help`; none reads a repo.
    "import otto.config": ["otto.config.repo", "otto.config.version"],
    # Command modules take constants from otto.utils; only wait_for_async
    # needs asyncio, and it already runs on a loop.
    "import otto.utils": ["asyncio"],
    # Every command reads settings; only one that loads a lab needs host code.
    "import otto.models.settings": [
        "otto.models.host",
        "otto.models.inventory",
        "otto.host.unix_host",
    ],
    # --- The run path: what every lab-loading command imports. ---
    # One host name imports its module, not the host package's other hosts,
    # nor the SSH stack a local host never uses.
    "from otto.host import LocalHost": [
        "asyncssh",
        "otto.host.docker_host",
        "otto.host.embedded_host",
        "otto.host.transfer.nc",
    ],
    # Host-class discovery (the dynamic `otto host` CLI) opens no connection.
    "from otto.host.os_profile import HOST_CLASSES, get_host_class": [
        "aioftp",
        "asyncssh",
        "otto.host.console",
        "telnetlib3",
    ],
    # A host with no session-setup hook never loads the hook machinery: the
    # session and the terminal bridge import it below their no-hook returns.
    "import otto.host.session": ["otto.host.session_setup"],
    "import otto.host.interact": ["otto.host.session_setup"],
    # Classifying a survey login failure loads asyncssh only for an SSH error.
    "from otto.host.survey.login import classify_login_error": ["asyncssh"],
    # Host-spec validation reads IMPAIRERS; the built-in resolves by reference.
    "from otto.link import IMPAIRERS": [
        "otto.link.manage",
        "otto.link.model",
        "otto.link.netem",
        "otto.link.placement",
    ],
    # Building a report imports the renderer only once it renders.
    "import otto.coverage.reporter": ["otto.coverage.renderer.spa_renderer"],
    # A plain `otto test` decides coverage through instrumentation alone; the
    # reporter, merger, store and fetcher belong to a run that reports.
    "from otto.coverage.instrumentation import decide_coverage, detect_for_lab": [
        "otto.coverage.collect",
        "otto.coverage.fetcher",
        "otto.coverage.merge",
        "otto.coverage.reporter",
        "otto.coverage.store",
        "otto.coverage.tree",
    ],
    # Every lab-loading command places the declared container hosts.
    "from otto.docker.compose import register_declared_container_hosts": [
        "otto.docker._context_hash",
        "otto.docker.build",
        "otto.docker.staging",
    ],
    # Every lab-loading command builds the inventory the lab names.
    "from otto.inventory import build_inventory": [
        "otto.inventory.netbox",
        "otto.inventory.snapshot",
    ],
    # Every lab-loading command builds the reservation gate, even under -R.
    "from otto.reservations import build_reservation_gate": [
        "otto.reservations.json_backend",
        "otto.reservations.null_backend",
    ],
}

# The positive control, for a row whose forbidden list alone could pass by
# loading nothing at all: what the entry must load. `from otto.host import
# LocalHost` must reach the module that defines LocalHost, and nothing else of
# the host package's hosts (its row above).
IMPORT_MUST_LOAD: dict[str, list[str]] = {
    "from otto.host import LocalHost": ["otto.host.local_host"],
}

# Runs in the child. It records, for each watched module, the frames that
# imported it, and reports which watched modules the entry loaded.
#
# It must not change what the entry imports, so it imports nothing before the
# entry runs (json and linecache load only afterwards, to render the report),
# and its finder never finds anything: returning None hands every lookup to
# the real finders unchanged. The finder runs for a module only while it is
# absent from sys.modules, so the frames stored LAST for a name are those of
# the import that loaded it (an earlier `importlib.util.find_spec` probe that
# imported nothing is overwritten).
_CHILD = r"""
import sys

ENTRY = sys.argv[1]
WATCHED = frozenset(sys.argv[2:])
PRELOADED = sorted(m for m in WATCHED if m in sys.modules)
FRAMES = {}


class FirstImportRecorder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name in WATCHED and name not in sys.modules:
            frames = []
            frame = sys._getframe(1)
            while frame is not None:
                frames.append((frame.f_code.co_filename, frame.f_lineno))
                frame = frame.f_back
            FRAMES[name] = frames[::-1]
        return None


sys.meta_path.insert(0, FirstImportRecorder)
try:
    exec(compile(ENTRY, "<entry>", "exec"), {"__name__": "__entry__"})
finally:
    sys.meta_path.remove(FirstImportRecorder)
LOADED = sorted(m for m in WATCHED if m in sys.modules and m not in PRELOADED)

import json
import linecache

ENTRY_LINES = ENTRY.splitlines()


def render(filename, lineno):
    if filename == "<entry>":
        return f"<entry>:{lineno}  {ENTRY_LINES[lineno - 1].strip()}"
    return f"{filename}:{lineno}  {linecache.getline(filename, lineno).strip()}"


def chain(frames):
    return [
        render(f, n)
        for f, n in frames
        if not f.startswith("<frozen") and f != "<string>"
    ]


print(json.dumps({
    "preloaded": PRELOADED,
    "loaded": {m: chain(FRAMES.get(m, [])) for m in LOADED},
}))
"""


def _shorten(line: str) -> str:
    """Show a frame's path relative to the repo, or from site-packages on."""
    root = f"{PROJECT_ROOT}/"
    if line.startswith(root):
        return line[len(root) :]
    marker = "site-packages/"
    return line[line.index(marker) + len(marker) :] if marker in line else line


def _explain(entry: str, report: dict) -> str:
    lines = [f"`{entry}` loaded modules its import contract forbids:"]
    for module, frames in report["loaded"].items():
        lines.append(f"  {module}, first imported by (outermost first):")
        shown = frames or ["(no import frames recorded)"]
        lines.extend(f"    {_shorten(frame)}" for frame in shown)
    lines.extend(
        f"  {module} was already loaded when the entry ran: interpreter startup (a .pth "
        "file or sitecustomize) imported it, so this row cannot see who else would"
        for module in report["preloaded"]
    )
    lines.append(
        "Cut the edge at the last otto frame: import the module inside the function that "
        "uses it, or under TYPE_CHECKING with quoted annotations for a type-only name. "
        "Rows live in tests/unit/test_import_contracts.py."
    )
    return "\n".join(lines)


def run_contract(entry: str, forbidden: list[str], *, pythonpath: Path | None = None) -> dict:
    """Run *entry* first in a fresh interpreter; report which *forbidden* modules it loaded."""
    env = None
    if pythonpath is not None:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(pythonpath), *sys.path])}
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, entry, *forbidden],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"`{entry}` failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("entry", "forbidden"), list(IMPORT_CONTRACTS.items()))
def test_an_entry_never_loads_what_its_contract_forbids(entry, forbidden):
    report = run_contract(entry, forbidden)
    assert report == {"preloaded": [], "loaded": {}}, _explain(entry, report)


@pytest.mark.parametrize(("entry", "required"), list(IMPORT_MUST_LOAD.items()))
def test_an_entry_loads_what_it_exists_to_load(entry, required):
    report = run_contract(entry, required)
    assert report["preloaded"] == [], report
    assert sorted(report["loaded"]) == sorted(required), report


def test_a_broken_contract_names_the_chain_that_loaded_the_module(tmp_path):
    """The failure message is the point of the table: it must name the edge.

    A three-module chain (top imports mid at module scope, mid imports heavy
    at module scope) breaks a row forbidding heavy to ``import top``; the
    report must carry every hop, outermost first, and leave out a watched
    module the entry never reached.
    """
    (tmp_path / "contract_top.py").write_text("import contract_mid\n")
    (tmp_path / "contract_mid.py").write_text("X = 1\nimport contract_heavy\n")
    (tmp_path / "contract_heavy.py").write_text("")
    (tmp_path / "contract_unused.py").write_text("")

    report = run_contract(
        "import contract_top", ["contract_heavy", "contract_unused"], pythonpath=tmp_path
    )

    assert report["preloaded"] == []
    assert report["loaded"] == {
        "contract_heavy": [
            "<entry>:1  import contract_top",
            f"{tmp_path}/contract_top.py:1  import contract_mid",
            f"{tmp_path}/contract_mid.py:2  import contract_heavy",
        ]
    }
    message = _explain("import contract_top", report)
    assert "contract_heavy, first imported by (outermost first):" in message
    assert f"{tmp_path}/contract_mid.py:2  import contract_heavy" in message


def test_the_recorder_changes_nothing_the_entry_imports():
    """Watching a module must not change whether, or what, the entry loads.

    The same entry is run with and without a watch list; the set of modules it
    leaves loaded must be identical, so the recorder can neither hide an edge
    nor invent one.
    """
    probe = (
        "import otto.cli.test\n"
        "import json, sys\n"
        "print(json.dumps(sorted(sys.modules)), file=sys.stderr)"
    )

    def modules(forbidden: list[str]) -> set[str]:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, probe, *forbidden],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return set(json.loads(proc.stderr.strip().splitlines()[-1]))

    watched = modules(["otto.suite.run", "otto.suite", "rich.table", "asyncio"])
    unwatched = modules([])
    assert watched == unwatched, sorted(watched ^ unwatched)
