# Design: Docker container hosts

How {class}`~otto.host.docker_host.DockerContainerHost` fits the host
subsystem ({doc}`hosts`): why containers delegate to a parent
{class}`~otto.host.unix_host.UnixHost` instead of being a parallel transport
stack, and the consequences of that choice. The user-facing workflow lives in
{doc}`../../cli/docker/index`.

## Why parent-delegation

Otto already has a mature host abstraction: `BaseHost` ABC, `Host`
Protocol, `UnixHost` (SSH/telnet), `LocalHost`. Adding Docker
containers as a new top-level host class — with their own connection
manager, transport, file-transfer module, and hop chain — would
duplicate all of that and force `ConnectionManager`/`FileTransfer` to
grow a `if term == 'docker'` branch in five places (no real TCP, no
port to forward, no FTP, no NetCat). Container "transport" is just
`docker exec` against a daemon, plus `docker cp` for files.

Instead, `DockerContainerHost` holds a reference to a *parent*
`UnixHost` (SSH-based) and implements every method by delegating to
that parent with a `docker exec` / `docker cp` wrapper. The parent
owns:

- Authentication
- The SSH connection (and asyncssh's channel multiplexing means many
  containers on the same parent share one TCP connection for free)
- The hop chain — so a container behind a multi-hop SSH path "just
  works" with no special-casing in the docker code path

`exec(cmd)` on a container becomes
`parent.exec(f"docker exec {ctr} sh -c {shlex.quote(cmd)}")`.
`login()` opens a PTY-backed `docker exec -it ctr /bin/sh` over the
parent's SSH conn (we extended `run_ssh_login()` with an optional
`command=` kwarg). `get`/`put` are two-step: `parent.put` to a per-call
staging dir under the parent's private staging root
({ref}`docker-staging`), then `parent.exec("docker cp ...")`.

## Why not docker -H ssh://

Tempting, but `docker -H ssh://user@host` invokes the system `ssh`
client and is unaware of otto's hop chain (which is asyncssh-internal).
For hopped parents it would silently fail. Worse, it would require the
user to manage local SSH keys for the parent host — directly
contradicting otto's "users don't have root locally" constraint. Going
through `parent.run("docker ...")` is one code path that handles both
hopped and direct parents identically.

## Use-case resolution

A deployment is a **use-case**, not a project: every active project contributes
*fragments* under the same use-case name, and the deploy pipeline resolves
them before touching anything. Three pure phases, in order — selection (which
fragments take part, after the provider competition), the parent (the one lab
host the stack lands on), and env assembly (the three channels merged into one
mapping). Purity is the design constraint, not an accident: it is what lets
`otto docker use-cases` render an inventory without contacting a device, and
what lets `--dry-run` print the *exact* compose command rather than a
description of one.

The parent has one owner, `observe.default_docker_parent`, and the rule is
stated once, for users, under "Which host" on {doc}`../../cli/docker/index`.
Two properties of it matter here. It reads the lab and nothing else, so no
fragment, project scope or file order can move it. And every verb calls it (the
CLI never computes a default), so `deployment.resolve_use_case`, shared by
`compose build`, `compose up`, `compose down`, `compose ps` and `compose logs`,
sees the same host the container-id placeholders below were minted under.

Resolution refuses rather than guesses, before a single file is staged, and
names the candidates and the knobs. A provider tie is a configuration refusal
(exit 1); a tie for the parent, or a `--parent` naming a host this lab does not
have, is a parent refusal (exit 2).
The "Errors" section of {doc}`../../cli/docker/use-cases` documents the split
for users; the
design rationale is in the docker use-cases design spec
(`docs/superpowers/specs/2026-08-30-docker-use-cases-design.md`, §4-§6).

Two fragments setting the same env key is the one ambiguity that is resolved
(the later fragment wins) rather than reported: a merged stack's fragments
routinely share innocuous keys, and refusing on every one of them would make
cross-project use-cases unusable.

## The compose boundary

otto never templates a product's compose file; it only assembles the
environment the compose run sees, and fact references (`${otto:...}`) resolve
entirely on otto's side, inside `settings.toml`. The test of that boundary is
the decoupling test: running the product's compose file by hand, with the
values supplied by the user, must behave identically to otto's deployment of
the same stack. If a change to otto ever broke that, otto would be the thing
that is wrong. Keeping fact references on otto's side is what keeps the
product deployable with no otto installed at all.

The resolved mapping reaches compose as its process environment, through a
staged env launcher every compose command that loads the deployment's files
runs under (`sh <staging>/otto-env.sh docker compose …`): `up`, the whole
`down` and the rollback. The commands that go by the project label alone,
otto's own `docker` probes and container-host `docker exec` do not;
{doc}`../../cli/docker/use-cases` lists them. Compose consumes env at three
moments: parse-time `${VAR}` interpolation, `environment:` pass-through, and
its own `COMPOSE_*`/`DOCKER_*` settings. Some versions read some of those
settings only from the process. The process environment serves all three
alike, which is the decoupling test again: it is what exporting the values
and running compose by hand gives. The docker parent is almost always a
remote host reached over a non-interactive session, so asking the user to
export anything there has no reliable home. The launcher carries the values
with the deployment instead, and `up`, `down` and the rollback run under the
same one.

## Naming scheme

Container host id = `<parent_id>.<usecase>.<service>`, lowercased.

- **Parent id** is whatever `UnixHost._generateId()` produces (e.g.
  `test3`, or `test3-rack-1` if the lab encodes `board`/`slot`).
- **Use-case** is the `[[docker.use_cases]]` `name` — the deployment, not
  the project, because a use-case is cross-project by construction. A project whose
  fragment is named after the project keeps its pre-use-case container ids
  literally unchanged.
- **Service** is the compose service name.

The verbose form prevents collisions when multiple deployments on the
same parent declare a service of the same name (e.g. two use-cases each have
an `api`). Tab-completion already does prefix matching, so typing
`test3.` narrows naturally to the containers on a given parent.

The **compose project** underneath is named separately, and differently:
`<lab>-<usecase>-<suffix>` (suffix = the invoking username, or
`OTTO_COMPOSE_SUFFIX`). This page is the home for why each segment is shaped
that way:

- The **lab** segment is load-bearing, not cosmetic. `--remove-orphans` reaps
  within a project, and one docker host can serve containers for several labs,
  so two labs sharing a project would have each `up` deleting the other's
  containers. It also makes a plain `docker ps` on a shared host attributable
  to a lab from outside otto.
- The **suffix** keeps concurrent users' stacks isolated on a shared host.
- There is **no `otto-` prefix**, deliberately. The deployment belongs to the
  product; branding it with the tool that enabled it is the backwards
  dependency the use-case design exists to refuse.

One exception a maintainer will meet in a live `docker ps`: the legacy
per-project path — the `compose_up`/`composed` primitives, which stay public
(spec §11) and which instructions still call — deploys under
`otto-<project>-<suffix>`, prefix included. That naming is frozen until the path
is removed; it is not evidence the rule above is broken.

Do **not** read that prefix as telling you whether the project declares
use-cases: it says nothing about that. The per-project path takes its parent from
the same rule a use-case does (`compose._project_parent_host`, with `parent=` as
the override).

## Lifecycle and the lab

On every otto invocation, `cli/main.py` calls
`register_declared_container_hosts(lab, projects)` after loading the
lab. This walks each project's `[docker]` settings and registers
**placeholder** `DockerContainerHost` instances in `lab.hosts` with
`container_id = ""`. Two effects:

1. `--list-hosts` and tab completion immediately show the declared
   container ids, under the lab's default parent — without needing to bring
   the stack up first. A lab whose rule refuses (a tie at the top) registers
   none, and the walk stays silent; `register_stack_hosts` registers under the
   parent a stack actually came up on. These are
   two synthesizers, not one: `--list-hosts` reads the placeholders this
   walk registered, while completion runs off the cached id list
   `config/completion_cache.collect_host_ids` builds without a lab. They must
   mint the same id SHAPE (use-case fragments → `<parent>.<usecase>.<service>`,
   a composes-only project → `<parent>.<project>.<service>`) or completion offers
   ids nothing registers; both take the same branch, and the divergence is
   pinned by `tests/unit/config/test_completion_container_ids.py`.
2. Operations against a not-yet-up container fail fast, rather than with a
   confusing "no such host"; see
   {ref}`Starting containers before you use them <docker-start-first>`.
   A `docker ps` probe that fails on the parent raises the same
   `ContainerNotRunningError`, carrying Docker's own text. A parent that
   cannot complete the probe raises `HostUnreachableError` instead.

When a stack starts (the use-case deploy behind `otto docker compose up`, or
`compose_up()` from an instruction or a test), each service's placeholder is
overwritten with a real entry whose `container_id` is resolved by the label
probe {func}`~otto.host.docker_host.compose_container_probe` (`docker ps -q --filter label=…`),
polled through the shared deadline poll for a few seconds in case the daemon has
not listed the container yet. `compose_down()` closes
each container host before removing its entry — sessions must drain
while the parent's connection is still alive, so container hosts
close before their parent, the same teardown discipline described in
{doc}`../principles` (the session-level mechanics are below, under
"Persistent shell sessions").

This avoids writing back to `lab.json` at runtime — that file stays
read-only — while still keeping `--list-hosts` and tab completion
populated immediately.

### One compose implementation

Every `docker compose` command otto runs comes from one builder,
{func}`~otto.docker.compose.compose_command`. That covers the use-case `up` and
teardown, the per-project `compose_up`/`compose_down`, `compose ps` and
`compose logs`, the use-case deploy's dry-run preview and the commands quoted
in error text. Quoting, the `-f` order and the `sh <launcher>` in front live
there once, so a preview and the live run cannot spell the command
differently. The ast-grep rule
`compose-command-through-builder` flags a command spelled by hand anywhere else.

The use-case deploy and the per-project `compose_up` start a stack through the
same {func}`~otto.docker.compose.up_stack`. A path that re-implemented `up` would silently lose
the hardening this holds. `up -d` is convergent, so docker's libnetwork race
(the `<proj>_default` network is created, then reported "not found" when a
container attaches) is re-run once after a short pause: the second run starts
the already-created container. The re-run is gated on that one signature, so a
genuine failure (a bad file, a denied pull, a port clash) is reported at once,
and a race that recurs points at a degraded daemon rather than a race.

When a start fails once it has run `up` (the `up` itself included), both paths
roll back through the same {func}`~otto.docker.compose.roll_back`, under one
rule: a rollback undoes exactly what its call did. A call owns the stack unless
the probe said it was already up (an unknown answer counts as owned, so a stack
nobody claims is never stranded). An owned stack is taken down with its own
`up` command minus the verb: the files it was started from, naming the services
that `up` named (docker's `down [SERVICES]`), so containers under the same
project label that it did not start stay up. The `down` is a compensating action
({func}`~otto.lifecycle.compensate`): an interrupt that lands while it runs
waits for it, up to the run's teardown deadline. A down that fails, raised or
returned, is logged as "could not be rolled back … whatever its up created
there is still up"; under an
interrupt, compensate's own warning names it instead. Nothing is unregistered:
on a start's one parent, registering the stack's container hosts is the last
step and is all or nothing, so a failed start registered nothing, and a
declared placeholder or a peer's id is never the rollback's to remove.

## Mounts come from compose, not settings

otto derives mounts from the product's own compose file, the only place
they are declared; there is no `mounts` setting in `settings.toml`: a second
copy of that truth would drift the first time a compose adapter rewrote a
path, so otto only reads what the compose file and the daemon already agree
on.
`DockerContainerHost.mounts` is populated by one batched `docker inspect`
call in `register_stack_hosts`, right after the same call resolves each
service's container id. Reading it at the point ids are already being
resolved falls out of a registration path that was already making one docker
round trip per service.

The table's lifetime follows from that placement: it lives in the process
that ran the bring-up, and — like the resolved container id above — is never
written back to `lab.json`. Unlike the id, it has no lazy re-resolution
path, so a placeholder registered by a later invocation keeps an empty
table (guide: "The table is per-process"). Giving it one would mean an
`await`-ing translator or a blocking inspect inside a property; both were
rejected in design in favour of the honest refusal.

Translation matches by longest prefix, component-wise, because a first-match
translation through an outer mount would name a parent path where a file in a
nested mount does not exist. otto warns about a *relative* bind source (it
points into a staging directory that is wiped per deploy) but deliberately
stays silent about a missing *absolute* one: docker creating that directory on
demand is documented behaviour products already rely on, and a warning would
fire on every legitimate first run of a new stack.

How to use the table is in {doc}`../../cli/docker/index`'s "Shared
directories" section.

## Builds

Every build runs `docker build` on the parent; otto keeps no record of
earlier builds and skips nothing, so docker's layer cache is the only cache.
It runs with `BUILDX_NO_DEFAULT_ATTESTATIONS=1`, so buildx attaches no
provenance attestation and a fully cached rebuild keeps its image ID, which
lets a later `up` leave the image's containers running (the legacy builder
ignores the variable).
The image is named exactly as declared (`<name>:latest`, or the tags typed
with `--tag`), and the build is read back from the parent's daemon, never
composed by otto: the references and id in the report are what
`docker images` lists there. {doc}`../../cli/docker/build` is the user-facing
home for the flags and the report.

A directory context is staged on the parent before the build. An archive
context is uploaded unchanged and given to `docker build` on stdin, so otto
never opens it.

## Reservation tags

A new `DockerContainerHost` copies its parent's `lab_info` — the lab it was
registered into, with that lab's declared `resources` — so concurrent test
runs that both want `test3.project1.api` serialize
through the existing reservation backend. There's no separate
container-reservation concept — the parent's reservation transitively
covers its containers. That's also why the `otto docker` command itself
carries no independent reservation gate (`gate=False` on its `CommandSpec`,
{doc}`../lifecycle`): gating the parent host is the whole story.

## Persistent shell sessions

`run()`, `open_session()`, `send()`, and `expect()` use a persistent
`docker exec -it <ctr> sh` session multiplexed on the parent's
existing asyncssh connection (`SSHClientConnection.create_process()`).
This is the one place the docker host reaches past parent-delegation
purity into the parent's transport — the same shape that `_interact`
already takes when it grabs `parent._connections.ssh()` for a
PTY-backed login. The trade-off is small: in exchange for one extra
SSH channel per active container, `run()` becomes consistent with
`LocalHost` and `UnixHost` (state persists across calls; no `&&`
chains required) and the session protocol's expect/send primitives
become available inside containers.

The session is implemented as `_DockerSshSession`, a thin subclass of
`SshSession` that overrides only `open_transport` to splice
`docker exec -it <cid> sh` in front of the channel's default shell.
The sentinel-wrapped command execution, expect handling, line-by-line
output streaming, and `\x03`-based timeout recovery all come from the
base class. `-it` is required for SIGINT semantics: without a TTY,
`\x03` is just data on stdin and timeout recovery wouldn't actually
interrupt the foreground process. asyncssh's `term_type='dumb'`
allocates the PTY for free.

`exec()` stays stateless — it keeps the original
`parent.exec("docker exec ...")` path and remains concurrent-safe.

The container id is resolved lazily at session-open time so that
hosts pre-registered as placeholders (with `container_id=""`) work
correctly once `compose_up` populates the id.

This is what makes the close-before-parent ordering described above
(under "Lifecycle and the lab") concrete: `compose_down` `await`s
`host.close()` on each container host before popping it from
`lab.hosts`, so this very session's docker exec channel drains while the
parent's SSH connection is still alive.

## Out of scope

- Local docker builds: builds always go to the parent.
- Cross-host networking between containers on different parents.
- Image push to a registry (an image is only built and tagged on the parent).
- Non-SSH parents for `run` / `open_session` / `send` / `expect` /
  `login` — rejected with a clear `NotImplementedError`. Local
  docker is expected to be managed via Kubernetes rather than as a
  first-class otto host. `exec()` (and `get` / `put`) still work
  against any parent.

## Where the code lives

- {mod}`otto.host.docker_host` — `DockerContainerHost`, `_DockerSshSession`,
  and parent delegation
- {mod}`otto.docker` — `build_images`, `compose_up`/`compose_down`, and
  placeholder registration
