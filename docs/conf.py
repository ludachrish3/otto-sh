import importlib.metadata
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

project = "otto"
author = "otto contributors"
release = importlib.metadata.version("otto-sh")
version = release
# Sphinx's default html_title is "{project} {release} documentation", which bakes
# the build-time package version into the page/tab title. Between tagged releases
# that resolves to a dev string (e.g. "otto 0.5.1.dev3+g1234567"), which is stale
# and noisy. The Read the Docs version selector already reports exactly which
# version (latest/stable/tag) the reader is on, so keep the title version-free.
html_title = f"{project} documentation"
# Treat all unresolved cross-references as errors.
nitpicky = True

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "myst_parser",
    "sphinx_immaterial",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

html_theme = "sphinx_immaterial"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_theme_options = {
    "palette": [
        {
            "media": "(prefers-color-scheme)",
            "scheme": "default",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-auto",
                "name": "Switch to dark mode",
            },
        },
        {
            "media": "(prefers-color-scheme: dark)",
            "scheme": "slate",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-4",
                "name": "Switch to light mode",
            },
        },
        {
            "media": "(prefers-color-scheme: light)",
            "scheme": "default",
            "primary": "custom",
            "accent": "pink",
            "toggle": {
                "icon": "material/brightness-7",
                "name": "Switch to system preference",
            },
        },
    ]
}

exclude_patterns = ["RESTRUCTURE_PLAN.md", "superpowers/**", "_inventories"]

# -- autodoc ------------------------------------------------------------------

autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    # model_config is pydantic boilerplate on every model class. Documenting it
    # adds ~25 unresolvable ConfigDict/SettingsConfigDict refs with no value.
    "exclude-members": "model_config",
}
autodoc_typehints = "signature"

# Sphinx 7.3+ auto-generates py:param cross-references; for TypeVar-typed
# parameters (T/P/R in async_typer_command, do_for_all_hosts, is_literal) these
# emit spurious "py:param reference target not found" warnings that -W promotes
# to errors. ref.param is the auto-generated param-name xref only — type/class
# resolution (ref.class/func/meth/attr) stays fully enforced under nitpicky.
suppress_warnings = ["ref.param"]

# -- intersphinx --------------------------------------------------------------
# Resolve stdlib + third-party type targets so nitpicky can follow them.
#
# Inventories are vendored locally in docs/_inventories/ so that `make docs`
# never live-fetches (fixes ~1-in-4 failures on readthedocs network jitter,
# issue #56).  Target URLs are kept verbatim so generated cross-reference links
# still point at the live published docs.  Refresh the local copies with:
#   make docs-inventories
_INV = pathlib.Path(__file__).parent / "_inventories"
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", str(_INV / "python.inv")),
    "typer": ("https://typer.tiangolo.com", str(_INV / "typer.inv")),
    "rich": ("https://rich.readthedocs.io/en/stable", str(_INV / "rich.inv")),
    "pydantic": ("https://docs.pydantic.dev/latest", str(_INV / "pydantic.inv")),
    "asyncssh": ("https://asyncssh.readthedocs.io/en/stable", str(_INV / "asyncssh.inv")),
    "pytest": ("https://docs.pytest.org/en/stable", str(_INV / "pytest.inv")),
    "telnetlib3": ("https://telnetlib3.readthedocs.io/en/latest", str(_INV / "telnetlib3.inv")),
}

# -- short-name type resolver -------------------------------------------------
# WHY THIS EXISTS: `from __future__ import annotations` (postponed evaluation)
# causes Python to store type annotations as raw strings, so autodoc renders
# them exactly as written in source — e.g. `Path` instead of `pathlib.Path`.
# Sphinx nitpicky then tries to resolve bare `Path` as a py:class target and
# fails, even though `:py:class:`pathlib.Path`` resolves fine via intersphinx.
# `autodoc_type_aliases` and `autodoc_typehints_format='fully-qualified'` do
# NOT fix this under postponed evaluation (verified).
#
# HOW IT WORKS: This handler RESOLVES short names to their fully-qualified
# intersphinx targets and re-dispatches to intersphinx — producing real
# clickable cross-reference links. It is NOT a nitpick_ignore (which silences
# warnings); it is a genuine resolution step.
#
# MAP POLICY: Only curated, currently-valid EXTERNAL types belong here.
# Internal otto types are NOT mapped here — they are qualified in their
# docstrings (Task 5). Only external (intersphinx-served) names belong in
# this map. If a mapped name is renamed or removed upstream, intersphinx will
# fail to resolve it and nitpicky will correctly flag genuine doc rot.
_SHORT_TYPE_ALIASES = {
    # stdlib
    "Path": "pathlib.Path",
    "datetime": "datetime.datetime",
    "timedelta": "datetime.timedelta",
    "asyncio.queues.Queue": "asyncio.Queue",
    "_contextvars.Token": "contextvars.Token",
    "types.Annotated": "typing.Annotated",
    # rich
    "Panel": "rich.panel.Panel",
    "Progress": "rich.progress.Progress",
    # asyncssh
    "SSHClientConnection": "asyncssh.SSHClientConnection",
    "SFTPClient": "asyncssh.SFTPClient",
    # already fully-qualified; re-dispatching through intersphinx lets it match
    # the asyncssh inventory across object types (the original xref's reftype
    # missed it).
    "asyncssh.connect": "asyncssh.connect",
    # pytest (_pytest.* private names map to their public pytest.* aliases)
    "_pytest.config.Config": "pytest.Config",
    "_pytest.nodes.Item": "pytest.Item",
    "_pytest.main.Session": "pytest.Session",
    "_pytest.stash.StashKey": "pytest.StashKey",
    "_pytest.reports.TestReport": "pytest.TestReport",
    "_pytest.runner.CallInfo": "pytest.CallInfo",
    # pydantic-settings (served by the pydantic inventory — pydantic.dev hosts a
    # combined inventory that includes pydantic-settings)
    "NoDecode": "pydantic_settings.NoDecode",
    "SettingsConfigDict": "pydantic_settings.SettingsConfigDict",
    "PydanticBaseSettingsSource": "pydantic_settings.PydanticBaseSettingsSource",
    "CliSettingsSource": "pydantic_settings.CliSettingsSource",
    # telnetlib3
    "telnetlib3.open_connection": "telnetlib3.client.open_connection",
}


def _resolve_short_types(app, env, node, contnode):
    """Resolve short/private type names to their canonical intersphinx targets."""
    full = _SHORT_TYPE_ALIASES.get(node.get("reftarget"))
    if not full:
        return None
    node["reftarget"] = full
    from sphinx.ext import intersphinx

    return intersphinx.missing_reference(app, env, node, contnode)


# Internal otto type aliases (module-level ``X = ...``) are documented as
# py:data but referenced as py:class in annotations; re-dispatch through the
# python domain's resolve_any_xref so the data target is matched.
# Also covers module-alias refs (e.g. ``rt.LocalPortForward`` where ``rt``
# is ``from ..host import options as rt`` in models/options.py).
_INTERNAL_ALIASES = {
    # otto.host.remote_host
    "OsType": "otto.host.remote_host.OsType",
    # otto.host.transfer.base (requires transfer_base.rst to be documented)
    "NcPortStrategy": "otto.host.transfer.base.NcPortStrategy",
    "NcListenerCheck": "otto.host.transfer.base.NcListenerCheck",
    # TransferProgressHandler/Factory are re-exported from the package __init__
    # and registered there as 'attribute' objects; resolve to the package path.
    "TransferProgressHandler": "otto.host.transfer.TransferProgressHandler",
    "TransferProgressFactory": "otto.host.transfer.TransferProgressFactory",
    # otto.coverage.reporter
    "TierSpec": "otto.coverage.reporter.TierSpec",
    # otto.host.options (referenced via ``rt`` alias in models/options.py)
    "rt.LocalPortForward": "otto.host.options.LocalPortForward",
    "rt.RemotePortForward": "otto.host.options.RemotePortForward",
    "rt.SocksForward": "otto.host.options.SocksForward",
}


def _resolve_internal_aliases(app, env, node, contnode):
    """Resolve internal otto type aliases via the local python domain."""
    full = _INTERNAL_ALIASES.get(node.get("reftarget"))
    if not full:
        return None
    pydom = env.get_domain("py")
    results = pydom.resolve_any_xref(
        env,
        node.get("refdoc", ""),
        app.builder,
        full,
        node,
        contnode,
    )
    return results[0][1] if results else None


# External types with NO intersphinx inventory get a hand-built reference node
# to their published docs. This is a genuine clickable link (NOT a silence), so
# the zero-``nitpick_ignore`` policy holds. aioftp publishes docs at
# aioftp.aio-libs.org but ships no objects.inv, so intersphinx cannot serve it;
# ``aioftp.Client`` is the public return type of ``ConnectionManager.ftp()``.
_EXTERNAL_DOC_LINKS = {
    "aioftp.Client": "https://aioftp.aio-libs.org/client_api.html#aioftp.Client",
}


def _resolve_external_doc_links(app, env, node, contnode):  # noqa: ARG001 — required by Sphinx missing-reference event handler signature
    """Link inventory-less external types to their published docs pages."""
    from docutils import nodes

    uri = _EXTERNAL_DOC_LINKS.get(node.get("reftarget"))
    if not uri:
        return None
    return nodes.reference("", "", contnode, refuri=uri, internal=False)


def _strip_inherited_pydantic_signature(
    app,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    what,
    name,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    obj,
    options,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    signature,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    return_annotation,
):
    """Blank the class signature when it is pydantic-settings' inherited
    auto-``__init__``.

    ``OttoEnvSettings(BaseSettings)`` adds no ``__init__`` of its own, so autodoc
    renders ``BaseSettings.__init__``'s ~37 private ``_env_*`` params into the
    class signature. Those carry private ``pydantic_settings.main`` types
    (``EnvPrefixTarget``/``DotenvType``/``PathType``) that have no public
    intersphinx target, and the params themselves document nothing the public
    settings fields (``sut_dirs``, ``lab``, ...) don't already. Drop the
    signature for any class whose ``__init__`` is inherited straight from
    pydantic-settings; otto-defined ``__init__`` methods are untouched.
    """
    if what != "class":
        return None
    init = getattr(obj, "__init__", None)
    if init is not None and getattr(init, "__module__", "").startswith("pydantic_settings"):
        return ("", return_annotation)
    return None


def setup(app):
    app.connect("missing-reference", _resolve_short_types)
    app.connect("missing-reference", _resolve_internal_aliases)
    app.connect("missing-reference", _resolve_external_doc_links)
    app.connect("autodoc-process-signature", _strip_inherited_pydantic_signature)


# -- napoleon -----------------------------------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_param = False
napoleon_use_rtype = False
napoleon_attr_annotations = True
napoleon_use_ivar = True

# -- doctest ------------------------------------------------------------------

doctest_global_setup = """
import asyncio
from otto.utils import Status, split_on_commas
from otto.result import CommandResult, Result, Results
from otto.host.local_host import LocalHost
from otto.monitor.parsers import human_readable

# Use a single persistent loop across all run() calls in a doctest block.
# asyncio.run() creates and closes a fresh loop each call, which breaks any
# LocalHost whose underlying ShellSession was lazily bound to the first loop
# (the second call raises "Future attached to a different loop").
_loop = asyncio.new_event_loop()

def run(coro):
    return _loop.run_until_complete(coro)
"""

# -- myst-parser --------------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "fieldlist",
    "deflist",
]

myst_heading_anchors = 3
