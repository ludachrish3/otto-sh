"""The one lab-document reader, the one entry-to-files rule, and the one read of a source.

Every site that reads a lab file reads it through :func:`read_lab_document`:
the runtime loader (``otto.labs.json_repository._JsonFileLabRepository``), the
``otto init`` doctor (``otto.init.doctor``), completion's declared-links read
(``otto.config.completion_cache``) and the Next steps panel
(``otto.init.next_steps.declares_lab``). The loader and the doctor read a
whole source through :func:`read_source`, which threads the in-source duplicate
rules across the source's files. What one of them accepts, every one of them
accepts. Every site that lists a source's lab files lists them through
:func:`expand_lab_files`, under the source's own
:class:`~otto.labs.sources.LabFileRule`; of those, the sites outside a
backend read only the files :func:`otto_decodes`.

Importing this module is light: the parsers load when a function runs.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from typing_extensions import final

if TYPE_CHECKING:
    from .drops import Diagnostic
    from .json_repository import LabDocument
    from .sources import LabFileRule

logger = logging.getLogger(__name__)

_GLOB_CHARS = frozenset("*?[")


@final
@dataclass(frozen=True, kw_only=True)
class LabDocumentSource:
    """One lab file as read: where it came from and its validated sections."""

    origin: Path
    """The file the document was read from, which every error about it names."""

    sections: dict[str, Any]
    """The ``labs``/``elements``/``links`` sections, secrets already wrapped.

    :func:`otto.labs.json_repository.parse_lab_sections`' result over the
    decoded value, after :func:`otto.models.base.wrap_secret_values`.
    """

    provenance: "Mapping[str, Any] | None" = None
    """The ``_otto.provenance`` table of a rendered document; ``None`` for a plain one."""


@final
@dataclass(frozen=True, kw_only=True)
class SourceRead:
    """One source's lab files as read: the documents that parsed and every problem."""

    documents: "list[LabDocument]"
    """The parsed documents, in file order; a file with a problem is left out."""

    problems: "list[Diagnostic]"
    """One per file that could not be read or parsed, in file order.

    :attr:`~otto.labs.drops.Diagnostic.where` is the file and
    :attr:`~otto.labs.drops.Diagnostic.reason` the error, which names the file
    too.
    """


def expand_lab_files(
    paths: Iterable[Path], *, rule: "LabFileRule", visited: "set[Path] | None" = None
) -> list[Path]:
    """Every lab file a list of ``paths`` entries names under *rule* (spec §2.4).

    THE single home of the entry-to-files rule, read by every file-backed
    source, the completion-cache fingerprint and the ``otto init`` doctor: a
    directory contributes its :attr:`~otto.labs.sources.LabFileRule.directory_file`;
    a path ending in the rule's :attr:`~otto.labs.sources.LabFileRule.suffix`
    is the file; an entry with a glob metacharacter is expanded relative to
    its non-glob prefix (sorted, files only, the rule's suffix only). Entries
    that resolve to nothing are skipped — the composite's existence rule
    reports the consequence.

    Each FILE appears once, at its first-seen position, however many entries
    name it: ``paths = [d, d/"elements/*.json"]`` — the main file by directory
    plus the split files by glob, one directory level down — is the documented
    layout written the natural way, and listing ``d/lab.json`` twice would
    trip the in-source duplicate rule
    (:func:`~otto.labs.json_repository.check_in_source_duplicates`) against
    the file itself. Identity is the RESOLVED path, so two spellings of one
    file collapse. A bare ``d/"*.json"`` glob straight over the lab directory
    is a hazard rather than a shortcut: ``inventory.json`` and ``creds.json``
    (spec 2026-09-06 creds-store §8.1) live in that same directory and would
    be swept in as lab files too.

    When *visited* is given, every directory this walk enters is added to it
    (the glob's non-glob root, each match's parent, a bare directory entry, a
    file entry's parent) — so a caller building a stat-only key set can see
    "a file appeared here" by the directory's own mtime instead of re-running
    the glob.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        """Append *candidate* unless some earlier entry already named that file."""
        key = candidate.resolve()
        if key in seen:
            logger.debug(f"lab path {candidate} already contributed by an earlier entry")
            return
        seen.add(key)
        found.append(candidate)

    for entry in paths:
        text = str(entry)
        if any(c in text for c in _GLOB_CHARS):
            parts = entry.parts
            first_glob = next(i for i, p in enumerate(parts) if any(c in p for c in _GLOB_CHARS))
            root = Path(*parts[:first_glob]) if first_glob else Path()
            pattern = str(Path(*parts[first_glob:]))
            if visited is not None:
                visited.add(root)
            matches = sorted(
                p for p in root.glob(pattern) if p.is_file() and p.suffix == rule.suffix
            )
            if not matches:
                logger.debug(f"lab path glob {text!r} matched no {rule.suffix} file")
            for match in matches:
                add(match)
                if visited is not None:
                    visited.add(match.parent)
            continue
        if entry.suffix == rule.suffix:
            candidate = entry
            if visited is not None and candidate.parent.is_dir():
                visited.add(candidate.parent)
        else:
            candidate = entry / rule.directory_file
            if visited is not None and entry.is_dir():
                visited.add(entry)
        if candidate.is_file():
            add(candidate)
    return found


def otto_decodes(path: Path) -> bool:
    """Return whether otto itself decodes *path* as a lab document.

    THE single decision of which lab files :func:`read_lab_document` can read:
    a file with the json backend's suffix
    (:data:`~otto.labs.sources.JSON_FILE_RULE`). A backend with its own
    :class:`~otto.labs.sources.LabFileRule` may list files otto cannot decode
    (a ``.yaml`` file, say); such a file is still fingerprinted, but every
    reader outside its backend (the ``otto init`` doctor, the Next steps panel,
    completion's declared-links read) skips it rather than misread it as
    malformed JSON.
    """
    from .sources import JSON_FILE_RULE

    return path.suffix == JSON_FILE_RULE.suffix


def _open_lab_json(path: Path) -> IO[str]:
    """Open the ``.json`` lab file *path* as text, in the locale's default encoding.

    The encoding is left to the locale, as the loader has always read lab
    files: a strict UTF-8 decode would refuse a locale-encoded file that loads
    today (on Windows, say).
    """
    return path.open()


def read_lab_document(path: Path) -> LabDocumentSource:
    """Read the lab file *path*: decode it, wrap its secrets, validate its sections.

    The secrets are wrapped (:func:`~otto.models.base.wrap_secret_values`)
    before the section contract runs, so no caller ever holds a parsed
    password as a plain string.

    Raises
    ------
    LabRepositoryError
        Naming the file, when it cannot be opened or decoded, holds malformed
        JSON, or breaks the section contract
        (:func:`otto.labs.json_repository.parse_lab_sections`).
    """
    import json

    from ..models.base import wrap_secret_values
    from .errors import LabRepositoryError
    from .json_repository import parse_lab_sections

    try:
        with _open_lab_json(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise LabRepositoryError(f"Lab file '{path}' contains malformed JSON: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise LabRepositoryError(f"Lab file '{path}' could not be read: {e}") from e
    wrap_secret_values(data)
    return LabDocumentSource(origin=path, sections=parse_lab_sections(data, str(path)))


def accepted_documents(read: SourceRead, *, best_effort: bool) -> "list[LabDocument]":
    """Return *read*'s documents, or raise its first problem unless *best_effort*.

    The one rule for a source read's problems, every file-backed source's: a
    load raises the first as a :class:`~otto.labs.errors.LabRepositoryError`;
    a best-effort read (completion paths) skips each problem file, logged at
    debug and recorded as a drop (``otto.labs.drops.record_drop``).
    """
    from .drops import record_drop
    from .errors import LabRepositoryError

    if read.problems and not best_effort:
        raise LabRepositoryError(read.problems[0].reason)
    for problem in read.problems:
        logger.debug(
            f"skipping lab file {problem.where} while enumerating (malformed or duplicating)"
        )
        record_drop(problem.where, problem.reason)
    return read.documents


def read_source(
    paths: Sequence[Path], *, read: "Callable[[Path], LabDocumentSource] | None" = None
) -> SourceRead:
    """Read the lab files of ONE source, *paths*, into documents and problems.

    Each file is read by *read* (``None``: :func:`read_lab_document`, looked up
    when this runs) and parsed by
    :func:`~otto.labs.json_repository.parse_lab_document`, which runs the
    in-source duplicate rules against state threaded across *paths* only: two
    sources may declare the same lab, one source may not. A file that cannot be
    read or parsed is a problem, never raised, and the files after it are still
    read; a rejected file leaves the duplicate state as it was.
    """
    from .drops import Diagnostic
    from .errors import LabRepositoryError
    from .json_repository import SeenElement, parse_lab_document

    reader = read if read is not None else read_lab_document
    documents: list[LabDocument] = []
    problems: list[Diagnostic] = []
    seen_labs: dict[str, Path] = {}
    seen_elements: dict[str, SeenElement] = {}
    for path in paths:
        try:
            source = reader(path)
            document = parse_lab_document(
                source.sections, path, seen_labs=seen_labs, seen_elements=seen_elements
            )
        except LabRepositoryError as e:
            problems.append(Diagnostic(where=str(path), reason=str(e)))
            continue
        documents.append(document)
    return SourceRead(documents=documents, problems=problems)
