# otto docker — stacks on lab hosts

`otto docker` builds images and orchestrates compose stacks **on
docker-capable lab hosts**, not on the machine otto runs on. Everything
rides the host subsystem: builds and compose commands execute over the
parent host's existing SSH connection (hops included), which is the
parent-delegation design covered in
{doc}`../subsystems/docker-hosts`.

## The compose lifecycle

Container hosts have a two-stage life:

1. **Declared.** At lab load, every service declared in a repo's `[docker]`
   settings is registered as a *placeholder*
   {class}`~otto.host.docker_host.DockerContainerHost` (id
   `<parent>.<project>.<service>`). Placeholders make `--list-hosts` and
   completion work before anything is running, and turn "no such host" into
   a precise "run `otto docker up` first."
2. **Live.** `compose up` resolves real container ids and overwrites the
   placeholders; `compose down` closes each container host *before* its
   parent's connection (sessions must drain while the transport is alive —
   the ordering rule from {doc}`index`) and restores the placeholders.
   `hosts.json` is never written back — lab data stays read-only.

## What is unique about `docker`

- **Content-addressed build skipping.** Images are tagged with a hash of the
  Dockerfile, build context (after `.dockerignore`), build args, and target
  stage; `docker image inspect` on the tag short-circuits a rebuild, and the
  hash is computed on the otto side so it stays correct across parents.
  `--rebuild` forces.
- **No reservation gate of its own** (`gate=False`): a container has no
  independent reservation — the parent host's reservation transitively
  covers its containers, so gating the parent is the whole story.

## `otto docker --help`

```{raw} html
:file: ../../_static/generated/termynal/help-docker.html
```
