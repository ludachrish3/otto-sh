"""Shared base model for all otto pydantic boundary specs."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from pydantic import ValidationError


class OttoModel(BaseModel):
    """Base for every otto boundary model.

    ``extra='forbid'`` turns a typo'd or unknown config field into a
    validation error that names the offending key (instead of silently
    dropping it, as the old hand-rolled merge did).

    Descendants that read *historical* data are the sanctioned exception:
    the lenient ``*Record`` spec variants in :mod:`otto.models.monitor`
    override ``extra`` to ``'ignore'`` so older otto builds can read exports
    written by newer ones.
    """

    model_config = ConfigDict(extra="forbid")


def compact_validation_error(error: "ValidationError") -> str:
    """One-line rendering of *error*: ``field: message`` per problem, ``;``-joined.

    ``str(ValidationError)`` spans several lines, and an error a human reads in
    a log line — or a test matches with a single regex — must not. Shared by
    every boundary reader that re-raises a pydantic failure as its own error:
    the inventory's stage-1 parser, the NetBox backend, the creds store.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<entry>'}: {item['msg']}"
        for item in error.errors()
    )
