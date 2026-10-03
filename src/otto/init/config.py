"""The values one ``otto init`` run scaffolds with, and the rules they obey."""

import dataclasses
import re
from pathlib import Path

from .errors import InitInputError
from .settings_file import existing_settings_name

DEFAULT_KMODCOV_DIR = "third_party/otto_kmodcov"
"""Where the kmodcov area vendors the otto_kmodcov library, relative to the repo root."""


@dataclasses.dataclass(frozen=True)
class InitConfig:
    """The repo root and the values the settings template is filled with.

    Refuses, with :class:`~otto.init.errors.InitInputError` naming the field:

    * ``root`` — *root* is not a directory;
    * ``kmodcov_dir`` — the directory does not resolve strictly inside
      *root*. ``pathlib`` silently discards *root* when *kmodcov_dir* is
      absolute (``root / "/etc"`` is ``/etc``), a ``../`` value climbs back
      out, and ``"."`` / ``""`` collapse onto *root* itself, which would
      export the library into the repo root and drop the consumer starter
      beside the repo. All are refused before any file is written.

    Build one with :meth:`for_repo` to get ``otto init``'s defaults.
    """

    root: Path
    name: str
    version: str
    kmodcov_dir: str = DEFAULT_KMODCOV_DIR
    """Where the kmodcov area vendors the otto_kmodcov library, relative to *root*."""

    def __post_init__(self) -> None:
        if not self.root.is_dir():
            raise InitInputError(f"{self.root} is not a directory", field="root")
        vendored = self.kmodcov_path.resolve()
        if self.root.resolve() not in vendored.parents:
            raise InitInputError(
                f"{self.kmodcov_dir!r} must be a relative path strictly inside the repo "
                f"({self.root}); it resolves to {vendored}",
                field="kmodcov_dir",
            )

    @classmethod
    def for_repo(
        cls,
        root: Path,
        *,
        name: str = "",
        version: str = "0.1.0",
        kmodcov_dir: str = DEFAULT_KMODCOV_DIR,
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
        )

    @property
    def kmodcov_path(self) -> Path:
        """The directory the kmodcov area exports the library into."""
        return self.root / self.kmodcov_dir

    @property
    def module_base(self) -> str:
        """``name`` sanitized into a valid module-name base (``my-repo`` -> ``my_repo``)."""
        base = re.sub(r"\W", "_", self.name)
        return f"_{base}" if base[:1].isdigit() else base

    @property
    def init_module(self) -> str:
        """The init module the scaffold writes when the settings declare none."""
        return f"{self.module_base}_instructions"
