# otto docker build

Build the container images the selected repos declare, on one lab host.

```text
otto docker build [--parent HOST] [--repo NAME] [IMAGE...]
                  [-t/--tag REF]... [--no-cache] [--pull]
                  [--build-arg KEY=VALUE]... [--target STAGE]
```

`build` builds images and nothing else. Like `docker build`, it knows nothing
about a composition: the context of each `[[docker.images]]` entry is staged
onto the parent and `docker build` runs on that host's daemon; the parent is
chosen as {ref}`Which host <docker-which-host>` describes. To build the images a
deployment would use, run {doc}`compose/build` instead.

Every build runs. Otto keeps no record of earlier builds and skips nothing:
docker's own layer cache decides what a rebuild reuses, and `--no-cache`
turns it off.

## Selecting images

| Option | Description |
| ------ | ----------- |
| `--parent HOST` | The docker-capable lab host to build on (default: {ref}`Which host <docker-which-host>`) |
| `--repo NAME` | Restrict to a single repo by name |
| `IMAGE...` (argument) | Declared `[[docker.images]]` names to build (default: every declared image) |

`IMAGE` is the `name` of a `[[docker.images]]` entry in a repo's
`settings.toml`, never a file path. A name no selected repo declares is
refused; with several repos selected, each builds the subset it declares. A
repo that declares no images is reported and skipped; if no selected repo
declares any, the command refuses.

## Flags

Each flag below is `docker build`'s own, with docker's name and docker's
meaning, and is passed to `docker build` on the host. This table is the one
home for them; {doc}`compose/build` links here.

| Flag | Meaning |
| ---- | ------- |
| `-t`, `--tag REF` | As `docker build`'s `-t`. Repeatable; see {ref}`docker-build-naming` |
| `--no-cache` | As `docker build`'s `--no-cache` |
| `--pull` | As `docker build`'s `--pull` |
| `--build-arg KEY=VALUE` | As `docker build`'s `--build-arg`. Repeatable; added to the entry's declared `build_args`, and a repeated key wins over the declaration |
| `--target STAGE` | As `docker build`'s `--target`; replaces the entry's declared `target` |

`--tag <TAB>` offers the references the parent's daemon listed in the last
day.

A flag docker does not have is not here, and a flag otto once had that
docker does not is gone; the parser rejects it like any unknown flag.

(docker-build-naming)=
## Naming

The `name` of a `[[docker.images]]` entry is the image name, exactly as
docker sees it. Otto adds no prefix and no suffix. It may carry a registry and
a path (`name = "ghcr.io/me/api"`); a tag in it (`api:1.0`) is refused when the
settings load, so put the tag in `--tag`.

- With no `--tag`, the image is tagged `<name>:latest`, which is what
  `docker build -t <name>` produces.
- With `--tag`, the tags are the ones typed, as typed, and nothing else is
  added.

Every name, tag and image id otto prints is one docker prints: nothing is
composed or abbreviated by otto.

## The report

After each image builds, otto asks the host's daemon what it holds for that
image and prints one line:

```text
repo1/repo1-api: built repo1-api:latest  cbd8571d4b6e  (test3)
```

That is `<repo>/<image>: built <references>  <id>  (<host>)`. The references
are the ones this build was asked to tag (`<name>:latest`, or the `--tag`
values), written the way `docker images` on the host lists them; another tag
the daemon holds on the same image is not printed. The id is the one
`docker images` lists for that image, in docker's own short form. A build that
succeeds but that the daemon does not list is reported as a failure. A failed
image prints `FAILED` and docker's output, and the exit code is 1 when any
image failed. Docker's own build output is relayed unmodified, as any host
command's output is.

## Refusals

Two selections are refused before anything runs, each naming the field at
fault:

- **`--tag` with several images.** One reference cannot name two images, so a
  selection of more than one image with `--tag` is refused. Name the one image
  to tag.
- **One image name declared twice.** When two selected repos declare the same
  `name`, the name docker would see is ambiguous, so the build is refused and
  both repos are named. Narrow the selection with `--repo`, or rename one
  declaration.

Two further refusals concern a single repo's own declarations: two of its
images whose names would share one staging directory, and an archive
`context` that is not a file.

## Tarball contexts

A `context` may be a tar archive instead of a directory: a path ending in
`.tar`, `.tar.gz`, `.tgz`, `.tar.bz2` or `.tar.xz`.

```toml
[[docker.images]]
name = "api"
context = "build/api-src.tar.gz"
dockerfile = "docker/Dockerfile"       # a path INSIDE the archive
```

Otto copies the archive to the host unopened and gives it to docker on stdin
(`docker build ... - < archive`); it never reads inside it. `dockerfile` is
then a path inside the archive and is not checked locally, so a wrong path is
docker's error, relayed.

## Dry run

Under `--dry-run` (see {doc}`../dry-run`) the verb prints its plan and builds
nothing. For each image the plan names the exact command a real run would
execute, as `on <host>: docker build ...`, and ends with "No context was
staged and no image was built."

Library: {func}`~otto.docker.build_on`.
