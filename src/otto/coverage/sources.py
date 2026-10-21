"""Resolve each source path a capture names to the file in the project.

Each ``SF:`` path is resolved on its own, in this order: a path under the
staging tree (clang records relative names against the ``.gcda``'s directory)
has the staging prefix removed and ``..`` normalised, then is tail-matched;
an absolute path that exists is used as is; an absolute path that does not
exist is tail-matched component-wise against the project's files, the
longest match winning. A path with no match keeps its raw value and is
logged once: a WARNING when project files share its name but none ends with
it, INFO when no project file is even named like it.
"""

import logging
import posixpath
from pathlib import Path

logger = logging.getLogger(__name__)


class SourceResolver:
    """Per-file source resolution for one capture."""

    def __init__(self, project_root: Path, staging: Path | None = None) -> None:
        self.project_root = project_root
        self.staging = staging
        self._by_name: dict[str, list[Path]] | None = None
        self._warned: set[str] = set()

    def resolve(self, raw: str) -> Path | None:
        """Return the project file *raw* names, or ``None`` when none matches."""
        if self.staging is not None and raw.startswith(f"{self.staging}/"):
            return self._tail_match(raw, posixpath.normpath(raw[len(str(self.staging)) + 1 :]))
        if Path(raw).is_absolute() and Path(raw).exists():
            return Path(raw)
        return self._tail_match(raw, raw)

    def _tail_match(self, raw: str, path: str) -> Path | None:
        parts = tuple(p for p in path.split("/") if p not in ("", ".", ".."))
        candidates = self._index().get(parts[-1], []) if parts else []
        top = [c for c in candidates if self._is_suffix(c, parts)]
        if top:
            # Every match is a whole project-relative path ending *parts*, so two
            # matches never share a length: the longest one is unique.
            return max(top, key=lambda c: len(c.relative_to(self.project_root).parts))
        self._warn_unresolved(raw, candidates)
        return None

    def _is_suffix(self, candidate: Path, parts: tuple[str, ...]) -> bool:
        """Report whether the candidate's whole project-relative path ends *parts*."""
        cparts = candidate.relative_to(self.project_root).parts
        return len(cparts) <= len(parts) and parts[len(parts) - len(cparts) :] == cparts

    def _index(self) -> dict[str, list[Path]]:
        if self._by_name is None:
            self._by_name = {}
            for p in self.project_root.rglob("*"):
                if p.is_file() and ".git" not in p.relative_to(self.project_root).parts:
                    self._by_name.setdefault(p.name, []).append(p)
        return self._by_name

    def _warn_unresolved(self, raw: str, candidates: list[Path]) -> None:
        """Log *raw* once: a WARNING when project files share its name, else INFO.

        A path no project file is even named like — a relocated build's
        system header, one per header — is ordinary and stays at INFO; a
        same-named file that does not match may be the one meant.
        """
        if raw not in self._warned:
            self._warned.add(raw)
            found = ", ".join(map(str, candidates)) if candidates else "none"
            logger.log(
                logging.WARNING if candidates else logging.INFO,
                "%s: no source under %s ends with this path (candidates: %s); keeping the raw path",
                raw,
                self.project_root,
                found,
            )


def write_resolved_info(raw_info: Path, resolved_info: Path, resolver: SourceResolver) -> None:
    """Copy *raw_info* to *resolved_info* with each ``SF:`` line resolved."""
    out: list[str] = []
    for line in raw_info.read_text().splitlines(keepends=True):
        if line.startswith("SF:"):
            raw = line[3:].rstrip("\n")
            resolved = resolver.resolve(raw)
            out.append(f"SF:{resolved if resolved is not None else raw}\n")
        else:
            out.append(line)
    resolved_info.write_text("".join(out))
