# Design: Docker container hosts

How {class}`~otto.host.docker_host.DockerContainerHost` fits the host
subsystem ({doc}`hosts`): why containers delegate to a parent
{class}`~otto.host.unix_host.UnixHost` instead of being a parallel transport
stack, and the consequences of that choice. The user-facing workflow lives in
{doc}`../../guide/cli/docker/index`.

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
`command=` kwarg). `get`/`put` are two-step: `parent.put` to a per-
container staging dir, then `parent.run("docker cp ...")`.

## Why not docker -H ssh://

Tempting, but `docker -H ssh://user@host` invokes the system `ssh`
client and is unaware of otto's hop chain (which is asyncssh-internal).
For hopped parents it would silently fail. Worse, it would require the
user to manage local SSH keys for the parent host — directly
contradicting otto's "users don't have root locally" constraint. Going
through `parent.run("docker ...")` is one code path that handles both
hopped and direct parents identically.

## Use-case resolution

A deployment is a **use-case**, not a repo: every active repo contributes
*fragments* under the same use-case name, and the deploy pipeline resolves
them before touching anything. Three pure phases, in order — selection (which
fragments take part, after the provider competition), placement (which lab
host each one lands on), and env assembly (the three channels merged into one
mapping). Purity is the design constraint, not an accident: it is what lets
`otto docker use-cases` render an inventory without contacting a device, and
what lets `--dry-run` print the *exact* compose command rather than a
description of one.

Resolution refuses rather than guesses. An ambiguous role, a provider tie, a
pin naming a host this lab does not have — each is a configuration error
naming the candidates and the knobs, raised before a single file is staged.
{doc}`../../guide/cli/docker/use-cases` documents the rules for users; the
design rationale is in the docker use-cases design spec
(`docs/superpowers/specs/2026-08-30-docker-use-cases-design.md`, §4-§6).

## Naming scheme

Container host id = `<parent_id>.<usecase>.<service>`, lowercased.

- **Parent id** is whatever `UnixHost._generateId()` produces (e.g.
  `test3`, or `test3_rack1` if the lab encodes `board`/`slot`).
- **Use-case** is the `[[docker.use_cases]]` `name` — the deployment, not
  the repo, because a use-case is cross-repo by construction. A repo whose
  fragment is named after the repo keeps its pre-use-case container ids
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
per-repo path — the `compose_up`/`composed` primitives, which stay public
(spec §11) and which instructions still call — deploys under
`otto-<repo>-<suffix>`, prefix included. That naming is frozen until the path
is removed; it is not evidence the rule above is broken.

Do **not** read that prefix as "this repo declares no use-cases". A repo with
no `[[docker.use_cases]]` cannot reach the per-repo path without an explicit
`on=`: `_resolve_parent` has nothing to resolve and raises. In practice an
`otto-<repo>-<suffix>` project belongs to a repo that *does* declare
fragments and was reached through a primitive rather than through
`deploy` — so the fragment a maintainer would go hunting for is already
there.

## Lifecycle and the lab

On every otto invocation, `cli/main.py` calls
`register_declared_container_hosts(lab, repos)` after loading the
lab. This walks each repo's `[docker]` settings and registers
**placeholder** `DockerContainerHost` instances in `lab.hosts` with
`container_id = ""`. Two effects:

1. `--list-hosts` and tab completion immediately show the declared
   container ids — without needing to bring the stack up first. These are
   two synthesizers, not one: `--list-hosts` reads the placeholders this
   walk registered, while completion runs off the cached id list
   `config/completion_cache.collect_host_ids` builds without a lab. They must
   mint the same id SHAPE (use-case fragments → `<parent>.<usecase>.<service>`,
   a composes-only repo → `<parent>.<repo>.<service>`) or completion offers
   ids nothing registers; both take the same branch, and the divergence is
   pinned by `tests/unit/config/test_completion_container_ids.py`.
2. Operations against a not-yet-up container produce a clear "run
   `otto docker up` first" error rather than a confusing "no such
   host."

When `compose_up()` runs (from CLI or directly from an instruction),
it overwrites the placeholder with a real entry whose `container_id`
is resolved from
`docker compose -p <proj> ps -q <service>`. `compose_down()` closes
each container host before removing its entry — sessions must drain
while the parent's connection is still alive, so container hosts
close before their parent, the same teardown discipline described in
{doc}`../principles` (the session-level mechanics are below, under
"Persistent shell sessions").

This avoids writing back to `lab.json` at runtime — that file stays
read-only — while still keeping `--list-hosts` and tab completion
populated immediately.

## Build skipping

Each image is tagged `<project>-<image>:<context_hash[:16]>`. The
hash covers Dockerfile bytes, every context file (after
`.dockerignore`), build args, and target stage. `docker image inspect`
on the tag short-circuits; `--rebuild` forces. The hash is computed on
the otto host and looked up on whichever parent will build, so caches
are correct even when bringing the same image up on a different parent
later.

## Reservation tags

A new `DockerContainerHost` copies its parent's `lab_info` — the lab it was
registered into, with that lab's declared `resources` — so concurrent test
runs that both want `test3.repo1.api` serialize
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
`SshSession` that overrides only `_open` to splice
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
- Image push to a registry (only local tagging on the parent).
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
