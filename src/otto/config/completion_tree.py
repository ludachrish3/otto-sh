"""Serialise the bootstrapped Typer tree into the shape the completion shim answers from.

Spec: docs/superpowers/specs/2026-09-04-shim-completion-design.md, sections 3.3-3.4.
Runs on the SLOW path only (a cache write), after bootstrap, so resolving
every command real here is a once-per-rewrite cost; the shim never imports
this module.

Typer 0.27 vendors click as ``typer._click``: the app's commands are
``typer.core.TyperGroup``/``TyperCommand`` and its parameters
``typer.core.TyperOption``/``TyperArgument``. ``isinstance(x, click.Group)``
against the upstream ``click`` package is ALWAYS False for this app (and
``click`` is not a dependency of otto-sh), so every class test below names
Typer's own classes and every context comes from the command's own
``context_class``.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from typer._click.types import File
from typer._types import TyperChoice
from typer.core import TyperGroup, TyperOption

from .cache_sections import SHIM_SECTION as SHIM_SECTION  # noqa: PLC0414 — explicit re-export

if TYPE_CHECKING:
    from pathlib import Path

    from typer._click.core import Command, Context, Parameter

    from .repo import Repo

HOST_SCOPED_BY = "host_id"


@runtime_checkable
class _ScopedGroup(Protocol):
    """What ``otto.cli.expose.HostGroup`` exposes to the serialiser.

    ``HostGroup`` is built by a factory function, not a top-level class, so
    there is no nominal type to import; this structural Protocol lets ``ty``
    narrow ``host``/``cmd`` and type-check ``scoped_names``/``scoped_command``.
    """

    def scoped_names(self, ctx: Any, cls: type) -> list[str]: ...
    def scoped_command(self, ctx: Any, cls: type, cmd_name: str) -> Any: ...


@dataclass(frozen=True)
class TreeSerialisation:
    """The serialised tree plus the completers it could not classify."""

    tree: dict[str, Any]
    unregistered: list[str]
    """``"<command path>/<param name>"`` per completer with no ``__completion_source__``."""


def _context(cmd: "Command", name: str, parent: "Context | None") -> "Context":
    """Build a resilient context of the class the command itself uses.

    ``context_settings`` is applied HERE, the way ``make_context`` does
    (``typer/_click/core.py:707-711``), because click applies it there and
    not in ``Context.__init__`` — and the real completion path reaches every
    context through ``make_context`` (``shell_completion._resolve_context``).
    Constructing the context directly without it dropped
    ``help_option_names = ["-h", "--help"]``, which otto sets on the root app
    and on every sub-app, so ``get_help_option_names`` fell back to the
    ``["--help"]`` default and the serialised tree was missing ``-h``
    everywhere (found by the differential: Typer offered ``-h``, the shim did
    not).
    """
    extra: dict[str, Any] = {"resilient_parsing": True}
    for key, value in cmd.context_settings.items():
        if key not in extra:
            extra[key] = value
    return type(cmd).context_class(cmd, info_name=name, parent=parent, **extra)


def _original_completer(param: "Parameter") -> Any | None:
    """Unwrap Typer's two layers to the ``autocompletion=`` callable, or ``None``."""
    compat = getattr(param, "_custom_shell_complete", None)
    if compat is None:
        return None
    code = getattr(compat, "__code__", None)
    if code is None:  # a raw shell_complete= callable need not be a function
        return compat
    for name, cell in zip(code.co_freevars, compat.__closure__ or (), strict=False):
        if name == "autocompletion":
            wrapper = cell.cell_contents
            return getattr(wrapper, "__wrapped__", wrapper)
    return compat


def _source_for(param: "Parameter", where: str, unregistered: list[str]) -> dict[str, Any]:
    fn = _original_completer(param)
    if fn is not None:
        declared = getattr(fn, "__completion_source__", None)
        if not isinstance(declared, dict):
            unregistered.append(where)
            return {"kind": "live"}
        source = dict(declared)
        if source.get("lab_scoped"):
            from ..host.builtin_hosts import builtin_host_ids

            source["always"] = list(builtin_host_ids())
        return source
    kind = param.type
    if isinstance(kind, TyperChoice):
        # TyperChoice.shell_complete: str(choice) by prefix, lower-cased unless case_sensitive
        return {
            "kind": "static",
            "values": [str(c) for c in kind.choices],
            "case_sensitive": bool(kind.case_sensitive),
        }
    if isinstance(kind, File):
        return {"kind": "echo"}  # File.shell_complete echoes the fragment (no otto parameter today)
    # typer.models.TyperPath.shell_complete returns [] (a path fragment gets NOTHING), exactly
    # like ParamType's default: both are "none", never "echo".
    return {"kind": "none"}


def _param(param: "Parameter", where: str, unregistered: list[str]) -> dict[str, Any] | None:
    if getattr(param, "hidden", False):
        return None
    is_option = isinstance(param, TyperOption)  # a TyperArgument otherwise
    source = _source_for(param, f"{where}/{param.name}", unregistered)
    return {
        # A TyperArgument's `opts` holds its NAME (['host_id']), not a flag: no flags at all.
        "flags": [*param.opts, *param.secondary_opts] if is_option else [],
        "name": param.name,
        "takes_value": not (param.is_flag or param.count) if is_option else True,
        "multiple": bool(param.multiple),
        "nargs": param.nargs,
        "sep": source.get("sep"),
        "source": source,
    }


def _child(group: TyperGroup, ctx: "Context", name: str) -> "Command | None":
    # otto's root group resolves only the pending dispatch target real
    # (everything else is a stub, whose parameters would be wrong here).
    # Imported lazily: a module-scope import of otto.cli.main from otto.config
    # would be circular/heavy (otto.cli.main pulls in the whole CLI registry).
    from ..cli.main import PENDING_SUBCMD_ARGS_KEY

    ctx.meta[PENDING_SUBCMD_ARGS_KEY] = [name]
    return group.get_command(ctx, name)


def _node(
    cmd: "Command", ctx: "Context", name: str, where: str, unregistered: list[str]
) -> dict[str, Any]:
    """Serialise one command as a Node.

    A Node's REQUIRED keys are ``name``, ``params``, ``commands`` and ``group``;
    the shim indexes all four and hands over if any is missing. ``scoped_by``
    (and, on the root, ``host_classes``) are the optional ones it reads with
    ``.get``.
    """
    params = [p for p in (_param(x, where, unregistered) for x in cmd.get_params(ctx)) if p]
    commands: dict[str, Any] = {}
    if isinstance(cmd, TyperGroup):  # the test typer._click's _resolve_context descends by
        for sub_name in cmd.list_commands(ctx):
            sub = _child(cmd, ctx, sub_name)
            if sub is None or getattr(sub, "hidden", False):
                continue
            sub_ctx = _context(sub, sub_name, ctx)
            commands[sub_name] = _node(sub, sub_ctx, sub_name, f"{where}/{sub_name}", unregistered)
    # "group" is the PARSER's shape, not "has subcommands": TyperGroup sets
    # allow_interspersed_args = False (typer/core.py:999) where a leaf TyperCommand
    # inherits click's True (typer/_click/core.py:517), and that one flag decides
    # whether an option-looking word AFTER a positional is parsed as an option at
    # all. A group with no registered subcommands is still a group.
    node = {
        "name": name,
        "params": params,
        "commands": commands,
        "group": isinstance(cmd, TyperGroup),
    }
    if isinstance(cmd, _ScopedGroup):  # otto.cli.expose.HostGroup
        node["scoped_by"] = HOST_SCOPED_BY
    return node


def _host_class_views(
    root: TyperGroup, root_ctx: "Context", unregistered: list[str]
) -> dict[str, Any]:
    from ..host.os_profile import HOST_CLASSES

    host = _child(root, root_ctx, "host")
    if host is None or not isinstance(host, _ScopedGroup):
        return {}
    host_ctx = _context(host, "host", root_ctx)
    views: dict[str, Any] = {}
    for class_name, cls in HOST_CLASSES.items():  # builtins + every register_host_class()
        verbs: dict[str, Any] = {}
        for verb in host.scoped_names(host_ctx, cls):
            cmd = host.scoped_command(host_ctx, cls, verb)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            where = f"host<{class_name}>/{verb}"
            verbs[verb] = _node(cmd, _context(cmd, verb, host_ctx), verb, where, unregistered)
        views[class_name] = verbs
    return views


def serialize_tree(cli: "Command") -> TreeSerialisation:
    """Serialise the CLI as Nodes plus per-host-class verb views, every completer classified.

    *cli* is ``typer.main.get_command(app)``, a ``TyperGroup`` for otto.
    """
    unregistered: list[str] = []
    ctx = _context(cli, "otto", None)
    tree = _node(cli, ctx, "otto", "otto", unregistered)
    views = _host_class_views(cli, ctx, unregistered) if isinstance(cli, TyperGroup) else {}
    tree["host_classes"] = views
    return TreeSerialisation(tree=tree, unregistered=unregistered)


def stat_triple(path: "Path") -> list[Any]:
    """``[path, mtime_ns, size]``, or ``[path, None, None]`` for a path that does not stat.

    The same facts :func:`otto.config.completion_cache.hash_file` folds into a
    digest, stored plainly so a reader can compare them without hashing.
    """
    try:
        st = path.stat()
    except OSError:
        return [str(path), None, None]
    return [str(path), st.st_mtime_ns, st.st_size]


def _build_inventory(repos: "list[Repo]") -> Any:
    from ..inventory import build_inventory

    return build_inventory(repos)


def inventory_block(repos: "list[Repo]") -> dict[str, Any]:
    """Return the process inventory's freshness as the shim can check it (spec §3.2)."""
    from ..inventory.protocol import SupportsStatPaths

    try:
        inventory = _build_inventory(repos)
        if inventory is None:
            return {"kind": "none"}
        # Also covers a third-party stat_paths() that raises: neither a broken
        # declaration nor a broken backend may ever abort the cache write.
        paths = inventory.stat_paths() if isinstance(inventory, SupportsStatPaths) else None
    except Exception:  # noqa: BLE001 — be safe regardless of what failed above
        return {"kind": "opaque"}
    if paths is None:
        return {"kind": "opaque"}
    return {"kind": "stat", "files": [stat_triple(p) for p in paths]}


def build_shim_payload(repos: "list[Repo]", app: Any | None = None) -> dict[str, Any]:
    """Everything the shim needs to validate and answer, from the bootstrapped app (spec §3)."""
    import typer

    from .cache_sections import section_by_name
    from .completion_cache import _cache_ttl_seconds, compute_fingerprint

    if app is None:
        from ..cli.main import app as root_app

        app = root_app
    tree = serialize_tree(typer.main.get_command(app)).tree
    keys = {
        name: [stat_triple(p) for p in sorted(set(section_by_name(name).key_paths(repos)))]
        for name in ("names", "tests")
    }
    return {
        "ttl_seconds": _cache_ttl_seconds(repos),
        "keys": keys,
        "inventory": inventory_block(repos),
        "tests_digest": compute_fingerprint(repos),
        "tree": tree,
    }
