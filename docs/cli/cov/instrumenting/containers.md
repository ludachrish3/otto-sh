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

The worked example throughout this page is `tests/repo5/docker/`: a static
`--coverage` build of a small C program, baked into an image and declared
as two products — `cov_container`, loaded from a tarball, and
`cov_container_ref`, the reference the tarball loaded:

```{literalinclude} ../../../../tests/repo5/.otto/settings.toml
:language: toml
:start-after: "# needs no pull. An archive scans unknown, hence `instrumented = true`."
:end-before: "# All three unix hosts are eligible"
```

Both entries' `run_args` set `GCOV_PREFIX` and nothing else — no
`GCOV_PREFIX_STRIP`. That half is baked into the image itself at build
time instead of written as a number in settings:

```{literalinclude} ../../../../tests/repo5/docker/Dockerfile
:language: docker
```

`tests/repo5/docker/build.sh` computes the strip count from its own build
directory's absolute path and passes it as `--build-arg
GCOV_PREFIX_STRIP`. Baking it into the image, rather than writing it in
`settings.toml`, is the point: the strip count is a property of *the
build* — where on disk the compiler ran — not of the product declaration,
and that path differs between a worktree and a plain checkout of the same
repo. A number in settings would be wrong the moment someone else built
the image from a different checkout; baked into the image, it travels
with whatever tarball or reference actually gets run.

## Tarball or reference

`image` is either a `docker save` tarball path or a `registry/name:tag`
reference — see {doc}`../../../configuration/declared-products-tools` for
the exact tarball suffixes. `install` tells the two apart by suffix alone:
a tarball is staged and loaded with `docker load -i`; a reference with
`pull = false` is verified with `docker image inspect`; with `pull = true`
it is `docker pull`ed on every install instead.

`pull` defaults to `false` because a lab is commonly air-gapped by
policy — a `docker pull` on every install would routinely reach for a
registry the lab cannot see. With `pull = false`, an image that is not
already present fails loud at `install`, naming itself, instead of
hanging behind a pull that was never going to land. `cov_container` above
is the tarball form, built once by `docker/build.sh` and saved to
`docker/otto-cov-demo.tar` (git-ignored); `cov_container_ref` is the
reference form, declared after it and pointed at the tag the load
produced — `pull = false` because loading already put it in the daemon's
cache.

## What is removed

See {doc}`../../../configuration/declared-products-tools` for the exact
`rm`/`rmi` rule. The image `rmi` removes is resolved from the container
itself (`docker container inspect -f '{{.Config.Image}}' <container_name>`), never
from in-process state, so it is found even when `uninstall` runs in a
separate process from the `install` that loaded it — the normal shape of
`otto install` followed later by `otto uninstall`. A container that is
already gone has nothing to resolve an image from, so nothing beyond the
(already-absent) container is removed. The staged tarball under `/tmp` is
removed right after a successful `docker load` — it is an intermediate the
load has already consumed, not the product itself.

## Logs

`docker logs <container_name>` is captured as
`logs/<host_id>/<product>/debug/container.log` whenever product logs are
hauled — `otto host <id> get-product-logs`, and `Host.uninstall`'s own
haul-then-remove sequence both do this. `otto test` never calls either
one, so a suite that installs and tears down its own products, the way
`tests/repo5/tests/test_cov_container.py`'s does, must haul the container's
logs itself before it removes the container — `docker logs` needs the
container to still exist. `docker logs` captures only the container's PID
1 — whatever a suite runs against it separately, with `docker exec`, never
reaches `container.log`.

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
(`tests/repo5/tests/test_cov_container.py`'s own fixture does this with
`sudo mkdir -p` and `chmod`).

## Compose is the other path

A container can also be the thing *under test*, rather than the vehicle
for a product — a service your suite talks to, running inside a container
the lab brought up with `docker compose`. That case is
`[docker.use_cases]`: the container becomes an ordinary lab host, and a
`shell` product installs onto it exactly as it would onto any Unix host,
because the image itself carries no product of otto's own. `docker_image`
is the opposite shape — the image *is* the deliverable, run as a product
on a host that already exists.
{doc}`../../../getting-started/docker-services` is the compose
walkthrough.
