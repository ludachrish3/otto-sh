import importlib.metadata
import pathlib
import sys
import typing

from sphinx.util import inspect as sphinx_inspect

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
# Otto version numbers in prose and code fences are never hand-written — pages
# use the %OTTO_VERSION% token and this source-read hook replaces it with the
# release version (bump-my-version keeps that identical to the latest tag).
# A source-read hook rather than MyST's substitution extension because the
# token must substitute inside fenced code blocks (`pip install otto-sh==X`),
# where MyST substitutions do not reach. scripts/lint_docs_versions.py bans
# hand-written version literals so the token stays the only spelling.
OTTO_VERSION_TOKEN = "%OTTO_VERSION%"  # noqa: S105 — a text placeholder, not a credential


def _substitute_version_token(app, docname, source):  # noqa: ARG001 — Sphinx event signature
    source[0] = source[0].replace(OTTO_VERSION_TOKEN, release)


# Treat all unresolved cross-references as errors.
nitpicky = True

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.graphviz",
    "sphinx.ext.inheritance_diagram",
    "myst_parser",
    "sphinx_immaterial",
    "sphinx_immaterial.graphviz",
]

# -- graphviz / diagrams --------------------------------------------------------
# Architecture-docs diagrams. Class hierarchies use inheritance_diagram, which
# imports the LIVE classes at build time — the diagram tracks the code, and a
# renamed/removed class fails this -W build instead of rotting silently.
# Pipeline/lifecycle flows are hand-authored DOT in the pages themselves.
# sphinx_immaterial.graphviz re-renders all of it as inline SVG using the
# theme's fonts and CSS variables, so diagrams follow light/dark mode with no
# hard-coded colors anywhere. Requires the `dot` binary: dev VM (Vagrantfile),
# RTD (.readthedocs.yaml apt_packages), CI (explicit apt-get in ci.yml).
#
# `dot` measures text with its own (often missing) fonts, so its size metrics
# rarely match the theme font the SVG is styled with; the extension warns and
# -W would turn that cosmetic mismatch into a build failure. Documented
# escape hatch from sphinx-immaterial for exactly this situation:
graphviz_ignore_incorrect_font_metrics = True

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

html_theme = "sphinx_immaterial"
html_static_path = ["_static"]
# termynal renders the build-time-captured CLI blocks (help menus, tab
# completion) as animated terminal windows. termynal.js/.css are vendored
# verbatim (MIT, Ines Montani); otto's tweaks live in termynal-otto.css and
# the lazy-start loader in termynal-init.js.
html_css_files = ["custom.css", "termynal.css", "termynal-otto.css"]
html_js_files = ["termynal.js", "termynal-init.js"]

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

exclude_patterns = ["RESTRUCTURE_PLAN.md", "superpowers/**", "_inventories", "examples/**"]

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
# parameters (T in do_for_all_hosts, TypeVar in is_literal) these
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
    # The C-accelerated Task's __module__ is the private _asyncio; same
    # misqualification class as _contextvars.Token below it.
    "_asyncio.Task": "asyncio.Task",
    "_contextvars.Token": "contextvars.Token",
    "types.Annotated": "typing.Annotated",
    # Autodoc qualifies a bare ``Annotated`` with the module of its FIRST
    # argument, so ``Annotated[Path, ...]`` (models/settings.py's RepoPath)
    # renders as ``pathlib.Annotated`` — a target that does not exist. Same
    # class of misqualification as ``types.Annotated`` above; both resolve to
    # the one real symbol rather than being silenced.
    "pathlib.Annotated": "typing.Annotated",
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
    # A discriminated union declared the only way pydantic allows for a list
    # of variants -- ``list[Annotated[A | B, Field(discriminator=...)]]``,
    # since the discriminator must sit on the union and not on the list
    # (CoverageExclusionsSpec.rules). Autodoc renders the ``Annotated``
    # metadata verbatim, so the FieldInfo repr's own type names surface as
    # bare xref targets. Both are real, published symbols, so they resolve
    # rather than being silenced -- there is no way to restate the annotation
    # that both keeps the discriminator and hides the repr.
    "FieldInfo": "pydantic.fields.FieldInfo",
    "NoneType": "types.NoneType",
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
    # otto.host.login_proxy.Cred: dataclass field annotations on an inherited
    # attribute (EmbeddedHost.creds -> ZephyrHost.creds) render the bare name
    # instead of the fully-qualified one autodoc uses everywhere else.
    "Cred": "otto.host.login_proxy.Cred",
    # AppShellT is a TYPE_CHECKING-only TypeVar (bound=AppShell) in host.py —
    # kept out of the runtime module namespace to protect the import-budget
    # guard, so autodoc can never document it directly. Point the signature
    # xref at its bound type instead.
    "AppShellT": "otto.host.app_shell.AppShell",
    # models/settings.py's RepoPath is ``Annotated[Path,
    # AfterValidator(anchor_to_repo)]``; autodoc renders every annotation
    # component as a py:class xref, but anchor_to_repo is documented as a
    # py:function. Already fully qualified — the identity mapping exists to
    # re-dispatch through resolve_any_xref, which matches across object types
    # (same trick as ``asyncssh.connect`` in _SHORT_TYPE_ALIASES).
    "otto.models.settings.anchor_to_repo": "otto.models.settings.anchor_to_repo",
    # models/host.py's IntOrStr is the same shape one alias down —
    # ``Annotated[int | str, BeforeValidator(coerce_digit_string)]``, carried by
    # HostSpec.site/.rack and InventoryRecord.site/.rack — so the validator
    # function needs the identical re-dispatch. (The other half of that repr,
    # BeforeValidator's ``json_schema_input_type``, is fixed at the source: see
    # the comment on IntOrStr for why a sentinel default cannot be mapped here.)
    "otto.models.host.coerce_digit_string": "otto.models.host.coerce_digit_string",
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
    # typer vendors its own click fork (Typer >= 0.26) and ships no intersphinx
    # inventory; TyperGroup is a real public name (used to build custom Typer
    # groups — see cli/invoke.py's RegistryBackedGroup / cli/expose.py's
    # HostGroup) documented on typer's own API reference page.
    "TyperGroup": "https://typer.tiangolo.com/reference/typer/#typer.core.TyperGroup",
    # typing_extensions.Self backport (used pre-3.11): intersphinx's python
    # inventory does carry typing.Self, but as a py:data object, while the
    # annotation is referenced via the py:class role (a TypeVar-like special
    # form, not a class) — role/objtype mismatch means intersphinx's own
    # role-scoped lookup (missing_reference) never matches it even after
    # retargeting. A direct link sidesteps that mismatch instead of fighting it.
    "Self": "https://docs.python.org/3/library/typing.html#typing.Self",
    # annotated-types ships no Sphinx docs / objects.inv (README-only project).
    # LinkSpec.endpoints (models/link.py) uses ``Field(min_length=2,
    # max_length=2)``, which pydantic renders in the signature as
    # ``Annotated[..., MinLen(...), MaxLen(...)]``; autodoc's annotation
    # stringifier turns each metadata class into a py:class xref attempt once
    # there are 2+ metadata args (a single-constraint field like
    # ``UnixHostSpec.creds`` renders as opaque text and never attempts one).
    "annotated_types.MinLen": "https://github.com/annotated-types/annotated-types#minlen-maxlen-len",
    "annotated_types.MaxLen": "https://github.com/annotated-types/annotated-types#minlen-maxlen-len",
    # Same stringifier behavior for range constraints: CoverageReportSpec
    # (models/settings.py) uses ``Field(ge=0, le=100)``, rendering as
    # ``Annotated[float, Ge(0), Le(100)]`` — two metadata args, so each
    # constraint class becomes a py:class xref attempt.
    "annotated_types.Ge": "https://github.com/annotated-types/annotated-types#gt-ge-lt-le",
    "annotated_types.Le": "https://github.com/annotated-types/annotated-types#gt-ge-lt-le",
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


def _drop_privately_typed_params(
    app,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    what,
    name,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    obj,
    options,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    signature,
    return_annotation,
):
    """Omit parameters typed with an otto-private class from the signature.

    ``run_command(..., _controller: _CommandRun | None = None)`` is the case
    this exists for: a tier-1 test seam typed with the module's internal state
    machine. ``autodoc_typehints="signature"`` renders every annotation as an
    xref, and a private class is one autodoc will never document and the
    zero-``nitpick_ignore`` policy will not let us silence.

    Scoped by the ANNOTATION, not the parameter name — ``expect``'s
    ``_stack_offset: int`` is private-by-name but publicly typed and
    deliberately documented in its ``Args:`` block. Scoped to ``otto.*`` — a
    private third-party type is a different problem and must still fail loudly.
    Autodoc's own signature string has one parameter removed rather than being
    rebuilt, so annotation formatting cannot drift from the rest of the docs.
    """
    if what not in {"function", "method", "class"} or not signature:
        return None

    def private(ann):
        return (
            getattr(ann, "__module__", "").startswith("otto.")
            and getattr(ann, "__name__", "").startswith("_")
        ) or any(private(arg) for arg in typing.get_args(ann))

    try:
        params = sphinx_inspect.signature(obj).parameters
    except (TypeError, ValueError):
        return None
    drop = {n for n, p in params.items() if p.annotation is not p.empty and private(p.annotation)}
    if not drop:
        return None

    # Split on TOP-LEVEL commas only: ``Coroutine[Any, Any, R]`` has its own.
    inner = signature[1:-1] if signature.startswith("(") and signature.endswith(")") else signature
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        depth += (ch in "[({") - (ch in "])}")
        if ch == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    parts.append(inner[start:])

    kept = [p.strip() for p in parts if p.split(":")[0].split("=")[0].strip() not in drop]
    if kept and kept[-1] == "*":  # a bare ``*`` with no keyword-only param behind it
        kept.pop()
    return ("(" + ", ".join(kept) + ")", return_annotation)


# -- build-time GUI media + terminal blocks ------------------------------------
# Screenshots, video clips, and termynal terminal blocks are PRODUCTS OF THE
# BUILD, never committed: scripts/capture_docs_media.py serves the real
# dashboard (via the browser-e2e harness fixtures) seeded with deterministic
# dummy data and captures it with headless Chromium; capture_docs_termynal.py
# scaffolds a demo repo with `otto init` and captures real --help output and
# tab-completion candidates. Both write into docs/_static/generated/
# (gitignored). Hooked here — rather than in the Makefile / CI / RTD configs —
# so every environment gets the same artifacts with one wiring point.
# Chromium is a hard requirement of the dev environment (`make browsers`);
# OTTO_DOCS_MEDIA=placeholder is the documented emergency escape hatch.
#
# The termynal capture runs for EVERY builder: its snippets are pulled in via
# `{raw} html :file:`, which docutils reads at parse time, so the doctest
# builder needs them on disk too. The browser capture is html-only — no other
# builder touches the image/video files.


def _run_capture_script(name: str) -> None:
    import subprocess

    from sphinx.util import logging as sphinx_logging

    logger = sphinx_logging.getLogger(__name__)
    script = pathlib.Path(__file__).parent.parent / "scripts" / name
    # Capture output: the dashboard harness prints benign asyncio teardown
    # noise on stderr; keep successful builds quiet and failures fully loud.
    proc = subprocess.run(  # noqa: S603 — fixed interpreter + repo-local script, no shell
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    if proc.stdout.strip():
        logger.info(proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {proc.returncode}:\n{proc.stderr}")


def _generate_docs_media(app):
    _run_capture_script("capture_docs_termynal.py")
    if app.builder.name == "html":
        _run_capture_script("capture_docs_media.py")


def _generate_support_matrix(app):  # noqa: ARG001 — Sphinx event signature
    """Render docs/architecture/support-matrix.md from the committed artifact.

    EVERY BUILDER, not html-only: the page is a real source file that the toctree
    names, so the doctest builder has to find it on disk too — the same reason the
    termynal capture above runs unconditionally.

    A `-m` invocation rather than a path, because the renderer imports `tests.*` to
    ask the tree which surfaces and profiles it still declares; run as a path,
    `sys.path[0]` would be `scripts/`. A non-zero exit RAISES, so a matrix whose axes
    the tree no longer backs is a build FAILURE and not a warning (spec §5).
    """
    import subprocess

    from sphinx.util import logging as sphinx_logging

    logger = sphinx_logging.getLogger(__name__)
    root = pathlib.Path(__file__).parent.parent
    # No S603 suppression here, unlike `_run_capture_script` above: ruff reports one
    # as UNUSED on this call, and a suppression that suppresses nothing is a claim
    # about a risk that was never raised. (Spelled without the directive word on
    # purpose -- ruff parses that token even inside a comment and warns.)
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.render_support_matrix"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(root),
    )
    if proc.stdout.strip():
        logger.info(proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(
            f"scripts/render_support_matrix.py failed with exit code "
            f"{proc.returncode}:\n{proc.stderr}"
        )


def setup(app):
    app.connect("source-read", _substitute_version_token)
    app.connect("builder-inited", _generate_docs_media)
    app.connect("builder-inited", _generate_support_matrix)
    app.connect("missing-reference", _resolve_short_types)
    app.connect("missing-reference", _resolve_internal_aliases)
    app.connect("missing-reference", _resolve_external_doc_links)
    app.connect("autodoc-process-signature", _strip_inherited_pydantic_signature)
    app.connect("autodoc-process-signature", _drop_privately_typed_params)


# -- napoleon -----------------------------------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_param = False
napoleon_use_rtype = False
napoleon_attr_annotations = True
napoleon_use_ivar = True

# -- doctest ------------------------------------------------------------------

_GS_EXAMPLE = pathlib.Path(__file__).resolve().parent / "examples" / "getting-started"

# An f-string: any literal brace in the setup code below must be doubled.
doctest_global_setup = f"""
import asyncio
import sys
from pathlib import Path
GS_EXAMPLE = Path({str(_GS_EXAMPLE)!r})
if str(GS_EXAMPLE / "libs") not in sys.path:
    sys.path.insert(0, str(GS_EXAMPLE / "libs"))
from otto.utils import Status, complete_separated_list, split_on
from otto.result import CommandResult, Result, Results
from otto.config.lab import split_lab_names
from otto.host.local_host import LocalHost
from otto.monitor.parsers import human_readable
from otto.registry import Registry

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
