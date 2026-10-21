import bz2
import gzip
import io
import json
import lzma
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from otto.coverage.objects import ObjectList, ObjectListCache, Unavailable, coverage_stems

ELF = b"\x7fELF"


def _zstd_compress():
    try:
        from compression.zstd import compress  # ty: ignore[unresolved-import]
    except ImportError:
        try:
            from zstandard import compress  # ty: ignore[unresolved-import]
        except ImportError:
            return None
    return compress


_HAS_ZSTD = _zstd_compress() is not None


def test_coverage_stems_reads_nul_terminated_paths():
    data = b"junk\x00/a/b/main.cpp.gcda\x00more\x00/a/c/util.c.gcda\x00/a/b/main.cpp.gcda\x00"
    assert coverage_stems(data) == ["/a/b/main.cpp", "/a/c/util.c"]


def test_coverage_stems_ignores_unterminated_and_bare_suffix():
    assert coverage_stems(b"x.gcda") == []
    assert coverage_stems(b"\x00.gcda\x00") == []


class _Shell:
    def __init__(self, name, artifact):
        self.name = name
        self.artifact = Path(artifact)


class _Img:
    def __init__(self, artifact, tarball=True):
        self.name = "img"
        self.artifact = Path(artifact)
        self.is_tarball = tarball


def _paths(result):
    assert isinstance(result, ObjectList), result
    return sorted(map(str, result.paths))


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
def test_real_gcc_binary_lists_absolute_stems(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("int main(void){return 0;}\n")
    subprocess.run(
        ["gcc", "--coverage", "-c", "src/main.c", "-o", "main.o"], cwd=tmp_path, check=True
    )
    subprocess.run(["gcc", "--coverage", "main.o", "-o", "app"], cwd=tmp_path, check=True)
    assert _paths(ObjectListCache(tmp_path).read(_Shell("app", tmp_path / "app"))) == [
        str(tmp_path / "main")
    ]


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
def test_dt_needed_library_under_root_is_scanned(tmp_path):
    (tmp_path / "lib.c").write_text("int f(void){return 1;}\n")
    (tmp_path / "main.c").write_text("int f(void); int main(void){return f();}\n")
    subprocess.run(
        ["gcc", "--coverage", "-fPIC", "-c", "lib.c", "-o", "lib.o"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["gcc", "--coverage", "-shared", "lib.o", "-o", "libcore.so", "-Wl,-soname,libcore.so"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["gcc", "--coverage", "-c", "main.c", "-o", "main.o"], cwd=tmp_path, check=True)
    subprocess.run(
        ["gcc", "--coverage", "main.o", "-L.", "-lcore", "-o", "app"], cwd=tmp_path, check=True
    )
    (tmp_path / "out").mkdir()
    shutil.move(tmp_path / "app", tmp_path / "out/app")
    assert _paths(ObjectListCache(tmp_path).read(_Shell("app", tmp_path / "out/app"))) == sorted(
        [str(tmp_path / "lib"), str(tmp_path / "main")]
    )
    shutil.move(tmp_path / "libcore.so", tmp_path.parent / f"{tmp_path.name}-libcore.so")
    assert _paths(ObjectListCache(tmp_path).read(_Shell("app", tmp_path / "out/app"))) == [
        str(tmp_path / "main")
    ]


def test_directory_artifact_is_walked(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "a").write_bytes(ELF + b"\x00/x/a.c.gcda\x00")
    (tmp_path / "bin" / "b").write_bytes(ELF + b"\x00/x/b.c.gcda\x00")
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", tmp_path / "bin"))) == [
        "/x/a.c",
        "/x/b.c",
    ]


def test_directory_descendant_rebuild_is_seen_by_a_new_cache(tmp_path):
    (tmp_path / "bin").mkdir()
    f = tmp_path / "bin" / "a"
    f.write_bytes(ELF + b"\x00/x/a.c.gcda\x00")
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", tmp_path / "bin"))) == ["/x/a.c"]
    f.write_bytes(ELF + b"\x00/x/a2.c.gcda\x00")
    os.utime(f, ns=(1, 1))
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", tmp_path / "bin"))) == ["/x/a2.c"]


def test_one_read_per_artifact_per_cache(tmp_path):
    art = tmp_path / "app"
    art.write_bytes(ELF + b"\x00/x/a.c.gcda\x00")
    cache = ObjectListCache(tmp_path)
    cache.read(_Shell("p", art))
    cache.read(_Shell("p", art))
    assert cache.files_read == 1


def _tar_gz(tmp_path: Path) -> Path:
    payload = ELF + b"\x00/x/m.c.gcda\x00"
    art = tmp_path / "p.tar.gz"
    with tarfile.open(art, "w:gz") as tf:
        info = tarfile.TarInfo("bin/m")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return art


def test_tar_gz_artifact_is_read_member_by_member(tmp_path):
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", _tar_gz(tmp_path)))) == ["/x/m.c"]


def test_repeated_tar_is_read_once_per_cache(tmp_path):
    art = _tar_gz(tmp_path)
    cache = ObjectListCache(tmp_path)
    assert _paths(cache.read(_Shell("p", art))) == ["/x/m.c"]
    assert _paths(cache.read(_Shell("p", art))) == ["/x/m.c"]
    assert cache.files_read == 1


@pytest.mark.parametrize(
    ("suffix", "compress"),
    [(".gz", gzip.compress), (".xz", lzma.compress), (".bz2", bz2.compress)],
)
def test_single_compressed_file_is_decompressed(tmp_path, suffix, compress):
    art = tmp_path / f"app{suffix}"
    art.write_bytes(compress(ELF + b"\x00/x/z.c.gcda\x00"))
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", art))) == ["/x/z.c"]


@pytest.mark.skipif(not _HAS_ZSTD, reason="no zstd decompressor importable")
def test_single_zst_file_is_decompressed(tmp_path):
    compress = _zstd_compress()
    assert compress is not None
    art = tmp_path / "app.zst"
    art.write_bytes(compress(ELF + b"\x00/x/z.c.gcda\x00"))
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", art))) == ["/x/z.c"]


@pytest.mark.skipif(_HAS_ZSTD, reason="a zstd decompressor is importable")
def test_zst_without_a_decompressor_is_unavailable_never_scanned(tmp_path):
    # The raw bytes carry a marker: scanning them instead of decompressing would "find" it.
    art = tmp_path / "app.zst"
    art.write_bytes(ELF + b"\x00/x/raw.c.gcda\x00")
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "zstd" in result.reason


def _layer(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """entries: (name, data, kind) with kind 'file' | 'symlink' | 'dir'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/bin/true"
                tf.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                body = data or b""
                info.size = len(body)
                tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _image(tmp_path: Path, layers: list[bytes]) -> Path:
    art = tmp_path / "img.tar"
    with tarfile.open(art, "w") as tf:
        names = []
        for i, data in enumerate(layers):
            name = f"l{i}/layer.tar"
            names.append(name)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        manifest = json.dumps([{"Layers": names}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
    return art


def _elf(stem: str) -> bytes:
    return ELF + b"\x00" + stem.encode() + b".gcda\x00"


def test_docker_replacement_and_nested_whiteout(tmp_path):
    l1 = _layer([("app/old", _elf("/b/old.c"), "file"), ("app/gone", _elf("/b/gone.c"), "file")])
    l2 = _layer([("app/old", _elf("/b/new.c"), "file"), ("app/.wh.gone", None, "file")])
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == ["/b/new.c"]


def test_docker_root_level_whiteout(tmp_path):
    l1 = _layer([("tool", _elf("/b/tool.c"), "file"), ("keep", _elf("/b/keep.c"), "file")])
    l2 = _layer([(".wh.tool", None, "file")])
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == ["/b/keep.c"]


def test_docker_opaque_dir_keeps_same_layer_additions(tmp_path):
    l1 = _layer([("opt/a", _elf("/b/a.c"), "file")])
    # the new file is listed BEFORE the opaque marker: it must survive
    l2 = _layer([("opt/b", _elf("/b/b.c"), "file"), ("opt/.wh..wh..opq", None, "file")])
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == ["/b/b.c"]


def test_docker_elf_replaced_by_symlink_is_gone(tmp_path):
    l1 = _layer([("usr/bin/app", _elf("/b/app.c"), "file"), ("usr/bin/x", _elf("/b/x.c"), "file")])
    l2 = _layer([("usr/bin/app", None, "symlink")])
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == ["/b/x.c"]


def test_docker_file_replaced_by_directory(tmp_path):
    # A directory entry removes an inherited FILE at its path; a re-declared
    # directory keeps the files it already held.
    l1 = _layer(
        [
            ("opt/app", _elf("/b/app.c"), "file"),
            ("srv", None, "dir"),
            ("srv/keep", _elf("/b/keep.c"), "file"),
        ]
    )
    l2 = _layer(
        [
            ("opt/app", None, "dir"),
            ("opt/app/inner", _elf("/b/inner.c"), "file"),
            ("srv", None, "dir"),
        ]
    )
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == [
        "/b/inner.c",
        "/b/keep.c",
    ]


def test_docker_directory_replaced_by_file(tmp_path):
    l1 = _layer([("opt/d/x", _elf("/b/x.c"), "file"), ("opt/d/y", _elf("/b/y.c"), "file")])
    l2 = _layer([("opt/d", _elf("/b/d.c"), "file")])
    assert _paths(ObjectListCache(tmp_path).read(_Img(_image(tmp_path, [l1, l2])))) == ["/b/d.c"]


def test_repeated_docker_image_is_read_once_per_cache(tmp_path):
    art = _image(tmp_path, [_layer([("bin/a", _elf("/b/a.c"), "file")])])
    cache = ObjectListCache(tmp_path)
    assert _paths(cache.read(_Img(art))) == ["/b/a.c"]
    assert _paths(cache.read(_Img(art))) == ["/b/a.c"]
    assert cache.files_read == 1


def test_uninstrumented_or_missing_artifact_is_unavailable(tmp_path):
    (tmp_path / "plain").write_bytes(ELF + b"no markers")
    cache = ObjectListCache(tmp_path)
    assert "not instrumented" in cache.read(_Shell("p", tmp_path / "plain")).reason
    assert "does not exist" in cache.read(_Shell("q", tmp_path / "absent")).reason


def test_reference_form_docker_image_is_unavailable(tmp_path):
    assert isinstance(ObjectListCache(tmp_path).read(_Img("redis:7", tarball=False)), Unavailable)


@pytest.mark.skipif(not _HAS_ZSTD, reason="no zstd decompressor importable")
def test_corrupt_zst_is_unavailable(tmp_path):
    art = tmp_path / "app.zst"
    art.write_bytes(b"\x28\xb5\x2f\xfd garbage, not a zstd frame")
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "could not be read" in result.reason


def test_zip_artifact_is_read_member_by_member(tmp_path):
    art = tmp_path / "p.zip"
    with zipfile.ZipFile(art, "w") as zf:
        zf.writestr("bin/m", ELF + b"\x00/x/m.c.gcda\x00")
        zf.writestr("bin/n", ELF + b"\x00/x/n.c.gcda\x00")
    assert _paths(ObjectListCache(tmp_path).read(_Shell("p", art))) == ["/x/m.c", "/x/n.c"]


def test_encrypted_zip_member_is_unavailable(tmp_path):
    art = tmp_path / "p.zip"
    with zipfile.ZipFile(art, "w") as zf:
        zf.writestr("bin/m", ELF + b"\x00/x/m.c.gcda\x00")
    # zipfile never writes an encrypted member: set the "encrypted" flag bit in
    # both the local header (flags at +6) and the central directory entry (+8).
    raw = bytearray(art.read_bytes())
    raw[6] |= 0x1
    raw[raw.index(b"PK\x01\x02") + 8] |= 0x1
    art.write_bytes(bytes(raw))
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "could not be read" in result.reason


def test_unsupported_zip_compression_is_unavailable(tmp_path, monkeypatch):
    art = tmp_path / "p.zip"
    with zipfile.ZipFile(art, "w") as zf:
        zf.writestr("bin/m", ELF + b"\x00/x/m.c.gcda\x00")

    def _unsupported(self, name, pwd=None):
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(zipfile.ZipFile, "read", _unsupported)
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "could not be read" in result.reason


def test_corrupt_deflate_zip_member_is_unavailable(tmp_path):
    art = tmp_path / "p.zip"
    with zipfile.ZipFile(art, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bin/m", ELF + b"\x00/x/m.c.gcda\x00" * 64)
    # The member's deflate stream starts right after its 30-byte local header and
    # name; 0xFF opens a block of the reserved type 3, which zlib rejects.
    raw = bytearray(art.read_bytes())
    raw[30 + len("bin/m")] = 0xFF
    art.write_bytes(bytes(raw))
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "could not be read" in result.reason


def test_corrupt_deflate_gzip_file_is_unavailable(tmp_path):
    art = tmp_path / "app.gz"
    raw = bytearray(gzip.compress(ELF + b"\x00/x/m.c.gcda\x00" * 64))
    raw[10] = 0xFF  # first deflate byte after the 10-byte gzip header
    art.write_bytes(bytes(raw))
    result = ObjectListCache(tmp_path).read(_Shell("p", art))
    assert isinstance(result, Unavailable)
    assert "could not be read" in result.reason
