"""Shared parameter-building utilities for converting Options dataclasses
into ``inspect.Parameter`` lists consumable by Typer.

Used by both ``otto.suite.register`` (suite options) and ``otto.cli.run``
(instruction options).
"""

import dataclasses
import inspect
from typing import Any, get_type_hints

import pydantic
import typer
from pydantic.fields import FieldInfo


def build_options(opts_cls: type, kwargs: dict[str, Any]) -> Any:
    """Construct an Options instance, translating a pydantic ``ValidationError``
    into a clean ``typer.BadParameter`` (exit 2 with the field+reason message)
    instead of letting a traceback escape.

    Uses ``typer.BadParameter``, not ``click.BadParameter``: Typer >= 0.26
    vendors its own click fork and only its handler catches the vendored
    exception — a real ``click.BadParameter`` would escape uncaught (exit 1, no
    message), the same trap that bit the missing-``--lab`` gate.

    Plain stdlib dataclasses construct exactly as before — no pydantic is
    involved unless the class is a ``@pydantic.dataclasses.dataclass`` (e.g. via
    ``@otto.options``) and a field constraint (``Field(gt=0)``, a validator, ...)
    rejects the value.
    """
    try:
        return opts_cls(**kwargs)
    except pydantic.ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
        raise typer.BadParameter(problems) from exc


def options_params(opts_cls: type) -> list[inspect.Parameter]:
    """Convert an Options dataclass into inspect.Parameters for Typer.

    Each field must be annotated as ``Annotated[T, typer.Option(...)]`` so that
    Typer can extract the help text and other metadata.  Works with inherited
    fields because ``get_type_hints`` and ``dataclasses.fields`` both traverse
    the full MRO.
    """
    params: list[inspect.Parameter] = []
    hints = get_type_hints(opts_cls, include_extras=True)
    flds = {f.name: f for f in dataclasses.fields(opts_cls)}
    for name, typ in hints.items():
        f = flds[name]
        default = (
            f.default
            if f.default is not dataclasses.MISSING
            else inspect.Parameter.empty
        )
        # An ``@options`` (pydantic dataclass) field written as
        # ``= Field(default=X, <constraint>)`` stores a ``FieldInfo`` as the
        # dataclass-field default. Unwrap it to the real value so Typer gets a
        # usable CLI default instead of a ``FieldInfo`` object.
        if isinstance(default, FieldInfo):
            if default.default_factory is not None:
                # Called once here (signature-build time), not per invocation —
                # safe because the pydantic dataclass calls the factory again on
                # each construction, so the per-instance field is always fresh;
                # this value is only Typer's CLI-default sentinel.
                default = default.default_factory()
            elif not default.is_required():
                default = default.default
            else:
                default = inspect.Parameter.empty
        params.append(inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=typ,
        ))
    return params
