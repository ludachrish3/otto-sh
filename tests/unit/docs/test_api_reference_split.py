"""docs/api splits into Public API pages and Internals pages (spec 1 §6 "API reference").

A declared namespace is rendered once, by a bare ``automodule`` outside
``docs/api/internals/``, which follows its ``__all__``. Every other module is
rendered under ``docs/api/internals/`` with ``:ignore-module-all:``, and a
directive that renders one object lives there too. The test reads the .rst
sources, so it fails before a docs build would.
"""

import importlib
import re

import pytest

from scripts.api_manifest import load_manifest
from scripts.docs_api_reference import MANIFEST, build_reference, load_namespaces, trackable
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

API = PROJECT_ROOT / "docs" / "api"
INTERNALS = API / "internals"
DECLARED = set(load_manifest(MANIFEST))
_DIRECTIVE = re.compile(r"^\.\. (auto\w+):: *(\S+)[ \t]*\n((?:[ \t]+:[\w-]+:.*\n)*)", re.MULTILINE)
_PUBLIC_FORBIDDEN = {"ignore-module-all", "no-index", "noindex", "no-members"}


def _directives() -> "list[tuple[str, str, str, set[str]]]":
    out = []
    for page in sorted(API.rglob("*.rst")):
        for m in _DIRECTIVE.finditer(page.read_text(encoding="utf-8")):
            options = set(re.findall(r"^[ \t]+:([\w-]+):", m.group(3), re.MULTILINE))
            out.append((str(page.relative_to(API)), m.group(1), m.group(2), options))
    return out


def _internal(page: str) -> bool:
    return page.startswith("internals/")


def test_every_declared_namespace_has_one_public_automodule():
    found: dict[str, list[tuple[str, set[str]]]] = {}
    for page, kind, target, options in _directives():
        if kind == "automodule" and target in DECLARED:
            found.setdefault(target, []).append((page, options))
    problems = []
    for namespace in sorted(DECLARED):
        entries = found.get(namespace, [])
        if len(entries) != 1:
            problems.append(f"{namespace}: {len(entries)} automodule directives, want 1")
            continue
        page, options = entries[0]
        if _internal(page):
            problems.append(f"{namespace}: rendered under internals/ ({page})")
        if options & _PUBLIC_FORBIDDEN:
            problems.append(f"{namespace}: {page} carries {sorted(options & _PUBLIC_FORBIDDEN)}")
    assert problems == []


def test_every_other_module_renders_on_an_internals_page_ignoring_its_all():
    problems = [
        f"{page}: {target}"
        for page, kind, target, options in _directives()
        if kind == "automodule"
        and target not in DECLARED
        and (not _internal(page) or "ignore-module-all" not in options)
    ]
    assert problems == []


def test_a_directive_for_one_object_lives_on_an_internals_page():
    problems = [
        f"{page}: {kind} {target}"
        for page, kind, target, _ in _directives()
        if kind != "automodule" and not _internal(page)
    ]
    assert problems == []


def test_the_index_holds_both_halves_and_the_stability_page():
    text = (API / "index.rst").read_text(encoding="utf-8")
    assert ":caption: Public API" in text
    assert ":caption: Internals" in text
    assert re.search(r"^   stability$", text, re.MULTILINE)
    assert re.search(r"^   internals/index$", text, re.MULTILINE)


def _resolve(target: str) -> object:
    """Import *target*'s longest importable module prefix, then walk the rest as attributes.

    A directive can name a nested object (``.. automethod:: otto.x.Cls.method``).
    """
    parts = target.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for part in parts[cut:]:
            obj = getattr(obj, part)
        return obj
    raise AssertionError(f"{target}: no importable module prefix")


def test_no_internals_directive_renders_an_object_with_a_public_home():
    """An explicit ``auto*`` on an Internals page never indexes a public object again.

    The skip rule keeps public objects off ``:ignore-module-all:`` entries, but an
    explicit directive bypasses it: the object would be indexed at its public home
    and again at its defining path, and a bare reference to it becomes ambiguous.
    """
    ref = build_reference(load_namespaces())
    problems = []
    for page, kind, target, _ in _directives():
        if kind == "automodule" or not _internal(page):
            continue
        obj = _resolve(target)
        if trackable(obj) and id(obj) in ref.homes:
            problems.append(f"{page}: {target} (home {ref.homes[id(obj)].path})")
    assert problems == []
