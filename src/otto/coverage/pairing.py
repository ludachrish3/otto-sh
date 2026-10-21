"""Pair each fetched ``.gcda`` with its own ``.gcno``, by path.

The rule (user docs): each object file needs a unique path, absolute or
relative to the project root. A fetched ``.gcda`` sits at its path relative to
the product's ``cov_dir`` — the object's coverage path, shortened by
``GCOV_PREFIX_STRIP`` when set — so it is always a component-wise tail of the
object's path. When the object's own notes file exists it is the only
candidate; the project root is searched only when it does not. Matching never
falls back to basenames.
"""

import filecmp
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import CoverageDataMismatchError, PairingError
from .merge.gcovfile import incompatibility
from .objects import ObjectList, Unavailable

logger = logging.getLogger(__name__)

_TMP = ".tmp_"


@dataclass(frozen=True)
class Pair:
    """One fetched ``.gcda`` and the notes file it decodes against."""

    gcda: Path
    rel: PurePosixPath
    gcno: Path


class GcnoIndex:
    """Every ``.gcno`` under *root* by object name — walked once, on first use."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.walks = 0
        self._by_name: dict[str, list[Path]] | None = None

    def candidates(self, stem_name: str) -> list[Path]:
        """List the notes files for an object named *stem_name*.

        Both ``<name>.gcno`` and the kernel's ``.tmp_<name>.gcno`` count.
        """
        if self._by_name is None:
            self.walks += 1
            self._by_name = {}
            for gcno in sorted(self.root.rglob("*.gcno")):
                self._by_name.setdefault(_object_name(gcno), []).append(gcno)
        return self._by_name.get(stem_name, [])


# directory + object name: a bare name never pairs by tail
_MIN_TAIL = 2


def _object_name(gcno: Path) -> str:
    name = gcno.name[: -len(".gcno")]
    return name.removeprefix(_TMP)


def _notes_stem(gcno: Path) -> tuple[str, ...]:
    return (*gcno.parent.parts, _object_name(gcno))


def _ends_with(path: PurePosixPath, tail: PurePosixPath) -> bool:
    return len(tail.parts) <= len(path.parts) and path.parts[-len(tail.parts) :] == tail.parts


def _common_tail(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _direct_notes(target: PurePosixPath) -> Path | None:
    for candidate in (Path(f"{target}.gcno"), Path(target.parent) / f"{_TMP}{target.name}.gcno"):
        if candidate.is_file():
            return candidate
    return None


def _anchored(path: PurePosixPath, root: Path) -> PurePosixPath:
    """Anchor a relative object path at the project *root*, never at the process cwd."""
    return path if path.is_absolute() else PurePosixPath(root.as_posix()) / path


def notes_for_stem(stem: PurePosixPath, index: GcnoIndex) -> Path | None:
    """Return the local notes file for an object stem, or ``None``.

    ``<stem>.gcno`` when present, else the longest-tail match: several
    equally good candidates count as one only when their bytes are identical.
    A tail must span a directory and the object name — a bare name would pair
    any same-named object in the tree.

    A relative stem is project-relative: it is anchored at ``index.root``
    first, never at the process's working directory.
    """
    stem = _anchored(stem, index.root)
    direct = _direct_notes(stem)
    if direct is not None:
        return direct
    candidates = index.candidates(stem.name)
    if not candidates:
        return None
    scored = [(_common_tail(_notes_stem(c), stem.parts), c) for c in candidates]
    best = max(score for score, _ in scored)
    if best < _MIN_TAIL:
        return None
    winners = [c for score, c in scored if score == best]
    if all(filecmp.cmp(winners[0], c, shallow=False) for c in winners[1:]):
        return winners[0]
    return None


def pair_product(
    product_dir: Path, objects: ObjectList | Unavailable, index: GcnoIndex, *, label: str
) -> list[Pair]:
    """Pair every ``.gcda`` under *product_dir*.

    Raises:
        CoverageDataMismatchError: the object's own notes file exists but
            cannot decode its data (rebuilt since the shipped binary, or written
            by another compiler generation).
        PairingError: a collision on the device, several differing build
            trees, or no notes file at all.
    """
    pairs: list[Pair] = []
    unlisted: list[str] = []
    for gcda in sorted(product_dir.rglob("*.gcda")):
        rel = PurePosixPath(gcda.relative_to(product_dir).as_posix())
        stem = PurePosixPath(str(rel)[: -len(".gcda")])
        target = stem
        if isinstance(objects, ObjectList):
            listed = [p for p in objects.paths if _ends_with(p, stem)]
            if len(listed) > 1:
                raise PairingError(
                    "merged-on-device",
                    f"{label}: {gcda} matches several objects ({', '.join(map(str, listed))}): "
                    "the device wrote their counters to one file; lower GCOV_PREFIX_STRIP",
                )
            if listed:
                target = listed[0]
                direct = _direct_notes(_anchored(target, index.root))
                if direct is not None:
                    reason = incompatibility(gcda, direct)
                    if reason is not None:
                        raise CoverageDataMismatchError(f"{label}: {reason}")
                    pairs.append(Pair(gcda=gcda, rel=rel, gcno=direct))
                    continue
            else:
                unlisted.append(str(rel))
        pairs.append(Pair(gcda=gcda, rel=rel, gcno=_search(gcda, target, index, label)))
    if unlisted:
        logger.info(
            "%s: %d .gcda not in the artifact's object list (a loaded library), paired by path: %s",
            label,
            len(unlisted),
            ", ".join(unlisted),
        )
    return pairs


def _search(gcda: Path, target: PurePosixPath, index: GcnoIndex, label: str) -> Path:
    decisions: list[str] = []
    fitting: list[Path] = []
    for gcno in index.candidates(target.name):
        reason = incompatibility(gcda, gcno)
        if reason is None:
            fitting.append(gcno)
        else:
            decisions.append(f"{gcno}: {reason.split(': ', 1)[-1]}")
    if not fitting:
        tried = "; ".join(decisions) or f"no notes file named {target.name}.gcno"
        raise PairingError(
            "no-notes",
            f"{label}: no notes file for {gcda} under {index.root} ({tried}); "
            "keep the build tree that produced the product",
        )
    tails = {g: _common_tail(_notes_stem(g), target.parts) for g in fitting}
    best = max(tails.values())
    top = [g for g in fitting if tails[g] == best]
    decisions += [f"{g}: shorter path match" for g in fitting if tails[g] < best]
    if len(top) == 1 or all(filecmp.cmp(top[0], g, shallow=False) for g in top[1:]):
        if len(top) > 1:
            logger.debug(
                "%s: identical notes for %s, using %s (also %s)",
                label,
                gcda,
                top[0],
                ", ".join(map(str, top[1:])),
            )
        return top[0]
    raise PairingError(
        "several-trees",
        f"{label}: these build trees each hold notes that match {gcda}, and otto cannot tell "
        f"which built it: {', '.join(map(str, top))}; remove the stale tree. "
        f"Also considered: {'; '.join(decisions) or 'nothing else'}",
    )
