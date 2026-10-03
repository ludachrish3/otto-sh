# The docker layer is thin and honest: docker's own names, flags and output — design

**Date:** 2026-10-02
**Source:** first external user feedback (otto 0.16.1), triaged in
`todo/user-feedback-design-items-2026-10-02.md` §1.
**Issues:** closes #495 (obsolete: the context hash is deleted); folds in #553
(`ps` host rule into the library). Refs #550 (compose staging in shared
`/tmp`), unchanged by this spec.
**Builds on:** `2026-09-30-docker-verbs-align-with-docker-design.md` (the verb
layout, the report types, the "adding a docker verb" recipe in its §7a).

## 1. Intent

Decisions (Chris, 2026-10-02):

- Otto has no handles of its own on docker things. Every name, tag and id a
  user sees is docker's. "Always let docker do what it is going to do", so a
  person who knows docker is never surprised.
- Otto's jurisdiction is convenience: it knows where files are, ships a build
  context (a directory or a user-provided tarball) and compose files to a
  docker host, and runs standard docker commands there.
- Cutting otto's default behaviour to get there is acceptable.
- Anything otto relays about a docker host is tested against what that
  host's daemon reports.

What the user reported, and what each turned out to be:

| report | cause |
| --- | --- |
| "hashes that map to no layer or container id" | otto's 16-hex build-context hash, used as the image **tag** and printed by `build` (`repo1-api:a7c8217f18991996`; the image id is `cbd8571d4b6e`) |
| "`--rebuild` uses cached layers" | `--rebuild` only bypasses otto's own skip check; `docker build` never gets `--no-cache` |
| "images get a `dock-` prefix" | the image is named `<repo name>-<image name>`; the declared name is never the image name |
| "want `logs`, `--tag`, thin wrappers for common commands" | the verb surface is `build`, `ps`, `use-cases`, `compose build/up/down` |

## 2. What this deletes

- `src/otto/docker/_context_hash.py`, the `<image>:<hash>` tag, the
  `docker image inspect` skip check, the `cached →` report line and the
  `Skipped` status on a build result.
- Otto's reading of `.dockerignore` (it existed only to compute the hash).
- `--rebuild` on `build` and `compose build`, and `rebuild=` on the library
  functions.
- `--no-build` on `compose up`.
- `[docker] registry_url`. Its only effect is prefixing the composed tag.
- The `<repo>-` prefix on image names (`_tag_base`).
- The implicit build before every `compose up`, in `deploy()` and in the
  per-repo `compose_up()`.
- `docs/cli/docker/rebuild-policy.md`. Its "persistent shell state" half
  moves to the docker index page.

No migration path and no deprecation alias: a removed flag or settings key
is refused by name, with the replacement in the message.

## 3. Declaring an image

```toml
[[docker.images]]
name = "api"                    # the image name, verbatim
dockerfile = "docker/Dockerfile"
context = "docker"              # a directory, or a tarball
```

- `name` is the image repository name docker sees. It may carry a registry
  and path (`registry.example/team/api`). It carries no tag.
- `context` is a directory or a tar archive (`.tar`, `.tar.gz`, `.tgz`,
  `.tar.bz2`, `.tar.xz`: what `docker build -` accepts). A directory is staged
  the way it is today. An archive is copied to the host unchanged and given to
  docker on stdin (`docker build … - < archive`). Otto never opens it.
- For an archive, `dockerfile` is a path **inside** the archive and is not
  checked locally; a wrong path is docker's error, relayed.
- `target` and `build_args` stay as they are: defaults a flag can extend.
- Two selected repos declaring the same `name` is a refusal by the build
  verbs (field `images`) that names both repos; `--repo` picks one. The old
  prefix hid that collision; nothing replaces it. It is a build-time rule,
  not a bootstrap one: a name clash must not stop unrelated otto commands.

## 4. Building

```text
otto docker build --on HOST [--repo NAME] [IMAGE...]
                  [-t/--tag REF]... [--no-cache] [--pull]
                  [--build-arg K=V]... [--target STAGE]
otto docker compose build [USE_CASE] [IMAGE...] [--on HOST] [--provide CAP=REPO]...
                  [--no-cache] [--pull] [--build-arg K=V]...
```

- Every flag after the selection has docker's name and docker's meaning, and
  is passed to `docker build` on the host.
- With no `--tag`, the image is tagged `<name>:latest`, exactly what
  `docker build -t <name>` produces. With `--tag`, the tags are the ones
  typed, as typed, and nothing is added. `--tag` with more than one selected
  image is a refusal (field `tag`): one reference cannot name two images.
  `compose build` has no `--tag`, as `docker compose build` has none.
- `--build-arg` adds to the declared `build_args`; a repeated key wins over
  the declaration. `--target` replaces the declared `target`.
- The build always runs. Docker's layer cache decides what is reused.
- Docker's own build output is relayed unmodified, the way any host
  command's output is. The report line for each image is read back from the
  daemon after the build:

  ```text
  repo1/api: built api:latest  cbd8571d4b6e  (test3)
  ```

  The references and the id are what `docker images` on the host prints for
  that image, the id in docker's own short form. Otto composes neither.

Library: `build_on(host, *, repo, images, tags, no_cache, pull, build_args,
target)` and `compose_build(use_case, *, on, provide, images, no_cache, pull,
build_args)`. `BuildReport` keeps its shape; each image entry becomes an
`ImageBuild` dataclass (`name`, `references: list[str]`, `image_id: str |
None`, `result: CommandResult`) in place of the bare `CommandResult`.

## 5. Compose

```text
otto docker compose up [USE_CASE [SERVICE...]] [--on HOST] [--build]
                  [--force-recreate] [--pull POLICY]
                  [--provide CAP=REPO]... [--env K=V]... [--env-file FILE]...
```

- `compose up` is `docker compose up -d`. It builds nothing on its own. A
  service whose image is missing gets docker's own error, relayed verbatim.
- `--build` runs §4 for the use-case's images first (default tags), then
  `up`. Library: `deploy(..., build: bool = False)`; `deployed()` forwards
  it. The per-repo `compose_up(..., build=False)` flips the same way.
- `--force-recreate` and `--pull POLICY` are passed to `docker compose up`.
- `compose build` and `compose down` are otherwise unchanged.

## 6. The verb surface

Passthrough already exists and is not duplicated: `otto host <HOST> exec
"docker …"` runs any docker command on a host. The docker index page says so
and shows it.

A curated verb exists only where otto adds something docker cannot do from
the host's shell: resolving a container **host id** or a **use-case** to the
real container or compose project, or fanning out over hosts.

| verb | resolves | docker flags carried |
| --- | --- | --- |
| `otto docker logs CONTAINER [--on HOST]` | a container host id (`test3.integration.web`), or a docker name/id with `--on` | `-f/--follow`, `--tail N`, `--since T`, `-t/--timestamps` |
| `otto docker compose logs [USE_CASE [SERVICE...]]` | use-case → project and host(s) | the same four |
| `otto docker compose ps [USE_CASE]` | use-case → project and host(s) | `-a/--all` |
| `otto docker images [--on HOST]` | every docker-capable host, or one | none |
| `otto docker ps [--on HOST]` | every docker-capable host, or one | `-a/--all`; host rule moves to the library (#553) |

Output rule: what docker printed is what otto prints. A verb that fans out
adds one header line per host and changes nothing inside it. No ids cut
shorter than docker printed them, no re-ordered columns, no otto-built table
over docker's data. `ps` gives up its Rich table for this.

Each verb follows the 2026-09-30 spec's §7a recipe: one library function, one
leaf, one `_Verb` row, one doc page.

Not in this spec: a registration seam for user verbs under `otto docker`
(top-level verbs via `register_cli_command` already exist); `exec`, `stop`,
`start`, `restart`, `rm`, `rmi`, `pull`, `inspect` (reach them through
`otto host <HOST> exec`; add one when it needs otto's resolution); netem on a
container.

## 7. Completion

Two sources, by where the truth lives.

**Declared names** (configuration; the existing `names` section, no TTL):
`IMAGE` on `build` / `compose build`, `SERVICE` on `compose up/down/logs`
(the services of the `USE_CASE` already on the line), `--repo`, and the
container host ids already synthesized for `otto host`.

**Observed docker state** (the daemon's; short-lived): image references,
image ids, container names and container ids, per host.

- Stored under a reserved top-level key of the completion cache,
  `__docker_observed__`, beside `__dynamic_tunnels__` and by the same
  pattern: keyed by host id, each entry stamped `observed_at`, with
  `DOCKER_OBSERVED_TTL_SECONDS = 900` (15 minutes).
- Written only as a by-product of a verb that already asked the daemon:
  `images`, `ps`, `compose ps`, `build`, `compose build`, `compose up`,
  `compose down`. A verb records what the daemon answered, replacing that
  host's entry.
- **A TAB never contacts a host.** It reads the cache, drops entries past the
  TTL and offers the rest. With nothing fresh it offers the declared names
  only. Dialling a lab host from a keypress would add seconds to a TAB, and
  would touch a host outside any reservation check.
- Consumers: `--tag` (references already on the `--on` host for the selected
  image), `logs CONTAINER` (host ids, then observed names and ids).
- Bounded: at most 200 references and 200 containers per host are stored, so
  a warm TAB stays one file read and O(1) in what a daemon holds.
- A completed id can be up to 15 minutes stale. That is a hint going stale,
  not otto asserting state: the command itself reports docker's answer.

The bash shim answers from this namespace the same way Typer's completer
does; the shim differential test pins the two against each other, TTL
included.

## 8. The honesty lane

One differential per verb, against a real daemon, in `tests/e2e/docker/`:

- run the verb through the CLI;
- collect every image reference, image id, container id, container name and
  state it printed (and every one in its report object);
- assert each against the daemon's own answer on that host (`docker image
  inspect`, `docker images --no-trunc`, `docker ps -a --no-trunc`).

A verb is not done until it has one. The lane also pins the negatives this
spec exists for: after `build`, no image on the host carries a tag otto was
not asked for; with `--no-cache`, the build output shows no `CACHED` step;
after `compose up` on a host missing the image, the error is docker's and no
build ran.

Unit tests cover parsing, refusals and report shapes as today. The
CLI-vs-library differential (#525) covers each new verb.

## 9. Errors and dry run

- Input refusals stay field-named (`DockerBuildError`), spelled in the
  command's flags at the one `usage_error_from` site: `tag` with several
  images; an archive `context` that does not exist; an unknown `IMAGE`.
- A removed flag is simply gone: `--rebuild` and `--no-build` are deleted
  outright and the parser rejects them like any unknown flag. No hidden
  option, no alias, no tailored refusal.
- A removed settings key is refused by name with its replacement
  (`registry_url` → put the registry in `name` or `--tag`).
- A docker failure is relayed whole, with the command that failed.
- Dry run: `build`, `compose build`, `compose up` and `compose down` keep
  their plan previews; the build plan lists the exact `docker build` command
  per image. `logs`, `images`, `ps` and `compose ps` contact a host, so they
  stop at the dry-run seam.

## 10. Documentation

- `docs/cli/docker/`: pages for `logs`, `images`, `compose/logs`,
  `compose/ps`; `build.md`, `compose/build.md`, `compose/up.md` and `ps.md`
  rewritten to the flags above; `rebuild-policy.md` deleted.
- `docs/cli/docker/index.md` opens with a **first deploy** section: the
  minimal `[[docker.images]]` + `[[docker.composes]]` + `[[docker.use_cases]]`
  block, then `use-cases` → `compose build -n` → `compose up -n` → the real
  run. This is the gap the link-only reader walk hit (design-items §3.1,
  point 7). The mounts material moves below it.
- The passthrough (`otto host <HOST> exec "docker …"`) is documented on the
  index page.
- `docs/configuration/settings.md`: the `[docker]` section loses
  `registry_url` and documents the archive `context`.
- `otto init`'s scaffold comments follow.

## 11. Work items

In order; each is its own issue and its own plan.

1. **Honest build** (§2, §3, §4, the §8 lane with its first differentials).
   The item the reporting user is waiting on.
2. **Compose semantics** (§5).
3. **Curated verbs** (§6), one verb per commit, each with its differential.
4. **Completion** (§7): declared names first, then the observed namespace
   and its writers.
5. **Docs** (§10): the first-deploy section and the passthrough note; the
   per-verb pages land with their verbs.

## 12. Out of scope (tracked)

- User-registered verbs under `otto docker`.
- Netem on a container or directly on a host (design-items §1.7, §4.3).
- Ad-hoc builds of an undeclared context (`--context PATH`): every build in
  this spec is of a declared `[[docker.images]]` entry.
- Container host ids are lower-cased but not slugged (design-items §1.1).
- #550, #364, #365.
