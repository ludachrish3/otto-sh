"""A product's object list: every object built into it, read from its local artifact.

An instrumented object embeds its own coverage path in the binary — the
absolute path of its coverage stem plus ``.gcda``, the file libgcov writes at
exit and the path ``otto_kmodcov`` dumps a module to. Scanning the artifact's
bytes for those NUL-terminated strings yields the full path of every object
the product was built from, which is what lets a fetched ``.gcda`` find its
own notes without guessing. An ELF executable's ``DT_NEEDED`` libraries are
followed when exactly one file under the project root carries the soname;
``dlopen`` plugins are invisible and belong in a directory or archive artifact.

The artifact may be a single file, a directory (every regular file under it),
a tar or zip archive (member by member), a single compressed file (``.gz``,
``.xz``, ``.bz2``, ``.zst`` — decompressed, never scanned raw) or a
``docker save`` tarball (the image's final filesystem, layers and whiteouts
applied). The archive forms are exactly :data:`~otto.host.product.ARCHIVE_SUFFIXES`.
A ``docker_image`` named by reference has no local artifact: its list is read
from its running container on the lab host instead (:meth:`ObjectListCache.read_running`).
"""

import bz2
import gzip
import json
import logging
import lzma
import os
import re
import secrets
import shlex
import struct
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from ..host.product import ARCHIVE_SUFFIXES
from ..host.shell_quote import path_operand, shell_operand

if TYPE_CHECKING:
    from ..host.host import Host
    from ..host.product import Product
    from ..result import Result

logger = logging.getLogger(__name__)

_STEM_RE = re.compile(rb"(?<=\x00)([\x21-\x7e][\x20-\x7e]{0,4095}?)\.gcda(?=\x00)")
_ELF_MAGIC = b"\x7fELF"
_ZIP = ".zip"
_WHITEOUT = ".wh."
_OPAQUE = ".wh..wh..opq"
_ELF_HEADER_SIZE = 64  # the 64-bit header; a 32-bit one is shorter
_ELFCLASS32 = 1
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_ELFDATA2MSB = 2
_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NEEDED = 1
_DT_STRTAB = 5


@dataclass(frozen=True)
class ObjectList:
    """Every object built into one product, as absolute coverage stems."""

    product: str
    paths: list[PurePosixPath]
    source: str


UnavailableKind = Literal[
    "no-artifact",
    "missing",
    "unreadable",
    "not-instrumented",
    "reference",
    "no-definition",
    "not-running",
    "dry-run",
]
"""The cases :class:`Unavailable` names, one per reason a product has no object list."""


@dataclass(frozen=True)
class Unavailable:
    """Why a product has no object list (pairing then searches by path).

    ``kind`` names the case for a caller that must react to it: ``"no-artifact"``
    (none declared), ``"missing"`` (declared, not on disk), ``"unreadable"`` (a
    corrupt or unsupported artifact, a failed container scan or copy),
    ``"not-instrumented"`` (read, but it holds no coverage paths),
    ``"reference"`` (a ``docker_image`` named by reference, not a local
    tarball), ``"no-definition"`` (the active lab does not define the product),
    ``"not-running"`` (its container is not running) or ``"dry-run"``.
    """

    product: str
    reason: str
    kind: UnavailableKind


class _UnreadableError(Exception):
    """An artifact this module cannot read; the message is the reason."""


@dataclass(frozen=True)
class ElfIdent:
    """An ELF image's class and byte order, from its identification bytes."""

    is64: bool
    """A 64-bit image (``EI_CLASS``); otherwise 32-bit."""
    big_endian: bool
    """The target's byte order (``EI_DATA``), which every word in the image uses."""

    @property
    def pointer_bits(self) -> int:
        """The target's pointer width, 32 or 64."""
        return 64 if self.is64 else 32

    @property
    def order(self) -> str:
        """The :mod:`struct` byte-order prefix the image's words use."""
        return ">" if self.big_endian else "<"


def elf_ident(data: bytes) -> ElfIdent | None:
    """Return an ELF image's class and byte order, or ``None`` when *data* is no ELF.

    An image whose class or data byte is neither of the two defined values is
    no ELF either: guessing a width or an order would misread every word.
    """
    if not data.startswith(_ELF_MAGIC) or len(data) < _ELF_HEADER_SIZE:
        return None
    ei_class, ei_data = data[4], data[5]
    if ei_class not in {_ELFCLASS32, _ELFCLASS64} or ei_data not in {_ELFDATA2LSB, _ELFDATA2MSB}:
        return None
    return ElfIdent(is64=ei_class == _ELFCLASS64, big_endian=ei_data == _ELFDATA2MSB)


def coverage_stems(data: bytes) -> list[str]:
    """Return the distinct coverage stems embedded in *data*, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _STEM_RE.finditer(b"\x00" + data):
        stem = match.group(1).decode("ascii", "replace")
        if stem and not stem.endswith("/"):
            seen.setdefault(stem, None)
    return list(seen)


def needed_sonames(data: bytes) -> list[str]:
    """Return the ``DT_NEEDED`` sonames of an ELF image, or ``[]`` when it has none."""
    ident = elf_ident(data)
    if ident is None:
        return []
    is64, end = ident.is64, ident.order
    if is64:
        (phoff,) = struct.unpack_from(end + "Q", data, 32)
        phentsize, phnum = struct.unpack_from(end + "HH", data, 54)
        ph_fmt, dyn_fmt = end + "IIQQQQQQ", end + "qQ"
    else:
        (phoff,) = struct.unpack_from(end + "I", data, 28)
        phentsize, phnum = struct.unpack_from(end + "HH", data, 42)
        ph_fmt, dyn_fmt = end + "IIIIIIII", end + "iI"
    loads: list[list[int]] = []  # [vaddr, offset, filesz]
    dynamic: list[int] | None = None  # [offset, filesz]
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + struct.calcsize(ph_fmt) > len(data):
            return []
        fields = struct.unpack_from(ph_fmt, data, off)
        if is64:
            p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = fields[:6]
        else:
            p_type, p_offset, p_vaddr, _paddr, p_filesz = fields[:5]
        if p_type == _PT_LOAD:
            loads.append([p_vaddr, p_offset, p_filesz])
        elif p_type == _PT_DYNAMIC:
            dynamic = [p_offset, p_filesz]
    if dynamic is None:
        return []
    entry = struct.calcsize(dyn_fmt)
    needed: list[int] = []
    strtab_vaddr: int | None = None
    for pos in range(dynamic[0], min(dynamic[0] + dynamic[1], len(data) - entry + 1), entry):
        tag, val = struct.unpack_from(dyn_fmt, data, pos)
        if tag == 0:
            break
        if tag == _DT_NEEDED:
            needed.append(val)
        elif tag == _DT_STRTAB:
            strtab_vaddr = int(val)
    if strtab_vaddr is None:
        return []
    strtab = next(
        (off + strtab_vaddr - va for va, off, sz in loads if va <= strtab_vaddr < va + sz), None
    )
    if strtab is None:
        return []
    names = []
    for idx in needed:
        start = strtab + idx
        if not 0 <= start < len(data):
            continue
        stop = data.find(b"\x00", start)
        if stop > start:
            names.append(data[start:stop].decode("ascii", "replace"))
    return names


class ObjectListCache:
    """Object lists for one report or collection; every artifact is read at most once.

    Each file it reads — a single artifact, a directory member, a needed
    library, an archive, a compressed file or an image tarball — is keyed by
    its resolved path, size and ``mtime_ns``, so a rebuild between two caches
    is seen and a second read within one cache is free.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.files_read = 0
        """How many artifact files this cache has actually read (cache misses)."""
        self._stems: dict[tuple[str, str, int, int], list[str]] = {}
        self._sonames: dict[str, list[Path]] | None = None
        self._images: dict[str, list[str]] = {}
        """Stems per running image id: a second product on one image scans nothing."""

    def read(self, product: "Product") -> ObjectList | Unavailable:
        """Return *product*'s object list, or why it has none (logged at INFO)."""
        artifact = getattr(product, "artifact", None)
        if artifact is None:
            return self._unavailable(
                product, "the product declares no local artifact", kind="no-artifact"
            )
        is_tarball = getattr(product, "is_tarball", None)
        if is_tarball is False:
            return self._unavailable(
                product, f"image {artifact} is a reference, not a local tarball", kind="reference"
            )
        path = Path(artifact)
        if not path.exists():
            return self._unavailable(product, f"artifact {path} does not exist", kind="missing")
        try:
            stems = self._read_artifact(path, docker=is_tarball is True)
        except (
            _UnreadableError,
            OSError,
            EOFError,
            lzma.LZMAError,
            tarfile.TarError,
            zipfile.BadZipFile,
            zlib.error,
            ValueError,
            KeyError,
            struct.error,
        ) as e:
            return self._unavailable(
                product, f"artifact {path} could not be read: {e}", kind="unreadable"
            )
        if not stems:
            return self._unavailable(
                product,
                f"artifact {path} holds no coverage paths (not instrumented)",
                kind="not-instrumented",
            )
        return ObjectList(
            product=product.name, paths=[PurePosixPath(s) for s in stems], source=str(path)
        )

    async def read_running(self, product: "Product", host: "Host") -> ObjectList | Unavailable:
        """Return a reference-form image's object list, read from its running container.

        Every command runs on *host*, the daemon host the product is installed
        on. The container's image is scanned once per cache: every regular file
        under its root that holds a coverage path is copied out and read, so the
        scan itself lists every instrumented file and no ``DT_NEEDED`` library is
        followed. Files on volumes and mounted builds are included; the virtual
        and runtime directories (``/proc``, ``/sys``, ``/dev``, ``/tmp``, ``/run``,
        ``/var/run``) and hidden top-level entries (``/.name``) are not.
        """
        from ..utils import Status

        container = str(getattr(product, "container_name", "") or product.name)
        state = await host.exec(_INSPECT_COMMAND + shlex.quote(container), timeout=60)
        if state.status is Status.NotRun:
            return self._unavailable(product, _DRY_RUN, kind="dry-run")
        if not state.status.is_ok and not _no_such_container(state):
            # the daemon is down, its socket refused us, or docker is missing
            return self._unavailable(
                product,
                f"could not inspect container {container} on {host.id}: {_said(state)}",
                kind="unreadable",
            )
        said = str(state.value).strip() if state.status.is_ok else ""
        running, _, image = said.partition(" ")
        if running != "true" or not image.strip():
            return self._unavailable(
                product,
                f"container {container} is not running on {host.id}",
                kind="not-running",
            )
        image = image.strip()
        # Keyed by image, not container: the image's own files are the object
        # list, so every container of one image shares it (a bind mount or
        # volume that supplies other binaries does not change it).
        stems = self._images.get(image)
        if stems is None:
            got = await self._scan_container(product, host, container)
            if isinstance(got, Unavailable):
                return got
            stems = self._images[image] = got
        if not stems:
            return self._unavailable(
                product,
                f"container {container} holds no coverage paths (not instrumented)",
                kind="not-instrumented",
            )
        return ObjectList(
            product=product.name,
            paths=[PurePosixPath(s) for s in stems],
            source=f"{host.id}:{container}",
        )

    async def _scan_container(
        self, product: "Product", host: "Host", container: str
    ) -> list[str] | Unavailable:
        """Return the stems of every file in *container* that holds a coverage path."""
        from ..utils import Status

        token = _scan_token()
        finished = False
        try:
            scan = await host.exec(
                f"docker exec -e OTTO_SCAN_ID={token} {shlex.quote(container)} "
                f"sh -c {shlex.quote(_SCAN_SCRIPT)}",
                timeout=_SCAN_TIMEOUT,
            )
            parsed = _parse_scan(str(scan.value))
            finished = scan.status is Status.NotRun or (scan.status.is_ok and not parsed.errors)
        finally:
            if not finished:
                # A host-side timeout, a cancellation or a lost connection leaves the
                # container's processes running (docker exec does not stop them), and a
                # scan that ended early may have left a grep behind: stop them all.
                await self._reap(product, host, container, token)
        if scan.status is Status.NotRun:
            return self._unavailable(product, _DRY_RUN, kind="dry-run")
        if not scan.status.is_ok:
            return self._unavailable(
                product,
                f"could not scan container {container}: {_said(scan)}",
                kind="unreadable",
            )
        if parsed.errors:
            # A partial scan may have missed an instrumented file: it is no object list.
            where = f" under {', '.join(parsed.failed_dirs)}" if parsed.failed_dirs else ""
            return self._unavailable(
                product,
                f"could not scan container {container}: the scan exited "
                f"{max(parsed.errors)}{where} (an unreadable file or directory, no find "
                f"or grep in the image, or the scan outran its {_SCAN_LIMIT}s limit)",
                kind="unreadable",
            )
        if not parsed.paths:
            return []
        return await self._fetch(product, host, container, parsed.paths)

    async def _reap(self, product: "Product", host: "Host", container: str, token: str) -> None:
        """Kill every process the scan carrying *token* left in *container*; best effort."""
        from ..host.connections import teardown_step

        with teardown_step(product.name, f"stop the scan in container {container} on {host.id}"):
            reaped = await host.exec(
                f"docker exec {shlex.quote(container)} "
                f"sh -c {shlex.quote(_REAP_SCRIPT)} sh {token}",
                timeout=60,
            )
            if not reaped.status.is_ok:
                logger.warning(
                    "%s: stop the scan in container %s on %s teardown failed: %s",
                    product.name,
                    container,
                    host.id,
                    _said(reaped),
                )

    async def _fetch(
        self, product: "Product", host: "Host", container: str, paths: list[str]
    ) -> list[str] | Unavailable:
        """Copy *paths* out of *container* through a host temp dir; return their stems."""
        from ..host.connections import teardown_step
        from ..utils import Status

        # The name is chosen here, so a creation whose result never arrives (a dropped
        # connection, a cancellation) still names the directory the cleanup removes.
        token = _scan_token()
        may_exist = True
        try:
            made = await host.exec(_make_temp_dir_command(token), timeout=60)
            if made.status is Status.NotRun:
                may_exist = False
                return self._unavailable(product, _DRY_RUN, kind="dry-run")
            hostdir = str(made.value).strip() if made.status.is_ok else ""
            if not hostdir:
                return self._unavailable(
                    product,
                    f"could not make a temp dir on {host.id}: {_said(made)}",
                    kind="unreadable",
                )
            return await self._copy_out(product, host, container, paths, hostdir)
        finally:
            if may_exist:
                where = f"{_TEMP_DIR_EXPR}/{_TEMP_DIR_PREFIX}{token}"
                with teardown_step(product.name, f"remove {where} on {host.id}"):
                    removed = await host.exec(f'rm -rf "{where}"', timeout=60)
                    if not removed.status.is_ok and removed.status is not Status.NotRun:
                        logger.warning(
                            "%s: remove %s on %s teardown failed: %s",
                            product.name,
                            where,
                            host.id,
                            _said(removed),
                        )

    async def _copy_out(
        self, product: "Product", host: "Host", container: str, paths: list[str], hostdir: str
    ) -> list[str] | Unavailable:
        from ..utils import Status

        staged: list[Path] = []
        for n, path in enumerate(paths):
            dest = f"{hostdir}/{n}"
            copied = await host.exec(
                f"docker cp {shell_operand(f'{container}:{path}')} {path_operand(dest)}",
                timeout=_COPY_TIMEOUT,
            )
            if copied.status is Status.NotRun:
                return self._unavailable(product, _DRY_RUN, kind="dry-run")
            if not copied.status.is_ok:
                return self._unavailable(
                    product,
                    f"could not copy {path} out of container {container}: {_said(copied)}",
                    kind="unreadable",
                )
            staged.append(Path(dest))
        with tempfile.TemporaryDirectory(prefix="otto-objlist-") as local:
            got = await host.get(staged, Path(local), show_progress=False)
            if got.status is Status.NotRun:
                return self._unavailable(product, _DRY_RUN, kind="dry-run")
            per_file = got.value if isinstance(got.value, dict) else {}
            for src, path in zip(staged, paths, strict=True):
                one = per_file.get(src)
                if not got.status.is_ok or (one is not None and not one.status.is_ok):
                    why = _said(one) if one is not None else _said(got)
                    return self._unavailable(
                        product,
                        f"could not fetch {path} (container {container}) from {host.id}: {why}",
                        kind="unreadable",
                    )
            groups: list[list[str]] = []
            for src, path in zip(staged, paths, strict=True):
                fetched = Path(local) / src.name
                if not fetched.is_file():
                    return self._unavailable(
                        product,
                        f"could not fetch {path} (container {container}) from {host.id}: "
                        "the transfer reported it fetched but delivered no file",
                        kind="unreadable",
                    )
                # one file in memory at a time: read, scan, drop
                groups.append(coverage_stems(fetched.read_bytes()))
                fetched.unlink()
                self.files_read += 1
            return _dedupe(groups)

    def _unavailable(
        self, product: "Product", reason: str, *, kind: UnavailableKind
    ) -> Unavailable:
        logger.info("%s: object list unavailable: %s", product.name, reason)
        return Unavailable(product=product.name, reason=reason, kind=kind)

    def _cached(self, reader: str, path: Path, compute: Callable[[Path], list[str]]) -> list[str]:
        """Return *compute(path)*, reading *path* at most once per (path, size, mtime_ns)."""
        stat = path.stat()
        key = (reader, str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        if key not in self._stems:
            self.files_read += 1
            self._stems[key] = []  # guards a library cycle
            try:
                self._stems[key] = compute(path)
            except BaseException:
                del self._stems[key]
                raise
        return self._stems[key]

    def _read_artifact(self, path: Path, *, docker: bool) -> list[str]:
        if docker:
            return self._cached("docker", path, _docker_stems)
        if path.is_dir():
            members = (p for p in sorted(path.rglob("*")) if p.is_file())
            return _dedupe(self._cached("file", p, self._file) for p in members)
        suffix = _archive_suffix(path.name)
        if suffix is None:
            return self._cached("file", path, self._file)
        if suffix == _ZIP:
            return self._cached("zip", path, _zip_stems)
        opener = _DECOMPRESSORS.get(suffix)
        if opener is not None:
            return self._cached("compressed", path, lambda p: self._image(opener(p)))
        return self._cached("tar", path, _tar_stems)

    def _file(self, path: Path) -> list[str]:
        return self._image(path.read_bytes())

    def _image(self, data: bytes) -> list[str]:
        """Return *data*'s stems plus those of every needed library found under the root."""
        groups = [coverage_stems(data)]
        for soname in needed_sonames(data):
            lib = self._library(soname)
            if lib is not None:
                groups.append(self._cached("file", lib, self._file))
        return _dedupe(groups)

    def _library(self, soname: str) -> Path | None:
        if self._sonames is None:
            self._sonames = {}
            for dirpath, dirnames, filenames in os.walk(self.project_root):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for filename in filenames:
                    p = Path(dirpath, filename)
                    if ".so" in filename and p.is_file():
                        self._sonames.setdefault(filename, []).append(p)
        matches = self._sonames.get(soname, [])
        if len(matches) > 1:
            logger.info(
                "needed library %s: %d matches under %s, not followed",
                soname,
                len(matches),
                self.project_root,
            )
        return matches[0] if len(matches) == 1 else None


_INSPECT_COMMAND = "docker container inspect -f '{{.State.Running}} {{.Image}}' "


_BOUNDED = (
    "bounded() { "
    'if [ -n "${OTTO_SCAN_END:-}" ]; then '
    'left=$(( $OTTO_SCAN_END - $(date +%s) )); [ "$left" -gt 0 ] || return 124; '
    'timeout -s KILL "$left" "$@"; '
    'else "$@"; fi; }\n'
)
"""``bounded CMD...`` runs CMD killed at the scan's deadline (epoch seconds in
``OTTO_SCAN_END``), or unbounded when the image has no usable ``timeout``. Each
process the scan starts runs under it, so the bound holds even where ``timeout``
kills only the command it runs (BusyBox), not that command's children."""

_HITS_SCRIPT = 'bounded grep -l "\\.gcda" "$@"; [ $? -lt 2 ]\n'
"""Run by ``find -exec ... {} +`` on each batch of regular files: list those holding a
coverage path, one per line. ``find`` reports any failed batch as its own ``1``, so grep's
"no match" (``1``) is a success here and only a read error (``2`` and up, or a kill) fails
the batch."""

_SKIPPED_TOP_DIRS = ["proc", "sys", "dev", "tmp", "run"]
"""Top directories the scan never enters: virtual filesystems, and runtime directories
whose files come and go in a live container (a file deleted between ``find`` listing it and
``grep`` opening it would fail the scan) and are never the image's binaries. ``/var/run``
is pruned inside ``/var`` for the same reason."""


def _scan_script(root: str = "", limit: int = 0) -> str:
    """Return the scan of every top directory under *root* (``""``: the container's ``/``).

    The scan ends itself *limit* seconds after it starts when the image has both
    ``timeout`` (BusyBox's or GNU's, ``-s`` and a bare duration) and ``date +%s``;
    without them it runs to completion, bounded only by the host-side timeout and
    the reap that follows it.
    """
    r = path_operand(root) if root else ""
    skipped = "|".join(f"{r}/{name}" for name in _SKIPPED_TOP_DIRS)
    return (
        "unset OTTO_SCAN_END; "
        'if timeout 5 sh -c : 2>/dev/null && now=$(date +%s 2>/dev/null) && [ -n "$now" ]; '
        f"then OTTO_SCAN_END=$(( now + {limit} )); export OTTO_SCAN_END; fi\n"
        + _BOUNDED
        + f"hits={shlex.quote(_BOUNDED + _HITS_SCRIPT)}\n"
        + f'for d in {r}/*; do case "$d" in {skipped}) ;; *) '
        f'bounded find "$d" -path {r}/var/run -prune -o -type f -exec sh -c "$hits" sh {{}} + '
        "2>/dev/null; rc=$?; "
        'printf \'otto-scan-rc:%s:%s\\n\' "$rc" "$d";; '
        "esac; done"
    )


_SCAN_LIMIT = 540
"""Seconds the scan may run inside the container: inside the host's own timeout,
so a slow scan reports its own failure rather than being abandoned."""
_SCAN_SCRIPT = _scan_script(limit=_SCAN_LIMIT)
"""Every regular file under the container's root that holds a coverage path, one top
directory at a time so each ``find``'s exit status is reported with its directory; any
failure (a file or directory it could not read, a missing tool, the time limit) refuses the
whole scan, since what it could not read may be instrumented. Only regular files are read:
BusyBox ``grep -r`` (Alpine images) opens a FIFO and blocks on it, and a symlink's target is
read under its own path. Mounts below a top directory are crossed: a volume or a
bind-mounted build holds the product's own files. ``/*`` does not match hidden top-level
entries (``/.name``), so those are not scanned, nor are the skipped top directories and
``/var/run``. Host execution merges stderr into stdout, so the tools' own diagnostics are
discarded in the container.

The tools are assumed to behave as BusyBox 1.30+ and GNU do: only short options both know
(``find -type f -path -prune -exec {} +``, ``grep -l``), and both greps list a binary file
that matches. ``-exec {} +`` needs BusyBox's ``FEATURE_FIND_EXEC_PLUS``; a ``find`` without
it fails, which refuses the scan rather than misreading it. ``timeout -s KILL SECS CMD`` is
the 1.30+ syntax; an older BusyBox (``timeout -t SECS``) fails the probe and the scan runs
unbounded."""
_REAP_SCRIPT = (
    "n=0; while [ $n -lt 10 ]; do f=; "
    "for e in /proc/[0-9]*/environ; do "
    'if grep -q "OTTO_SCAN_ID=$1" "$e" 2>/dev/null; then '
    'p=${e#/proc/}; kill -KILL "${p%/environ}" 2>/dev/null && f=1; fi; done; '
    '[ -n "$f" ] || break; n=$((n + 1)); done; :'
)
"""Kill every process in the container that carries the scan's token (``$1``) in its
environment: the scan's shell and everything it started, which ``docker exec`` does not
stop when its client goes away. Repeated until a sweep finds none, so a process started
mid-sweep is caught by the next."""
_RC_MARKER = "otto-scan-rc:"


def _scan_token() -> str:
    """Return a fresh, unguessable token naming a scan's processes or a host temp dir."""
    return secrets.token_hex(16)


@dataclass(frozen=True)
class _ScanOutput:
    """What one container scan reported."""

    paths: list[str]
    errors: list[int]
    failed_dirs: list[str]


def _parse_scan(text: str) -> _ScanOutput:
    """Return the paths, error statuses and failed top directories a scan printed."""
    paths: dict[str, None] = {}
    errors: list[int] = []
    failed_dirs: list[str] = []
    # Only a newline ends a line: str.splitlines() would also split a name at a character
    # such as U+2028 or U+0085.
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith(_RC_MARKER):
            rc, _, top = line[len(_RC_MARKER) :].strip().partition(":")
            code = int(rc) if rc.isdigit() else -1
            if code != 0:
                errors.append(code)
                if top:
                    failed_dirs.append(top)
        elif line.startswith("/"):
            paths.setdefault(line, None)
    return _ScanOutput(paths=list(paths), errors=errors, failed_dirs=failed_dirs)


_TEMP_DIR_EXPR = "${TMPDIR:-/tmp}"
_TEMP_DIR_PREFIX = "otto-objlist."


def _make_temp_dir_command(token: str) -> str:
    """Return the command making the host's private temp dir named by *token*; it prints it.

    ``mkdir`` without ``-p`` fails when the path exists, so a directory or symlink planted
    there is never used, and ``-m 700`` creates it private from the start.
    """
    return f'd="{_TEMP_DIR_EXPR}/{_TEMP_DIR_PREFIX}{token}" && mkdir -m 700 "$d" && echo "$d"'


_SCAN_TIMEOUT = 600
_COPY_TIMEOUT = 120
_DRY_RUN = "dry run: the container was not read"


def _no_such_container(result: "Result") -> bool:
    """Return whether a failed inspect says the container does not exist."""
    value = result.value
    said = f"{result.msg} {value if isinstance(value, str) else ''}"
    return "no such container" in said.lower()


def _said(result: "Result") -> str:
    """Return what a failed host command reported, its message first."""
    value = result.value
    said = result.msg or (value.strip() if isinstance(value, str) else "")
    return said or f"status {result.status.name}"


def _archive_suffix(name: str) -> str | None:
    """Return the longest :data:`~otto.host.product.ARCHIVE_SUFFIXES` member *name* ends with."""
    lowered = name.lower()
    return max((s for s in ARCHIVE_SUFFIXES if lowered.endswith(s)), key=len, default=None)


def _zstd_open(path: Path) -> bytes:
    """Decompress a ``.zst`` file; a missing decompressor or a corrupt frame is unreadable."""
    data = path.read_bytes()
    try:
        from compression import zstd  # ty: ignore[unresolved-import]  # Python 3.14+
    except ImportError:
        pass
    else:
        try:
            return zstd.decompress(data)
        except zstd.ZstdError as e:
            raise _UnreadableError(f"corrupt zstd data: {e}") from e
    try:
        import zstandard  # ty: ignore[unresolved-import]
    except ImportError:
        raise _UnreadableError(
            "no zstd decompressor is importable "
            "(Python 3.14's compression.zstd or the zstandard package)"
        ) from None
    try:
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except zstandard.ZstdError as e:
        raise _UnreadableError(f"corrupt zstd data: {e}") from e


def _gzip_open(path: Path) -> bytes:
    with gzip.open(path) as fh:
        return fh.read()


def _xz_open(path: Path) -> bytes:
    with lzma.open(path) as fh:
        return fh.read()


def _bz2_open(path: Path) -> bytes:
    with bz2.open(path) as fh:
        return fh.read()


_DECOMPRESSORS: dict[str, Callable[[Path], bytes]] = {
    ".gz": _gzip_open,
    ".xz": _xz_open,
    ".bz2": _bz2_open,
    ".zst": _zstd_open,
}
"""The single-file compressed forms of :data:`~otto.host.product.ARCHIVE_SUFFIXES`;
every other suffix there but ``.zip`` is a tar form, which ``tarfile`` opens
whatever its compression."""


def _dedupe(groups: Iterable[Iterable[str]]) -> list[str]:
    seen: dict[str, None] = {}
    for group in groups:
        for stem in group:
            seen.setdefault(stem, None)
    return list(seen)


def _tar_stems(path: Path) -> list[str]:
    with tarfile.open(path) as tf:
        return _dedupe(coverage_stems(d) for d in _tar_files(tf))


def _tar_files(tf: tarfile.TarFile) -> Iterator[bytes]:
    for member in tf.getmembers():
        if member.isfile():
            f = tf.extractfile(member)
            if f is not None:
                yield f.read()


def _zip_stems(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        try:
            return _dedupe(coverage_stems(zf.read(n)) for n in zf.namelist() if not n.endswith("/"))
        # NotImplementedError: an unsupported compression method; RuntimeError: an encrypted member.
        except (NotImplementedError, RuntimeError) as e:
            raise _UnreadableError(str(e)) from e


def _member_path(name: str) -> str:
    """Return a layer member's path without a leading ``./`` or ``/`` (never other dots)."""
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/").rstrip("/")


class _Filesystem:
    """An image's ELF files by path, as the layers so far leave them."""

    def __init__(self) -> None:
        self.files: dict[str, list[str]] = {}
        self._under: dict[str, int] = {}  # directory -> ELF files beneath it

    def _ancestors(self, path: str) -> Iterator[str]:
        parent = path.rpartition("/")[0]
        while parent:
            yield parent
            parent = parent.rpartition("/")[0]

    def put(self, path: str, stems: list[str]) -> None:
        if path not in self.files:
            for d in self._ancestors(path):
                self._under[d] = self._under.get(d, 0) + 1
        self.files[path] = stems

    def remove(self, path: str) -> None:
        """Remove the file at exactly *path*, if any."""
        if self.files.pop(path, None) is not None:
            for d in self._ancestors(path):
                self._under[d] -= 1

    def remove_tree(self, path: str) -> None:
        """Remove the file at *path* and every file beneath it."""
        self.remove(path)
        if self._under.get(path):
            prefix = path + "/"
            for key in [k for k in self.files if k.startswith(prefix)]:
                self.remove(key)

    def clear(self) -> None:
        self.files.clear()
        self._under.clear()


def _docker_stems(path: Path) -> list[str]:
    return _dedupe(_docker_effective_elves(path))


def _docker_effective_elves(path: Path) -> Iterator[list[str]]:
    """Yield the coverage stems of every ELF file in a ``docker save`` image's final filesystem.

    Per layer, whiteouts (``.wh.<name>``, opaque ``.wh..wh..opq``) apply to the
    state inherited from earlier layers before the layer's own entries land, so
    member order inside a layer never matters. A directory entry replaces an
    inherited file at its path and keeps what the directory already holds; any
    other entry (file, symlink, device) replaces the inherited path and
    everything beneath it, and only an ELF file stores itself.
    """
    fs = _Filesystem()
    with tarfile.open(path) as outer:
        manifest_file = outer.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("manifest.json missing from image archive")
        manifest = json.load(manifest_file)
        for layer_name in manifest[0]["Layers"]:
            layer = outer.extractfile(layer_name)
            if layer is None:
                raise ValueError(f"layer {layer_name} missing from image archive")
            with tarfile.open(fileobj=layer) as lt:
                additions: list[tarfile.TarInfo] = []
                for member in lt.getmembers():
                    name = _member_path(member.name)
                    parent, _, base = name.rpartition("/")
                    if base == _OPAQUE:
                        if parent:
                            fs.remove_tree(
                                parent
                            )  # no file at a directory's path: only contents go
                        else:
                            fs.clear()
                    elif base.startswith(_WHITEOUT):
                        fs.remove_tree((parent + "/" if parent else "") + base[len(_WHITEOUT) :])
                    else:
                        additions.append(member)
                for member in additions:
                    name = _member_path(member.name)
                    if member.isdir():
                        fs.remove(name)
                        continue
                    fs.remove_tree(name)
                    if not member.isfile():
                        continue
                    f = lt.extractfile(member)
                    if f is None:
                        continue
                    magic = f.read(len(_ELF_MAGIC))
                    if magic == _ELF_MAGIC:
                        fs.put(name, coverage_stems(magic + f.read()))
    yield from fs.files.values()
