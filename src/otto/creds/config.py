"""``[creds]`` → one :class:`~otto.creds.protocol.CredsStore` per process (spec 2026-09-06 §4).

Compilation validates the store's kwargs knowing the backend and anchors
paths to the declaring directory; construction instantiates the registered
class. Resolution across repos and the user file — and the rule that a store
needs an inventory to be keyed against — lives in
:mod:`otto.inventory.config`, which owns the one-inventory-per-process walk.

IMPORT DISCIPLINE: :mod:`otto.models.settings` is imported inside the one
function that needs it. This package is reached from the bootstrap path via
:mod:`otto.inventory`, and the settings model is the heavy end of
``otto.models``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..utils import anchor_path
from .errors import CredsError
from .protocol import CredsStore
from .registry import get_creds_backend_class

if TYPE_CHECKING:
    from ..models.settings import CredsConfigSpec


@dataclass(frozen=True)
class CompiledCreds:
    """A validated, anchored ``[creds]`` table."""

    backend: str
    kwargs: dict[str, Any]
    anchor_dir: Path
    origin: str

    def same_as(self, other: "CompiledCreds") -> bool:
        """Return whether this names the same store as *other* — origin and anchor excluded.

        Two repos declaring the same store from their own settings files are
        not a conflict; two repos whose anchored kwargs differ are.
        """
        return (self.backend, self.kwargs) == (other.backend, other.kwargs)


def compile_creds(cfg: "CredsConfigSpec", *, anchor_dir: Path, origin: str) -> CompiledCreds:
    """Validate the store's kwargs knowing the backend; anchor paths to *anchor_dir*.

    The json store takes ``path`` (required); anything else is an error naming
    it. Other backends validate their own kwargs in their constructor (the
    ``compile_inventory`` precedent).
    """
    extras: dict[str, Any] = dict(cfg.model_extra or {})
    if cfg.backend == "json":
        path = extras.pop("path", None)
        if not isinstance(path, str) or not path:
            raise CredsError(f"{origin}: [creds] backend 'json' requires a 'path' string")
        if extras:
            raise CredsError(
                f"{origin}: [creds] unknown key(s) for the json backend: {sorted(extras)}"
            )
        kwargs: dict[str, Any] = {"path": anchor_path(Path(path), anchor_dir)}
    else:
        kwargs = extras
    return CompiledCreds(backend=cfg.backend, kwargs=kwargs, anchor_dir=anchor_dir, origin=origin)


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
    """Instantiate the registered store. ``Path.resolve()`` happens here, on the way in.

    A third-party constructor's ``TypeError``/``ValueError`` is wrapped naming
    the origin and the backend — the raw ``unexpected keyword argument``
    names neither the settings file nor the backend the user selected.
    """
    try:
        cls = get_creds_backend_class(compiled.backend)
    except ValueError as e:
        raise CredsError(f"{compiled.origin}: {e}") from e
    if compiled.backend == "json":
        return cls(path=Path(compiled.kwargs["path"]).resolve())
    try:
        return cls(repo_dir=compiled.anchor_dir, **compiled.kwargs)
    except (TypeError, ValueError) as e:
        raise CredsError(f"{compiled.origin}: [creds] backend {compiled.backend!r}: {e}") from e
