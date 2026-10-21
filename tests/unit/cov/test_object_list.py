import bz2
import contextlib
import gzip
import io
import json
import logging
import lzma
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import time
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


@pytest.fixture
def token(monkeypatch):
    """Pin the scan's process token, which names it for the reap."""
    monkeypatch.setattr("otto.coverage.objects._scan_token", lambda: _TOKEN)
    return _TOKEN


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


def test_reference_form_docker_image_is_unavailable_to_the_local_read(tmp_path):
    result = ObjectListCache(tmp_path).read(_Img("redis:7", tarball=False))
    assert isinstance(result, Unavailable)
    assert result.kind == "reference"


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


# --- a reference-form image, read from its running container ---------------------------


class _RefImg:
    def __init__(self, name="img", container_name="img-c"):
        self.name = name
        self.artifact = Path("redis:7")
        self.is_tarball = False
        self.container_name = container_name


_INSPECT = "docker container inspect"
_EXEC = "docker exec -e OTTO_SCAN_ID="
_REAP = "docker exec img-c"
_TOKEN = "f00d"
_MKDIR = 'd="${TMPDIR:-/tmp}/otto-objlist.'
_CP = "docker cp"
_RM = "rm -rf"
_HOST_TMP = f"/tmp/otto-objlist.{_TOKEN}"
_RM_CMD = f'rm -rf "${{TMPDIR:-/tmp}}/otto-objlist.{_TOKEN}"'


def _ok(value=""):
    from otto.result import Result
    from otto.utils import Status

    return Result(Status.Success, value=value)


def _failed(value="", msg="failed"):
    from otto.result import Result
    from otto.utils import Status

    return Result(Status.Error, value=value, msg=msg)


def _scan(paths, rcs=(0,), extra=()):
    lines = [*paths, *extra, *(f"otto-scan-rc:{rc}" for rc in rcs)]
    return _ok("\n".join(lines) + "\n")


class _DockerHost:
    """A daemon host: ``exec`` answers by command prefix; ``docker cp`` and ``get`` move bytes."""

    def __init__(self, container_files, *, answers=None, get_fails=False, get_drops=()):
        self.id = "lab1"
        self.container_files = container_files
        self.host_files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.get_fails = get_fails
        self.get_drops = set(get_drops)
        self.answers = {
            _INSPECT: _ok("true sha256:feed\n"),
            _EXEC: _scan(sorted(container_files)),
            _REAP: _ok(),
            _MKDIR: _ok(_HOST_TMP + "\n"),
            _RM: _ok(),
        }
        self.answers.update(answers or {})

    async def exec(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd.startswith(_CP):
            return self._cp(cmd)
        for prefix, answer in self.answers.items():
            if cmd.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"unexpected command: {cmd}")

    def _cp(self, cmd):
        import shlex

        _docker, _cp, src, dest = shlex.split(cmd)
        answer = self.answers.get(_CP)
        if answer is not None:
            return answer
        path = src.split(":", 1)[1]
        self.host_files[dest] = self.container_files[path]
        return _ok()

    async def get(self, src_files, dest_dir, user=None, show_progress=True, **_):
        from otto.result import Result
        from otto.utils import Status

        assert show_progress is False
        if self.get_fails:
            return Result(Status.Error, value={}, msg="sftp: connection lost")
        got = {}
        for src in src_files:
            if self.host_files[str(src)] in self.get_drops:
                got[src] = _ok()  # reported fetched, never delivered
                continue
            (Path(dest_dir) / src.name).write_bytes(self.host_files[str(src)])
            got[src] = _ok()
        return Result(Status.Success, value=got)

    def issued(self, prefix):
        return [c for c in self.calls if c.startswith(prefix)]


_TWO = {
    "/usr/bin/app": ELF + b"\x00/build/app/main.c.gcda\x00\x00/build/app/util.c.gcda\x00",
    "/usr/lib/libcore.so": ELF + b"\x00/build/core/core.c.gcda\x00\x00/build/app/util.c.gcda\x00",
}


@pytest.mark.asyncio
async def test_running_container_lists_the_stems_of_its_scanned_files(tmp_path, token):
    from otto.coverage.objects import _SCAN_SCRIPT

    host = _DockerHost(_TWO)
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert host.issued(_INSPECT) == [
        "docker container inspect -f '{{.State.Running}} {{.Image}}' img-c"
    ]
    assert host.issued(_EXEC) == [
        f"docker exec -e OTTO_SCAN_ID={_TOKEN} img-c sh -c {shlex.quote(_SCAN_SCRIPT)}"
    ]
    assert host.issued(_REAP) == []  # a clean scan left nothing to stop
    assert host.issued(_MKDIR) == [
        f'd="${{TMPDIR:-/tmp}}/otto-objlist.{_TOKEN}" && mkdir -m 700 "$d" && echo "$d"'
    ]
    assert host.issued(_RM) == [_RM_CMD]


@pytest.mark.asyncio
async def test_scan_keeps_only_absolute_path_lines(tmp_path):
    stray = ["grep: warning: recursive search of stdin", "sh: 1: something odd"]
    host = _DockerHost(_TWO, answers={_EXEC: _scan(sorted(_TWO), rcs=(0, 0), extra=stray)})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert len(host.issued(_CP)) == 2  # the stray lines were never fetched


@pytest.mark.asyncio
async def test_a_grep_error_beside_hits_is_unreadable_and_not_cached(tmp_path):
    """A partial scan may miss an instrumented file: never an object list, never cached."""
    scan = _scan(sorted(_TWO), rcs=("0:/bin", "2:/opt", "0:/srv"))
    host = _DockerHost(_TWO, answers={_EXEC: scan})
    cache = ObjectListCache(tmp_path)
    first = await cache.read_running(_RefImg(), host)
    assert isinstance(first, Unavailable)
    assert first.kind == "unreadable"
    assert first.reason == (
        "could not scan container img-c: the scan exited 2 under /opt (an unreadable file "
        "or directory, no find or grep in the image, or the scan outran its 540s limit)"
    )
    assert host.issued(_CP) == []
    await cache.read_running(_RefImg(), host)
    assert len(host.issued(_EXEC)) == 2  # the failed scan was not cached


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "fails"])
async def test_a_failed_temp_dir_removal_keeps_the_scan_result_and_is_logged(
    tmp_path, caplog, failure, token
):
    answer = OSError("ssh: broken pipe") if failure == "raises" else _failed(msg="rm: busy")
    host = _DockerHost(_TWO, answers={_RM: answer})
    with caplog.at_level("WARNING"):
        result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"otto-objlist.{_TOKEN}" in w for w in warnings), warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspect",
    [_ok("false sha256:feed\n"), _failed("Error: No such container: img-c")],
    ids=["stopped", "absent"],
)
async def test_a_container_not_running_is_unavailable(tmp_path, inspect):
    host = _DockerHost(_TWO, answers={_INSPECT: inspect})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "not-running"
    assert "img-c" in result.reason
    assert host.issued(_EXEC) == []


@pytest.mark.asyncio
async def test_the_same_image_is_scanned_once(tmp_path):
    host = _DockerHost(_TWO)
    cache = ObjectListCache(tmp_path)
    first = await cache.read_running(_RefImg("a", "a-c"), host)
    second = await cache.read_running(_RefImg("b", "b-c"), host)
    assert _paths(first) == _paths(second)
    assert second.product == "b"
    assert len(host.issued(_EXEC)) == 1
    assert len(host.issued(_INSPECT)) == 2


@pytest.mark.asyncio
async def test_a_dry_run_is_unavailable_and_issues_nothing_further(tmp_path):
    from otto.result import NotRunResult
    from otto.utils import Status

    declined = NotRunResult(Status.NotRun, command="docker container inspect", msg="declined")
    host = _DockerHost(_TWO, answers={_INSPECT: declined})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "dry-run"
    assert host.calls == [host.issued(_INSPECT)[0]]


@pytest.mark.asyncio
async def test_a_grep_error_with_no_stems_is_unreadable_not_uninstrumented(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _scan([], rcs=(0, 1))})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "could not scan container img-c" in result.reason
    assert "the scan exited 1" in result.reason


@pytest.mark.asyncio
async def test_a_clean_scan_with_no_matches_is_not_instrumented(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _scan([], rcs=(0, 0))})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "not-instrumented"


@pytest.mark.asyncio
async def test_a_failed_scan_exec_is_unreadable(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _failed("OCI runtime exec failed: no sh")})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"


@pytest.mark.asyncio
async def test_a_failed_docker_cp_is_unreadable_naming_the_path(tmp_path, token):
    host = _DockerHost(_TWO, answers={_CP: _failed("Error: no such file")})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "/usr/bin/app" in result.reason
    assert host.issued(_RM) == [_RM_CMD]


@pytest.mark.asyncio
async def test_the_host_temp_dir_is_removed_when_the_get_fails(tmp_path, token):
    host = _DockerHost(_TWO, get_fails=True)
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert host.issued(_RM) == [_RM_CMD]


def test_the_scan_uses_only_grep_options_busybox_knows():
    """BusyBox grep (Alpine images) rejects GNU long options with rc 2, failing every scan."""
    from otto.coverage.objects import _SCAN_SCRIPT

    calls = [part.split() for part in _SCAN_SCRIPT.split("grep ")[1:]]
    assert [words[: words.index('"\\.gcda"')] for words in calls] == [["-l"]]


@pytest.mark.skipif(shutil.which("busybox") is None, reason="no busybox here")
def test_busybox_grep_lists_a_binary_file_holding_a_coverage_path(tmp_path):
    (tmp_path / "bin").write_bytes(ELF + b"\x00\xff/b/a.c.gcda\x00\xfe")
    (tmp_path / "txt").write_text("plain\n")
    done = subprocess.run(
        ["busybox", "grep", "-l", "\\.gcda", str(tmp_path / "bin"), str(tmp_path / "txt")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout.split()) == (0, [str(tmp_path / "bin")])


@pytest.mark.asyncio
async def test_an_inspect_that_fails_otherwise_is_unreadable_with_dockers_error(tmp_path):
    said = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
    host = _DockerHost(_TWO, answers={_INSPECT: _failed(said, msg="")})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert said in result.reason
    assert "img-c" in result.reason
    assert host.issued(_EXEC) == []


@pytest.mark.asyncio
async def test_a_file_the_get_reported_but_never_delivered_is_unreadable(tmp_path, token):
    host = _DockerHost(_TWO, get_drops=[_TWO["/usr/lib/libcore.so"]])
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "/usr/lib/libcore.so" in result.reason
    assert host.issued(_RM) == [_RM_CMD]


# The scan as it runs inside a container, run here for real: BusyBox (Alpine images) and the
# host's own POSIX shell with GNU find and grep. The fake root stands in for the container's /.

_BUSYBOX = shutil.which("busybox")
_needs_busybox = pytest.mark.skipif(_BUSYBOX is None, reason="no busybox here")
_APPLETS = ["sh", "find", "grep", "timeout", "date", "kill", "sleep", "cat", "mkdir"]


def _busybox_bin(tmp_path: Path) -> Path:
    """Return a directory holding only BusyBox applets, as an Alpine image's PATH would."""
    assert _BUSYBOX is not None
    bin_dir = tmp_path / "bb-bin"
    bin_dir.mkdir()
    for applet in _APPLETS:
        (bin_dir / applet).symlink_to(_BUSYBOX)
    return bin_dir


def _gnu_bin(tmp_path: Path, *, grep_sleeps=False) -> str:
    """Return a PATH of the host's own tools; *grep_sleeps* puts a grep that never ends first."""
    shim = tmp_path / "shim-bin"
    shim.mkdir()
    if grep_sleeps:
        grep = shim / "grep"
        grep.write_text("#!/bin/sh\nexec sleep 60\n")
        grep.chmod(0o755)
    return f"{shim}:/usr/bin:/bin"


def _scan_pids(token: str) -> list[int]:
    """Return the processes whose environment carries the scan's token."""
    pids = []
    needle = f"OTTO_SCAN_ID={token}".encode()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if needle in (entry / "environ").read_bytes():
                    pids.append(int(entry.name))
            except OSError:
                continue
    return pids


@pytest.fixture
def scan_token():
    """A token unique to this test; any scan process it leaves behind is killed afterwards."""
    import secrets

    token = secrets.token_hex(8)
    yield token
    for pid in _scan_pids(token):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _run(shell: str, script: str, path: str, token: str, *args: str, timeout: float = 30):
    return subprocess.run(
        [shell, "-c", script, "sh", *args],
        env={"PATH": path, "OTTO_SCAN_ID": token},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _rcs(stdout: str) -> list[int]:
    return [
        int(line.split(":")[1]) for line in stdout.split("\n") if line.startswith("otto-scan-rc:")
    ]


def test_a_scan_past_its_limit_ends_inside_the_container(tmp_path, scan_token):
    """A grep that never ends is killed at the limit: the scan reports an error, nothing stays."""
    from otto.coverage.objects import _scan_script

    root = tmp_path / "root"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "app").write_bytes(ELF + b"\x00/b/a.c.gcda\x00")
    path = _gnu_bin(tmp_path, grep_sleeps=True)
    started = time.monotonic()
    done = _run("/bin/sh", _scan_script(str(root), limit=2), path, scan_token, timeout=20)
    assert time.monotonic() - started < 15
    assert any(rc != 0 for rc in _rcs(done.stdout)), done.stdout
    assert _scan_pids(scan_token) == []


@pytest.mark.parametrize("tools", ["busybox", "gnu"])
def test_bounded_kills_its_command_at_the_deadline(tmp_path, scan_token, tools):
    from otto.coverage.objects import _BOUNDED

    if tools == "busybox":
        if _BUSYBOX is None:
            pytest.skip("no busybox here")
        bin_dir = _busybox_bin(tmp_path)
        shell, path = str(bin_dir / "sh"), str(bin_dir)
    else:
        shell, path = "/bin/sh", _gnu_bin(tmp_path)
    script = (
        _BOUNDED
        + "OTTO_SCAN_END=$(( $(date +%s) + 1 )); export OTTO_SCAN_END; "
        + 'bounded sleep 30; echo "rc=$?"'
    )
    started = time.monotonic()
    done = _run(shell, script, path, scan_token, timeout=20)
    assert time.monotonic() - started < 10
    assert done.stdout.split()[-1] in ("rc=124", "rc=137"), done.stdout
    assert _scan_pids(scan_token) == []


def test_a_scan_without_timeout_in_the_image_still_runs_unbounded(tmp_path, scan_token):
    """An image with no ``timeout`` takes the unbounded branch, and still scans.

    Run with dash and a PATH of the host's own tools minus ``timeout``: BusyBox's shell
    runs its own ``timeout`` applet whatever PATH holds, so it cannot reach this branch.
    The ``grep`` shim reports whether the deadline reached the scan's processes.
    """
    from otto.coverage.objects import _scan_script

    root = tmp_path / "root"
    app = _hit(root / "opt" / "app")
    bin_dir = tmp_path / "no-timeout-bin"
    bin_dir.mkdir()
    for tool in ("sh", "find", "date"):
        real = shutil.which(tool)
        assert real is not None
        (bin_dir / tool).symlink_to(real)
    grep = bin_dir / "grep"
    real_grep = shutil.which("grep")
    assert real_grep is not None
    grep.write_text(f'#!/bin/sh\necho "deadline=${{OTTO_SCAN_END-unset}}"\nexec {real_grep} "$@"\n')
    grep.chmod(0o755)
    done = _run("/bin/sh", _scan_script(str(root), limit=60), str(bin_dir), scan_token)
    lines = done.stdout.split("\n")
    assert "deadline=unset" in lines, done.stdout
    assert str(app) in lines, done.stdout
    assert _rcs(done.stdout) == [0], done.stdout


@pytest.mark.parametrize("reaper", ["busybox", "gnu"])
def test_the_reap_ends_a_running_scan_and_everything_it_started(tmp_path, scan_token, reaper):
    """What a cancelled or timed-out host command leaves running in the container is stopped."""
    from otto.coverage.objects import _REAP_SCRIPT, _scan_script

    root = tmp_path / "root"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "app").write_bytes(ELF + b"\x00/b/a.c.gcda\x00")
    path = _gnu_bin(tmp_path, grep_sleeps=True)
    scan = subprocess.Popen(
        ["/bin/sh", "-c", _scan_script(str(root), limit=300)],
        env={"PATH": path, "OTTO_SCAN_ID": scan_token},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while len(_scan_pids(scan_token)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)  # the shell, its timeout and the sleeping grep
        assert len(_scan_pids(scan_token)) >= 3
        if reaper == "busybox":
            if _BUSYBOX is None:
                pytest.skip("no busybox here")
            bin_dir = _busybox_bin(tmp_path)
            shell, rpath = str(bin_dir / "sh"), str(bin_dir)
        else:
            shell, rpath = "/bin/sh", "/usr/bin:/bin"
        subprocess.run(
            [shell, "-c", _REAP_SCRIPT, "sh", scan_token],
            env={"PATH": rpath},
            timeout=20,
            check=True,
        )
        scan.wait(timeout=10)
        assert _scan_pids(scan_token) == []
    finally:
        scan.kill()
        scan.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["fails", "times-out-inside", "raises", "cancelled"],
)
async def test_a_scan_that_did_not_finish_cleanly_is_reaped(tmp_path, token, outcome):
    import asyncio

    answer = {
        "fails": _failed(msg="Command timed out after 600s"),
        "times-out-inside": _scan(sorted(_TWO), rcs=("0:/bin", "137:/opt")),
        "raises": OSError("ssh: connection lost"),
        "cancelled": asyncio.CancelledError(),
    }[outcome]
    host = _DockerHost(_TWO, answers={_EXEC: answer})
    cache = ObjectListCache(tmp_path)
    if outcome in ("raises", "cancelled"):
        with pytest.raises(type(answer)):
            await cache.read_running(_RefImg(), host)
    else:
        result = await cache.read_running(_RefImg(), host)
        assert isinstance(result, Unavailable)
        assert result.kind == "unreadable"
    from otto.coverage.objects import _REAP_SCRIPT

    assert host.issued(_REAP) == [
        f"docker exec img-c sh -c {shlex.quote(_REAP_SCRIPT)} sh {_TOKEN}"
    ]


def _tools(tmp_path: Path, kind: str) -> list[str]:
    """Return ``[shell, PATH]`` for *kind*: BusyBox applets only, or the host's own tools."""
    if kind == "busybox":
        if _BUSYBOX is None:
            pytest.skip("no busybox here")
        bin_dir = _busybox_bin(tmp_path)
        return [str(bin_dir / "sh"), str(bin_dir)]
    return ["/bin/sh", _gnu_bin(tmp_path)]


def _hit(path: Path, stem: str = "/b/a.c") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ELF + b"\x00\xff" + stem.encode() + b".gcda\x00\xfe")
    return path


@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_the_scan_skips_a_fifo_and_lists_only_regular_files(tmp_path, scan_token, kind):
    """BusyBox ``grep -r`` opens a FIFO and blocks on it; the scan must never read one."""
    from otto.coverage.objects import _scan_script

    shell, path = _tools(tmp_path, kind)
    root = tmp_path / "root"
    app = _hit(root / "opt" / "app")
    (root / "srv").mkdir()
    os.mkfifo(root / "srv" / "pipe")
    (root / "opt" / "link").symlink_to(app)
    started = time.monotonic()
    done = _run(shell, _scan_script(str(root), limit=5), path, scan_token, timeout=20)
    assert time.monotonic() - started < 4, done.stdout
    assert [ln for ln in done.stdout.split("\n") if ln.startswith("/")] == [str(app)]
    assert _rcs(done.stdout) == [0, 0], done.stdout


@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_a_directory_with_no_match_is_clean(tmp_path, scan_token, kind):
    """grep's "no match" (1) for a batch is no error: only a failure to read is."""
    from otto.coverage.objects import _scan_script

    shell, path = _tools(tmp_path, kind)
    root = tmp_path / "root"
    (root / "srv").mkdir(parents=True)
    (root / "srv" / "plain").write_text("plain\n")
    _hit(root / "opt" / "app")
    (root / "opt" / "readme").write_text("plain\n")
    done = _run(shell, _scan_script(str(root), limit=60), path, scan_token)
    assert _rcs(done.stdout) == [0, 0], done.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("kind", ["busybox", "gnu"])
@pytest.mark.parametrize("what", ["file", "directory"])
def test_an_unreadable_file_or_directory_is_a_scan_error(tmp_path, scan_token, kind, what):
    from otto.coverage.objects import _parse_scan, _scan_script

    shell, path = _tools(tmp_path, kind)
    root = tmp_path / "root"
    _hit(root / "opt" / "app")
    (root / "srv").mkdir()
    (root / "srv" / "plain").write_text("plain\n")
    locked = root / "srv" / ("secret" if what == "file" else "private")
    if what == "file":
        _hit(locked)
    else:
        _hit(locked / "inner")
    locked.chmod(0)
    try:
        done = _run(shell, _scan_script(str(root), limit=60), path, scan_token)
    finally:
        locked.chmod(0o700)
    parsed = _parse_scan(done.stdout)
    assert parsed.failed_dirs == [str(root / "srv")], done.stdout
    assert parsed.paths == [str(root / "opt" / "app")]


@pytest.mark.asyncio
async def test_any_nonzero_scan_status_for_a_directory_is_unreadable(tmp_path):
    """``find`` folds every failed batch into ``1``: no status but ``0`` is a clean directory."""
    host = _DockerHost(_TWO, answers={_EXEC: _scan(sorted(_TWO), rcs=("0:/bin", "1:/srv"))})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "under /srv" in result.reason


@pytest.mark.asyncio
async def test_a_temp_dir_whose_creation_result_was_lost_is_still_removed(tmp_path, token):
    """The remote mkdir ran, then the connection dropped: its exact path is still removed."""
    host = _DockerHost(_TWO, answers={_MKDIR: OSError("ssh: connection lost")})
    with pytest.raises(OSError, match="connection lost"):
        await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert host.issued(_RM) == [_RM_CMD]


@pytest.mark.asyncio
async def test_a_declined_temp_dir_creation_removes_nothing(tmp_path, token):
    from otto.result import NotRunResult
    from otto.utils import Status

    declined = NotRunResult(Status.NotRun, command="mkdir", msg="declined")
    host = _DockerHost(_TWO, answers={_MKDIR: declined})
    result = await ObjectListCache(tmp_path).read_running(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "dry-run"
    assert host.issued(_RM) == []


@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_the_temp_dir_is_private_and_never_reuses_an_existing_path(tmp_path, kind):
    """``mkdir -m 700`` refuses a path that exists, a planted symlink included."""
    from otto.coverage.objects import _make_temp_dir_command

    shell, path = _tools(tmp_path, kind)
    tmp = tmp_path / "tmp"
    tmp.mkdir()

    def make():
        return subprocess.run(
            [shell, "-c", _make_temp_dir_command("abc")],
            env={"PATH": path, "TMPDIR": str(tmp)},
            capture_output=True,
            text=True,
            check=False,
        )

    first = make()
    made = tmp / "otto-objlist.abc"
    assert (first.returncode, first.stdout.strip()) == (0, str(made))
    assert made.stat().st_mode & 0o777 == 0o700
    made.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    made.symlink_to(elsewhere)
    second = make()
    assert second.returncode != 0
    assert second.stdout == ""


def test_only_a_newline_ends_a_scanned_name():
    """``str.splitlines()`` would split a name at U+2028 or U+0085; the scan never does."""
    from otto.coverage.objects import _parse_scan

    name = "/opt/line" + chr(0x2028) + "sep" + chr(0x85) + "end"
    parsed = _parse_scan(f"{name}\r\n/opt/ok\notto-scan-rc:0:/opt\n")
    assert parsed.paths == [name, "/opt/ok"]


def _can_bind_mount() -> bool:
    """Return whether an unprivileged user may bind-mount in its own mount namespace."""
    import tempfile

    if shutil.which("unshare") is None:
        return False
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        done = subprocess.run(
            ["unshare", "-rm", "mount", "--bind", a, b], capture_output=True, check=False
        )
    return done.returncode == 0


@pytest.mark.skipif(not _can_bind_mount(), reason="no unprivileged mount namespace here")
@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_a_mounted_build_below_a_top_directory_is_scanned(tmp_path, scan_token, kind):
    """A build mounted into the container, or a VOLUME, holds the product's own files."""
    from otto.coverage.objects import _scan_script

    shell, path = _tools(tmp_path, kind)
    build = tmp_path / "build"
    _hit(build / "app")
    root = tmp_path / "root"
    (root / "opt" / "app").mkdir(parents=True)
    # a filesystem of its own, as a volume or a bind mount from another disk is
    target = shlex.quote(str(root / "opt" / "app"))
    mounted = (
        f"/bin/mount -t tmpfs none {target} && /bin/cp {shlex.quote(str(build / 'app'))} {target}/"
    )
    script = _scan_script(str(root), limit=60)
    unshare = shutil.which("unshare")
    assert unshare is not None
    done = subprocess.run(
        [unshare, "-rm", "/bin/sh", "-c", f'{mounted} && exec "$0" -c "$1" sh', shell, script],
        env={"PATH": path, "OTTO_SCAN_ID": scan_token},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert str(root / "opt" / "app" / "app") in done.stdout.split("\n"), done.stdout + done.stderr
    assert _rcs(done.stdout) == [0], done.stdout


@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_a_backslash_in_a_top_directory_name_cannot_swallow_the_next_line(
    tmp_path, scan_token, kind
):
    """dash's ``echo`` reads ``\\c`` as "stop here", gluing the next directory's line on."""
    from otto.coverage.objects import _parse_scan, _scan_script

    shell, path = _tools(tmp_path, kind)
    root = tmp_path / "root"
    (root / "a\\cb").mkdir(parents=True)
    app = _hit(root / "opt" / "app")
    done = _run(shell, _scan_script(str(root), limit=60), path, scan_token)
    assert done.stdout.split("\n") == [
        f"otto-scan-rc:0:{root}/a\\cb",
        str(app),
        f"otto-scan-rc:0:{root}/opt",
        "",
    ]
    assert _parse_scan(done.stdout).paths == [str(app)]


@pytest.mark.parametrize("kind", ["busybox", "gnu"])
def test_runtime_directories_are_not_scanned(tmp_path, scan_token, kind):
    """/tmp, /run and /var/run churn in a live container and hold no image binaries."""
    from otto.coverage.objects import _parse_scan, _scan_script

    shell, path = _tools(tmp_path, kind)
    root = tmp_path / "root"
    for skipped in ("tmp/a", "run/b", "var/run/c", "proc/d", "sys/e", "dev/f"):
        _hit(root / skipped)
    kept = [_hit(root / "opt" / "app"), _hit(root / "var" / "lib" / "g")]
    done = _run(shell, _scan_script(str(root), limit=60), path, scan_token)
    parsed = _parse_scan(done.stdout)
    assert sorted(parsed.paths) == sorted(str(p) for p in kept), done.stdout
    assert _rcs(done.stdout) == [0, 0], done.stdout
