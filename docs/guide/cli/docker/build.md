# otto docker build

Build the container images declared by the selected repos, on every
docker-capable lab host (or just the one named by `--on`).

```text
otto docker build [--repo NAME] [--on HOST] [--rebuild] [<IMAGE>...]
```

| Option | Description |
| ------ | ----------- |
| `--repo NAME` | Restrict to a single repo by name |
| `--on HOST` | Lab host id to build on (default: all docker-capable hosts) |
| `--rebuild` | Force a rebuild even when a context-hash tag already exists |
| `<IMAGE>...` | Image names to build (default: all declared images) |

Builds are skipped when an image tagged with the current context hash already
exists — see {doc}`rebuild-policy` for exactly what counts as a change and how
to force one.
