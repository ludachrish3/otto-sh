"""A test patches a lazily exported name where it is DEFINED, never on its package.

``monkeypatch.setattr("otto.config.load_user_settings", fake)`` leaves the real object
cached in the package ``__dict__`` on undo and later hides a patch of the
defining module (see ``tests/_fixtures/_lazy_exports.py``, which also fails the
test at teardown at run time). ``mock.patch`` on the package does not leak but
can shadow a defining-module patch inside its block. This is the static half of
the same rule: it parses every ``.py`` file under ``tests/`` (nothing is
imported or run, so the integration and e2e trees are covered too) and names
each package-level patch with the module to patch instead. There is no
allowlist.

Recognised: string targets of ``setattr`` / ``delattr`` / ``patch``
(``monkeypatch``, ``mock.patch``, ``mocker.patch``) and the attribute forms
``setattr(pkg, "name")``, ``delattr(pkg, "name")`` and
``patch.object(pkg, "name")`` where ``pkg`` is a plain import alias of an otto
package in the same file, or a dotted spelling (``otto``, ``otto.session``)
once any ``import otto`` / ``import otto.<module>`` binds ``otto``. An
expression the file's imports do not resolve is out of scope here; the
teardown guard covers it. So are targets this scan cannot read statically: an
assignment alias (``cfg = otto.config``), an f-string or concatenated target,
a ``target=`` keyword, ``patch.multiple`` and ``patch.dict``. The guard's own
self-tests build their package-level target at run time on purpose, so they
stay invisible here.

The second scan covers EAGER re-exports, which the lazy tables cannot see: a
module that binds, at import, an object another otto module defines (spec
2026-10-11 placement-and-phases §3.13). A patch of the re-export replaces a
binding the owner's own code never reads, so it is reported with the owner to
patch instead. The owner's patch reaches every reader that imports the name
from the owner when called; a module that bound the name at import, the
re-exporting module included, keeps the object it bound. The re-exports are
found by identity at test time (an object whose ``__module__`` names another
otto module that holds that same object under its own name, and that the
scanned module lists in its ``__all__``), so the list is never written by
hand. Objects without a ``__module__`` (a constant, a registry instance) are
out of its reach. A test whose consumer still reads the name through the
re-export, and so must patch it there, is excluded, each exclusion naming the
landing that retires it; an exclusion that matches nothing fails. There is
one: ``session/logs.py`` reads ``get_projects`` through ``otto.bootstrap``, so its
test patches it there until that module reads it from ``otto.declared``.
"""

import ast
import importlib
import inspect
import sys
from pathlib import Path
from typing import NamedTuple

from tests._fixtures._lazy_exports import lazy_export_map, lazy_facade_names
from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return f"{base}.{node.attr}"
    return None


def _package_aliases(tree: ast.AST) -> dict[str, str]:
    """``local expression -> otto package`` for the plain imports in one file."""
    packages = set(lazy_facade_names())
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in packages:
                    aliases[alias.asname or alias.name] = alias.name
                if alias.asname is None and alias.name.startswith("otto."):
                    # `import otto.config` or `import otto.session.lab` binds `otto`;
                    # dotted uses (`otto`, `otto.session`) spell the package.
                    aliases.setdefault("otto", "otto")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in packages:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # A dotted spelling (`otto.config`) names its package once `otto` is imported.
    if "otto" in aliases:
        aliases.update({package: package for package in packages})
    return aliases


class PatchSite(NamedTuple):
    """One patch call whose target is a module attribute."""

    line: int
    module: str
    """The module the target names: a package, or a module *aliases* maps to."""
    name: str


def _patched_names(tree: ast.AST, aliases: "dict[str, str] | None" = None) -> list[PatchSite]:
    """A :class:`PatchSite` for every patch call whose target is a module attribute.

    *aliases* maps a local expression to the module it names; by default, the
    lazy packages' (:func:`_package_aliases`).
    """
    aliases = _package_aliases(tree) if aliases is None else aliases
    found: list[PatchSite] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and len(node.args) >= 1):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        first = node.args[0]
        if callee in ("setattr", "delattr", "patch") and isinstance(first, ast.Constant):
            if isinstance(first.value, str):
                package, _, name = first.value.rpartition(".")
                found.append(PatchSite(node.lineno, package, name))
        elif callee in ("setattr", "delattr", "object") and len(node.args) >= 2:
            if callee == "object" and not (
                isinstance(func, ast.Attribute)
                and (_dotted(func.value) or "").rpartition(".")[2] == "patch"
            ):
                continue
            second = node.args[1]
            package = aliases.get(_dotted(first) or "")
            if package and isinstance(second, ast.Constant) and isinstance(second.value, str):
                found.append(PatchSite(node.lineno, package, second.value))
    return found


def package_level_patches(source: str, filename: str) -> list[str]:
    """One ``file:line pkg.name -> patch defining.attr`` line per package-level patch."""
    exports = lazy_export_map()
    return [
        f"{filename}:{site.line} {target} -> patch {exports[target]}"
        for site in _patched_names(ast.parse(source))
        if (target := f"{site.module}.{site.name}") in exports
    ]


def _offences(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
        return package_level_patches(source, str(path.relative_to(PROJECT_ROOT)))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []  # a deliberately broken fixture file; nothing to read


def test_no_test_patches_a_lazy_export_on_its_package():
    offenders = [line for path in sorted(TESTS_ROOT.rglob("*.py")) for line in _offences(path)]
    assert not offenders, "patch where the name is defined:\n  " + "\n  ".join(offenders)


class TestTheScannerSeesEveryForm:
    """A scan that matched nothing would pass vacuously, so each form is pinned."""

    def scan(self, body: str) -> list[str]:
        return package_level_patches(body, "t.py")

    def test_string_setattr(self):
        got = self.scan('monkeypatch.setattr("otto.config.load_user_settings", f)')
        assert got == [
            (
                "t.py:1 otto.config.load_user_settings"
                " -> patch otto.config.user_settings.load_user_settings"
            )
        ]

    def test_mock_patch_in_its_spellings(self):
        for call in (
            'patch("otto.lab.get_lab")',
            'mock.patch("otto.lab.get_lab")',
            'mocker.patch("otto.lab.get_lab")',
        ):
            assert self.scan(call) == ["t.py:1 otto.lab.get_lab -> patch otto.fleet.get_lab"], call

    def test_a_hop_through_another_lazy_facade_names_the_true_home(self):
        assert self.scan('patch("otto.app")') == ["t.py:1 otto.app -> patch otto.cli.main.app"]

    def test_attribute_forms_through_an_import_alias(self):
        source = (
            "import otto.config as config\n"
            "from otto import config as cfg\n"
            'monkeypatch.setattr(config, "load_user_settings", f)\n'
            'patch.object(cfg, "load_user_settings")\n'
            'mock.patch.object(otto.config, "load_user_settings")\n'
        )
        got = self.scan(source)
        assert [line.split()[0] for line in got] == ["t.py:3", "t.py:4"]

    def test_delattr_in_its_string_and_attribute_forms(self):
        source = (
            "import otto.lab as lab\n"
            'monkeypatch.delattr("otto.config.load_user_settings")\n'
            'monkeypatch.delattr(lab, "get_lab")\n'
        )
        assert self.scan(source) == [
            (
                "t.py:2 otto.config.load_user_settings"
                " -> patch otto.config.user_settings.load_user_settings"
            ),
            "t.py:3 otto.lab.get_lab -> patch otto.fleet.get_lab",
        ]

    def test_a_plain_package_import_binds_the_root_for_dotted_spellings(self):
        source = (
            "import otto.config\n"
            'monkeypatch.setattr(otto, "get_lab", f)\n'
            'monkeypatch.setattr(otto.session, "build_lab", f)\n'
        )
        assert [line.split()[0] for line in self.scan(source)] == ["t.py:2", "t.py:3"]

    def test_dotted_spelling_needs_the_root_import(self):
        source = 'import otto\nmonkeypatch.setattr(otto.config, "load_user_settings", f)\n'
        assert len(self.scan(source)) == 1

    def test_the_defining_module_and_unrelated_targets_pass(self):
        source = (
            "import otto.config as config\n"
            'monkeypatch.setattr("otto.declared.get_projects", f)\n'
            'monkeypatch.setattr("otto.config.load_otto_env", f)\n'
            'monkeypatch.setattr(config, "load_otto_env", f)\n'
            'patch("otto.logger.management._ConsoleHandler")\n'
            'monkeypatch.setattr(thing, "get_projects", f)\n'
        )
        assert self.scan(source) == []


# ── Eager re-exports ──────────────────────────────────────────────────────────

EAGER_REEXPORT_MODULES = ["otto.bootstrap"]
"""The modules whose eager re-exports are scanned: each one a test patches by habit."""


class Exclusion(NamedTuple):
    """A test file whose consumer still reads *name* through the re-export, until *expires*."""

    test_file: str
    name: str
    expires: str


EAGER_REEXPORT_EXCLUSIONS: list[Exclusion] = []
"""None: every consumer reads each name from its owner, so every test patches it there."""


class Offence(NamedTuple):
    """One patch of a re-export: where, what it patched, and the owner to patch instead."""

    test_file: str
    line: int
    target: str
    owner: str

    def render(self) -> str:
        return f"{self.test_file}:{self.line} {self.target} -> patch {self.owner}"


class ScanReport(NamedTuple):
    """The offences no exclusion covers, and the exclusions that covered nothing."""

    offences: list[Offence]
    stale: list[Exclusion]


def eager_reexports(module: str) -> dict[str, str]:
    """``name -> owner.name`` for each name *module* exports that another otto module defines.

    Only the names in *module*'s ``__all__`` count: a helper the module imports
    for its own use (``otto.bootstrap``'s ``registering_project``) is read through
    that binding, so patching it there is right.
    """
    namespace = vars(importlib.import_module(module))
    found = {}
    for name in namespace["__all__"]:
        if name not in namespace:
            continue  # a lazy export: the scan of lazy facades covers its patches
        obj = namespace[name]
        if inspect.ismodule(obj):
            continue
        owner = getattr(obj, "__module__", None)
        if not isinstance(owner, str) or owner == module or owner.split(".")[0] != "otto":
            continue
        attr = getattr(obj, "__name__", name)
        if getattr(sys.modules.get(owner), attr, None) is obj:
            found[name] = f"{owner}.{attr}"
    return found


def _module_aliases(tree: ast.AST, module: str) -> dict[str, str]:
    """``local expression -> module`` for each import in one file that binds *module*.

    A plain ``import otto`` (or ``import otto.<anything>``) binds ``otto``, so
    the dotted spelling of *module* resolves too.
    """
    parent, _, leaf = module.rpartition(".")
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    aliases[alias.asname or module] = module
                elif alias.asname is None and alias.name.partition(".")[0] == "otto":
                    aliases[module] = module
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == parent:
            for alias in node.names:
                if alias.name == leaf:
                    aliases[alias.asname or leaf] = module
    return aliases


def eager_reexport_patches(
    source: str, filename: str, reexports: "dict[str, dict[str, str]]"
) -> list[Offence]:
    """Each patch in *source* of a name a scanned module re-exports (*reexports*: per module)."""
    tree = ast.parse(source)
    found = []
    for module, names in reexports.items():
        aliases = _module_aliases(tree, module)
        found.extend(
            Offence(filename, site.line, f"{module}.{site.name}", names[site.name])
            for site in _patched_names(tree, aliases)
            if site.module == module and site.name in names
        )
    return found


def apply_exclusions(found: list[Offence], exclusions: list[Exclusion]) -> ScanReport:
    """Drop the offences an exclusion covers; report every exclusion that covered none."""

    def covers(exclusion: Exclusion, offence: Offence) -> bool:
        return (
            exclusion.test_file == offence.test_file
            and offence.target.rpartition(".")[2] == exclusion.name
        )

    offences = [o for o in found if not any(covers(e, o) for e in exclusions)]
    stale = [e for e in exclusions if not any(covers(e, o) for o in found)]
    return ScanReport(offences, stale)


def _reexport_offences(path: Path, reexports: "dict[str, dict[str, str]]") -> list[Offence]:
    try:
        source = path.read_text(encoding="utf-8")
        return eager_reexport_patches(source, str(path.relative_to(PROJECT_ROOT)), reexports)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []  # a deliberately broken fixture file; nothing to read


def test_no_test_patches_an_eager_reexport():
    reexports = {module: eager_reexports(module) for module in EAGER_REEXPORT_MODULES}
    found = [
        offence
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        for offence in _reexport_offences(path, reexports)
    ]
    report = apply_exclusions(found, EAGER_REEXPORT_EXCLUSIONS)
    assert not report.offences, "patch where the name is defined:\n  " + "\n  ".join(
        offence.render() for offence in report.offences
    )
    assert not report.stale, f"exclusions that match no patch any more: {report.stale}"


def test_the_bootstrap_error_types_are_re_exported_from_their_owner():
    """Anti-vacuity: the scan finds the four error types and the composed state's four names."""
    found = eager_reexports("otto.bootstrap")
    for name in ["BootstrapError", "BootstrapWarning", "DependencyError", "ProjectScopeError"]:
        assert found.get(name) == f"otto.config.scope.{name}", (name, found.get(name))
    for name in ["BootstrapResult", "get_projects", "get_ordered_projects", "is_bootstrapped"]:
        assert found.get(name) == f"otto.declared.{name}", (name, found.get(name))
    assert "bootstrap" not in found, "otto.bootstrap's own function counted as a re-export"
    assert "registering_project" not in found, "a helper otto.bootstrap uses counted as a re-export"


_FIXED_REEXPORTS = {
    "otto.bootstrap": {
        "BootstrapError": "otto.config.scope.BootstrapError",
        "ProjectScopeError": "otto.config.scope.ProjectScopeError",
        "get_projects": "otto.declared.get_projects",
    }
}
"""The self-tests' re-export table: fixed, so they do not depend on the tree's."""


class TestTheEagerScanSeesEveryForm:
    """Each form of the eager scan, and its exclusions, on a fixed re-export table."""

    def scan(self, body: str) -> list[str]:
        return [o.render() for o in eager_reexport_patches(body, "t.py", _FIXED_REEXPORTS)]

    def test_a_moved_error_type_patched_by_string(self):
        got = self.scan('monkeypatch.setattr("otto.bootstrap.BootstrapError", f)')
        assert got == [
            "t.py:1 otto.bootstrap.BootstrapError -> patch otto.config.scope.BootstrapError"
        ]

    def test_a_moved_error_type_patched_through_an_import(self):
        source = (
            "from otto import bootstrap as bs\n"
            "import otto.bootstrap\n"
            'monkeypatch.setattr(bs, "ProjectScopeError", f)\n'
            'patch.object(otto.bootstrap, "BootstrapError")\n'
        )
        assert [line.split()[0] for line in self.scan(source)] == ["t.py:3", "t.py:4"]

    def test_the_root_package_import_binds_the_dotted_spelling(self):
        for imported in ("otto", "otto.config"):
            source = f'import {imported}\npatch.object(otto.bootstrap, "ProjectScopeError")\n'
            assert self.scan(source) == [
                (
                    "t.py:2 otto.bootstrap.ProjectScopeError"
                    " -> patch otto.config.scope.ProjectScopeError"
                )
            ], imported

    def test_a_moved_accessor_patched_at_the_root(self):
        source = (
            "from otto import bootstrap as bs\n"
            'monkeypatch.setattr("otto.bootstrap.get_projects", f)\n'
            'monkeypatch.setattr(bs, "get_projects", f)\n'
        )
        assert self.scan(source) == [
            "t.py:2 otto.bootstrap.get_projects -> patch otto.declared.get_projects",
            "t.py:3 otto.bootstrap.get_projects -> patch otto.declared.get_projects",
        ]

    def test_the_owner_and_the_root_s_own_names_pass(self):
        source = (
            "from otto import bootstrap as bs\n"
            'monkeypatch.setattr("otto.config.scope.BootstrapError", f)\n'
            'monkeypatch.setattr("otto.declared.get_projects", f)\n'
            'monkeypatch.setattr("otto.bootstrap.bootstrap", f)\n'
            'monkeypatch.setattr(bs, "bootstrap", f)\n'
            'monkeypatch.setattr(thing, "BootstrapError", f)\n'
        )
        assert self.scan(source) == []

    def test_an_exclusion_covers_its_file_and_name_only(self):
        found = eager_reexport_patches(
            'monkeypatch.setattr("otto.bootstrap.BootstrapError", f)\n'
            'monkeypatch.setattr("otto.bootstrap.ProjectScopeError", f)\n',
            "t.py",
            _FIXED_REEXPORTS,
        )
        report = apply_exclusions(found, [Exclusion("t.py", "BootstrapError", "P9b")])
        assert [o.target for o in report.offences] == ["otto.bootstrap.ProjectScopeError"]
        assert report.stale == []

    def test_a_stale_exclusion_is_reported(self):
        stale = Exclusion("t.py", "DependencyError", "P9b")
        found = eager_reexport_patches(
            'monkeypatch.setattr("otto.bootstrap.BootstrapError", f)\n', "t.py", _FIXED_REEXPORTS
        )
        report = apply_exclusions(found, [stale])
        assert report.stale == [stale]
        assert len(report.offences) == 1
