"""Map a host method's signature to Typer CLI parameters.

Generalizes the proven ``cli/run.py::_wrap_with_options`` pattern: resolve type
hints with ``get_type_hints(include_extras=True)``, build an ``inspect.Parameter``
list (a mix of arguments, options, flags, and variadics), and let a wrapper with an
assigned ``__signature__`` reconstruct the bound-method call. Typer owns parsing,
help, validation, completion, and exit codes.
"""
from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Union, get_args, get_origin, get_type_hints

import typer

from ..utils import Arg, Exclude, Opt


def coerce_scalar(value: str, target: type) -> Any:
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
    if raw is None:
        return None
    if raw == "":
        return []
    return [coerce_scalar(s, elem_type) for s in raw.split(",")]


def parse_kv_dict(raw: str | None, val_type: type) -> dict[str, Any] | None:
    if raw is None:
        return None
    if raw == "":
        return {}
    out: dict[str, Any] = {}
    for pair in raw.split(","):
        key, _, val = pair.partition("=")
        out[key] = coerce_scalar(val, val_type)
    return out


@dataclass
class CliBinding:
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
            return typing.Optional[inner_base], tuple(meta)
    return hint, ()


def _is_optional(base: Any) -> tuple[bool, Any]:
    """Detect ``X | None`` / ``Optional[X]``; return (is_optional, X)."""
    _native_union = getattr(types, "UnionType", None)
    origin = get_origin(base)
    is_union = origin is Union or (_native_union is not None and isinstance(base, _native_union))
    if is_union:
        args = [a for a in get_args(base) if a is not type(None)]
        if len(args) == 1 and len(get_args(base)) == 2:
            return True, args[0]
    return False, base


def _normalize_scalar(base: Any, marker_type: type | None) -> Any:
    """Collapse a CLI-incompatible scalar to a Typer-acceptable type."""
    if marker_type is not None:
        return marker_type
    is_opt, inner = _is_optional(base)
    if is_opt:
        norm = _normalize_scalar(inner, None)
        return typing.Optional[norm]
    _native_union = getattr(types, "UnionType", None)
    origin = get_origin(base)
    is_union = origin is Union or (_native_union is not None and isinstance(base, _native_union))
    if is_union:
        return str  # str | Path and friends -> str
    return base


def build_cli_binding(func: Callable[..., Any]) -> CliBinding:
    """Introspect *func*'s signature and produce a :class:`CliBinding`.

    Args:
        func: An unbound method or function whose first parameter is ``self``.

    Returns:
        A :class:`CliBinding` with Typer-facing parameters, excluded params,
        and converters for list/dict options.

    Raises:
        ValueError: If more than one parameter is marked ``Arg(variadic=True)``.
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)
    binding = CliBinding()
    variadic_count = 0

    for name, sp in sig.parameters.items():
        if name == "self":
            continue
        hint = hints.get(name, sp.annotation)
        base, meta = _split_annotation(hint)
        arg = next((m for m in meta if isinstance(m, Arg)), None)
        opt = next((m for m in meta if isinstance(m, Opt)), None)
        is_excluded = any(isinstance(m, Exclude.__class__) for m in meta)
        default = sp.default

        if is_excluded:
            binding.excluded[name] = default
            continue

        # --- variadic positional list (union-typed lists need the marker) ---
        if arg is not None and arg.variadic:
            variadic_count += 1
            if variadic_count > 1:
                raise ValueError(
                    f"{getattr(func, '__name__', func)!r}: at most one parameter "
                    f"may be Arg(variadic=True)"
                )
            elem = arg.elem_type or str
            ann = Annotated[list[elem], typer.Argument(help=arg.help)]
            binding.params.append(inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=(sp.default if sp.default is not inspect.Parameter.empty
                         else inspect.Parameter.empty),
                annotation=ann,
            ))
            continue

        origin = get_origin(base)

        # --- list/dict OPTION (comma / key=value), forward-looking ---
        if origin in (list, dict) and arg is None:
            elem = (get_args(base) or (str,))[-1]
            if origin is list:
                binding.converters[name] = lambda raw, e=elem: parse_comma_list(raw, e)
                help_txt = (opt.help if opt else None)
            else:
                binding.converters[name] = lambda raw, e=elem: parse_kv_dict(raw, e)
                help_txt = (opt.help if opt else None)
            ann = Annotated[typing.Optional[str], typer.Option(help=help_txt)]
            binding.params.append(inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=ann,
            ))
            continue

        # --- scalar: normalize union, then arg/opt/inference ---
        norm = _normalize_scalar(base, (arg.elem_type if arg else (opt.elem_type if opt else None)))
        if arg is not None:  # explicit positional (e.g. defaulted scalar we keep positional)
            ann = Annotated[norm, typer.Argument(help=arg.help)]
            kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        elif opt is not None:  # explicit option
            ann = Annotated[norm, typer.Option(help=opt.help)]
            kind = inspect.Parameter.KEYWORD_ONLY
        else:  # attach explicit Typer annotation so KEYWORD_ONLY wrappers still render correctly
            if default is inspect.Parameter.empty:
                ann = Annotated[norm, typer.Argument()]
            else:
                ann = Annotated[norm, typer.Option()]
            kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        binding.params.append(inspect.Parameter(
            name, kind, default=default, annotation=ann,
        ))

    return binding
