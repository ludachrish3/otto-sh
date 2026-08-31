# otto docker

`otto docker` builds container images and deploys **use-case** stacks on the
lab's docker-capable hosts. The containers it brings up become first-class lab
hosts: they appear in `--list-hosts` and accept every `otto host` verb.

A use-case is one named deployment that several active repos contribute
fragments to; `up`, `down` and `build` all speak it. {doc}`use-cases` is the
workflow home for that model — start there.

```{raw} html
:file: ../../../_static/generated/termynal/help-docker.html
```

## Synopsis

```text
otto docker use-cases [USE_CASE]
otto docker up        [USE_CASE [SERVICE]...] [--on HOST] [--no-build]
                      [--provide CAP=REPO]... [--env K=V]... [--env-file PATH]...
otto docker down      [USE_CASE [SERVICE]...] [--on HOST] [--provide CAP=REPO]...
otto docker build     [USE_CASE [IMAGE]...] [--repo NAME] [--on HOST] [--rebuild]
                      [--provide CAP=REPO]...
otto docker ps        [--on HOST]
```

## Options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `USE_CASE` (argument) | `use-cases` | Show only this use-case (default: every declared one) |
| `USE_CASE` (argument) | `up`, `down` | The use-case to deploy or tear down (default: the only one declared) |
| `USE_CASE` (argument) | `build` | Build only the repos taking part in this use-case (default: every selected repo) |
| `SERVICE...` (argument) | `up`, `down` | Narrow to these services; requires an explicit `USE_CASE` |
| `IMAGE...` (argument) | `build` | Image names to build (default: all declared) |
| `--on HOST` | `up`, `down` | Collapse every fragment of the deployment onto this lab host |
| `--on HOST` | `build` | Lab host id to build on |
| `--on HOST` | `ps` | Lab host id to query (default: all docker-capable hosts) |
| `--provide CAP=REPO` | `up`, `down`, `build` | Break a provider tie for capability `CAP`. Repeatable |
| `--env K=V` | `up` | Extra env var; wins over every channel. Repeatable |
| `--env-file PATH` | `up` | Local `KEY=VALUE` file merged under `--env`. Repeatable |
| `--no-build` | `up` | Skip the implicit build step before `compose up` |
| `--repo NAME` | `build` | Restrict to a single repo by name |
| `--rebuild` | `build` | Force rebuild even if a context-hash tag exists |

`--repo` is a `build`-only option: `up` and `down` deploy a merged, cross-repo
use-case, which is not a per-repo thing to narrow.

## Container hosts

After `otto docker up`, the resulting containers appear in `--list-hosts`
under ids of the form `<parent>.<usecase>.<service>` (e.g.
`test3.integration.api`). Use them anywhere a host id is expected:

```text
otto host test3.integration.api login
otto host test3.integration.api run "uname -a"
otto host test3.integration.api put ./local /remote/path
otto host test3.integration.api get /etc/os-release ./
```

Container ids are also synthesized at lab-load time **before** any
`otto docker up`, so tab completion works immediately. Accessing a
declared-but-stopped container auto-starts its compose stack on demand
(`build=False`, so access never triggers an image rebuild). If the stack
can't be started — for example its image hasn't been built — the command
fails fast with a clear "run `otto docker up` first" error.

See {doc}`../../../architecture/subsystems/docker-hosts` for why a container
delegates to its parent host instead of being a parallel transport stack.

Configuration lives with the rest of the project's settings: the per-project
`[docker]` block in {doc}`../../configuration/settings`, and the per-lab
`docker_capable`/`roles` host fields in {doc}`../../configuration/lab-config`.

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

up
down
build
ps
```

```{toctree}
:caption: Topics
:hidden:

use-cases
rebuild-policy
```
