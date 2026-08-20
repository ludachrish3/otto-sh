"""Compile ``[[lab.sources]]`` declarations into constructible source records.

Sits between the settings model (which types the entries loosely — backend
kwargs are open-ended) and backend construction. Kept import-light: consumed
from ``Repo.parse_settings`` and the completion fast path, so the heavy
backend modules are imported lazily where needed.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..utils import anchor_path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..config.repo import Repo
    from ..models.settings import LabConfigSpec, LabSourceSpec
    from .protocol import LabRepository


@dataclass(frozen=True)
class CompiledLabSource:
    """A validated, anchored source declaration, ready to construct."""

    label: str
    """``<repo-name>/<name-or-default>`` — names this source in warnings/errors."""

    backend: str
    repo_dir: Path
    # Attribute docstrings here must not open with a ``word: text`` shape —
    # Napoleon reads that as a type annotation and the nitpicky docs build
    # fails on the phantom class ("json backend only").
    paths: list[Path] = field(default_factory=list)
    """Anchored directory-or-``.json``-file entries (json backend only)."""

    kwargs: dict[str, Any] = field(default_factory=dict)
    """Constructor kwargs passed verbatim (custom backends only)."""

    def lab_files(self) -> list[Path]:
        """Return the lab files a json source reads (empty for custom backends).

        The single home of the file-vs-directory rule on the CONFIG side —
        completion's fingerprint and raw link reader both use it, so they can
        never disagree with the backend about which files matter.
        """
        from .json_repository import LAB_FILENAME  # lazy: keep this module light

        return [p if p.suffix == ".json" else p / LAB_FILENAME for p in self.paths]


def compile_lab_sources(
    cfg: "LabConfigSpec | None", *, repo_name: str, sut_dir: Path
) -> list[CompiledLabSource]:
    """Compile one repo's lab-source declarations, in declaration order.

    Raises ``ValueError`` (a settings error, surfaced at bootstrap) for every
    malformed shape: json entries without usable ``paths`` or with unknown
    keys, and duplicate labels within the repo.
    """
    if cfg is None:
        return []
    compiled = [
        _compile_entry(spec, ordinal=i, repo_name=repo_name, sut_dir=sut_dir)
        for i, spec in enumerate(cfg.sources, start=1)
    ]
    labels = [s.label for s in compiled]
    if len(set(labels)) != len(labels):
        dupes = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
        raise ValueError(
            f"repo {repo_name!r}: [[lab.sources]] labels must be unique; duplicated: {dupes}"
        )
    return compiled


def _compile_entry(
    spec: "LabSourceSpec", *, ordinal: int, repo_name: str, sut_dir: Path
) -> CompiledLabSource:
    label = f"{repo_name}/{spec.name or f'{spec.backend}#{ordinal}'}"
    extra = dict(spec.model_extra or {})
    if spec.backend == "json":
        raw = extra.pop("paths", None)
        if extra:
            raise ValueError(
                f"source {label}: unknown key(s) for the json backend: {sorted(extra)}; "
                "it takes only `paths`"
            )
        if not isinstance(raw, list) or not raw or not all(isinstance(p, str) and p for p in raw):
            raise ValueError(
                f"source {label}: the json backend requires `paths`, a non-empty "
                "list of directories (searched for lab.json) or .json files"
            )
        # ``anchor_path`` expands ``~`` itself, then anchors still-relative
        # entries to the repo — settings.toml is committed, so a CWD-relative
        # path in it could never resolve stably.
        paths = [anchor_path(Path(p), sut_dir) for p in raw]
        return CompiledLabSource(
            label=label, backend="json", repo_dir=sut_dir, paths=paths, kwargs={}
        )
    return CompiledLabSource(
        label=label, backend=spec.backend, repo_dir=sut_dir, paths=[], kwargs=extra
    )


def build_lab_sources(repos: "Sequence[Repo]") -> "LabRepository":
    """Construct the process's host source from every repo's compiled list.

    Concatenates per-repo ``[[lab.sources]]`` lists in the given (OTTO_SUT_DIRS)
    order — later sources override earlier ones inside the composite. Exactly
    one source returns the bare backend (single-source setups run the same
    code they always did); zero sources returns an empty composite whose
    ``load_lab`` fails loud with configuration guidance.

    Raises
    ------
    LabRepositoryError
        If a source names an unregistered backend.
    """
    from .composite import CompositeLabRepository, LabSource
    from .registry import get_lab_repository_class

    entries: "list[LabSource]" = []
    for repo in repos:
        for src in repo.lab_sources:
            cls = get_lab_repository_class(src.backend)
            if src.backend == "json":
                built = cls(search_paths=list(src.paths))
            else:
                built = cls(repo_dir=src.repo_dir, **src.kwargs)
            entries.append(LabSource(label=src.label, repository=built))
    if len(entries) == 1:
        return entries[0].repository
    return CompositeLabRepository(entries)
