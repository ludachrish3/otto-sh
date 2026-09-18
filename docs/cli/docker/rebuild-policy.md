# Image rebuild policy

When `otto docker build` decides to rebuild, and what container shell state
survives between calls.

## Image rebuild policy

Each image is tagged with a hash of:

- Dockerfile bytes
- Every file in the build context (after `.dockerignore`)
- Build args
- Multi-stage target (if any)

`docker image inspect <tag>:<hash>` is consulted before every build. A
match short-circuits the build; `--rebuild` forces it.

Every build also tags the image `:latest`, and a short-circuited build
re-points `:latest` at the cached digest — that is the tag your `compose.yml`
should name. If the re-tag fails, the image is reported as a build **failure**
rather than as cached: `:latest` would otherwise still resolve to a previous
build, and the stack would come up on the wrong image with nothing to say so.

## Persistent shell state

`run()` preserves shell state (`cd`, environment variables, shell
variables) across separate calls — same as `LocalHost` and `UnixHost`:

```python
await api.run(["cd /tmp", "pwd"])  # prints /tmp
await api.run("export FOO=bar")
await api.run("echo $FOO")  # prints bar
```

`exec()` is the stateless, concurrent-safe counterpart — each call
spawns a fresh `docker exec` against the parent. Use `exec()` when
you want to fan out independent commands; use `run()` when you need
stateful or interactive flows.
