# Container-image products

A `kind = "docker_image"` product is a docker image, installed with `docker
run -d` and removed with `docker rm -f` instead of copied onto the host and
run directly. Its coverage story reuses the ordinary `GCOV_PREFIX` machinery
this page's siblings already rely on — the only new problem is getting the
counters out of the container and onto the host otto already knows how to
fetch from.

## The bind mount

`install` runs the container with `-v <cov_dir>:<cov_dir>` — the same path,
inside and outside. A process inside the container that writes its counters
under `GCOV_PREFIX=<cov_dir>` is, from the daemon host's point of view,
writing ordinary files under `<cov_dir>` on its own filesystem: nothing
downstream has to know the writer was containerized, because the fetcher
and `otto cov get` both just see a directory. `GCOV_PREFIX`
and `GCOV_PREFIX_STRIP` mean exactly what they mean for any GCC build —
{doc}`gcc` is the whole rule; a container only changes *where* the two
variables get set, never what they do. See
{doc}`../../../configuration/declared-products-tools` for every parameter
this kind takes, including why an instrumented tarball entry must say
`instrumented = true`.

The worked example throughout this page is `tests/project5/docker/`: a static
`--coverage` build of a small C program, baked into an image and declared
as two products — `cov_container`, loaded from a tarball, and
`cov_container_ref`, the reference the tarball loaded:

```{literalinclude} ../../../../tests/project5/.otto/settings.toml
:language: toml
:start-after: "# needs no pull. An archive scans unknown, hence `instrumented = true`."
:end-before: "# All three unix hosts are eligible"
```

Both entries' `run_args` set `GCOV_PREFIX` and nothing else — no
`GCOV_PREFIX_STRIP`. That half is baked into the image itself at build
time instead of written as a number in settings:

```{literalinclude} ../../../../tests/project5/docker/Dockerfile
:language: docker
```

`tests/project5/docker/build.sh` computes the strip count from its own build
directory's absolute path and passes it as `--build-arg
GCOV_PREFIX_STRIP`. The strip count depends on where on disk the compiler
ran, which differs between checkouts of the same project, so bake it into the
image at build time rather than writing it in `settings.toml`: it then
travels with whatever tarball or reference actually gets run.

## Tarball or reference

`image` is either a `docker save` tarball path or a `registry/name:tag`
reference — see {doc}`../../../configuration/declared-products-tools` for
the exact tarball suffixes. `install` tells the two apart by suffix alone:
a tarball is staged and loaded with `docker load -i`; a reference with
`pull = false` is verified with `docker image inspect`; with `pull = true`
it is `docker pull`ed on every install instead.

`pull` defaults to `false`, which suits a lab that can't reach a registry; set
`pull = true` only where one is reachable. With `pull = false`, an image that
is not already present fails loud at `install`, naming itself. `cov_container`
above is the tarball form, built once by `docker/build.sh` and saved to
`docker/otto-cov-demo.tar` (git-ignored); `cov_container_ref` is the reference
form, declared after it and pointed at the tag the load produced —
`pull = false` because loading already put it in the daemon's cache.

## What is removed

See {doc}`../../../configuration/declared-products-tools` for the exact
`rm`/`rmi` rule. The image `rmi` removes is resolved from the container
itself (`docker container inspect -f '{{.Config.Image}}' <container_name>`), never
from in-process state, so it is found even when `uninstall` runs in a
separate process from the `install` that loaded it — the normal shape of
`otto install` followed later by `otto uninstall`. A container that is
already gone has nothing to resolve an image from, so nothing beyond the
(already-absent) container is removed. The staged tarball under the entry's
`stage_dir` is removed right after a successful `docker load`.

## Logs

`docker logs <container_name>` is captured as
`logs/<host_id>/<product>/debug/container.log` whenever product logs are
hauled — `otto host <id> get-product-logs`, and `Host.uninstall`'s own
haul-then-remove sequence both do this. `otto test` never calls either
one, so a test module that installs and tears down its own products, the way
`tests/project5/tests/test_cov_container.py` does, must haul the container's
logs itself before it removes the container — `docker logs` needs the
container to still exist. `docker logs` captures only the container's PID
1 — whatever a test runs against it separately, with `docker exec`, never
reaches `container.log`.

A container that is already gone (an earlier `otto run uninstall` removed it)
has no logs to collect. The haul notes it — `container <name> is gone; no
docker logs to collect` — and carries on, so a `cleanup` after an `uninstall`,
or a second `cleanup`, succeeds. Any other `docker logs` failure (an
unreachable daemon, say) is still reported.

(coverage-container-root-owned-files)=
## Root-owned files

A container commonly writes its counters as whichever user its own process
runs as, not the lab's SSH user — often root, inside a minimal image with
no user management of its own. `reset_coverage` deletes through `sudo` for
exactly that reason, the same elevated delete a `kmod` product's
kernel-written counters use ({doc}`kernel-modules`).

The write side needs the same care: docker creates a missing bind-mount
source directory itself, owned by `root:root` with mode `0755`. A
container whose own process is not root then fails to write under
`GCOV_PREFIX=<cov_dir>` — silently, with no error anywhere otto surfaces,
just an empty fetch afterward. Either run the container as root, or
pre-create `<cov_dir>` writable for the container's user before `install`
(`tests/project5/tests/test_cov_container.py`'s own fixture does this with
`sudo mkdir -p` and `chmod`).

## Compose is the other path

A container can also be the thing *under test*, rather than the vehicle
for a product — a service your tests talk to, running inside a container
the lab brought up with `docker compose`. That case is
`[docker.use_cases]`: the container becomes an ordinary lab host, and a
`shell` product is declared for it exactly as for any Unix host (given an
absolute `stage_dir`), because the image itself carries no product of
otto's own. `otto run install`, `status` and `cleanup` visit a container
host your project owns a product on once its stack is up
({ref}`a project's own containers <fleet-owned-containers>`); the bed below installs its product from the
suite instead, for the reason it gives
({ref}`coverage-container-host-example`). `docker_image`
is the opposite shape — the image *is* the deliverable, run as a product
on a host that already exists.
{doc}`../../docker/use-cases` is the compose workflow.

(coverage-container-host-example)=
### Collecting from the container

A container host is an ordinary coverage host, so `[coverage] hosts` may name
it alone. This is the bed project `tests/project_ctr/`'s settings: the
compose stack and use-case that create the container host
({doc}`../../docker/use-cases`), the product, and the pattern.

```{literalinclude} ../../../../tests/project_ctr/.otto/settings.toml
:language: toml
:start-after: "# docs-container-host-begin"
:end-before: "# docs-container-host-end"
```

- `match` selects the container by its host id
  ({ref}`host identity <host-identity>`).
- `stage_dir` is absolute because a container host has no default staging
  directory and no login home.
- `cov_dir` is where the program's counters land: run it with
  `GCOV_PREFIX=/var/cov/counter`. The directory must be writable by the
  user the program runs as
  ({ref}`root-owned files <coverage-container-root-owned-files>`).
- `hosts` fullmatches only the container, so no bare host is collected.

The build path can stay as built: the product was compiled in a container
with the project mounted at `/build`, so its counters carry
`/build/build/main.gcda` paths that exist nowhere on the lab, and no
`GCOV_PREFIX_STRIP` is required. {ref}`coverage-build-requirement` is the
pairing rule.

Two things differ from a bare host:

- The program writes its counters as its own user, so `cov_dir` must exist
  and be writable before it runs. `otto run install` installs the product on
  the container like on any host, but creates no `cov_dir`; the bed's suite
  therefore stages and installs the product itself, after creating
  `stage_dir` and a writable `cov_dir`. Its `_install` does it with the
  product's own `stage` and `install` verbs (`_sh` runs one command through
  `host.exec` and raises unless it succeeds):

  ```{literalinclude} ../../../../tests/project_ctr/tests/test_cov_ctr.py
  :language: python
  :pyobject: _install
  ```

  Its class fixture gets the container host — the plain `all_hosts` the test
  calls is not a project's view, so it leaves containers out unless asked
  (`include_containers=True`; {ref}`a project's own containers <fleet-owned-containers>`);
  `ctx.get_host("<id>")` works too — installs, and runs the program once with `GCOV_PREFIX` set
  (`_CONTAINER` is the container's host-id pattern, `_BINARY` the installed
  program):

  ```{literalinclude} ../../../../tests/project_ctr/tests/test_cov_ctr.py
  :language: python
  :pyobject: TestCovCtr._container
  ```

- The stack must be up when coverage is fetched. otto does not start
  containers on use: a stopped container fails the fetch of every product
  on it, each a failed product named as
  `<id>/<product>: fetch failed: Container '<id>' is not running: ...`,
  which names the remedy (`otto docker compose up <use-case>`).
