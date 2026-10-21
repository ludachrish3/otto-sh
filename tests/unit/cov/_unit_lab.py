"""A fake lab whose products own project files, for the unit tier's reporter tests.

A product here is a small dataclass with the attributes the unit preflight
reads; its artifact embeds one coverage path per source, with a gcc 13 notes
file beside each stem naming that source, so the real object-list scan and
notes reader see a real product build.
"""

import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.coverage.merge.gcovfile import gcno_sources
from tests.unit.cov._gcno import clang_gcno, gcc13_gcno


@dataclass
class FakeProduct:
    name: str
    owner: str | None = "proj"
    artifact: str | None = None
    is_tarball: bool | None = None
    instrumented_override: bool | None = None
    kind: str = "shell"
    container_name: str = ""


@dataclass
class FakeHost:
    id: str
    products: list[Any] = field(default_factory=list)
    toolchain: Any = None


@dataclass
class ContainerHost(FakeHost):
    """A docker daemon host: its container lists *files*, which ``docker cp`` and ``get`` copy.

    ``running`` answers the inspect; ``scan``, when set, replaces the scan's answer.
    """

    running: bool = True
    files: dict[str, bytes] = field(default_factory=dict)
    scan: Any = None
    calls: list[str] = field(default_factory=list)
    staged: dict[str, bytes] = field(default_factory=dict)

    async def exec(self, cmd: str, timeout: float | None = None) -> Any:
        from otto.result import Result
        from otto.utils import Status

        self.calls.append(cmd)
        if cmd.startswith("docker container inspect"):
            return Result(Status.Success, value=f"{str(self.running).lower()} sha256:feed\n")
        if cmd.startswith("docker exec") and " OTTO_SCAN_ID=" not in cmd.split(" sh -c ", 1)[0]:
            return Result(Status.Success, value="")  # the reap of an unclean scan
        if cmd.startswith("docker exec"):
            if self.scan is not None:
                return self.scan
            listed = "".join(f"{p}\n" for p in self.files)
            return Result(Status.Success, value=listed + "otto-scan-rc:0\n")
        if cmd.startswith('d="${TMPDIR:-/tmp}/otto-objlist.'):
            return Result(Status.Success, value="/tmp/otto-objlist.x\n")
        if cmd.startswith("docker cp"):
            _docker, _cp, src, dest = shlex.split(cmd)
            self.staged[dest] = self.files[src.split(":", 1)[1]]
            return Result(Status.Success, value="")
        if cmd.startswith("rm -rf"):
            return Result(Status.Success, value="")
        raise AssertionError(f"unexpected command: {cmd}")

    async def get(self, src_files: list[Path], dest_dir: Path, **_: Any) -> Any:
        from otto.result import Result
        from otto.utils import Status

        for src in src_files:
            (dest_dir / src.name).write_bytes(self.staged[str(src)])
        return Result(Status.Success, value={src: Result(Status.Success) for src in src_files})


def reference_image(name: str = "img", container: str = "img-c") -> FakeProduct:
    """Return a ``docker_image`` product named by reference, run as *container*."""
    return FakeProduct(
        name=name,
        artifact="registry.example/img:1",
        is_tarball=False,
        kind="docker_image",
        container_name=container,
    )


@dataclass
class FakeLab:
    hosts: dict[str, Any]
    name: str = "bench"


@dataclass
class FakeContext:
    lab: FakeLab


def install_lab(monkeypatch, hosts: list[Any]) -> FakeContext:
    """Make *hosts* the active lab (patched at the defining module)."""
    ctx = FakeContext(FakeLab(hosts={h.id: h for h in hosts}))
    monkeypatch.setattr("otto.run_context.try_get_context", lambda: ctx)
    return ctx


def write_sources(root: Path, rels: list[str]) -> None:
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        if not (root / rel).exists():
            (root / rel).write_text("int a;\nint b;\nint c;\n")


def product_build(
    root: Path, name: str, sources: list[str], *, owner: str | None = "proj"
) -> FakeProduct:
    """Build a product *name* from *sources*: its artifact and the notes beside each stem."""
    write_sources(root, sources)
    out = root / "build" / name
    stems = []
    for rel in sources:
        stem = out / rel
        stem.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{stem}.gcno").write_bytes(gcc13_gcno(str(root), [str(root / rel)]))
        stems.append(f"{stem}.gcda".encode())
    artifact = out / f"{name}.bin"
    artifact.write_bytes(b"\0" + b"\0".join(stems) + b"\0")
    return FakeProduct(name=name, owner=owner, artifact=str(artifact))


def _notes(compiler: str, root: Path, rel: str) -> bytes:
    if compiler == "clang":
        return clang_gcno(str(root / rel), [])
    gcc13 = gcc13_gcno(str(root), [str(root / rel)])
    if compiler == "gcc13":
        return gcc13
    if compiler == "gcc12":
        return gcc13[:4] + b"*42B" + gcc13[8:]  # gcc 12 writes the same layout
    raise ValueError(compiler)


def unit_build(hdir: Path, root: Path, sources: list[str], compiler: str = "gcc13") -> None:
    """Write one notes file and one (empty) counters file per unit source under *hdir*.

    *compiler* is the notes' stamp: ``gcc13``, ``gcc12`` or ``clang``.
    """
    write_sources(root, sources)
    hdir.mkdir(parents=True, exist_ok=True)
    for rel in sources:
        base = hdir / Path(rel).name
        Path(f"{base}.gcno").write_bytes(_notes(compiler, root, rel))
        Path(f"{base}.gcda").write_bytes(b"")


def stub_tool(bin_dir: Path, name: str, version: str = "") -> Path:
    """Write an executable *name* under *bin_dir* that prints *version* and exits 0."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_text(
        "#!/bin/sh\n" + (f"echo {shlex.quote(version)}\n" if version else "") + "exit 0\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def stub_unit_tools(monkeypatch, bin_dir: Path, *, only: bool = False) -> Path:
    """Put a stub ``gcov`` (13.3.0) and ``lcov`` under *bin_dir* first on ``PATH``.

    The unit preflight resolves the default tools from ``PATH`` before any
    capture, so a test that fakes the capture still needs them there — CI
    installs no lcov. With *only*, ``PATH`` is *bin_dir* alone.
    """
    stub_tool(bin_dir, "gcov", "gcov (stub) 13.3.0")
    stub_tool(bin_dir, "lcov")
    path = str(bin_dir) if only else f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", path)
    return bin_dir


def own_files(
    monkeypatch, root: Path, sources: list[str], *, owner: str | None = "proj"
) -> FakeProduct:
    """Install a lab with one product ``core`` built from *sources*; return it."""
    core = product_build(root, "core", sources, owner=owner)
    install_lab(monkeypatch, [FakeHost("h1", [core])])
    return core


def install_capture(monkeypatch) -> list[dict[str, Any]]:
    """Fake ``LcovMerger.capture_dir``: record each call, write one record per named source."""
    from otto.coverage.merge import merger as merger_mod

    calls: list[dict[str, Any]] = []

    async def fake_capture(self, directory, output, toolchain=None, includes=None):
        calls.append({"directory": directory, "toolchain": toolchain, "includes": includes})
        records = []
        for gcno in sorted(Path(directory).rglob("*.gcno")):
            got = gcno_sources(gcno)
            names = got.names if got is not None else []
            records.extend(f"TN:\nSF:{name}\nDA:1,5\nend_of_record\n" for name in names)
        output.write_text("".join(records))
        return output

    monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture)
    return calls
