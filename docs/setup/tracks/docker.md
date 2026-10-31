# Docker track: tools in containers

This track puts the shell track's tools in containers. Two of them,
`wordcount` and `shout`, are installed into a container that a compose
use-case brings up. A third, `banner`, is baked into an image of its own. otto
builds both images on a docker host, brings the stack up, and installs and
tests the tools from the build machine. The track ends with one coverage
report that combines three tiers: the end-to-end tests, the unit tests and a
session by hand.

## You need

- A Linux host you can reach over SSH that runs Docker with Compose, where
  your login may run `docker` ([Where docker
  runs](../../cli/docker/index.md#where-docker-runs)). This is the docker
  parent. The buildx plugin is recommended, not required: otto runs plain
  `docker build`, and without buildx docker falls back to its legacy builder,
  which builds these images too but prints a deprecation warning. On Debian
  and Ubuntu, buildx is a package of its own, `docker-buildx`; `docker buildx
  version` answers when it is there. Your login also needs `sudo` there:
  `banner`'s container writes its counters as root, and otto clears them with
  `sudo` ({ref}`root-owned files <coverage-container-root-owned-files>`). otto answers `sudo`'s prompt with
  the login's password from `lab.json`, so a password or `NOPASSWD` both work
  ({doc}`../../cli/host/capabilities/privilege`).
- The same CPU architecture on that host as on the build machine. The tools
  are built on the build machine as static binaries and copied into the
  containers; nothing is compiled on the host.
- `alpine:3.20` on the host, or a registry it can pull it from: both images
  start from it.
- CMake 3.24 or newer, g++ with its static C and C++ libraries (Debian and
  Ubuntu install them with g++), git and lcov on the build machine.
  [Coverage's prerequisites](../../cli/cov/index.md#prerequisites) list what
  the coverage commands need there.
- GoogleTest, installed (`libgtest-dev` on Debian and Ubuntu) or downloaded by
  the build. Offline, install it first; {doc}`../../installation` covers
  installing otto itself offline.
- The shared setup done: {doc}`../shared-setup`.

The captures below were taken with:

```{literalinclude} ../../examples/captures/qs-docker-versions.txt
:language: text
```

The `fleet of interest` line is explained under the install dry run below.

## Get the example

Download {download}`quickstart-docker.tar.gz </_static/generated/examples/quickstart-docker.tar.gz>`
and unpack it:

```bash
tar xzf quickstart-docker.tar.gz && cd quickstart-docker
```

`lab_data/lab.json` holds one placeholder host, `docker-host`. Edit its
address and credentials to your docker host's, the same edit as [one host,
written inline](../shared-setup.md#one-host-written-inline). Keep its
`docker_capable: true`, which lets otto build and deploy there
({doc}`../../configuration/lab/docker`). Then make the project a git
repository and tell your shell where it is. If git asks who you are, set
`git config user.name` and `git config user.email` first:

```bash
git init && git add -A && git commit -m "quickstart-docker"
export OTTOPATH=$PWD
export OTTO_XDIR=~/otto-runs
```

The project is a git repository because coverage records the commit a run
measured. Each command's output then goes under `~/otto-runs/otto-output/`
({ref}`invocation-output-directory`). Check that otto reaches your host, as
in {ref}`the shared setup's first success <shared-setup-first-success>`.
`--list-hosts` already lists `docker-host.quickstart.tools`: a declared
container host exists as an id before its stack is up ([Container
hosts](../../cli/docker/index.md#container-hosts)), and using it before `up`
fails.

## Build

```bash
cmake -S . -B build && cmake --build build
```

```{literalinclude} ../../examples/quickstart-docker/CMakeLists.txt
:language: cmake
:caption: CMakeLists.txt
```

```{literalinclude} ../../examples/quickstart-docker/src/banner/CMakeLists.txt
:language: cmake
:caption: src/banner/CMakeLists.txt
```

The build is {doc}`the shell track's <shell>`, plus `banner`, a third tool
that links the same library. The tools link statically because alpine has no
glibc. CMake compiles with absolute source and object paths, so the three
`main.cpp` files stay apart. That is all {ref}`the one build requirement
<coverage-build-requirement>` asks.

## Declare the images and the use-case

The `[docker]` table declares the two images, the compose file and the
use-case that names it:

```{literalinclude} ../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin docker"
:end-before: "# doc: end docker"
```

```{literalinclude} ../../examples/quickstart-docker/docker/runtime/Dockerfile
:language: docker
:caption: docker/runtime/Dockerfile
```

```{literalinclude} ../../examples/quickstart-docker/docker/runtime/compose.yml
:language: yaml
:caption: docker/runtime/compose.yml
```

```{literalinclude} ../../examples/quickstart-docker/docker/banner/Dockerfile
:language: docker
:caption: docker/banner/Dockerfile
```

`banner`'s build context is the build directory that holds its binary, so
build the tools before the image. A long-running process belongs in the
image's `CMD`, never in a command backgrounded with `&`
({ref}`a long-running product <declared-long-running>`). Docker reads a `.dockerignore` only at the
root of a build context, and neither context here is the project root, so
the example needs none ({ref}`invocation-output-directory` says when one
does).

Check what is declared, then preview the build and the deployment. A dry run
contacts no host:

```bash
otto --lab example_lab docker use-cases quickstart
otto --lab example_lab -n docker compose build quickstart
otto --lab example_lab -n docker compose up quickstart
```

```{literalinclude} ../../examples/captures/qs-docker-use-cases.txt
:language: text
```

```{literalinclude} ../../examples/captures/qs-docker-build-dry.txt
:language: text
```

```{literalinclude} ../../examples/captures/qs-docker-up-dry.txt
:language: text
```

The `Output directory` lines are the docs build's, under `/tmp/otto-gs`;
yours are your own. The `/tmp/otto-docker-<login>/...` paths are otto's
staging on the parent, in the private directory of the account the parent
logs in as ({ref}`docker-staging`); yours differ in that login and in the
suffix (below). Then build the images and bring the stack up. The build prints
docker's own log, which is not shown here:

```bash
otto --lab example_lab docker compose build quickstart
otto --lab example_lab docker compose up quickstart
```

```{literalinclude} ../../examples/captures/qs-docker-up.txt
:language: text
```

The `tools` service's container is now the lab host
`docker-host.quickstart.tools`: the parent, the use-case and the service
([Container hosts](../../cli/docker/index.md#container-hosts)). The
captures' compose project ends in `docs`: they were taken with
`OTTO_COMPOSE_SUFFIX=docs`. Yours ends in your login name
({ref}`docker-use-case-naming`).

The `up` staged its compose files in the parent's private staging directory
and keeps them; [Clean up](#clean-up) removes them. The build removes its own
staged inputs once it has run.
{ref}`docker-first-deploy` is the same sequence for a project of your own.

## Declare the products

`wordcount` and `shout` are `shell` products on the container host:

```{literalinclude} ../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin compose-products"
:end-before: "# doc: end compose-products"
```

Their `stage_dir` is absolute because a container host has no default
staging directory and no login home. `banner` is the image itself, a
`docker_image` product on the parent:

```{literalinclude} ../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin image-product"
:end-before: "# doc: end image-product"
```

`image` names the image by the exact reference the build tagged it with: an
entry's `name` plus `:latest` ({ref}`docker-build-naming`), here
`quickstart-banner:latest`. That is the only link between the product and its
`[[docker.images]]` entry, and the install looks the reference up on the host
the product's [`match`](../../configuration/declared-products-tools.md#matching)
selects, so the image must be built there (unless the entry sets `pull = true`,
{ref}`docker-image-kind`). `compose build` above built it with
the use-case's images. A project with a `docker_image` product and no use-case
builds it with `otto docker build --parent <host>`, naming that host
({doc}`../../cli/docker/build`).

`otto run install` reaches the container because the project owns products
on it ({ref}`a project's own containers <fleet-owned-containers>`). It never
starts the stack, which is why the stack went up first
({ref}`docker-start-first`).

Preview the install. The dry run contacts no host:

```bash
otto --lab example_lab -n run install
```

```{literalinclude} ../../examples/captures/qs-docker-install-dry.txt
:language: text
```

The lab-wide commands log `fleet of interest: 2 of 3 lab hosts`: the parent
and its container. The third host is otto's {ref}`built-in local
host <overview-local-host>`, which lab-wide commands leave out.

Install the tools:

```bash
otto --lab example_lab run install
```

```{literalinclude} ../../examples/captures/qs-docker-install.txt
:language: text
```

Lines tagged `@docker-host:tools` are the container host's own: its name is
the parent's id and the service. The `docker exec` and `docker cp` lines that
carry out its work run on the parent, tagged `@docker-host`. otto works on
the parent and the container at once, so their lines interleave differently
on each run. {ref}`docker-image-kind` documents every
field of a `docker_image` entry, and
{doc}`../../configuration/declared-products-tools` those of a `shell` entry.

## An end-to-end test

`libs` puts `pylib` on the import path, and `tests` is where `otto test`
finds this page's tests ({doc}`../../configuration/settings`):

```{literalinclude} ../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin libs"
:end-before: "# doc: end libs"
```

The tests run each tool through one helper:

```{literalinclude} ../../examples/quickstart-docker/pylib/quickstart_docker/tools.py
:language: python
:caption: pylib/quickstart_docker/tools.py
```

```{literalinclude} ../../examples/quickstart-docker/tests/test_tools.py
:language: python
:caption: tests/test_tools.py
```

Each tool writes its counters as it exits, with `GCOV_PREFIX` and
`GCOV_PREFIX_STRIP` as in {doc}`the shell track <shell>`
({ref}`a tool run once per test <coverage-run-per-test-case>`). `banner`'s
container already has `GCOV_PREFIX` from the product's `run_args`, so the
helper adds only `GCOV_PREFIX_STRIP` to its `docker exec`.

A process that the image's `CMD` starts gets the strip count another way:
{doc}`../../cli/cov/instrumenting/containers` covers it.

The coverage section below runs these tests, with coverage on.

## A unit test

The unit tests exercise the library on the build machine:

```{literalinclude} ../../examples/quickstart-docker/tests/unit/test_text.cpp
:language: cpp
:caption: tests/unit/test_text.cpp
```

Run them:

```bash
./build/tests/unit/unit_tests
```

```{literalinclude} ../../examples/captures/qs-docker-unit.txt
:language: text
```

The first line names the GoogleTest the build used, so its path can differ
from yours.

## Coverage

The project declares three tiers:

```{literalinclude} ../../examples/quickstart-docker/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin coverage"
:end-before: "# doc: end coverage"
```

`hosts` names both hosts ({ref}`coverage-configuration`). `wordcount` and
`shout` write their counters in the container. `banner` writes them under its `cov_dir`, which the bind mount
puts on the parent, so the parent is where they are fetched from.

Collect the tiers in this order. {doc}`../../cli/cov/tiers` explains the
order, and {doc}`../../cli/cov/report` the rules the report applies. The
stack must be up whenever coverage is fetched
({ref}`coverage-container-host-example`).

First, zero the counters ({doc}`../../cli/cov/clean`):

```bash
otto --lab example_lab cov clean
```

Exercise `wordcount` by hand, twice, in the container. `/tmp/wordcount` is
its `cov_dir`, the default `/tmp/<name>`. `GCOV_PREFIX_STRIP` is `3` for the
docs build's root, `/tmp/otto-gs/quickstart-docker`; [the shell track's
coverage section](shell.md#coverage) shows how to print yours. Run it with
your number in place of `3`:

```bash
otto --lab example_lab host docker-host.quickstart.tools exec 'GCOV_PREFIX=/tmp/wordcount GCOV_PREFIX_STRIP=3 /opt/quickstart/wordcount by hand; GCOV_PREFIX=/tmp/wordcount GCOV_PREFIX_STRIP=3 /opt/quickstart/wordcount once more'
```

```{literalinclude} ../../examples/captures/qs-docker-manual-run.txt
:language: text
```

Retrieve that session as the manual tier, under a ticket:

```bash
otto --lab example_lab cov get --tier manual --ticket QS-1 --tester-name docs --tester-email docs@example.invalid
```

```{literalinclude} ../../examples/captures/qs-docker-cov-get-manual.txt
:language: text
```

As with the install, the parent's lines and the container's interleave
differently on each run. Only `wordcount` ran, so only `wordcount` is
captured. `otto-no-cov-dir` means `shout` has no counters in the container.
`banner`'s `cov_dir` exists, because docker made it for the bind mount, but
holds none yet, so its archive is empty. The `skipped` line counts the system
headers the tool compiled in, which are not the project's sources.

`--tester-name` and `--tester-email` record who did the testing. Leave them
out to record your own login and git email ({doc}`../../cli/cov/get`, which
also covers a product whose counters cannot be fetched).

`otto cov get` wrote the capture under `.otto/coverage/manual/`. Commit it:

```bash
git add .otto/coverage/manual && git commit -m "manual coverage QS-1"
```

This commit comes before the e2e run because a report only takes an e2e
capture taken at `HEAD` ({ref}`the report's commit rule <coverage-capture-commit>`).
Commit any other change, a settings edit included, before the run too: a commit
made after it leaves the capture at an older commit, and the report refuses it.

Run the end-to-end tests with coverage, into a directory of your choosing.
`--no-random` keeps the test order stable:

```bash
otto --lab example_lab test --cov --cov-dir ~/otto-runs/quickstart-docker/e2e/cov --no-random TestTools
```

```{literalinclude} ../../examples/captures/qs-docker-test-cov.txt
:language: text
```

The run clears every product's counters before the first test and again
after collection. `banner`'s are cleared through `sudo`, because its
container wrote them as root. Here too the parent's lines and the
container's interleave differently on each run. `object list unavailable` is
expected for `banner`: the report reads its binary from the running container
instead (below).

`--cov-dir` refuses a directory that is not empty, so a second run adds
`--overwrite-cov-dir` or names a new directory ({ref}`coverage-cov-dir`).

The report reads `banner`'s binary from its running container (below), so
the container must be up when you report. The track's tests leave it running.
If you stopped it, as a long-running process's counters need, start it again
first ({ref}`coverage-container-stop-to-flush`).

Your unit run's counters are already in `build/`, from the unit test
section. Report all three tiers, naming the directory that holds `cov/`,
not `cov/` itself ({doc}`../../cli/cov/report`):

```bash
otto --lab example_lab cov report ~/otto-runs/quickstart-docker/e2e --dir ~/otto-runs/quickstart-docker/report
```

```{literalinclude} ../../examples/captures/qs-docker-report.txt
:language: text
```

The `unit unit:` line is the unit tier, keeping only the products' own
sources ({ref}`what the unit report counts <coverage-unit-filter>`).
`banner` is declared by an image reference, so it has no local artifact to
list its sources from: the report finds its binary in the running
container and copies it out. That is why the report comes before the clean
up. The log says nothing of the other two tiers, but the report loads both.
Open `~/otto-runs/quickstart-docker/report/index.html` to see all three, and
which tier covered each line.

## Monitoring

This track skips monitoring. Its tools run in containers, and `otto monitor`
leaves container hosts out of its walk ({ref}`a project's own
containers <fleet-owned-containers>`). Monitor the parent like any Linux
host: {doc}`../../cli/monitor/live`, and {doc}`../../extend/custom-parsers`
for a metric of your own; {doc}`shell` shows one.

## Clean up

{ref}`teardown-recipe` is the order for taking any lab down. This track
skips its two uninstall steps: `wordcount` and `shout` live only in the
stack's container and go with it, and `cleanup` uninstalls `banner` itself.
So the stack goes down first, then `cleanup` runs, and the images are
removed by hand last. Take the stack down:

```bash
otto --lab example_lab docker compose down quickstart
```

`down` first lists the files it staged on the parent (the shell function
and the `d` and `f` lines), then runs `docker compose down` on exactly those
files:

```{literalinclude} ../../examples/captures/qs-docker-down.txt
:language: text
```

`down` never removes the files otto staged on the parent, because the next
`down` reads them back to scope what it stops ([what `down`
removes](../../cli/docker/use-cases.md#what-down-removes)). `cleanup` removes
them once the stack has no containers. It also removes `banner`'s container
([what `cleanup` takes off the lab](../../cli/run/defaults.md#what-cleanup-takes-off-the-lab)).
`wordcount` and `shout` went with their container, so `cleanup` finds nothing
of theirs to remove:

```{literalinclude} ../../examples/captures/qs-docker-cleanup.txt
:language: text
```

The two flags are the recipe's opt-outs for a shared lab. On the parent,
`cleanup` saves `banner`'s `docker logs` with the run's output, removes its
container, and removes the stack's staging; the removal checks again, in
the same command, that the stack has no containers. The closing `skipped` line
is the container host it passed over, which the recipe explains.

`cleanup` leaves the two images: otto removes no image it built, and
`banner`'s uninstall keeps an image it names by reference
({ref}`docker-image-kind`). Remove them
({doc}`../../cli/docker/image/rm`):

```bash
otto --lab example_lab docker image rm quickstart-runtime:latest quickstart-banner:latest
```

It prints docker's own `Untagged:` and `Deleted:` lines for each image.

Docker created `banner`'s `cov_dir` as root for the bind mount, and it
stays. Remove it with `sudo`:

```bash
otto --lab example_lab host docker-host exec --sudo 'rm -rf /tmp/quickstart-docker'
```

It logs the command and prints nothing else when it succeeds.

## Next

- {doc}`../coverage` — coverage for your own products.
- {doc}`../writing-tests` — writing tests of your own.
- {doc}`../defining-products-and-tools` — products and tools of your own.
- {doc}`../../cli/cov/instrumenting/containers` — coverage from a
  `docker_image` product, and a product compiled in a builder container.
- {doc}`../../cli/docker/use-cases` — use-cases that several projects
  contribute to.
