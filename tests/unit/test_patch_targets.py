"""A test patches a lazily exported name where it is DEFINED, never on its package.

``monkeypatch.setattr("otto.config.get_repos", fake)`` leaves the real object
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
"""

import ast
from pathlib import Path

from tests._fixtures._lazy_exports import lazy_export_map, lazy_package_names
from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return f"{base}.{node.attr}"
    return None


def _package_aliases(tree: ast.AST) -> dict[str, str]:
    """``local expression -> otto package`` for the plain imports in one file."""
    packages = set(lazy_package_names())
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


def _patched_names(tree: ast.AST) -> list[tuple[int, str, str]]:
    """``(line, package, name)`` for every patch call whose target is a package attribute."""
    aliases = _package_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and len(node.args) >= 1):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        first = node.args[0]
        if callee in ("setattr", "delattr", "patch") and isinstance(first, ast.Constant):
            if isinstance(first.value, str):
                package, _, name = first.value.rpartition(".")
                found.append((node.lineno, package, name))
        elif callee in ("setattr", "delattr", "object") and len(node.args) >= 2:
            if callee == "object" and not (
                isinstance(func, ast.Attribute)
                and (_dotted(func.value) or "").rpartition(".")[2] == "patch"
            ):
                continue
            second = node.args[1]
            package = aliases.get(_dotted(first) or "")
            if package and isinstance(second, ast.Constant) and isinstance(second.value, str):
                found.append((node.lineno, package, second.value))
    return found


def package_level_patches(source: str, filename: str) -> list[str]:
    """One ``file:line pkg.name -> patch defining.attr`` line per package-level patch."""
    exports = lazy_export_map()
    return [
        f"{filename}:{line} {package}.{name} -> patch {exports[f'{package}.{name}']}"
        for line, package, name in _patched_names(ast.parse(source))
        if f"{package}.{name}" in exports
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
        got = self.scan('monkeypatch.setattr("otto.config.get_repos", f)')
        assert got == ["t.py:1 otto.config.get_repos -> patch otto.config.bootstrapped.get_repos"]

    def test_mock_patch_in_its_spellings(self):
        for call in (
            'patch("otto.config.get_lab")',
            'mock.patch("otto.config.get_lab")',
            'mocker.patch("otto.config.get_lab")',
        ):
            assert self.scan(call) == [
                "t.py:1 otto.config.get_lab -> patch otto.config.fleet.get_lab"
            ], call

    def test_a_hop_through_another_lazy_package_names_the_true_home(self):
        assert self.scan('patch("otto.get_lab")') == [
            "t.py:1 otto.get_lab -> patch otto.config.fleet.get_lab"
        ]

    def test_attribute_forms_through_an_import_alias(self):
        source = (
            "import otto.config as config\n"
            "from otto import config as cfg\n"
            'monkeypatch.setattr(config, "get_repos", f)\n'
            'patch.object(cfg, "get_repos")\n'
            'mock.patch.object(otto.config, "get_repos")\n'
        )
        got = self.scan(source)
        assert [line.split()[0] for line in got] == ["t.py:3", "t.py:4"]

    def test_delattr_in_its_string_and_attribute_forms(self):
        source = (
            "import otto.config as config\n"
            'monkeypatch.delattr("otto.config.get_repos")\n'
            'monkeypatch.delattr(config, "get_lab")\n'
        )
        assert self.scan(source) == [
            "t.py:2 otto.config.get_repos -> patch otto.config.bootstrapped.get_repos",
            "t.py:3 otto.config.get_lab -> patch otto.config.fleet.get_lab",
        ]

    def test_a_plain_package_import_binds_the_root_for_dotted_spellings(self):
        source = (
            "import otto.config\n"
            'monkeypatch.setattr(otto, "get_lab", f)\n'
            'monkeypatch.setattr(otto.session, "build_lab", f)\n'
        )
        assert [line.split()[0] for line in self.scan(source)] == ["t.py:2", "t.py:3"]

    def test_dotted_spelling_needs_the_root_import(self):
        source = 'import otto\nmonkeypatch.setattr(otto.config, "get_repos", f)\n'
        assert len(self.scan(source)) == 1

    def test_the_defining_module_and_unrelated_targets_pass(self):
        source = (
            "import otto.config as config\n"
            'monkeypatch.setattr("otto.config.bootstrapped.get_repos", f)\n'
            'monkeypatch.setattr("otto.config.load_otto_env", f)\n'
            'monkeypatch.setattr(config, "load_otto_env", f)\n'
            'patch("otto.logger.management._ConsoleHandler")\n'
            'monkeypatch.setattr(thing, "get_repos", f)\n'
        )
        assert self.scan(source) == []
