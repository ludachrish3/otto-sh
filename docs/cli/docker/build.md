# otto docker build

Build the container images declared by the selected repos.

```text
otto docker build [USE_CASE [IMAGE]...] [--repo NAME] [--on HOST] [--rebuild]
                  [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Build only the repos taking part in this use-case (default: every selected repo) |
| `IMAGE...` (argument) | Image names to build (default: all declared images) |
| `--repo NAME` | Restrict to a single repo by name |
| `--on HOST` | Lab host id to build on |
| `--rebuild` | Force a rebuild even when a context-hash tag already exists |
| `--provide CAP=REPO` | Break a provider tie while narrowing to `USE_CASE`. Repeatable |

With a `USE_CASE`, `build` runs the **same** provider competition
{doc}`up` runs and builds only the winners' images — so the images that get
built are the ones a deployment would actually use, and a displaced mock's
image is not built for nothing. Bare `build` keeps its per-repo meaning across
every selected repo. See {doc}`use-cases` for the competition and for
`--provide`.

Builds are skipped when an image tagged with the current context hash already
exists — see {doc}`rebuild-policy` for exactly what counts as a change and how
to force one.
