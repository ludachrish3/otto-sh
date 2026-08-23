# One autouse fixture makes every test under `tests/integration/` destructive

## The defect

[tests/integration/conftest.py:140](../tests/integration/conftest.py#L140)
defines `reap_orphan_docker_stacks` as `scope="session", autouse=True`. When
**any** test anywhere under `tests/integration/` runs, it SSHes to
`_DOCKER_HOST_IP = "10.10.200.13"` (test3) and executes `docker rm -f` on every
container matching `-e2e-` or `-noexist-`, plus `docker network rm` on matching
networks. The only thing gating it is a 2s TCP preflight to port 22, so on a
reachable bed it always fires.

The reap logic itself is careful and its purpose is legitimate — leaked stacks
exhaust docker's address pool and wedge every later `compose up`. The defect is
narrower and worse:

**The blast radius is keyed on directory membership rather than on any test
actually needing docker.**

That distinction is the whole finding. A stale docstring can be corrected; a
fixture narrowed today goes quietly wrong again the moment the subtree grows.
Keyed on need, it cannot.

## The measurement

**27 of the 31 test files under `tests/integration/` never mention docker.**
The four that do are all top-level files:

```
tests/integration/test_docker_build.py
tests/integration/test_docker_compose.py
tests/integration/test_docker_in_instruction.py
tests/integration/test_docker_run_get_put.py
```

Everything else — the whole of `cov/`, `busybox_bed/`, `chaos/`, `logger/`,
`suite/`, and all ten files in `host/` — has nothing to do with docker
and still triggers the reap.

The fixture's own docstring asserts:

> All tests in this directory drive docker on a shared host

That premise is false for **87%** of the directory it guards. It was true when
written; the subtree outgrew it. Same shape as the recurring "a rationale is a
claim about the code" defect — the claim was never re-checked as the tree grew.

## Verified mechanics

Measured with a toy replica rather than by running anything against the real
bed:

- It fires on **run**, not on collection. `--collect-only` does not trigger it.
  (Worth stating precisely — the inverse mistake is issue #196, where a run
  precondition fired when nothing ran.)
- A parent conftest's autouse fixture reaches tests in **arbitrarily deep
  subdirectories**, which is why `cov/test_overrides_report.py` is caught.

## How it actually bites

Found 2026-08-16 when a session read
`tests/integration/cov/test_overrides_report.py`, saw `TmpGitRepo`,
`run_coverage_report` and no host imports, correctly concluded the *test* was
lab-free, and ran it. The reap executed twice against the live bed.

The file is genuinely innocent. **There is no reading of the test that would
have prevented this** — only reading the conftest would have, and nothing in
the test points at it. That is what makes directory-keyed blast radius
dangerous rather than merely surprising: the evidence you would naturally
gather is real, sufficient-looking, and silent about the hazard.

## The ask — two changes, not one

Narrowing and self-assertion are **not alternatives**. The second is cheaper
and is what stops the drift recurring.

1. **Key the fixture on need, not location.** No `docker` marker is registered
   in `pyproject.toml` today; the docker tests declare their need only by
   passing `docker_capable=True` to a host constructor inside the test body.
   Options worth costing: register a `docker` marker and gate the fixture on
   it; make it a non-autouse fixture the four files request explicitly; or move
   it into a conftest scoped to just the docker tests (note this requires
   moving those four files into their own subpackage, since a top-level
   conftest necessarily covers every subdirectory).

2. **Make the fixture assert its own premise.** When it is about to run for a
   test that does not declare a docker need, it should fail loudly rather than
   reap. This is the repo's existing rule — the premise of a test gets its own
   assertion — applied to a fixture. Without it, the next non-docker test filed
   under the tree silently re-creates the hazard, and a narrowed-but-unasserted
   fixture will be correct-and-dangerous again with no one noticing.

## Related

Same class as
[treewide-guards-invisible-to-scoped-runs-2026-08-16.md](treewide-guards-invisible-to-scoped-runs-2026-08-16.md),
inverted. There, a path-scoped run cannot reach a tree-wide guard. Here, a
path-scoped run reaches out and mutates shared lab state on another host. Both
are mismatches between what a run *selects* and what it *executes* — worth
fixing as a pair, since the mental model is the same.

Also touches the standing rule from issue #139 that docker belongs in only one
or two old-OS e2e tests: the more of the tree that transitively depends on the
docker host, the less that rule buys.
