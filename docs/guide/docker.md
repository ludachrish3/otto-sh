# Docker Containers

Otto can manage Docker containers as first-class hosts. Once a project's
compose stack is up, every container appears in `--list-hosts` and is
addressable by `otto host <id>` for `login`, `run`, `get`, and `put` —
exactly like any other host. Hops are inherited from the parent host so a
container behind a multi-hop SSH chain works without extra wiring.

## Constraints

- Otto users typically don't have local root. Builds and compose runs
  happen on a **remote** docker-capable host that *can* run as root
  (or have its user in the `docker` group).
- All docker invocations are routed through the parent host's existing
  SSH connection (`parent.run("docker ...")`) — no local docker daemon
  is required.

## Configuration

### Per-project (`<repo>/.otto/settings.toml`)

```toml
[docker]
registry_url = "docker.io"   # optional; default. Non-default registries
                              # get prefixed onto image tags.

[[docker.images]]
name = "api"                              # short logical name
dockerfile = "${sutDir}/docker/api.Dockerfile"
context = "${sutDir}/docker"

[[docker.images]]
name = "db"
dockerfile = "${sutDir}/docker/db.Dockerfile"
context = "${sutDir}/docker"
build_args = { VERSION = "1.2.3" }       # optional; influences hash
target = "prod"                          # optional multi-stage target

[[docker.composes]]
path = "${sutDir}/docker/compose.yml"
default_host = "pepper_seed"             # lab host id; CLI --on overrides
services = ["api", "db"]                 # used for tab-completion only
```

### Per-lab (`hosts.json`)

Mark hosts that can host containers:

```text
{ "ne": "pepper", "board": "seed", "ip": "...", "creds": {...},
  "docker_capable": true,
  "labs": ["veggies"] }
```

## CLI

```text
otto docker build [--rebuild] [--on <host>] [<image>...]   # build images
otto docker up    [--on <host>]                            # compose up -d
otto docker down  [--on <host>]                            # compose down
otto docker ps    [--on <host>]                            # docker ps
```

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
`otto docker up` so tab completion works immediately. Operations against
a not-yet-running container produce a clear "run `otto docker up` first"
error.

## Library API (instructions and suites)

The CLI is a thin wrapper around `otto.docker`. Project instructions and
suites import the same library directly:

```python
from otto.docker import build_images, compose_up, compose_down, composed

@instruction()
async def smoke():
    async with composed(repo, lab, own=True) as containers:
        api = containers["api"]
        await api.run(["./run-tests"])
```

`composed()` is the recommended scope — it tears the stack down on exit
unless it found the stack already running, in which case nested users
share without yanking the stack from peers.

## Image rebuild policy

Each image is tagged with a hash of:

- Dockerfile bytes
- Every file in the build context (after `.dockerignore`)
- Build args
- Multi-stage target (if any)

`docker image inspect <tag>:<hash>` is consulted before every build. A
match short-circuits the build; `--rebuild` forces it.

## Limitations (MVP)

- Builds run on the parent only. No local-build path yet.
- Cross-host networking between containers on different parents is not
  managed.
- `DockerContainerHost.run()` does not preserve shell state across
  separate calls. Chain commands with `&&` if you need state.
- `interact()` requires `parent.term == 'ssh'`. Telnet parents are
  rejected.
