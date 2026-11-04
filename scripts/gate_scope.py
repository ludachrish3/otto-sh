"""Classify a diff into the gates it needs (#566).

Each changed path gets a class by location (and, for test modules, by whether the
change is structural): ``none``, ``docs``, ``web``, ``tests-leaf``,
``tests-structural``, ``src-leaf``, ``scripts``, ``escalate``, ``full``. A path with no
row is ``full``. Only a landing's tip is gated: whatever reaches ``main`` in one push,
one squash or several persisting commits, runs the tip list once, at its final tip, and
every earlier commit runs only its likely breakage areas plus ruff. The commit list is a
working check, not a landing gate. The pure classifier, the command
composition and the repository layer all live here. Exit codes: 0 ok, 1 repository command
failure, 2 refusal or usage.
"""

import argparse
import ast
import copy
import fnmatch
import json
import re
import shlex
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.gate_stamp_common import (  # noqa: E402 -- path set up above
    GateError,
    run_git,
    tree_hash,
    tree_state,
)


@dataclass(frozen=True)
class Change:
    """One changed path: repo-relative POSIX path and status A, M or D."""

    path: str
    status: str


@dataclass(frozen=True)
class Scope:
    """The gates a diff needs, as lists of paths, classes and commands."""

    # sorted, deduplicated path classes present (FULL when a path has no row;
    # tests-structural replaces tests-leaf: it already runs every leaf line)
    classes: "list[str]"
    # PATHS for `make quick` at the commit (sorted; every one exists at head and holds a
    # test module)
    quick_paths: "list[str]"
    # changed test modules whose pytestmark is interpreter_agnostic
    guard_files: "list[str]"
    # the src/otto packages touched (sorted; includes trigger files' packages)
    src_packages: "list[str]"
    # changed paths that still exist at head (the lint/typecheck inputs)
    changed_files: "list[str]"
    # every changed path, deleted ones included (the build-file rider reads it)
    changed_paths: "list[str]"
    # commit-time lane commands (SPECIAL_TREES / STABILITY_FILES / LANE_MARKERS)
    special: "list[str]"
    # the same rows' tip-time commands
    special_tip: "list[str]"
    # the sorted bed lane lines a changed src package adds to the tip and to a full commit
    # column
    bed_lanes: "list[str]"
    # paths no row matched (each makes the class `full`)
    unknown: "list[str]"
    # reasons the commit column is the full gate (a FULL row, > 2 packages, unknown path,
    # no surviving root); any one also sends the tip to the bed half (`bed_reach`)
    full_at_commit: "list[str]"
    # the sorted changed test modules in the bed half (`in_bed_half`): each sends the tip
    # to the bed half of the coverage selection
    bed_modules: "list[str]"


Reader = Callable[[str, str], "str | None"]


NONE = "none"
DOCS = "docs"
WEB = "web"
TESTS_LEAF = "tests-leaf"
TESTS_STRUCTURAL = "tests-structural"
SRC_LEAF = "src-leaf"
SCRIPTS = "scripts"
ESCALATE = "escalate"
FULL = "full"

# ── data: every row the classification reads; pinned against the live tree by the tests ──
# fnmatch has no `**`; its `*` crosses `/`, and every glob below is written for that.

NONE_GLOBS = (
    "docs/superpowers/specs/*",
    "todo/*",
    "AGENTS.md",
)  # AGENTS.md: policy text, owner decision
TRIGGER_FILES = frozenset(
    {
        "src/otto/__init__.py",
        "src/otto/registry.py",
        "src/otto/invocation.py",
        "src/otto/context.py",
        "src/otto/run_context.py",
        "src/otto/bootstrap.py",
        "src/otto/lifecycle.py",
        "pyproject.toml",
        "Makefile",
        "noxfile.py",
        ".coveragerc",
        ".github/workflows/ci.yml",
    }
)
TRIGGER_GLOBS = ("src/otto/_shim*.py", "src/otto/*/__init__.py", "tests/_fixtures/*", ".githooks/*")
DOCS_EXCLUDED = ("docs/conf.py", "docs/_ext/*", "docs/examples/*")
EXAMPLE_GLOBS = (
    "docs/examples/quickstart-*",
    "docs/examples/captures.toml",
    "docs/examples/captures/*",
    "docs/examples/bed/*",
)
"""Example projects a reader downloads: `make docs` checks them hostlessly and builds their
archives; the shared captures manifest and its artifacts are page inputs, as are the bed
overlays the captures run with. The other `docs/examples` trees stay excluded: tests and
doctests import them."""
DOCS_HARNESS = frozenset(
    {
        "scripts/refresh_docs_captures.py",
        "scripts/check_docs_examples.py",
        "scripts/build_example_archives.py",
        "scripts/lint_docs_citations.py",
    }
)
"""Scripts `make docs` runs over the docs and examples: a change to one also runs the docs lane."""
CAPTURE_INPUTS = (
    "docs/examples/getting-started*",
    "docs/examples/quickstart-*",
    "docs/examples/captures.toml",
    "docs/examples/captures/*",
    "docs/examples/bed/*",
    "scripts/refresh_docs_captures.py",
    "scripts/check_docs_examples.py",
)
"""Besides src/otto, what the docs example captures read: a change to any of these can move
what otto prints on the bed, so the tip runs their drift check. The runner imports the
examples checker's copy rule, so that script is an input too."""
CAPTURES_LANE = "make docs-captures-check"
WEB_EXCLUDED = ("web/src/api/*.gen.ts",)
MAX_SRC_PACKAGES = 2
"""More packages than this in one commit is `escalate`."""
_TEST_PATH_DEPTH = 3
"""Leading parts of a test path: `tests/<tree>/<pkg>`."""
_SRC_PATH_DEPTH = 3
"""Leading parts of a src path: `src/otto/<pkg>`."""

# src/otto/<pkg> or <module>.py stem → the test trees a leaf change runs at the commit
# (unit tree first; then the e2e/integration trees the spec's source→bed map names; then
# the standalone tests/unit modules named for the package, which no package tree holds).
UNIT_TREES = {
    "cli": [
        "tests/unit/cli",
        "tests/e2e/cli",
        "tests/unit/test_cli_exposed_output_dir.py",
        "tests/unit/test_utils_cli_markers.py",
    ],
    "host": [
        "tests/unit/host",
        "tests/integration/host",
        "tests/e2e/host",
        "tests/e2e/console",
        "tests/unit/test_host_pool_lease.py",
    ],
    "docker": [
        "tests/unit/docker",
        "tests/e2e/docker",
        "tests/integration/test_docker_build.py",
        "tests/integration/test_docker_compose.py",
        "tests/integration/test_docker_in_instruction.py",
        "tests/integration/test_docker_run_get_put.py",
        "tests/integration/test_docker_use_case.py",
        "tests/unit/test_docker_reap.py",
        "tests/unit/test_docker_reaper_scope.py",
    ],
    "link": [
        "tests/unit/link",
        "tests/e2e/test_link_impair_e2e.py",
        "tests/unit/test_chaos_link_lab_scope.py",
    ],
    "tunnel": [
        "tests/unit/tunnel",
        "tests/e2e/test_tunnel_e2e.py",
        "tests/e2e/test_tunnel_check_e2e.py",
        "tests/unit/test_tunnel_bed_guard.py",
    ],
    "coverage": [
        "tests/unit/cov",
        "tests/e2e/cov",
        "tests/integration/cov",
        "tests/unit/test_coverage_floors.py",
        "tests/unit/test_coverage_html_placement.py",
        "tests/unit/test_coverage_schema_preinit.py",
    ],
    "kmodcov": [
        "tests/unit/cov",
        "tests/e2e/cov",
        "tests/integration/cov",
        "tests/unit/test_collate_kmodcov_matrix.py",
        "tests/unit/test_kmodcov_export.py",
        "tests/unit/test_kmodcov_kernel_columns.py",
        "tests/unit/test_kmodcov_matrix.py",
        "tests/unit/test_provision_kmodcov_kernels.py",
        # a module under another package's tree, not found by name: it tests the
        # kmod tool kind `kmodcov` builds (the standalone-name pin cannot see it)
        "tests/unit/host/test_kmod_tool_kind.py",
    ],
    "vendoring": [
        "tests/unit/test_vendoring.py",
        "tests/unit/test_kmodcov_export.py",
        "tests/unit/cov/test_embedded_gcov_export.py",
    ],
    "monitor": [
        "tests/unit/monitor",
        "tests/e2e/monitor",
        "tests/unit/test_monitor_service_reads_no_lab.py",
    ],
    "suite": ["tests/unit/suite", "tests/e2e/suite"],
    "init": ["tests/unit/init", "tests/e2e/test_init_e2e.py"],
    "config": ["tests/unit/config", "tests/e2e/config"],
    "fleet": ["tests/unit/fleet", "tests/unit/test_lab_facade.py"],
    "inventory": ["tests/unit/inventory", "tests/e2e/config"],
    "labs": ["tests/unit/labs", "tests/e2e/config"],
    "lab": [
        "tests/unit/labs",
        "tests/e2e/config",
        "tests/unit/test_chaos_link_lab_scope.py",
        "tests/unit/test_lab_data_hops.py",
        "tests/unit/test_lab_facade.py",
        "tests/unit/test_monitor_service_reads_no_lab.py",
    ],
    "session": ["tests/unit/session", "tests/integration/host"],
    "logger": ["tests/unit/logger", "tests/integration/logger"],
    "check": ["tests/unit/check"],
    "creds": ["tests/unit/creds"],
    "env": [
        "tests/unit/env",
        "tests/unit/test_ci_color_env.py",
        "tests/unit/test_conftest_env_writes.py",
        "tests/unit/test_env_hermeticity.py",
        "tests/unit/test_env_strip_sites.py",
    ],
    "examples": ["tests/unit/examples"],
    "models": ["tests/unit/models"],
    "project": [
        "tests/unit/project",
        "tests/unit/test_project_instruction_table.py",
        # they test the project helpers in tests/_fixtures, not otto.project; they sit
        # here because their names carry the project token, which the package-token
        # rule matches
        "tests/unit/test_fake_project_fixture.py",
        "tests/unit/test_project_scaffold_fixture.py",
        "tests/unit/test_project_scaffold_policy.py",
    ],
    "reservations": ["tests/unit/reservations"],
    "testing": ["tests/unit/testing"],
    "_webassets": ["tests/unit/monitor", "tests/e2e/monitor"],
    # top-level modules, used everywhere: the whole unit tree, plus the bed tree their
    # stem names
    "console": ["tests/unit", "tests/e2e/console"],
    # the usable-terminal decision: the CLI's width default, the console pin and a
    # login's remote terminal size
    "_terminal": ["tests/unit", "tests/e2e/console", "tests/e2e/host/test_interact_e2e.py"],
    "result": ["tests/unit"],
    "errors": ["tests/unit"],
    "filesystem": ["tests/unit"],
    "instructions": ["tests/unit"],
    "instruction_catalog": ["tests/unit"],
    "layout": ["tests/unit"],
    "params": ["tests/unit"],
    "snmp": ["tests/unit"],
    "tls": ["tests/unit"],
    "utils": ["tests/unit"],
    "version": ["tests/unit"],
    "declared": ["tests/unit"],
    "__main__": ["tests/unit"],
}


SINGLE_PASS_DEPTH = {"stability-tunnel": ["CYCLES=2"]}
"""What a target needs beyond `COUNT=1` to run one pass, not a soak: the tunnel tests loop
internally (`CYCLES`, default 5), and `CYCLES=2` is the Makefile's documented smoke run."""


def stability_once(target: str) -> str:
    """Return the line that runs one stability lane once, never at its soak depth.

    `COUNT=1`, plus the target's :data:`SINGLE_PASS_DEPTH` when it soaks inside each test.
    Every stability line the oracle emits is spelled here, whichever row selects it. The
    soaks (the Makefile's defaults) never ride a gate (docs/contributing.md says where
    each one runs); a full gate a diff earns, or a stability test file or tree it
    changes, runs a single pass.
    """
    return " ".join(["make", target, "COUNT=1", *SINGLE_PASS_DEPTH.get(target, [])])


STABILITY_UNIT = stability_once("stability-unit")
STABILITY_UNIX = stability_once("stability-unix")
STABILITY_TUNNEL = stability_once("stability-tunnel")
STABILITY_EMBEDDED = stability_once("stability-embedded")
BED_LANES = {
    "host": ["make conformance-bed"],
    "link": [STABILITY_UNIX],
    "tunnel": [STABILITY_TUNNEL],
    "console": [STABILITY_EMBEDDED],
}
"""The lane lines a change to these sources adds to the tip and to a full commit column:
`make coverage-bed` selects by tier, and a change to these packages is what the beds'
own lanes exercise."""
GUARD_RIDERS = [
    "tests/unit/test_error_base.py",
    "tests/unit/test_declared_additions.py",
    "tests/unit/init/test_init_templates.py",
    "tests/unit/import_budget",
    "tests/unit/api_snapshot",
]
"""Tree-wide guards no package path selects; they ride every src-leaf commit's `quick`."""
MATRIX_READERS = [
    "tests/unit/test_collate_kmodcov_matrix.py",
    "tests/unit/test_kmodcov_matrix.py",
    "tests/unit/test_matrix_downgrades.py",
    "tests/unit/test_support_matrix.py",
]
"""The test modules that read a measured matrix; a measurement commit owes exactly these."""
MEASURED_ROWS = {
    "schemas/support_matrix.json": MATRIX_READERS,
    "schemas/kmodcov_matrix.json": MATRIX_READERS,
}
"""The bed and kmodcov lanes rewrite these with what they measured (the Makefile says review
and commit the diff); the commit is a tests-leaf change over the readers, never a full gate."""

_BUMP_CONFIG_VERSION = re.compile(r'^current_version = "([^"\n]+)"$', re.MULTILINE)
_LOCK_VERSION = re.compile(r'^name = "otto-sh"\nversion = "([^"\n]+)"$', re.MULTILINE)
_AS_OF_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_A_DATE = object()
"""Stands in for an ``as_of`` date, so two matrices compare equal across a re-date only."""


def _bumped_pyproject(base: str, head: str) -> bool:
    """Return whether *head* is *base* with exactly bump-my-version's pyproject edit applied.

    ``[[tool.bumpversion.files]]`` replaces ``version = "{old}"`` with
    ``version = "{new}"``, which also rewrites ``current_version``; this
    repeats that replacement on *base* and requires every other byte to match.
    Like bump-my-version's own search, it is unscoped: any other key whose
    text ends in ``version = "{old}"`` moves with it, so a key of that shape
    must not be added to ``pyproject.toml``.
    """
    old, new = _BUMP_CONFIG_VERSION.search(base), _BUMP_CONFIG_VERSION.search(head)
    if old is None or new is None or old[1] == new[1]:
        return False
    return base.replace(f'version = "{old[1]}"', f'version = "{new[1]}"') == head


def _bumped_lock(base: str, head: str) -> bool:
    """Return whether *head* is *base* with only otto-sh's own lock version moved."""
    old, new = _LOCK_VERSION.search(base), _LOCK_VERSION.search(head)
    if old is None or new is None or old[1] == new[1]:
        return False
    entry = 'name = "otto-sh"\nversion = "{}"'
    return base.replace(entry.format(old[1]), entry.format(new[1])) == head


def _undated(value: object) -> object:
    """Return *value* with each ``as_of`` that holds a date replaced by one placeholder.

    Only a date is replaced: an ``as_of`` that is missing, ``null`` or not a
    date stays as it is, so it compares unequal to a dated one.
    """
    if isinstance(value, dict):
        return {
            k: _A_DATE
            if k == "as_of" and isinstance(v, str) and _AS_OF_DATE.fullmatch(v)
            else _undated(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_undated(v) for v in value]
    return value


def _redated_matrix(base: str, head: str) -> bool:
    """Return whether *head* differs from *base* in nothing but ``as_of`` dates.

    The two are compared as parsed JSON, so a change of key order or
    whitespace alone also passes; the matrix readers parse it the same way.
    """
    try:
        return _undated(json.loads(base)) == _undated(json.loads(head))
    except ValueError:
        return False


RELEASE_OWNED: "dict[str, Callable[[str, str], bool]]" = {
    "pyproject.toml": _bumped_pyproject,
    "uv.lock": _bumped_lock,
    "schemas/support_matrix.json": _redated_matrix,
    "schemas/kmodcov_matrix.json": _redated_matrix,
}
"""The non-code edits a release makes, each recognised by its content, never by path alone.

``make release`` re-dates the two matrices (``release-matrix``,
``release-kmodcov-matrix``) and bumps the version in ``pyproject.toml`` and
``uv.lock`` after its test stages have passed on the tree before them, so
none of these edits can change a test's outcome. A change that passes its
check is classed ``none``: it adds no lane and no stamp, and the push gate
does not ask the release to re-run coverage for its own commits (#660). Any
other change to these files (a dependency, a tool setting, a verdict) is
classified as before.

The checks read content, not who made the commit: a hand-made version-only
edit passes too, and CI's locked sync and build catch a pyproject and lock
that disagree. They exempt only these files: commits not yet pushed before
the release still owe their own lanes on the released tree.
"""


def release_owned(change: Change, read: Reader, base_rev: str, head_rev: str) -> bool:
    """Return whether *change* is one of :data:`RELEASE_OWNED`'s non-code release edits."""
    check = RELEASE_OWNED.get(change.path)
    if check is None or change.status != "M":
        return False
    base, head = read(base_rev, change.path), read(head_rev, change.path)
    return base is not None and head is not None and check(base, head)


_GATE_PIN_MODULES = frozenset({"scripts.gate_scope", "scripts.gate_tip"})


def _imported_names(module: "ast.Module") -> "Iterator[str]":
    """Yield every dotted name *module* imports, in the three absolute forms, at any depth.

    `import a.b [as c]` yields `a.b`; `from a.b import c` yields `a.b` and `a.b.c` (the
    second so `from otto import host` names `otto.host`, as `import otto.host` does). A
    relative import names nothing: :func:`_imports_a_gate_pin` asks about absolute modules.
    """
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def _imports_a_gate_pin(source: str) -> bool:
    """Report whether *source* imports the oracle or the runner; unparsable is False."""
    tree = _parse(source)
    return tree is not None and any(n in _GATE_PIN_MODULES for n in _imported_names(tree))


def matrix_readers(sources: "dict[str, str]") -> "list[str]":
    """Return the test modules among *sources* that read a measured matrix.

    A module names a matrix file either to read it or to pin this oracle's row for it;
    the pins import the oracle or the runner, the readers do not. An unparsable module
    that names a matrix counts as a reader (judged conservatively, as `_parse` callers do).
    """
    names = [name.rsplit("/", 1)[-1] for name in MEASURED_ROWS]
    return sorted(
        path
        for path, text in sources.items()
        if _is_test_module(path)
        and any(name in text for name in names)
        and not _imports_a_gate_pin(text)
    )


# (glob, commit-time commands, tip-time commands): a directory-stamped tree — every item is
# a lane's, so `quick` is never added. First match wins. [FULL] = the commit column is full.
# A row may name several lanes; each column is named even when it is the full gate. Only a
# tip line whose goals are all BED_GOALS reaches the tip (`commands`).
SPECIAL_TREES = [
    # The bed-opener witness carries `conformance_bed`, which `make conformance` excludes
    # (`make conformance`'s selector): the bed lane is its only lane, at the commit too.
    (
        "tests/conformance/test_bed_opener_witness.py",
        ["make conformance-bed"],
        ["make conformance-bed"],
    ),
    ("tests/conformance/*", ["make conformance"], ["make conformance-bed"]),
    ("tests/busybox/*", ["make busybox"], ["make busybox"]),
    # The #386 bed proof (`release_proof`): its own lane, run when the file itself changes
    # and once per release by `make release-campaign`; no tip runs it.
    (
        "tests/integration/host/test_console_release_bed.py",
        ["make console-release-bed"],
        [],
    ),
    # its items are integration-marked: the tip's `make coverage-bed` runs them
    ("tests/integration/busybox_bed/*", ["make coverage-unix"], []),
    (
        "tests/e2e/monitor/dashboard/test_replay_soak.py",
        ["make dashboard-soak"],
        ["make dashboard-soak"],
    ),
    (
        "tests/e2e/tunnel_stability/*",
        [STABILITY_TUNNEL],
        [STABILITY_TUNNEL],
    ),
    (
        "tests/e2e/chaos/*",
        ["make chaos", "make chaos-embedded"],
        ["make chaos", "make chaos-embedded"],
    ),
]
# A MIXED file: the lane selects its marked tests, `quick` over the surviving root runs the
# rest — when there IS a rest (else quick's legs would select nothing and go red). The
# stability lanes name files (the `stability-*` targets).
STABILITY_FILES = {
    "tests/unit/host/test_app_shell_concurrency.py": [STABILITY_UNIT],
    "tests/unit/host/test_session_concurrency.py": [STABILITY_UNIT],
    "tests/unit/host/test_unix_host.py": [STABILITY_UNIT],
    "tests/unit/monitor/test_collector_tunnel_soak.py": [STABILITY_UNIT],
    "tests/integration/host/test_app_shell_stability_integration.py": [STABILITY_UNIX],
    "tests/integration/host/test_host_stability_contract.py": [
        STABILITY_UNIX,
        STABILITY_EMBEDDED,
    ],
    "tests/integration/host/test_proxy_user_stability_integration.py": [STABILITY_UNIX],
    "tests/integration/host/test_session_stability_integration.py": [STABILITY_UNIX],
}
LANE_MARKERS = {
    "kmodcov": "make kmodcov",
    "browser": "make dashboard",
    "busybox": "make busybox",
}
"""A changed test file carrying one of these marks runs the lane too, wherever it lives."""
# The marks a lane row's selector covers, for "is anything left for quick".
LANE_COVERS = {
    STABILITY_UNIT: {"concurrency"},
    STABILITY_UNIX: {"stability"},
    STABILITY_EMBEDDED: {"stability"},
    "make kmodcov": {"kmodcov"},
    "make dashboard": {"browser"},
    "make busybox": {"busybox"},
}
QUICK_EXCLUDED = frozenset(
    {
        "stability",
        "busybox",
        "conformance",
        "kmodcov",
        "release_proof",
        "browser",
        "interpreter_agnostic",
    }
)
"""The marks both `make quick` legs deselect (M_QUICK; pinned against the Makefile)."""
STATIC_LANES = ["make check-python", "make check-ts", "make docs"]
"""The full commit column's lanes that run no test suite: lint and types (Python, then
TypeScript), then docs. Together the first two are `make check`; they are spelled apart
because the tip stamps `check-python` and runs no TypeScript lane (CI's `check-ts` job does)."""
TEST_SUITE_LANES = ["make coverage-bed"]
"""The full commit column's lanes that run test suites; every static lane precedes them.

The hostless half (`tests_hostless-3.10` … `3.14`) is not here: it is the pull request's
CI, which runs before the landing (#795). The local gate owns what CI cannot run: the bed
half of the coverage selection and the diff's bed lanes."""
FULL_GATE = STATIC_LANES + TEST_SUITE_LANES
"""The full commit column, cheapest first: the static lanes (minutes) before the bed half,
so that run by hand a lint, type or docs failure costs minutes, never the bed run's. An
`escalate` or `full` commit, or one with a `full_at_commit` reason, runs it; the tip never
does (`commands` composes the tip from `STATIC_GOALS`, `BED_GOALS` and the live line)."""

PYTHON_CLASSES = [TESTS_LEAF, TESTS_STRUCTURAL, SRC_LEAF, SCRIPTS, ESCALATE, FULL]
"""The classes whose tip earns the static lanes (`static_reach`)."""
STATIC_GOALS = ["check-python", "collect-check"]
"""Tree-pure, hostless, stamped: the tip runs them for any Python-touching diff."""
BED_GOALS = [
    "coverage-bed",
    "conformance-bed",
    "stability-unix",
    "stability-tunnel",
    "stability-embedded",
    "docs-captures-check",
    "kmodcov",
    "chaos",
    "chaos-embedded",
]
"""The lanes that take the bed lease, stamped: the tip runs the bed half (`coverage-bed`)
when the diff reaches it (`bed_reach`), plus the diff's bed lanes and bed special rows."""
COMMIT_ONLY_GOALS = [
    "quick",
    "quick-guards",
    "check-ts",
    "test-ts",
    "docs",
    "lint-arch",
    "ruff",
    "ty",
    "conformance",
    "busybox",
    "coverage-unix",
    "dashboard",
    "dashboard-soak",
    "stability-unit",
    "console-release-bed",
]
"""What the commit column may emit beyond the tip's goals: a working check, never stamped,
never in a tip. Their landing venues are CI's jobs and nightly's."""
RANGE_TARGETS = ["check-breaking"]
"""The emitted targets that run a history check, whose RANGE defaults to origin/main..HEAD."""
LIVE_GOALS = RANGE_TARGETS
"""History-dependent goals: run by the push itself over the pushed range, never stamped."""
STAMPED_GOALS = STATIC_GOALS + BED_GOALS
"""Every goal a tip can emit that the runner stamps by tree."""


class UnknownGoalError(ValueError):
    """The oracle met a line whose goals it does not classify."""


def goals(line: str) -> "list[str]":
    """Return the goals of one oracle line: make targets, or the uv tool (ruff, ty).

    The commit column emits ruff and ty lines per file; every goal must be one the oracle
    classifies (stamped, live or commit-only), or the line is refused.
    """
    words = shlex.split(line)
    if words[:1] == ["make"]:
        found = [w for w in words[1:] if "=" not in w]
    elif words[:2] == ["uv", "run"] and words[2:3] in (["ruff"], ["ty"]):
        found = [words[2]]
    else:
        raise UnknownGoalError(f"the oracle emitted a lane it does not classify: {line}")
    known = STAMPED_GOALS + LIVE_GOALS + COMMIT_ONLY_GOALS
    unknown = [g for g in found if g not in known]
    if not found or unknown:
        raise UnknownGoalError(f"the oracle emitted a lane it does not classify: {line}")
    return found


def is_live(line: str) -> bool:
    """Return whether any goal of *line* is live (run by the push, never stamped)."""
    return any(g in LIVE_GOALS for g in goals(line))


def is_bed(line: str) -> bool:
    """Return whether any goal of *line* takes the bed lease (a goal in BED_GOALS)."""
    return any(g in BED_GOALS for g in goals(line))


_RANGE = re.compile(r"\s+RANGE=\S+$")


def lane_key(line: str) -> str:
    """Return the oracle line minus its RANGE decoration: base-independent by construction."""
    return _RANGE.sub("", line)


def _match(path: str, globs: "tuple[str, ...]") -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def _is_test_module(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= _TEST_PATH_DEPTH
        and parts[0] == "tests"
        and parts[-1].startswith("test_")
        and path.endswith(".py")
    )


def classify_path(path: str) -> "str | None":
    """Return the class of one path by its location alone; None is "no row" (→ full)."""
    if _match(path, NONE_GLOBS):
        return NONE
    if (
        path in TRIGGER_FILES
        or path.rsplit("/", 1)[-1] == "conftest.py"
        or _match(path, TRIGGER_GLOBS)
    ):
        return ESCALATE
    if path.startswith("docs/"):
        excluded = _match(path, DOCS_EXCLUDED) and not _match(path, EXAMPLE_GLOBS)
        return None if excluded else DOCS
    if "/" not in path and path.endswith(".md"):
        return DOCS  # AGENTS.md never reaches here: NONE_GLOBS matched it first
    if path.startswith("web/src/"):
        return None if _match(path, WEB_EXCLUDED) else WEB
    if path in MEASURED_ROWS:
        return TESTS_LEAF
    if path.startswith("tests/"):
        return TESTS_LEAF if _is_test_module(path) else None
    if path.startswith("src/otto/"):
        return SRC_LEAF if "/" in path[len("src/otto/") :] or path.endswith(".py") else ESCALATE
    if path.startswith("scripts/"):
        return SCRIPTS
    return None


_UNPARSABLE = "<unparsable>"


def _parse(source: "str | None") -> "ast.Module | None":
    if source is None:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: a null byte (3.10, 3.11)
        return None


def _tests_in(
    body: "list[ast.stmt]", prefix: str = ""
) -> "Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]]":
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            yield prefix + node.name, node
            yield from _tests_in(node.body, prefix + node.name + ".")


def test_identities(source: str) -> "set[str]":
    """Return the class-qualified names of every test function and test class in `source`."""
    tree = _parse(source)
    return set() if tree is None else {name for name, _node in _tests_in(tree.body)}


def _mark_name(node: ast.expr) -> "str | None":
    """`pytest.mark.x`, `mark.x`, called or not: the mark's name; anything else: None."""
    target = node
    while isinstance(target, ast.Call):
        target = target.func
    if not isinstance(target, ast.Attribute):
        return None
    owner = target.value
    if isinstance(owner, ast.Name) and owner.id == "mark":
        return target.attr
    if (
        isinstance(owner, ast.Attribute)
        and owner.attr == "mark"
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "pytest"
    ):
        return target.attr
    return None


def _bindings(tree: ast.Module) -> "dict[str, ast.expr]":
    """Map every module-level `NAME = value` (annotated too) to its value; the last one wins."""
    bindings: "dict[str, ast.expr]" = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id != "pytestmark":
            bindings[target.id] = value
    return bindings


def _aliases(tree: ast.Module) -> "dict[str, ast.expr]":
    """Map module-level `NAME = pytest.mark.x[(...)]` bindings to their mark expressions."""
    return {name: v for name, v in _bindings(tree).items() if _mark_name(v) is not None}


def _resolve(node: ast.expr, aliases: "dict[str, ast.expr]") -> "ast.expr | None":
    """Return the mark expression `node` stands for, else None.

    An alias applied bare is its definition; an alias applied as a CALL keeps the call's
    own arguments (`@P("x", [1, 2])` with `P = pytest.mark.parametrize` reads as
    `pytest.mark.parametrize("x", [1, 2])`), so an edit to them is an edit to the mark.
    """
    if _mark_name(node) is not None:
        return node
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in aliases:
        return ast.Call(func=aliases[node.func.id], args=node.args, keywords=node.keywords)
    return None


def _inline(node: ast.AST, bindings: "dict[str, ast.expr]", seen: "frozenset[str]") -> ast.AST:
    """Replace, in place, each module-level name `node` reads with that name's value."""
    if (
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in bindings
        and node.id not in seen  # a cycle stops at its first repeat
    ):
        return _inline(copy.deepcopy(bindings[node.id]), bindings, seen | {node.id})
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            setattr(
                node,
                field,
                [_inline(v, bindings, seen) if isinstance(v, ast.AST) else v for v in value],
            )
        elif isinstance(value, ast.AST):
            setattr(node, field, _inline(value, bindings, seen))
    return node


def _whole_text(value: ast.expr, bindings: "dict[str, ast.expr]") -> str:
    """Return the value's source with the module names it reads inlined (raw where none)."""
    return ast.unparse(_inline(copy.deepcopy(value), bindings, frozenset()))


@dataclass
class _Marks:
    """One identity's marks, in three views.

    Their text (the structural view), every name that can reach an item (the routing view)
    and the names that reach EVERY item (whether anything is left for quick).
    """

    signature: "list[str]"
    names: "set[str]"
    covering: "set[str]"

    def add(self, node: ast.expr) -> None:
        self.signature.append(ast.unparse(node))
        name = _mark_name(node)
        if name is not None:
            self.names.add(name)
            self.covering.add(name)

    def unreadable(self, text: str) -> None:
        self.signature.append(text)
        self.names.add(_UNPARSABLE)
        self.covering.add(_UNPARSABLE)


def _is_param(node: ast.expr) -> bool:
    """Report whether `node` is a `pytest.param(...)` (or a bare imported `param(...)`) call."""
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id == "param"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "param"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
    )


def _param_marks(param: ast.Call, aliases: "dict[str, ast.expr]") -> "set[str]":
    """Return the mark names a `pytest.param(..., marks=...)` carries (one, a list or a tuple)."""
    names: "set[str]" = set()
    for keyword in param.keywords:
        if keyword.arg != "marks":
            continue
        value = keyword.value
        for element in value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]:
            resolved = _resolve(element, aliases)
            name = None if resolved is None else _mark_name(resolved)
            if name is not None:
                names.add(name)
    return names


def _add_parameter_marks(marks: _Marks, mark: ast.expr, aliases: "dict[str, ast.expr]") -> None:
    """Route the marks a `parametrize`'s `pytest.param(marks=...)` cases carry.

    Every such mark joins the routing names (its items are that lane's); it reaches every
    item of the identity only when the case list is a literal whose every case carries it.
    The decorator's own text already holds the cases, so the signature needs nothing more.
    """
    if _mark_name(mark) != "parametrize" or not isinstance(mark, ast.Call):
        return
    for node in ast.walk(mark):
        if isinstance(node, ast.Call) and _is_param(node):
            marks.names |= _param_marks(node, aliases)
    cases = mark.args[1] if len(mark.args) > 1 else None
    cases = next((k.value for k in mark.keywords if k.arg == "argvalues"), cases)
    if isinstance(cases, (ast.List, ast.Tuple)) and cases.elts:
        per_case = [
            _param_marks(c, aliases) if isinstance(c, ast.Call) and _is_param(c) else set()
            for c in cases.elts
        ]
        marks.covering |= set.intersection(*per_case)


def _add_pytestmark(
    marks: _Marks,
    body: "list[ast.stmt]",
    aliases: "dict[str, ast.expr]",
    bindings: "dict[str, ast.expr]",
) -> None:
    """Read the `pytestmark` assignments in `body`, element by element when each is a mark.

    Any other shape — `A + B`, `[*X, ...]`, `helper()`, an element that is not a mark —
    enters the signature WHOLE, as its source with the module names it reads inlined (raw
    text where it reads none), and adds the `_UNPARSABLE` mark name: an edit to it is
    structural, and routing treats it as a mark that may be a lane's. Conservative by
    design: a shape the walker cannot follow never routes as if it carried nothing.
    """
    for node in body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        resolved = [_resolve(e, aliases) for e in elements]
        readable = [mark for mark in resolved if mark is not None]
        if len(readable) < len(resolved):
            marks.unreadable(_whole_text(value, bindings))
            continue
        for mark in readable:
            marks.add(mark)


def _add_decorators(
    marks: _Marks,
    decorators: "list[ast.expr]",
    aliases: "dict[str, ast.expr]",
    bindings: "dict[str, ast.expr]",
) -> None:
    """Read a test's decorators into `marks`.

    A mark (direct or aliased) counts, with its parameter-level marks; a decorator naming a
    module-level binding that is not a mark is unreadable (as in `_add_pytestmark`); any
    other decorator, imported or defined here, is not a mark.
    """
    for decorator in decorators:
        mark = _resolve(decorator, aliases)
        if mark is not None:
            marks.add(mark)
            _add_parameter_marks(marks, mark, aliases)
            continue
        named = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(named, ast.Name) and named.id in bindings:
            marks.unreadable(_whole_text(decorator, bindings))


def _marks_by_identity(tree: ast.Module) -> "dict[str, _Marks]":
    """Collect marks per test identity ("pytestmark" is the module's)."""
    found: "dict[str, _Marks]" = {}
    bindings = _bindings(tree)
    aliases = _aliases(tree)
    module = _Marks([], set(), set())
    _add_pytestmark(module, tree.body, aliases, bindings)
    if module.signature:
        found["pytestmark"] = module
    for identity, node in _tests_in(tree.body):
        marks = _Marks([], set(), set())
        _add_decorators(marks, node.decorator_list, aliases, bindings)
        if isinstance(node, ast.ClassDef):
            _add_pytestmark(marks, node.body, aliases, bindings)
        if marks.signature:
            found[identity] = marks
    return found


def marker_names(source: "str | None") -> "dict[str, set[str]]":
    """Mark NAMES per identity — the routing view (parameter-level marks included)."""
    if source is None:
        return {}
    tree = _parse(source)
    if tree is None:
        return {_UNPARSABLE: {_UNPARSABLE}}
    return {identity: marks.names for identity, marks in _marks_by_identity(tree).items()}


def _covering_names(source: "str | None") -> "dict[str, set[str]]":
    """Mark names per identity that reach EVERY item the identity collects."""
    tree = _parse(source)
    if tree is None:
        return {_UNPARSABLE: {_UNPARSABLE}}
    return {identity: marks.covering for identity, marks in _marks_by_identity(tree).items()}


def marker_signature(source: "str | None") -> "dict[str, list[str]]":
    """Return normalized mark TEXT per identity (sorted `ast.unparse`): the structural view."""
    if source is None:
        return {}
    tree = _parse(source)
    if tree is None:
        return {_UNPARSABLE: [_UNPARSABLE]}
    signature = {
        identity: sorted(marks.signature) for identity, marks in _marks_by_identity(tree).items()
    }
    # Conservative by design: the definition of every alias the module READS joins the
    # module-level slot, so an edit to one is structural even where the walker cannot follow
    # the use (a `marks=` keyword, a helper). An alias nothing reads changes no test.
    read = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    alias_text = [
        f"alias {name} = {ast.unparse(v)}" for name, v in _aliases(tree).items() if name in read
    ]
    if alias_text:
        signature["pytestmark"] = sorted(signature.get("pytestmark", []) + alias_text)
    return signature


def is_structural(base: "str | None", head: "str | None") -> bool:
    """Report a removed or renamed test, a mark edit (arguments too) or an unparsable module."""
    if base is None:
        return _parse(head) is None if head is not None else False
    if head is None:
        return True
    if _parse(base) is None or _parse(head) is None:
        return True
    return not test_identities(base) <= test_identities(head) or marker_signature(
        base
    ) != marker_signature(head)


def carries_family_mark(source: "str | None") -> bool:
    """Report whether every test in `source` carries `interpreter_agnostic`.

    The module-level `pytestmark`, or each test's own (or class's) mark: either way quick's
    legs deselect the whole module, and `make quick-guards` is what runs it.
    """
    if "interpreter_agnostic" in marker_names(source).get("pytestmark", set()):
        return True
    return (
        source is not None
        and bool(test_identities(source))
        and not _has_tests_outside(source, {"interpreter_agnostic"})
    )


def _all_marks(source: "str | None") -> "set[str]":
    return set().union(*marker_names(source).values()) if source is not None else set()


def _has_tests_outside(source: "str | None", covered: "set[str]") -> bool:
    """Report whether some test's effective marks (own, class, module) miss every `covered` mark.

    A parameter-level mark counts only where every case of the identity carries it.
    """
    tree = _parse(source)
    if tree is None:
        return True  # judged conservatively: run quick too
    names = _covering_names(source)
    module = names.get("pytestmark", set())
    for identity, _node in _tests_in(tree.body):
        if isinstance(_node, ast.ClassDef):
            continue
        parts = identity.split(".")
        effective = set(module)
        for depth in range(1, len(parts) + 1):
            effective |= names.get(".".join(parts[:depth]), set())
        if not effective & covered:
            return True
    return False


def _package_of_test(path: str) -> str:
    """tests/<tree>/<pkg>/… → tests/<tree>/<pkg>; a tree-level file is its own path."""
    parts = path.split("/")
    return "/".join(parts[:_TEST_PATH_DEPTH]) if len(parts) > _TEST_PATH_DEPTH else path


def _within(path: str, roots: "Iterable[str]") -> bool:
    """Report whether `path` is one of `roots` or lies under one (roots carry no trailing slash)."""
    return any(path == r or path.startswith(r + "/") for r in roots)


def _holds_a_test(root: str, head_paths: "set[str]") -> bool:
    """Report whether a quick root is a surviving test module or contains one."""
    if root in head_paths:
        return _is_test_module(root)
    return any(_is_test_module(p) and _within(p, [root]) for p in head_paths)


class _QuickRoots:
    """Which quick roots hold, at head, a test module quick's legs select from.

    A surviving filename is not enough: a module emptied of its tests, or one whose every
    test carries a mark M_QUICK excludes, gives both legs nothing to run. Each module is read
    through the reader once; one the reader cannot return is judged by its filename alone.
    """

    def __init__(self, read: Reader, head_rev: str, head_paths: "set[str]") -> None:
        self._read = read
        self._head_rev = head_rev
        self._modules = sorted(p for p in head_paths if _is_test_module(p))
        self._selects: "dict[str, bool]" = {}

    def _selected_from(self, path: str) -> bool:
        if path not in self._selects:
            source = self._read(self._head_rev, path)
            self._selects[path] = _has_tests_outside(source, set(QUICK_EXCLUDED))
        return self._selects[path]

    def runnable(self, root: str) -> bool:
        """Report whether `root` (a module or a directory) holds a module quick selects from."""
        return any(self._selected_from(p) for p in self._modules if _within(p, [root]))


def _surviving_root(path: str, runnable: "Callable[[str], bool]") -> "str | None":
    """Return the nearest enclosing root (package, tree, `tests`) quick can run, else None."""
    parts = path.split("/")
    candidates = [_package_of_test(path), "/".join(parts[:2]), parts[0]]
    for candidate in candidates:
        if runnable(candidate):
            return candidate
    return None


def _src_key(path: str) -> str:
    parts = path.split("/")  # src/otto/<pkg>/… or src/otto/<module>.py
    return parts[2] if len(parts) > _SRC_PATH_DEPTH else parts[2].removesuffix(".py")


def _is_src(path: str) -> bool:
    """Report whether a src/otto path is a package member or a top-level module (never py.typed)."""
    return path.startswith("src/otto/") and (
        "/" in path[len("src/otto/") :] or path.endswith(".py")
    )


def _special_lane(path: str, source: "str | None") -> "list[Any] | None":
    """[commit commands, tip commands, covered marks, rule kind] for a file a lane owns, else None.

    A [FULL] commit column is a `full_at_commit` reason, which also sends the tip to the
    bed half (`bed_reach`); the tip column then names nothing of its own.

    Kind: "tree" (SPECIAL_TREES — never quick), "file" (STABILITY_FILES), "unreadable" (a
    pytestmark or decorator the walker cannot read → full), "mark" (LANE_MARKERS), "stray"
    (a stability/chaos mark outside every lane's roots → full).
    A "file" or "mark" row adds quick over the surviving root only when the file holds a
    test the covered marks do not reach.
    """
    for glob, at_commit, at_tip in SPECIAL_TREES:
        if fnmatch.fnmatchcase(path, glob):
            return [list(at_commit), list(at_tip), set(), "tree"]
    if path in STABILITY_FILES:
        lanes = STABILITY_FILES[path]
        return [
            list(lanes),
            list(lanes),
            set().union(*(LANE_COVERS[lane] for lane in lanes)),
            "file",
        ]
    marks = _all_marks(source)
    if _UNPARSABLE in marks and _parse(source) is not None:
        # a pytestmark or decorator the walker cannot read may hold a lane mark that quick
        # deselects; a module that does not parse is left to quick, whose collection fails
        return [[FULL], [], set(), "unreadable"]
    lanes = [lane for mark, lane in LANE_MARKERS.items() if mark in marks]
    if lanes:
        return [lanes, list(lanes), set().union(*(LANE_COVERS[lane] for lane in lanes)), "mark"]
    if marks & {"stability", "chaos"}:
        return [[FULL], [], set(), "stray"]
    return None


def _owning_tree_lanes(path: str) -> "list[list[Any]]":
    """Return the lane rows a conftest or helper (a non-test-module path) under a lane tree feeds.

    Each directory row (`<root>/*`) whose root holds `path` gives a "tree" lane row, as a
    test module there would: the path keeps its own class (a conftest escalates), and the
    tree's lanes join both columns because the full gate excludes that tier; at the tip only
    the lanes whose goals are all bed goals survive (`commands`). File rows name one module
    only and feed nothing else. A docs-harness script feeds the docs lane at the commit (the
    docs build is CI's at the tip).
    """
    if path in DOCS_HARNESS:
        return [[["make docs"], [], set(), "tree"]]
    if not path.startswith("tests/") or _is_test_module(path):
        return []
    return [
        [list(at_commit), list(at_tip), set(), "tree"]
        for glob, at_commit, at_tip in SPECIAL_TREES
        if glob.endswith("/*") and path.startswith(glob[:-1])
    ]


def _captures_lane(path: str) -> "list[str]":
    """Return the captures drift check when the docs example captures read *path*."""
    return [CAPTURES_LANE] if _is_src(path) or _match(path, CAPTURE_INPUTS) else []


def _route_lane(
    path: str,
    source: "str | None",
    lane: "list[Any]",
    special: "set[str]",
    special_tip: "set[str]",
    full_commit: "list[str]",
) -> bool:
    """Record a lane row's commands and full-gate reasons; return True when quick must run too."""
    at_commit, at_tip, covered, kind = lane
    if at_commit == [FULL]:
        reason = {
            "stray": "stability/chaos mark outside every lane's roots",
            "unreadable": "unreadable pytestmark",
        }.get(kind, "full at the commit")
        full_commit.append(f"{path}: {reason}")
    else:
        special.update(at_commit)
    special_tip.update(at_tip)
    return kind in {"file", "mark"} and _has_tests_outside(source, covered)


def _outermost(roots: "set[str]") -> "list[str]":
    """Return `roots` sorted, without any root nested under another (the outer one runs it)."""
    return sorted(r for r in roots if not any(r.startswith(o + "/") for o in roots if o != r))


def _present_paths(changes: "list[Change]") -> "list[str]":
    """Return the sorted paths among *changes* that still exist after them (not deleted)."""
    return sorted({c.path for c in changes if c.status != "D"})


def _route_measured(
    path: str,
    read: Reader,
    head_rev: str,
    head_paths: "set[str]",
    add_root: "Callable[[str], None]",
    guards: "set[str]",
    full_commit: "list[str]",
) -> None:
    """Route a measured matrix to its surviving readers: family to guards, others to quick."""
    readers = [p for p in MEASURED_ROWS[path] if p in head_paths]
    if not readers:
        full_commit.append(f"{path}: no surviving reader")
    for reader in readers:
        if carries_family_mark(read(head_rev, reader)):
            guards.add(reader)
        else:
            add_root(reader)


def full_gate(classes: "set[str]", full_at_commit: "list[str]") -> bool:
    """Decide whether the commit column is the full gate (:data:`FULL_GATE`).

    `escalate` and `full`, or any `full_at_commit` reason. The tip has no full gate: it is
    composed from :func:`static_reach` and :func:`bed_reach` (`commands`).
    """
    return bool(classes & {ESCALATE, FULL}) or bool(full_at_commit)


def in_bed_half(path: str, source: "str | None") -> bool:
    """Report whether a test module is in the bed half of the coverage selection.

    Under `tests/integration` (its conftest marks every item there), or any of its tests
    carries an `integration` or `embedded` mark in the routing view (module, class,
    function and parameter marks): a module mixing hostless and bed tests is in the bed half.
    """
    return _within(path, ["tests/integration"]) or bool(
        {"integration", "embedded"} & _all_marks(source)
    )


def static_reach(scope: Scope) -> bool:
    """Report whether the tip earns the static lanes: a Python class or any `.py` path.

    A Python file under `docs/examples/` is classed `docs` for its bed selection and still
    gets the cheap static lanes.
    """
    return bool(set(scope.classes) & set(PYTHON_CLASSES)) or any(
        p.endswith(".py") for p in scope.changed_paths
    )


def bed_reach(scope: Scope) -> bool:
    """Report whether the tip earns the bed half (`make coverage-bed`).

    A source, script or escalating change; a changed test module in the bed half; or any
    `full_at_commit` reason (an unreadable mark, no surviving root), the conservative
    fallback. A hostless test change takes no lease.
    """
    return (
        bool(set(scope.classes) & {SRC_LEAF, SCRIPTS, ESCALATE, FULL})
        or bool(scope.bed_modules)
        or bool(scope.full_at_commit)
    )


def classify(
    changes: "list[Change]",
    read: Reader,
    base_rev: str,
    head_rev: str,
    head_paths: "set[str]",
) -> Scope:
    """Classify `changes` between `base_rev` and `head_rev`; `head_paths` are the files at head.

    A release's own non-code edits (:data:`RELEASE_OWNED`) are classed ``none``
    and take no further part. One Scope serves both columns: `commands` composes each.
    """
    kept = [c for c in changes if not release_owned(c, read, base_rev, head_rev)]
    classes: "set[str]" = {NONE} if len(kept) < len(changes) else set()
    changes = kept
    quick: "set[str]" = set()
    guards: "set[str]" = set()
    packages: "set[str]" = set()
    paths: "set[str]" = set()
    special: "set[str]" = set()
    special_tip: "set[str]" = set()
    bed: "set[str]" = set()
    unknown: "list[str]" = []
    full_commit: "list[str]" = []
    test_sources: "dict[str, str | None]" = {}

    runnable = _QuickRoots(read, head_rev, head_paths).runnable

    def add_root(path: str) -> None:
        root = _surviving_root(path, runnable)
        if root is None:
            full_commit.append(f"{path}: no surviving test root")
        else:
            quick.add(root)

    for change in changes:
        path = change.path
        paths.add(path)
        if _is_src(path):
            bed.update(BED_LANES.get(_src_key(path), []))
        special_tip.update(_captures_lane(path))
        cls = classify_path(path)
        for lane in _owning_tree_lanes(path):
            _route_lane(path, None, lane, special, special_tip, full_commit)
        if cls is None:
            unknown.append(path)
            full_commit.append(f"no row: {path}")
            classes.add(FULL)
            continue
        if path in MEASURED_ROWS:
            _route_measured(path, read, head_rev, head_paths, add_root, guards, full_commit)
            classes.add(TESTS_LEAF)
            continue
        if cls == TESTS_LEAF:
            head = None if change.status == "D" else read(head_rev, path)
            base = None if change.status == "A" else read(base_rev, path)
            if is_structural(base, head):
                cls = TESTS_STRUCTURAL
            source = head if head is not None else base
            test_sources[path] = source
            lane = _special_lane(path, source)
            if lane is not None:
                if _route_lane(path, source, lane, special, special_tip, full_commit):
                    add_root(path)
                classes.add(cls)
                continue
            if carries_family_mark(source):
                guards.add(path)
            else:
                add_root(path)
        elif cls == SRC_LEAF:
            key = _src_key(path)
            packages.add(key)
            trees = UNIT_TREES.get(key)
            if trees is None:
                unknown.append(path)
                full_commit.append(f"no row: {path}")
                classes.add(FULL)
                continue
            alive = [t for t in trees if runnable(t)]
            if not alive:
                full_commit.append(f"{path}: none of its test trees survives at head")
            quick.update(alive)
            quick.update(r for r in GUARD_RIDERS if runnable(r))
        elif cls == ESCALATE and _is_src(path) and "/" in path[len("src/otto/") :]:
            packages.add(_src_key(path))
        classes.add(cls)
    if len(packages) > MAX_SRC_PACKAGES:
        classes.discard(SRC_LEAF)
        classes.add(ESCALATE)
        full_commit.append(f"{len(packages)} src packages (> {MAX_SRC_PACKAGES})")
    if TESTS_STRUCTURAL in classes:
        classes.discard(TESTS_LEAF)  # structural runs every leaf line and more
    if full_gate(classes, full_commit):
        quick.clear()
    return Scope(
        sorted(classes),
        _outermost(quick),
        sorted(guards),
        sorted(packages),
        _present_paths(changes),
        sorted(paths),
        sorted(special),
        sorted(special_tip),
        sorted(bed),
        sorted(unknown),
        full_commit,
        sorted(p for p, source in test_sources.items() if in_bed_half(p, source)),
    )


BUILD_FILES = frozenset(
    {"Makefile", "noxfile.py", "pyproject.toml", ".coveragerc", ".github/workflows/ci.yml"}
)


def _ty_line(path: str) -> str:
    """Return the ty command for a path; `scripts/` is outside ty's root and needs the repo root."""
    if path.startswith("scripts/"):
        return f"uv run ty check --extra-search-path . {path}"
    return f"uv run ty check {path}"


def _with_range(line: str, base: str, tip_rev: str) -> str:
    """Carry the range to a `make` line whose target runs a history check.

    The default display pair (`base` origin/main, `tip_rev` HEAD) stays bare, the target's
    own default; any other pair is spelled exactly, so a `plan`'s live line names its shas.
    """
    target = line.split()[1] if line.startswith("make ") else None
    if target not in RANGE_TARGETS or (base == "origin/main" and tip_rev == "HEAD"):
        return line
    return f"{line} RANGE={base}..{tip_rev}"


def _dedupe(lines: "list[str]") -> "list[str]":
    return list(dict.fromkeys(lines))


def _commit_lines(scope: Scope) -> "list[str]":
    """Return the commit column's lines after the live one, before deduplication."""
    classes = set(scope.classes)
    python = [p for p in scope.changed_files if p.endswith(".py")]
    running = classes & {TESTS_LEAF, TESTS_STRUCTURAL, SRC_LEAF}
    out: "list[str]" = []
    if full_gate(classes, scope.full_at_commit):
        out += FULL_GATE
        out += scope.bed_lanes
        if set(scope.changed_paths) & BUILD_FILES:
            out.append("make quick-guards")  # the bed half does not run the family
    else:
        if running and scope.quick_paths:
            out.append(f'make quick PATHS="{" ".join(scope.quick_paths)}"')
        if SCRIPTS in classes and not running:
            out.append("make quick-guards")
        if classes & {SRC_LEAF, SCRIPTS}:
            out += [_ty_line(p) for p in python if not p.startswith("tests/")]
        if running or SCRIPTS in classes:
            out += [f"uv run ruff check {p}" for p in python]
        if running:
            out.append("make lint-arch")
        if scope.guard_files or classes & {TESTS_STRUCTURAL, SRC_LEAF, SCRIPTS}:
            out.append("make quick-guards")
    if DOCS in classes:
        out.append("make docs")
    if WEB in classes:
        # One goal per line, so `_dedupe` drops `make check-ts` beside a full gate.
        out += ["make check-ts", "make test-ts"]
    return out + scope.special


def _tip_lines(scope: Scope) -> "list[str]":
    """Return the tip's lines after the live one, before deduplication."""
    out: "list[str]" = []
    if static_reach(scope):
        out += [f"make {goal}" for goal in STATIC_GOALS]
    if bed_reach(scope):
        out += TEST_SUITE_LANES
    out += scope.bed_lanes
    out += [line for line in scope.special_tip if all(g in BED_GOALS for g in goals(line))]
    return out


def commands(scope: Scope, *, tip: bool, base: str, tip_rev: str = "HEAD") -> "list[str]":
    """Return the exact lines to run, in order, deduplicated; `make check-breaking` first.

    The tip: `make check-python` and `make collect-check` when :func:`static_reach` holds,
    `make coverage-bed` when :func:`bed_reach` holds, the diff's bed lanes, and the special
    rows' tip lines whose every goal is a bed goal. Nothing else runs locally at the tip:
    the hostless matrix, docs and TypeScript are the pull request's CI.

    Per commit (a working check, never a landing gate) — none: check-breaking only; docs:
    `make docs`; web: check-ts + test-ts; tests-leaf: quick over the surviving roots +
    ruff + lint-arch (a family module runs quick-guards instead); tests-structural: the
    same + quick-guards; src-leaf: quick (trees + riders) + ty + ruff + lint-arch +
    quick-guards; scripts: quick-guards + ty + ruff; escalate / full, or a `full_at_commit`
    reason: the full gate (:data:`FULL_GATE`) + bed lanes, + quick-guards when a build file
    changed. Special rows add their commit lines, full column or not; a full column
    replaces quick/ty/ruff/lint-arch but never a web or docs line. Every stability line is
    one pass (:func:`stability_once`), and the test-isolation guard (`nox-unit-repeat`) is
    never emitted: nightly's unit-repeat job runs it.
    """
    rest = _tip_lines(scope) if tip else _commit_lines(scope)
    return _dedupe([_with_range(line, base, tip_rev) for line in ["make check-breaking", *rest]])


class GateScopeError(Exception):
    """A git failure: the answer cannot be computed (exit 1)."""


def _git(repo: Path, *args: str, env: "dict[str, str] | None" = None) -> str:
    try:
        completed = run_git(repo, *args, env=env)
    except GateError as exc:
        raise GateScopeError(f"git: {exc}") from exc
    if completed.returncode != 0:
        raise GateScopeError(f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout


def changes_between(
    repo: Path, rev_range: str, env: "dict[str, str] | None" = None
) -> "list[Change]":
    """Return the paths changed by `rev_range`; a rename is a delete plus an add."""
    out = _git(repo, "diff", "--name-status", "--no-renames", "-z", rev_range, env=env)
    fields = out.split("\0")
    return [
        Change(path, status[0])
        for status, path in zip(fields[0::2], fields[1::2], strict=False)
        if status
    ]


def head_tree(repo: Path, env: "dict[str, str] | None" = None, rev: str = "HEAD") -> "set[str]":
    """Return every path tracked at *rev*."""
    return {
        p for p in _git(repo, "ls-tree", "-r", "--name-only", "-z", rev, env=env).split("\0") if p
    }


def _reader(repo: Path, env: "dict[str, str] | None") -> Reader:
    def read(rev: str, path: str) -> "str | None":
        completed = run_git(repo, "show", f"{rev}:{path}", env=env)
        return completed.stdout if completed.returncode == 0 else None

    return read


@dataclass(frozen=True)
class Plan:
    """The landing's tip, decided once for every consumer (the runner, the hook, CI)."""

    base_sha: str
    tip_sha: str
    # the tip's committed tree: what every stamped lane is keyed by
    tree: str
    # the lines the push runs itself over `base_sha..tip_sha`, ranged exactly
    live: "list[str]"
    # the lane keys stamped by tree, in run order
    stamped: "list[str]"
    # whether a stamped lane takes the bed lease
    bed: bool
    # the range's path classes, sorted (the scope's ``classes``)
    classes: "list[str]"


def plan(repo: Path, base_sha: str, tip_sha: str, env: "dict[str, str] | None" = None) -> Plan:
    """Return the tip for ``base_sha..tip_sha``, reading sources at *tip_sha*, never HEAD."""
    scope = classify(
        changes_between(repo, f"{base_sha}..{tip_sha}", env),
        _reader(repo, env),
        base_sha,
        tip_sha,
        head_tree(repo, env, tip_sha),
    )
    cmds = commands(scope, tip=True, base=base_sha, tip_rev=tip_sha)
    stamped = [lane_key(line) for line in cmds if not is_live(line)]
    return Plan(
        base_sha=base_sha,
        tip_sha=tip_sha,
        tree=tree_hash(repo, tip_sha),  # takes no env: the ambient git, as the runner keys stamps
        live=[line for line in cmds if is_live(line)],
        stamped=stamped,
        bed=any(is_bed(key) for key in stamped),
        classes=sorted(scope.classes),
    )


def tip_commands(
    repo: Path, base_sha: str, tip_sha: str, env: "dict[str, str] | None" = None
) -> "list[str]":
    """Return the tip list for ``base_sha..tip_sha``: :func:`plan`'s live lines, then its keys."""
    p = plan(repo, base_sha, tip_sha, env)
    return p.live + p.stamped


def render(
    commit: Scope,
    tip_scope: "Scope | None",
    *,
    base: str,
    merge_base: str,
    commit_range: str,
    tip_range: str,
    count: int,
) -> str:
    """Return the report: a header, then each column's class line and exact commands."""
    lines = [f"gate-scope: base {base} (merge-base {merge_base}), {count} commit(s) to land"]
    for label, rng, scope, tip in (
        ("this commit", commit_range, commit, False),
        ("tip", tip_range, tip_scope, True),
    ):
        if scope is None:
            lines.append(f"tip: HEAD is the merge base with {base} — nothing to land")
            continue
        files = len(scope.changed_files)
        lines.append(f"{label} ({rng}): {', '.join(scope.classes)} — {files} file(s) at head")
        label_reason = "bed half" if tip else "full gate"
        lines += [f"  ({label_reason}: {reason})" for reason in scope.full_at_commit]
        lines += [f"  {cmd}" for cmd in commands(scope, tip=tip, base=base)]
    if tip_scope is not None:
        lines.append("run them and stamp the tree: make gate-tip")
    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None, *, env: "dict[str, str] | None" = None) -> int:
    """Run the oracle; `env` is the git environment. Exit 0 ok, 1 git failure, 2 refusal."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    try:
        state = tree_state(args.repo)
        if state.tracked_dirty:
            print(
                "gate-scope: refusing — tracked files are modified or staged; commit first:\n  "
                + "\n  ".join(state.tracked_dirty),
                file=sys.stderr,
            )
            return 2
        if run_git(args.repo, "rev-parse", "--verify", "-q", "HEAD^1", env=env).returncode != 0:
            print("gate-scope: HEAD has no parent; nothing to classify per commit", file=sys.stderr)
            return 2
        merge_base = _git(args.repo, "merge-base", args.base, "HEAD", env=env).strip()
        head = _git(args.repo, "rev-parse", "HEAD", env=env).strip()
        count = int(_git(args.repo, "rev-list", "--count", f"{merge_base}..HEAD", env=env).strip())
        read = _reader(args.repo, env)
        tree = head_tree(args.repo, env)
        commit_scope = classify(
            changes_between(args.repo, "HEAD^1..HEAD", env), read, "HEAD^1", "HEAD", tree
        )
        tip_scope = (
            None
            if head == merge_base
            else classify(
                changes_between(args.repo, f"{merge_base}..HEAD", env),
                read,
                merge_base,
                "HEAD",
                tree,
            )
        )
        print(
            render(
                commit_scope,
                tip_scope,
                base=args.base,
                merge_base=merge_base[:9],
                commit_range="HEAD^1..HEAD",
                tip_range=f"{merge_base[:9]}..HEAD",
                count=count,
            ),
            end="",
        )
    except (GateScopeError, GateError) as exc:
        print(f"gate-scope: {exc}", file=sys.stderr)
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
