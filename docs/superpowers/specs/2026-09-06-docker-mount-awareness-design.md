# Docker mount awareness — container hosts know their parent-side paths

**Date:** 2026-09-06
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** nothing unlanded. Builds on the shipped `otto.docker`
use-cases pipeline (`c4b5df95`), `DockerContainerHost` registration in
`otto.docker.compose.register_stack_hosts`, and the compose staging layer
in `otto.docker.staging`.

## 1. Goal

A `DockerContainerHost` should know which of its directories are shared
with the machine running the docker daemon, so library and test code can
translate a path from one side to the other:

```python
ctr = get_container_host("test3.integration.api")
parent_log = ctr.parent_path("/var/log/app/run.log")   # -> PosixPath on test3
await ctr.parent.get([parent_log], local_dir)          # collect it
```

The motivating workflow is artifact collection and data seeding: a test
knows a path *inside* the container and needs the corresponding path on
the parent, where otto already has a fully capable `Host`.

### In scope

- A `Mount` record and a `mounts` field on `DockerContainerHost` (§3).
- Path translation in both directions (§4).
- Population from `docker inspect` at registration (§5).
- One new warning: a **relative** bind source in a rendered compose file,
  which otto's staging relocates into a directory it wipes (§6).
- Testing (§7) and documentation (§8).

### Out of scope

- **A declared `mounts = {...}` in `settings.toml`.** Rejected in design:
  the compose file already holds this truth, a second copy drifts the
  first time an adapter rewrites a path, and otto would be asserting
  identity it did not derive. Every input here is derived from the
  product's own compose file or from docker.
- **Pre-`up` existence checks on absolute bind sources.** Decided
  against (§6): docker's auto-create is documented behaviour many
  products rely on, so the warning would fire on correct configurations.
  Documented instead (§8).
- **Routing `put`/`get` through a mount.** `parent_path()` makes that
  shortcut possible, but transfer keeps going through `docker cp`
  unchanged. Folding it in here would ship a behaviour change to a path
  that currently works; it is independently arguable later.
- No `settings.toml` change, therefore **no `SCHEMA_VERSION` bump**.

## 2. Why not build-time or declaration

A host directory cannot be bound in a Dockerfile — by design, since it
would make the image non-portable. `VOLUME /data` declares only an
anonymous volume and names no host path. Bind mounts are a runtime
concern, and for a declared stack their home is the compose file's
per-service `volumes:` list. That is where products already put them,
and where otto's adapter can already rewrite them per lab.

This keeps otto on the right side of the principle the use-cases design
established: products own the deploy surface. otto reads; it does not
declare.

## 3. The `Mount` record

New module `src/otto/host/mount.py` — `docker_host.py` is already 1089
lines and this is a separable concern.

```python
@dataclass(frozen=True)
class Mount:
    container_path: Path   # where it appears inside the container
    parent_path: Path      # absolute path on the PARENT host
    kind: str              # "bind" | "volume"
    name: str | None = None    # named volume's name; None for a bind
    read_only: bool = False    # the CONTAINER's view; parent side unaffected
```

`Path`, not `PurePosixPath`, despite both sides being remote POSIX paths.
It is what the codebase already uses for parent-side paths
(`staging.project_root` returns one) and what `Host.get`/`Host.put`
annotate (`list[Path] | Path`). `PurePosixPath` is not a `Path` subclass,
so the semantically tidier choice would force a conversion at every
transfer call site — the exact calls this feature exists to make easy.

`parent_path`, not `host_path`: in otto a container **is** a host, so
"host path" is ambiguous exactly where precision matters. `parent` is the
word `DockerContainerHost.parent` already uses for the daemon-side
machine.

On `DockerContainerHost`:

```python
mounts: list[Mount] = field(default_factory=list, repr=False)
```

A plain list, never `list | None`. The tri-state (never-inspected vs.
genuinely-unmounted) was considered and rejected: no ordinary caller
behaves differently between the two, so the distinction would tax every
call site to serve one error path. That error path gets the distinction
from §4's message instead.

`read_only` records the container's view only. A read-only mount is fully
writable from the parent, which is precisely what makes the parent side
useful for seeding.

## 4. Translation

```python
def parent_path(self, container_path: str | Path) -> Path
def container_path(self, parent_path: str | Path) -> Path
def mount_for(self, container_path: str | Path) -> Mount | None
```

Both translators match by **longest prefix** and carry the remainder
through, so a mount of `/var/lib/app` answers for
`/var/lib/app/logs/x.txt`. Longest-prefix, not first-match: compose
permits one mount nested inside another, and first-match would answer
from the outer mount and name a parent path where the file does not
exist.

Prefix matching is **path-component-wise**, not string-wise: a mount at
`/var/lib/app` must not claim `/var/lib/application/x`.

The translators raise; `mount_for` returns `None`. A caller asking for a
translation has already asserted the path is shared, and returning
`None` there would let a wrong path flow onward silently. `mount_for` is
the predicate for callers that are genuinely asking.

The failure message carries the honesty the type no longer does. With a
non-empty `mounts`, it names the mounts that were checked. With an empty
`mounts` it branches on `container_id`: an empty id means the container
is not up and mounts are inspected at bring-up; otherwise it points at
the bring-up warning that §5's inspect failure would have logged.

## 5. Population

One batched call in `register_stack_hosts`, after the container ids
resolve — the function is already async and already spends one exec per
service:

```text
docker inspect --format '{{.Id}}\t{{json .Mounts}}' <cid1> <cid2> ...
```

Verified against docker 29.1.3 on the dev VM. Findings that shape the
parser, each of which would otherwise have been a defect:

1. **Output is one line per container, in argument order**, with the full
   64-character id. A container with no mounts prints an empty JSON array.
2. **A missing container makes the whole command exit 1, but the good
   lines are still printed.** Parsing must therefore NOT be gated on
   `result.status.is_ok` — one container reaped between `ps -q` and
   `inspect` would otherwise discard the entire stack's mapping. Parse
   the lines that came back, key them by id, and warn naming the ids that
   produced none.
3. **otto merges stderr into stdout** (`asyncssh.STDOUT` in
   `host/session.py`, `subprocess.STDOUT` in `local_host.py`), so
   `error: no such object: X` arrives interleaved in the same stream.
   The parser accepts only lines shaping as `<64 hex>\t<JSON array>` and
   ignores everything else, rather than assuming a clean stream.
4. **Field names are `Type`, `Source`, `Destination`, `RW`** — so
   `read_only = not RW`. There is no `ReadOnly` key. Named volumes carry
   `Name` and `Driver`; binds do not.
5. **tmpfs handling is form-dependent.** Compose's short `tmpfs:` key
   does not appear in `.Mounts` at all, but the long form
   (`type: tmpfs`) does, with `Source: ""`. So a filter IS required, and
   the robust rule is **drop any entry with an empty `Source`** — that is
   the actual disqualifier (no parent-side path), and it covers both
   forms without enumerating types.

Named volumes are kept, with `kind="volume"` and their
`/var/lib/docker/volumes/<name>/_data` source. They are genuinely
parent-visible, usually root-owned, and `kind` lets a caller decide what
to do about that rather than otto deciding for them.

A dry-run decline is refused via `refuse_declined_fact`, matching
`otto.docker.build._image_exists`. Placeholders from
`register_declared_container_hosts` are untouched: they keep an empty
`mounts` beside their empty `container_id`. A container restart changes
its id, which already forces re-registration through the convergent
`up`, so the mapping re-derives with it and cannot go stale.

## 6. The relative bind source warning

`./data:/var/lib/app` resolves against the compose file's directory. otto
relocates that directory: `stage_compose_files` copies each compose file
into `<project>/compose/<idx>/`, a tree it `rm -rf`s and recreates on
**every** stage. A product author writing the most natural thing in
compose therefore gets a mount whose contents are destroyed on the next
deploy, with nothing in their compose file hinting at it.

otto warns, naming the resolved staging path and saying it is wiped per
stage. This is pure text analysis of the rendered compose file — no exec
and no device state to reason about. It does not fire on a `--dry-run`,
because both staging entry points refuse at the top under `is_dry_run()`
and the use-case deploy path skips `stage_use_case` entirely; surfacing it
there would take a separate call site in `deployment.py`, and the
advisory's value is at real deploy time.

The walk is **advisory all the way down**: a text it cannot parse produces
no warning AND no error. `stage_compose_files` ships repo-authored compose
files verbatim and has never parsed them, so letting this turn an
unreadable compose file into a staging refusal would be a behaviour change
smuggled in behind a warning — the more so because `compose_down` wraps
that staging call in `except RuntimeError` precisely to keep a staging
failure from replacing the real exception during teardown.

Implementation is a `_relative_bind_sources` sibling to
`staging._collect_env_file_refs`, reusing its YAML handling and
`_resolution_error`. The parse contract:

- Short string form (`src:dst`, `src:dst:ro`) and long mapping form
  (`type: bind`, `source:`, `target:`).
- **Only sources beginning `./` or `../` are warned about.** This follows
  compose-go's own `isFilePath`, which keys a bind on a `.`, `/` or `~`
  prefix: a bare first field (`appvol:/data`) is a named volume, an
  absolute source is the author's own business (see below), and a
  `~`-relative source resolves against the daemon user's home rather than
  the relocated compose directory. Only the `./`-relative form is the one
  otto's staging moves out from under the author.
- **Any source containing `$` is skipped.** Compose interpolates from
  the env file at `up` time, not at render time, so a static walk
  genuinely cannot resolve `${DATA_DIR}/data`. otto must not warn about a
  path it has not evaluated.

Absolute sources that do not exist on the parent are deliberately
**silent** — see §1 out-of-scope and §8.

## 7. Testing

Unit, against a mocked parent:

- Translation: exact match, sub-path carry-through, longest-prefix with
  one mount nested in another, the component-wise boundary case
  (`/var/lib/app` vs `/var/lib/application/x`), no-match raise, and the
  empty-`mounts` message naming the not-up case.
- Inspect parsing: bind, named volume, empty array, multi-container
  output keyed by id, the empty-`Source` filter, interleaved stderr
  lines, and the partial-failure case from §5.2 — exit 1 with good lines
  present must still populate the containers that answered.
- The compose walk: both volume forms, named-vs-bind, the `$` skip,
  malformed YAML.

Per standing practice, the translation and parse tests each get a
**mutate-and-observe-red** demonstration. Longest-prefix in particular is
logic a test can appear to cover while passing against a broken
implementation — a first-match bug passes every single-mount case.

An e2e on the bed brings up a stack with a real bind mount, asserts
`mounts`, then writes a file inside the container and reads it back
through `parent.get()` at the translated path — the §1 workflow, proven
end to end. Bed docker lanes lease test3.

## 8. Documentation

One new section in the docker guide, linked from the container-hosts
page rather than restated there:

- What `mounts` is, where it comes from, and that it is empty until the
  stack is up.
- The translation helpers, with the artifact-collection example from §1.
- The relative-source trap (§6) and its warning.
- Docker's **auto-create** behaviour: a missing absolute bind source is
  not an error — docker creates it `root:root` and empty, so a service
  that comes up against an unexpectedly empty directory is usually a
  missing directory on the parent, not a broken image. The permissions
  variant is worth naming too: an image running as a non-root `USER`
  against a root-owned auto-created directory fails on write, and the
  failure points nowhere near the mount.

## 9. Open questions

None. All design questions were resolved in the 2026-09-06 session:
inspect-only population, a plain `list[Mount]`, and silence on missing
absolute bind sources.
