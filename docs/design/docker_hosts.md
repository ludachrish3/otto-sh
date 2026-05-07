---
orphan: true
---

# Design: Docker container hosts

## Why parent-delegation

Otto already has a mature host abstraction: `BaseHost` ABC, `Host`
Protocol, `RemoteHost` (SSH/telnet), `LocalHost`. Adding Docker
containers as a new top-level host class — with their own connection
manager, transport, file-transfer module, and hop chain — would
duplicate all of that and force `ConnectionManager`/`FileTransfer` to
grow a `if term == 'docker'` branch in five places (no real TCP, no
port to forward, no FTP, no NetCat). Container "transport" is just
`docker exec` against a daemon, plus `docker cp` for files.

Instead, `DockerContainerHost` holds a reference to a *parent*
`RemoteHost` (SSH-based) and implements every method by delegating to
that parent with a `docker exec` / `docker cp` wrapper. The parent
owns:

- Authentication
- The SSH connection (and asyncssh's channel multiplexing means many
  containers on the same parent share one TCP connection for free)
- The hop chain — so a container behind a multi-hop SSH path "just
  works" with no special-casing in the docker code path

`oneshot(cmd)` on a container becomes
`parent.oneshot(f"docker exec {ctr} sh -c {shlex.quote(cmd)}")`.
`interact()` opens a PTY-backed `docker exec -it ctr /bin/sh` over the
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

## Naming scheme

Container host id = `<parent_id>.<project>.<service>`, lowercased.

- **Parent id** is whatever `RemoteHost._generateId()` produces (e.g.
  `pepper_seed`, `pepper_seed_1` if the lab encodes board+slot).
- **Project** is `Repo.name` (the per-repo `name` in
  `.otto/settings.toml`).
- **Service** is the compose service name.

The verbose form prevents collisions when multiple projects on the
same parent declare a service of the same name (e.g. both repos have
an `api`). Tab-completion already does prefix matching, so typing
`pepper_seed.` narrows naturally to the containers on a given parent.

## Lifecycle and the lab

On every otto invocation, `cli/main.py` calls
`register_declared_container_hosts(lab, repos)` after loading the
lab. This walks each repo's `[docker]` settings and registers
**placeholder** `DockerContainerHost` instances in `lab.hosts` with
`container_id = ""`. Two effects:

1. `--list-hosts` and tab completion immediately show the declared
   container ids — without needing to bring the stack up first.
2. Operations against a not-yet-up container produce a clear "run
   `otto docker up` first" error rather than a confusing "no such
   host."

When `compose_up()` runs (from CLI or directly from an instruction),
it overwrites the placeholder with a real entry whose `container_id`
is resolved from
`docker compose -p <proj> ps -q <service>`. `compose_down()` removes
the entry again.

This avoids writing back to `hosts.json` at runtime — that file stays
read-only — while still giving the tab-completion / listing UX the
TODO asked for.

## Build skipping

Each image is tagged `<project>-<image>:<context_hash[:16]>`. The
hash covers Dockerfile bytes, every context file (after
`.dockerignore`), build args, and target stage. `docker image inspect`
on the tag short-circuits; `--rebuild` forces. The hash is computed on
the otto host and looked up on whichever parent will build, so caches
are correct even when bringing the same image up on a different parent
later.

## Reservation tags

A new `DockerContainerHost` copies its parent's `resources` set so
concurrent test runs that both want `pepper_seed.repo1.api` serialize
through the existing reservation backend. There's no separate
container-reservation concept — the parent's reservation transitively
covers its containers.

## Persistent shell sessions

`run()`, `open_session()`, `send()`, and `expect()` use a persistent
`docker exec -it <ctr> sh` session multiplexed on the parent's
existing asyncssh connection (`SSHClientConnection.create_process()`).
This is the one place the docker host reaches past parent-delegation
purity into the parent's transport — the same shape that `_interact`
already takes when it grabs `parent._connections.ssh()` for a
PTY-backed login. The trade-off is small: in exchange for one extra
SSH channel per active container, `run()` becomes consistent with
`LocalHost` and `RemoteHost` (state persists across calls; no `&&`
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

`oneshot()` stays stateless — it keeps the original
`parent.oneshot("docker exec ...")` path and remains concurrent-safe.

The container id is resolved lazily at session-open time so that
hosts pre-registered as placeholders (with `container_id=""`) work
correctly once `compose_up` populates the id.

Lifecycle: `compose_down` now `await`s `host.close()` on each
container host before popping it from `lab.hosts`, ensuring the
session's docker exec channel drains while the parent's SSH
connection is still alive. Container hosts must close before their
parents.

## Out of scope

- Local docker builds: builds always go to the parent.
- Cross-host networking between containers on different parents.
- Image push to a registry (only local tagging on the parent).
- Non-SSH parents for `run` / `open_session` / `send` / `expect` /
  `interact` — rejected with a clear `NotImplementedError`. Local
  docker is expected to be managed via Kubernetes rather than as a
  first-class otto host. `oneshot()` (and `get` / `put`) still work
  against any parent.
