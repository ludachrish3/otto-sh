"""Coverage directory handling shared by the ``otto test --cov`` / ``otto cov`` paths.

:func:`prepare_destination` is the typer-free two-mode destination gate shared
by ``cov_dir`` and ``cov_report_dir``: create-if-missing, refuse a non-empty
target unless ``overwrite``, and prove writability with a real probe file.
:func:`check_destination` is the same refusal for a dry run, which creates
and clears nothing. The ``[coverage]`` settings lookup (which repo declares
it, its ``hosts`` selector) lives beneath the coverage pipeline, in
:mod:`otto.config.coverage_settings`.
"""

import contextlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Literal

from ..errors import OttoError

DestinationErrorKind = Literal["not_a_directory", "not_writable", "not_empty"]
"""The three ways a destination can be unusable; see :class:`DestinationError`."""


def destination_message(
    kind: DestinationErrorKind, path: Path, *, subject: str, remedy: str, reason: str = ""
) -> str:
    """Return the destination refusal for *kind*, with *subject* and *remedy* already spelled.

    The library passes the field name and ``set <field>=True``; the CLI passes
    the flag and ``pass --flag``. One template, two spellings, no text
    rewriting.
    """
    if kind == "not_empty":
        return f"{subject} target {path} is not empty; {remedy} to clear it."
    if kind == "not_writable":
        return f"{subject} target {path} cannot be written: {reason}."
    return f"{subject} target {path} is not a directory."


class DestinationError(OttoError, ValueError):
    """A run's destination directory cannot receive its files.

    ``field`` names the option the path came from (``cov_dir``,
    ``output_dir``), ``remedy_field`` the option that would allow clearing
    it, and ``kind`` is one of ``not_a_directory``, ``not_writable`` or
    ``not_empty``. The message speaks in those field names; the CLI spells
    them as flags (:func:`otto.cli.invoke.usage_error_from`). ``reason`` (the
    ``not_writable`` OS-level detail, otherwise empty) is stored too — a
    caller that catches this from a lower-level field name (say,
    ``run_coverage_report``'s ``output_dir``/``overwrite``) and wants to
    re-raise it in ITS OWN caller-facing field names (say, ``cov_report_dir``/
    ``overwrite_cov_report_dir``) needs every constructor argument back,
    ``reason`` included.
    """

    def __init__(
        self,
        path: Path,
        *,
        field: str,
        remedy_field: str,
        kind: DestinationErrorKind,
        reason: str = "",
    ) -> None:
        self.path = path
        self.field = field
        self.remedy_field = remedy_field
        self.kind = kind
        self.reason = reason
        text = destination_message(
            kind, path, subject=field, remedy=f"set {remedy_field}=True", reason=reason
        )
        super().__init__(text)


def check_destination(path: Path, *, overwrite: bool, field: str, remedy_field: str) -> None:
    """Refuse *path* as :func:`prepare_destination` would, touching nothing.

    Best effort by design: an existing directory must be empty (or
    *overwrite* set) and writable and searchable by this user; a missing
    one's nearest existing ancestor must itself be a directory, writable and
    searchable. A dangling symlink at *path* counts as existing (it is not a
    directory either, so it is refused the same way). ``os.access`` can be
    wrong on NFS or under ACLs, and the walk up to the nearest ancestor can
    itself raise ``PermissionError`` through an unsearchable ancestor of
    *that* — both surface as :class:`DestinationError`, never a raw
    ``OSError``; the real run's probe (:func:`prepare_destination`) is the
    authority.
    """
    try:
        if path.is_symlink() or path.exists():
            if not path.is_dir():
                raise DestinationError(
                    path, field=field, remedy_field=remedy_field, kind="not_a_directory"
                )
            if not overwrite and any(path.iterdir()):
                raise DestinationError(
                    path, field=field, remedy_field=remedy_field, kind="not_empty"
                )
            probe = path
        else:
            probe = path.parent
            while not probe.exists():
                probe = probe.parent
            if not probe.is_dir():
                raise DestinationError(
                    path, field=field, remedy_field=remedy_field, kind="not_a_directory"
                )
        if not os.access(probe, os.W_OK | os.X_OK):
            raise DestinationError(
                path,
                field=field,
                remedy_field=remedy_field,
                kind="not_writable",
                reason="Permission denied",
            )
    except OSError as e:
        raise DestinationError(
            path,
            field=field,
            remedy_field=remedy_field,
            kind="not_writable",
            reason=e.strerror or str(e),
        ) from e


def prepare_destination(path: Path, *, overwrite: bool, field: str, remedy_field: str) -> None:
    """Make *path* an empty, writable directory, or raise :class:`DestinationError`.

    Creates the directory (and its parents), refuses a non-empty one unless
    *overwrite* (then clears it: files and symlinks are unlinked, real
    subdirectories removed), and proves writability by creating and
    removing a probe file, the only check that is honest on NFS and under
    ACLs. Every step — the existence/emptiness pre-checks included — runs
    under one ``OSError`` guard, so a permission error anywhere along the
    way (an unreadable existing directory, an unsearchable ancestor) becomes
    :class:`DestinationError` rather than a raw ``OSError`` escaping this
    library function.
    """
    try:
        if path.is_symlink() or path.exists():
            if not path.is_dir():
                raise DestinationError(
                    path, field=field, remedy_field=remedy_field, kind="not_a_directory"
                )
            if not overwrite and any(path.iterdir()):
                raise DestinationError(
                    path, field=field, remedy_field=remedy_field, kind="not_empty"
                )
        path.mkdir(parents=True, exist_ok=True)
        if overwrite:
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        probe = path / f".otto-write-probe-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"")
        finally:
            # Best effort: the write already proved writability, so a stray
            # unlink failure (a race, an odd ACL) must not be reported as
            # "not writable" — but it also must not leave the probe behind
            # to make a later run see a non-empty directory.
            with contextlib.suppress(OSError):
                probe.unlink()
    except OSError as e:
        raise DestinationError(
            path,
            field=field,
            remedy_field=remedy_field,
            kind="not_writable",
            reason=e.strerror or str(e),
        ) from e
