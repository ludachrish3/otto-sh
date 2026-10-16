# #566 follow-up — enforce the per-commit gate with a stamp

Recorded 2026-10-15 at Phase D execution; Chris agreed this is a work item
**after** Phase D lands (it builds on `make gate-scope`).

## The gap

Phase D codifies the per-commit testing policy three ways: the AGENTS.md
bullet every agent session loads, `make gate-scope` as the executable oracle
(the diff is classified by tables and the exact commands printed), and the
tests that keep the oracle complete (table pins, the oracle census over every
tracked test module, two negative controls). The tip is already forced: the
pre-push hook runs `gate-fresh` and CI runs the full gate on every push.

Nothing forces anyone to **run** the per-commit commands the oracle prints.
A committer can read the output and skip it; the scoped gate is advisory.

## The design (same shape as the coverage stamp `make release` trusts)

1. `make gate-scope` gains a run mode (`scripts/gate_scope.py --run`) that
   executes its "this commit" lines and, on green, writes a stamp keyed by the
   commit's **tree hash** and the exact command list it ran. Tree hash, not
   commit hash: a squash or a rebase that keeps the tree keeps the stamp.
2. `.githooks/pre-push` refuses a push when any commit in the pushed range has
   no stamp for its tree, or whose stamp names fewer commands than the oracle
   prints for that commit now (the tables may have grown since the stamp).
3. A squash carries the tip's full-gate stamp, which `gate-fresh` already
   produces; no second mechanism.

Stamps live beside the coverage stamp (gitignored), one file per tree hash,
so a worktree sweep or `git clean -fdx` simply means re-running the gate.

## Acceptance

- [ ] `--run` executes exactly the lines `commands()` prints for the commit,
      in that order, and stops at the first red one; the stamp is written only
      on all-green.
- [ ] pre-push walks `<remote-tip>..<local-tip>` and names every unstamped
      commit (short sha + the commands it is missing) before refusing.
- [ ] An `escalate`/`full` classification is satisfied by the `gate-fresh`
      stamp alone.
- [ ] Tests: a stamp written for tree T is honoured by a different commit
      with the same tree; a stale stamp (fewer commands than the oracle now
      prints) is refused; the hook's refusal text is pinned.
- [ ] Docs: `docs/architecture/quality-gates.md` gets the stamp row; the
      AGENTS.md bullet says the push refuses unstamped commits.

Roughly a Task-4-sized item. Refs: #566.
