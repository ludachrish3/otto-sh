"""Module-scope env writes in test infrastructure are banned (G11).

An ``os.environ`` write at conftest import time runs before any fixture,
survives outside monkeypatch's view, and — the year-long live case — can
quietly re-inject a variable AFTER the root conftest's hermeticity strip,
splitting the suite into two env lanes the hermeticity pin certified as
one. The sanctioned spellings are: runtime writes (fixture/monkeypatch,
torn down in scope), or the ONE allowlisted module-scope block in the root
``tests/conftest.py`` (its colour block + the OTTO_* strip itself, which
must precede the conftest's own otto imports and therefore cannot move to
``pytest_configure``).

AST, not regex — a docstring mentioning ``os.environ["X"] = ...`` must not
count (the quoted-annotation lesson). Module-level *calls* are resolved one
hop into functions defined in the scanned set, because the live offender
was exactly ``ensure_sut_dirs()`` — a module-level call whose write lives
in another file's function body. Aliases are resolved, not stated as blind
spots: import aliases (``import os as _os``, ``from os import environ as
e`` — a mutation run walked through that hole by accident) and
alias-by-assignment (``_env = os.environ`` — the review's escape shape),
plus ``environb``, ``__setitem__``, ``|=``, annotated assigns, and a bare
imported ``putenv``. Remaining stated blind spots: attribute/method calls
(``paths.ensure_sut_dirs()``), transitive helpers deeper than one hop,
``exec``-built writes — and alias resolution covers single-target direct
assignment only (tuple-unpack and walrus bindings escape it).
Stated over-approximations (fail-loud, acceptable):
a module-scope name literally bound as an env alias is tracked by NAME, so
an unrelated dict named ``environ`` would flag; one-hop call resolution is
name-based, so a module-scope call flags if ANY scanned file defines an
env-writing function of that name. The per-tree collection pin in
``test_env_hermeticity.py`` (whole tree roots — every conftest imports)
backstops the remaining blind spots at runtime.
"""

import ast
from pathlib import Path

from tests._fixtures.paths import TESTS_ROOT

# Fixture SUT repos and firmware are user-example INPUT DATA, not harness
# code — same exclusion set as the ast-grep tests/ scope (G0).
_DATA_DIRS = {"repo1", "repo2", "repo3", "repo4", "repo_broken", "repo_e2e", "firmware"}

# The one sanctioned module-scope block (see module docstring).
_SANCTIONED = {TESTS_ROOT / "conftest.py"}


def _scanned_files() -> list:
    files = [
        p
        for p in TESTS_ROOT.rglob("conftest.py")
        # Path parts RELATIVE to tests/ — an absolute-parts match would empty
        # the scan for a checkout living under a directory named e.g. repo1.
        if not (set(p.relative_to(TESTS_ROOT).parts) & _DATA_DIRS) and p not in _SANCTIONED
    ]
    files += (TESTS_ROOT / "_fixtures").glob("*.py")
    return sorted(set(files))


class _Aliases:
    """Names bound to the ``os`` module, ``os.environ``, and ``putenv`` in a tree.

    ``import os as _os`` / ``from os import environ as e`` are one typo away
    from the canonical spelling — a mutation run walked through exactly that
    hole in this detector's first cut, caught only by the runtime collection
    pin. ``_env = os.environ`` (alias-by-assignment) was the review round's
    escape. Both are resolved (to a fixpoint, so ``_e2 = _env`` chains
    count) instead of stated as blind spots.
    """

    def __init__(self, tree) -> None:
        self.os_names = {"os"}
        self.environ_names = {"environ"}
        self.putenv_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.os_names.update(a.asname for a in node.names if a.name == "os" and a.asname)
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for a in node.names:
                    if a.name == "environ":
                        self.environ_names.add(a.asname or a.name)
                    elif a.name == "putenv":
                        self.putenv_names.add(a.asname or a.name)
        while True:  # fixpoint over alias-by-assignment chains
            grew = False
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and self.is_environ(node.value)
                    and any(isinstance(t, ast.Name) for t in node.targets)
                ):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id not in self.environ_names:
                            self.environ_names.add(t.id)
                            grew = True
            if not grew:
                break

    def is_environ(self, node) -> bool:
        """True for ``os.environ``/``os.environb`` / aliased expressions."""
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "environb"):
            return isinstance(node.value, ast.Name) and node.value.id in self.os_names
        return isinstance(node, ast.Name) and node.id in self.environ_names


_MUTATORS = {"setdefault", "pop", "update", "clear", "popitem", "__setitem__", "__delitem__"}


def _env_write_lines(nodes, aliases) -> list:
    """Line numbers of env writes among *nodes* — each node checked AS-IS.

    No internal descent: the caller chooses the walk. Module-scope scanning
    hands in the function-boundary-respecting stream from
    ``_module_scope_statements`` (an ``ast.walk`` here would dive into a
    ``def`` nested under a module-level ``if`` and false-flag it); function
    bodies hand in a full walk.
    """
    hits = []
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:  # Assign and Delete both carry .targets
                targets = node.targets
            hits.extend(
                node.lineno
                for t in targets
                if isinstance(t, ast.Subscript) and aliases.is_environ(t.value)
            )
            # `os.environ |= {...}` — the operator spelling of update():
            # environ IS the AugAssign target, no Subscript involved.
            if isinstance(node, ast.AugAssign) and aliases.is_environ(node.target):
                hits.append(node.lineno)
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in _MUTATORS
                    and aliases.is_environ(fn.value)
                )
                or (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "putenv"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in aliases.os_names
                )
                or (isinstance(fn, ast.Name) and fn.id in aliases.putenv_names)
            ):
                hits.append(node.lineno)
    return hits


def _env_writes_in(nodes, aliases=None) -> list:
    """Env-write lines anywhere under *nodes*, full descent (function bodies)."""
    aliases = aliases if aliases is not None else _Aliases(ast.parse(""))
    return _env_write_lines((n for root in nodes for n in ast.walk(root)), aliases)


def _module_scope_statements(tree):
    """Module-executing statements: the module body, descending through
    module-level ``if``/``for``/``with``/``try`` and CLASS bodies (a class
    body executes at import) but never into function bodies."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        )


def _env_writing_defs(trees_by_path) -> set:
    """Names of functions (across the scanned set) whose bodies write env."""
    names = set()
    for tree in trees_by_path.values():
        aliases = _Aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _env_writes_in(
                node.body, aliases
            ):
                names.add(node.name)
    return names


def _module_scope_env_offenders(trees_by_path) -> list:
    """``path:line`` for every module-scope env write or module-scope call
    to a scanned-set function that writes env."""
    writer_names = _env_writing_defs(trees_by_path)
    offenders = []
    for path, tree in trees_by_path.items():
        aliases = _Aliases(tree)
        stmts = list(_module_scope_statements(tree))
        offenders.extend(f"{path}:{line}" for line in _env_write_lines(stmts, aliases))
        offenders.extend(
            f"{path}:{node.lineno}"
            for node in stmts
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in writer_names
        )
    return sorted(set(offenders))


def _parse_all() -> dict:
    return {p.relative_to(TESTS_ROOT.parent): ast.parse(p.read_text()) for p in _scanned_files()}


def test_no_module_scope_env_writes_outside_the_sanctioned_block():
    offenders = _module_scope_env_offenders(_parse_all())
    assert offenders == [], (
        "module-scope os.environ write (or module-scope call to an "
        "env-writing helper) in test infrastructure — write env in a "
        f"fixture with teardown instead: {offenders}"
    )


def test_scan_actually_covers_the_trees():
    """Anti-vacuity: the glob must keep finding the real conftest chains —
    including the DEEP ones, which this AST guard covers alone whenever a
    future edit narrows the collection pin's targets."""
    scanned = {str(p.relative_to(TESTS_ROOT.parent)) for p in _scanned_files()}
    for expected in (
        "tests/integration/conftest.py",
        "tests/e2e/conftest.py",
        "tests/_fixtures/paths.py",
        # Deep conftests — one per tree, the review's blind-spot class.
        "tests/unit/cli/conftest.py",
        "tests/integration/host/conftest.py",
        "tests/e2e/chaos/conftest.py",
    ):
        assert expected in scanned, f"{expected} fell out of the scan set"
    assert "tests/conftest.py" not in scanned, "the sanctioned root block joined the scan"
    # Count floor: 14 conftests + ~24 fixture modules today; a glob
    # regression that quietly halves the set must fail here.
    assert len(scanned) >= 30, f"scan set shrank to {len(scanned)} files: {sorted(scanned)}"


# ---------------------------------------------------------------------------
# Embedded positive controls: the detector must flag each banned shape and
# stay quiet on each sanctioned one.
# ---------------------------------------------------------------------------


def _offenders_of(source: str, helper_source: str = "") -> list:
    trees = {Path("synthetic/conftest.py"): ast.parse(source)}
    if helper_source:
        trees[Path("synthetic/_fixtures/helper.py")] = ast.parse(helper_source)
    return _module_scope_env_offenders(trees)


def test_detector_flags_each_banned_shape():
    assert _offenders_of("import os\nos.environ['OTTO_X'] = '1'\n")
    assert _offenders_of("import os\nos.environ.setdefault('OTTO_X', '1')\n")
    assert _offenders_of("import os\nos.environ.pop('OTTO_X', None)\n")
    assert _offenders_of("import os\nos.putenv('OTTO_X', '1')\n")
    assert _offenders_of("import os\nfor v in ('A',):\n    os.environ.pop(v, None)\n")
    assert _offenders_of("import os\ndel os.environ['OTTO_X']\n")
    assert _offenders_of("from os import environ\nenviron['OTTO_X'] = '1'\n")
    # Aliased imports are resolved, not stated as blind spots — a mutation
    # run reached this hole by accident before the resolution existed.
    assert _offenders_of("import os as _os\n_os.environ.setdefault('OTTO_X', '1')\n")
    assert _offenders_of("from os import environ as e\ne['OTTO_X'] = '1'\n")
    # Alias-by-assignment — the review round's verified escape shape —
    # including a chained alias, plus the sibling spellings it suggested.
    assert _offenders_of("import os\n_env = os.environ\n_env['OTTO_X'] = '1'\n")
    assert _offenders_of("import os\n_env = os.environ\n_e2 = _env\n_e2.pop('OTTO_X', None)\n")
    assert _offenders_of("import os\nos.environ.__setitem__('OTTO_X', '1')\n")
    assert _offenders_of("from os import putenv\nputenv('OTTO_X', '1')\n")
    assert _offenders_of("import os\nos.environ['OTTO_X']: str = '1'\n")
    assert _offenders_of("import os\nos.environb[b'OTTO_X'] = b'1'\n")
    # `|=` is the operator spelling of update() — the final review falsified
    # the blind-spot statement with it, so it is caught, not stated.
    assert _offenders_of("import os\nos.environ |= {'OTTO_X': '1'}\n")
    assert _offenders_of("import os\n_env = os.environ\n_env |= {'OTTO_X': '1'}\n")
    # A class body executes at import time — module-scope in disguise.
    assert _offenders_of("import os\nclass C:\n    os.environ['OTTO_X'] = '1'\n")
    # The live offender's exact shape: a module-scope call whose write lives
    # in a scanned-set helper's function body.
    assert _offenders_of(
        "from helper import ensure\nensure()\n",
        helper_source="import os\ndef ensure():\n    os.environ.setdefault('OTTO_X', 'y')\n",
    )


def test_detector_ignores_each_sanctioned_shape():
    assert not _offenders_of("import os\ndef f():\n    os.environ['OTTO_X'] = '1'\n")
    assert not _offenders_of(
        "import os\nimport pytest\n@pytest.fixture\ndef f(monkeypatch):\n"
        "    monkeypatch.setenv('OTTO_X', '1')\n"
    )
    assert not _offenders_of('"""os.environ["OTTO_X"] = "docs only"."""\n')
    assert not _offenders_of("import os\nX = os.environ.get('OTTO_X')\n")  # read, not write
    # A def nested under a module-level `if` is still a function body.
    assert not _offenders_of(
        "import os\nif True:\n    def f():\n        os.environ['OTTO_X'] = '1'\n"
    )
    # A module-scope call to a helper that does NOT write env is fine.
    assert not _offenders_of(
        "from helper import on_path\non_path()\n",
        helper_source="import sys\ndef on_path():\n    sys.path.insert(0, 'x')\n",
    )
