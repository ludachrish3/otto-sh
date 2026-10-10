import functools
import importlib.metadata
import pathlib
import sys
import types
import typing

from sphinx.errors import PycodeError
from sphinx.ext.autodoc import AttributeDocumenter, DataDocumenter, ModuleDocumenter
from sphinx.pycode import ModuleAnalyzer
from sphinx.util import inspect as sphinx_inspect
from typing_extensions import override

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts import docs_api_reference, docs_build_stamp

project = "otto"
author = "otto contributors"
release = importlib.metadata.version("otto-sh")
version = release
# The title carries the bare release ("otto 0.9.0 documentation") — no leading
# "v", and never the full git-describe string, which is too long for a browser
# tab. otto's version is static in pyproject.toml, so it reads the same at the
# tag and past it; _BUILD_STAMP is what distinguishes them, adding a "+dev"
# marker off-tag. The detail (commit, distance, link to the stable docs) goes in
# the announcement bar rendered by _templates/base.html, which is the convention
# Python, Django and NumPy all follow: short title, banner carries the warning.
_BUILD_STAMP = docs_build_stamp.build_stamp()
html_title = docs_build_stamp.html_title(project, release, _BUILD_STAMP)
templates_path = ["_templates"]
html_context = {
    "otto_dev_banner_text": docs_build_stamp.dev_banner_text(_BUILD_STAMP),
    "otto_stable_docs_url": docs_build_stamp.STABLE_DOCS_URL,
    "otto_api_notice": docs_api_reference.provisional_notice(release),
}
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
    # Lets a reader dismiss the work-in-progress banner (_templates/base.html);
    # the theme only renders the close button and its JS when this is set.
    # content.code.copy adds a one-click copy button to every code block.
    "features": ["announce.dismiss", "content.code.copy"],
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
    ],
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
    "requests": ("https://requests.readthedocs.io/en/latest", str(_INV / "requests.inv")),
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
#
# WHICH path to qualify an internal name with: its public path when it has one
# (``otto.reservations.ReservationBackendBase``), else its defining module's.
# Each public object is documented once, at the home scripts/docs_api_reference.py
# picks; a reference to its defining-module path resolves there too
# (_resolve_public_home), so either spelling links.
_SHORT_TYPE_ALIASES = {
    # stdlib
    "Path": "pathlib.Path",
    "datetime": "datetime.datetime",
    "timedelta": "datetime.timedelta",
    # A TYPE_CHECKING-only import in config/repo.py; the class's __module__ is
    # the private _frozen_importlib, so the stdlib inventory only has it here.
    "ModuleSpec": "importlib.machinery.ModuleSpec",
    "asyncio.queues.Queue": "asyncio.Queue",
    # A loop's class is documented at its re-export; autodoc names the defining module.
    "asyncio.events.AbstractEventLoop": "asyncio.AbstractEventLoop",
    # The C-accelerated Task's __module__ is the private _asyncio; same
    # misqualification class as _contextvars.Token below it.
    "_asyncio.Task": "asyncio.Task",
    "_contextvars.Token": "contextvars.Token",
    "_contextvars.ContextVar": "contextvars.ContextVar",
    "types.Annotated": "typing.Annotated",
    # ``contextlib`` re-exports the ABC that ``@asynccontextmanager`` produces;
    # the Host protocol names it as the return type of ``as_user``/``app_shell``,
    # and the stdlib inventory carries it only fully qualified.
    "AbstractAsyncContextManager": "contextlib.AbstractAsyncContextManager",
    # Autodoc qualifies a bare ``Annotated`` with the module of its FIRST
    # argument, so ``Annotated[Path, ...]`` (models/settings.py's RepoPath)
    # renders as ``pathlib.Annotated`` — a target that does not exist. Same
    # class of misqualification as ``types.Annotated`` above; both resolve to
    # the one real symbol rather than being silenced.
    "pathlib.Annotated": "typing.Annotated",
    # rich
    "Panel": "rich.panel.Panel",
    "Tree": "rich.tree.Tree",
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
    # pydantic re-exports this from pydantic-core; the inventory carries it
    # only under its defining module (bare ``ValidationError`` and
    # ``pydantic.ValidationError`` both miss).
    "ValidationError": "pydantic_core.ValidationError",
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
    # otto.init.areas: scaffolder.py's docstrings say :data:`AREA_NAMES` bare,
    # relative to their own module, where only areas.py defines it.
    "AREA_NAMES": "otto.init.areas.AREA_NAMES",
    # otto.labs.registry: register_lab_repository's signature names the
    # configuration TypeVar it imports from otto.registry, bare.
    "C": "otto.registry.C",
    # otto.init's package docstring names its two entry points bare, relative
    # to the package; the pages document them under their defining modules.
    "check_repo": "otto.init.doctor.check_repo",
    "scaffold": "otto.init.scaffolder.scaffold",
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
    full = docs_api_reference.public_target(_api_reference(), full) or full
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
    # typer >= 0.26 vendors click as ``typer._click`` and ships no intersphinx
    # inventory for it; ``typer.BadParameter`` is the vendored copy of
    # click's class, and click's own API page is where it is documented. It
    # is the return type of ``otto.cli.invoke.usage_error_from``.
    "typer._click.exceptions.BadParameter": "https://click.palletsprojects.com/en/stable/api/#click.BadParameter",
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
    # ``Annotated[..., MinLen(...), MaxLen(...)]``; in any signature Sphinx can
    # parse as Python, each metadata class becomes a py:class xref attempt.
    "annotated_types.MinLen": "https://github.com/annotated-types/annotated-types#minlen-maxlen-len",
    "annotated_types.MaxLen": "https://github.com/annotated-types/annotated-types#minlen-maxlen-len",
    # Same stringifier behavior for range constraints: CoverageReportSpec
    # (models/settings.py) uses ``Field(ge=0, le=100)``, rendering as
    # ``Annotated[float, Ge(0), Le(100)]``, and ConsoleOptionsSpec
    # (models/options.py) ``login_timeout``'s ``Field(gt=0)`` as ``Gt(gt=0)``.
    # _typeshed.DataclassInstance is a stub-only protocol (it exists in
    # typeshed, never at runtime), so no inventory can serve it; it is the
    # annotation on ``otto.params.options_params``'s options-class parameter.
    "DataclassInstance": "https://github.com/python/typeshed/blob/main/stdlib/_typeshed/__init__.pyi",
    "annotated_types.Ge": "https://github.com/annotated-types/annotated-types#gt-ge-lt-le",
    "annotated_types.Le": "https://github.com/annotated-types/annotated-types#gt-ge-lt-le",
    "annotated_types.Gt": "https://github.com/annotated-types/annotated-types#gt-ge-lt-le",
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


def _drop_cli_metadata(
    app,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    what,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    name,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    obj,
    options,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    signature,
    return_annotation,
):
    """Render ``Annotated[T, <CLI metadata>]`` as ``T`` in a signature and its return annotation.

    ``otto.utils.Arg``, ``Opt`` and ``Exclude`` tell the CLI how to expose a
    verb parameter; a Python caller passes a plain ``T``. Autodoc would print
    each one's repr: ``Exclude``'s names a private class and a memory address
    that changes every build, and an ``Arg``/``Opt`` prints its help text
    unquoted, which is not Python, so Sphinx cannot parse the signature and
    shows it as raw text (``docs_api_reference.drop_annotated_markers``).
    A ``None``-defaulted ``Annotated[T | None, Opt(...)]`` gains an outer
    ``Optional`` on Python 3.10 (``get_type_hints``), as it does everywhere
    when the source spells ``| None`` out, so dropping the metadata leaves
    ``T | None | None``: the repeated ``None`` is shown once
    (``docs_api_reference.single_none``).
    """
    from otto.utils import Arg, Exclude, Opt

    markers = [repr(Exclude)]
    try:
        sig = sphinx_inspect.signature(obj)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        annotations = [p.annotation for p in sig.parameters.values()] + [sig.return_annotation]
        for meta in docs_api_reference.annotated_metadata(annotations):
            if meta is Exclude or isinstance(meta, (Arg, Opt)):
                markers.extend(docs_api_reference.annotated_metadata_text(meta))
    rewritten = (
        docs_api_reference.single_none_in_signature(
            docs_api_reference.drop_annotated_markers(signature, markers)
        )
        if signature
        else signature,
        docs_api_reference.single_none(
            docs_api_reference.drop_annotated_markers(return_annotation, markers)
        )
        if return_annotation
        else return_annotation,
    )
    return None if rewritten == (signature, return_annotation) else rewritten


def _python_valid_defaults(
    app,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    what,
    name,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    obj,
    options,  # noqa: ARG001 — required by Sphinx autodoc-process-signature event handler signature
    signature,
    return_annotation,
):
    """Show each default whose repr is not Python as Python.

    A ``default_factory`` field shows ``<factory>``, a class default
    ``<class 'otto.host.unix_host.UnixHost'>`` and a function default
    ``<function SessionManager.<lambda>>``. One such default makes Sphinx's
    parser give up on the whole signature: it falls back to splitting the raw
    text on commas, so no annotation links anywhere and every ``~otto.`` prefix
    shows. ``docs_api_reference.python_defaults`` says what each shows
    instead. Only the default's text changes, never the parameter.
    """
    if what not in {"function", "method", "class"} or not signature:
        return None
    try:
        params = sphinx_inspect.signature(obj).parameters
    except (TypeError, ValueError):
        return None
    factories = docs_api_reference.field_factories(obj) if what == "class" else {}
    defaults = docs_api_reference.python_defaults(params, factories)
    rewritten = docs_api_reference.replace_defaults(signature, defaults)
    return None if rewritten == signature else (rewritten, return_annotation)


#: CLI metadata goes first: its help text is unquoted and may hold commas and
#: brackets, which would split the parameter list at the wrong place for the
#: rewrites after it.
_SIGNATURE_REWRITES = (
    _strip_inherited_pydantic_signature,
    _drop_cli_metadata,
    _drop_privately_typed_params,
    _python_valid_defaults,
)


def _process_signature(app, what, name, obj, options, signature, return_annotation):
    """Apply every signature rewrite in turn.

    Autodoc keeps only the first handler's result for this event, so the
    rewrites are chained here, each seeing the one before it.
    """
    changed = False
    for rewrite in _SIGNATURE_REWRITES:
        result = rewrite(app, what, name, obj, options, signature, return_annotation)
        if result is not None:
            signature, return_annotation = result
            changed = True
    return (signature, return_annotation) if changed else None


# -- build-time GUI media + terminal blocks ------------------------------------
# Screenshots and termynal terminal blocks are PRODUCTS OF THE BUILD, never
# committed: scripts/capture_docs_media.py serves the real dashboard (via the
# browser-e2e harness fixtures), feeds it committed web/fixtures/ exports and
# captures it with headless Chromium; capture_docs_termynal.py
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
# builder touches the image files.


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


_RENDERED_PAGES = {
    "scripts.render_support_matrix": "docs/architecture/support-matrix.md",
    "scripts.render_kmodcov_matrix": "docs/cli/cov/instrumenting/kmodcov-matrix.md",
    "scripts.render_proven_range": "docs/cli/known-good.md",
    "scripts.render_module_graph": "docs/architecture/modules.md",
}
"""Each git-ignored page rendered at build time, keyed by the renderer that writes it.

Their sources are schemas/support_matrix.json, schemas/kmodcov_matrix.json,
src/otto/check/proven.json and tach.toml. Makefile SPHINX_SRCS lists every
source and renderer, so editing one re-triggers the build.
"""


def _generate_rendered_pages(app):  # noqa: ARG001 — Sphinx event signature
    """Render every page in :data:`_RENDERED_PAGES` from its committed source.

    EVERY BUILDER, not html-only: each page is a real source file that a toctree
    names, so the doctest builder has to find it on disk too — the same reason the
    termynal capture above runs unconditionally.

    A `-m` invocation rather than a path, because the support-matrix renderer
    imports `tests.*` to ask the tree which surfaces and profiles it still
    declares; run as a path, `sys.path[0]` would be `scripts/`. A non-zero exit
    RAISES, so a source the renderer cannot read, or a matrix whose axes the tree
    no longer backs, is a build FAILURE and not a warning (spec §5).
    """
    import subprocess

    from sphinx.util import logging as sphinx_logging

    logger = sphinx_logging.getLogger(__name__)
    root = pathlib.Path(__file__).parent.parent
    for module, page in _RENDERED_PAGES.items():
        proc = subprocess.run(  # noqa: S603 — fixed interpreter + a repo-local module named above
            [sys.executable, "-m", module],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(root),
        )
        if proc.stdout.strip():
            logger.info(proc.stdout.strip())
        if proc.returncode != 0:
            raise RuntimeError(
                f"{module} ({page}) failed with exit code {proc.returncode}:\n{proc.stderr}"
            )


@functools.cache
def _api_reference() -> docs_api_reference.Reference:
    """Every public object's documented home, read once per build from the declaration."""
    return docs_api_reference.build_reference(docs_api_reference.load_namespaces())


def _api_skip_member(app, what, name, obj, skip, options):  # noqa: ARG001 — Sphinx event signature
    """Index each public object once: at its home, never again on another page."""
    return docs_api_reference.skip_member(
        _api_reference(), app.env.temp_data.get("autodoc:module"), what, obj, options
    )


def _api_docstring(app, what, name, obj, options, lines):  # noqa: ARG001 — Sphinx event signature
    """Mark module docstrings; spell out a re-homed docstring's relative roles."""
    reference = _api_reference()
    if what == "module":
        docs_api_reference.module_docstring(reference, name, options, lines)
        return
    documented_in = docs_api_reference.documenting_module(name)
    defined_in = docs_api_reference.defining_module(obj)
    if defined_in is None and what == "attribute":
        defined_in = docs_api_reference.owner_module(name)
    if defined_in is None and what == "data" and documented_in:
        sources = docs_api_reference.data_sources(documented_in, name.rpartition(".")[2])
        defined_in = sources[0] if sources else None
    if documented_in and defined_in:
        lines[:] = docs_api_reference.qualify_relative_roles(lines, documented_in, defined_in)


def _resolve_public_home(app, env, node, contnode):
    """Resolve a reference written against an object's defining module to where it is documented."""
    if node.get("refdomain") != "py":
        return None
    target = docs_api_reference.public_target(
        _api_reference(), node.get("reftarget", ""), node.get("py:module")
    )
    if target is None:
        return None
    results = env.get_domain("py").resolve_any_xref(
        env, node.get("refdoc", ""), app.builder, target, node, contnode
    )
    return results[0][1] if results else None


class _DataDocumenter(DataDocumenter):
    """Render module data, with no value for one whose repr is the default one.

    It replaces the stock ``DataDocumenter``, so every data entry on the API
    pages leaves out a ``<otto.registry.Registry object>`` value, which says
    nothing the name and docstring do not (``docs_api_reference.shows_value``).
    """

    @override
    def should_suppress_value_header(self) -> bool:
        return not docs_api_reference.shows_value(self.object) or (
            super().should_suppress_value_header()
        )


class _AttributeDocumenter(AttributeDocumenter):
    """Render class attributes, with no value for one whose repr is the default one.

    It replaces the stock ``AttributeDocumenter`` by the rule
    :class:`_DataDocumenter` applies to module data.
    """

    @override
    def should_suppress_value_header(self) -> bool:
        return not docs_api_reference.shows_value(self.object) or (
            super().should_suppress_value_header()
        )


class _ReexportedDataDocumenter(_DataDocumenter):
    """Render module data a lazy package lists in ``__all__`` but binds elsewhere.

    ``DataDocumenter`` renders module data only when the documented module's
    own source gives it an attribute docstring, and a lazy package's
    ``__init__`` binds none, so a public constant such as ``otto.init.AREA_NAMES``
    would have no anchor at all. This renders it on the package's page, with
    the docstring the module that binds it gives it, and the value rule
    :class:`_DataDocumenter` applies.
    """

    objtype = "reexporteddata"
    directivetype = "data"
    priority = DataDocumenter.priority + 1

    @override
    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        return (
            isinstance(parent, ModuleDocumenter)
            and not parent.options.ignore_module_all
            and not isattr
            and membername in (getattr(parent.object, "__all__", None) or [])
            and not isinstance(member, types.ModuleType)
            and docs_api_reference.defining_module(member) is None
        )

    @override
    def get_module_comment(self, attrname):
        for module in docs_api_reference.data_sources(self.modname, attrname):
            try:
                analyzer = ModuleAnalyzer.for_module(module)
                analyzer.analyze()
            except PycodeError:
                continue
            comment = analyzer.attr_docs.get(("", attrname))
            if comment:
                return list(comment)
        return None

    @override
    def get_doc(self):
        comment = self.get_module_comment(self.objpath[-1])
        return [comment] if comment else []


def setup(app):
    app.add_autodocumenter(_DataDocumenter, override=True)
    app.add_autodocumenter(_AttributeDocumenter, override=True)
    app.add_autodocumenter(_ReexportedDataDocumenter)
    app.connect("source-read", _substitute_version_token)
    app.connect("builder-inited", _generate_docs_media)
    app.connect("builder-inited", _generate_rendered_pages)
    app.connect("missing-reference", _resolve_short_types)
    app.connect("missing-reference", _resolve_internal_aliases)
    app.connect("missing-reference", _resolve_public_home)
    app.connect("autodoc-skip-member", _api_skip_member)
    app.connect("autodoc-process-docstring", _api_docstring)
    app.connect("missing-reference", _resolve_external_doc_links)
    app.connect("autodoc-process-signature", _process_signature)


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
from otto import Status
from otto.utils import complete_marker_expression, complete_separated_list, split_on
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
