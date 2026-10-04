# otto docker

`otto docker` builds container images and deploys **use-case** stacks on the
lab's docker-capable hosts. The containers it brings up become first-class lab
hosts: they appear in `--list-hosts` and accept every `otto host` verb.

A use-case is one named deployment that several active repos contribute
fragments to; `compose build`, `compose up`, `compose down`, `compose ps` and
`compose logs` all speak it.
{doc}`use-cases` is the workflow home for that model — start there.

```{raw} html
:file: ../../_static/generated/termynal/help-docker.html
```

(docker-verbs)=
## Verbs

| otto | docker analogue | scope | `--on` |
| --- | --- | --- | --- |
| `otto docker build --on HOST [--repo NAME] [IMAGE...] [-t REF]... [--no-cache] [--pull] [--build-arg K=V]... [--target STAGE]` | `docker build` | the selected repos' images, on one host | required |
| `otto docker ps [-a] [--on HOST]` | `docker ps` | containers per host, as docker prints them | optional |
| `otto docker images [--on HOST]` | `docker images` | images per host, as docker prints them | optional |
| `otto docker logs CONTAINER [--tail N] [--since T] [-t] [-f|--follow] [--on HOST]` | `docker logs` | one container's log, found by host id or named on a host | optional |
| `otto docker use-cases [USE_CASE]` | none | declared inventory, config only | none |
| `otto docker compose build [USE_CASE [IMAGE]...] [--on HOST] [--provide CAP=REPO]... [--no-cache] [--pull] [--build-arg K=V]...` | `docker compose build` | the images `up` would deploy, placed by the engine | optional collapse |
| `otto docker compose up [USE_CASE [SERVICE]...] [--on HOST] [--build] [--force-recreate] [--pull POLICY] [--provide]... [--env]... [--env-file]...` | `docker compose up` | deploy a use-case | optional collapse |
| `otto docker compose down [USE_CASE [SERVICE]...] [--on HOST] [--provide]...` | `docker compose down` | tear a use-case down | optional collapse |
| `otto docker compose ps [USE_CASE] [-a] [--on HOST] [--provide]...` | `docker compose ps` | the use-case's containers per host, as docker prints them | optional collapse |
| `otto docker compose logs [USE_CASE [SERVICE]...] [--tail N] [--since WHEN] [-t] [-f|--follow] [--on HOST] [--provide]...` | `docker compose logs` | the use-case's logs per host, as docker prints them | optional collapse |

`build` builds images and nothing else: it needs a host (`--on`), not a
use-case. Everything use-case-scoped is a `compose` verb, mirroring docker's
own `docker build` / `docker compose` split.

Both build verbs take `docker build`'s own flags; {doc}`build` is the one home for them.

A curated verb exists only where otto adds something docker cannot do from the host's shell: resolving a container host id ({doc}`logs`) or a use-case ({doc}`compose/ps`, {doc}`compose/logs`) to the real container or project, or fanning out over the lab's docker hosts ({doc}`ps`, {doc}`images`). Each prints what docker printed: every verb that may reach several hosts prints it under one `== host ==` line per host (whether one host or ten answered); `logs`, one container on one host, prints docker's lines alone. Everything else is `otto host <HOST> exec "docker …"`:

```text
otto host test3 exec "docker ps --format '{{.ID}} {{.Names}}'"
```

## Completion

TAB always offers the names a repo declares: the images in `[[docker.images]]`,
the services of the use-case already on the line, the repo names, and the
lab's container host ids. It adds what a docker daemon last said, once a verb
has asked it: `ps`, `compose ps`, `compose up` and `compose down` record that
host's containers, `images`, `build` and `compose build` its images, and only for the
host it asked. Those names are offered at `logs CONTAINER` ({doc}`logs`) and
`build --tag` with `--on` ({doc}`build`). A TAB never contacts a host; it reads
what the last verb recorded. `otto cache info` ({doc}`../cache/index`) shows
which hosts still have something vouched for, and
{doc}`../../architecture/subsystems/completion-cache` is the home for which
verb records what, for how long, and how the record is kept.

## Synopsis

```text
otto docker build     --on HOST [--repo NAME] [IMAGE...] [-t/--tag REF]... [--no-cache]
                      [--pull] [--build-arg K=V]... [--target STAGE]
otto docker ps        [-a|--all] [--on HOST]
otto docker images    [--on HOST]
otto docker logs      CONTAINER [--tail N] [--since T] [-t|--timestamps] [-f|--follow]
                      [--on HOST]
otto docker use-cases [USE_CASE]
otto docker compose build [USE_CASE [IMAGE]...] [--on HOST] [--provide CAP=REPO]...
                          [--no-cache] [--pull] [--build-arg K=V]...
otto docker compose up    [USE_CASE [SERVICE]...] [--on HOST] [--build] [--force-recreate]
                          [--pull POLICY] [--provide CAP=REPO]... [--env K=V]...
                          [--env-file PATH]...
otto docker compose down  [USE_CASE [SERVICE]...] [--on HOST] [--provide CAP=REPO]...
otto docker compose ps    [USE_CASE] [-a|--all] [--on HOST] [--provide CAP=REPO]...
otto docker compose logs  [USE_CASE [SERVICE]...] [--tail N] [--since WHEN]
                          [-t|--timestamps] [-f|--follow] [--on HOST]
                          [--provide CAP=REPO]...
```

## Container hosts

After `otto docker compose up`, the resulting containers appear in `--list-hosts`
under ids of the form `<parent>.<usecase>.<service>` (e.g.
`test3.integration.api`), where `<parent>` is the lab id of the docker-capable
host the stack runs on. Use them anywhere a host id is expected:

```text
otto host test3.integration.api login
otto host test3.integration.api exec "uname -a"
otto host test3.integration.api put ./local /remote/path
otto host test3.integration.api get /etc/os-release ./
```

Container ids are also synthesized at lab-load time **before** any
`otto docker compose up`, so tab completion works immediately. Accessing a
declared-but-stopped container auto-starts its compose stack on demand
(`build=False`, so access never triggers an image build). If the stack
can't be started — for example its image hasn't been built — the command
fails fast with a clear "run `otto docker compose up --build <project>` first" error.

See {doc}`../../architecture/subsystems/docker-hosts` for why a container
delegates to its parent host instead of being a parallel transport stack.

Configuration lives with the rest of the project's settings: the per-project
`[docker]` block in {doc}`../../configuration/settings`, and the per-lab
`docker_capable`/`roles` host fields in {doc}`../../configuration/lab-config`.

(docker-persistent-shell-state)=
## Persistent shell state

`run()` preserves shell state (`cd`, environment variables, shell
variables) across separate calls — same as `LocalHost` and `UnixHost`:

```python
await api.run(["cd /tmp", "pwd"])  # prints /tmp
await api.run("export FOO=bar")
await api.run("echo $FOO")  # prints bar
```

`exec()` is the stateless, concurrent-safe counterpart — each call
spawns a fresh `docker exec` against the parent. Use `exec()` when
you want to fan out independent commands; use `run()` when you need
stateful or interactive flows.

## Shared directories

A bind mount or named volume in the product's compose file gives one
directory two names: one inside the container, one on the parent running
the docker daemon. A `DockerContainerHost` knows that pairing and can
translate a path from either side to the other — the motivating case is a
test that knows a path *inside* the container and wants it fetched through
the parent, where otto already has a fully capable {class}`~otto.host.host.Host`.

These mounts are declared in the product's own compose file, under each
service's `volumes:` — there is no `mounts` setting in `settings.toml`. otto
reads what the compose file and the daemon agree on.

Use {attr}`~otto.host.docker_host.DockerContainerHost.mounts` and its three
helpers:

- `mount_for(container_path)` — returns the covering
  {class}`~otto.host.mount.Mount`, or `None` if the path isn't shared. Use
  this to ask.
- `parent_path(container_path)` — translates a container-side path to the
  parent. Raises {class}`~otto.host.errors.MountNotFoundError` if the path
  isn't under any known mount.
- `container_path(parent_path)` — the reverse translation, same exception
  on no match.

Each {class}`~otto.host.mount.Mount` also carries `kind` (`"bind"` or
`"volume"`) and `read_only`. Named volumes are included, with
`kind="volume"` and docker's own `/var/lib/docker/volumes/<name>/_data`
as their parent-side path — usually `root`-owned, so reaching a named
volume's contents through the parent generally needs a parent user with
root or `sudo`, where a bind source is owned by whoever created it. A
`tmpfs` mount is never included, since it has no parent-side path to
translate to. `read_only` describes only the **container's** view — the
parent side of a read-only mount stays fully writable, which is what makes
a mount usable for seeding fixture data into a container before it starts
reading, not just for collecting data out of one.

A worked, self-contained artifact-collection example: a test knows the
container writes its log to `/var/log/app/run.log` and wants that file
fetched locally. {func}`~otto.docker.compose.get_container_host` returns
the registered `DockerContainerHost` for a container-host id (the same id
`otto --list-hosts` prints), raising `KeyError` if no container host by that
id is registered.

```python
from pathlib import Path

from otto.docker.compose import get_container_host

ctr = get_container_host("test3.integration.api")
parent_log = ctr.parent_path("/var/log/app/run.log")  # -> PosixPath on test3
local_dir = Path("./artifacts")
result = await ctr.parent.get([parent_log], local_dir)
assert result.is_ok, result.msg
```

`parent_path`/`container_path` match by **longest prefix**: when one mount
is nested inside another (say `/var/lib/app` and `/var/lib/app/logs` both
declared), the more specific mount always wins (see
{func}`~otto.host.mount.mount_for`). Matching is also **component-wise**,
not a string prefix: a mount at `/var/lib/app` answers for
`/var/lib/app/logs/x.txt`, but never for `/var/lib/application/x` — a
sibling directory that merely shares a longer name is never captured.

Going through the mount is worth it when the parent can do something the
container cannot: fetching a directory tree in one transfer, reading a file
an image without a shell or `tar` cannot hand to `docker cp`, or seeding
data before the service starts reading it. For a single file out of a
running container, `ctr.get(...)` is simpler and needs no mount at all —
`parent_path` exists for the cases where you want otto's full `Host` API
pointed at the same bytes.

### The table is per-process

The mount table is read from the docker daemon (`docker inspect`) when the
stack comes up, whichever way it came up — `otto docker compose up`, or a use-case
deploy — populates it identically. It is **in-memory state belonging to the
otto process that ran that bring-up**, and nothing writes it to `lab.json`
or anywhere else on disk. A *later* otto invocation re-registers each
declared container host as a placeholder with an empty `mounts`, even while
the stack is up and healthy — so bringing the stack up from the CLI and
then translating a path from a separate `pytest` process does not work.
Touching the container first is not a dependable workaround either.
`exec`/`run`/`put`/`get` on a placeholder resolve its container id, and if
the stack is *down* they auto-start it — that bring-up does re-register the
stack's hosts with fresh mount tables, so a later `get_container_host(...)`
in the same process returns a populated one. But when the stack is already
up the id resolves without any bring-up and no mount table is ever read;
and the placeholder object you are already holding keeps its empty `mounts`
either way.

To translate paths, the process doing the translating must be the one that
brought the stack up — a test that deploys through `compose_up` or a
use-case deploy and then translates is the supported shape, and is what the
example above assumes. On a placeholder, `mount_for` returns `None` and the
translators raise a `MountNotFoundError` naming both causes (the stack is
down, or it came up under a different invocation) rather than reading like
a genuinely mount-less container.

### The relative bind-source trap

Compose lets a service declare a bind source relative to the compose
file's own directory, e.g. `./data:/var/lib/app`. otto's staging
relocates the rendered compose file into a directory it deletes and
recreates on every deploy — so a relative source like this points at a
directory whose contents are wiped on the *next* `otto docker compose up`, with
nothing in the product's compose file hinting at it. When `otto docker compose up`
stages such a file, it warns, naming the resolved staging path and noting
that it is wiped per stage. The fix is either an absolute path on the
parent or a named volume. The warning does not fire under `--dry-run`,
since nothing is actually staged then.

The one gap in the warning: a source containing `$`, e.g.
`${DATA_DIR}/data:/var/lib/app`, draws **no warning**, relative or not.
Compose only interpolates such a value at `up` time from the env file, so
otto's static walk over the rendered compose text cannot evaluate it and says
nothing. If a variable-driven source resolves to something relative, it is
wiped exactly like a literal `./data` source — otto just can't tell you so in
advance.

### Docker's auto-create behavior

A missing **absolute** bind source is not an error: docker creates it on
the parent, owned `root:root` and empty. This is normal docker behavior,
not something otto second-guesses — so if a service comes up healthy but
finds an unexpectedly empty directory where it expected pre-seeded data,
the usual cause is that the directory does not exist yet on the parent,
not that the image is broken.

The permissions variant of the same trap is worth naming separately: if
the image runs as a non-root `USER`, that user has no write access to a
`root`-owned auto-created directory, and the resulting failure is a plain
permission-denied error at write time — it points nowhere near the mount
that caused it. otto does not warn about a missing absolute bind source.

## Where docker runs

- Otto users typically don't have local root. Builds and compose runs
  happen on a **remote** docker-capable host that *can* run as root
  (or have its user in the `docker` group).
- All docker invocations are routed through the parent host's existing
  SSH connection (`parent.run("docker ...")`) — no local docker daemon
  is required.

## Limits

- Builds run on the parent only; there is no local-build path.
- Cross-host networking between containers on different parents is not
  managed.
- `run()`, `open_session()`, `send()`, and `expect()` require an
  SSH-based `UnixHost` parent — they open a persistent
  `docker exec -it` channel multiplexed on the parent's SSH
  connection. Telnet parents and `LocalHost` parents are rejected with
  `NotImplementedError`. `exec()` (and `get` / `put`) still work
  through any parent.
- The container must provide `/bin/sh`. Distroless or minimal images
  without a shell will fail at session-open time.
- `login()` requires `parent.term == 'ssh'`. Telnet parents are
  rejected.

```{toctree}
:caption: Subcommands
:hidden:

build
ps
images
logs
```

```{toctree}
:caption: Compose
:hidden:

compose/build
compose/up
compose/down
compose/ps
compose/logs
```

```{toctree}
:caption: Topics
:hidden:

use-cases
```
