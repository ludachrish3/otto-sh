# Container-image products

A `kind = "docker_image"` product is a docker image, installed with `docker
run -d` and removed with `docker rm -f` instead of copied onto the host and
run directly. Its coverage story reuses the ordinary `GCOV_PREFIX` machinery
this page's siblings already rely on — the only new problem is getting the
counters out of the container and onto the host otto already knows how to
fetch from.

(coverage-container-bind-mount)=
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
this kind takes, including why an entry must say `instrumented = true`.

The worked example is the docker track's `banner` product
({doc}`../../../setup/tracks/docker`): a static `--coverage` build of a small
C++ tool, copied into an image and declared as a reference to it:

```{literalinclude} ../../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:start-after: "# doc: begin image-product"
:end-before: "# doc: end image-product"
```

```{literalinclude} ../../../examples/quickstart-docker/docker/banner/Dockerfile
:language: docker
```

`run_args` sets `GCOV_PREFIX` and nothing else. `GCOV_PREFIX_STRIP` is set
where the process starts, and its value depends on where the build ran, so it
is computed by the tests, never hard-coded in them ({ref}`a tool run once per test
<coverage-run-per-test-case>`). The track's tests compute it and pass it on
each `docker exec`. A process the image's `CMD` starts gets it from an `ENV`
the image sets from a build argument (`ARG GCOV_PREFIX_STRIP`, then `ENV
GCOV_PREFIX_STRIP=$GCOV_PREFIX_STRIP`), and the build passes the computed
value with `otto docker compose build --build-arg GCOV_PREFIX_STRIP=<n>`.

(coverage-container-stop-to-flush)=
### A long-running process: stop it to flush

A process the image's `CMD` starts runs for as long as the container does,
and writes its counters only when it exits
({ref}`how it must exit <coverage-process-exit>`). An exec-form `CMD`
(`CMD ["prog", ...]`) runs as the container's PID 1, which ignores a SIGTERM
it does not handle, so `docker stop` kills it with SIGKILL after its timeout
and nothing is written. `uninstall` is no way out: its `docker rm -f` writes
nothing either. One coverage run of such a product goes in this order:

1. Make the `CMD` handle SIGTERM by calling `exit()`.
2. Stop the container in a fixture's teardown. Its process exits and writes
   its counters under `cov_dir`, which the bind mount puts on the parent:

   ```python
   from collections.abc import AsyncIterator

   import pytest_asyncio

   from otto.lab import get_host


   class TestMyImage:
       @pytest_asyncio.fixture(scope="class", autouse=True)
       @classmethod
       async def _stop_after(cls) -> AsyncIterator[None]:
           """Stop the container after the class: its process writes its counters."""
           yield
           result = await get_host("<parent>").exec("docker stop <container_name>")
           if not result.is_ok:
               raise RuntimeError(f"docker stop failed: {result.value}")
   ```

3. Let the run end. Its tail fetches the counters from the parent, then
   clears them.
4. Start the container again on its parent:

   ```bash
   otto --lab <lab> host <parent> exec 'docker start <container_name>'
   ```

   A stopped container answers not installed, and its name blocks the next
   install. Started, it answers installed again.
5. Report: `otto --lab <lab> cov report <run dir>`. For an image named by
   reference, the unit tier reads the product's objects from its running
   container, and a stopped one refuses the whole report
   ({ref}`the unit refusals <coverage-unit-filter>`).

`otto test --cov --cov-report` reports inline before step 4, so for an image
named by reference it loses its report to that refusal; run step 5 instead.
The process step 4 starts holds its own counts from then on, and a clean
cannot zero them while it runs: before the next coverage run, stop it as
{ref}`zeroing a running process <coverage-clean-running-process>` says.

## Tarball or reference

`image` is either a `docker save` tarball path or a `registry/name:tag`
reference — see {doc}`../../../configuration/declared-products-tools` for
the exact tarball suffixes. `install` tells the two apart by suffix alone:
a tarball is staged and loaded with `docker load -i`; a reference with
`pull = false` is verified with `docker image inspect`; with `pull = true`
it is `docker pull`ed on every install instead.

A reference has no local file to read the image's objects from, so capturing
its counters logs one INFO line, `<product>: object list unavailable: image
<ref> is a reference, not a local tarball`. It needs no action: each `.gcda` is
then paired with its notes by its own path, under the project root
({ref}`coverage-pairing-errors` lists what can go wrong there).

`pull` defaults to `false`, which suits a lab that can't reach a registry; set
`pull = true` only where one is reachable. With `pull = false`, an image that
is not already present fails loud at `install`, naming itself. `banner` above
is the reference form: `otto docker compose build` builds
`quickstart-banner:latest` on the parent, so the image is already in the
daemon's cache and the entry keeps the default `pull = false`. The tarball
form, its staging and its `instrumented = true`, are under
{ref}`docker-image-kind`.

## What is removed

See {doc}`../../../configuration/declared-products-tools` for the exact
`rm`/`rmi` rule. The staged tarball under the entry's `stage_dir` is removed
right after a successful `docker load`.

## Logs

`docker logs <container_name>` is captured as
`logs/<host_id>/<product>/debug/container.log` whenever product logs are
hauled — `otto host <id> get-product-logs`, and `Host.uninstall`'s own
haul-then-remove sequence both do this. `otto test` never calls either
one, so a test module that installs and removes its own products must haul
the container's logs itself before it removes the container — `docker logs`
needs the container to still exist. `docker logs` captures only the
container's PID 1 — whatever a test runs against it separately, with
`docker exec`, never reaches `container.log`.

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
pre-create `<cov_dir>` writable for the container's user before `install`,
for example with `otto host <parent> exec --sudo 'install -d -m 777
<cov_dir>'`.

## Compose is the other path

A container can also be the thing *under test*, rather than the vehicle
for a product — a service your tests talk to, running inside a container
the lab brought up with `docker compose`. That case is
`[docker.use_cases]`: the container becomes an ordinary lab host, and a
`shell` product is declared for it exactly as for any Unix host (given an
absolute `stage_dir`), because the image itself carries no product of
otto's own. No kind declares a tool already built into the image: a `shell`
product copies its `artifact` in on every install. To skip that copy, give the
entry a `class` whose `stage` does nothing ({ref}`class-entries`), and keep
`artifact` naming the local build the image's binary came from, which coverage
reads the product's objects from. `otto run install` installs into a project's
own container once its stack is up, as the track does
({ref}`a project's own containers <fleet-owned-containers>`).
`docker_image` is the opposite shape — the image *is* the deliverable, run
as a product on a host that already exists.
{doc}`../../docker/use-cases` is the compose workflow.

(coverage-container-host-example)=
### Collecting from the container

A container host is an ordinary coverage host, so `[coverage] hosts` may name
it. The docker track installs two `shell` products, `wordcount` and `shout`,
into its compose container:

```{literalinclude} ../../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:start-after: "# doc: begin compose-products"
:end-before: "# doc: end compose-products"
```

```{literalinclude} ../../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:start-after: "# doc: begin coverage"
:end-before: "# doc: end coverage"
```

- `match` selects the container by its host id
  ({ref}`host identity <host-identity>`).
- `stage_dir` is absolute, for the reason [Declare the
  products](../../../setup/tracks/docker.md#declare-the-products) gives.
- `cov_dir` is left at its default, `/tmp/<name>`, which any user can
  create.
- `hosts` names the parent too, for the image product
  ([Coverage](../../../setup/tracks/docker.md#coverage) says why).

A project with only container products names only the container in `hosts`.

The track builds its tools on the build machine. A product can instead be
compiled in a builder container with the project mounted at `/build`.
Its counters then carry `/build/...` paths, which pair with their `.gcno`
files by path even though `/build` exists nowhere on the lab, so no
`GCOV_PREFIX_STRIP` is needed ({ref}`coverage-build-requirement`).

`otto run install` stages and installs the products into the container
and creates `stage_dir`, as on any host. It never creates a `cov_dir`: the
tool's own process creates it as it writes its first counters, so a
`cov_dir` that user cannot create (one under `/var` for a user that is not
root, say) must be made writable first. The default `/tmp/<name>` needs
nothing. The tests then run each tool with `GCOV_PREFIX`
and `GCOV_PREFIX_STRIP` set, through one helper:

```{literalinclude} ../../../examples/quickstart-docker/pylib/quickstart_docker/tools.py
:language: python
:pyobject: run_tool
```

One thing differs from a bare host: the stack must be up when coverage is
fetched. otto does not start containers on use: a stopped container fails
the fetch of every product on it, each a failed product named as
`<id>/<product>: fetch failed: Container '<id>' is not running: ...`,
which names the remedy (`otto docker compose up <use-case>`). `otto cov get`
then exits `1` naming it ({doc}`../get`); `otto test --cov` warns and still
passes ({doc}`../during-tests`).

A compose container host's counters stay inside the container, so stop a
long-running product's process there, not the container. If the container
was stopped, bring the stack back up with a plain `otto docker compose up
<use-case>` and an unchanged compose config, which starts the stopped
container again with its files, then fetch. A changed config recreates the
container instead, and its counters are lost. The track's tools exit after
each run, so they need none of this.
