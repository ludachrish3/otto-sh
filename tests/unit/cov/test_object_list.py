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

from otto.coverage.objects import (
    _SCAN_SCRIPT,
    ObjectList,
    ObjectListCache,
    Unavailable,
    coverage_stems,
)
from otto.host.docker_cli import _INSPECT_TEMPLATE

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


def test_a_reference_image_is_logged_at_debug_not_info(tmp_path, caplog):
    """The state is expected and needs no action, so only --log-level debug shows it."""
    with caplog.at_level(logging.INFO, logger="otto.coverage.objects"):
        ObjectListCache(tmp_path).read(_Img("redis:7", tarball=False))
    assert [r for r in caplog.records if "object list unavailable" in r.getMessage()] == []
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="otto.coverage.objects"):
        ObjectListCache(tmp_path).read(_Img("redis:7", tarball=False))
    (record,) = [r for r in caplog.records if "object list unavailable" in r.getMessage()]
    assert record.levelno == logging.DEBUG


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
        self.image = "redis:7"
        self.is_tarball = False
        self.container_name = container_name


_INSPECT = "docker container inspect"
_IMAGE_ID = "docker image inspect --format '{{.Id}}'"
_EXEC = "docker exec -e OTTO_SCAN_ID="
_REAP = "docker exec img-c"
_TOKEN = "f00d"
_MKDIR = 'd="${TMPDIR:-/tmp}/otto-objlist.'
_CP = "docker cp"
_RM = "rm -rf"
_HOST_TMP = f"/tmp/otto-objlist.{_TOKEN}"
_RM_CMD = f'rm -rf "${{TMPDIR:-/tmp}}/otto-objlist.{_TOKEN}"'
_PARENT_NOW = 1700000000
_CLOCK = "date +%s"
_SWEEP_PS = "docker ps -a --filter label=otto.scan=1"
_SWEEP_IMAGES = "docker image ls -q"
_COMMIT = "docker commit"
_CREATE = "docker create"
_START = "docker start -a"
_RM_SCAN = "docker rm -f -v otto-scan-"
_RMI_SCAN = "docker image rm -- otto-scan:"
_RM_SCAN_CMD = f"docker rm -f -v otto-scan-{_TOKEN}"
_RMI_SCAN_CMD = f"docker image rm -- otto-scan:{_TOKEN}"


def _state(status="running", image="sha256:feed", user="", groups=None, ref="redis:7"):
    """The daemon's answer to the one container inspect (otto.host.docker_cli)."""
    return json.dumps(
        {
            "status": status,
            "running": status == "running",
            "image": image,
            "ref": ref,
            "user": user,
            "groups": groups,
        }
    )


def _ok(value=""):
    from otto.result import Result
    from otto.utils import Status

    return Result(status=Status.Success, value=value)


def _failed(value="", msg="failed"):
    from otto.result import Result
    from otto.utils import Status

    return Result(status=Status.Error, value=value, msg=msg)


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
        self.timeouts: dict[str, float | None] = {}
        self.before_get = None
        defaults = {
            _INSPECT: _ok(_state()),
            _IMAGE_ID: _ok("sha256:feed"),
            _EXEC: _scan(sorted(container_files)),
            _REAP: _ok(),
            _MKDIR: _ok(_HOST_TMP + "\n"),
            _RM: _ok(),
            # the snapshot path's commands
            _CLOCK: _ok(f"{_PARENT_NOW}"),
            _SWEEP_PS: _ok(),
            _SWEEP_IMAGES: _ok(),
            _COMMIT: _ok("sha256:5eed"),
            _CREATE: _ok("c0ffee"),
            _START: _scan(sorted(container_files)),
            _RM_SCAN: _ok(),
            _RMI_SCAN: _ok(),
        }
        # A test's own answers are matched first, so a longer prefix of its own wins
        # over a default that is a prefix of it.
        self.answers = dict(answers or {})
        self.answers.update({k: v for k, v in defaults.items() if k not in self.answers})

    async def exec(self, cmd, timeout=None):
        """Answer by prefix: an exception is raised, a callable is awaited with the timeout."""
        self.calls.append(cmd)
        self.timeouts[cmd] = timeout
        if cmd.startswith(_CP):
            answer = self.answers.get(_CP)
            if callable(answer):
                await answer(cmd, timeout)
            return self._cp(cmd)
        for prefix, answer in self.answers.items():
            if cmd.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                if callable(answer):
                    return await answer(cmd, timeout)
                return answer
        raise AssertionError(f"unexpected command: {cmd}")

    def _cp(self, cmd):
        import shlex

        _docker, _cp, src, dest = shlex.split(cmd)
        answer = self.answers.get(_CP)
        if answer is not None and not callable(answer):
            return answer
        path = src.split(":", 1)[1]
        self.host_files[dest] = self.container_files[path]
        return _ok()

    async def get(self, src_files, dest_dir, user=None, show_progress=True, **_):
        from otto.result import Result
        from otto.utils import Status

        assert show_progress is False
        self.calls.append("GET")
        if self.before_get is not None:
            await self.before_get()
        if self.get_fails:
            return Result(status=Status.Error, value={}, msg="sftp: connection lost")
        got = {}
        for src in src_files:
            if self.host_files[str(src)] in self.get_drops:
                got[src] = _ok()  # reported fetched, never delivered
                continue
            (Path(dest_dir) / src.name).write_bytes(self.host_files[str(src)])
            got[src] = _ok()
        return Result(status=Status.Success, value=got)

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
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert host.issued(_INSPECT) == [
        f"docker container inspect --format {shlex.quote(_INSPECT_TEMPLATE)} img-c"
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
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert len(host.issued(_CP)) == 2  # the stray lines were never fetched


@pytest.mark.asyncio
async def test_a_grep_error_beside_hits_is_unreadable_and_not_cached(tmp_path):
    """A partial scan may miss an instrumented file: never an object list, never cached."""
    scan = _scan(sorted(_TWO), rcs=("0:/bin", "2:/opt", "0:/srv"))
    host = _DockerHost(_TWO, answers={_EXEC: scan})
    cache = ObjectListCache(tmp_path)
    first = await cache.read_container(_RefImg(), host)
    assert isinstance(first, Unavailable)
    assert first.kind == "unreadable"
    assert first.reason == (
        "could not scan container img-c: the scan exited 2 under /opt (an unreadable file "
        "or directory, no find or grep in the image, or the scan outran its 540s limit)"
    )
    assert host.issued(_CP) == []
    await cache.read_container(_RefImg(), host)
    assert len(host.issued(_EXEC)) == 2  # the failed scan was not cached


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "fails"])
async def test_a_failed_temp_dir_removal_keeps_the_scan_result_and_is_logged(
    tmp_path, caplog, failure, token
):
    answer = OSError("ssh: broken pipe") if failure == "raises" else _failed(msg="rm: busy")
    host = _DockerHost(_TWO, answers={_RM: answer})
    with caplog.at_level("WARNING"):
        result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"otto-objlist.{_TOKEN}" in w for w in warnings), warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "path"),
    [
        ("running", "exec"),
        ("restarting", "exec"),
        ("created", "snapshot"),
        ("exited", "snapshot"),
        ("paused", "snapshot"),
    ],
)
async def test_the_container_state_picks_the_path(tmp_path, token, status, path):
    host = _DockerHost(_TWO, answers={_INSPECT: _ok(_state(status))})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert result.source == "lab1:img-c"
    if path == "exec":
        assert len(host.issued(_EXEC)) == 1
        assert host.issued(_COMMIT) == []
    else:
        assert len(host.issued(_COMMIT)) == 1
        assert "--volumes-from img-c" in host.issued(_CREATE)[0]
        assert not [c for c in host.calls if c.startswith("docker exec")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["dead", "removing"])
async def test_a_dead_or_removing_container_is_unreadable_with_its_own_reason(tmp_path, status):
    host = _DockerHost(_TWO, answers={_INSPECT: _ok(_state(status))})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert result.reason == f"container img-c is {status}; remove it (`docker rm`) and reinstall"
    assert host.calls == host.issued(_INSPECT)


@pytest.mark.asyncio
async def test_any_other_status_is_unreadable_naming_it(tmp_path):
    host = _DockerHost(_TWO, answers={_INSPECT: _ok(_state("frozen"))})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert result.reason == "container img-c on lab1 is in state 'frozen', which otto cannot read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ["not json\n", json.dumps({"running": True, "image": "x", "ref": "y", "user": ""})],
    ids=["text", "no-status"],
)
async def test_an_unparseable_inspect_is_unreadable_and_scans_nothing(tmp_path, answer):
    host = _DockerHost(_TWO, answers={_INSPECT: _ok(answer)})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert result.reason == (
        "could not read the inspect of container img-c on lab1: unexpected output "
        f"{answer.strip()[:200]!r}"
    )
    assert host.calls == host.issued(_INSPECT)


_NO_CONTAINER = _failed("Error response from daemon: No such container: img-c", msg="")


@pytest.mark.asyncio
async def test_with_no_container_the_image_alone_is_scanned(tmp_path, token):
    host = _DockerHost(_TWO, answers={_INSPECT: _NO_CONTAINER})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert _paths(result) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert result.source == "lab1:redis:7"
    assert host.issued(_IMAGE_ID) == ["docker image inspect --format '{{.Id}}' redis:7"]
    (create,) = host.issued(_CREATE)
    assert "--volumes-from" not in create
    assert "--user" not in create
    assert create.endswith(f"--entrypoint sh sha256:feed -c {shlex.quote(_SCAN_SCRIPT)}")
    assert host.issued(_COMMIT) == []
    assert host.issued(_RMI_SCAN) == []  # the product's own image is never removed


@pytest.mark.asyncio
async def test_the_no_container_path_sweeps_and_labels_too(tmp_path, token):
    host = _DockerHost(_TWO, answers={_INSPECT: _NO_CONTAINER})
    await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert host.issued(_CLOCK) == [_CLOCK]
    assert len(host.issued(_SWEEP_PS)) == 1
    (create,) = host.issued(_CREATE)
    assert f"--label otto.scan=1 --label otto.scan.expires={_expires()} " in create
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]


@pytest.mark.asyncio
async def test_a_cache_hit_commits_nothing(tmp_path, token):
    host = _DockerHost(_TWO)
    cache = ObjectListCache(tmp_path)
    await cache.read_container(_RefImg("a", "a-c"), host)
    host.answers[_INSPECT] = _ok(_state("exited"))
    second = await cache.read_container(_RefImg("b", "b-c"), host)
    assert _paths(second) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    assert host.issued(_COMMIT) == []


@pytest.mark.asyncio
async def test_a_missing_image_is_image_missing_and_a_daemon_failure_is_unreadable(tmp_path):
    host = _DockerHost(
        _TWO,
        answers={
            _INSPECT: _NO_CONTAINER,
            _IMAGE_ID: _failed("Error response from daemon: No such image: redis:7", msg=""),
        },
    )
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "image-missing"
    assert result.reason == (
        "image redis:7 is not on lab1's docker daemon — install the product (or pull the "
        "image) first"
    )

    denied = "permission denied while trying to connect to the Docker daemon socket"
    host = _DockerHost(_TWO, answers={_INSPECT: _NO_CONTAINER, _IMAGE_ID: _failed(denied, msg="")})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert denied in result.reason

    # a local tarball that is not on disk keeps its own kind
    tarball = _Shell("t", tmp_path / "gone.tar")
    tarball.is_tarball = True
    assert ObjectListCache(tmp_path).read(tarball).kind == "missing"


@pytest.mark.asyncio
async def test_the_same_image_is_scanned_once(tmp_path):
    host = _DockerHost(_TWO)
    cache = ObjectListCache(tmp_path)
    first = await cache.read_container(_RefImg("a", "a-c"), host)
    second = await cache.read_container(_RefImg("b", "b-c"), host)
    assert _paths(first) == _paths(second)
    assert second.product == "b"
    assert len(host.issued(_EXEC)) == 1
    assert len(host.issued(_INSPECT)) == 2


@pytest.mark.asyncio
async def test_a_declined_image_inspect_is_a_dry_run(tmp_path):
    from otto.result import NotRunResult
    from otto.utils import Status

    declined = NotRunResult(status=Status.NotRun, command="docker image inspect", msg="declined")
    host = _DockerHost(_TWO, answers={_INSPECT: _NO_CONTAINER, _IMAGE_ID: declined})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "dry-run"


@pytest.mark.asyncio
async def test_a_dry_run_is_unavailable_and_issues_nothing_further(tmp_path):
    from otto.result import NotRunResult
    from otto.utils import Status

    declined = NotRunResult(
        status=Status.NotRun, command="docker container inspect", msg="declined"
    )
    host = _DockerHost(_TWO, answers={_INSPECT: declined})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "dry-run"
    assert host.calls == [host.issued(_INSPECT)[0]]


@pytest.mark.asyncio
async def test_a_grep_error_with_no_stems_is_unreadable_not_uninstrumented(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _scan([], rcs=(0, 1))})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "could not scan container img-c" in result.reason
    assert "the scan exited 1" in result.reason


@pytest.mark.asyncio
async def test_a_clean_scan_with_no_matches_is_not_instrumented(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _scan([], rcs=(0, 0))})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "not-instrumented"


@pytest.mark.asyncio
async def test_a_failed_scan_exec_is_unreadable(tmp_path):
    host = _DockerHost(_TWO, answers={_EXEC: _failed("OCI runtime exec failed: no sh")})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"


@pytest.mark.asyncio
async def test_a_failed_docker_cp_is_unreadable_naming_the_path(tmp_path, token):
    host = _DockerHost(_TWO, answers={_CP: _failed("Error: no such file")})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "/usr/bin/app" in result.reason
    assert host.issued(_RM) == [_RM_CMD]


@pytest.mark.asyncio
async def test_the_host_temp_dir_is_removed_when_the_get_fails(tmp_path, token):
    host = _DockerHost(_TWO, get_fails=True)
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
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
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert said in result.reason
    assert "img-c" in result.reason
    assert host.issued(_EXEC) == []


@pytest.mark.asyncio
async def test_a_file_the_get_reported_but_never_delivered_is_unreadable(tmp_path, token):
    host = _DockerHost(_TWO, get_drops=[_TWO["/usr/lib/libcore.so"]])
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
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
            await cache.read_container(_RefImg(), host)
    else:
        result = await cache.read_container(_RefImg(), host)
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
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(result, Unavailable)
    assert result.kind == "unreadable"
    assert "under /srv" in result.reason


@pytest.mark.asyncio
async def test_a_temp_dir_whose_creation_result_was_lost_is_still_removed(tmp_path, token):
    """The remote mkdir ran, then the connection dropped: its exact path is still removed."""
    host = _DockerHost(_TWO, answers={_MKDIR: OSError("ssh: connection lost")})
    with pytest.raises(OSError, match="connection lost"):
        await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert host.issued(_RM) == [_RM_CMD]


@pytest.mark.asyncio
async def test_a_declined_temp_dir_creation_removes_nothing(tmp_path, token):
    from otto.result import NotRunResult
    from otto.utils import Status

    declined = NotRunResult(status=Status.NotRun, command="mkdir", msg="declined")
    host = _DockerHost(_TWO, answers={_MKDIR: declined})
    result = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
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


# --- a stopped or paused container, read through a labelled snapshot ---------------------


def _exited(user="", groups=()):
    from otto.host.docker_cli import ContainerState

    return ContainerState("exited", False, "sha256:feed", "img:1", user, list(groups))


def _expires() -> int:
    from otto.coverage import objects

    return _PARENT_NOW + int(objects.SCAN_TOTAL_BUDGET + objects.SCAN_EXPIRY_MARGIN)


async def _snapshot(host, tmp_path, *, source="exited", image="sha256:feed", cache=None):
    """Drive ``_scan_snapshot`` the way the reader does, for the container ``img-c``."""
    cache = cache or ObjectListCache(tmp_path)
    state = _exited() if source == "exited" else source
    what = "container img-c" if state is not None else "image redis:7"
    return await cache._scan_snapshot(
        _RefImg(), host, source=state, image=image, container="img-c", what=what
    )


def _stall():
    """An answer that never comes and ignores its timeout: a stalled connection or channel."""
    import asyncio

    async def answer(cmd, timeout):
        await asyncio.Event().wait()

    return answer


def _held(event):
    """An answer that waits for *event*, then succeeds."""

    async def answer(cmd, timeout):
        await event.wait()
        return _ok()

    return answer


def _honouring(event):
    """An answer that waits for *event* but times out as a host does, after *timeout*."""
    import asyncio

    async def answer(cmd, timeout):
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{cmd}: no answer in {timeout}s") from None
        return _ok()

    return answer


def _create_cmd(*, volumes_from="img-c", user="''", groups=(), ref=f"otto-scan:{_TOKEN}"):
    from otto.coverage.objects import _SCAN_SCRIPT

    mounts = f" --volumes-from {volumes_from}" if volumes_from else ""
    who = f" --user {user}" if user is not None else ""
    added = "".join(f" --group-add {g}" for g in groups)
    return (
        f"docker create --name otto-scan-{_TOKEN} --label otto.scan=1 "
        f"--label otto.scan.expires={_expires()} --network none --no-healthcheck"
        f"{mounts}{who}{added} --entrypoint sh {ref} -c {shlex.quote(_SCAN_SCRIPT)}"
    )


@pytest.mark.asyncio
async def test_a_stopped_container_is_committed_created_started_and_copied_never_execd(
    tmp_path, token
):
    host = _DockerHost(_TWO)
    got = await _snapshot(host, tmp_path)
    assert sorted(got) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    commit = (
        "docker commit --pause=false --change 'LABEL otto.scan=1' "
        f"--change 'LABEL otto.scan.expires={_expires()}' img-c otto-scan:{_TOKEN}"
    )
    assert host.issued(_COMMIT) == [commit]
    assert host.issued(_CREATE) == [_create_cmd()]
    assert host.issued(_START) == [f"docker start -a otto-scan-{_TOKEN}"]
    copies = host.issued(_CP)
    assert len(copies) == 2
    assert all(c.startswith(f"docker cp otto-scan-{_TOKEN}:/usr/") for c in copies), copies
    assert not [c for c in host.calls if c.startswith("docker exec")]
    order = [host.calls.index(c) for c in (commit, _create_cmd(), host.issued(_START)[0])]
    assert order == sorted(order)
    assert order[-1] < host.calls.index(copies[0])
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [-(10**6), 10**6])
async def test_commit_and_create_carry_the_scan_labels_with_the_parents_expiry(
    tmp_path, token, monkeypatch, skew
):
    import time

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew)
    host = _DockerHost(_TWO)
    await _snapshot(host, tmp_path)
    (commit,) = host.issued(_COMMIT)
    (create,) = host.issued(_CREATE)
    assert "--change 'LABEL otto.scan=1'" in commit
    assert f"--change 'LABEL otto.scan.expires={_expires()}'" in commit
    assert "--label otto.scan=1 " in create
    assert f"--label otto.scan.expires={_expires()} " in create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "rendered"),
    [("", "''"), ("1000:1000", "1000:1000"), ("app user", "'app user'")],
    ids=["empty", "uid-gid", "named"],
)
async def test_the_user_and_groups_pass_through_quoted(tmp_path, token, user, rendered):
    host = _DockerHost(_TWO)
    await _snapshot(host, tmp_path, source=_exited(user=user, groups=["audio", "44"]))
    assert host.issued(_CREATE) == [_create_cmd(user=rendered, groups=["audio", "44"])]


def test_the_expiry_margin_covers_the_teardown():
    from otto.coverage import objects
    from otto.host.host import _EXEC_REAP_TIMEOUT

    worst = objects.SCAN_TEARDOWN_BUDGET + (3 + 1) * objects.SCAN_REAP_BOUND + 1
    assert worst + 30 <= objects.SCAN_EXPIRY_MARGIN
    assert objects.SCAN_REAP_BOUND >= 2 * _EXEC_REAP_TIMEOUT
    assert objects.SCAN_TEARDOWN_BUDGET / 3 == objects._TEARDOWN_STEP_CAP


@pytest.mark.asyncio
async def test_the_scan_sweeps_through_the_shared_sweep_first(tmp_path, token, monkeypatch):
    from otto.host import docker_cli

    host = _DockerHost(_TWO)
    seen = []

    async def recorder(swept_host, *, now=None):
        seen.append((swept_host, now, list(host.calls)))
        return docker_cli.SweepReport([], [])

    monkeypatch.setattr(docker_cli, "sweep_stale_scans", recorder)
    await _snapshot(host, tmp_path)
    assert len(seen) == 1
    swept_host, now, before = seen[0]
    assert (swept_host, now) == (host, _PARENT_NOW)
    assert not [c for c in before if c.startswith(_COMMIT)]


@pytest.mark.asyncio
async def test_exceeding_the_total_budget_aborts_tears_down_and_is_unreadable(
    tmp_path, token, monkeypatch
):
    import asyncio

    from otto.coverage import objects

    monkeypatch.setattr(objects, "SCAN_TOTAL_BUDGET", 0.05)

    async def slow(cmd, timeout):
        await asyncio.sleep(1)
        return _scan(sorted(_TWO))

    host = _DockerHost(_TWO, answers={_START: slow})
    got = await _snapshot(host, tmp_path)
    assert isinstance(got, Unavailable)
    assert got.kind == "unreadable"
    assert got.reason == "the scan of container img-c outran its 0-second budget"
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["mkdir", "cp", "get"])
async def test_a_step_timing_out_well_inside_the_budget_is_that_steps_timeout(
    tmp_path, token, step
):
    """Only the total deadline says "outran its budget"; a fetch step's own timeout says so.

    The fetch's commands are the reader's shared ones, not the sequence's
    ``_run_step``: their own TimeoutError (``asyncio.TimeoutError`` on 3.10,
    the builtin later) must never read as the budget's.
    """
    lost = TimeoutError("ssh: no answer")

    async def raising(cmd, timeout):
        raise lost

    async def raising_get():
        raise lost

    answers = {"mkdir": {_MKDIR: lost}, "cp": {_CP: raising}, "get": {}}[step]
    host = _DockerHost(_TWO, answers=answers)
    if step == "get":
        host.before_get = raising_get
    got = await _snapshot(host, tmp_path)
    assert isinstance(got, Unavailable), got
    assert got.kind == "unreadable"
    assert "budget" not in got.reason, got.reason
    assert got.reason == (
        f"a step of the scan of container img-c on {host.id} timed out: ssh: no answer"
    )
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "scan-failure", "timeout", "cancellation"])
async def test_teardown_runs_both_removals(tmp_path, token, ending):
    import asyncio

    started = asyncio.Event()

    async def cancelled_start(cmd, timeout):
        started.set()
        await asyncio.Event().wait()

    start = {
        "success": _scan(sorted(_TWO)),
        "scan-failure": _scan([], rcs=("2:/opt",)),
        "timeout": TimeoutError("ssh: no answer"),
        "cancellation": cancelled_start,
    }[ending]
    host = _DockerHost(_TWO, answers={_START: start})
    task = asyncio.ensure_future(_snapshot(host, tmp_path))
    if ending == "cancellation":
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
async def test_when_only_create_succeeded_both_removals_still_run(tmp_path, token):
    host = _DockerHost(_TWO, answers={_START: _failed("cannot start", msg="")})
    got = await _snapshot(host, tmp_path)
    assert isinstance(got, Unavailable)
    assert got.reason == "could not scan container img-c: cannot start"
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["failed", "partial"])
async def test_a_failed_image_only_scan_names_the_image_not_a_container(tmp_path, token, answer):
    """It names the image the product declares, as every other image-path reason does."""
    start = {"failed": _failed("cannot start", msg=""), "partial": _scan([], rcs=("2:/opt",))}
    host = _DockerHost(_TWO, answers={_INSPECT: _NO_CONTAINER, _START: start[answer]})
    got = await ObjectListCache(tmp_path).read_container(_RefImg(), host)
    assert isinstance(got, Unavailable), got
    assert got.reason.startswith("could not scan image redis:7: "), got.reason
    assert "img-c" not in got.reason
    assert "sha256:" not in got.reason


@pytest.mark.asyncio
async def test_a_commit_whose_reply_was_lost_still_removes_the_tag(tmp_path, token):
    host = _DockerHost(_TWO, answers={_COMMIT: ConnectionResetError("reply lost")})
    with pytest.raises(ConnectionResetError):
        await _snapshot(host, tmp_path)
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]
    assert host.issued(_CREATE) == []
    assert host.issued(_RM_SCAN) == []  # nothing was created, so nothing names a container


@pytest.mark.asyncio
async def test_a_failing_rm_never_skips_the_rmi(tmp_path, token, caplog):
    host = _DockerHost(_TWO, answers={_RM_SCAN: _failed("permission denied", msg="")})
    with caplog.at_level("WARNING"):
        got = await _snapshot(host, tmp_path)
    assert sorted(got) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"otto-scan-{_TOKEN}" in w and "permission denied" in w for w in warnings)
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
async def test_a_cancel_during_the_rm_still_reaches_the_rmi(tmp_path, token):
    import asyncio

    release = asyncio.Event()
    in_rm = asyncio.Event()

    async def rm(cmd, timeout):
        in_rm.set()
        await release.wait()
        return _ok()

    host = _DockerHost(_TWO, answers={_RM_SCAN: rm})
    task = asyncio.ensure_future(_snapshot(host, tmp_path))
    await in_rm.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert host.issued(_RMI_SCAN) == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
async def test_a_missing_scan_tag_is_quiet_and_a_daemon_failure_is_logged(tmp_path, token, caplog):
    gone = _failed(f"Error response from daemon: No such image: otto-scan:{_TOKEN}", msg="")
    host = _DockerHost(_TWO, answers={_RMI_SCAN: gone})
    with caplog.at_level("WARNING"):
        await _snapshot(host, tmp_path)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    caplog.clear()
    down = _failed("Cannot connect to the Docker daemon", msg="")
    host = _DockerHost(_TWO, answers={_RMI_SCAN: down})
    with caplog.at_level("WARNING"):
        await _snapshot(host, tmp_path)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Cannot connect to the Docker daemon" in warnings[0]


@pytest.mark.asyncio
async def test_the_fetch_temp_dir_is_removed_under_cancellation(tmp_path, token):
    import asyncio

    in_get = asyncio.Event()

    async def hold():
        in_get.set()
        await asyncio.Event().wait()

    host = _DockerHost(_TWO)
    host.before_get = hold
    task = asyncio.ensure_future(_snapshot(host, tmp_path))
    await in_get.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tail = host.calls[host.calls.index("GET") + 1 :]
    assert tail == [_RM_CMD, _RM_SCAN_CMD, _RMI_SCAN_CMD]


def _cleanup_timeouts(host) -> list[float]:
    return [
        host.timeouts[c]
        for c in host.calls
        if c in (_RM_CMD, _RM_SCAN_CMD, _RMI_SCAN_CMD) and host.timeouts.get(c) is not None
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["snapshot", "exec"])
async def test_the_cleanup_steps_share_one_budget(tmp_path, token, monkeypatch, caplog, path):
    import asyncio

    from otto import lifecycle
    from otto.coverage import objects

    monkeypatch.setattr(objects, "SCAN_TEARDOWN_BUDGET", 0.3)
    monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 0.1)
    entered = []
    real = lifecycle.compensate

    async def counting(coro, **kw):
        entered.append(kw.get("what"))
        return await real(coro, **kw)

    monkeypatch.setattr(lifecycle, "compensate", counting)
    host = _DockerHost(_TWO, answers={_RM: _honouring(asyncio.Event())})
    cache = ObjectListCache(tmp_path)
    if path == "snapshot":
        await _snapshot(host, tmp_path, cache=cache)
        assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
        assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]
    else:
        await cache._scan_container(_RefImg(), host, "img-c")
        assert host.issued(_RM) == [_RM_CMD]
    granted = _cleanup_timeouts(host)
    assert granted, host.calls
    assert all(g <= objects._TEARDOWN_STEP_CAP + 1e-9 for g in granted), granted
    assert sum(granted) <= objects.SCAN_TEARDOWN_BUDGET + 1e-9, granted
    assert len(entered) == 1, entered

    if path == "exec":
        return
    # Spent before the rmi: the first two steps each hold until their timeout.
    monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 0.2)
    host = _DockerHost(
        _TWO,
        answers={_RM: _honouring(asyncio.Event()), _RM_SCAN: _honouring(asyncio.Event())},
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        await _snapshot(host, tmp_path)
    granted = _cleanup_timeouts(host)
    assert sum(granted) <= objects.SCAN_TEARDOWN_BUDGET + 1e-9, granted
    assert host.issued(_RMI_SCAN) == []
    left = [r.getMessage() for r in caplog.records if "left for the expiry sweep" in r.getMessage()]
    assert [m for m in left if f"otto-scan:{_TOKEN}" in m and "budget" in m], left


@pytest.mark.asyncio
async def test_a_stalled_connection_or_channel_is_cut_off_by_wait_for(
    tmp_path, token, monkeypatch, caplog
):
    import asyncio

    from otto.coverage import objects

    monkeypatch.setattr(objects, "SCAN_TEARDOWN_BUDGET", 0.3)
    monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 0.1)
    host = _DockerHost(_TWO, answers={_RM: _stall()})
    res = objects._ScanResources(_TOKEN, staging=True, container=True, snapshot=True)
    loop = asyncio.get_running_loop()
    began = loop.time()
    with caplog.at_level("WARNING"):
        await ObjectListCache(tmp_path)._scan_cleanup(_RefImg(), host, res)
    assert loop.time() - began <= objects.SCAN_TEARDOWN_BUDGET + 0.1
    failed = [r.getMessage() for r in caplog.records if "teardown failed" in r.getMessage()]
    assert len(failed) == 1, failed
    assert "otto-objlist." in failed[0], failed
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
async def test_a_stalled_sequence_is_cut_off_and_the_cleanup_still_runs(
    tmp_path, token, monkeypatch
):
    from otto.coverage import objects

    monkeypatch.setattr(objects, "SCAN_TOTAL_BUDGET", 0.1)
    host = _DockerHost(_TWO, answers={_START: _stall()})
    got = await _snapshot(host, tmp_path)
    assert isinstance(got, Unavailable)
    assert got.reason == "the scan of container img-c outran its 0-second budget"
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
async def test_external_cancellation_propagates_through_the_wait_for(tmp_path, token):
    import asyncio

    started = asyncio.Event()

    async def stalled(cmd, timeout):
        started.set()
        await asyncio.Event().wait()

    host = _DockerHost(_TWO, answers={_START: stalled})
    task = asyncio.ensure_future(_snapshot(host, tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["step", "backstop"])
async def test_a_cleanup_timeout_keeps_a_successful_scan(
    tmp_path, token, monkeypatch, caplog, bound
):
    from otto.coverage import objects

    if bound == "step":
        monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 0.1)
    else:
        # A backstop of 0.1 s against steps that may each take 1 s: it fires during the rm.
        monkeypatch.setattr(objects, "SCAN_TEARDOWN_BUDGET", 1.0)
        monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 1.0)
        monkeypatch.setattr(objects, "SCAN_REAP_BOUND", -0.3)
    host = _DockerHost(_TWO, answers={_RM_SCAN: _stall()})
    with caplog.at_level("WARNING"):
        got = await _snapshot(host, tmp_path)
    assert sorted(got) == ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c"]
    left = [r.getMessage() for r in caplog.records if "left for the expiry sweep" in r.getMessage()]
    assert [m for m in left if f"otto-scan-{_TOKEN}" in m], [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_a_cancelled_scan_whose_cleanup_outruns_its_budget_names_what_is_left(
    tmp_path, token, monkeypatch, caplog
):
    import asyncio

    from otto.coverage import objects

    # The backstop fires during the stalled rm, as in the "backstop" case above.
    monkeypatch.setattr(objects, "SCAN_TEARDOWN_BUDGET", 1.0)
    monkeypatch.setattr(objects, "_TEARDOWN_STEP_CAP", 1.0)
    monkeypatch.setattr(objects, "SCAN_REAP_BOUND", -0.3)
    started = asyncio.Event()

    async def stalled(cmd, timeout):
        started.set()
        await asyncio.Event().wait()

    host = _DockerHost(_TWO, answers={_START: stalled, _RM_SCAN: _stall()})
    task = asyncio.ensure_future(_snapshot(host, tmp_path))
    await started.wait()
    with caplog.at_level("WARNING"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    left = [r.getMessage() for r in caplog.records if "left for the expiry sweep" in r.getMessage()]
    assert len(left) == 1, [r.getMessage() for r in caplog.records]
    assert f"otto-scan-{_TOKEN}" in left[0]  # the container
    assert f"otto-scan:{_TOKEN}" in left[0]  # the snapshot tag


@pytest.mark.asyncio
async def test_the_total_deadline_does_not_cut_the_cleanup(tmp_path, token, monkeypatch):
    from otto.coverage import objects

    monkeypatch.setattr(objects, "SCAN_TOTAL_BUDGET", 0.05)
    host = _DockerHost(_TWO, answers={_START: _stall()})
    got = await _snapshot(host, tmp_path)  # nothing escapes: no CancelledError, no TimeoutError
    assert isinstance(got, Unavailable)
    assert got.reason == "the scan of container img-c outran its 0-second budget"
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]
    # Each step had its own cap, never what was left of the spent total.
    for cmd in (_RM_SCAN_CMD, _RMI_SCAN_CMD):
        assert host.timeouts[cmd] == pytest.approx(objects._TEARDOWN_STEP_CAP, abs=1.0)


_ANSWERS = {
    "not-run": "NOTRUN",
    "failed": _failed("exec failed", msg=""),
    "parse-errors": _scan(["/opt/app"], rcs=("0:/bin", "2:/opt")),
    "empty": _scan([]),
    "two-files": _scan(sorted(_TWO)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_ANSWERS))
async def test_one_scan_answer_reads_the_same_through_both_paths(tmp_path, token, case):
    from otto.result import NotRunResult
    from otto.utils import Status

    answer = _ANSWERS[case]
    if answer == "NOTRUN":
        answer = NotRunResult(status=Status.NotRun, command="scan", retcode=-1, host_name="lab1")
    exec_host = _DockerHost(_TWO, answers={_EXEC: answer})
    by_exec = await ObjectListCache(tmp_path)._scan_container(_RefImg(), exec_host, "img-c")
    snap_host = _DockerHost(_TWO, answers={_START: answer})
    by_snapshot = await _snapshot(snap_host, tmp_path)
    if isinstance(by_exec, Unavailable):
        assert isinstance(by_snapshot, Unavailable)
        assert (by_snapshot.kind, by_snapshot.reason) == (by_exec.kind, by_exec.reason)
    else:
        assert sorted(by_snapshot) == sorted(by_exec)


@pytest.mark.asyncio
async def test_the_scan_removes_through_the_shared_builders(tmp_path, token):
    from otto.host.docker_cli import image_rm_command, rm_container_command

    host = _DockerHost(_TWO)
    await _snapshot(host, tmp_path)
    assert host.issued(_RM_SCAN) == [rm_container_command(f"otto-scan-{_TOKEN}", force=True)]
    assert host.issued(_RMI_SCAN) == [image_rm_command([f"otto-scan:{_TOKEN}"])]


@pytest.mark.asyncio
async def test_a_mounted_build_below_a_top_directory_is_scanned_through_the_stopped_path(
    tmp_path, token
):
    mounted = {"/opt/build/app": ELF + b"\x00/src/build/app.c.gcda\x00"}
    host = _DockerHost(mounted)
    got = await _snapshot(host, tmp_path)
    assert got == ["/src/build/app.c"]
    assert "--volumes-from img-c" in host.issued(_CREATE)[0]
    assert host.issued(_CP) == [f"docker cp otto-scan-{_TOKEN}:/opt/build/app {_HOST_TMP}/0"]


# --- two scans against one daemon ------------------------------------------------------


class _StatefulDaemon:
    """One docker parent shared by concurrent scans: labelled resources, a settable clock.

    A scan's commit or copy-out can be held on an event per token, and a cleanup
    step on another, so a test can line two scans up against each other.
    """

    def __init__(self, files):
        self.files = files
        self.now = _PARENT_NOW
        self.containers: dict[str, int] = {}  # name -> expires
        self.images: dict[str, int] = {}  # tag -> expires
        self.host_files: dict[str, bytes] = {}
        self.holds: dict[str, object] = {}  # "<step>:<token>" -> awaitable factory
        self.id = "lab1"
        self.calls: list[str] = []

    async def _hold(self, step, cmd):
        for key, hold in list(self.holds.items()):
            name, _, tok = key.partition(":")
            if name == step and tok in cmd:
                await hold()

    async def exec(self, cmd, timeout=None):  # noqa: PLR0911 — one answer per docker command
        import re

        self.calls.append(cmd)
        if cmd == _CLOCK:
            return _ok(f"{self.now}")
        if cmd.startswith(_SWEEP_PS):
            return _ok("\n".join(f"{n} {e}" for n, e in self.containers.items()))
        if cmd.startswith(_SWEEP_IMAGES):
            return _ok("\n".join(self.images))
        if cmd.startswith("docker image inspect"):
            ids = shlex.split(cmd)[5:]
            return _ok("\n".join(f"{i} {self.images[i]}" for i in ids if i in self.images))
        expires = re.search(r"otto\.scan\.expires=(\d+)", cmd)
        if cmd.startswith(_COMMIT):
            tag = shlex.split(cmd)[-1]
            self.images[tag] = int(expires.group(1))
            await self._hold("commit", cmd)
            return _ok("sha256:5eed")
        if cmd.startswith(_CREATE):
            self.containers[shlex.split(cmd)[3]] = int(expires.group(1))
            return _ok("c0ffee")
        if cmd.startswith(_START):
            return _scan(sorted(self.files))
        if cmd.startswith(_MKDIR):
            tok = cmd.split("otto-objlist.", 1)[1].split('"', 1)[0]
            return _ok(f"/tmp/otto-objlist.{tok}")
        if cmd.startswith(_CP):
            await self._hold("cp", cmd)
            _docker, _cp, src, dest = shlex.split(cmd)
            self.host_files[dest] = self.files[src.split(":", 1)[1]]
            return _ok()
        if cmd.startswith(_RM):
            await self._hold("rm-rf", cmd)
            return _ok()
        if cmd.startswith("docker rm -f -v "):
            await self._hold("rm", cmd)
            name = shlex.split(cmd)[-1]
            if self.containers.pop(name, None) is None:
                return _failed(f"Error response from daemon: No such container: {name}", msg="")
            return _ok(name)
        if cmd.startswith("docker image rm -- "):
            tag = shlex.split(cmd)[-1]
            if self.images.pop(tag, None) is None:
                return _failed(f"Error response from daemon: No such image: {tag}", msg="")
            return _ok(f"Untagged: {tag}")
        raise AssertionError(f"unexpected command: {cmd}")

    async def get(self, src_files, dest_dir, user=None, show_progress=True, **_):
        from otto.result import Result
        from otto.utils import Status

        got = {}
        for src in src_files:
            (Path(dest_dir) / src.name).write_bytes(self.host_files[str(src)])
            got[src] = _ok()
        return Result(status=Status.Success, value=got)


@pytest.fixture
def two_tokens(monkeypatch):
    tokens = iter(["aaaa", "bbbb", "cccc", "dddd"])
    monkeypatch.setattr("otto.coverage.objects._scan_token", lambda: next(tokens))


def _held_until(event, entered=None):
    async def hold():
        if entered is not None:
            entered.set()
        await event.wait()

    return hold


@pytest.mark.asyncio
@pytest.mark.parametrize("held_in", ["commit", "cp"])
async def test_overlapping_scans_never_sweep_each_other(tmp_path, two_tokens, held_in):
    import asyncio

    from otto.coverage import objects

    files = {**_TWO, "/usr/bin/tool": ELF + b"\x00/build/tool/t.c.gcda\x00"}
    daemon = _StatefulDaemon(files)
    release, entered = asyncio.Event(), asyncio.Event()
    daemon.holds[f"{held_in}:aaaa"] = _held_until(release, entered)
    a = asyncio.ensure_future(_snapshot(daemon, tmp_path))
    await entered.wait()
    # B starts as late as A's own total budget allows A to still be running.
    daemon.now = _PARENT_NOW + int(objects.SCAN_TOTAL_BUDGET) - 1
    b = await _snapshot(daemon, tmp_path)
    assert sorted(b) == sorted(
        ["/build/app/main.c", "/build/app/util.c", "/build/core/core.c", "/build/tool/t.c"]
    )
    assert "otto-scan:aaaa" in daemon.images, "B's sweep removed A's live snapshot"
    if held_in == "cp":
        assert "otto-scan-aaaa" in daemon.containers, "B's sweep removed A's live container"
    release.set()
    assert sorted(await a) == sorted(b)
    assert (daemon.containers, daemon.images) == ({}, {})
    for tok in ("aaaa", "bbbb"):
        assert daemon.calls.count(f"docker rm -f -v otto-scan-{tok}") == 1
        assert daemon.calls.count(f"docker image rm -- otto-scan:{tok}") == 1


@pytest.mark.asyncio
async def test_a_scan_held_in_its_cleanup_at_the_budget_edge_is_never_swept(
    tmp_path, two_tokens, monkeypatch
):
    import asyncio

    from otto.coverage import objects
    from otto.host.docker_cli import sweep_stale_scans

    monkeypatch.setattr(objects, "SCAN_TOTAL_BUDGET", 0.2)
    daemon = _StatefulDaemon(_TWO)
    in_rm_rf, rm_rf_go = asyncio.Event(), asyncio.Event()
    in_rm, rm_go = asyncio.Event(), asyncio.Event()
    in_cp = asyncio.Event()
    daemon.holds["cp:aaaa"] = _held_until(asyncio.Event(), in_cp)  # never released: A times out
    daemon.holds["rm-rf:aaaa"] = _held_until(rm_rf_go, in_rm_rf)
    daemon.holds["rm:aaaa"] = _held_until(rm_go, in_rm)
    a = asyncio.ensure_future(_snapshot(daemon, tmp_path))
    await in_cp.wait()
    daemon.now = _PARENT_NOW + int(objects.SCAN_TOTAL_BUDGET)
    await in_rm_rf.wait()  # A's deadline fired mid-copy-out; its cleanup is held
    # The worst-case end of A's cleanup, on the parent's clock.
    daemon.now = _PARENT_NOW + int(objects.SCAN_TOTAL_BUDGET + objects.SCAN_TEARDOWN_BUDGET) + 1
    del daemon.holds["cp:aaaa"]
    b = await _snapshot(daemon, tmp_path)
    assert not isinstance(b, Unavailable), b
    assert "otto-scan:aaaa" in daemon.images
    assert "otto-scan-aaaa" in daemon.containers
    rm_rf_go.set()
    await in_rm.wait()
    assert "otto-scan-aaaa" in daemon.containers
    rm_go.set()
    got = await a
    assert isinstance(got, Unavailable)
    assert got.reason == "the scan of container img-c outran its 0-second budget"
    assert (daemon.containers, daemon.images) == ({}, {})

    # Had A's cleanup never run, a sweep at its expiry would have removed what it left.
    daemon.images["otto-scan:aaaa"] = _PARENT_NOW + int(
        objects.SCAN_TOTAL_BUDGET + objects.SCAN_EXPIRY_MARGIN
    )
    daemon.containers["otto-scan-aaaa"] = daemon.images["otto-scan:aaaa"]
    daemon.now = daemon.images["otto-scan:aaaa"] - 1
    assert (await sweep_stale_scans(daemon)).removed == []
    daemon.now += 1
    assert (await sweep_stale_scans(daemon)).removed == [
        "container otto-scan-aaaa",
        "image otto-scan:aaaa",
    ]


@pytest.mark.asyncio
async def test_no_cleanup_step_starts_once_the_teardown_deadline_has_expired(
    tmp_path, token, caplog
):
    """The held cancellation arms the deadline at entry; it expires during the
    first step, so the container and snapshot removals never start and the
    warning names both (before: both started after expiry, each with up to
    its 40 s share, and the abandonment waited for them)."""
    import asyncio

    from otto.coverage import objects
    from otto.invocation import RunPolicy, install_policy, reset_binding

    host = _DockerHost(_TWO, answers={_RM: _stall()})
    res = objects._ScanResources(_TOKEN, staging=True, container=True, snapshot=True)
    binding = install_policy(RunPolicy(teardown_deadline=0))
    try:
        with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
            # A bound that only turns a hang into a failure, never a pass.
            await asyncio.wait_for(
                ObjectListCache(tmp_path)._cleanup_once(
                    _RefImg(), host, res, held=asyncio.CancelledError()
                ),
                5,
            )
    finally:
        reset_binding(binding)
    assert host.issued(_RM_SCAN) == []
    assert host.issued(_RMI_SCAN) == []
    left = [r.getMessage() for r in caplog.records if "left for the expiry sweep" in r.getMessage()]
    assert len(left) == 1, [r.getMessage() for r in caplog.records]
    assert f"otto-scan-{_TOKEN}" in left[0]
    assert f"otto-scan:{_TOKEN}" in left[0]


@pytest.mark.asyncio
async def test_a_cleanup_that_removed_everything_under_a_held_cancellation_warns_nothing(
    tmp_path, token, caplog
):
    """A held cancellation re-raises after a successful cleanup; that is no
    abandonment (before: both resources were named as left for the sweep)."""
    import asyncio

    from otto.coverage import objects

    host = _DockerHost(_TWO)
    res = objects._ScanResources(_TOKEN, staging=True, container=True, snapshot=True)
    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await ObjectListCache(tmp_path)._cleanup_once(
            _RefImg(), host, res, held=asyncio.CancelledError()
        )
    assert host.issued(_RM_SCAN) == [_RM_SCAN_CMD]
    assert host.issued(_RMI_SCAN) == [_RMI_SCAN_CMD]
    assert [r.getMessage() for r in caplog.records if r.levelno >= 30] == []  # WARNING
