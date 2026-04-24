"""Shared parameter-building utilities for converting Options dataclasses
into ``inspect.Parameter`` lists consumable by Typer.

Used by both ``otto.suite.register`` (suite options) and ``otto.cli.run``
(instruction options).
"""

import dataclasses
import inspect
from typing import get_type_hints


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
        params.append(inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=typ,
        ))
    return params
