"""The values one ``otto init`` run scaffolds with, and the rules they obey."""

import dataclasses
import re
from pathlib import Path

from .area_dirs import DEFAULT_EMBEDDED_GCOV_DIR, DEFAULT_KMODCOV_DIR
from .errors import InitInputError
from .settings_file import existing_settings_name


@dataclasses.dataclass(frozen=True)
class InitConfig:
    """The project root and the values the settings template is filled with.

    Refuses, with :class:`~otto.init.errors.InitInputError` naming the field:

    * ``root`` — *root* is not a directory;
    * ``kmodcov_dir``, ``embedded_gcov_dir`` — the directory does not resolve
      strictly inside *root*. ``pathlib`` silently discards *root* when the directory is
      absolute (``root / "/etc"`` is ``/etc``), a ``../`` value climbs back
      out, and ``"."`` / ``""`` collapse onto *root* itself, which would
      export into the project root and drop the consumer starter beside the
      project root. All are refused before any file is written.

    Build one with :meth:`for_project` to get ``otto init``'s defaults.
    """

    root: Path
    name: str
    version: str
    kmodcov_dir: str = DEFAULT_KMODCOV_DIR
    """Where the kmodcov area vendors the otto_kmodcov library, relative to *root*."""
    embedded_gcov_dir: str = DEFAULT_EMBEDDED_GCOV_DIR
    """Where the embedded-gcov area writes the runtime patch, relative to *root*."""

    def __post_init__(self) -> None:
        if not self.root.is_dir():
            raise InitInputError(f"{self.root} is not a directory", field="root")
        self._require_inside_root("kmodcov_dir", self.kmodcov_dir)
        self._require_inside_root("embedded_gcov_dir", self.embedded_gcov_dir)

    def _require_inside_root(self, field: str, value: str) -> None:
        """Refuse *value*, held by directory field *field*, unless strictly inside *root*."""
        resolved = (self.root / value).resolve()
        if self.root.resolve() not in resolved.parents:
            raise InitInputError(
                f"{value!r} must be a relative path strictly inside the project root "
                f"({self.root}); it resolves to {resolved}",
                field=field,
            )

    @classmethod
    def for_project(
        cls,
        root: Path,
        *,
        name: str = "",
        version: str = "0.1.0",
        kmodcov_dir: str = DEFAULT_KMODCOV_DIR,
        embedded_gcov_dir: str = DEFAULT_EMBEDDED_GCOV_DIR,
    ) -> "InitConfig":
        """Build the config ``otto init`` uses for *root* (resolved).

        An empty *name* defaults to the existing settings' ``name``, else the
        directory name.
        """
        resolved = Path(root).resolve()
        return cls(
            root=resolved,
            name=name or existing_settings_name(resolved) or resolved.name,
            version=version,
            kmodcov_dir=kmodcov_dir,
            embedded_gcov_dir=embedded_gcov_dir,
        )

    @property
    def kmodcov_path(self) -> Path:
        """The directory the kmodcov area exports the library into."""
        return self.root / self.kmodcov_dir

    @property
    def embedded_gcov_path(self) -> Path:
        """The directory the embedded-gcov area exports the patch into."""
        return self.root / self.embedded_gcov_dir

    @property
    def module_base(self) -> str:
        """``name`` sanitized into a valid module-name base (``my-project`` -> ``my_project``)."""
        base = re.sub(r"\W", "_", self.name)
        return f"_{base}" if base[:1].isdigit() else base

    @property
    def init_module(self) -> str:
        """The init module the scaffold writes when the settings declare none."""
        return f"{self.module_base}_instructions"
