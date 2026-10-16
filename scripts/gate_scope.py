"""Classify a diff into the gates it needs (#566).

Each changed path gets a class by location (and, for test modules, by whether the
change is structural): ``none``, ``docs``, ``web``, ``tests-leaf``,
``tests-structural``, ``src-leaf``, ``scripts``, ``escalate``, ``full``. A path with no
row is ``full``. The pure classifier, the command composition and the repository
layer all live here. Exit codes: 0 ok, 1 repository command failure, 2 refusal or usage.
"""

import argparse
import ast
import copy
import fnmatch
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.gate_fresh import GateFreshError, tree_state  # noqa: E402 -- path set up above


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
    # every changed path, deleted ones included (the rider triggers)
    changed_paths: "list[str]"
    # commit-time lane commands (SPECIAL_TREES / STABILITY_FILES / LANE_MARKERS)
    special: "list[str]"
    # the same rows' tip-time commands
    special_tip: "list[str]"
    # the bed lanes a changed src package adds to any full gate this diff earns (sorted)
    bed_lanes: "list[str]"
    # paths no row matched (each makes the class `full`)
    unknown: "list[str]"
    # reasons the commit column is the full gate (a FULL row, > 2 packages, unknown path,
    # no surviving root)
    full_at_commit: "list[str]"
    # reasons the tip column is the full gate beyond the class rule (a FULL tip row)
    full_at_tip: "list[str]"


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
    "monitor": [
        "tests/unit/monitor",
        "tests/e2e/monitor",
        "tests/unit/test_monitor_service_reads_no_lab.py",
    ],
    "suite": ["tests/unit/suite", "tests/e2e/suite"],
    "init": ["tests/unit/init", "tests/e2e/test_init_e2e.py"],
    "config": ["tests/unit/config", "tests/e2e/config"],
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
    ],
    "examples": ["tests/unit/examples"],
    "models": ["tests/unit/models"],
    "project": ["tests/unit/project", "tests/unit/test_project_instruction_table.py"],
    "reservations": ["tests/unit/reservations"],
    "testing": ["tests/unit/testing"],
    "_webassets": ["tests/unit/monitor", "tests/e2e/monitor"],
    # top-level modules, used everywhere: the whole unit tree, plus the bed tree their
    # stem names
    "console": ["tests/unit", "tests/e2e/console"],
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
BED_LANES = {
    "host": ["conformance-bed"],
    "link": ["stability-unix"],
    "tunnel": ["stability-tunnel"],
    "console": ["stability-embedded"],
}
"""The lane every full gate this diff earns adds for these sources: the full gate
excludes the bed tiers, and a change to these packages is what the beds exercise."""
GUARD_RIDERS = [
    "tests/unit/test_error_base.py",
    "tests/unit/test_declared_additions.py",
    "tests/unit/init/test_init_templates.py",
    "tests/unit/import_budget",
    "tests/unit/api_snapshot",
]
"""Tree-wide guards no package path selects; they ride every src-leaf `quick`."""
# (glob, commit-time commands, tip-time commands): a directory-stamped tree — every item is
# a lane's, so `quick` is never added. First match wins. [FULL] = that column is full.
# A row may name several lanes; each column is named even when it is the full gate.
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
    ("tests/integration/busybox_bed/*", ["make coverage-unix"], [FULL]),
    (
        "tests/e2e/monitor/dashboard/test_replay_soak.py",
        ["make dashboard-soak"],
        ["make dashboard-soak"],
    ),
    ("tests/e2e/tunnel_stability/*", ["make stability-tunnel"], ["make stability-tunnel"]),
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
    "tests/unit/host/test_app_shell_concurrency.py": ["make stability-unit"],
    "tests/unit/host/test_session_concurrency.py": ["make stability-unit"],
    "tests/unit/host/test_unix_host.py": ["make stability-unit"],
    "tests/unit/monitor/test_collector_tunnel_soak.py": ["make stability-unit"],
    "tests/integration/host/test_app_shell_stability_integration.py": ["make stability-unix"],
    "tests/integration/host/test_host_stability_contract.py": [
        "make stability-unix",
        "make stability-embedded",
    ],
    "tests/integration/host/test_proxy_user_stability_integration.py": ["make stability-unix"],
    "tests/integration/host/test_session_stability_integration.py": ["make stability-unix"],
}
LANE_MARKERS = {
    "kmodcov": "make kmodcov",
    "browser": "make dashboard",
    "busybox": "make busybox",
}
"""A changed test file carrying one of these marks runs the lane too, wherever it lives."""
# The marks a lane row's selector covers, for "is anything left for quick".
LANE_COVERS = {
    "make stability-unit": {"concurrency"},
    "make stability-unix": {"stability"},
    "make stability-embedded": {"stability"},
    "make kmodcov": {"kmodcov"},
    "make dashboard": {"browser"},
    "make busybox": {"busybox"},
}
QUICK_EXCLUDED = frozenset(
    {"stability", "busybox", "conformance", "kmodcov", "browser", "interpreter_agnostic"}
)
"""The marks both `make quick` legs deselect (M_QUICK; pinned against the Makefile)."""
FULL_GATE = [
    "make coverage",
    "nox -s tests_hostless-3.14",
    "make check-python",
    "make docs",
    "make gate-fresh",
]


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
        return None if _match(path, DOCS_EXCLUDED) else DOCS
    if "/" not in path and path.endswith(".md"):
        return DOCS  # AGENTS.md never reaches here: NONE_GLOBS matched it first
    if path.startswith("web/src/"):
        return None if _match(path, WEB_EXCLUDED) else WEB
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
    except SyntaxError:
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


def _holds_a_test(root: str, head_paths: "set[str]") -> bool:
    """Report whether a quick root is a surviving test module or contains one."""
    if root in head_paths:
        return _is_test_module(root)
    return any(_is_test_module(p) and p.startswith(root + "/") for p in head_paths)


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
        return any(
            self._selected_from(p) for p in self._modules if p == root or p.startswith(root + "/")
        )


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
        return [[FULL], [FULL], set(), "unreadable"]
    lanes = [lane for mark, lane in LANE_MARKERS.items() if mark in marks]
    if lanes:
        return [lanes, list(lanes), set().union(*(LANE_COVERS[lane] for lane in lanes)), "mark"]
    if marks & {"stability", "chaos"}:
        return [[FULL], [FULL], set(), "stray"]
    return None


def _owning_tree_lanes(path: str) -> "list[list[Any]]":
    """Return the lane rows a conftest or helper (a non-test-module path) under a lane tree feeds.

    Each directory row (`<root>/*`) whose root holds `path` gives a "tree" lane row, as a
    test module there would: the path keeps its own class (a conftest escalates), and the
    tree's lanes join both columns because the full gate excludes that tier. File rows name
    one module only and feed nothing else.
    """
    if not path.startswith("tests/") or _is_test_module(path):
        return []
    return [
        [list(at_commit), list(at_tip), set(), "tree"]
        for glob, at_commit, at_tip in SPECIAL_TREES
        if glob.endswith("/*") and path.startswith(glob[:-1])
    ]


def _route_lane(
    path: str,
    source: "str | None",
    lane: "list[Any]",
    special: "set[str]",
    special_tip: "set[str]",
    full_commit: "list[str]",
    full_tip: "list[str]",
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
    if at_tip == [FULL]:
        full_tip.append(f"{path}: full at the tip")
    else:
        special_tip.update(at_tip)
    return kind in {"file", "mark"} and _has_tests_outside(source, covered)


def _outermost(roots: "set[str]") -> "list[str]":
    """Return `roots` sorted, without any root nested under another (the outer one runs it)."""
    return sorted(r for r in roots if not any(r.startswith(o + "/") for o in roots if o != r))


def classify(
    changes: "list[Change]", read: Reader, base_rev: str, head_rev: str, head_paths: "set[str]"
) -> Scope:
    """Classify `changes` between `base_rev` and `head_rev`; `head_paths` are the files at head."""
    classes: "set[str]" = set()
    quick: "set[str]" = set()
    guards: "set[str]" = set()
    packages: "set[str]" = set()
    files: "set[str]" = set()
    paths: "set[str]" = set()
    special: "set[str]" = set()
    special_tip: "set[str]" = set()
    bed: "set[str]" = set()
    unknown: "list[str]" = []
    full_commit: "list[str]" = []
    full_tip: "list[str]" = []

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
        if change.status != "D":
            files.add(path)
        if _is_src(path):
            bed.update(BED_LANES.get(_src_key(path), []))
        cls = classify_path(path)
        for lane in _owning_tree_lanes(path):
            _route_lane(path, None, lane, special, special_tip, full_commit, full_tip)
        if cls is None:
            unknown.append(path)
            full_commit.append(f"no row: {path}")
            classes.add(FULL)
            continue
        if cls == TESTS_LEAF:
            head = None if change.status == "D" else read(head_rev, path)
            base = None if change.status == "A" else read(base_rev, path)
            if is_structural(base, head):
                cls = TESTS_STRUCTURAL
            source = head if head is not None else base
            lane = _special_lane(path, source)
            if lane is not None:
                if _route_lane(path, source, lane, special, special_tip, full_commit, full_tip):
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
    if classes & {ESCALATE, FULL} or full_commit:
        quick.clear()
    return Scope(
        sorted(classes),
        _outermost(quick),
        sorted(guards),
        sorted(packages),
        sorted(files),
        sorted(paths),
        sorted(special),
        sorted(special_tip),
        sorted(bed),
        sorted(unknown),
        full_commit,
        full_tip,
    )


BUILD_FILES = frozenset(
    {"Makefile", "noxfile.py", "pyproject.toml", ".coveragerc", ".github/workflows/ci.yml"}
)
REPEAT_TRIGGERS = ("tests/_fixtures/*",)


def _ty_line(path: str) -> str:
    """Return the ty command for a path; `scripts/` is outside ty's root and needs the repo root."""
    if path.startswith("scripts/"):
        return f"uv run ty check --extra-search-path . {path}"
    return f"uv run ty check {path}"


RANGE_TARGETS = ["check-breaking", "lint-arch", "check-python", "gate-fresh"]
"""The emitted targets that run check-breaking, whose RANGE defaults to origin/main..HEAD.

lint-arch (and check-python through it) has it as a prerequisite; gate-fresh runs lint-arch
in its throwaway worktree through a nested make, which inherits a command-line RANGE through
MAKEFLAGS. So check-breaking runs again inside lint-arch: the separate line stays, because
the policy is check-breaking on every column, lint-arch or not.
"""


def _with_range(line: str, base: str) -> str:
    """Carry a non-default base to a `make` line whose target runs check-breaking."""
    target = line.split()[1] if line.startswith("make ") else None
    if base == "origin/main" or target not in RANGE_TARGETS:
        return line
    return f"{line} RANGE={base}..HEAD"


def _dedupe(lines: "list[str]") -> "list[str]":
    return list(dict.fromkeys(lines))


def commands(scope: Scope, *, tip: bool, base: str) -> "list[str]":
    """Return the exact lines to run, in order: every class's column composed, then deduplicated.

    Per commit — none: check-breaking only; docs: `make docs`; web: `make check-ts test-ts`;
    tests-leaf: quick over the surviving roots + ruff + lint-arch (a family module runs
    quick-guards instead); tests-structural: the same + quick-guards; src-leaf: quick
    (trees + riders) + ty + ruff + lint-arch + quick-guards; scripts: quick-guards + ty +
    ruff; escalate / full: the full gate + bed lanes, + quick-guards when a build file
    changed, + nox-unit-repeat when a conftest, tests/_fixtures file or the registry
    changed (deleted ones included). At the tip — none: check-breaking; docs: gate-fresh;
    web: check-ts coverage-ts + gate-fresh; tests-*: the commit lines + quick-guards +
    gate-fresh; src-leaf / scripts / escalate / full: the full gate + bed lanes, + the same
    nox-unit-repeat rider (quick-guards is commit-only: `make coverage` runs it). Special
    rows add their column's lanes in both forms, full column or not (`make coverage`
    excludes those tiers); a full column replaces quick/ty/ruff/lint-arch but never a
    web or docs line (check-python is Python only; the full gate already runs docs).
    """
    classes = set(scope.classes)
    python = [p for p in scope.changed_files if p.endswith(".py")]
    changed = set(scope.changed_paths)
    running = classes & {TESTS_LEAF, TESTS_STRUCTURAL, SRC_LEAF}
    full = (
        bool(classes & {ESCALATE, FULL})
        or bool(scope.full_at_commit)
        or (tip and (bool(classes & {SRC_LEAF, SCRIPTS}) or bool(scope.full_at_tip)))
    )
    out: "list[str]" = ["make check-breaking"]
    if full:
        out += FULL_GATE
        out += [f"make {lane}" for lane in scope.bed_lanes]
        if not tip and changed & BUILD_FILES:
            out.append("make quick-guards")  # the tip's `make coverage` runs the family
        # both columns: a landing is one squashed commit nobody re-runs the oracle on
        if "src/otto/registry.py" in changed or any(
            p.split("/")[-1] == "conftest.py" or _match(p, REPEAT_TRIGGERS) for p in changed
        ):
            out.append("make nox-unit-repeat")
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
        if (
            (tip and running)
            or scope.guard_files
            or classes & {TESTS_STRUCTURAL, SRC_LEAF, SCRIPTS}
        ):
            out.append("make quick-guards")
    if DOCS in classes and not tip:
        out.append("make docs")
    if WEB in classes:
        out.append("make check-ts coverage-ts" if tip else "make check-ts test-ts")
    out += scope.special_tip if tip else scope.special
    if tip and not full and classes & {DOCS, WEB, TESTS_LEAF, TESTS_STRUCTURAL}:
        out.append("make gate-fresh")
    return _dedupe([_with_range(line, base) for line in out])


class GateScopeError(Exception):
    """A git failure: the answer cannot be computed (exit 1)."""


def _run_git(
    repo: Path, args: "list[str]", env: "dict[str, str] | None"
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell; `repo`/`args` are ours
        ["git", *args],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git(repo: Path, *args: str, env: "dict[str, str] | None" = None) -> str:
    try:
        completed = _run_git(repo, list(args), env)
    except OSError as exc:
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


def head_tree(repo: Path, env: "dict[str, str] | None" = None) -> "set[str]":
    """Return every path tracked at HEAD."""
    return {
        p
        for p in _git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD", env=env).split("\0")
        if p
    }


def _reader(repo: Path, env: "dict[str, str] | None") -> Reader:
    def read(rev: str, path: str) -> "str | None":
        completed = _run_git(repo, ["show", f"{rev}:{path}"], env)
        return completed.stdout if completed.returncode == 0 else None

    return read


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
        reasons = scope.full_at_tip if tip else scope.full_at_commit
        lines += [f"  (full gate: {reason})" for reason in reasons]
        lines += [f"  {cmd}" for cmd in commands(scope, tip=tip, base=base)]
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
        if _run_git(args.repo, ["rev-parse", "--verify", "-q", "HEAD^1"], env).returncode != 0:
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
    except (GateScopeError, GateFreshError) as exc:
        print(f"gate-scope: {exc}", file=sys.stderr)
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
