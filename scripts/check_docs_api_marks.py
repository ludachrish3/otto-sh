"""Check the BUILT API reference's marks against the declaration.

Reads the HTML and ``objects.inv`` that Sphinx wrote, never the hooks that
wrote them (``docs/conf.py``), so a hook that stops marking a page fails
here. The rules are ``docs/api/stability.md``'s "How these pages mark it":

* every declared namespace has its module anchor on a Public API page, not
  under ``api/internals/``. Its section carries ``otto-stability-<tier>``
  unless the tier is ``stable``, and a stable section carries no stability
  mark at all;
* every public object is indexed once, in the section of a namespace that
  holds it, and every other holder's section links to that anchor;
* no public object, under any path, is indexed on an Internals page;
* every other module anchor under ``api/`` is on an Internals page, and its
  section carries ``otto-internal``;
* no signature on a Public API page, and no data or attribute signature on
  an Internals page, shows ``object``'s default repr
  (``<otto.registry.Registry object>``), a default that is not Python
  (``<factory>``, ``<class '…'>``, ``<function …>``), CLI metadata
  (``Annotated[T, otto.utils.Opt(…)]``) or a raw ``~otto.`` prefix outside a
  string literal. The data and attribute documenters leave a default-repr
  value out (``scripts/docs_api_reference.py``'s ``shows_value``), and
  ``docs/conf.py`` renders CLI metadata away (``drop_annotated_markers``) and
  each other default as Python (``factory_default``, ``object_default``).
  Text that is not Python makes Sphinx show the whole signature raw, which is
  what lets a ``~otto.`` prefix through. An Internals page's other signatures
  are not checked: CLI internals take Typer's ``OptionInfo`` objects as
  defaults;
* while the release is before 1.0, every page carries ``otto-api-provisional``.

Usage: ``python scripts/check_docs_api_marks.py docs/_build/html [--manifest PATH]``.
It prints one ``FAIL`` line per finding and exits 1, or prints ``api marks: OK``.
"""

import argparse
import html
import importlib
import io
import posixpath
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, NamedTuple

from typing_extensions import Self

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.api_manifest import load_manifest  # noqa: E402 -- path set up above
from scripts.docs_api_reference import (  # noqa: E402 -- path set up above
    MANIFEST,
    blank_string_literals,
    trackable,
)

INTERNALS = "api/internals/"
NOTICE = "otto-api-provisional"
_MISSING = object()
_MODULE_ANCHOR = re.compile(r'id="module-([\w.]+)"')
_VERSION = re.compile(rb"^# Version: (.*)$", re.MULTILINE)
_VALUE_SIGNATURE = re.compile(
    r'<dl class="py (?:data|attribute)\b[^"]*">\s*<dt\b([^>]*)>(.*?)</dt>', re.DOTALL
)
_SIGNATURE = re.compile(r"<dt\b([^>]*)>(.*?)</dt>", re.DOTALL)
_ID = re.compile(r'\bid="([^"]*)"')
_TAG = re.compile(r"<[^>]+>")


class _Leak(NamedTuple):
    """One shape a signature must not show."""

    #: What to find; its ``shown`` group is what a finding quotes.
    pattern: "re.Pattern[str]"
    #: How a finding names it, with ``{}`` for the ``shown`` text.
    template: str
    #: Whether a match inside a quoted string literal counts.
    inside_strings: bool = True


#: What a signature must not show. A signature that holds any text which is
#: not Python (all but the last) falls back to raw text, where every
#: ``~otto.`` prefix shows. That last rule reads only the text outside string
#: literals: a string default may legitimately say ``'~otto.x'``.
_LEAKS = [
    _Leak(
        re.compile(r"<(?P<shown>[\w.]+) object(?: at 0x[0-9a-fA-F]+)?>"),
        "a default repr (<{} object>)",
    ),
    _Leak(re.compile(r"(?P<shown><factory>)"), "a default that is not Python ({})"),
    _Leak(re.compile(r"(?P<shown><class '[^']*'>)"), "a default that is not Python ({})"),
    _Leak(
        re.compile(r"(?P<shown><function [^\s<>]*(?:<[^<>]*>[^\s<>]*)*>)"),
        "a default that is not Python ({})",
    ),
    _Leak(re.compile(r"[\[,]\s*(?P<shown>~?otto\.utils\.(?:Arg|Opt))\("), "CLI metadata ({}(…))"),
    _Leak(
        re.compile(r"(?P<shown>~otto\.)"),
        "a raw {} prefix: Sphinx could not parse it",
        inside_strings=False,
    ),
]


@dataclass
class Site:
    """What a built docs tree indexes: its version, module anchors and object anchors."""

    root: Path
    version: str
    modules: "dict[str, str]"
    objects: "dict[str, str]"

    def text(self, uri: str) -> str:
        """Return the HTML of the page *uri* points into."""
        return (self.root / page_of(uri)).read_text(encoding="utf-8")


def page_of(uri: str) -> str:
    """Return the page part of an inventory URI."""
    return uri.split("#", 1)[0]


class _Stream:
    """The bytes of ``objects.inv`` as the stream ``InventoryFile.load`` reads.

    Sphinx's stream protocol takes ``read(size=...)`` by keyword, which the
    stdlib's positional-only ``read`` does not declare.
    """

    def __init__(self, raw: bytes) -> None:
        self._buffer = io.BytesIO(raw)

    def read(self, size: int = -1) -> bytes:
        """Return up to *size* bytes, or the rest when *size* is negative."""
        return self._buffer.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._buffer.close()


def load_site(root: Path) -> Site:
    """Read *root*'s ``objects.inv``: its version, py:module anchors and other py anchors."""
    from sphinx.util.inventory import InventoryFile

    raw = (root / "objects.inv").read_bytes()
    match = _VERSION.search(raw)
    version = match.group(1).decode().strip() if match else ""
    inventory = InventoryFile.load(_Stream(raw), "", posixpath.join)
    modules = {name: inventory_uri(entry) for name, entry in inventory.get("py:module", {}).items()}
    objects: dict[str, str] = {}
    for kind, entries in inventory.items():
        if kind.startswith("py:") and kind not in ("py:module", "py:parameter"):
            objects.update({name: inventory_uri(entry) for name, entry in entries.items()})
    return Site(root, version, modules, objects)


def inventory_uri(entry: Any) -> str:
    """Return an ``objects.inv`` entry's URI, on every Sphinx the lanes resolve.

    Sphinx 8.1 (Python 3.10) loads each entry as a
    ``(project, version, uri, display_name)`` tuple. Sphinx 8.2 and later load
    an ``_InventoryItem`` whose ``uri`` attribute is the supported spelling, and
    whose tuple indexing warns (an error under the suite's warning filters).
    """
    return entry[2] if isinstance(entry, tuple) else entry.uri


def section(text: str, module: str) -> "str | None":
    """Return *text* from *module*'s anchor up to the next module anchor, or None.

    A section starts at the tag that carries the anchor: docutils moves a
    module's target id onto its first element, which is often the banner.
    """
    marks = [(text.rfind("<", 0, m.start()), m.group(1)) for m in _MODULE_ANCHOR.finditer(text)]
    for i, (start, name) in enumerate(marks):
        if name == module:
            return text[start : marks[i + 1][0] if i + 1 < len(marks) else len(text)]
    return None


def _before_1_0(version: str) -> bool:
    major = version.split(".", 1)[0]
    return major.isdigit() and int(major) < 1


def _namespace_findings(
    site: Site, stabilities: "dict[str, str]", sections: "dict[str, str]"
) -> "list[str]":
    """Each declared namespace: on a Public API page, with its tier's banner and no other mark.

    Fills *sections* with each namespace's section of HTML for the other rules.
    """
    out: list[str] = []
    for namespace in sorted(stabilities):
        uri = site.modules.get(namespace)
        if uri is None:
            out.append(f"{namespace}: no module anchor in objects.inv")
            continue
        if page_of(uri).startswith(INTERNALS):
            out.append(f"{namespace}: rendered on an Internals page ({page_of(uri)})")
        body = section(site.text(uri), namespace) or ""
        sections[namespace] = body
        tier = stabilities[namespace]
        if tier == "stable":
            if "otto-stability" in body:
                out.append(f"{namespace}: stable, yet its section carries a stability mark")
        elif f"otto-stability-{tier}" not in body:
            out.append(f"{namespace}: {tier}, and its section carries no {tier} banner")
        if "otto-internal" in body:
            out.append(f"{namespace}: declared, yet its section carries the internal note")
    return out


def _homes(site: Site, group: "list[str]", sections: "dict[str, str]") -> "list[str]":
    """Return the paths of *group* indexed on their namespace's page, inside its section."""

    def indexed_in_section(path: str) -> bool:
        namespace = path.rsplit(".", 1)[0]
        uri, module_uri = site.objects.get(path), site.modules.get(namespace)
        return (
            uri is not None
            and module_uri is not None
            and page_of(uri) == page_of(module_uri)
            and f'id="{path}"' in sections.get(namespace, "")
        )

    return [path for path in group if indexed_in_section(path)]


def _group_findings(
    site: Site, groups: "list[list[str]]", sections: "dict[str, str]"
) -> "list[str]":
    """Each public object: indexed once in a holder's section, linked from the other holders."""
    out: list[str] = []
    for group in groups:
        homes = _homes(site, group, sections)
        if not homes:
            out.append(f"{group[0]}: indexed in no declared namespace's section")
        elif len(homes) > 1:
            out.append(f"{homes[0]}: indexed in {len(homes)} sections ({', '.join(homes)})")
        else:
            for path in group:
                holder = sections.get(path.rsplit(".", 1)[0], "")
                if path not in homes and not any(f'#{home}"' in holder for home in homes):
                    out.append(f"{path}: its section does not link to {homes[0]}")
        for path in group:
            uri = site.objects.get(path)
            if uri is not None and page_of(uri).startswith(INTERNALS):
                out.append(f"{path}: public, yet indexed on an Internals page")
    return out


def _resolve(path: str) -> object:
    """Return the module attribute *path* names (a module, then one name), or ``_MISSING``."""
    module_name, _, name = path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return _MISSING
    return getattr(module, name, _MISSING)


def _reindexed_findings(
    site: Site, groups: "list[list[str]]", sections: "dict[str, str]"
) -> "list[str]":
    """No public object is indexed again, at another path, on an Internals page.

    An explicit directive on an Internals page indexes its target at the
    defining module's path, which no public group names: only the object's
    identity shows it is public.
    """
    public: dict[int, str] = {}
    for group in groups:
        home = (_homes(site, group, sections) or group)[0]
        for path in group:
            obj = _resolve(path)
            if obj is not _MISSING and trackable(obj):
                public.setdefault(id(obj), home)
    named = {path for group in groups for path in group}
    out: list[str] = []
    for name, uri in sorted(site.objects.items()):
        if name in named or not page_of(uri).startswith(INTERNALS):
            continue
        obj = _resolve(name)
        if obj is not _MISSING and trackable(obj) and id(obj) in public:
            out.append(
                f"{name}: indexed on an Internals page, yet it is public as {public[id(obj)]}"
            )
    return out


def _internal_findings(site: Site, stabilities: "dict[str, str]") -> "list[str]":
    """Every other module under ``api/``: on an Internals page, with the internal note."""
    out: list[str] = []
    for module, uri in sorted(site.modules.items()):
        if module in stabilities or not page_of(uri).startswith("api/"):
            continue
        if not page_of(uri).startswith(INTERNALS):
            out.append(f"{module}: undeclared, yet rendered outside api/internals/")
        body = section(site.text(uri), module) or ""
        if "otto-internal" not in body:
            out.append(f"{module}: its section carries no internal note")
        if "otto-stability" in body:
            out.append(f"{module}: internal, yet its section carries a stability mark")
    return out


def _signature_leak(text: str) -> "str | None":
    """Return how a finding names what signature *text* must not show, or None."""
    bare = blank_string_literals(text)
    for leak in _LEAKS:
        match = leak.pattern.search(text if leak.inside_strings else bare)
        if match:
            return leak.template.format(match.group("shown"))
    return None


def _signature_findings(site: Site, pages: "list[str]") -> "list[str]":
    """No signature on a Public API page, or data signature on Internals, shows what is not Python.

    That is ``object``'s default repr, a ``<factory>``/``<class '…'>``/
    ``<function …>`` default, CLI metadata, or a raw ``~otto.`` prefix outside
    a string literal. On an Internals page only data and attribute signatures
    are read: CLI internals take Typer's ``OptionInfo`` objects as defaults.
    Sphinx highlights a shown value token by token, so this reads the text of
    each ``<dt>`` signature, not its markup.
    """
    out: list[str] = []
    for page in pages:
        if not page.startswith("api/"):
            continue
        signatures = _VALUE_SIGNATURE if page.startswith(INTERNALS) else _SIGNATURE
        for attrs, body in signatures.findall((site.root / page).read_text(encoding="utf-8")):
            leak = _signature_leak(html.unescape(_TAG.sub("", body)))
            if leak:
                anchor = _ID.search(attrs)
                out.append(f"{page}: {anchor.group(1) if anchor else '<dt>'} shows {leak}")
    return out


def findings(
    site: Site, stabilities: "dict[str, str]", groups: "list[list[str]]", pages: "list[str]"
) -> "list[str]":
    """Return every way the built site breaks the marking rules.

    *stabilities* maps each declared namespace to its tier. Each of *groups*
    lists the public paths (``namespace.name``) of one object. *pages* lists
    every HTML page, relative to the site root.
    """
    out: list[str] = []
    if not (site.root / "api/stability.html").is_file():
        out.append("api/stability.html: missing")
    sections: dict[str, str] = {}
    out.extend(_namespace_findings(site, stabilities, sections))
    out.extend(_group_findings(site, groups, sections))
    out.extend(_reindexed_findings(site, groups, sections))
    out.extend(_internal_findings(site, stabilities))
    out.extend(_signature_findings(site, pages))
    if _before_1_0(site.version):
        out.extend(
            f"{page}: no pre-1.0 notice"
            for page in pages
            if NOTICE not in (site.root / page).read_text(encoding="utf-8")
        )
    return out


def public_groups(stabilities: "dict[str, str]") -> "list[list[str]]":
    """Group every declared namespace's ``__all__`` paths by the object they name."""
    by_object: dict[object, list[str]] = {}
    for namespace in sorted(stabilities):
        module = importlib.import_module(namespace)
        for name in getattr(module, "__all__", []):
            obj = getattr(module, name)
            if isinstance(obj, types.ModuleType):
                continue
            key: object = id(obj) if trackable(obj) else f"{namespace}.{name}"
            by_object.setdefault(key, []).append(f"{namespace}.{name}")
    return list(by_object.values())


def html_pages(root: Path) -> "list[str]":
    """Every built HTML page, skipping Sphinx's ``_static``/``_images``/``_sources`` trees."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.html")
        if not path.relative_to(root).parts[0].startswith("_")
    )


def main(argv: "list[str]") -> int:
    """Check a built docs tree; print FAIL lines and return 1, or print OK and return 0."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("html", type=Path)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    stabilities = {name: ns.stability for name, ns in load_manifest(args.manifest).items()}
    site = load_site(args.html)
    problems = findings(site, stabilities, public_groups(stabilities), html_pages(args.html))
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print("api marks: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
