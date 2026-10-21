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
"""

import bz2
import gzip
import json
import logging
import lzma
import os
import re
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..host.product import ARCHIVE_SUFFIXES

if TYPE_CHECKING:
    from ..host.product import Product

logger = logging.getLogger(__name__)

_STEM_RE = re.compile(rb"(?<=\x00)([\x21-\x7e][\x20-\x7e]{0,4095}?)\.gcda(?=\x00)")
_ELF_MAGIC = b"\x7fELF"
_ZIP = ".zip"
_WHITEOUT = ".wh."
_OPAQUE = ".wh..wh..opq"
_ELF_HEADER_SIZE = 64  # the 64-bit header; a 32-bit one is shorter
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
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


@dataclass(frozen=True)
class Unavailable:
    """Why a product has no object list (pairing then searches by path)."""

    product: str
    reason: str


class _UnreadableError(Exception):
    """An artifact this module cannot read; the message is the reason."""


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
    if not data.startswith(_ELF_MAGIC) or len(data) < _ELF_HEADER_SIZE:
        return []
    is64 = data[4] == _ELFCLASS64
    end = "<" if data[5] == _ELFDATA2LSB else ">"
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

    def read(self, product: "Product") -> ObjectList | Unavailable:
        """Return *product*'s object list, or why it has none (logged at INFO)."""
        artifact = getattr(product, "artifact", None)
        if artifact is None:
            return self._unavailable(product, "the product declares no local artifact")
        is_tarball = getattr(product, "is_tarball", None)
        if is_tarball is False:
            return self._unavailable(
                product, f"image {artifact} is a reference, not a local tarball"
            )
        path = Path(artifact)
        if not path.exists():
            return self._unavailable(product, f"artifact {path} does not exist")
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
            return self._unavailable(product, f"artifact {path} could not be read: {e}")
        if not stems:
            return self._unavailable(
                product, f"artifact {path} holds no coverage paths (not instrumented)"
            )
        return ObjectList(
            product=product.name, paths=[PurePosixPath(s) for s in stems], source=str(path)
        )

    def _unavailable(self, product: "Product", reason: str) -> Unavailable:
        logger.info("%s: object list unavailable: %s", product.name, reason)
        return Unavailable(product=product.name, reason=reason)

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
