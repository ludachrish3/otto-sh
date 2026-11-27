"""Rebuild tab-completable Typer commands from cached option schemas.

Paired with :mod:`otto.config.completion_cache`: the cache stores
option metadata for every registered instruction and suite, and this module
rebuilds a ``typer.Typer`` command whose callback has a signature equivalent
to the real one — enough to light up ``--<TAB>`` on the fast path without
importing user code.

The rebuilt callbacks are never invoked. Only their ``__signature__`` is
inspected by Click during completion.
"""

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
    if kind == "str_list":
        return list[str]
    return _KIND_TO_TYPE.get(kind, str)


def _default_for(kind: str, cached_default: Any) -> Any:
    """Coerce a JSON-decoded default back to the Python type the kind implies."""
    if cached_default is None:
        return None
    if kind == "path":
        return Path(cached_default)
    return cached_default


def stub_param_from_row(row: dict[str, Any]) -> inspect.Parameter:
    """Rebuild one cached completion row as the parameter of a completion stub.

    *row* is a parameter's row as a tree node stores it
    (``otto.config.completion_tree``): its spellings (``flags``; none for an
    argument), whether it takes a value, ``multiple`` and ``nargs``. The
    stub parses a command line as the live parameter does: an option that
    takes no value is a flag, one that takes ``nargs`` words takes that many,
    a repeated one repeats (a flag too, so it is offered again once given)
    and an argument is positional (``nargs`` ``-1`` takes the rest). Every
    value is a plain ``str`` and no value completer is rebuilt, whatever the
    row's ``source``: the stub offers the parameter, and a word typed for it
    lands where the live parameter puts it, but it suggests no value. The one
    builder of a stub parameter from a row.
    """
    flags = list(row.get("flags") or ())
    nargs = int(row.get("nargs", 1))
    word: Any = tuple[tuple([str] * nargs)] if nargs > 1 else str  # ty: ignore[invalid-type-form] — a type built at runtime
    default: Any = None
    if not flags:
        base: Any = list[str] if nargs == -1 else word
        info: Any = typer.Argument()
    elif not row.get("takes_value", True) and row.get("multiple"):
        # Typer repeats a switch only as a ``list[bool]`` declared with a
        # ``/``; a trailing one keeps every spelling a primary.
        base, info = list[bool], typer.Option(*flags[:-1], f"{flags[-1]}/")
    elif not row.get("takes_value", True):
        base, default, info = bool, False, typer.Option(*flags)
    else:
        base = list[word] if row.get("multiple") else word
        info = typer.Option(*flags)
    return inspect.Parameter(
        row["name"],
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
        annotation=Annotated[base, info],
    )


def stub_params(options: list[dict[str, Any]]) -> list[inspect.Parameter]:
    """Rebuild keyword-only stub parameters from cached option dicts.

    A record that carries a ``"row"`` (an argument, or an option whose
    ``kind`` cannot say how it parses) is rebuilt from that row
    (:func:`stub_param_from_row`, a ``typer.Argument`` for an argument);
    every other record is a ``typer.Option`` over the plain type of its
    ``kind``, with its flags, help and default.
    """
    params: list[inspect.Parameter] = []
    for opt in options:
        if isinstance(opt.get("row"), dict):
            params.append(stub_param_from_row(opt["row"]))
            continue
        kind = opt["kind"]
        base = _kind_to_annotation(kind)
        flags = list(opt.get("flags") or ())
        help_text = opt.get("help") or ""

        if flags:
            option_info = typer.Option(*flags, help=help_text)
        else:
            option_info = typer.Option(help=help_text)

        annotation = Annotated[base, option_info]  # ty: ignore[invalid-type-form] — runtime type construction from a value, not an annotation
        default = _default_for(kind, opt.get("default"))
        params.append(
            inspect.Parameter(
                opt["name"],
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
    return params


def _build_callback(options: list[dict[str, Any]]) -> Any:
    """Build a no-op callback whose ``__signature__`` mirrors the cached options.

    Typer walks the signature for completion; it never calls the function.
    The returned callback intentionally raises if invoked — hitting it would
    mean the fast-path stub leaked into a non-completion code path.
    """
    params = stub_params(options)

    def _stub(**_kw: Any) -> None:  # pragma: no cover — never invoked
        raise RuntimeError(
            "completion-stub callback was invoked; this should only happen "
            "if a fast-path Typer leaked into real execution.",
        )

    _stub.__signature__ = inspect.Signature(params)  # ty: ignore[missing-attribute]
    return _stub


def build_stub_command(
    name: str,
    options: list[dict[str, Any]],
    *,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
) -> typer.Typer:
    """Return a single-command ``typer.Typer`` named ``name``, ready to attach.

    Mirrors the shape ``@instruction`` produces on the slow path: a sub-Typer
    with exactly one registered command whose callback carries the option
    signature.
    """
    sub = typer.Typer()
    callback = _build_callback(options)
    # Python identifiers can't contain '-'; instruction/suite names like
    # "test-instruction" must be sanitized before use as __name__.
    callback.__name__ = name.replace("-", "_")
    sub.command(name=name, help=help)(callback)
    return sub


def build_stub_group(
    name: str,
    help: str | None,  # noqa: A002 — mirrors typer's own `help=` keyword
    children: list[dict[str, Any]],
    options: "list[dict[str, Any]] | None" = None,
) -> typer.Typer:
    """Rebuild a cached third-party GROUP as a nested stub Typer.

    Leaf children come back through :func:`build_stub_command` (the same
    shape as suite/instruction stubs); a child that itself carries
    ``"commands"`` recurses, with its own ``"options"``. *options* are the
    group's own parameters (its callback's), rebuilt by :func:`stub_params`
    on the group's callback. Only names/helps/option signatures exist —
    stubs are never dispatched (the callback raises if invoked).
    """
    grp = typer.Typer(name=name, help=help, callback=_build_callback(options) if options else None)
    for child in children:
        cname = child.get("name")
        if not cname:
            continue
        if child.get("commands") is not None:
            grp.add_typer(
                build_stub_group(cname, child.get("help"), child["commands"], child.get("options")),
                name=cname,
            )
        else:
            grp.add_typer(
                build_stub_command(cname, child.get("options") or [], help=child.get("help"))
            )
    return grp
