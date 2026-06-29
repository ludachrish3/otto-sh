"""Shared base model for all otto pydantic boundary specs."""

from pydantic import BaseModel, ConfigDict


class OttoModel(BaseModel):
    """Base for every otto boundary model.

    ``extra='forbid'`` turns a typo'd or unknown config field into a
    validation error that names the offending key (instead of silently
    dropping it, as the old hand-rolled merge did).
    """

    model_config = ConfigDict(extra="forbid")
