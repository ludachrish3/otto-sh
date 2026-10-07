"""``scripts/check_docs_api_marks.py`` reads the BUILT reference, never the hooks that marked it.

Each case builds a small site in tmp_path (an ``objects.inv`` and its pages),
plants one fault, and expects the check to name it. Stable is never marked.
"""

import zlib

import pytest

from scripts.check_docs_api_marks import findings, inventory_uri, load_site

pytestmark = pytest.mark.interpreter_agnostic

NOTICE = '<span class="otto-api-provisional">provisional until 1.0</span>'
BANNER = '<div class="admonition otto-stability otto-stability-provisional">p</div>'
INTERNAL = '<div class="admonition otto-internal">i</div>'
HOST = (
    NOTICE + '<h1 id="module-otto.host">host</h1>' + BANNER + '<dt id="otto.host.UnixHost">U</dt>'
)
ROOT = (
    NOTICE
    + '<h1 id="module-otto">otto</h1>'
    + BANNER
    + '<a href="host/index.html#otto.host.UnixHost">U</a>'
)
UNIX = NOTICE + '<h1 id="module-otto.host.unix_host">unix_host</h1>' + INTERNAL
STABILITY = NOTICE + "<h1>API stability</h1>"
STABILITIES = {"otto": "provisional", "otto.host": "provisional"}
GROUPS = [["otto.UnixHost", "otto.host.UnixHost"]]


def _site(tmp_path, *, pages=None, objects=None, version="0.16.1"):
    pages = pages or {
        "api/otto.html": ROOT,
        "api/host/index.html": HOST,
        "api/internals/host/unixhost.html": UNIX,
        "api/stability.html": STABILITY,
    }
    objects = objects or [
        ("otto", "py:module", "api/otto.html#module-otto"),
        ("otto.host", "py:module", "api/host/index.html#module-otto.host"),
        (
            "otto.host.unix_host",
            "py:module",
            "api/internals/host/unixhost.html#module-otto.host.unix_host",
        ),
        ("otto.host.UnixHost", "py:class", "api/host/index.html#otto.host.UnixHost"),
    ]
    for rel, html in pages.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    body = "".join(f"{name} {role} 1 {uri} -\n" for name, role, uri in objects)
    head = (
        "# Sphinx inventory version 2\n# Project: otto\n"
        f"# Version: {version}\n# The remainder of this file is compressed using zlib.\n"
    )
    (tmp_path / "objects.inv").write_bytes(head.encode() + zlib.compress(body.encode()))
    return load_site(tmp_path), sorted(pages)


def test_a_marked_tree_passes(tmp_path):
    site, pages = _site(tmp_path)
    assert site.version == "0.16.1"
    assert findings(site, STABILITIES, GROUPS, pages) == []


class _InventoryItem:
    """The entry shape Sphinx after 8.1 loads: ``uri`` is read, tuple indexing warns."""

    def __init__(self, uri: str) -> None:
        self.uri = uri

    def __getitem__(self, key: int) -> str:
        raise AssertionError(f"read through the deprecated tuple interface (key {key})")


def test_an_inventory_entry_is_read_by_its_uri_on_every_sphinx():
    """The docs lanes run Sphinx 8.1 (tuples) and later releases (items); both read the same URI."""
    uri = "api/host/index.html#otto.host.UnixHost"
    assert inventory_uri(("otto", "0.16.1", uri, "-")) == uri
    assert inventory_uri(_InventoryItem(uri)) == uri


def test_the_site_reads_item_shaped_entries(tmp_path, monkeypatch):
    """``load_site`` reads every entry through its URI, so Sphinx 8.2+ items load on any lane."""
    from sphinx.util.inventory import InventoryFile

    items = {
        "py:module": {"otto.host": _InventoryItem("api/host/index.html#module-otto.host")},
        "py:class": {
            "otto.host.UnixHost": _InventoryItem("api/host/index.html#otto.host.UnixHost")
        },
    }
    _site(tmp_path)
    monkeypatch.setattr(InventoryFile, "load", staticmethod(lambda *_args: items))
    site = load_site(tmp_path)
    assert site.modules == {"otto.host": "api/host/index.html#module-otto.host"}
    assert site.objects == {"otto.host.UnixHost": "api/host/index.html#otto.host.UnixHost"}


def test_a_provisional_module_needs_its_banner(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST.replace(BANNER, ""),
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.host: provisional, and its section carries no provisional banner"
    ]


def test_a_mark_on_the_element_that_carries_the_anchor_counts(tmp_path):
    """Docutils moves a module's target id onto its first element: here, the banner itself."""
    host = (
        NOTICE
        + '<div class="otto-stability otto-stability-provisional admonition" id="module-otto.host">'
        + 'p</div><dt id="otto.host.UnixHost">U</dt>'
    )
    unix = NOTICE + '<div class="otto-internal admonition" id="module-otto.host.unix_host">i</div>'
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": host,
            "api/internals/host/unixhost.html": unix,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == []


def test_stable_is_never_marked(tmp_path):
    site, pages = _site(tmp_path)
    stable = {"otto": "provisional", "otto.host": "stable"}
    assert findings(site, stable, GROUPS, pages) == [
        "otto.host: stable, yet its section carries a stability mark"
    ]


def test_a_public_name_indexed_on_an_internals_page_fails(tmp_path):
    site, pages = _site(
        tmp_path,
        objects=[
            ("otto", "py:module", "api/otto.html#module-otto"),
            ("otto.host", "py:module", "api/host/index.html#module-otto.host"),
            (
                "otto.host.unix_host",
                "py:module",
                "api/internals/host/unixhost.html#module-otto.host.unix_host",
            ),
            (
                "otto.host.UnixHost",
                "py:class",
                "api/internals/host/unixhost.html#otto.host.UnixHost",
            ),
        ],
    )
    got = findings(site, STABILITIES, GROUPS, pages)
    assert "otto.UnixHost: indexed in no declared namespace's section" in got
    assert "otto.host.UnixHost: public, yet indexed on an Internals page" in got


def test_every_other_holder_links_to_the_home(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT.replace("#otto.host.UnixHost", "#elsewhere"),
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.UnixHost: its section does not link to otto.host.UnixHost"
    ]


def test_an_internal_module_needs_its_note_and_its_place(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX.replace(INTERNAL, ""),
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.host.unix_host: its section carries no internal note"
    ]


def test_a_declared_namespace_on_an_internals_page_fails(tmp_path):
    site, pages = _site(
        tmp_path,
        objects=[
            ("otto", "py:module", "api/otto.html#module-otto"),
            ("otto.host", "py:module", "api/internals/host/unixhost.html#module-otto.host"),
            (
                "otto.host.unix_host",
                "py:module",
                "api/internals/host/unixhost.html#module-otto.host.unix_host",
            ),
            ("otto.host.UnixHost", "py:class", "api/host/index.html#otto.host.UnixHost"),
        ],
    )
    assert "otto.host: rendered on an Internals page (api/internals/host/unixhost.html)" in (
        findings(site, STABILITIES, GROUPS, pages)
    )


def test_every_page_carries_the_notice_before_1_0_only(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY.replace(NOTICE, ""),
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == ["api/stability.html: no pre-1.0 notice"]
    released, pages = _site(tmp_path, version="1.0.0")
    assert findings(released, STABILITIES, GROUPS, pages) == []


def test_the_stability_page_must_exist(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == ["api/stability.html: missing"]


OBJECTS = [
    ("otto", "py:module", "api/otto.html#module-otto"),
    ("otto.host", "py:module", "api/host/index.html#module-otto.host"),
    (
        "otto.host.unix_host",
        "py:module",
        "api/internals/host/unixhost.html#module-otto.host.unix_host",
    ),
    ("otto.host.UnixHost", "py:class", "api/host/index.html#otto.host.UnixHost"),
]


def test_a_public_object_indexed_again_on_an_internals_page_fails(tmp_path):
    """An explicit Internals directive re-indexes a public object at its defining path."""
    unix = UNIX + '<dt id="otto.host.unix_host.UnixHost">U</dt>'
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": unix,
            "api/stability.html": STABILITY,
        },
        objects=[
            *OBJECTS,
            (
                "otto.host.unix_host.UnixHost",
                "py:class",
                "api/internals/host/unixhost.html#otto.host.unix_host.UnixHost",
            ),
        ],
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        (
            "otto.host.unix_host.UnixHost: indexed on an Internals page, "
            "yet it is public as otto.host.UnixHost"
        )
    ]


def test_an_object_indexed_in_two_public_sections_fails(tmp_path):
    root = ROOT + '<dt id="otto.UnixHost">U</dt>'
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": root,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
        objects=[*OBJECTS, ("otto.UnixHost", "py:class", "api/otto.html#otto.UnixHost")],
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.UnixHost: indexed in 2 sections (otto.UnixHost, otto.host.UnixHost)"
    ]


def test_an_undeclared_module_outside_internals_fails(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
        objects=[
            *OBJECTS[:2],
            (
                "otto.host.unix_host",
                "py:module",
                "api/host/unixhost.html#module-otto.host.unix_host",
            ),
            OBJECTS[3],
        ],
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.host.unix_host: undeclared, yet rendered outside api/internals/"
    ]


def test_an_internal_module_with_a_stability_mark_fails(tmp_path):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": UNIX + BANNER,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "otto.host.unix_host: internal, yet its section carries a stability mark"
    ]


_REGISTRY_VALUE = (
    '<span class="p"> <span class="pre">=</span> </span><code class="code python">'
    '<span class="o">&lt;</span><span class="n">otto</span><span class="o">.</span>'
    '<span class="n">host</span><span class="o">.</span><span class="n">_Registry</span> '
    '<span class="nb">object</span><span class="o">&gt;</span></code>'
)
_FUNCTION = (
    '<dl class="py function objdesc"><dt class="sig" id="{path}">'
    "make(x: ~typing.Annotated[bool, &lt;otto.utils._Exclude object at 0xe39387&gt;])"
    "</dt><dd>like &lt;otto.host._Registry object&gt;</dd></dl>"
)


def test_an_internals_data_signature_showing_a_default_repr_value_fails(tmp_path):
    """A value that keeps ``object``'s repr is never shown (``docs_api_reference.shows_value``).

    Sphinx highlights the value token by token, so the check reads the
    signature's text, not its markup. A description that mentions such a repr,
    a value with a real repr, and, on an Internals page, a function signature
    are fine.
    """
    unix = (
        UNIX
        + '<dl class="py data objdesc">\n<dt class="sig" id="otto.host.unix_host.REGISTRY">R'
        + f"{_REGISTRY_VALUE}</dt><dd>like &lt;otto.host._Registry object&gt;</dd></dl>"
        + '<dl class="py attribute objdesc"><dt id="otto.host.unix_host.KINDS">'
        + "K = [&#39;unix&#39;]</dt></dl>"
        + '<dl class="py attribute objdesc"><dt id="otto.host.unix_host.Shell.store">'
        + "store = &lt;otto.host._Store object at 0x7f00&gt;</dt></dl>"
        + _FUNCTION.format(path="otto.host.unix_host.make")
    )
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": unix,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        (
            "api/internals/host/unixhost.html: otto.host.unix_host.REGISTRY "
            "shows a default repr (<otto.host._Registry object>)"
        ),
        (
            "api/internals/host/unixhost.html: otto.host.unix_host.Shell.store "
            "shows a default repr (<otto.host._Store object>)"
        ),
    ]


def test_a_public_function_signature_showing_a_default_repr_fails(tmp_path):
    """On a Public API page every signature is read, a function's annotations included.

    The same function on an Internals page passes (the test above).
    """
    host = HOST + _FUNCTION.format(path="otto.host.make")
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": host,
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        "api/host/index.html: otto.host.make shows a default repr (<otto.utils._Exclude object>)"
    ]


def _signature(path, text, kind="function"):
    return f'<dl class="py {kind} objdesc"><dt class="sig" id="{path}">{text}</dt></dl>'


def _public_page_findings(tmp_path, body):
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST + body,
            "api/internals/host/unixhost.html": UNIX,
            "api/stability.html": STABILITY,
        },
    )
    return findings(site, STABILITIES, GROUPS, pages)


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            "class otto.host.Lab(name: str, resources: set[str] = &lt;factory&gt;)",
            "a default that is not Python (<factory>)",
        ),
        (
            "to_host(cls: type[UnixHost] = &lt;class &#x27;otto.host.unix_host.UnixHost&#x27;&gt;)",
            "a default that is not Python (<class 'otto.host.unix_host.UnixHost'>)",
        ),
        (
            "SessionManager(log: Callable = &lt;function SessionManager.&lt;lambda&gt;&gt;)",
            "a default that is not Python (<function SessionManager.<lambda>>)",
        ),
        (
            "exec(timeout: ~typing.Annotated[float, otto.utils.Opt(help=Seconds, min=0.0)])",
            "CLI metadata (otto.utils.Opt(…))",
        ),
        (
            "exec(cmd: ~types.Annotated[str, ~otto.utils.Arg(name=COMMAND, help=One)])",
            "CLI metadata (~otto.utils.Arg(…))",
        ),
        (
            "class otto.host.RepoBuild(repo: str, images: dict[str, ~otto.docker.ImageBuild])",
            "a raw ~otto. prefix: Sphinx could not parse it",
        ),
    ],
)
def test_a_public_signature_showing_text_that_is_not_python_fails(tmp_path, text, shown):
    """Each shape the old ``<X object>``-only check let through, read from the escaped HTML.

    Any one of them makes Sphinx fall back to the raw, comma-split signature.
    """
    assert _public_page_findings(tmp_path, _signature("otto.host.make", text)) == [
        f"api/host/index.html: otto.host.make shows {shown}"
    ]


def test_a_parsed_public_signature_passes(tmp_path):
    """A signature Sphinx parsed shows no ``~`` and only Python defaults.

    The CLI metadata classes' own signatures start ``otto.utils.Opt(``: that is
    the object documented, not metadata. An enum's repr is a value, not a leak.
    """
    parsed = (
        '<em class="property">class </em><span class="sig-prename">otto.utils.</span>'
        '<span class="sig-name">Opt</span>(help: str | None = None, min: float | None = None)'
    )
    body = (
        _signature("otto.host.make", "make(x: bool = True, y: set[str] = set(), z: list = ...)")
        + _signature("otto.utils.Opt", parsed, kind="class")
        + _signature("otto.host.Lab.ROLE", "ROLE = &lt;UserSupport.chown: &#x27;chown&#x27;&gt;")
    )
    assert _public_page_findings(tmp_path, body) == []


def test_an_internals_data_signature_showing_a_factory_fails_but_a_typer_default_passes(
    tmp_path,
):
    """On an Internals page a data or attribute signature is held to the same shapes."""
    unix = (
        UNIX
        + _signature(
            "otto.host.unix_host.Shell.names",
            "names : set[str] = &lt;factory&gt;",
            kind="attribute",
        )
        + _signature(
            "otto.host.unix_host.main",
            "main(path: ~pathlib.Path = &lt;typer.models.ArgumentInfo object&gt;)",
        )
    )
    site, pages = _site(
        tmp_path,
        pages={
            "api/otto.html": ROOT,
            "api/host/index.html": HOST,
            "api/internals/host/unixhost.html": unix,
            "api/stability.html": STABILITY,
        },
    )
    assert findings(site, STABILITIES, GROUPS, pages) == [
        (
            "api/internals/host/unixhost.html: otto.host.unix_host.Shell.names "
            "shows a default that is not Python (<factory>)"
        )
    ]
