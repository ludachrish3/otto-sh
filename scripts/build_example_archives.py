"""Build the downloadable archives of the docs' example projects.

There is one archive per example track (``docs/examples/quickstart-*``), and one per
entry of :data:`BUNDLES`, which ships several projects side by side because one loads
another's code by a relative path. Each project unpacks into a directory of its own
name. An archive holds the projects' TRACKED files only (``git ls-files``), so local
build output and ``.git`` never ship. It is byte-reproducible: entries come in a fixed
order (the projects in their ``BUNDLES`` order, each one's files sorted) and carry a
fixed mtime and owner, the mode is only executable or not, and the gzip header has no
timestamp. Sphinx runs this at ``builder-inited``; a page links the result with
``{download}``.
"""

import gzip
import io
import subprocess
import sys
import tarfile
from collections.abc import Collection
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "examples"
OUT_DIR = ROOT / "docs" / "_static" / "generated" / "examples"
BUNDLES: "dict[str, tuple[str, ...]]" = {
    "getting-started": ("getting-started", "getting-started-inventory"),
}
"""Archive name -> the example projects it holds, side by side.
``getting-started-inventory`` loads ``../getting-started/libs``, so the two ship together."""


def _tracked(repo: Path, track: Path) -> "list[Path]":
    listed = subprocess.run(  # noqa: S603 — git with fixed arguments, no shell
        ["git", "-C", str(repo), "ls-files", "-z", "--", str(track.relative_to(repo))],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted(repo / name for name in listed.split("\0") if name)


def _archive(projects: "list[Path]", checkout: Path) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for project in projects:
            for path in _tracked(checkout, project):
                _add(tar, f"{project.name}/{path.relative_to(project).as_posix()}", path)
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def _add(tar: tarfile.TarFile, name: str, path: Path) -> None:
    data = path.read_bytes()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
    tar.addfile(info, io.BytesIO(data))


def _archives(examples_root: Path) -> "dict[str, list[Path]]":
    """Archive name -> its projects: each ``quickstart-*`` track alone, then each bundle."""
    archives = {p.name: [p] for p in examples_root.glob("quickstart-*") if p.is_dir()}
    for name, members in BUNDLES.items():
        projects = [examples_root / m for m in members]
        if all(p.is_dir() for p in projects):
            archives[name] = projects
    return dict(sorted(archives.items()))


def build(
    examples_root: Path,
    out_dir: Path,
    *,
    checkout: Path,
    only: "Collection[str] | None" = None,
) -> "list[Path]":
    """Write ``<out_dir>/<name>.tar.gz`` for every track and bundle; return the paths.

    *only* names the archives to write (``"getting-started"``, say), so a
    caller checking one packs only it; ``None`` writes every one.
    """
    built: "list[Path]" = []
    for name, projects in _archives(examples_root).items():
        if only is not None and name not in only:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{name}.tar.gz"
        target.write_bytes(_archive(projects, checkout))
        built.append(target)
    return built


def main() -> int:
    """Build every archive under ``docs/_static/generated/examples``."""
    for path in build(EXAMPLES, OUT_DIR, checkout=ROOT):
        print(f"built {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
