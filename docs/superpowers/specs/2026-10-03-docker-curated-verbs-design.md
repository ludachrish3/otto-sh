# Curated docker verbs: docker's output, otto's resolution — design

**Date:** 2026-10-03
**Issue:** #569 (closes #553: the `ps` host rule moves into the library).
**Parent spec:** `2026-10-02-docker-thin-honest-design.md` §6 (the verb
surface), §8 (the honesty lane), §9 (errors and dry run). This document
settles the mechanism for work item 3 of that spec; it changes none of its
decisions.

## 1. What the parent spec fixes

Five verbs, with exactly the docker flags listed there and nothing of otto's
added inside docker's output:

| verb | resolves | docker flags carried |
| --- | --- | --- |
| `otto docker logs CONTAINER [--on HOST]` | a container host id, or a docker name/id with `--on` | `-f/--follow`, `--tail N`, `--since T`, `-t/--timestamps` |
| `otto docker compose logs [USE_CASE [SERVICE...]]` | use-case → project and host(s) | the same four |
| `otto docker compose ps [USE_CASE]` | use-case → project and host(s) | `-a/--all` |
| `otto docker images [--on HOST]` | every docker-capable host, or one | none |
| `otto docker ps [--on HOST]` | every docker-capable host, or one | `-a/--all` |

What docker printed is what otto prints. A verb that fans out adds one
header line per host and changes nothing inside it. `ps` gives up its Rich
table. Each verb is one library function, one leaf, one `_Verb` row, one doc
page; the read-only verbs stop at the dry-run seam; each has a daemon
differential before it is done.

## 2. The library: `otto.docker.observe`

One module for the read-only daemon questions. The parent spec's §7
(completion) will later record what these functions were told; nothing here
anticipates that beyond keeping docker's answer whole.

```python
@dataclass
class HostOutput:
    host_id: str
    command: str          # the exact docker command run on that host
    result: CommandResult # docker's output verbatim, and its status

@dataclass
class ObserveReport:
    hosts: list[HostOutput]
    @property
    def ok(self) -> bool: ...   # every host's result is_ok

def docker_parents(on: str | None = None) -> list[UnixHost]
async def list_images(on: str | None = None) -> ObserveReport
async def list_containers(on: str | None = None, *, all: bool = False) -> ObserveReport
async def compose_ps(use_case: str, *, all: bool = False) -> ObserveReport
async def compose_logs(use_case: str, services: Sequence[str] = (), *,
                       tail: str | None = None, since: str | None = None,
                       timestamps: bool = False) -> ObserveReport
async def container_logs(container: str, *, on: str | None = None,
                         tail: str | None = None, since: str | None = None,
                         timestamps: bool = False) -> ObserveReport
async def follow_logs(target: LogsTarget) -> None
def resolve_logs(container: str, *, on: str | None = None) -> LogsTarget
def resolve_compose_logs(use_case: str, services: Sequence[str]) -> list[LogsTarget]
```

- `docker_parents` is #553's rule, moved: `None` is every docker-capable
  unix host of the lab, in lab order; a name that is not one refuses with
  `DockerVerbError(field="host")` listing the capable ids. The build verbs'
  private `_docker_parent` becomes this module's `docker_parent` (one host,
  required) and both share one refusal text.
- `DockerBuildError` is renamed `DockerVerbError` ("a docker verb's input is
  unusable; nothing was touched") — one field-named refusal class for the
  build and the observe verbs, caught at the CLI's one translation site. No
  alias is kept.
- The library parses nothing. `HostOutput.result.value` is docker's text as
  the host returned it; `command` is the string that produced it. No column
  is re-ordered, no id shortened.
- The existing dict-returning `compose_ps` in `otto.docker.compose` is
  deleted with the table that consumed it.

## 3. Resolution

**`container_logs`.** `container` is looked up in the lab first. A
`DockerContainerHost` id (`test3.integration.web`) names its parent and its
compose project + service; the container is found on the parent with
`docker ps -aq --filter label=com.docker.compose.project=… --filter
label=com.docker.compose.service=…` (`-a`: a stopped container's logs are
still docker's to print); no container → `DockerVerbError(field="container")`
naming the id and the parent. With `--on HOST`, `container` is handed to
`docker logs` on that host verbatim, and an unknown name is docker's error.
Neither (no `--on`, and not a container host id) →
`DockerVerbError(field="container")` listing the lab's container host ids.

**`compose_ps` / `compose_logs`.** They run the pure prefix `deploy`,
`teardown` and `deployed` share (`select_fragments` → `resolve_placement` →
`_acting_hosts`), so the four verbs cannot disagree about what a deployment
is. The leaf defaults an omitted `USE_CASE` the way `compose up`'s leaf does
(the sole declared use-case, else a refusal naming them); the library takes
the name. Per acting host the command is `docker
compose -p <use_case_project(...)> ps|logs …` with no `-f`, as `teardown`
already does: the project label is the whole input. `SERVICE...` is checked
against the declared services with `_validated_services`, as `up` does.

**`LogsTarget`** is `(parent: UnixHost, command: str)`: the one resolved
thing both the bounded and the following forms run. `container_logs` and
`compose_logs` build their targets with `resolve_logs` /
`resolve_compose_logs` and `exec` them; `follow_logs` takes one target and
bridges it (§4).

## 4. `--follow`

The host layer has no streaming exec: `exec` and `run` collect a command's
output and return. A follow rides the PTY bridge `otto host <container>
login` already uses — `run_ssh_login(conn, host_name, command=…)` on the
parent's SSH connection — with the resolved `docker logs -f …` /
`docker compose -p … logs -f …` as its command. The user's terminal is the
process's terminal: output arrives as docker writes it, Ctrl-C reaches docker
and ends the follow, and the transcript lands in `session.log` as every
bridged session does.

- SSH parents only. A parent whose transport is telnet refuses before any
  connection: `DockerVerbError(field="follow")`, `--follow needs an SSH
  parent; <host> is reached by telnet`.
- `compose logs -f` on a use-case placed on more than one host refuses the
  same way (`field="follow"`): one terminal follows one process. Without
  `-f` the fan-out prints every host.
- The bridge needs a terminal. Without one (stdin is not a tty), the refusal
  is the bridge's own, as `login` gives it.

## 5. The CLI

Five leaves in `otto.cli.docker`, each parse → one library call → render,
registered through `_VERBS` with the seam defaults (`output_dir=False`, no
dry-run preview: the preamble stops above the leaf, as `ps` does today).

Rendering, one function for every `ObserveReport`:

```text
== test3 ==
<docker's output, untouched>

== test4 ==
<docker's output, untouched>
```

The header is printed for every fan-out verb, `--on` or not, so the shape is
stable for a script. `logs` (one container, one host) prints docker's output
alone. A host whose command failed prints docker's whole error under its
header; the verb goes on to the next host and exits 1 at the end. Nothing is
printed between docker's lines.

Refusals reach the user through `_run_docker`'s existing arms; `_BUILD_FLAGS`
(renamed `_DOCKER_FLAGS`) gains `container → CONTAINER`, `follow → --follow`,
`use_case → USE_CASE`, `services → SERVICE`.

## 6. Tests

- Unit (`tests/unit/docker/test_observe.py`): every refusal in §3–§4 by
  field; the exact command string per verb and flag combination, pinned with
  a recorder host; the report shape; `docker_parents` ordering. Rendering
  (`tests/unit/cli/test_docker_observe_render.py`): header, blank line
  between hosts, verbatim body, exit 1 on a failing host, no header for
  `logs`. The bridge is pinned at its seam: the `(parent, command)` handed to
  it, and the telnet / multi-host refusals; no PTY is opened in a unit test.
- `tests/unit/cli/test_docker_differential.py` gains one row per verb
  (#525): the leaf hands the library exactly what the flags said.
- Honesty lane (`tests/e2e/docker/test_docker_observe_honesty.py`): one test
  per verb through the CLI on the real daemon — every image reference, image
  id, container id, container name and state the verb printed is checked
  against `docker images --no-trunc`, `docker ps -a --no-trunc` and `docker
  image inspect` on that host; `ps` and `images` across every docker-capable
  host; `compose ps` / `compose logs` / `logs` against a stack the test
  brings up with `deployed(...)`. `--follow` is covered at the seam only.

## 7. Documentation

New pages `docs/cli/docker/logs.md`, `images.md`, `compose/logs.md`,
`compose/ps.md`; `ps.md` rewritten for the new output. Each page shows the
verb, its docker flags, and one real capture. The index gains the rule in
one line: a curated verb exists only where otto resolves a container host id
or a use-case, or fans out over hosts; everything else is `otto host <HOST>
exec "docker …"`. API page for `otto.docker.observe`.

## 8. Order of landing

One verb per commit, each with its differential: `docker_parents` + `ps`
(closes #553) → `images` → `compose ps` → `compose logs` → `logs` →
`--follow`. The `DockerBuildError` rename lands with the first.
