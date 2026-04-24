"""Rebuild tab-completable Typer commands from cached option schemas.

Paired with :mod:`otto.configmodule.completion_cache`: the cache stores
option metadata for every registered instruction and suite, and this module
rebuilds a ``typer.Typer`` command whose callback has a signature equivalent
to the real one — enough to light up ``--<TAB>`` on the fast path without
importing user code.

The rebuilt callbacks are never invoked. Only their ``__signature__`` is
inspected by Click during completion.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated, Any

import typer

from .completion_cache import _KIND_TO_TYPE


def _kind_to_annotation(kind: str) -> Any:
    """Return the Python type for a cached kind tag.

    ``str_list`` maps to ``list[str]``; everything else uses the
    ``_KIND_TO_TYPE`` dict shared with the writer so the two halves can't
    drift apart.
    """
    if kind == 'str_list':
        return list[str]
    return _KIND_TO_TYPE.get(kind, str)


def _default_for(kind: str, cached_default: Any) -> Any:
    """Coerce a JSON-decoded default back to the Python type the kind implies."""
    if cached_default is None:
        return None
    if kind == 'path':
        return Path(cached_default)
    return cached_default


def _build_callback(options: list[dict[str, Any]]) -> Any:
    """Build a no-op callback whose ``__signature__`` mirrors the cached options.

    Typer walks the signature for completion; it never calls the function.
    The returned callback intentionally raises if invoked — hitting it would
    mean the fast-path stub leaked into a non-completion code path.
    """
    params: list[inspect.Parameter] = []
    for opt in options:
        kind = opt['kind']
        base = _kind_to_annotation(kind)
        flags = list(opt.get('flags') or ())
        help_text = opt.get('help') or ''

        if flags:
            option_info = typer.Option(*flags, help=help_text)
        else:
            option_info = typer.Option(help=help_text)

        annotation = Annotated[base, option_info]
        default = _default_for(kind, opt.get('default'))
        params.append(inspect.Parameter(
            opt['name'],
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=annotation,
        ))

    def _stub(**_kw: Any) -> None:  # pragma: no cover — never invoked
        raise RuntimeError(
            'completion-stub callback was invoked; this should only happen '
            'if a fast-path Typer leaked into real execution.',
        )

    setattr(_stub, '__signature__', inspect.Signature(params))
    return _stub


def build_stub_command(name: str, options: list[dict[str, Any]]) -> typer.Typer:
    """Return a single-command ``typer.Typer`` named ``name``, ready to attach.

    Mirrors the shape ``@register_suite`` / ``@instruction`` produce on the
    slow path: a sub-Typer with exactly one registered command whose
    callback carries the option signature.
    """
    sub = typer.Typer()
    callback = _build_callback(options)
    # Python identifiers can't contain '-'; instruction/suite names like
    # "test-instruction" must be sanitized before use as __name__.
    callback.__name__ = name.replace('-', '_')
    sub.command(name=name)(callback)
    return sub
