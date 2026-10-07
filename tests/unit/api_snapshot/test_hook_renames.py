"""The D-5 hook renames hold: no extension hook keeps its pre-cutover underscore name.

Dump spec §7.4 freezes the rename inventory from the pre-cutover tree, and
``RENAMES`` below spells it out: every underscore name in a declared class's
``abstract``/``requires`` set, plus every underscore member the docs validator
found taught, plus ``_apply_mode``, which the owner ruled in because the docs
teach it where the validator cannot see. Each entry names the old member, the
public name it became and the class that defines it. These checks keep the old
names from coming back:

* at runtime, the defining class has the new name and no class in its hierarchy
  still defines the old one, and a subclass written against an old abstract hook
  cannot be instantiated;
* no docs page or shipped example spells an old name;
* no src/tests/scripts file spells an unambiguous old name (``_open`` and
  ``_write`` are common spellings, so they are judged by the runtime and
  rebinding checks instead);
* no code rebinds an old name on an object (``host._login = spy``) or patches it
  by string: after a rename that spy would replace nothing and its test would pass
  without ever being called.

Once the declaration is live, the API-dump producer refuses a hidden obligation
and the docs validator refuses a taught underscore member (dump spec §7.2, §7.3);
this file pins the specific names the cutover retired.
"""

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# The frozen rename inventory, spelled literally: this guard never reads the JSON.
RENAMES = [
    {
        "old": "otto.host.command_frame:ZephyrFrame._region_before_end",
        "new": "otto.host.command_frame:ZephyrFrame.region_before_end",
        "defined_in": "otto.host.command_frame:ZephyrFrame",
    },
    {
        "old": "otto.host:Host._login",
        "new": "otto.host:Host.run_login",
        "defined_in": "otto.host.host:Host",
    },
    {
        "old": "otto.host:Host._logout",
        "new": "otto.host:Host.run_logout",
        "defined_in": "otto.host.host:Host",
    },
    {
        "old": "otto.host:ShellSession._open",
        "new": "otto.host:ShellSession.open_transport",
        "defined_in": "otto.host.session:ShellSession",
    },
    {
        "old": "otto.host:ShellSession._read_until_pattern",
        "new": "otto.host:ShellSession.read_transport_until",
        "defined_in": "otto.host.session:ShellSession",
    },
    {
        "old": "otto.host:ShellSession._write",
        "new": "otto.host:ShellSession.write_transport",
        "defined_in": "otto.host.session:ShellSession",
    },
    {
        "old": "otto.host.transfer:BaseFileTransfer._apply_mode",
        "new": "otto.host.transfer:BaseFileTransfer.apply_mode",
        "defined_in": "otto.host.transfer.base:BaseFileTransfer",
    },
    {
        "old": "otto.host.transfer:BaseFileTransfer._dispatch_per_file",
        "new": "otto.host.transfer:BaseFileTransfer.dispatch_per_file",
        "defined_in": "otto.host.transfer.base:BaseFileTransfer",
    },
    {
        "old": "otto.host.transfer:BaseFileTransfer._run_get",
        "new": "otto.host.transfer:BaseFileTransfer.run_get",
        "defined_in": "otto.host.transfer.base:BaseFileTransfer",
    },
    {
        "old": "otto.host.transfer:BaseFileTransfer._run_put",
        "new": "otto.host.transfer:BaseFileTransfer.run_put",
        "defined_in": "otto.host.transfer.base:BaseFileTransfer",
    },
]

# Spellings that are also ordinary private names elsewhere (ConsoleClient._write,
# module-level ``_write``/``_open`` test helpers): never text-scanned.
AMBIGUOUS = {"_open", "_write"}

# Files that may keep an old spelling, with the names each may keep and why.
TEXT_EXCEPTIONS = {
    # The validator's and the producer's own unit tests: synthetic fixtures that
    # declare fictional underscore members to prove the checks fire.
    "tests/unit/scripts/test_api_teaching.py": {"_login", "_run_put"},
    "tests/unit/scripts/test_api_dump_child.py": {"_login", "_run_put"},
    # The interactive e2e's ``<timestamp>_login`` output-directory suffix.
    "tests/e2e/host/test_interact_e2e.py": {"_login"},
    # A tuple-unpacking loop variable over survey seams.
    "tests/unit/host/survey/test_engine.py": {"_login"},
}
DOC_ROOTS = ["docs", "README.md"]
CODE_ROOTS = ["src", "tests", "scripts", ".ast-grep"]
NOT_SCANNED = ("docs/superpowers/", "docs/_build/")
SUFFIXES = {".py", ".md", ".rst", ".yml", ".toml", ".txt"}


def _member(path: str) -> str:
    return path.rpartition(".")[2]


OLD = {_member(e["old"]) for e in RENAMES}
# This file names the old spellings to explain them.
TEXT_EXCEPTIONS[Path(__file__).resolve().relative_to(PROJECT_ROOT).as_posix()] = OLD


def _files(roots: "list[str]") -> "list[Path]":
    out: list[Path] = []
    for root in roots:
        base = PROJECT_ROOT / root
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if path.is_file() and path.suffix in SUFFIXES and not rel.startswith(NOT_SCANNED):
                out.append(path)
    return sorted(out)


def _word(names: "set[str]") -> "re.Pattern[str]":
    return re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, names))) + r")(?![\w])")


def _resolve(key: str) -> type:
    module, _, qualname = key.partition(":")
    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _subclasses(cls: type) -> "set[type]":
    seen: set[type] = set()
    todo = [cls]
    while todo:
        klass = todo.pop()
        if klass not in seen:
            seen.add(klass)
            todo.extend(klass.__subclasses__())
    return seen


def test_the_inventory_renames_each_underscore_member_to_a_distinct_public_name():
    assert RENAMES, "the hook-rename inventory is empty"
    news = [e["new"] for e in RENAMES]
    assert len(set(news)) == len(news), f"two hooks share a new name: {news}"
    for e in RENAMES:
        assert _member(e["old"]).startswith("_"), e
        assert not _member(e["new"]).startswith("_"), e
        assert e["old"].rpartition(".")[0] == e["new"].rpartition(".")[0], e


def _otto_host_classes() -> "set[type]":
    """Every class an otto.host module defines, after importing all of them."""
    import otto.host

    modules = [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(otto.host.__path__, "otto.host.")
    ]
    return {
        obj
        for module in modules
        for obj in vars(module).values()
        if isinstance(obj, type) and obj.__module__.startswith("otto.")
    }


@pytest.mark.parametrize("entry", RENAMES, ids=[e["old"] for e in RENAMES])
def test_every_implementer_has_the_new_name_and_not_the_old(entry):
    definer = _resolve(entry["defined_in"])
    old, new = _member(entry["old"]), _member(entry["new"])
    assert new in vars(definer), f"{entry['defined_in']} does not define {new}"
    # A protocol's implementers do not subclass it (BaseHost is not a Host
    # subclass), so the roots are the definer plus every class defining the new
    # name; everything under them must have dropped the old one.
    roots = {definer} | {k for k in _otto_host_classes() if new in vars(k)}
    # The old-spelling subclass test_an_old_hook_spelling_fails_loudly builds lives
    # on in __subclasses__ until the garbage collector frees it: it is not an
    # implementer, so classes this module defines are skipped.
    implementers = {
        k for k in set().union(*(_subclasses(root) for root in roots)) if k.__module__ != __name__
    }
    # A slots dataclass leaves its pre-slots class object behind in __subclasses__,
    # so one class can appear twice: dedupe by name.
    stale = sorted({f"{k.__module__}:{k.__qualname__}" for k in implementers if old in vars(k)})
    assert stale == [], f"{old} is still defined on {stale}; it is {new} now"


# The abstract hooks: a subclass that still implements only the old spelling
# leaves the new abstract method unimplemented, so it cannot be instantiated.
# The concrete hooks (Host's run_login/run_logout, BaseFileTransfer's
# apply_mode) are not here: an old spelling of one is simply not called.
ABSTRACT_HOOKS = {
    "otto.host.session:ShellSession": ["_open", "_read_until_pattern", "_write"],
    "otto.host.transfer.base:BaseFileTransfer": ["_run_get", "_run_put"],
}
NEW_OF = {(e["defined_in"], _member(e["old"])): _member(e["new"]) for e in RENAMES}


@pytest.mark.parametrize("definer", sorted(ABSTRACT_HOOKS), ids=lambda key: key.rpartition(":")[2])
def test_an_old_hook_spelling_fails_loudly(definer):
    """A subclass written against the old abstract names is refused at instantiation."""
    olds = ABSTRACT_HOOKS[definer]
    news = [NEW_OF[(definer, old)] for old in olds]

    async def _old_spelling(self, *args, **kwargs):
        return None

    namespace = {"__module__": __name__, **dict.fromkeys(olds, _old_spelling)}
    subclass = type("OldSpelling", (_resolve(definer),), namespace)
    for new in news:
        with pytest.raises(TypeError, match=rf"abstract.*\b{new}\b"):
            subclass()


def test_no_docs_page_or_example_spells_an_old_hook_name():
    pattern = _word(OLD)
    hits = [
        f"{path.relative_to(PROJECT_ROOT)}:{no}: {m.group(1)}"
        for path in _files(DOC_ROOTS)
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for m in pattern.finditer(line)
    ]
    assert hits == [], "docs still teach a renamed hook:\n" + "\n".join(hits)


def test_no_code_file_spells_an_unambiguous_old_hook_name():
    names = OLD - AMBIGUOUS
    hits = []
    for path in _files(CODE_ROOTS):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        allowed = TEXT_EXCEPTIONS.get(rel, set())
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits += [f"{rel}:{no}: {n}" for n in _word(names).findall(line) if n not in allowed]
    assert hits == [], "a renamed hook is still spelled the old way:\n" + "\n".join(hits)


_SETTERS = {"setattr", "object"}  # setattr(o, "x", v), patch.object(o, "x"), monkeypatch.setattr


def _rebinds(tree: ast.AST) -> "list[tuple[int, str]]":
    out = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        out += [
            (t.lineno, t.attr) for t in targets if isinstance(t, ast.Attribute) and t.attr in OLD
        ]
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _SETTERS:
                out += [
                    (a.lineno, a.value)
                    for a in node.args[1:2]
                    if isinstance(a, ast.Constant) and a.value in OLD
                ]
            if name == "patch":  # patch("otto.host.session.SshSession._open")
                out += [
                    (a.lineno, _member(a.value))
                    for a in node.args[:1]
                    if isinstance(a, ast.Constant)
                    and isinstance(a.value, str)
                    and _member(a.value) in OLD
                ]
    return out


def test_no_code_rebinds_or_patches_an_old_hook_name():
    hits = []
    for path in _files(CODE_ROOTS):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if path.suffix != ".py" or rel in TEXT_EXCEPTIONS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # deliberately broken fixture sources (tests/repo_broken)
        hits += [f"{rel}:{no}: {name}" for no, name in _rebinds(tree)]
    assert hits == [], "a spy or patch still targets a renamed hook:\n" + "\n".join(hits)
