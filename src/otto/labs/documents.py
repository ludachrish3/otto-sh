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

A ``.jsonnet`` entry is a lab file too, read through its ``jsonnet`` lab
source: :func:`read_lab_document` with its :class:`RenderInputs` renders it
(:func:`render_outcome`, the one place a render's key and import policy are
built) and then reads the rendered document exactly as a hand-written one.

Importing this module is light: the parsers and the templating core load
when a function runs.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import final

if TYPE_CHECKING:
    from ..inventory.config import ProtectedPaths
    from ..inventory.view import InventoryView
    from ..templating.memo import RenderKey, RenderOutcome
    from ..templating.render import RenderResult
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
class RenderInputs:
    """What one ``jsonnet`` lab source renders its entry files with, in one operation."""

    view: "InventoryView"
    """The operation's inventory view.

    It holds the inventories a render reads, its protected-file catalog and
    its render memo (:attr:`~otto.inventory.InventoryView.render_memo`)."""

    inventories: tuple[str, ...]
    """The inventories the source declares, and the only ones a render reads."""

    project: str
    """The declaring project's name, a template's ``otto.project``."""

    project_root: Path
    """The declaring project's root, which is the render memo's scope."""

    import_roots: tuple[Path, ...]
    """The directories an import may resolve under, in search order."""

    entries: tuple[Path, ...]
    """The source's entry files, as its ``paths`` expand: the only files it renders."""


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
            matches = sorted(p for p in root.glob(pattern) if p.is_file() and rule.names_file(p))
            if not matches:
                logger.debug(f"lab path glob {text!r} matched no {rule.suffix} file")
            for match in matches:
                add(match)
                if visited is not None:
                    visited.add(match.parent)
            continue
        if rule.names_file(entry):
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
    """Return whether otto itself decodes *path* as a written lab document.

    THE single decision of which lab files :func:`read_lab_document` can read
    on their own: a file with the json backend's suffix
    (:data:`~otto.labs.sources.JSON_FILE_RULE`). A ``jsonnet`` source's
    entry is not one: :func:`read_lab_document` reads it only through its
    source's render inputs. A backend with its own
    :class:`~otto.labs.sources.LabFileRule` may list files otto cannot decode
    (a ``.yaml`` file, say); such a file, or a Jsonnet entry, is still
    fingerprinted, but every reader outside its backend (the ``otto init``
    doctor, the Next steps panel, completion's declared-links read) skips it
    rather than misread it as malformed JSON.
    """
    from .sources import JSON_FILE_RULE

    return JSON_FILE_RULE.names_file(path)


def render_key(entry: Path, inputs: RenderInputs, *, protected: "ProtectedPaths") -> "RenderKey":
    """Return the memo key *entry* renders under with *inputs*: THE one construction of a lab's.

    Everything the render's outcome depends on: the declaring project (the
    memo's scope), the resolved entry, the import roots, the inventories, the
    project name, the view's protected-file catalog (*protected*, the value
    the caller resolved once from :attr:`~otto.inventory.InventoryView.protected`)
    and the packaged library's digest. :func:`render_outcome` renders under
    it, and a source's validated read is kept under its entries' keys.

    Raises:
        TemplatingError: The packaged library is missing.
    """
    from ..templating.imports import library_digest
    from ..templating.memo import RenderKey

    return RenderKey(
        scope=inputs.project_root,
        entry=entry.resolve(),
        import_roots=inputs.import_roots,
        inventories=inputs.inventories,
        project=inputs.project,
        protected=protected,
        library_digest=library_digest(),
    )


def render_outcome(entry: Path, inputs: RenderInputs) -> "RenderOutcome":
    """Return how the Jsonnet *entry* renders under *inputs*: the operation's one evaluation.

    THE one place a lab render's key and import policy are built, so the
    policy that refuses an import and the key that identifies the render
    always agree. The view's protected-file catalog is resolved once
    (:attr:`~otto.inventory.InventoryView.protected`), and that one value
    goes into both the :class:`~otto.templating.memo.RenderKey` and the
    :class:`~otto.templating.imports.ImportPolicy`. The render runs through
    the view's memo (:meth:`~otto.templating.memo.RenderMemo.get_or_render`),
    so every reader of one operation shares its outcome: it builds the
    template's ``otto`` argument (:func:`~otto.inventory.snapshot.template_inputs`)
    and renders (:func:`~otto.templating.render.render`), and both ask the
    operation's cancellation first, so a render whose operation was given up
    on never reaches the evaluator.

    An *entry* that is not one of :attr:`RenderInputs.entries`, or that is
    a protected file, or a view whose catalog refuses to render
    (:attr:`~otto.inventory.ProtectedPaths.refusal`: a view no builder made,
    or one whose user settings file cannot be read), is refused before
    anything is read: its outcome is that error, and nothing
    is memoized. An :class:`~otto.inventory.errors.InventoryError` raised
    while the ``otto`` argument is built is settled as the key's error,
    naming *entry* and the error's class, so no other reader of the
    operation retries the discovery.
    """
    from ..inventory.errors import InventoryError
    from ..inventory.snapshot import template_inputs
    from ..templating.errors import RenderCancelled, RenderError, TemplatingError
    from ..templating.imports import ImportPolicy
    from ..templating.memo import RenderOutcome
    from ..templating.render import RenderRequest, render
    from .drops import is_timeout

    resolved = entry.resolve()
    if resolved not in {e.resolve() for e in inputs.entries}:
        return RenderOutcome(error=f"{entry}: not an entry file of this lab source's paths")
    protected = inputs.view.protected()
    if protected.contains(resolved):
        return RenderOutcome(error=f"{entry}: a protected file is never rendered")
    try:
        key = render_key(entry, inputs, protected=protected)
        policy = ImportPolicy(roots=inputs.import_roots, protected=protected)
    except TemplatingError as e:  # the catalog refuses to render, or no packaged library
        return RenderOutcome(error=f"{entry}: {e}")

    def run() -> "RenderResult":
        try:
            otto = template_inputs(
                view=inputs.view, names=inputs.inventories, project=inputs.project
            )
        except InventoryError as e:
            # Settled as the key's error (or its timeout), so the operation's
            # other readers never retry a discovery that already failed.
            if is_timeout(e):
                raise RenderCancelled(f"{entry}: {e}") from e
            raise RenderError(f"{entry}: {type(e).__name__}: {e}", origin=entry, line=None) from e
        return render(RenderRequest(entry=entry, tla={"otto": otto}, policy=policy))

    return inputs.view.render_memo.get_or_render(key, run)


def read_lab_document(path: Path, *, render: "RenderInputs | None" = None) -> LabDocumentSource:
    """Read the lab file *path*: decode it, wrap its secrets, validate its sections.

    The secrets are wrapped (:func:`~otto.models.base.wrap_secret_values`)
    before the section contract runs, so no caller ever holds a parsed
    password as a plain string.

    A ``.jsonnet`` *path* is an entry file of a ``jsonnet`` lab source, read
    with that source's *render* inputs: its document is the one
    :func:`render_outcome` renders, then decoded, wrapped and validated as a
    ``.json`` file's is, with the rendered ``_otto.provenance`` table as
    :attr:`LabDocumentSource.provenance`.

    Raises
    ------
    LabRepositoryError
        Naming the file, when it cannot be opened or decoded, holds malformed
        JSON, or breaks the section contract
        (:func:`otto.labs.json_repository.parse_lab_sections`); for a
        ``.jsonnet`` file, when *render* is ``None`` or the render failed.
    RenderCancelled
        For a ``.jsonnet`` file whose render was cancelled
        (:class:`~otto.templating.errors.RenderCancelled`).
    """
    from ..models.base import JsonFileError, read_json_file, wrap_secret_values
    from .errors import LabRepositoryError
    from .json_repository import parse_lab_sections
    from .sources import JSONNET_FILE_RULE

    provenance: Mapping[str, Any] | None = None
    if JSONNET_FILE_RULE.names_file(path):
        if render is None:
            raise LabRepositoryError(
                f"{path}: a Jsonnet entry is read through its jsonnet lab source"
            )
        data = rendered_data(path, render)
        provenance = _provenance(data)
    else:
        try:
            data = read_json_file(path)
        except JsonFileError as e:
            what = "contains malformed JSON" if e.malformed else "could not be read"
            raise LabRepositoryError(f"Lab file '{path}' {what}: {e}") from e.__cause__
    wrap_secret_values(data)
    return LabDocumentSource(
        origin=path, sections=parse_lab_sections(data, str(path)), provenance=provenance
    )


def rendered_data(path: Path, render: RenderInputs) -> object:
    """Return the JSON value the entry *path* renders to, through :func:`render_outcome`.

    A cancelled render is no problem with the file, so it is never a
    :class:`~otto.labs.errors.LabRepositoryError`: it raises the templating
    layer's own :class:`~otto.templating.errors.RenderCancelled`, which
    :func:`read_source` does not collect, and the source that rendered it
    reports the whole read as its timeout.

    Raises:
        LabRepositoryError: Naming *path*, when the render failed.
        RenderCancelled: Naming *path*, when the render was cancelled.
    """
    from ..templating.errors import RenderCancelled, RenderError, without_entry
    from .errors import LabRepositoryError

    outcome = render_outcome(path, render)
    if outcome.timed_out:
        raise RenderCancelled(
            f"Lab file '{path}' was not rendered: {without_entry(outcome.error, path)}"
        )
    if outcome.result is None:
        raise LabRepositoryError(
            f"Lab file '{path}' could not be rendered: {without_entry(outcome.error, path)}"
        )
    try:
        return outcome.result.value()
    except RenderError as e:
        raise LabRepositoryError(
            f"Lab file '{path}' could not be rendered: {without_entry(e, path)}"
        ) from e


def _provenance(data: object) -> "Mapping[str, Any] | None":
    """Return a rendered document's ``_otto.provenance`` table, or ``None`` when it has none."""
    meta = data.get("_otto") if isinstance(data, dict) else None
    provenance = meta.get("provenance") if isinstance(meta, dict) else None
    return provenance if isinstance(provenance, dict) else None


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
    read; a rejected file leaves the duplicate state as it was. A cancelled
    render (:class:`~otto.templating.errors.RenderCancelled`) is no file's
    problem: it propagates, and no file after it is read.
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
                source.sections,
                path,
                seen_labs=seen_labs,
                seen_elements=seen_elements,
                provenance=source.provenance,
            )
        except LabRepositoryError as e:
            problems.append(Diagnostic(where=str(path), reason=str(e)))
            continue
        documents.append(document)
    return SourceRead(documents=documents, problems=problems)
