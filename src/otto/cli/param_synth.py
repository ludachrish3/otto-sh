"""Map a host method's signature to Typer CLI parameters.

Generalizes the proven ``cli/invoke.py::_wrap_with_options`` pattern: resolve type
hints with ``get_type_hints(include_extras=True)``, build an ``inspect.Parameter``
list (a mix of arguments, options, flags, and variadics), and let a wrapper with an
assigned ``__signature__`` reconstruct the bound-method call. Typer owns parsing,
help, validation, completion, and exit codes.
"""

import copy
import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

import typer

from ..utils import Arg, Exclude, Opt, split_on
from .completers import completion_source


def coerce_scalar(value: str, target: type) -> Any:
    """Convert a raw string value to *target* (bool, int, float, Path, or str)."""
    if target is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is Path:
        return Path(value)
    return value


def parse_comma_list(raw: str | None, elem_type: type) -> list[Any] | None:
    """Parse a comma-separated string into a typed list, returning ``None`` for a ``None`` input."""
    if raw is None:
        return None
    if raw == "":
        return []
    return [coerce_scalar(s, elem_type) for s in split_on(raw)]


def parse_kv_dict(raw: str | None, val_type: type) -> dict[str, Any] | None:
    """Parse a ``key=value,...`` string into a typed dict.

    Returns ``None`` when *raw* is ``None``, or an empty dict when *raw* is an empty string.
    """
    if raw is None:
        return None
    if raw == "":
        return {}
    out: dict[str, Any] = {}
    for pair in split_on(raw):
        key, _, val = pair.partition("=")
        out[key] = coerce_scalar(val, val_type)
    return out


def _remote_completer(marker: "Arg | Opt | None") -> Any:
    """Return the autocompletion callback for a ``remote_path``-marked param, else ``None``."""
    kind = getattr(marker, "remote_path", None)
    if kind is None:
        return None

    @completion_source(kind="live")
    def _complete(ctx: typer.Context, incomplete: str) -> list[str]:
        from . import remote_completion

        # Late attribute lookup (not a from-import) so tests can monkeypatch
        # remote_completion.remote_path_completer.
        return remote_completion.remote_path_completer(ctx, incomplete, kind=kind)

    return _complete


def _user_completer(marker: "Arg | Opt | None") -> Any:
    """Return the autocompletion callback for a ``host_user``-marked param, else ``None``."""
    flavour = getattr(marker, "host_user", None)
    if flavour is None:
        return None

    from .completers import host_user_completer

    return host_user_completer(flavour)


def _completer_for(marker: "Arg | Opt | None") -> Any:
    """Return the one completer a marker asks for: remote path, host user, or none."""
    return _remote_completer(marker) or _user_completer(marker)


def _opt_decls(opt: "Opt", param_name: str, norm_type: Any = None) -> "list[str]":
    """Build the typer param_decls an ``Opt`` asks for: long flag first, short alias second.

    Empty when the option carries neither, so typer synthesizes its own
    ``--<param-name>`` exactly as before. A ``short`` alone still gets the
    synthesized long flag spelled out, because typer names an option after
    its FIRST decl and a bare ``-r`` would make ``r`` the option's name — and
    when *norm_type* is ``bool``, that spelled-out flag is written in click's
    ``--name/--no-name`` slash form, because a bare ``--name`` decl loses
    typer's auto-generated ``--no-name`` secondary that a no-decls bool
    option gets for free.
    """
    decls: list[str] = []
    if opt.name:
        decls.append(opt.name)
    elif opt.short:
        long_flag = "--" + param_name.replace("_", "-")
        if norm_type is bool:
            long_flag += "/--no-" + param_name.replace("_", "-")
        decls.append(long_flag)
    if opt.short:
        decls.append(opt.short)
    return decls


@dataclass
class CliBinding:
    """Typer-facing parameter list and metadata produced by ``build_cli_binding``.

    Holds the synthesised ``inspect.Parameter`` objects that define the CLI
    signature (``params``), parameters excluded from the CLI that carry their
    original defaults (``excluded``), and per-parameter converters that
    reconstruct list or dict values from the comma-delimited CLI strings
    (``converters``).
    """

    params: list[inspect.Parameter] = field(default_factory=list)
    excluded: dict[str, Any] = field(default_factory=dict)
    converters: dict[str, Callable[[Any], Any]] = field(default_factory=dict)


def _split_annotation(hint: Any) -> tuple[Any, tuple[Any, ...]]:
    """Return (base_type, marker_metadata) from a possibly-Annotated hint.

    Handles two forms:
    - ``Annotated[T, markers...]`` — direct
    - ``Optional[Annotated[T, markers...]]`` — get_type_hints wraps Annotated
      hints in Optional when the parameter has a ``None`` default; unwrap it.
    """
    if get_origin(hint) is Annotated:
        base, *meta = get_args(hint)
        return base, tuple(meta)
    # Handle Optional[Annotated[T, ...]] produced by get_type_hints on None-defaulted params
    _native_union = getattr(types, "UnionType", None)
    origin = get_origin(hint)
    is_union = origin is Union or (_native_union is not None and isinstance(hint, _native_union))
    if is_union:
        args = get_args(hint)
        none_stripped = [a for a in args if a is not type(None)]
        if len(none_stripped) == 1 and get_origin(none_stripped[0]) is Annotated:
            inner_annotated = none_stripped[0]
            inner_base, *meta = get_args(inner_annotated)
            # Reconstruct Optional[inner_base] as the base type
            return typing.Optional[inner_base], tuple(meta)  # noqa: UP045 — runtime Optional[] construction (not an annotation); X | None raises TypeError on special forms  # ty: ignore[invalid-type-form] — runtime type construction from a value, not an annotation
    return hint, ()


def _is_optional(base: Any) -> tuple[bool, Any]:
    """Detect ``X | None`` / ``Optional[X]``; return (is_optional, X)."""
    _native_union = getattr(types, "UnionType", None)
    origin = get_origin(base)
    is_union = origin is Union or (_native_union is not None and isinstance(base, _native_union))
    if is_union:
        args = [a for a in get_args(base) if a is not type(None)]
        if len(args) == 1 and len(get_args(base)) == 2:  # noqa: PLR2004 — Optional[X] has exactly 2 type args (X and NoneType), clearer inline
            return True, args[0]
    return False, base


def _normalize_scalar(base: Any, marker_type: type | None) -> Any:
    """Collapse a CLI-incompatible scalar to a Typer-acceptable type."""
    if marker_type is not None:
        return marker_type
    is_opt, inner = _is_optional(base)
    if is_opt:
        norm = _normalize_scalar(inner, None)
        return typing.Optional[norm]  # noqa: UP045 — runtime Optional[] construction (not an annotation); X | None raises TypeError on special forms  # ty: ignore[invalid-type-form] — runtime type construction from a value, not an annotation
    _native_union = getattr(types, "UnionType", None)
    origin = get_origin(base)
    is_union = origin is Union or (_native_union is not None and isinstance(base, _native_union))
    if is_union:
        return str  # str | Path and friends -> str
    return base


def _list_element(base: Any) -> Any:
    """Return ``X`` when *base* is ``list[X]`` or a union containing ``list[X]``, else ``None``."""
    if get_origin(base) is list:
        return (get_args(base) or (str,))[0]
    _native_union = getattr(types, "UnionType", None)
    if get_origin(base) is Union or (_native_union is not None and isinstance(base, _native_union)):
        for member in get_args(base):
            if get_origin(member) is list:
                return (get_args(member) or (str,))[0]
    return None


def build_cli_binding(func: Callable[..., Any]) -> CliBinding:
    """Introspect *func*'s signature and produce a :class:`CliBinding`.

    The parameter KIND decides argument versus option (spec §6.6): a
    positional parameter is a positional argument, in signature order,
    required or defaulted alike (a list-typed one, or a union containing
    ``list[X]``, is the one ``nargs=-1`` variadic); a keyword-only parameter
    is a ``--option``, required when it has no default. ``Arg`` and ``Opt``
    carry presentation only (metavar, help, flag spelling, completer,
    ``elem_type``, ``min``), and an ``Exclude``-marked parameter stays off the
    CLI with its default passed through. ``**kwargs`` is not exposed.

    Args:
        func: An unbound method or function whose first parameter is ``self``.

    Returns:
        A :class:`CliBinding` with Typer-facing parameters, excluded params,
        and converters for list/dict options.

    Raises:
        ValueError: For a positional-only parameter (the command dispatches by
            keyword), a ``*args``, more than one positional list parameter, a
            marker that contradicts the signature (``Arg`` on a keyword-only
            parameter, ``Opt`` on a positional one), or a ``list``/``dict``
            option carrying ``remote_path``/``host_user`` (that option renders
            one comma/``key=value`` string a per-element completer cannot
            complete).
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)
    binding = CliBinding()
    verb = repr(getattr(func, "__name__", func))
    variadic: str | None = None

    for name, sp in sig.parameters.items():
        if name == "self" or sp.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if sp.kind is inspect.Parameter.VAR_POSITIONAL:
            raise ValueError(
                f"{verb}: *{name} — *args on an exposed verb is refused; "
                f"spell a variadic as a positional list parameter"
            )
        if sp.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise ValueError(
                f"{verb}: {name!r} — positional-only parameters cannot be dispatched by keyword"
            )
        hint = hints.get(name, sp.annotation)
        base, meta = _split_annotation(hint)
        arg = next((m for m in meta if isinstance(m, Arg)), None)
        opt = next((m for m in meta if isinstance(m, Opt)), None)
        default = sp.default

        if any(isinstance(m, Exclude.__class__) for m in meta):
            binding.excluded[name] = default
            continue

        positional = sp.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        if arg is not None and not positional:
            raise ValueError(
                f"{verb}: {name!r} — the Arg marker contradicts the signature: the "
                f"parameter is keyword-only, so it is a --option; use Opt(...) for its presentation"
            )
        if opt is not None and positional:
            raise ValueError(
                f"{verb}: {name!r} — the Opt marker contradicts the signature: the "
                f"parameter is positional, so it is an argument; use Arg(...) for its presentation"
            )

        if positional:
            elem = _list_element(base)
            if elem is not None:
                if variadic is not None:
                    raise ValueError(
                        f"{verb}: at most one positional list-typed parameter "
                        f"({variadic!r} and {name!r}) — a verb has one variadic argument"
                    )
                variadic = name
                cli_type: Any = list[(arg.elem_type if arg and arg.elem_type else elem)]  # ty: ignore[invalid-type-form] — runtime type construction from a value, not an annotation
            else:
                cli_type = _normalize_scalar(base, arg.elem_type if arg else None)
            ann = Annotated[
                cli_type,
                typer.Argument(
                    metavar=arg.name if arg else None,
                    help=arg.help if arg else None,
                    autocompletion=_completer_for(arg),
                ),
            ]
            binding.params.append(
                inspect.Parameter(
                    name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default, annotation=ann
                )
            )
            continue

        origin = get_origin(base)
        if origin in (list, dict):
            # This branch renders one comma/key=value STRING, so a per-element
            # completer (a path, a host login) would complete the whole field,
            # not the element under the cursor. Fail loud rather than accept a
            # marker that silently does nothing.
            for marker_field in ("remote_path", "host_user"):
                if getattr(opt, marker_field, None) is not None:
                    raise ValueError(
                        f"{verb}: {name!r} — {marker_field} is not "
                        f"supported on a comma-list/key=value option"
                        + (
                            "; make it a positional list parameter with Arg(remote_path=...) "
                            "for a completable path list"
                            if marker_field == "remote_path"
                            else ""
                        )
                    )
            elem = (get_args(base) or (str,))[-1]
            parse = parse_comma_list if origin is list else parse_kv_dict
            # Omitted (``None`` from Typer) is the parameter's own default (§6.6),
            # copied so a verb that mutates it never changes the next call's.
            declared = None if default is inspect.Parameter.empty else default
            binding.converters[name] = lambda raw, e=elem, p=parse, d=declared: (
                copy.deepcopy(d) if raw is None else p(raw, e)
            )
            opt_decls = _opt_decls(opt, name) if opt is not None else []
            # `typer.Option`'s signature is `Option(default=..., *param_decls, ...)`; a
            # name override must land in the vararg slot, so the leading `...` is
            # required — it's ignored in favor of the Python parameter's own default.
            ann = Annotated[
                str | None, typer.Option(..., *opt_decls, help=opt.help if opt else None)
            ]
            binding.params.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None if default is not inspect.Parameter.empty else default,
                    annotation=ann,
                )
            )
            continue

        norm = _normalize_scalar(base, opt.elem_type if opt else None)
        # See the list/dict branch above for why `...` must precede `*opt_decls`.
        ann = Annotated[
            norm,  # ty: ignore[invalid-type-form] — runtime type construction from a value, not an annotation
            typer.Option(
                ...,
                *(_opt_decls(opt, name, norm) if opt is not None else []),
                help=opt.help if opt else None,
                min=opt.min if opt else None,
                autocompletion=_completer_for(opt),
            ),
        ]
        binding.params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann)
        )

    return binding
