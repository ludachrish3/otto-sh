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
    """One-line rendering of *error*: ``field: message [type=…]`` per problem, ``;``-joined.

    ``str(ValidationError)`` spans several lines and carries pydantic's
    ``input_value=`` repr of the rejected value — for a boundary reader
    validating a whole dict (a host entry, a merged cred), that repr can be
    the tail of a password. This calls ``errors(include_input=False)`` and
    reads only ``loc``/``msg``/``type`` — never ``input`` — so the input
    itself cannot reach the message, while ``type=…`` (pydantic's stable
    error code, e.g. ``extra_forbidden``) still names *why* precisely enough
    to search a fix by. Shared by every boundary reader that re-raises a
    pydantic failure as its own error: the inventory's stage-1 parser, the
    NetBox backend, the creds store, ``otto init``'s item validator, and the
    lab repository's host-entry wrap.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<entry>'}: "
        f"{item['msg']} [type={item['type']}]"
        for item in error.errors(include_input=False)
    )
