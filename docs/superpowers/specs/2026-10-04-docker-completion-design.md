# Docker completion: declared names, and what the daemon last said — design

**Date:** 2026-10-04
**Issue:** #570.
**Parent spec:** `2026-10-02-docker-thin-honest-design.md` §7 (completion). This
document settles the mechanism for work item 4 of that spec and amends one
number in it (§2 below: two TTLs, not one).

## 1. What the parent spec fixes

Two sources, by where the truth lives. **Declared names** come from
configuration and never expire: `IMAGE` on `build` / `compose build`,
`SERVICE` on `compose up/down/logs` (the services of the `USE_CASE` already on
the line), `--repo`, and the container host ids `otto host` already
synthesizes. **Observed docker state** is the daemon's and short-lived: image
references, image ids, container names and container ids, per host.

Observed state is written only as a by-product of a verb that already asked
the daemon, under a reserved top-level key of the completion cache beside
`__dynamic_tunnels__`, bounded to 200 references and 200 containers per host.
**A TAB never contacts a host.** It reads the cache, drops what is past its
TTL and offers the rest; with nothing fresh it offers the declared names
only. A completed id can be stale: a hint going stale, not otto asserting
state. The bash shim answers from the namespace the same way Typer's
completer does, and the differential pins them against each other.

## 2. The namespace

In `otto.config.completion_cache`, by the pattern of `record_tunnel_ids` /
`read_tunnel_ids` (read, merge, atomic write; skipped on an ephemeral
inventory fingerprint):

```python
DOCKER_OBSERVED_KEY = "__docker_observed__"
DOCKER_OBSERVED_SCHEMA_VERSION = 1
DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS = 15 * 60
DOCKER_OBSERVED_IMAGES_TTL_SECONDS = 24 * 60 * 60
DOCKER_OBSERVED_CAP = 200

@dataclass(frozen=True)
class ObservedDockerState:
    image_refs: list[str]        # "repo:tag", never "<none>:<none>"
    image_ids: list[str]
    container_names: list[str]
    container_ids: list[str]

def record_docker_images(repos, host_id, *, refs, ids) -> None
def record_docker_containers(repos, host_id, *, names, ids) -> None
def read_docker_observed(repos, host_id) -> ObservedDockerState
def read_docker_observed_hosts(repos) -> list[str]   # host ids with ANY fresh sub-entry
```

The entry is keyed by host id, and each host holds two **independently
stamped** sub-entries, because a verb that asked about images knows nothing
about containers:

```json
"__docker_observed__": {
  "schema_version": 1,
  "hosts": {
    "test3": {
      "images":     {"observed_at": 1759530000, "refs": ["repo1-api:latest"], "ids": ["sha256:…"]},
      "containers": {"observed_at": 1759530100, "names": ["unix-integration-ci-api-1"], "ids": ["3f9a…"]}
    }
  }
}
```

- **Two TTLs, by how fast the truth moves** (amending parent §7's single
  `DOCKER_OBSERVED_TTL_SECONDS = 900`): containers come and go, so a
  containers sub-entry lives 15 minutes; an image reference is a hint still
  worth offering a day later, so an images sub-entry lives the main cache's
  day. Each sub-entry is judged against its own TTL; an expired one is
  simply absent from the read, never deleted on a TAB (a TAB writes nothing).
- A writer **replaces its sub-entry whole** for that host and leaves the
  other sub-entry alone. Every list is cut at `DOCKER_OBSERVED_CAP` in the
  daemon's order.
- `read_docker_observed` returns empty lists for a cold, expired, malformed
  or other-schema entry; it never raises on the TAB path.
- `otto cache info` reports the namespace in one line (the tunnels
  namespace has none today; this is the first observed namespace a person
  may want to see): the hosts with a fresh images entry and the hosts with a
  fresh containers entry, or "nothing observed".

## 3. The writers: a second, machine-readable ask on the same session

`ps`, `images` and `compose ps` print docker's text verbatim and parse
nothing (parent §6; #569). They do not parse it for the cache either. A
writing verb asks the daemon **once more, in a shape made for a program**,
on the session the verb already holds open, and records that answer. Two
library functions in `otto.docker.observe`, both `LogMode.QUIET`, both plain
data:

```python
async def observed_images(parent: UnixHost) -> ObservedImages        # refs, ids
async def observed_containers(parent: UnixHost) -> ObservedContainers  # names, ids
```

- `observed_images` runs `docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}'`
  and drops `<none>:<none>` rows (a dangling layer is nothing a person types).
- `observed_containers` runs `docker ps -a --format '{{.Names}}\t{{.ID}}'`
  (`-a`: a stopped container's logs are still docker's to print, so its name
  is still worth completing).
- Both are **host-wide**, never project-scoped: "replacing that host's entry"
  is then exactly true, and `compose up integration` does not hide another
  stack's containers from `logs <TAB>`.
- A failed or declined probe returns empty lists and the caller records
  nothing: a probe is never a fact the verb reports.

The **CLI leaf** calls the probe after the verb's own work and records the
answer — the cache is a completion concern, and `record_tunnel_ids` lives in
the CLI for the same reason. One helper in `otto.cli.docker`,
`_record_observed(hosts, *, images: bool, containers: bool)`, called by:

| verb | records | hosts |
| --- | --- | --- |
| `images` | images | `report.hosts` |
| `build`, `compose build` | images | the hosts the build report names |
| `ps`, `compose ps` | containers | `report.hosts` |
| `compose up` | containers | `stack.by_host` |
| `compose down` | containers | the hosts the teardown report names |

Best-effort end to end: the helper catches everything, logs one DEBUG line
to `verbose.log`, and never changes the verb's output or exit. A dry run
asked the daemon nothing and writes nothing. A verb that refused before
touching a host writes nothing. The library functions `list_images`,
`compose_ps`, … are unchanged: a suite calling them does not write a
completion cache.

## 4. Declared names

Three keys join the `names` section, built in
`otto.config.cache_sections._collect_names` from parsed settings alone (no
lab, no bootstrap), keyed by the same paths the section already keys on:

| key | value | from |
| --- | --- | --- |
| `docker_images` | sorted, deduped image names | `repo.docker_settings.images[*].name` |
| `docker_services_by_use_case` | `{use_case: sorted services}` | the `services` of every `DockerCompose` a `DockerUseCase` of that name references, across repos |
| `repos` | sorted repo names | `repo.name` |

Container host ids need no new key: they are in `hosts`, and
`host_classes_by_id` says which are `DockerContainerHost`.

## 5. The consumers

Each is one Typer completer with a `@completion_source` row, one shim mirror,
and differential rows (§7). Candidates are prefix-filtered by the fragment,
as every completer here is.

| site | offers |
| --- | --- |
| `build IMAGE…`, `compose build USE_CASE IMAGE…` | `docker_images` |
| `compose up/down/logs USE_CASE SERVICE…` | `docker_services_by_use_case[USE_CASE]` for the use-case already on the line; nothing until one is |
| `--repo` | `repos` |
| `logs CONTAINER` | container host ids, then observed container names, then observed container ids — the `--on` host's when `--on` is on the line, else the union over every host with a fresh containers entry |
| `--tag` (on `build`) | observed image references on the `--on` host; nothing without `--on` |

`--tag` offers every fresh reference on the host. The parent spec's "for the
selected image" narrowing (references whose repository matches the `IMAGE`
on the line) is not done: the typed prefix already narrows, and the
narrowing would need an image-to-repository lookup on the TAB path.

Order within `logs CONTAINER` is deliberate — a host id is otto's own name
and is right whatever the daemon holds; an observed name is a hint; an id
is a hint a person rarely types.

## 6. The shim

`otto._shim_complete` gains one source kind, `observed`, with
`key="images" | "containers"` and the `host_scoped` convention the login
completers use (the `--on` value on the line, if any):

- It reads `__docker_observed__` from the same file it already holds open,
  applies the same two TTLs to `observed_at` (`time.time()` arithmetic, as
  the tables' TTL is applied), and answers. **No handover**: a warm TAB stays
  one file read, and its file operations do not grow with what a daemon
  holds (the cap is the bound).
- The three declared keys ride the existing `payload` kind; the use-case
  scoping of `SERVICE` mirrors `_payload_values`'s lab scoping: the
  `USE_CASE` positional already parsed from the line selects the map entry.
- The `logs CONTAINER` order (host ids, names, ids) is produced by one
  function, `_container_candidates`, named after the Typer function it
  mirrors; change both or neither.

The constants (`DOCKER_OBSERVED_KEY`, the schema version, both TTLs) are
pinned equal to the Python side by `tests/unit/shim`, as `SCHEMA` and
`TABLE_TTL_SECONDS` are today.

## 7. Tests

- **Namespace** (`tests/unit/config/test_completion_cache_docker.py`): record
  then read; images and containers stamped independently; a containers entry
  16 minutes old is absent while the host's 2-hour-old images entry is still
  served; a 25-hour-old images entry is absent; the cap; a writer replaces
  its own sub-entry and leaves the other; malformed and other-schema entries
  read empty; an ephemeral fingerprint writes nothing.
- **Probes** (`tests/unit/docker/test_observe.py`): the exact command string
  of each probe; the parse of its `--format` lines with a recorder host,
  including a `<none>:<none>` row and a failed probe (empty lists, nothing
  raised); `LogMode.QUIET`.
- **Writers** (`tests/unit/cli/test_docker_observed_record.py`): each leaf in
  the table records after its verb for the hosts its result names; a dry
  run records nothing; a verb refused by field records nothing; a probe that
  raises leaves the verb's output and exit untouched.
- **Completers** (`tests/unit/cli/test_docker_completion.py`): each row of
  §5, including the `--on` scoping and the union, the `SERVICE` scoping by
  the `USE_CASE` on the line, and the host-ids-then-names-then-ids order.
- **Differential** (`tests/unit/shim/test_differential.py`): the world gains
  declared images, services and repos and a seeded `__docker_observed__`
  with one fresh and one expired sub-entry per kind; hand-written lines for
  every §5 site, with and without `--on`, so the shim and Typer agree
  including on what the TTLs drop.
- **Honesty** (`tests/e2e/docker/test_docker_observe_honesty.py`): after
  `otto docker ps` on the leased host, the cache's containers entry for that
  host equals `docker ps -a --format '{{.Names}}\t{{.ID}}'` run there; after
  `otto docker images`, the images entry equals the `--format` listing minus
  `<none>:<none>`.

## 8. Documentation

- `docs/cli/docker/index.md`: a "Completion" paragraph — declared names
  always; observed names for 15 minutes (containers) or a day (images) after
  a verb asked the daemon; a TAB never contacts a host; which verbs write.
- `docs/architecture/subsystems/completion-cache.md` (where
  `__dynamic_tunnels__` is described) gains the namespace, both TTLs and the
  cap.
- `docs/cli/docker/logs.md` and `build.md`: one sentence each on what TAB
  offers for `CONTAINER` and `--tag`.
- `docs/api/docker/observe.rst` picks up the two probes.

## 9. Order of landing

Namespace + probes (library, no consumer) → declared keys + shim mirrors →
writers → consumers + differential rows → docs. One `feat(docker)` squash.
