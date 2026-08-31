# Docker services as lab hosts

Not every host in a lab is a machine. Otto can bring up docker compose
services on a docker-capable lab host and register each resulting **container
as a first-class lab host** — it appears in `--list-hosts`, completes on the
command line, and accepts `otto host <id> run`, `login`, `put` and `get` like
any other.

That makes a container a place to run a service your tests talk to, a mock of
a device that is not on the bench today, or a build environment — declared in
the project, not set up by hand.

This page is the tour. Each step links to the page that owns the detail.

## What you need first

- A lab host that can run docker — call it `dock1`. Tag it in your lab file
  with `"docker_capable": true`, plus a `roles` tag naming what the lab uses
  it for. See {doc}`../guide/configuration/lab-config`.

  ```text
  {
    "name": "dock1",
    "labs": ["example_lab"],
    "hosts": [
      { "ip": "10.0.0.13", "creds": [...], "docker_capable": true, "roles": ["edge"] }
    ]
  }
  ```

  The element's `name` is where the `dock1` in `dock1.demo.api` below comes
  from.

- A compose file in your project. An ordinary one — otto adds nothing to it
  and requires nothing of it.

  ```yaml
  # docker/compose.yml
  services:
    api:
      image: nginx:alpine
  ```

Otto never has to have local root or a local docker daemon: every docker
command is routed over the lab host's existing SSH connection.

## Step 1 — declare it

Two blocks in the project's `.otto/settings.toml`. The first is a file
inventory; the second says what deployment that file is part of.

```toml
[[docker.composes]]
name = "core"                 # a handle you can refer to
path = "docker/compose.yml"
services = ["api"]            # the service names inside that file

[[docker.use_cases]]
name = "demo"                 # the deployment's name — you'll type this
composes = ["core"]
role = "edge"                 # deploy onto the host tagged "edge"
```

The `[[docker.use_cases]]` block is the interesting one. A **use-case** is a
named deployment that any number of active projects can contribute to, so
`otto docker up demo` means the same thing whichever combination of projects
is loaded. Here there is exactly one contributor, which is the simple case.

Check it before deploying anything — this contacts nothing:

```bash
otto --lab example_lab docker use-cases
```

It prints a table: which fragments were declared, which lab host each resolves
to, and which env keys it will set.

## Step 2 — bring it up

```bash
otto --lab example_lab docker up demo
```

Otto builds any images the project declares, stages the compose files onto the
lab host, and runs one `docker compose up -d` there. Add `--dry-run` first if
you want to see the plan — including the exact compose command — without
touching anything:

```bash
otto --lab example_lab --dry-run docker up demo
```

### If the service is your own image, not a published one

`nginx:alpine` above is pulled, not built. When the service is your product,
add a `[[docker.images]]` entry naming its Dockerfile and build context, and
have the compose file name that image's `:latest` tag instead of a registry
one. `otto docker up` builds before it deploys, and `otto docker build` runs
the same step on its own — including the context-hash caching that decides
when a rebuild is actually needed. {doc}`../guide/cli/docker/build` and
{doc}`../guide/cli/docker/rebuild-policy` are the home for both; the keys
themselves are in {doc}`../guide/configuration/settings`.

## Step 3 — use the container like a host

The container is now a lab host, named `<parent>.<usecase>.<service>`:

```bash
otto --lab example_lab --list-hosts            # dock1.demo.api is in the list
otto --lab example_lab host dock1.demo.api run "uname -a"
otto --lab example_lab host dock1.demo.api login
otto --lab example_lab host dock1.demo.api get /etc/os-release ./
```

Tear it down with `otto --lab example_lab docker down demo`. The id keeps
completing afterwards — it comes from the declaration, not from what is
running — and the next access starts the stack again.

## Templating: getting lab facts into a service

The part worth knowing about early, because it is what makes a container a
*lab* host rather than an isolated box: otto can feed a service values only
otto knows — the address of the host a role landed on, of the parent it is
running on, or of another unix host in the lab, such as a bench device it is
meant to drive — **without putting anything otto-specific into your compose
file.**

Your compose file stays plain compose, using a variable name *you* chose:

```yaml
services:
  api:
    image: nginx:alpine
    environment:
      - EDGE_ADDR                      # supplied from the environment
      - LOG_LEVEL=${LOG_LEVEL:-info}   # ordinary compose default
```

Your `settings.toml` says where that value comes from:

```toml
[[docker.use_cases]]
name = "demo"
composes = ["core"]
role = "edge"
env = { EDGE_ADDR = "${otto:role.edge.addr}", LOG_LEVEL = "debug" }
```

`${otto:role.edge.addr}` is a **fact reference**. Otto resolves it at deploy
time — the address of whichever lab host the `edge` role landed on — and sets
`EDGE_ADDR` to the result. The compose file never sees `${otto:...}`; it sees
a resolved address under the name it asked for. `${otto:parent.addr}`,
`${otto:host.<id>.addr}` and a few more are available the same way.

The point of splitting it that way:
`EDGE_ADDR=10.0.0.13 docker compose -f docker/compose.yml up -d` run by hand
behaves identically. Your compose file remains deployable by someone who has
never installed otto.

There is more to it — a second channel where a project registers Python code
to compute values or render whole compose files, an allowlist for passing
variables through from your shell, and `--env` on the command line. The full
mechanism, with the rules for each layer, is in
{doc}`../guide/cli/docker/use-cases`.

## Where to go next

- {doc}`../guide/cli/docker/use-cases` — the workflow home. Fragments and how
  several projects contribute to one deployment, swapping a mock for the real
  service (provider competition), how placement resolves, the full templating
  walkthrough, and the compose adapter.
- {doc}`../guide/cli/docker/index` — every `otto docker` verb, its options,
  and how container hosts behave.
- {doc}`../guide/configuration/settings` — the reference for every
  `[docker]`, `[[docker.images]]`, `[[docker.composes]]` and
  `[[docker.use_cases]]` key.
- {doc}`../guide/configuration/lab-config` — `docker_capable` and `roles` on
  a lab host.
- {doc}`../library/suite-recipes` — deploying a use-case from an instruction
  or a test suite instead of the CLI.
- {doc}`../api/docker/index` — the API reference for `otto.docker`.
- {doc}`../architecture/subsystems/docker-hosts` — why a container delegates
  to its parent host, and what that rules out.
