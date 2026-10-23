"""Build one downloadable archive per docs example track (``docs/examples/quickstart-*``).

An archive holds the track's TRACKED files only (``git ls-files``), so local build
output and ``.git`` never ship. It is byte-reproducible: entries are sorted and carry
a fixed mtime and owner, the mode is only executable or not, and the gzip header has
no timestamp. Sphinx runs this at ``builder-inited``; a page links the result with
``{download}``.
"""

import gzip
import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "examples"
OUT_DIR = ROOT / "docs" / "_static" / "generated" / "examples"


def _tracked(repo: Path, track: Path) -> "list[Path]":
    listed = subprocess.run(  # noqa: S603 — git with fixed arguments, no shell
        ["git", "-C", str(repo), "ls-files", "-z", "--", str(track.relative_to(repo))],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted(repo / name for name in listed.split("\0") if name)


def _archive(track: Path, files: "list[Path]") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{track.name}/{path.relative_to(track).as_posix()}")
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def build(examples_root: Path, out_dir: Path, *, repo: Path) -> "list[Path]":
    """Write ``<out_dir>/<track>.tar.gz`` for every ``quickstart-*`` track; return the paths."""
    built: "list[Path]" = []
    for track in sorted(p for p in examples_root.glob("quickstart-*") if p.is_dir()):
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{track.name}.tar.gz"
        target.write_bytes(_archive(track, _tracked(repo, track)))
        built.append(target)
    return built


def main() -> int:
    """Build every track's archive under ``docs/_static/generated/examples``."""
    for path in build(EXAMPLES, OUT_DIR, repo=ROOT):
        print(f"built {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
