# otto docker build

Build the container images the selected repos declare, on one lab host.

```text
otto docker build --on HOST [--repo NAME] [IMAGE...] [--rebuild]
```

| Option | Description |
| ------ | ----------- |
| `--on HOST` | The docker-capable lab host to build on. Required |
| `--repo NAME` | Restrict to a single repo by name |
| `IMAGE...` (argument) | Declared `[[docker.images]]` names to build (default: every declared image) |
| `--rebuild` | Force a rebuild even when a context-hash tag already exists |

`build` builds images and nothing else. Like `docker build`, it knows nothing
about a composition: the context of each `[[docker.images]]` entry is staged
onto `HOST` and `docker build` runs on that host's daemon, tagging the result
`<repo>-<name>:<context-hash>` with `:latest` re-pointed at it. Because an image has
to land on the daemon that will run it, the host is the one thing this verb
needs, so `--on` is required. To build the images a deployment would use, on
the hosts it would use, run {doc}`compose/build` instead.

`IMAGE` is the `name` of a `[[docker.images]]` entry in a repo's
`settings.toml`, never a file path or a registry name. A name no selected
repo declares is refused; with several repos selected, each builds the
subset it declares. A repo that declares no images is reported and skipped;
if no selected repo declares any, the command refuses.

Every line of output is one image: `cached → <tag>`, `built → <tag>`, or
`FAILED` with the build output. The exit code is 1 when any image failed.
Library: {func}`~otto.docker.build_on`.

Builds are skipped when an image tagged with the current context hash already
exists; see {doc}`rebuild-policy`.
