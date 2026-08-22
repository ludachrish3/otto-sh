# otto docker

`otto docker` builds container images and orchestrates compose stacks on the
lab's docker-capable hosts. The containers it brings up become first-class lab
hosts: they appear in `--list-hosts` and accept every `otto host` verb.

```{raw} html
:file: ../../../_static/generated/termynal/help-docker.html
```

## Synopsis

```text
otto docker build [--repo NAME] [--on HOST] [--rebuild] [<IMAGE>...]
otto docker up    [--repo NAME] [--on HOST] [--no-build]
otto docker down  [--repo NAME] [--on HOST]
otto docker ps    [--on HOST]
```

## Options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `--repo NAME` | `build`, `up`, `down` | Restrict to a single repo by name |
| `--on HOST` | all | Lab host id to operate on (default: all docker-capable hosts) |
| `--rebuild` | `build` | Force rebuild even if a context-hash tag exists |
| `--no-build` | `up` | Skip the implicit build step before compose up |
| `<IMAGE>...` (argument) | `build` | Image names to build (default: all) |

## Container hosts

After `otto docker up`, the resulting containers appear in `--list-hosts`
under ids of the form `<parent>.<project>.<service>` (e.g.
`pepper_seed.repo1.api`). Use them anywhere a host id is expected:

```text
otto host pepper_seed.repo1.api login
otto host pepper_seed.repo1.api run "uname -a"
otto host pepper_seed.repo1.api put ./local /remote/path
otto host pepper_seed.repo1.api get /etc/os-release ./
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
declaration in {doc}`../../configuration/lab-config`.

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
up
down
ps
```

```{toctree}
:caption: Topics
:hidden:

rebuild-policy
```
