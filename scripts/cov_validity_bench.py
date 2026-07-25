"""Manual benchmark: fold cost at scale, with git-spawn count.

Run on demand (never in CI): `uv run python scripts/cov_validity_bench.py [N_FILES]`.
Reports wall-clock and git subprocess count so the cache decision (spec §9) is made
on evidence about batching, not just SSD wall-clock.
"""

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from otto.coverage.capture import gitio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from _fixtures._repo_timeline import RepoTimeline


def bench(n_files: int) -> None:
    """Benchmark the fold operation on a repo with n_files, measuring wall-clock and git spawns."""
    with TemporaryDirectory() as td:
        tl = RepoTimeline(Path(td) / "repo")
        for i in range(n_files):
            tl.write(f"src/f{i:04}.c", f"int f{i}(void)\n{{\n    return {i};\n}}\n")
        tl.commit("base")
        tl.capture("bench", {f"src/f{i:04}.c": {3: 1} for i in range(n_files)})
        for i in range(0, n_files, 10):  # touch 10% of files
            tl.write(f"src/f{i:04}.c", f"int f{i}(void)\n{{\n    return -{i};\n}}\n")
        tl.commit("churn")

        calls = 0
        real = gitio._run_raw  # noqa: SLF001

        def counting(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> bytes:
            nonlocal calls
            calls += 1
            return real(args, cwd, ok_codes)

        gitio._run_raw = counting  # noqa: SLF001
        try:
            t0 = time.perf_counter()
            tl.fold()
            dt = time.perf_counter() - t0
        finally:
            gitio._run_raw = real  # noqa: SLF001
        print(f"files={n_files} fold={dt * 1000:.0f}ms git_spawns={calls}")


if __name__ == "__main__":
    bench(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
