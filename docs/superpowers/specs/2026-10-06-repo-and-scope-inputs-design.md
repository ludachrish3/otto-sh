# Repo and scope inputs — design (spec 4 of the #590 contract-first series)

**Status:** v5, **approved by the owner 2026-10-06** (S-6 included). **Date:** 2026-10-06.
- v1 had an Opus review: one critical finding (the cycle ratchet, now S-5) and
  eighteen others, all folded into v2.
- v2 had a Codex (Astra) review, verdict "not ready". Its critical finding: a demoted,
  dependency-skipped repo still lost its declaration and widened the walk. That is now §4 Rule 1.
  Its six other findings are folded in: the classifier's inputs, the init-failure rows, remote
  completion's `-I` and gate, §3's exact domain, duplicate names, and the purity test's patch
  target.
- Astra's re-check of v3 confirmed all seven addressed and raised four more, folded into v4:
  - lab names are never normalized;
  - Rule 1 also makes D3 see a skipped first repo;
  - `status --full` shows every verdict;
  - the completion path without a reservation gate already bootstraps.
- Fable's final review of v4 found no critical issue and eleven text fixes, folded into v5. The
  most visible is S-6 in §0. One premise was checked and found false: with no `OTTO_SUT_DIRS`,
  `get_repos()` returns `[]` rather than raising. That is now stated in §4 and pinned by §8 row 18.

**Amends:** spec 1 (`2026-10-04-public-api-manifest-design.md`) and its appendix. This spec
settles the pending seam spec 1 §7 assigns to spec 4: "repo and scope inputs
(`otto.config.scope`, `get_ordered_repos`, `is_bootstrapped`)". It replaces the placeholder rows
spec 1 left for the scope helpers (appendix B rows `EmptySelectionError`, `resolve_scopes`,
`scoped_ids`). §7 lists every amendment, and this spec's commits make them.

**Series.** #590 makes one public cutover, P1 (spec 1 §8 P-3). Specs 2–4 each settle a seam
before P1. Their public-path deltas land in P1; their contract changes land after P1, each as its
own marked commit, where the live API dump shows exactly what each one breaks (decision S-1).
Spec 4 goes first because spec 2's run state consumes the scope verdicts.

## 0. Owner decisions (2026-10-05 and 2026-10-06)

| # | Decision |
|---|---|
| S-1 | **Paths in P1, contracts after it.** Specs 2–4 design both now. A spec's public-path and name delta lands in P1; its contract changes land after P1 as separately marked commits, judged by the live dump. Designing the contracts before P1 is what keeps any name from moving twice. Order: spec 4, then 2, then 3. |
| S-2 | **One public scope answer, at `otto.lab`.** A single pure function answers "which hosts are this project's fleet of interest". `resolve_scopes`, `scoped_ids` and `ProjectScope` stay internal. `EmptySelectionError` is public at `otto.lab`, beside the walks that raise it. |
| S-3 | **`otto.config` parses; `otto.bootstrap` composes.** The accessors that read the composition root's cached result move from `otto.config` to `otto.bootstrap`. `otto.config` keeps settings, `Repo` and the pure parsers. |
| S-4 | **A fleet walk never widens silently.** When the repos cannot be read, a walk refuses instead of walking the whole lab (§4 Rule 2). |
| S-5 | **One approved ratchet re-baseline in P1** (2026-10-06, after review). Re-pointing internal callers to `otto.bootstrap` turns seven transitive paths through `otto.config` into direct in-cycle edges and cuts one (§10). P1 records the delta once, with the owner's approval. Spec 2 is expected to retire those edges by making the run's repos run state, read through the context; spec 2 decides (§11). |
| S-6 | **Approved with this spec (2026-10-06):** a dependency skip changes what a repo registers, never what it declared (§4 Rule 1). It follows from S-4, and closes the widening Astra found in v2. It is the only part of §4 that changes CLI behaviour. A walk whose last active declarer was skipped and switched off refuses instead of taking the whole lab. D3 sees a skipped first repo. `status --full` lists skipped repos' verdicts. |

## 1. What exists today

Evidence: a read-only survey at `d0839893`, re-checked by the review of v1.

- **`otto.config.scope` is pure.** It reads no environment, flag, file or working directory. At
  module level it imports only `dataclasses`, `re`, `typing` and `otto.errors`. Its function-scope
  imports are `otto.models.dependencies`, `otto.bootstrap` (for `ProjectScopeError`) and
  `otto.config` (for `get_repos`, in `scope_for_repo` only). The one membership predicate,
  `repo_targets`, is what every consumer calls. The module must never import `otto.host` or
  `otto.project`.
- **One page teaches it.** `docs/getting-started/boards-of-interest.md` imports `resolve_scopes`
  and `scoped_ids` to show the fleet of interest without connecting. That makes a two-step
  internal computation public: two signatures and `ProjectScope`, a ten-field verdict dataclass,
  to produce one set of host ids.
- **Spec 1's placeholder contradicts D1.** Appendix B homes the scope helpers at `otto.config`
  "placeholder only", while D1 says `otto.config` holds settings and `Repo` only.
- **`EmptySelectionError`** is raised by the fleet walks and caught by readers, but is reachable
  only at `otto.config.scope`, which is not a public path.
- **The repos have two homes.** `otto.bootstrap.bootstrap()` returns a `BootstrapResult` carrying
  `repos` and `ordered_repos`. `otto/config/bootstrapped.py` wraps the same singleton as
  `get_repos`, `get_ordered_repos`, `is_bootstrapped`, `get_completion_names` and `get_env`.
  Two of those (`is_bootstrapped`, `get_completion_names`) only delegate to functions
  `bootstrap.py` already defines.
- **Scope readers disagree on failure.**
  - `scoped_ids` raises on an unknown owner when any scope was resolved, because "a caller asked
    to be bounded BY a repo and getting the whole lab instead would be a widening".
  - `OttoContext.scopes` catches any exception from `get_ordered_repos()`, logs it at debug and
    returns `{}`, which walks the whole lab.
  - Bootstrap also loses declarations without raising. Discovery *contains* an unparseable
    `settings.toml`: it skips the repo and records a `BootstrapError`. The dependency pass skips a
    repo with unsatisfied dependencies: it stays in `repos` but is absent from `ordered_repos`,
    which is what `scopes` resolves over. Either way that repo's `[project]` declaration
    disappears. If it was the only declaring repo, a walk falls back to the whole lab.
  - The CLI and `open_context` run `session.check_repos` first, which treats both cases as fatal
    for an active repo. A context built without it (by hand, or by remote-path completion) walks
    hosts the project declared out of scope.

## 2. The public path delta (lands in P1)

| Name | Today | After P1 |
|---|---|---|
| `fleet_of_interest` (new, §3) | — | `otto.lab` |
| `EmptySelectionError` | `otto.config.scope` (not public) | `otto.lab` (newly public) |
| `resolve_scopes`, `scoped_ids`, `ProjectScope` | `otto.config.scope` (taught once) | internal; the taught import is retired |
| `get_repos` (taught), `get_ordered_repos`, `is_bootstrapped`, `get_completion_names`, `get_env` (reference only) | `otto.config` | `otto.bootstrap` |
| `invalidate`, `BootstrapResult`, `ProjectScopeError`, `DependencyError`, `BootstrapWarning` | `otto.bootstrap`, undeclared | declared at `otto.bootstrap` |
| `Repo` | `otto.config` (and the deep `otto.config.repo` path) | `otto.config` (deep path retired, as appendix G already says) |

- **`get_env` moves too.** It returns `discover().env`: discovery's cached result, which
  validates every SUT directory and builds a `Repo` for each. By S-3 that is composition.
  `load_otto_env`, the pure parser, stays at `otto.config`.
- **Which `otto.bootstrap` names are declared.** The rule: what an embedder calls, catches, or
  receives in a declared return type.
  - Called: `bootstrap`, `invalidate` (the recovery path `docs/architecture/subsystems/bootstrap.md`
    teaches), and the five accessors.
  - Caught: `BootstrapError`; `ProjectScopeError` (a fleet walk raises it when the declared fleet
    is empty, and `fleet_of_interest` on an unknown owner).
  - Received: `BootstrapResult` is `bootstrap()`'s return type; its `errors` carry
    `DependencyError` and its `warnings` carry `BootstrapWarning`.
  - Internal: `discover`, `DiscoveryResult` and `set_completion_names` are phase-1 and CLI
    plumbing. The docs that link them use the Internals rendering (§9).
- **After P1**, `otto.config` exports ten names: `DockerCompose`, `DockerImage`, `DockerSettings`,
  `MonitorSettings`, `Repo`, `ResolvedDependency`, `Version`, `load_otto_env`,
  `load_user_settings` and `user_settings_path`. D1 takes the six fleet names `otto.config`
  exports to `otto.lab` (D1's seventh, `Lab`, was never an `otto.config` export); this
  spec takes the five accessors to `otto.bootstrap`.
- **`otto.bootstrap`** declares twelve names: `BootstrapError`, `BootstrapResult`,
  `BootstrapWarning`, `DependencyError`, `ProjectScopeError`, `bootstrap`,
  `get_completion_names`, `get_env`, `get_ordered_repos`, `get_repos`, `invalidate` and
  `is_bootstrapped`.
- **`otto.lab`** declares D1's seven names plus `EmptySelectionError` and `fleet_of_interest`.

### 2.1 How the accessors move

P1 folds the wrappers into `src/otto/bootstrap.py` and deletes `src/otto/config/bootstrapped.py`.
It does not re-export them from a module that imports `otto.bootstrap` back.

- `bootstrap.py` already defines `is_bootstrapped` and `get_completion_names`. It gains
  `get_repos` (`bootstrap().repos`), `get_ordered_repos` (`bootstrap().ordered_repos`) and
  `get_env` (`discover().env`), with today's semantics. `get_repos` and `get_ordered_repos` still
  bootstrap lazily; `get_env` still discovers lazily; `is_bootstrapped` is still true
  mid-bootstrap. `get_completion_names` keeps the wrapper's docstring, which documents the
  snapshot's keys, over `bootstrap.py`'s one-liner.
- Internal callers import from `otto.bootstrap`, the owning module (spec 1 §9), always inside the
  function that uses them (the lazy-getter rule, `docs/contributing.md`). Test patch targets
  become `otto.bootstrap.<name>`. The migration is 364 import and patch lines across 33 `src`
  files and 68 test files, measured at `d0839893`, plus the bed-lane module
  `tests/repo3/tests/test_embedded_coverage.py:39`, which binds `get_repos` at module level.
- **Six sites import an accessor inside a broad `except`.** At each one, a missed migration raises
  `ImportError`, which is swallowed, so it fails silently. The primary defence is the static
  old-spelling guard (§8), which covers every site. These six also run unpatched against a real
  bootstrap:
  - `config/scope.py`, `scope_for_repo`: `from . import get_repos` (a relative package import that
    a grep for `config import get_repos` misses). Missed, the ingest gate admits every provider.
  - `lifecycle.py`, the teardown-deadline read of `get_env`. Missed, `OTTO_TEARDOWN_DEADLINE` is
    ignored.
  - `declared.py`, `declared_for_host`. Missed, every host loses its declared products and tools.
  - `context.py`, `OttoContext.scopes`. Missed, every walk takes the whole lab until commit 4
    removes the `except`: S-4's own failure.
  - `context.py`, `_detect_cov`. Missed, `ctx.cov` is always `False`.
  - `cli/remote_completion.py`, `_required_for` and `_reservation_allows`, under the completer's
    catch-all. Missed, remote-path TAB is dead.
- `otto.config`'s lazy table loses the five entries.

## 3. `fleet_of_interest`

```python
def fleet_of_interest(
    lab: Lab,
    repos: list[Repo],
    *,
    owner: str | None = None,
    exclude_projects: list[str] | None = None,
    include_containers: bool = False,
    include_local: bool = False,
) -> list[str]:
```

- **What it answers:** the ids of the hosts `OttoContext.all_hosts()` yields with the same flags,
  for the same lab, repos and `-E` set, in the lab's order. With `owner` set, the comparison is
  `ctx.for_repo(owner).all_hosts()`. "Fleet of interest" is the term the docs already use
  (`docs/cli/run/defaults.md`, *The fleet of interest*).
- **Where the two agree, exactly.** For a context that is not on the library sentinel lab, whose
  scope verdicts resolved from the same repos, the function's list equals the walk's ids. There
  are three stated exceptions, each tested on its own (§8):
  - **An empty declared fleet:** the walk refuses, and the function returns `[]`.
  - **An unknown `owner` over empty `repos`:** the function raises, and the walk falls back.
  - **The sentinel lab:** a context on it walks the whole lab whatever the repos declare, while
    the function scopes the lab it was given.
- **One definition, in two shared pieces.**
  - The admissible set: the scope verdicts, live membership through `repo_targets`, then `-E`.
    That is `scoped_ids` today, and `OttoContext.admissible_ids` calls it.
  - The membership-flag filter: containers and `local` held out by type unless their flag is set.
    Today this is inline in `OttoContext.all_hosts` and repeated in `_flags_hiding_every_match`.
    Commit 2 extracts it into one helper in `otto.config.fleet`, which `all_hosts`,
    `_flags_hiding_every_match` and `fleet_of_interest` all call.

  The walk adds only what belongs to a walk: the empty-fleet refusal, and pattern selection.
- **Placement.** The implementation goes in `src/otto/config/fleet.py`. It owns the walks and
  already imports `otto.host`, which the type checks need; `scope.py` must not. P1 declares it at
  `otto.lab`, and spec 5 may move the implementation without changing the public path.
- **Pure.** It does no I/O, never calls `bootstrap()` and loads no lab. The caller passes the
  repos: usually `get_repos()` (every parsed repo, which is what the walk resolves over after
  §4), or a hand-built `[Repo(sut_dir=...)]` as the boards-of-interest page does. When repo
  names are unique, their order does not affect the result. Two repos with the same name: the
  last one wins, as in the walk, so order then matters.
- **An empty result is an answer.** A walk refuses an empty declared fleet because a walk that
  touches nothing is a silent failure. A query that returns `[]` is not silent, so it raises
  nothing for emptiness.
- **`owner`** is a `Repo.name` exactly as declared. It is not normalized, as in the walk.
  - It raises `ProjectScopeError` when `owner` is not `None` and is not the name of any element
    of `repos`, **including when `repos` is empty**.
  - With empty `repos` this differs from the walk on purpose. `scoped_ids` falls back to the whole
    lab when no scope was resolved, because a context may not reach its repos. A caller that
    passed `repos` has reached them, so returning the whole lab for an unknown owner would be the
    widening the scoping exists to prevent.
  - `fleet_of_interest` does not special-case the library sentinel lab (`LIBRARY_LAB_NAME`); that
    sentinel belongs to contexts.
- **`exclude_projects`** is a list of repo names, normalized as `-E` is (PEP 503); `None` means
  none. It narrows only the union (`owner=None`); with `owner` set it is ignored, as in the walk.
  Names that match no repo are ignored: validating them is `session.select_projects`'s job.
- **Membership flags** apply after scoping, through the shared helper. `include_containers=True`
  admits container hosts the scope admits. `include_local=True` admits `local` **when the scope
  admits it**. `local` is always admitted when no repo declares a scope. Under a declaration it
  depends on its stamped `source_lab`, exactly as in the walk; the flag never adds a host the
  scope excluded.
- **No `pattern` parameter.** Narrowing by pattern is the walk's job; a caller can filter a list.

## 4. The contract change: a walk never widens silently (after P1)

A `fix(context)!:` commit after P1 changes how `OttoContext.scopes` obtains its verdicts. Two
rules close every path by which a declaration can vanish.

**Rule 1: a dependency skip changes what a repo registers, never what it declared.**
- `scopes` resolves its verdicts over every parsed repo (`get_repos()`), not over
  `get_ordered_repos()`. A repo the dependency pass skipped keeps its `[project]` declaration.
  Activation and `-E` are applied afterwards, exactly as for a healthy repo.
- Why: with an undeclared repo A and a declaring repo B whose dependencies are unsatisfied, today's
  `scopes` sees only A, and the walk takes the whole lab. That holds even through the CLI when B is
  switched off with `-E`, because `check_repos` demotes B's error. With B's declaration kept, the
  run behaves as it would if B were healthy and switched off: the union of active declared scopes
  is empty, and the walk refuses through the existing empty-fleet refusal.
- **CLI behaviour changes in two places, both from silence to a refusal:**
  - **The fleet walk.** It changes in the case above, from a whole-lab walk to the empty-fleet
    refusal.
  - **Project-layer entry (D3).** The orchestrator and the live monitor enforce D3 on the first
    parsed repo (`repos[0]`). When that repo was skipped by the dependency pass and its
    declaration cannot work on this lab, it used to have no verdict, and D3 returned. Now it has
    one, and D3 raises its `ProjectScopeError` even when another repo supplies a healthy fleet.
    Demoting the repo's bootstrap error does not suppress D3: the bootstrap error is about loading,
    D3 is about the declaration.
- A healthy run resolves the same verdicts as before: with unique names, the result does not
  depend on order (§3).
- **`status --full` shows every verdict.** `status` builds its scoping display while iterating
  the ordered repos, so a skipped repo's declaration would narrow the fleet while staying absent
  from the display that explains it. Commit 4 builds the scoping projection from every
  `ctx.scopes` verdict, in `get_repos()` order (`OTTO_SUT_DIRS`), and marks a dependency-skipped
  repo as skipped. "Skipped" is derived, not stored on `ProjectScope`: the repo is in `get_repos()`
  but not in `get_ordered_repos()`. `RepoScope` (`project/state.py`) gains one additive `skipped`
  field for the renderer. Actions and install-state aggregation still iterate only the ordered
  repos.

**Rule 2: what cannot be known refuses.**
- **The library sentinel lab** (`LIBRARY_LAB_NAME`) still returns `{}`. Nothing changes.
- **No environment is not a failure.** With no `OTTO_SUT_DIRS` (the default is an empty list),
  `get_repos()` returns `[]` and raises nothing. That was checked against the code, with every
  `OTTO_*` variable removed. No repos means no declarations, so the whole-lab fallback for
  undeclared runs still applies: a library caller with a hand-built lab and no SUT directories
  walks the lab as today.
- **When `get_repos()` raises** (an environment-level failure, such as `OTTO_SUT_DIRS` naming a
  directory that does not exist), the exception propagates. The `except Exception` and its debug
  line go.
- **Fallout in the test tree.** Some unit tests walk a hand-built context and may reach the
  removed `except` through another failure, such as a patched or partial bootstrap. Commit 4 runs
  the whole unit tree. Each test that relied on the catch is moved to the sentinel lab or to a
  patched `otto.bootstrap.get_repos`, and the commit names each one. None may keep the catch
  alive.
- **When bootstrap recorded an error that `check_repos` would treat as fatal,** `scopes` raises
  `otto.session.RepoLoadError` carrying every such error, the same exception `open_context`
  raises. One shared classifier decides fatality for both. It lives in `otto.session`
  (`session/projects.py`), extracted from `check_repos`, which then calls it. `otto.context →
  otto.session` is already a baseline edge, so commit 4 adds no ratchet edge.
  - "Owned" is decided over `bootstrap().repos` by `str(sut_dir)`, exactly as `check_repos` does.
  - An error no discovered repo owns (an unparseable `settings.toml`) is fatal: that repo's
    declaration cannot be known.
  - An owned error of an active repo is fatal, whatever its kind: an init-module failure, or a
    dependency skip.
  - An owned error of a repo switched off with `-E`, or out of scope before the lab, is demoted.
    Its declaration is still parsed and kept (Rule 1), so demotion hides nothing.
- **The classifier's inputs.** It takes the run's lab component names and the project selection.
  Only repo names and the `-I`/`-E` selections are normalized on read (PEP 503), as `active` does.
  Lab names pass to `inactive_before_lab` unchanged, because lab patterns are regexes fullmatched
  against the exact name: normalizing `Bench_A` to `bench-a` would make a broken repo look
  lab-inactive and demote a fatal error. A name in both lists counts as
  excluded, matching `active`'s exclusion-first order; `check_repos` checks inclusion first, which
  agrees only because `select_projects` forbids overlap. Validation (unknown names, overlap)
  stays where it is today: `session.select_projects`, at the CLI and `open_context` boundary. A
  hand-built context is never refused for its selection's spelling.
- **A refusal is cached.** `functools.cached_property` does not cache an exception, so a raised
  `scopes` would re-run `bootstrap()` on every read. `scopes` stores the refusal and re-raises the
  same exception on later reads.

**Every reader of `scopes` sees the change:**

| Reader | Behaviour after the change |
|---|---|
| `all_hosts`, `do_for_all_hosts`, `run_on_all_hosts`, `admissible_ids`, `get_hosts_in_play` | raise the refusal |
| `config.scope.active` and its callers (instruction dispatch, `session/activation.py`, `project/orchestrator.py`) | raise the refusal. `active`'s docstring promise that a library context is active still holds for the sentinel lab |
| `monitor/live.py`'s D3 check | its `except Exception` narrows to the no-context case (`get_context()`'s `RuntimeError`), so the refusal propagates instead of skipping D3. The no-environment world its docstring protects is unaffected: there `get_repos()` returns `[]`, and D3 has no current repo to enforce |
| `ctx.cov` (`_detect_cov`) | keeps its documented "never raises for a misconfiguration": it catches the refusal, logs one warning and answers `False`. Turning coverage off is the narrowing direction |
| remote-path completion's reservation gate (`cli/remote_completion._required_for`) | builds its context without `check_repos`; its catch-all turns the refusal into no candidates. The command itself then fails loudly through `check_repos` |

- **Remote-path completion carries `-I` too.** Its parsed chain records `exclude_projects` but not
  `include_projects`, so the classifier would demote an error the command treats as fatal.
  Commit 4 adds `include_projects` to the chain and to the context it builds.
- **Completion without a reservation gate reads no scope, by design.** When no repo configures
  reservations, completion returns before `_required_for` and lists a directory on the host the
  user named. Explicit targeting is never scoped (the scoping spec), so nothing widens there. This is recorded so a reader does not mistake it
  for a missed path. That path already calls `get_repos()` (and so bootstraps) today; commit 4
  adds no scope resolution and no classifier call to it, and changes nothing else there.
- The CLI's command paths and `open_context` run `check_repos` before building a context, so they
  meet Rule 2 only in the classifier's own terms, and they meet Rule 1 in the one case above.
- **The dump cannot see this change.** It is behaviour, not shape. The mark is the author's
  judgment (spec 1 §8 P-2), and the commit's tests enforce the behaviour.
- **Docs:**
  - the docstrings that state the old fallback: `OttoContext.scopes`, `OttoContext.for_repo`,
    `scoped_ids`'s empty-scopes paragraph, `active`, `_detect_cov`, and `monitor/live.py`'s D3
    check;
  - the Project scope section of `docs/configuration/lab-config.md`, including the skipped-repo
    rule;
  - the manual steps in `docs/cookbook/python-library.md`: a hand-built context that skips
    `check_repos` now refuses instead of widening.

## 5. What stays, and why

- **`scope_for_repo` stays fail-open.** It feeds the ingest gate, which runs in bare library use,
  in `otto init` scaffolding and in unit tests with no bootstrap to ask. A narrowing that cannot
  be computed narrows nothing; refusing there would turn "otto could not find its config" into
  "your host has no products". Its docstring records the argument.
- **The accessors keep bootstrapping lazily.** `get_repos()` before bootstrap runs discovery and
  the init imports, as today. `is_bootstrapped()` remains the probe that never forces it.
- **`scope.py` stays pure and import-light.** `fleet_of_interest` lives in `otto.config.fleet`
  (§3) and adds no module-level import anywhere: `Lab` and `Repo` are annotations, and the host
  types are imported inside the shared flag helper, as `all_hosts` does today.
- **The scoping spec's rules are unchanged**
  (`2026-08-18-project-lab-host-scoping-design.md`): one predicate, fullmatch on both axes,
  explicit targeting never scoped, D2 and D3. The whole-lab fallback when no repo declares a
  scope is unchanged too. What changes (§4) is the fallback taken when the repos cannot be read,
  and which repos' declarations count: every parsed repo's, skipped or not.

## 6. Commits

| # | Commit | Lands |
|---|---|---|
| 1 | `docs(spec)`: this spec and the spec 1 amendments (§7) | now |
| 2 | `feat(config)`: `fleet_of_interest`, the shared flag helper, and the differential test (§8) | before P1 |
| 3 | the §2 path delta, the migration and the doc re-points (§9) | inside P1's single marked commit |
| 4 | `fix(context)!`: `scopes` never widens silently (§4) | after P1 |

Commit 2 is an addition, so it needs no mark; it lands at the implementation module, and P1
declares it public. Its refactor of `OttoContext.all_hosts` onto the shared flag helper changes
no behaviour, which the existing walk tests and the differential show. Commit 4's footer names
the behaviour change.

## 7. Spec 1 amendments (made in this spec's commits)

- **Design §7:**
  - the pending-seams line for spec 4 is marked settled and links here;
  - the paragraph on the scope helpers' placeholder now names their resolution;
  - the K disposition ("every current export … stays public-provisional at its current path")
    gains the exception for spec 4's five accessors, beside D1's fleet names;
  - the specs 2–4 paragraph records S-1.
- **Appendix B:**
  - `otto.config.scope:EmptySelectionError` → `otto.lab:EmptySelectionError`.
  - `otto.config.scope:resolve_scopes` and `otto.config.scope:scoped_ids` → *internal*; the page
    that imported them teaches `fleet_of_interest` instead.
- **Appendix C:** the header and the `otto.config` row name the move: the taught `get_repos` and
  four reference-only accessors go to `otto.bootstrap`, and six fleet names go to `otto.lab`.
- **Appendix F:** `otto.bootstrap`'s initial `__all__` is the twelve names in §2; `otto.lab`'s
  gains `EmptySelectionError` and `fleet_of_interest`.
- **Appendix G:** the two `otto.config.scope` rows retire to `otto.lab:fleet_of_interest`.

The appendix header says it is generated from the evidence ledger. These rows are hand-amended
from this spec's decisions; P1 regenerates nothing from the ledger, it reads the declaration.

## 8. Tests

- **`fleet_of_interest` differential** (commit 2). Over generated cases, its result equals the
  walk's ids: `ctx.all_hosts(...)` with `owner=None`, `ctx.for_repo(owner).all_hosts(...)`
  otherwise, for a non-sentinel context built on the same lab, repos and `-E`, with the context's
  repo read patched to return those same repos. The cases cover:
  - no repo declared;
  - one repo declared, and several;
  - a declared repo beside an undeclared one;
  - `owner` set and unset;
  - `-E` on a declaring repo, with and without an `owner`, and an `-E` name that matches no repo;
  - container hosts and `local` present, each flag on and off;
  - a declaring repo whose scope does or does not admit `local`, with `include_local` on and off;
  - two repos with the same name, in both orders (last wins in both).

  Each axis is proved red by a planted divergence in the function under test, not in the oracle.
  §3's three exceptions are tested on their own:
  - an empty declared fleet: the function returns `[]`, and the walk raises `ProjectScopeError`;
  - `fleet_of_interest([], owner="x")` raises `ProjectScopeError`, and so does an unknown owner
    over non-empty repos;
  - on a sentinel-lab context the walk yields the whole lab, while the function scopes it.
- **Purity:** `fleet_of_interest` runs with `otto.bootstrap.bootstrap` patched to fail (the same
  target before and after P1: the accessors import it inside their bodies), and loads nothing
  beyond its arguments. The oracle's repo read is patched at the accessor's owning module, which
  is `otto.config.bootstrapped` before P1 and `otto.bootstrap` after; P1 migrates it. At commit 4
  the read itself changes from `get_ordered_repos` to `get_repos` (§4 Rule 1), so the patched
  name changes too.
- **The path delta** (commit 3) is proved by P1's machinery: the agreement tests, the dump and the
  docs validator. The boards-of-interest doctest runs against the new import.
- **No old spelling survives** (commit 3). A static test fails when any `src/otto` module imports
  one of the five accessors from `otto.config`, its package or `.bootstrapped`, in any spelling,
  relative included. It is the primary defence for the six swallowed-import sites in §2.1, which
  also run unpatched against a real bootstrap.
- **The walk contract** (commit 4). On a hand-built context:

  | # | Case | Expected |
  |---|---|---|
  | 1 | `OTTO_SUT_DIRS` naming a directory that does not exist | the walk raises `load_otto_env`'s `FileNotFoundError` |
  | 2 | an unparseable `settings.toml` in one SUT directory | the walk raises `RepoLoadError` carrying that `BootstrapError` |
  | 3 | an active, dependency-skipped repo that declares `[project]` | the walk raises `RepoLoadError` carrying its `DependencyError` |
  | 4 | undeclared repo A, plus repo B as in row 3 but switched off with `-E` | no `RepoLoadError`; B's declaration is kept, the active declared union is empty, and the walk raises the existing empty-fleet `ProjectScopeError`, never the whole lab |
  | 5 | an active parsed repo whose init module failed | the walk raises `RepoLoadError` |
  | 6 | the same repo switched off with `-E`, or out of scope before the lab | demoted; its declaration still counts |
  | 7 | `-E` spelled `My_Repo` for a repo named `my-repo`; a name in both `-I` and `-E` | normalized; the overlapping name counts as excluded; no refusal for spelling |
  | 8 | the sentinel lab | `scopes == {}` |
  | 9 | a refusal read twice | `bootstrap()` runs once |
  | 10 | `ctx.cov` with an unparseable sibling `settings.toml` | `False` and one warning |
  | 11 | remote-path completion with reservations configured, same tree | no candidates |
  | 12 | remote-path completion with a lab-inactive broken repo forced active by `-I` | the classifier sees `-I`; no candidates |
  | 13 | remote-path completion with no reservations configured | lists the named host as today; `_required_for` is never reached |
  | 14 | `monitor/live.py`'s D3 check on row 2's tree | the refusal propagates |
  | 15 | a lab named `Bench_A`, and a broken repo whose `lab_patterns` is `["Bench_A"]` | the lab name is not normalized; the repo is lab-active, so its error stays fatal |
  | 16 | a dependency-skipped first repo whose declaration targets another lab, then a healthy repo; with and without `-E` on the first | the orchestrator's and the monitor's D3 raise `ProjectScopeError` |
  | 17 | `status --full` with a dependency-skipped declaring repo | its verdict is shown, marked skipped, in `get_repos()` order |
  | 18 | no `OTTO_*` variable at all, hand-built lab | the walk takes the whole lab as today; `otto monitor`'s D3 check returns quietly |
  | 19 | a healthy tree | verdicts identical to before |

  Restoring the `except` turns row 1 red. Removing the shared classifier turns rows 2, 3 and 5
  red. Resolving over `ordered_repos` again turns row 4 red, with the whole lab.

## 9. P1 work for spec 4

Mirrored into `todo/590-p1-public-surface-switch-on.md`.

- **Before P1:** commit 2 (`fleet_of_interest`, the shared flag helper, the differential).
- **The fold and the migration** (§2.1): fold the accessors into `bootstrap.py`, delete
  `config/bootstrapped.py`, migrate the import and patch sites (including the bed-lane module and
  commit 2's patch target), and add the old-spelling guard (§8).
- **Exports:** remove the five accessors from `otto.config`'s `__all__` and lazy table. Write
  `otto.bootstrap`'s first `__all__` (the twelve names in §2), and add `EmptySelectionError` and
  `fleet_of_interest` to `otto.lab`.
- **The lazy-getter guard.** `tests/unit/test_no_import_time_lazy_exports.py` today guards only
  names in a `_LAZY_EXPORTS` table. After the fold, none of the accessors is in one, so a
  module-level `from ..bootstrap import get_repos` would pass and dodge test patches. The guard
  gains an explicit set of process-wide getters (`otto.bootstrap`: `bootstrap`, `get_repos`,
  `get_ordered_repos`, `get_env`, `is_bootstrapped`, `get_completion_names`), whether lazy or not.
  `tests/_fixtures/_lazy_exports.py`, `tests/unit/test_patch_targets.py` and the expected message in
  `tests/unit/test_lazy_packages.py` follow, and `docs/contributing.md`'s two rules are rewritten to
  cover it.
- **The ratchet re-baseline (S-5).** `tach.toml` gains seven `depends_on` entries and loses one;
  `tests/unit/test_import_cycle_ratchet.py`'s `BASELINE` records the same delta (§10), and
  `docs/architecture/modules.md` is regenerated.
- **The preview declaration** (`scripts/api_public_preview.toml`, which becomes
  `api/public.toml`) carries spec 4's delta, and its `pending = "spec 4 …"` note on `otto.config`
  is removed.
- **Docs build sites:**
  - `docs/api/config.rst` drops `.. automodule:: otto.config.bootstrapped`;
  - the `src` docstring roles that name `otto.config.bootstrapped.*` are re-pointed
    (`bootstrap.py`'s `is_bootstrapped`, `project/orchestrator.py`, `coverage/collect.py`);
  - `docs/api/bootstrap.rst` renders the public `__all__`, and an Internals entry with
    `:ignore-module-all:` keeps `discover`, `DiscoveryResult` and `set_completion_names` resolvable
    for the roles in `docs/architecture/subsystems/bootstrap.md`, `cli/main.py` and
    `config/completion_cache.py` (spec 1 §11's Internals rule).
- **Docs re-points:**
  - `docs/getting-started/boards-of-interest.md` teaches `fleet_of_interest`;
  - `docs/cookbook/python-library.md:535` and `docs/configuration/settings.md:399` import
    `get_repos` from `otto.bootstrap`;
  - `docs/contributing.md:582-611`, the patch-target guide;
  - the `EmptySelectionError` roles at `docs/cli/run/defaults.md:235` and
    `docs/cookbook/authoring/writing-instructions.md:89`;
  - `docs/architecture/subsystems/bootstrap.md` and the `otto.config` row of
    `docs/architecture/overview.md`;
  - the Project scope section of `docs/configuration/lab-config.md`.
- **P1's footer:**
  - retired: `otto.config.scope:resolve_scopes`, `otto.config.scope:scoped_ids`, and the five
    `otto.config` accessors;
  - additions: `otto.lab:fleet_of_interest`, `otto.lab:EmptySelectionError` (newly public), and
    the `otto.bootstrap` names;
  - the ratchet and `tach.toml` delta (S-5).
- **After P1:** commit 4 (§4).

## 10. File operations and the cycle ratchet

- **File operations** are accounted in P1's table (spec 1 §7, *File-operation accounting*).
  Expected: zero growth on the five pinned surfaces (`import_otto`, `version_repo`,
  `completion_repo_warm`, `completion_repo_handover`, `help_repo_warm`), and small decreases on
  surfaces that read an accessor: they stop loading `config/bootstrapped.py`, and paths that
  imported `otto.config` only for an accessor (the completion fast path in `cli/main.py`) may skip
  `otto.config`'s package init. The P1 table names each decrease, and P1 re-baselines the shrunk
  ceilings in the same commit. Any growth is named and justified. Commit 2 adds a function and a
  helper to an existing module and no module-level import, so its table is all zeros.
- **The cycle ratchet (S-5).** Each of these tach modules reaches the accessors through
  `otto.config` today and has no direct edge to `otto.bootstrap`. Re-pointing them adds seven
  in-cycle edges:

  | New edge | Sites at `d0839893` |
  |---|---|
  | `otto.coverage → otto.bootstrap` | `coverage/collect.py`, `coverage/get.py`, `coverage/instrumentation.py` |
  | `otto.docker → otto.bootstrap` | `docker/build_verbs.py`, `docker/deployment.py` |
  | `otto.host → otto.bootstrap` | `host/docker_host.py` |
  | `otto.lifecycle → otto.bootstrap` | `lifecycle.py` |
  | `otto.monitor.live → otto.bootstrap` | `monitor/live.py` |
  | `otto.project → otto.bootstrap` | `project/plan.py`, `project/orchestrator.py` |
  | `otto.suite → otto.bootstrap` | `suite/run.py` |

  - **One edge is cut:** `otto.lifecycle → otto.config`. `lifecycle.py`'s `get_env` read was its
    only import of `otto.config`.
  - **The coupling does not grow.** Each of the seven already reached `otto.bootstrap` through
    `otto.config`; the cycle's members do not change. The ratchet counts direct edges, so P1
    records the delta once, with the owner's approval (S-5).
  - **The edges have an exit.** Spec 2 is expected to make the run's repos run state, read
    through the context; spec 2 decides (§11).
    Six of the seven modules already have a context edge; `otto.docker` does not. Spec 5 measures
    which edges that retires.
  - **What survives.** `config → bootstrap` itself remains through `config/dependencies.py`
    (module-level `BootstrapWarning`, `DependencyError`) and `scope.py`'s function-scope
    `ProjectScopeError`. Cutting it is spec 5's work (§11).

## 11. Out of scope, recorded

- **Spec 2:**
  - whether the run's repos become run state, read through the context, which would retire S-5's
    edges (expected, not yet decided);
  - `otto.context` imports the private `config.scope._not_switched_off`; spec 2 owns the context
    side, and the shared computation in §3 is the natural place for that import to stop;
  - `OttoContext.admissible_ids` is already public (three-level reservations spec §5). Spec 2
    decides its contract; this spec only requires that it and `fleet_of_interest` share one
    computation.
- **Spec 5:** the rest of `config → bootstrap`. `ProjectScopeError` and the dependency errors are
  defined in `otto.bootstrap` but raised from `otto.config`; spec 5 decides whether they move down
  to a leaf.
