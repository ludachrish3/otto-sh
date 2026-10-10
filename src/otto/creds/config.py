"""``[creds]`` → one :class:`~otto.creds.protocol.CredsStore` per process (spec 2026-09-06 §4).

Compilation prepares the table with the selected store's configuration model
(:data:`~otto.creds.registry.CREDS_BACKENDS`), which anchors paths to the
declaring directory; construction builds the store with its factory.
Resolution across repos and the user file — and the rule that a store needs an
inventory to be keyed against — lives in :mod:`otto.inventory.config`, which
owns the one-inventory-per-process walk.

IMPORT DISCIPLINE: :mod:`otto.models.settings` is imported inside the one
function that needs it. This package is reached from the bootstrap path via
:mod:`otto.inventory`, and the settings model is the heavy end of
``otto.models``. The built-in store's configuration model lives here, in the
module that path already loads, so preparing it imports nothing more.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, ValidationError, ValidationInfo, field_validator

from ..models.base import OttoModel
from ..utils import anchor_path
from .errors import CredsError
from .protocol import CredsStore
from .registry import CREDS_BACKENDS, CredsEnv

if TYPE_CHECKING:
    from ..models.settings import CredsConfigSpec
    from ..registry import Prepared


class JsonCredsConfig(OttoModel):
    """The json store's ``[creds]`` keys: the file's ``path``, anchored to the declaring repo."""

    model_config = ConfigDict(frozen=True)

    path: Path
    """The creds file; a relative path is anchored to the declaring repo, and ``~`` is expanded."""

    @field_validator("path", mode="before")
    @classmethod
    def _not_empty(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the repo root."""
        if value == "":
            raise ValueError("must name the creds file")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _anchor(cls, value: Path, info: ValidationInfo) -> Path:
        """Anchor a relative path to the directory the table was declared in."""
        env = (info.context or {}).get("env")
        return anchor_path(value, env.anchor_dir, quote=False) if env is not None else value


@dataclass(frozen=True)
class CompiledCreds:
    """A ``[creds]`` table, prepared by its store's configuration model."""

    prepared: "Prepared[CredsEnv, CredsStore, None]"
    """The parsed configuration, ready to build."""
    anchor_dir: Path
    """Directory relative paths anchored to."""
    origin: str
    """The settings file that declared the table — for error text."""

    def same_as(self, other: "CompiledCreds") -> bool:
        """Return whether this names the same store as *other* — origin and anchor excluded.

        Two repos declaring the same store from their own settings files are
        not a conflict; two repos whose parsed configurations differ are. The
        configurations are compared in their normalized form, so one file
        named relatively from one repo and absolutely from another is one store.
        """
        return (self.prepared.backend, self.prepared.normalized) == (
            other.prepared.backend,
            other.prepared.normalized,
        )


def compile_creds(cfg: "CredsConfigSpec", *, anchor_dir: Path, origin: str) -> CompiledCreds:
    """Prepare the table's keys with the selected store's configuration model.

    Every key but ``backend`` is parsed by the model the store registered, so
    an unknown key or a bad value fails here, naming the backend, the module
    that registered it and *origin*.

    Raises:
        otto.creds.errors.CredsConstructionError: The store is not registered,
            or its keys do not parse.
    """
    prepared = CREDS_BACKENDS.prepare(
        cfg.backend, dict(cfg.model_extra or {}), CredsEnv(anchor_dir, origin), source=origin
    )
    return CompiledCreds(prepared=prepared, anchor_dir=anchor_dir, origin=origin)


def compile_creds_table(table: dict[str, Any], *, anchor_dir: Path, origin: str) -> CompiledCreds:
    """Validate a raw ``[creds]`` table as :class:`~otto.models.settings.CredsConfigSpec`.

    Then :func:`compile_creds`.
    """
    from ..models.settings import CredsConfigSpec  # deferred: see module docstring

    try:
        cfg = CredsConfigSpec.model_validate(table)
    except ValidationError as e:
        raise CredsError(f"{origin}: [creds] {e}") from e
    return compile_creds(cfg, anchor_dir=anchor_dir, origin=origin)


def construct_creds_store(compiled: CompiledCreds) -> CredsStore:
    """Build the store with its registered factory.

    Raises:
        otto.creds.errors.CredsConstructionError: The factory failed, or what
            it built is not a creds store; the message names the backend, the
            module that registered it and the settings file.
    """
    return CREDS_BACKENDS.build(compiled.prepared)
