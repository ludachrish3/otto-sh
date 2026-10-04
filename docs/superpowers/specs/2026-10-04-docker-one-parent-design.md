# Docker verbs: one use-case, one parent — design

**Date:** 2026-10-04
**Status:** approved in conversation; spec for review
**Supersedes:** the placement machinery of `2026-08-30-docker-use-cases-design.md`
§5 (knobs 2–4), the `--on` flag of `2026-09-30-docker-verbs-align-with-docker-design.md`,
and the `logs` container-host-id route plus the no-`--on` candidate union of
`2026-10-04-docker-completion-design.md` §5.

## 1. Why

A use-case today resolves *which host each fragment lands on* through four
knobs — `--on`, a committed `placement` pin keyed by `role`, a unique host
carrying the fragment's `role` within the repo's scope, and "exactly one
docker-capable host in scope". That is an otto-only DSL (role, pin, scope,
fallback) a user must learn before the first `compose up` works on a two-host
lab, and it is what made `logs CONTAINER` mean two different things depending
on `--on`. The first external feedback on the docker verbs was that the host
is ambiguous.

Docker's own model has no placement: a daemon is a host, and a stack runs on
the daemon you point it at. This design adopts that model. **A use-case
deploys on one parent host**, named by `--parent`, or chosen by one declared,
deterministic default. Roles, pins and scope-based placement are removed. The
host is otto's "parent" everywhere — the word the code, the facts and the docs
already use for the docker-capable host under which containers live.

Fragment *composition* (same-named fragments across repos form one use-case;
`provides`/`priority` provider competition) is unchanged and out of scope.

## 2. The rule

One library function owns host resolution:

```python
def default_docker_parent(lab: Lab) -> UnixHost:
```

Among the docker-capable unix hosts of the active lab selection (what `-l`
selected, in the session's `Lab`):

1. exactly one → that host;
2. several → the one whose `docker_priority` is strictly highest;
3. a tie at the top (including every host at the default `0`) → refuse,
   naming the tied hosts: `lab 'east' has 3 docker-capable hosts at priority 0
   (alt2, test1, test3) — name one with --parent, or rank one higher with
   "docker_priority" in lab.json`;
4. none → refuse: `lab 'east' has no docker-capable unix host`.

`--parent HOST` (library `parent=`) always overrides the rule. An explicit
parent is checked by `docker_parent(lab, id)` (never ranked): it must exist in
the lab and be a docker-capable unix host, and the refusal carries
`field="parent"` like the rule's. Per-repo scope is not consulted by
either path — an explicit parent already bypasses scope today, and the
default follows the same rule so there is one rule.

There is no "first in file order", no role, no pin. Every default is either
forced (one host) or declared (`docker_priority`).

### 2.1 `docker_priority`

A new optional integer on the unix host spec in `lab.json`, default `0`:

```json
{ "ip": "10.10.200.3", "element": "test3", "docker_capable": true, "docker_priority": 10 }
```

Lab intent, like `docker_capable` — never an inventory fact. Meaningful only
on a docker-capable host; a non-docker-capable host declaring it is refused at
lab load ("docker_priority on 'dut1', which is not docker_capable"). Negative
values are allowed (rank a host below the default). The field replaces `roles`,
which is removed from the host spec.

## 3. CLI surface

`--on` is deleted outright (not hidden, not aliased) and `--parent HOST` takes
its place on every docker verb. Docker's own `-H/--host` names a daemon
socket; otto's `--parent` names the lab host whose daemon runs the stack, and
the flag's help says exactly that.

| verb | `--parent` omitted means |
| --- | --- |
| `compose up USE_CASE [SERVICE…]` | the default parent (§2); refused when the rule refuses |
| `compose down USE_CASE [SERVICE…]` | same |
| `compose build USE_CASE [IMAGE…]` | same |
| `build [IMAGE…]` | same (today `build` refuses an omitted host; it now defaults like the rest) |
| `logs CONTAINER` | same — CONTAINER is a docker container name or id on that parent |
| `compose logs USE_CASE [SERVICE…]` | the parent the use-case is up on |
| `ps`, `images`, `compose ps` | every docker-capable host of the lab selection, as today (the listing verbs are the one place "all of them" is a fact, not a choice) |

`logs CONTAINER` has one contract: a docker name or id, handed to
`docker logs` verbatim on the parent. The container-host-id route
(`logs test3.integration.api`) is removed; `compose logs USE_CASE SERVICE` is
the use-case-native form and needs no host.

`--parent` completes from the lab selection's docker-capable hosts
(`_docker_host_completer`, unchanged but renamed to match).

Every refusal from the rule or the explicit check is a `DockerVerbError`
with `field="parent"`, so the library's message and the CLI's flag finally
spell the same word; the `"host": "--on"` entry in the CLI's flag map is
deleted.

## 4. Library surface

Every `on=` keyword becomes `parent=`, and every entry point that takes it
calls `default_docker_parent(lab)` when it is `None` — the CLI never computes
a default. Affected (rename, same semantics unless noted):

- `otto.docker.deployment`: `deploy(use_case, *, parent=None, …)`,
  `teardown(use_case, *, parent=None, …)`, `deployed(…)`,
  `resolve_use_case(use_case, *, parent=None, provide=None)`;
  `_canonical_on` → `_canonical_parent`.
- `otto.docker.build_verbs`: `build_on(host, …)` → `build_on(parent=None, …)`
  (the refusal of `None` moves into the rule); `compose_build(use_case, *,
  parent=None, …)`.
- `otto.docker.observe`: `list_containers(parent=None, …)`,
  `list_images(parent=None)`, `compose_ps(use_case, *, parent=None, …)`,
  `resolve_compose_logs`/`compose_logs(…)` (the parent the use-case is up
  on, unchanged), `resolve_logs(container, *, parent=None, …)`,
  `container_logs`/`follow_logs(…)`; `docker_parents(lab, parent)` keeps
  its fleet-or-one meaning for the listing verbs; `docker_parent(lab, id)`
  unchanged.
- `otto.docker.resolve`: `resolve_placement(selection, lab, *, on=None)` →
  `place(selection, parent: UnixHost) -> dict[str, list[SelectedFragment]]`,
  one entry, every selected fragment on `parent.id`. `UseCaseResolutionError`
  stays for configuration refusals that are not about the host (composition,
  env facts).

The probes `observed_images(host_id)` / `observed_containers(host_id)` keep
their positional host id (they are given a parent that already resolved).

## 5. Settings and lab changes (breaking)

Removed, refused by name at parse time with a message that names the
replacement:

- `[[docker.use_cases]].role` — "role is gone: a use-case deploys on one
  parent; name it with --parent or rank a host with docker_priority".
- `[[docker.use_cases]].placement` — same message.
- host spec `roles` — "roles is gone; see docker_priority".
- env fact refs `{{role.<name>.<fact>}}` — refused by `resolve_fact_refs`
  with "role facts are gone; use {{parent.<fact>}}". `{{parent.*}}` and
  `{{host.*}}` are unchanged.

Added: host spec `docker_priority: int = 0` (§2.1).

## 6. Resolution internals

Deleted: `_place_fragment`, `_validate_pin`, `_PLACEMENT_KNOBS`, the four
placement refusals, `RoleFact` and the role branch of `build_facts`,
`DockerUseCase.role` / `.placement`, `HostSpec.roles`.

Kept with their shapes, now only ever seeing one parent (no rewrite for its
own sake): `acting_hosts`, `_plan`, `_rollback`, `UseCaseStack.by_host`,
`BuildReport.repos`/hosts, `parent_for`.

`otto docker use-cases` (the dry-run plan) prints the parent as given or as
the rule chose it, and drops its placement column; a refusal of the rule is
reported there the way `deploy` would report it.

A multi-host deployment is two use-cases, each brought up with its own
`--parent`. Cross-host env references between them are the user's (`env` /
`pass_env` on the second use-case), not otto's.

## 7. Container-host placeholders

`_register_use_case_placeholders` registers `<parent>.<usecase>.<service>`
for a repo's fragments **only when `default_docker_parent(lab)` resolves**;
when the rule refuses, no placeholder is registered and the walk stays
silent (it runs at the start of every invocation). The completion sources
that synthesize container ids (`collect_host_ids`, `collect_host_ids_by_lab`
via `_declared_container_ids`) use the same rule, so TAB and the lab agree
on exactly which container hosts exist before the first `up`. After `up`,
`register_stack_hosts` registers under the parent actually used, as today.

Consequence stated plainly: a lab with several docker-capable hosts and no
`docker_priority` has no container hosts until a stack is up, so first-touch
auto-up of `otto host <container-id>` works only where the rule resolves.

## 8. Completion

### 8.1 Cache

The names section gains `docker_default_parent_by_lab: {lab: host_id}`,
built by a new collector from the same per-lab host data the cache already
holds plus each host's `docker_priority`; a lab the rule refuses is absent
from the map. `SCHEMA_VERSION` → 26 (both sides). The `__docker_observed__`
namespace is unchanged.

### 8.2 The one rule for CONTAINER and `--tag`

With `--parent` on the line, candidates are that host's observed names then
ids (containers) or refs (`--tag`), as today. **Without it, candidates are
the default parent's** — the parent the verb itself would use — read from
`docker_default_parent_by_lab` for the active lab selection; when the map
has no entry (the rule refuses), nothing is offered, and the verb would
refuse too. TAB never offers what the verb rejects.

Removed: the three-tier union, the cross-host dedup, the dotted-id
"container host id" tier and its rule, `_container_candidates` on both
sides. `_observed_by_host` goes with them — every observed answer now reads
one host's entry.

The shim mirrors the default-parent lookup (lab selection → map entry) with
no new file read; the `by_option="parent"` rows replace `by_option="on"`.
The differential's seed gains `docker_default_parent_by_lab` and the
hand-written docker lines are reworded for `--parent`, with two new lines
pinning the default-parent path (`otto docker logs ` and
`otto docker build --tag ` with no flag, answered from dut1 when it is the
lab's only docker host) and one pinning the refusal (a two-host selection
with equal priority → both sides answer nothing).

## 9. Docs

- `docs/cli/docker/index.md`: a "Which host" section (the rule, the flag,
  `docker_priority`, the note on docker's `-H`) that every verb page links
  to; the Completion section loses the tier description.
- `docs/cli/docker/use-cases.md`: placement section replaced by one
  paragraph pointing at "Which host"; `role` leaves the example.
- `docs/cli/docker/logs.md`: one contract.
- `docs/configuration/lab-config.md`: `docker_priority` beside
  `docker_capable`; `roles` removed. `docs/configuration/settings.md`:
  `role`/`placement` removed from the use-case table.
- `docs/architecture/subsystems/docker-hosts.md` and `completion-cache.md`:
  the rule, the new key, schema 26.

One home: the rule is described once, on `docker/index.md`.

## 10. Testing

- Unit, `default_docker_parent`: one host; several with a unique top
  priority; a tie at the top (two at 10; all at 0); none; a priority on a
  non-docker-capable host refused at lab load; negative priority ranks
  below 0.
- Unit, every acting verb: `--parent` given → used; omitted → the rule's
  choice; the rule refuses → exit 2 with the tied hosts named and nothing
  run (dry-run shows no plan).
- Unit, parse-time refusals for `role`, `placement`, `roles`, `{{role.*}}`.
- Unit, placeholders: registered under the default parent; none when the
  rule refuses; `collect_host_ids` agrees.
- Completion: `logs`/`--tag` with and without `--parent`, with and without a
  resolvable default; the Typer never-bootstrap pin stays.
- Shim: the differential's reworded lines plus the three new pins (§8.2);
  the cache key-set pin and schema 26 on both sides.
- e2e (bed): `compose up integration` with no flag on the single-docker-host
  bed brings the stack up on that host and `logs <name>` with no flag prints
  that container's logs; both honesty differentials keep passing.

## 11. Out of scope

Fragment composition and the provider competition (`provides`, `priority`
on fragments, `--provide`) — a separate question with its own trade-off.
The listing verbs' fleet semantics. The observed cache's TTLs and shape.
