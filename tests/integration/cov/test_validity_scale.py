"""Small-scale pin of the batched fold: spawn count is the load-bearing assert.

Wall-clock budgets flake on shared runners; the spawn count is
deterministic. 200 files keeps this test <1s everywhere.
"""

from otto.coverage.capture import gitio
from tests._fixtures._repo_timeline import RepoTimeline


def test_fold_200_files_is_batched(tmp_path, monkeypatch):
    tl = RepoTimeline(tmp_path / "repo")
    for i in range(200):
        tl.write(f"f{i:03}.c", f"int f{i}(void)\n{{\n    return {i};\n}}\n")
    tl.commit("base")
    tl.capture("bench", {f"f{i:03}.c": {3: 1} for i in range(200)})
    tl.write("f000.c", "int f0(void)\n{\n    return -1;\n}\n")
    tl.commit("churn")

    calls: list[list[str]] = []
    real = gitio._run_raw
    monkeypatch.setattr(
        gitio,
        "_run_raw",
        lambda a, c, ok=(0,): (calls.append(list(a)), real(a, c, ok))[1],
    )
    tl.fold()
    assert len(calls) <= 2, [" ".join(c) for c in calls]


def test_fold_gcd_base_is_batched(tmp_path, monkeypatch):
    tl = RepoTimeline(tmp_path / "repo")
    for i in range(100):
        tl.write(f"f{i:03}.c", f"int f{i}(void)\n{{\n    return {i};\n}}\n")
    tl.commit("base")
    cap = tl.capture("bench", {f"f{i:03}.c": {3: 1} for i in range(100)})
    for i in range(0, 100, 10):
        tl.write(f"f{i:03}.c", f"int f{i}(void)\n{{\n    return -1;\n}}\n")
    tl.commit("churn")
    # Simulate a GC'd/shallow base: the commit is gone, only the capture's
    # per-file blobs and the current worktree remain to resolve against.
    tl.captures[-1] = cap.model_copy(update={"base_commit": "0" * 40})

    calls: list[list[str]] = []
    real_run_raw = gitio._run_raw
    real_run_raw_input = gitio._run_raw_input
    monkeypatch.setattr(
        gitio,
        "_run_raw",
        lambda a, c, ok=(0,): (calls.append(list(a)), real_run_raw(a, c, ok))[1],
    )
    monkeypatch.setattr(
        gitio,
        "_run_raw_input",
        lambda a, c, stdin, ok=(0,): (
            calls.append(list(a)),
            real_run_raw_input(a, c, stdin, ok),
        )[1],
    )
    tl.fold()
    assert len(calls) <= 6, [" ".join(c) for c in calls]
