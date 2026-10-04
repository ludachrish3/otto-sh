# otto-sh system architecture review

Reviewed 2026-10-04 at commit `5096bb78` (version declared: 0.16.1).
Read-only review; recommendations below are not implementation decisions except
where a subsequent clarification from Chris is explicitly recorded.

## Assessment

**Otto has a worthwhile core, but it owns too much of the behavior around that core.**
Its strongest role is connecting heterogeneous lab hosts, controlling their lifecycle,
deploying products, and attaching test evidence to the right host, product, and build.
SSH, Telnet, serial consoles, BusyBox, containers, cross-compilation, and offline labs
make that orchestration substantially more specialized than a generic task runner.

The largest avoidable costs are around pytest execution and discovery, Python
environment management, and dependencies between packages. Docker has recently
improved considerably: this review does **not** recommend redoing the work that
removed otto's build-cache decision and restored Docker's own identifiers and output.

The quality problem is not an absence of tests. Otto already has extensive tests,
high coverage floors, conformance suites, architecture checks, and executable docs.
The missing protection is concentrated at the boundaries: an installed package
versus a development environment, otto versus the tools it wraps, a cached view
versus the real system, and a declared architecture versus actual dependencies.

My recommended order is:

1. Fix the reproduced dependency on a development-only pytest plugin; add an
   installed-package smoke test. Resolve the already-filed credential leaks in the
   same immediate reliability tranche.
2. Keep completion cached and inexpensive for NFS users; make collection during an
   actual test run authoritative, and prioritize pytest configuration passthrough.
   A convenience cache must not decide that a real test does not exist.
3. Land precise contract and architecture checks, then cut concrete dependency
   edges. Avoid a broad package reshuffle before those checks exist.
4. Finish the existing uv delegation and thin-CLI work, with explicit code deletion
   goals. Address Docker staging ownership before expanding its wrapper surface.

## Scope and evidence

I read the built architecture documentation as requested, then checked current
source documentation and implementation. The local HTML is stale in places: it
still describes `OttoSuite` and downward-only dependencies; current source docs
describe plain pytest tests and acknowledge the dependency cycle. Findings below
use current source, not those obsolete HTML claims.

The [GitHub workboard](https://github.com/users/ludachrish3/projects/1) was read through
its paginated API: 200 items, including 169 open issues. Of the open items, 92 were
To triage, 50 Backlog, 24 Ready, and 3 In progress. These are a dated snapshot, not
an assertion that every open issue still describes HEAD. I checked relevant issue
bodies against the implementation; several are already partly or wholly superseded.

Checks performed:

| Check | Result and limits |
| --- | --- |
| `make typecheck` | Passed Python and TypeScript checks. Used `UV_CACHE_DIR=/tmp/otto-review-uv-cache UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1` after the default uv cache was unwritable. The target also refreshed ignored `web/node_modules` through its existing prerequisite. The TS wrapper reported 88 ignored vendored diagnostics and 323 deferred test-file `noUncheckedIndexedAccess` sites; this is the repository's filtered gate, not an unfiltered TS pass. |
| `.venv/bin/tach check` | Passed. |
| `.venv/bin/tach check --exact` | Failed on nine unused declared dependencies, detailed below. No config was changed. |
| Existing module-graph functions | Measured 38 declared modules, 230 edges, a 23-module strongly connected component, and 15 mutual dependency pairs. |
| Temporary minimal SUT, actual CLI subprocess | `otto test --list-tests` succeeded normally, then failed with exit 4 with `pytest-cov` disabled. All fixture files and otto state were under `/tmp`. |
| Recent GitHub CI results | Read 100 push-to-main workflow records; analysis below. No jobs were started or changed. |

No application fixes, workboard edits, commits, Docker operations, or lab operations
were performed during the initial review. The subsequently requested issue filing
is recorded under findings 1 and 2. Full coverage, bed lanes, and `gate-fresh` were not run for this
review. This is an architectural assessment with one targeted failure reproduction,
not a certification of the whole product or a fresh security audit.

## Where custom machinery earns its place

| Area | Keep in otto | Delegate or constrain |
| --- | --- | --- |
| Host orchestration | Lab identity, hop topology, transport selection, loop ownership, scoped teardown, console recovery, embedded capabilities | Keep AsyncSSH and protocol libraries responsible for their protocols. Avoid recreating their security defaults or generic configuration systems. |
| Product lifecycle | Host/product matching, dependency-ordered lab actions, staging, per-product hooks, lab cleanup | Product hooks should invoke the user's build/install tools. Do not grow a second general build system or package manager. |
| Testing | Lab fixtures, reservation checks, product convergence, artifacts, monitor events, remote coverage retrieval | Pytest should own ordinary collection, node IDs, fixture lifetimes, reporting, configuration, and plugin loading. |
| Docker | Resolve lab parents, compose use-cases, container host identities, and heterogeneous access paths | Docker/Compose should own caching, image identity, command output, and Compose interpretation. |
| Python environment | Select the relevant otto repositories and explain which lab/project combinations are available | uv should resolve and synchronize Python packages; `packaging` should interpret Python project versions. |
| Monitoring | Agentless lab sampling, test-event correlation, offline session replay | Keep the scope at test-session observation unless there is an actual requirement for an operational monitoring platform. |
| Coverage | Retrieve counters from unusual targets, prove build identity, preserve host/product provenance, combine manual evidence | Keep compiler coverage interpretation and standard counter merging in gcov/LLVM/lcov. Treat the report UI and ticket attribution as separable consumers. |
| Inventory and reservations | Adapt sources, join inventory to lab topology, enforce access policy, expose conformance contracts | Continue integrating existing sources such as NetBox; avoid becoming their authoritative asset or booking database. |

These boundaries do not require separate distributions or services. A modular
monolith and one offline-installable wheel remain sensible. Internal dependency
direction matters much more than splitting the package for appearance's sake.

## Findings and recommendations

### 1. P0 — The development environment hides an actual test-runner dependency failure

**Follow-up requested by Chris:** filed
[#593](https://github.com/ludachrish3/otto-sh/issues/593) as **P0 - HIGH / Ready**,
with reproduction steps and the recommended replacement of the inner pytest
`--no-cov` argument by `-p no:pytest_cov`. Chris explicitly rejected adding an
unused runtime dependency. The issue includes the independently verified argument
handling and requires runtime-only installed-wheel regression coverage.

**Reproduced.** [`_pytest_session`](../src/otto/suite/run.py), around line 982,
unconditionally supplies `--no-cov`. [`pyproject.toml`](../pyproject.toml) declares
`pytest-cov` only in the development group, although pytest, pytest-asyncio,
pytest-timeout, and pytest-randomly are runtime dependencies.

Against a temporary SUT containing one plain `test_smoke`, the actual CLI produced:

```text
python -m otto test --list-tests
exit 0; lists test_smoke

PYTEST_ADDOPTS='-p no:pytest_cov' python -m otto test --list-tests
exit 4
pytest.main(): error: unrecognized arguments: --no-cov
```

This proves dependency on an enabled plugin outside the runtime dependency contract.
It simulates plugin absence in the current development interpreter; it is not a
fresh wheel installation test. The error also prints a misleading empty test tree
after collection fails, alongside the error diagnostics.

**Recommendation:** make the runner work without pytest-cov, unless Python coverage
is deliberately adopted as a runtime feature. Do not add a runtime dependency merely
to recognize a flag used to disable that dependency. Resolve that policy alongside
the broader pytest configuration work in
[#469](https://github.com/ludachrish3/otto-sh/issues/469).

**Missing gate:** install the built wheel into an isolated environment with only its
declared runtime dependencies, run outside the checkout, scaffold a small SUT, list
and execute one ordinary test, and load the packaged web assets. Existing
[`wheel-check`](../Makefile) checks archive contents, while development tests have
development plugins available. Both checks are useful; neither proves this boundary.
Run the smoke on the oldest and newest supported Python, initially with a tiny fixture.

**Acceptance:** that smoke succeeds without pytest-cov, source-tree imports, or
development groups. Deliberately removing one required runtime dependency makes it
fail. This is a new concrete finding from this review, not merely a restatement of
the older test-infrastructure backlog.

### 2. P0 — A test-discovery cache has become part of the correctness model

**Confirmed design limitation; subsequently reproduced on 2026-10-04.**
[`config/collected_tests.py`](../src/otto/config/collected_tests.py) and
[`suite/plugin.py`](../src/otto/suite/plugin.py) use previous collection records to
prune files before pytest sees them. The
[selection documentation](../docs/cli/test/selection.md), “What the cache can't
follow,” explicitly describes tests generated from non-Python data or imported
plain values being missed. If another file already holds the requested name, otto
can run that file's test while omitting the newly generated matching test.

**Follow-up requested by Chris:** filed
[#592](https://github.com/ludachrish3/otto-sh/issues/592) as **P0 - HIGH / Ready**.
A temporary SUT at the reviewed commit reproduced the false green: after changing
only an existing JSON input, the warm-cache named run reported **1 passed, exit 0**;
native pytest reported **1 failed, 1 passed, exit 1**. Explicit unfiltered recollection
made the identical otto command also report **1 failed, 1 passed, exit 1**. Every
invocation used a fresh subprocess, and test-file/directory mtime and size remained
unchanged across the JSON edit. The issue contains the copyable reproduction,
commit-pinned root-cause links, and acceptance criteria preserving cached completion
and filesystem-operation startup budgets.

That is an architectural boundary violation for a pytest convenience wrapper. A
stale completion menu is inconvenient; a stale execution set can produce incomplete
validation with a successful result. Tracking ever more Python dependencies does
not solve arbitrary collection hooks, environment-dependent generation, or external
input files.

**Chris's clarification, 2026-10-04:** repeated collection is too expensive for NFS
users. Slightly stale tab completion is an acceptable tradeoff; collection at test
runtime is necessary for accuracy. This establishes different freshness requirements
for suggestions and execution, rather than requiring fresh collection everywhere.

**Recommendation:** retain cached completion without making a keystroke wait for
pytest collection. Refresh from completed runtime collections or explicit refresh;
any background refresh must also respect the NFS I/O budget. At execution, let the
run's own pytest collection determine the tests within the requested scope. Do not
add a second collection pass merely to validate the cache, and do not let cached
names exclude files that the user's actual selection could match. Explicit file
paths and node IDs can narrow that scope honestly; a name intended to match across
repositories cannot be narrowed to yesterday's known files without losing accuracy.

**Acceptance:** warm-cache and cold-cache runs select the same node IDs after adding
a JSON-generated test, changing a collection environment variable, and adding a
same-named test in another file. Compare against native pytest collection, not just
against a second call through otto's selection code.
Completion checks should separately prove that a stale or cold cache does not force
synchronous collection on TAB and that a later runtime collection refreshes names.

Related work: [#489](https://github.com/ludachrish3/otto-sh/issues/489) broadens
selection/completion, and [#490](https://github.com/ludachrish3/otto-sh/issues/490)
makes cache clearing more selective. Neither by itself removes this correctness risk.

### 3. P1 — Pytest wrapping still carries substantial test-framework ownership

The move to plain pytest tests is good. The remaining execution machinery is still
much more than lab integration:

- [`suite/run.py`](../src/otto/suite/run.py), `_pytest_session`, clears the repo's
  `addopts`, replaces its warning configuration, forces capture/logging and asyncio
  settings, invokes one `pytest.main()` per repo, and evicts selected imported
  modules afterward. The current CLI has no general pytest-argument passthrough.
- [`suite/plugin.py`](../src/otto/suite/plugin.py), `pytest_runtest_protocol`, uses
  `_pytest.runner`, `_request`, `_initrequest`, and `funcargs` to repeat just the
  call phase. Its report-status hook reproduces pytest's outcome categorization.
- [`suite/loops.py`](../src/otto/suite/loops.py) obtains private pytest-asyncio
  fixtures named `_<scope>_scoped_runner`.
- [`suite/_retry.py`](../src/otto/suite/_retry.py) owns another SIGALRM timeout
  mechanism layered around pytest-timeout. Its own comments record the hook-order
  and repeated-body bugs this has already required fixing.

Pytest itself cautions against repeated `pytest.main()` calls in one process because
of Python's import caching. That supports investigating process isolation, but does
not prove all current multi-repo runs fail.
[Upstream guidance](https://docs.pytest.org/en/stable/how-to/usage.html#calling-pytest-from-python-code).

**Recommendation:** promote [#469](https://github.com/ludachrish3/otto-sh/issues/469)
from P1 Ready to the first simplification tranche. Preserve the user's pytest config
and pass native arguments through a documented delimiter. Make any unavoidable otto
overrides explicit. Build toward a plugin usable by ordinary `python -m pytest`,
with `otto test` adding lab selection and convenient defaults.

Evaluate one fresh process per repo/session as a bounded design spike. The child
must own its live hosts, loop, fixtures, cleanup, and monitor; passing live host
objects across processes is not a solution. Compare that cost against retaining
in-process execution behind a small, tested compatibility adapter.

Ordinary retries and repetitions overlap with
[pytest-rerunfailures](https://pytest-rerunfailures.readthedocs.io/stable/) and
[pytest-repeat](https://github.com/pytest-dev/pytest-repeat). They are candidates,
not established drop-in replacements: otto's “setup once, repeat the body, calculate
a stability threshold” deliberately differs from normal fixture execution. Preserve
that specialized mode only where it is a user requirement, with an explicit name
and semantics. Keep ordinary pytest execution free of those changes.

**Acceptance:** an unmodified small pytest project preserves its collection,
warnings, xfail/skip behavior, fixture lifetimes, and exit status under the wrapper;
plugin and loop compatibility have dedicated tests. Keep any unavoidable private
API usage in one adapter and test both minimum supported and freshly resolved
pytest/pytest-asyncio versions.

### 4. P1 — The dependency gate permits a large shared change domain

Using the existing [`render_module_graph`](../scripts/render_module_graph.py)
functions, the declared graph has **23 of 38 modules in one strongly connected
component**: each can reach every other through dependency paths. Of 230 declared
edges, 118 are internal to that component. `otto.config` participates in 9 of the
15 declared mutual pairs. This corroborates
[#590](https://github.com/ludachrish3/otto-sh/issues/590).

There is an additional precision problem. Normal `tach check` passes, but `--exact`
finds these unused permissions:

| Module | Unused declared dependencies |
| --- | --- |
| `otto.cli` | `otto.labs` |
| `otto.config` | `otto.utils`, `otto.lifecycle`, `otto.result`, `otto.logger` |
| `otto.context` | `otto.inventory` |
| `otto.session` | `otto.context` |
| `otto.monitor.live` | `otto.host` |
| `otto.suite` | `otto.result` |

Removing just those permissions in an in-memory graph calculation leaves the
23-member component intact, but reduces edges to 221 and mutual pairs to 13.
Therefore neither the cycle nor the opportunity to improve the gate is an artifact
of stale prose. These are package dependency measurements, not claims of a current
runtime circular-import crash.

**Recommendation:** land [#522](https://github.com/ludachrish3/otto-sh/issues/522)
with today's measured baseline, plus exact-edge checking. Ratchet both membership
and internal dependency edges: a member-only rule still permits additional coupling
among the same 23 packages. Remove stale permissions by hand, retaining the reasons
for permitted debt; do not use `tach sync` to bless new dependencies.

Then follow #590's concrete cuts. Separate settings parsing and immutable
configuration from fleet access, discovery, and runtime composition. Have the CLI
provide its completion-tree snapshot instead of config importing CLI internals.
Keep runtime construction in the composition layer. Re-measure after each cut.

Recent `otto.session` extraction improves CLI/library parity even though it adds a
package to the component. Do not confuse that legitimate improvement with completed
layering. Lazy imports reduce startup cost; they do not remove dependencies.

**Acceptance:** a planted new backward edge fails the gate, removed edges cannot
silently return, and each extraction retires concrete dependencies. File size alone
is not an acceptance criterion.

### 5. P1 — Environment management duplicates conventions that can be delegated

[`env/backends.py`](../src/otto/env/backends.py) supports uv and pip paths;
[`env/manage.py`](../src/otto/env/manage.py), `_fill`, installs repositories and
otto in two separate transactions. That needlessly prevents one combined resolution
and requires otto-owned metadata, staleness checks, and conflict attribution.
[`config/version.py`](../src/otto/config/version.py) and
[`models/dependencies.py`](../src/otto/models/dependencies.py) also define a custom
version/constraint language. The documented quirk is significant: dependency
comparison ignores prerelease suffixes although `Version` equality includes them.

The design in [#555](https://github.com/ludachrish3/otto-sh/issues/555) already makes
the appropriate distinction: otto owns repository availability and selected lab
project combinations; uv owns Python packages for the selected closure. It also
records that some unselected groups are expected to be unsatisfied. Therefore
“just use a uv workspace” is not an adequate replacement for otto's domain graph:
uv workspaces share a lock and resolve their members together.
[uv workspace contract](https://docs.astral.sh/uv/concepts/projects/workspaces/).

**Recommendation:** proceed with the agreed pyproject identity and uv-only direction,
while treating the unfinished group design as unfinished. Prioritize the current
install failure [#554](https://github.com/ludachrish3/otto-sh/issues/554). Use
[`packaging` specifiers](https://packaging.pypa.io/en/stable/specifiers.html) for the
adopted Python version semantics. Explicitly retire the old parser, pip fallback,
two-phase installation, and output-parsing heuristics as the replacement lands.
Keep local repository identities from falling through to public-index lookup.

**Acceptance:** one native resolution for otto and the selected Python dependencies;
non-package repositories work; missing optional repositories remain a meaningful
domain result; switching selections removes dependencies according to the chosen
sync policy. Do not build a second dependency solver around uv.

### 6. P1 — Docker is becoming a good wrapper; staging remains too much ownership

Current [`docker/build.py`](../src/otto/docker/build.py) always invokes Docker and
reads actual identifiers back. [`docker/observe.py`](../src/otto/docker/observe.py)
returns Docker's text. Real-daemon differential tests now exist in
[`test_docker_honesty.py`](../tests/e2e/docker/test_docker_honesty.py) and
[`test_docker_observe_honesty.py`](../tests/e2e/docker/test_docker_observe_honesty.py).
These are the right direction. The context-hash caching complaint in open
[#495](https://github.com/ludachrish3/otto-sh/issues/495) is historical at this HEAD;
likewise, the library now owns container-list parent selection addressed by
[#553](https://github.com/ludachrish3/otto-sh/issues/553).

The remaining concern is [`docker/staging.py`](../src/otto/docker/staging.py).
`stage_image_context` archives the entire local directory before Docker applies its
ignore rules on the parent. Consequently `.dockerignore` does not protect the
earlier transfer or staging copy, and unnecessary files still incur I/O. The
predictable `/tmp/otto-docker/<project>/build/<image>` directory is removed and
recreated; the source itself acknowledges cross-user collisions. Compose staging
also owns YAML inspection, relative-path handling, sidecar materialization, and env
file serialization.

**Recommendation:** test a native remote Docker client route for straightforward SSH
parents before further expanding staging. Docker supports SSH endpoints and
contexts; that is a candidate to eliminate build-context copying logic for that
transport. It does not automatically solve remote bind mounts, access through
Telnet/console-only parents, or lab-specific compose inputs.
[Docker's remote-access mechanism](https://docs.docker.com/engine/security/protect-access/).

Keep a fallback where those requirements demand it, with private staging directories,
explicit ownership, concurrency isolation, and cleanup. Use Compose's own
configuration rendering where feasible instead of approximating its complete
semantics in Python. Validate relocation and interpolation before adopting it:
[`docker compose config`](https://docs.docker.com/reference/cli/docker/compose/config/)
can normalize a model, but does not transfer its referenced files.

**Acceptance:** concurrent builds do not destroy each other's contexts; the ordinary
SSH route need not stage files excluded by Docker; secrets are private on fallback
paths; identifiers and common verb behavior remain covered by daemon differentials.
Finish [#571](https://github.com/ludachrish3/otto-sh/issues/571)'s first-deploy docs
before adding more curated verbs that contribute no lab-specific resolution.

### 7. P0 — Existing secret-handling issues reflect a missing architectural boundary

This is included because it directly illustrates the cost of wrapper-owned machinery,
not as a new audit of every security issue. Source confirms:

- [`suite/expect.py`](../src/otto/suite/expect.py), around lines 90–103, formats
  arbitrary caller locals using `repr` in failure reports.
- [`host/session.py`](../src/otto/host/session.py), around line 969, logs expect
  responses at DEBUG, which also carries elevation responses.
- [`host/options.py`](../src/otto/host/options.py) deliberately defaults
  `known_hosts=None`, disabling AsyncSSH host-key verification.
- Docker stages generated `otto.env` through ordinary file creation and `mkdir -p`,
  without establishing private remote permissions at that seam.

These correspond to [#548](https://github.com/ludachrish3/otto-sh/issues/548),
[#549](https://github.com/ludachrish3/otto-sh/issues/549),
[#552](https://github.com/ludachrish3/otto-sh/issues/552), and
[#550](https://github.com/ludachrish3/otto-sh/issues/550), all P0 but still To triage
on the board. No credentials were inspected or exploit scenarios executed here.

**Recommendation:** prioritize these over additional dashboard polish. Make secret
handling a transport-to-artifact rule: secret values do not have revealing reprs,
expect responses carry sensitivity, arbitrary local-variable dumps are opt-in and
redacted, staging establishes permissions before writing, and SSH verification is
secure by default with an explicit disposable-lab exception. Merely changing one
log statement or adopting `SecretStr` at one input does not cover all these paths.

**Acceptance:** use synthetic canary credentials through failure, DEBUG, JUnit,
monitor export, and staging paths, and assert where they may and may not appear.
Track the fixed-prompt replay issue
[#551](https://github.com/ludachrish3/otto-sh/issues/551) separately: redaction alone
does not solve authentication-response routing.

### 8. P1 — Churn controls observe the wrong portion of the public contract

[`api_snapshot.py`](../scripts/api_snapshot.py) covers exports, documented deep
imports, and Host method parameter names. It does not cover settings keys, lab
schema changes, CLI flags, or most behavioral defaults. Its Host snapshot explicitly
does not encode parameter kinds or defaults. Meanwhile, the common result payload
is `Any`, and `unsound-return-statement` is globally demoted in `pyproject.toml`.
Many important changes can therefore be green without being made visible to reviewers.

**Recommendation:** prioritize [#520](https://github.com/ludachrish3/otto-sh/issues/520).
Generate schema and CLI surface diffs from the real models/command metadata. Include
requiredness, value types, defaults, and positional-versus-keyword calling shapes
where those are part of the contract. Separate a change to what docs teach from a
change to what the API accepts. A visible breaking-change mark is useful, but is
not proof that the replacement behavior works.

The user's willingness to break APIs is an advantage: use it for deliberate
simplification. Do not impose a general compatibility freeze or cooling-off period
as a substitute for design. Batch related changes around one target workflow and
record what old surface is deleted. Prefer a few executable consumer examples over
an ever larger set of text goldens.

Also support [#527](https://github.com/ludachrish3/otto-sh/issues/527)'s single Host
declaration goal and [#532](https://github.com/ludachrish3/otto-sh/issues/532)'s test-double
conformance. Split the large Host/session modules by lifecycle, framing, execution,
and product operations only where ownership becomes clearer. Generic result typing
is a worthwhile later improvement at the most important call boundaries, not a
prerequisite for fixing the concrete issues above.

**Acceptance:** planted schema/flag/default changes produce a reviewable contract
diff; representative downstream projects either continue to work or fail with the
intended replacement guidance. No requirement to preserve the old API indefinitely.

### 9. P1 — Gate fidelity matters more than adding another coverage threshold

The inspected CI has Python version lanes, repeated unit runs, browser engines,
static analysis, docs, and BusyBox checks. Existing coverage floors are 95.5 locally
and 94.75 for hostless CI. CLI/library differential tests now cover test, coverage,
Docker, monitoring, init, and session preparation in addition to project operations.
It would be incorrect to recommend these as wholly missing.

The remaining gaps are more specific:

| Gap | Concrete action |
| --- | --- |
| Development dependencies mask runtime assumptions | Add the isolated installed-wheel smoke from finding 1. |
| Two otto paths can agree on the same wrong behavior | Keep direct expected-outcome witnesses alongside CLI/library equality; add native-tool comparisons. The new session differential tests already demonstrate this well. |
| Real Docker comparison tests are marked `integration` | Add a small hermetic real-daemon behavior lane on relevant PRs if the runner permits it; nightly Docker chaos already exists, but exercises a different purpose. |
| `gate-fresh` is described more broadly than it executes | Its current default targets are lint, architecture, API snapshot, Python types, collection, and docs. It explicitly dropped the hostless execution lane. State that scope clearly and separately test runtime behavior in a pristine tree. |
| Local and CI rendering environments differ | Complete [#572](https://github.com/ludachrish3/otto-sh/issues/572), and avoid relying on exact Rich line wrapping for semantic assertions. |
| Filesystem-operation measurements need reproducible conditions | Preserve filesystem-operation budgets as primary startup gates for NFS users. Control interpreter, bytecode/cache state, environment, and measured process scope; isolate any demonstrated unstable counter instead of moving all I/O budgets to monitoring. Keep import-boundary checks too. Reconsider #523's blanket relocation proposal in light of Chris's clarification below. |
| Coverage denominator can miss a required scenario | Assert lane collection and required capability cells as well as percentages; see [#404](https://github.com/ludachrish3/otto-sh/issues/404). |
| Dependency updates exercise only part of the compatibility promise | Test runtime minimum/latest plugin combinations and installed-wheel behavior; batch compatible updates and deliberate ty upgrades as proposed in [#533](https://github.com/ludachrish3/otto-sh/issues/533). |

**Chris's clarification, 2026-10-04:** NFS users are the main reason startup budgets
count filesystem operations instead of wall-clock time. File I/O is the expected
bottleneck, while CPU capability varies across users. A fast CI machine can hide
additional filesystem work that becomes expensive on NFS; its elapsed time is
therefore not a suitable replacement for the operation budget.

**Revised recommendation:** retain operation counts as the primary startup cost
contract. Distinguish the metric's value from defects in its measurement harness.
Import-module sets complement these counts but cannot catch extra `stat`, directory
enumeration, or file reads within modules already loaded. Fix reproducibility at
the affected measurement boundary, and move a counter out of the blocking lane only
when its instability is demonstrated and an adequate regression check remains.
Wall-clock measurements can supplement diagnosis and end-to-end NFS validation;
they should not replace the filesystem budgets. A docs-only red is a triage signal,
not automatic proof of harness failure—docs tests can expose real defects too.

Measured CI snapshot: the most recent 100 returned push-to-main CI records span
2026-09-12 through 2026-10-04 UTC, with 65 successes, 11 failures, and 24 cancellations.
That is **11/76 = 14.5% failures among non-cancelled results**. The newest 20 records
contain 13 successes and 7 cancellations, so this sample does not support a claim
that the latest work is making CI worse. These are returned run conclusions, not
a classification of first-attempt failures or their root causes.
[CI workflow history](https://github.com/ludachrish3/otto-sh/actions/workflows/ci.yml).

[#523](https://github.com/ludachrish3/otto-sh/issues/523) contains the earlier audit's
root-cause analysis of harness failures. Use
[#529](https://github.com/ludachrish3/otto-sh/issues/529) to make failure cause,
first-attempt red rate, rerun cost, cancellation rate, and gate duration routinely
visible. Do not turn the percentage above into a product defect rate.

### 10. P2 — Preserve specialized monitoring and coverage, but set their scale boundary

Monitoring has a defensible use case: an offline lab session with correlated test
events does not imply a requirement for a separately operated metrics service.
The implementation already has bounded metric/log rings and bounded subscriber
queues ([`store.py`](../src/otto/monitor/store.py),
[`broadcast.py`](../src/otto/monitor/broadcast.py)). Keep those choices.

The scaling questions are elsewhere. [`context.py`](../src/otto/context.py),
`do_for_all_hosts`, offers serial or unrestricted `gather`, with no concurrency
budget at that entry point. [`collector.py`](../src/otto/monitor/collector.py)
polls all entries in a bucket concurrently, waits for all results, then processes
them; [`db.py`](../src/otto/monitor/db.py), `write_point`, awaits a commit for every
metric point. Replay reads complete session rows into memory. These are mechanisms
that merit measurement, not proof of unacceptable performance at today's lab size.

**Recommendation:** define representative host counts, shared-hop contention,
metrics per host, and archive duration. Add bounded concurrency at the fleet and
shared-transport boundaries; benchmark batched writes, slow hosts, archive loading,
and cancellation. Keep asyncio ownership explicit and do not introduce thread-pool
fan-out. Add export/integration seams if long-running fleet observability becomes a
requirement rather than expanding the session viewer into that product by default.

Coverage's cross-target counter retrieval and build identity checks are stronger
reasons for custom code than generic report rendering. Keep the producer pipeline
independent of its SPA and ticket attribution. Standard-format export lets users
consume evidence without adopting every otto UI feature. Continue native compiler
and lcov delegation; [#519](https://github.com/ludachrish3/otto-sh/issues/519)'s custom
clang runtime should have an explicit supported-target justification and maintenance
budget. Its implementation was not audited here.

Reuse the existing schema-to-TypeScript mechanism for covapp as proposed in
[#591](https://github.com/ludachrish3/otto-sh/issues/591). Introducing another manually
maintained wire model would enlarge the same churn problem already solved for the
monitor.

## Proposed workboard sequence

The priorities below are review recommendations, distinct from the board's current
labels. No issue was opened, closed, or reprioritized during the initial review;
Chris subsequently requested #592 and #593, now filed as P0 - HIGH / Ready
(findings 2 and 1, respectively).

| Sequence | Work | Relationship to current board |
| --- | --- | --- |
| Now | Runtime-only pytest smoke and remove unintended pytest-cov requirement | New reproduced finding; coordinate with #469. |
| Now | Credential/expect/staging/SSH trust boundaries | Existing P0 #548–#552 still need triage; fix before adding more secret-carrying paths. |
| Next | Authoritative runtime collection with cached completion; preserve native config and arguments | Chris clarified the freshness boundary; promote #469, with cache-correctness work beyond #489/#490. |
| Next | Exact dependency permissions, cycle ratchet, public schema/CLI contract diffs | #522, #590, #520. Small protective changes before major extraction. |
| Continue | Finish CLI/library parity, especially reservation reporting | #525 is In progress and substantially implemented; #588 remains P0 Ready. |
| Continue | Fix installability, then simplify environment ownership | #554 and the agreed portions of #555; do not implement undecided group semantics. |
| Then | Docker staging ownership/native route and the first-deploy walkthrough | #550 and #571; retain the newly landed Docker behavior and differential tests. |
| Then | Test-double contracts, measured module splits, scale budgets, covapp codegen | #532, #527/#528, #591. Evidence should determine how far to take each. |

Keep the already-prioritized host correctness fixes
[#435](https://github.com/ludachrish3/otto-sh/issues/435) and
[#451](https://github.com/ludachrish3/otto-sh/issues/451) moving; a long architecture
program should not delay a concrete reboot failure. Likewise, the selected user
feedback's first-project walkthrough is worthwhile engineering: discovering that a
feature already exists is cheaper than adding a second configuration route for it.

## Clarified boundary and remaining decisions

1. **Clarified: cached completion, authoritative runtime collection.** Chris accepts
   slightly stale suggestions to avoid repeated NFS collection costs. The actual
   test run pays for collection to preserve accuracy. The implementation question is
   how to keep that to the run's necessary collection, without redundant preflight
   passes or cache-based omissions.
   Startup performance remains budgeted in filesystem operations to reflect NFS
   costs independently of users' CPU capabilities; measurement instability calls
   for harness repair, not replacing that contract with elapsed-time thresholds.
2. **Is persistent-fixture soak testing a core otto feature?** If yes, preserve it as
   a clearly separate mode; if no, adopt standard retry/repeat behavior and delete
   the bespoke protocol machinery.
3. **Can the next simplification tranche be judged by ownership removed?** For
   example: one resolver instead of two installers, no custom version grammar,
   native Docker transport where it fits, and fewer backward dependencies—not just
   more files, wrappers, and regression guards around the existing machinery.

The collection boundary preserves both NFS usability and trust in executed tests.
The installed-runtime failure is the most concrete place to start improving trust
in the release while the remaining design questions are resolved.
