# Tree-wide guards are invisible to every scoped test run

## The blind spot

A growing family of `tests/unit/` tests takes **the repository** as its subject
rather than a module: they `rglob` the source tree, or read the Makefile,
noxfile, `.gitignore` and pytest config, and fail when *any* file in the tree
breaks a policy.

Selecting tests by path — which is what every focused iteration does, and what
every per-task run in a subagent-driven workstream does — **cannot select
them**. They are only reached by a full-suite gate. So the feedback loop for
the one class of guard that watches the whole tree is the slowest loop the repo
has.

The guards are not the problem; they work. The problem is *when* they speak.

## What it cost, concretely

Dry-run contract workstream, 2026-08-16. Task 7 added
`tests/unit/config/test_repo_provenance_dry_run.py`, whose `_head_sha()` helper
spawned `subprocess.run(["git", "-C", …, "log", …])` with no hermetic
environment — inheriting the developer's `HOME`, global and system git config,
credential prompts and `PATH`.

`tests/unit/test_gitenv_hermeticity.py::test_no_hand_rolled_git_envs_outside_the_fixture`
exists precisely to catch that, and it did — **at Task 8's `make coverage`,
after seven tasks of green scoped runs.** Every task had run only the files it
touched, so nothing in seven cycles could have seen it. The defect rode the
branch for a full task cycle and surfaced only at the ~4-minute whole-suite
gate, where it competes for attention with everything else that gate reports.

## The known set

Repo-wide source scanners:

| Guard | Subject |
| --- | --- |
| [test_gitenv_hermeticity.py](../tests/unit/test_gitenv_hermeticity.py) | hand-rolled git envs outside the fixture |
| [test_no_bare_asyncio_run.py](../tests/unit/test_no_bare_asyncio_run.py) | bare `asyncio.run` |
| [test_no_skip_lanes.py](../tests/unit/test_no_skip_lanes.py) | skips standing in for host-down failures |
| [test_tuple_return_debt.py](../tests/unit/test_tuple_return_debt.py) | tuple returns where a dataclass belongs |
| [test_e2e_clock_hygiene.py](../tests/unit/test_e2e_clock_hygiene.py) | wall-clock use in e2e |
| [test_sutrepo_scaffold_policy.py](../tests/unit/test_sutrepo_scaffold_policy.py) | SUT-repo scaffold policy |
| [test_bed_oracle_honesty.py](../tests/unit/test_bed_oracle_honesty.py) | bed oracles that cannot fail |

Repo-artifact scanners (Makefile / noxfile / `.gitignore` / pytest config):

| Guard | Subject |
| --- | --- |
| [test_tier_marker_invariants.py](../tests/unit/test_tier_marker_invariants.py) | tier markers across every conftest and test file |
| [test_conftest_env_writes.py](../tests/unit/test_conftest_env_writes.py) | env writes in conftests |
| [test_lane_invariants.py](../tests/unit/test_lane_invariants.py) | an addopts override must not drop the tach guard |
| [test_declared_harness_bounds.py](../tests/unit/test_declared_harness_bounds.py) | every lane declares its own runaway guard |
| [test_webassets_guard.py](../tests/unit/test_webassets_guard.py) | gitignored-inside-the-package == registered build artifact |

The list is assembled by inspection, so treat it as a starting point rather
than a closed set — establishing the real membership is part of the work.

## Proposal

**Measured first:** all twelve run in **24s single-process, 109 tests, no
coverage** (`--no-cov -n0`, 2026-08-16, dev VM). That is cheap enough to append
to any focused run, which is the entire argument.

1. Register a `treewide` marker in `pyproject.toml` and mark the family. The
   `serial_timing` entry is the precedent for a marker whose description
   carries the structural rule the pins cannot enforce — write this one the
   same way, and say in it that a tree-subject guard **must** be marked or it
   inherits this blind spot.
2. Add a make target (`make gate-treewide` or similar) that runs `-m treewide
   --no-cov -n0`, and name it in the per-task gate guidance so a scoped
   iteration is `pytest <paths>` **plus** that target.
3. Consider a guard-for-the-guards: a test asserting that anything in
   `tests/unit/` which globs the source tree or reads a build file carries the
   marker. Same shape as the existing scanners, and it is what stops the family
   silently growing back into invisibility.

## Related

The same "the run that would have caught it was never selected" shape, for a
different reason (co-scheduling rather than path scoping), is why
`serial_timing` exists and why the asyncio-leak detector had to be armed in
every coverage lane. This file is about the path-scoping half.

Process lessons from the same workstream:
[plan-and-review-practice-2026-08-16.md](plan-and-review-practice-2026-08-16.md).
