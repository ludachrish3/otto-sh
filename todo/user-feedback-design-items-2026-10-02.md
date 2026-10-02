# User feedback 2026-10-02: items that need a design

Source: `todo/20261002-user-feedback.md` (first external user, otto 0.16.1).
Every claim in the feedback was checked against the code at `d891bcbe`. The
simple items were triaged separately and are not repeated here. This file
holds only the items that need a decision before anyone writes code.

Priority order: docker (an interested user is waiting) → data-driven project
setup → docs structure → the small standalone decisions.

A finding that cuts across all of it: three things the user asked for
**already existed in 0.16.1** and were not found — data-only products
(`[[products]]`), multi-file lab data (glob entries in `paths`), and `-I`/`-E`
completion. That is evidence for the docs item, and a reason not to build
anything in sections 2 and 4 before checking what is already there.

---

## 1. Docker: a thin and honest layer (P0)

Principle (Chris, 2026-10-02): the docker layer stays as thin and as honest
as possible. Anything otto relays about a docker host must be tested against
what that host's daemon actually reports.

### 1.1 Identifiers otto prints that docker does not know

Verified on a real daemon (dev VM, docker 29.1.3):

```
REPOSITORY   TAG                IMAGE ID
repo1-api    a7c8217f18991996   cbd8571d4b6e
repo1-api    latest             cbd8571d4b6e
```

- `a7c8217f18991996` is otto's **context hash** (sha256 of Dockerfile, context
  files, build args, target; `src/otto/docker/_context_hash.py`), truncated to
  16 hex and used as the image **tag** (`src/otto/docker/build.py:40-42`).
  `otto docker build` prints `built → repo1-api:a7c8217f18991996`. It looks
  like a docker id and matches no image id, layer digest or container id.
  This is almost certainly the "hashes that map to nothing" report.
- The image **name** is `<repo name>-<image name>` (`build.py:26-37`), so
  `name = "api"` never produces an image called `api`. No literal `dock-`
  exists in the source at HEAD or at v0.16.1; the reported `dock-` prefix is
  most likely the user's repo (or lab) name. To confirm with the user.
- Compose projects are `<lab>-<usecase>-<user>` (use-case path) or
  `otto-<repo>-<user>` (legacy per-repo path) (`src/otto/docker/compose.py:67-113`),
  so compose names containers `<project>-<service>-1`.
- The ids otto prints for containers are real: `docker ps -q` output
  (`compose.py:219-262`), shown as `[:12]`.
- Container **host ids** are `<parent>.<project>.<service>`, lower-cased but
  not slugged (`src/otto/host/docker_host.py:178`).

Decisions:

1. Where does the context hash live? Options: (a) an image **label**
   (`sh.otto.context-hash=<hex>`), with the skip check reading the label and
   the report printing the real image id; (b) keep it as a tag but make it
   self-describing (`ctx-<hex>`); (c) drop otto's skip and rely on docker's
   layer cache (costs a context re-stage on every build). Recommendation: (a).
2. Image naming: the declared `name` is the image name, verbatim. A
   cross-repo collision becomes a loud refusal rather than a silent prefix.
3. What a build line reports: `<name>:<tag>  <image id>`, both read back from
   the daemon after the build, never composed locally.

### 1.2 `--rebuild` does not rebuild (confirmed bug)

`--rebuild` only bypasses otto's context-hash skip. The `docker build`
command line never gets `--no-cache` (`build.py:128-141`), so docker's layer
cache still answers. The flag name promises something docker does not do.

Decision: mirror docker's own flags. `--no-cache` (passed through, and
implies "do not skip"), `--pull` (passed through). Whether a separate "run
docker build even if the context hash matches" flag survives depends on 1.1.

### 1.3 The verb surface

Asked for: `otto docker logs <container>`, `--tag` on build, and thin
wrappers for the common docker commands with their common options.

Today: `build`, `ps`, `use-cases`, `compose build|up|down`
(`src/otto/cli/docker.py:731-738`). The cost of a verb is fixed by the
recipe in `docs/superpowers/specs/2026-09-30-docker-verbs-align-with-docker-design.md` §7a.

Known gaps in the existing verbs: `otto docker ps` is `docker ps` over the
whole host (not scoped to otto's stacks, no `-a`, id truncated by otto;
`compose.py:881-920`, #553); there is no `compose ps`, `compose logs`,
`images`, `logs`, `exec`, `stop/start/restart`, `rm`, `rmi`, `pull`,
`inspect`.

Decisions:

1. Shape of "thin". Options: (A) one hand-written wrapper per verb, mirroring
   its top options; (B) a generic passthrough, `otto docker --on H -- <docker
   args>`, output relayed verbatim; (C) both: passthrough for everything,
   curated verbs only where otto adds something docker cannot (resolving a
   container **host id** or a **use-case** to the real container/project,
   fanning out over hosts). Recommendation: (C). A curated verb that adds
   nothing is surface to keep honest for no gain.
2. Which curated verbs first. Proposed: `logs`, `compose logs`, `compose ps`,
   `images`, `exec` (or defer `exec` to `otto host <container-id> exec`,
   which already exists).
3. `--tag`: an extra `-t` on build (docker's meaning), on top of 1.1's
   naming. What `:latest` means once a user names a tag.
4. Output: relayed docker output is printed as docker printed it; otto adds a
   host column or header only when fanning out.

### 1.4 User-defined docker verbs

Question from the feedback: can `@cli_exposed` add to first-party commands?
No. It marks host-class methods and is read only by the `otto host` group
(`src/otto/cli/expose.py`). Top-level verbs are supported
(`register_cli_command` / `@cli_command`), but there is no supported way to
add a leaf under `otto docker`.

Decision: whether `_VERBS` becomes a public registration seam
(`register_docker_verb`), or whether user verbs stay top-level.

### 1.5 The honesty lane

No test compares otto's docker output with the daemon. Proposed: a lane that
runs each verb against a real daemon (the dev VM has one) and asserts every
identifier and state otto printed against `docker inspect` / `docker image
inspect` / `docker ps -a` on that host. One differential per verb; a new verb
is not done until it has one.

### 1.6 Related open issues to fold in

#495 (context hash matches `.dockerignore` with fnmatch → stale image
reported cached), #496 (displacement printed twice), #553 (`ps` host rule
into the library), #550 (compose staging in shared `/tmp` leaks `otto.env`),
#364, #365 (`docker_image` product kind).

### 1.7 Netem on a container (feature request, lower priority)

Impairment is link-scoped only (`src/otto/link/manage.py`); there is no
`host.impair()`. The netem impairer serves the `unix` family only, and
`DockerContainerHost` has no impairer. Decide whether a container's veth is a
link endpoint, and whether a host-scoped "impair everything on this netdev"
verb should exist at all.

---

## 2. Data-driven project setup: products (P1)

### 2.1 What exists, and what is actually missing

Exists (0.16.1 too): `[[products]]` / `[[dev_tools]]` in `settings.toml`
with `name`, `kind`, `match`, and per-kind params; kinds `shell`,
`docker_image`, `kmod`, `embedded` (`src/otto/models/settings.py:850-887`,
`src/otto/declared.py`, `src/otto/host/shell_kind.py`). No Python needed.
The getting-started page leads with a five-method `Product` subclass and
mentions the data route in its last paragraph, which is why it was missed.

Missing:

- **No public base to override one method.** `Product` has four abstract
  methods; `ShellProduct` leaves three abstract. The class with sane
  defaults, `DeclaredShell`, is internal.
- **No "defined once" rule.** Today config wins and code fills gaps: a
  provider product whose name a declared entry already holds is skipped with
  a `logger.debug` (`src/otto/host/product.py:603-623`, `:672-679`).
- **A TOML entry cannot name a user class.** `kind` must be a kind registered
  from an `init` module.
- **`[project]` becomes required** as soon as a repo declares a product, and
  that is explained on a different page.

Decisions:

1. The one-definition rule: a product name is defined in data **or** in code.
   Both → refusal naming both sites (not a silent skip). Or: code may
   *refine* a declared entry. The feedback leans to "skip data registration
   when code defines it"; the honest version of that is a refusal or an
   explicit `class = "pkg.mod:Class"` key on the entry.
2. A public default-behaviour base (working name `DeclaredProduct`), so a
   custom product is "subclass, override `install`".
3. Programmatic definition for version permutations: providers already do
   this; it needs a documented pattern (the Advanced section, 3.2).

### 2.2 Debug vs field

The root `--field/--debug` flag is **dead**: parsed, documented
(`docs/cli/index.md:29,96`), and consumed by nothing
(`src/otto/cli/main.py:539-546`, `# noqa: ARG001`; `OttoEnvSettings.field_*`
have no readers). Products have no variant notion.

Decision: give products a variant (per-entry `variant = "debug" | "field"`,
or per-variant param tables) selected by the root flag, or delete the flag.
A documented flag that does nothing cannot stay.

### 2.3 `--list-products` / `--list-tools`

Nothing lists products or dev tools. The useful answer is per host: which
products resolved, from which repo, declared or provider, which kind, and
(after 2.2) which variant, plus entries that matched no host. Shape follows
2.1 and 2.2, so it is designed with them. Root flag vs. `otto inventory`
subcommand is open.

---

## 3. Docs structure and findability (P1)

### 3.1 The naive-reader walk (in progress)

A lower-tier agent is given a from-scratch setup goal and may only open
pages reachable by links/toctrees from `docs/index.rst`: no grep, no
listing, no source. It reports its trail, wasted reads and backtracks. The
restructuring proposal is written against that trail, and the walk is
re-run after the restructure as the acceptance test.

### 3.2 Known structural problems (before the walk)

- The worked example in Getting Started is a different project from the
  `otto init` scaffold the reader just made; only the last two pages return
  to the scaffold.
- Install appears twice, and `installation` sits after Getting Started.
- `overview.md` "Where to start" does not point at Getting Started.
- Docker setup lives only under the CLI reference and settings reference.
- No "Advanced usage" section. Requested: a new top-level section, split
  liberally, starting with custom products and their registration (2.1).
- Cookbook: already four groups, but "Recipes" is a flat list of seven and
  "Extending otto" is a flat list of eleven. Requested groups include
  `asyncio` and host management.

Decisions: the top-level section list and order; what moves from Cookbook to
Advanced; one linear from-scratch path on one project; URL redirects (#374).

---

## 4. Small standalone decisions

### 4.1 Host id delimiter

The feedback's premise is wrong: element and board both go through `slug()`
(kebab). The `_` is the single structural delimiter between them
(`element_board<slot>`, `src/otto/host/remote_host.py:89-101`), chosen so
`a-b` (element) and `a` + `b` (element + board) cannot collide. Changing it
re-maps every host id, link handle, tunnel sentinel and user preference
regex. Decide: keep and explain it where ids are first shown, or pick a
different unambiguous delimiter. Separately, container host ids are not
slugged at all (1.1).

### 4.2 Fuzzy tab completion

Every completion source filters by prefix, in the shim and in Typer. Needs a
definition (substring? subsequence? which shells?) and a check that it stays
O(1) on a warm TAB before it is worth a design.

### 4.3 Netem directly on a host

See 1.7; same decision, not docker-specific.
