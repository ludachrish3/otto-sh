"""The hermetic import callback every Jsonnet render goes through.

One callback serves ``import``, ``importstr`` and ``importbin`` for both
consumers, the ``jsonnet`` lab source and the ``.jsonnet`` inventory. For
``import 'rel'`` written in a file in directory *d* it decides, in order:

1. the reserved name ``otto.libsonnet`` is always served, from otto's package
   data, whatever the roots hold;
2. an absolute *rel* is refused;
3. the candidates are *d*/*rel* when *d* is under an import root, then
   *root*/*rel* for each root in order, each resolved with its symlinks
   collapsed;
4. a candidate outside every root, or a protected file
   (:meth:`~otto.inventory.config.ProtectedPaths.contains`), is refused before
   anything opens it;
5. the first candidate that is a regular file is opened, checked again
   through its descriptor (still the file the checks saw, and not a protected
   one), and its bytes served unchanged; a candidate that cannot be statted
   is refused as unreadable; with none, the import is not found.

What is guaranteed, and what is assumed. A protected file is never served:
the last check is bound to the open descriptor (its device and inode), so no
rename, link or swap between the checks and the read can slip one through.
The root boundary is checked on the resolved path before the open, so it
assumes no concurrent writer inside an import root: one that swaps an
intermediate directory for a symlink between the checks and the open can
still have a file outside the roots served.

A file the reserved library imports may only be another reserved name: the
library never reaches the user's roots.

The entry file's own directory is not a root, so a file beside the entry is
never importable unless a root holds it. ``JSONNET_PATH`` and every ambient
search path are never consulted: the callback replaces the binding's own
importer, and it reads no environment.
"""

import errno
import functools
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from typing_extensions import final

from ..utils import is_under, resolved_path
from .errors import RenderError, TemplatingError

if TYPE_CHECKING:
    from importlib.abc import Traversable  # importlib.resources.abc from 3.11 on

    from ..config.corpus_snapshot import Unreadable
    from ..inventory.config import ProtectedPaths

LIBRARY_NAME = "otto.libsonnet"
"""The reserved import name; it always resolves to otto's packaged library."""

RESERVED_PREFIX = "<otto>/"
"""The directory a reserved file is reported under, in errors and to its own imports."""

REFUSED_PREFIX = "<refused>/"
"""The directory a refused import's stand-in document is reported under."""


def _library_source() -> "Traversable":
    """Return where otto's packaged ``otto.libsonnet`` lives: THE one spelling of its place."""
    return resources.files("otto.templating").joinpath(LIBRARY_NAME)


def library_file() -> "Path | None":
    """Return the packaged ``otto.libsonnet`` as a file on disk, or ``None`` when it is not one.

    A key path for a stat-based freshness check (the completion cache's), so
    an in-place otto upgrade that changes the library moves the key. ``None``
    for an install whose package data is not a plain file (a zipped install);
    the render memo and the inventory fingerprints hold
    :func:`library_digest`, which covers every install.
    """
    source = _library_source()
    return source if isinstance(source, Path) else None


@functools.cache
def reserved_library() -> Mapping[str, bytes]:
    """Return the reserved imports, ``{"otto.libsonnet": <bytes>}``, read once per process.

    The bytes are otto's package data, read through :mod:`importlib.resources`.
    Raises :class:`~otto.templating.errors.TemplatingError` naming the file
    when the installed otto lacks it (a broken install); that failure is not
    memoized.
    """
    source = _library_source()
    try:
        data = source.read_bytes()
    except OSError as e:
        raise TemplatingError(
            f"otto's Jsonnet library {LIBRARY_NAME} is missing from the otto.templating "
            f"package data ({e}); reinstall otto"
        ) from e
    return MappingProxyType({LIBRARY_NAME: data})


@final
@dataclass(frozen=True, kw_only=True)
class ImportPolicy:
    """What one render may import: its roots, the files it may never read, the reserved files.

    No policy exists without a protected-file catalog: built under a catalog
    that refuses to render (its
    :attr:`~otto.inventory.ProtectedPaths.refusal` is set:
    :data:`~otto.inventory.config.NO_CATALOG`, which a hand-built view or an
    env-less inventory carries, or the catalog of a view whose user settings
    file cannot be read), it raises
    :class:`~otto.templating.errors.TemplatingError` with that refusal, so
    nothing is rendered unprotected and nothing reaches the evaluator.

    Raises:
        TemplatingError: *protected* refuses to render; or *reserved* is
            left to its default and otto's library is missing
            (:func:`reserved_library`).
    """

    roots: tuple[Path, ...]
    """The import roots, searched in order."""
    protected: "ProtectedPaths"
    """The files no import may read, even inside a root."""
    reserved: Mapping[str, bytes] = field(default_factory=reserved_library)
    """Reserved import names and their bytes, always :func:`reserved_library` unless a test says.

    A render never passes it, so the reserved library is served on every
    render without each caller remembering it."""

    def __post_init__(self) -> None:
        refusal = self.protected.refusal
        if refusal is not None:
            raise TemplatingError(refusal)


@dataclass
class ImportTrace:
    """What one render's callback did; mutable, and private to that render."""

    read: list[Path] = field(default_factory=list)
    """Every file the callback served from disk, resolved, in the order it read them."""
    failure: "str | None" = None
    """The first import the callback refused or could not find, as its message.

    Set means the render failed, whatever the evaluation returned."""


def library_digest() -> str:
    """Return the sha256 of the packaged ``otto.libsonnet``, the library's freshness.

    A render's memo and fingerprints hold this digest rather than any version
    number, so a changed library moves them with no constant to bump.
    """
    return hashlib.sha256(reserved_library()[LIBRARY_NAME]).hexdigest()


def make_callback(
    policy: ImportPolicy, trace: ImportTrace
) -> Callable[[str, str], tuple[str, bytes]]:
    """Return the binding's ``import_callback(dir, rel)`` for one render under *policy*.

    It returns ``(found_here, bytes)`` and records every file it serves in
    *trace*'s ``read``. It never raises into the binding: gojsonnet's
    callback-failure path copies the exception's text without its NUL
    terminator and reads it as a C string, so the error it reports is
    corrupt or empty. A refused or unfound import is instead recorded in
    *trace*'s ``failure`` and served as a document under
    :data:`REFUSED_PREFIX` whose evaluation fails with otto's message;
    :func:`~otto.templating.render.render` fails the render on any recorded
    failure, so an ``importstr`` or ``importbin``, which never evaluate what
    they import, fails it too.
    """
    roots = tuple(r for r in (resolved_path(root) for root in policy.roots) if r is not None)

    def _serve(directory: str, rel: str) -> tuple[str, bytes]:
        try:
            return _serve_import(policy, roots, trace, directory, rel)
        except RenderError as e:
            failure = str(e)
        except Exception as e:  # noqa: BLE001 — nothing may raise into the binding (docstring)
            failure = f"import {rel!r} from {Path(directory)}: failed ({e!r})"
        if trace.failure is None:
            trace.failure = failure
        return f"{REFUSED_PREFIX}{rel}", f"error {json.dumps(failure)}".encode()

    return _serve


def _serve_import(
    policy: ImportPolicy, roots: tuple[Path, ...], trace: ImportTrace, directory: str, rel: str
) -> tuple[str, bytes]:
    """Resolve ``import rel`` written in *directory*; see the module docstring for the order."""
    if rel in policy.reserved:
        return f"{RESERVED_PREFIX}{rel}", policy.reserved[rel]
    importer = Path(directory)

    def failed(outcome: str) -> RenderError:
        return RenderError(f"import {rel!r} from {importer}: {outcome}", origin=importer, line=None)

    if directory.startswith((RESERVED_PREFIX, REFUSED_PREFIX)):
        raise failed("refused (the reserved library imports only reserved names)")
    if Path(rel).is_absolute():
        raise failed("refused (an absolute import path)")
    candidates: list[Path] = []
    here = resolved_path(importer)
    if here is not None and any(is_under(here, root) for root in roots):
        candidates.append(here / rel)
    candidates.extend(root / rel for root in roots)
    for candidate in candidates:
        resolved = resolved_path(candidate)
        if resolved is None:
            raise failed("refused (it cannot be resolved)")
        if not any(is_under(resolved, root) for root in roots):
            raise failed("refused (outside the import roots)")
        if policy.protected.contains(resolved):
            raise failed("refused (a protected file)")
        checked = _stat_candidate(resolved)
        if checked is None:
            continue  # absent: the next candidate
        if not isinstance(checked, os.stat_result):
            raise failed(f"unreadable ({checked.error})")
        if not stat.S_ISREG(checked.st_mode):
            continue  # a directory or another non-file: the next candidate
        data = _read_checked(resolved, checked, policy.protected, failed)
        trace.read.append(resolved)
        return str(resolved), data
    searched = ", ".join(str(root) for root in roots) or "none configured"
    raise failed(f"not found under the import roots ({searched})")


def _read_checked(
    path: Path,
    checked: os.stat_result,
    protected: "ProtectedPaths",
    failed: Callable[[str], RenderError],
) -> bytes:
    """Read *path* through one descriptor, refusing it unless it is still the file *checked*.

    The descriptor's ``fstat`` must name the same regular file the checks
    just statted (:func:`_still_the_checked_file`), and not a protected one,
    so a file swapped for a symlink, a hardlink to a protected file or a FIFO
    between the check and the open is refused. Where the platform has
    ``O_NOFOLLOW`` the open also refuses a final component that has become a
    symlink, and ``O_NONBLOCK`` keeps a file swapped for a FIFO from blocking
    the open.
    """
    try:
        fd = _open_candidate(path)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise failed("refused (it changed after it was checked)") from e
        raise failed(f"unreadable ({e})") from e
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not _still_the_checked_file(opened, checked):
            raise failed("refused (it changed after it was checked)")
        if protected.contains_stat(opened):
            raise failed("refused (a protected file)")
        try:
            return handle.read()
        except OSError as e:
            raise failed(f"unreadable ({e})") from e


def _still_the_checked_file(opened: os.stat_result, checked: os.stat_result) -> bool:
    """Return whether the open file is the regular file the checks statted.

    The same device and inode (``os.path.samestat``), and the same change
    time: a filesystem may hand a just-unlinked file's inode number to the
    file created in its place, which then matches on device and inode alone.
    """
    return (
        stat.S_ISREG(opened.st_mode)
        and os.path.samestat(opened, checked)
        and opened.st_ctime_ns == checked.st_ctime_ns
    )


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


def _stat_candidate(path: Path) -> "os.stat_result | Unreadable | None":
    """Stat *path* through ``classify``, the one stat classification; never memoized.

    The seam a test patches to change the file between the checks and the stat.
    """
    from ..config.corpus_snapshot import classify  # function-local, per tach.toml

    return classify(path)


def _open_candidate(path: Path) -> int:
    """Open *path* for reading, never following a symlink in its final component.

    The seam a test patches to change the file between the check and the open.
    """
    return os.open(path, _OPEN_FLAGS)
