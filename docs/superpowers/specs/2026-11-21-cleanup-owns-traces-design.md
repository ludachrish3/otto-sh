# Cleanup owns the traces otto placed: #836, #837, #838

**Status:** revision 4, 2026-11-21.

r4 applies the third Astra and Fable checks: a failed home discovery is a failure and never "nothing staged"; the question confirms absence without following a final symlink; kept directories are reported without classifying why; a container host's removal runs as root; one function names the staged path; the project layer dispatches the step.

r3: revision 3, 2026-11-21.

It followed the second Astra and Fable reviews: it defines which entries the removal step covers, makes directory removal opportunistic (never a failure, never asked by `is_clean`), states the cost of removing a file by its declared path, and corrects the claims about elevation, the plan's override check and the tests that go.

r2 followed the first reviews and two further rulings: it dropped the tarball-manifest route and the per-kind hook, kept the docker staging root, and made one owner-scoped removal step with one matching question.

## Why

The maintainer's principle (2026-11-04, from the #777 batch): cleanup removes every tool and trace that otto placed on a host during install and execution. Uninstall only exits or unloads a product, and leaves tools and data on the host.

Three places still deviate:

- **#836:** a `docker_image` product loaded from a tarball removes its image in `uninstall`.
- **#837:** cleanup leaves a `shell` product's staged artifact and every stage directory, and `is_clean` asks about neither.
- **#838:** cleanup leaves the docker staging root, toolchain destination directories and images otto pulled or built.

## Rulings (Chris, 2026-11-21)

1. **No record of what otto created.** otto keeps no manifest, marker or stored state about what it made on a host.
2. **otto removes no product image.** Not one it pulled, built or loaded from a tarball, and not one that was there already. (otto's own coverage-scan snapshots, which it labels `otto.scan` and sweeps itself, are not product images and are unchanged, as are the user-invoked `otto docker image rm` and `prune`.) A user who wants an image gone removes it with `docker image rm`. (The pulled-image ruling came first; the tarball case followed after review showed that a tarball names an image without showing that otto placed it, that the tarball is often gone when cleanup runs, and that another product may share the image.)
3. **The docker staging root stays.** Sessions that share a login share it, otto creates it with mode 0700 and refuses one with any other mode, so removing it while another session is staging would break that session's host.
4. **The examples' `uninstall` strings stay as they are.** For a tool that is only a file, removing the file is the only thing its own `uninstall` command can mean.
5. One pull request for all three issues.

## Goals

- `uninstall` on every kind only stops, exits or unloads.
- After `cleanup`, no file otto staged remains, and a declared stage directory that is empty is gone when `rmdir` accepts it.
- `is_clean` asks the question that matches the new removal.
- The dry-run plan of a cleanup names every removal it would make.
- The docs say, in one place, what cleanup leaves behind and why.

## Non-goals

- Removing any image.
- Removing the docker staging root, or a toolchain tool's destination directory.
- Tracking which directories otto created.
- Changing what a user's own `uninstall` command does.
- Compose stacks at cleanup (#839).
- The root-owned bind-mount parent a `docker_image` product's `cov_dir` leaves (noted in the #777 spec); it is listed under what cleanup leaves behind.

## Anti-goals

- Cleanup removing anything that holds content otto did not place.
- A second teardown path beside `Host.cleanup`, `ProjectActions.cleanup` and the orchestrator's `cleanup`.
- Plain `uninstall` or `uninstall-tools` removing a file.

## Approaches considered

**A. One stateless removal step (chosen).** Cleanup removes what otto can name from the declaration alone: the staged file at `<stage_dir>/<artifact basename>`, and a declared stage directory only when `rmdir` accepts it. Nothing is stored anywhere.

**B. A placement record on each host.** Exact about who created what, but a second mechanism to keep correct, itself a trace, and lost when it lives under `/tmp`. Ruled out by ruling 1.

**C. A per-kind hook called after each entry's uninstall (r1).** Rejected in review: the cleanup entry points do not walk entries (the walks live in `Host.uninstall` and `Host.uninstall_dev_tools`, which the plain verbs share), a stage directory that holds a `cov_dir` is not empty until the later coverage sweep, and a project's `uninstall` override would skip it.

**D. Naming a tarball's image from its manifest (r1).** Rejected in review and by ruling 2.

## Design

### 1. `uninstall` of a tarball-loaded `docker_image` product (#836)

`uninstall` runs `docker rm -f -v <container>` and nothing else, for a tarball product exactly as for a reference product. The container inspect that existed only to find the image to remove goes. `loaded_ref` stops being an attribute of the product: install still reads the reference `docker load` printed, as a local.

The product's plan shows the one command and drops the "image to remove" gap.

Nothing else in `src/` depends on the image going: `is_installed` asks whether the container runs, and `ensure_installed`'s partial recovery stages and loads again, where a second `docker load` of the same tarball is a no-op (a test pins it). A rebuilt tarball loaded over an old one leaves the old image untagged on the host; the docs name `docker image prune` for it.

### 2. One removal step for staged files (#837)

A new pair on the host, modelled on `remove_coverage_dirs` / `coverage_dirs_clean` and scoped by `owner` the same way:

- `remove_staged_files(*, owner=None) -> Result`
- `staged_files_absent(*, owner=None) -> bool`

Both are exposed on the CLI as the coverage pair is, and both join the `Host` protocol beside it.

**Which entries.** One function decides, and removal, the question and the plan all call it. An owned product or dev tool is covered when all of these hold:

- its `stages_artifact` is true (the `shell` products and dev tools, a tarball `docker_image` product, `kmod` products, the `kmod` and `kmodcov` dev tools, and a provider's product built on the shell class);
- it has an artifact and a stage directory that can be resolved (the same test `check_stage_collisions` applies);
- it is not a `DeclaredProduct` whose `_overridden("stage")` says a `class =` subclass replaced staging. The built-in kinds override `stage` themselves and are always covered.

A host that does not create stage directories (`_creates_stage_dir` is false: embedded targets) is sent nothing. A stopped container host is passed over where the other project steps pass it over: the project layer gets a dispatcher for the step with the same `_passed_over` handling (the host verbs themselves have none).

**Resolving the path.** The path is the entry's resolved stage directory joined with the artifact's basename, which is where every built-in kind stages (none renames). One function answers "where is this entry's staged file"; the two places that compute it today (`docker_image_kind.py`'s `_staged_tarball` and `UnixHost.load`) call it, and so does the new step. An entry is skipped only when its declaration and the host's capabilities give it no staging destination at all (no declared directory, no `default_dest_dir`, and a host with no login-home concept): it was never staged there. A login home that exists but could not be discovered just now (the probe failed or answered nonsense) is a failure of the step, and makes the question raise; it is never read as "nothing staged". A dry run resolves what it can without the host and declines the rest.

**What is removed.** `rm -f` of each such path. For the kinds that delete their staged file after use this is a no-op on a good day; it removes the tarball a failed `docker load` left, a `.ko` left by a killed load, and the file of an `otto host stage` that no install followed. A path that is absent is success.

**The cost, stated.** The file is named by the declaration, not by a record of otto having put it there. On a host where the product was never installed, a file of the same name in the same directory is removed, exactly as an install would have overwritten it. This is the price of ruling 1; the default stage directory (the login home) is where it is most likely to matter, and the docs say so.

**Then the directories, opportunistically.** For each distinct stage directory that was DECLARED on a covered entry (never the host's `default_dest_dir`, never the login home), deepest first, one `rmdir`. It removes the directory only when it is empty. A directory that is absent, not empty, or that the session cannot remove (a root-owned parent) is kept, and that is never a failure. No reason is classified: after the `rmdir`s the same command reports which of the declared directories still exist, and each is logged at INFO as kept ("not empty, or the session cannot remove it"), the way the coverage sweep reports the roots it keeps. Parents that `mkdir -p` made are left. Two stateless guards keep a shared system directory: a directory one component below `/` (`/tmp`, `/opt`) is never tried (the cov_dir rule's depth decision, shared), and a sticky directory (`test -k`: `/var/tmp`) is skipped by the command, because the container's root `rmdir` of an empty one would be recreated plain, without the sticky bit, by the next `mkdir -p`; the sticky one falls under the kept line.

One builder spells "remove this directory if it is empty, and never fail" for the code base. `docker/staging.py`'s build-input cleanup and the coverage sweep's two spellings in `host/product.py` either call it or are named in the plan as different on purpose (the coverage one that is not quiet stays as it is).

**One command.** Staging is a plain `put` as the login user on every built-in kind, so on a unix host the removal is one unelevated command. On a container host the transfer lands the file as root (its own `mkdir`, `chmod` and `chown` run as root), so the removal runs as root there too; otherwise a root-owned stage directory would refuse the `rm` on every cleanup and the host would never read clean.

**Failures.** A file that exists and cannot be removed is an error naming the path, folded into cleanup's best-effort result like the other steps. An unreachable host fails as it does for the other steps.

**Where it runs.** After the coverage sweep and after the dev tools are uninstalled, so a stage directory that held coverage data or a tool's file is empty by then:

- `Host.cleanup`: `uninstall`, `remove_coverage_dirs`, `uninstall_dev_tools`, **`remove_staged_files`**, `remove_toolchain_tools`. Host-global, as today.
- `ProjectActions.cleanup`: the same step dispatched with the project's `owner`, after its dev-tool step and before its docker-staging step.
- The orchestrator's cleanup reaches it through each project. Two projects sharing a host and a stage directory each remove their own files (the collision check makes each staged path unique per host); whichever empties the directory removes it, and the other finds it absent.

A project's `uninstall` override still gets the step, since it is cleanup that calls it. A project or host class that overrides `cleanup` itself must call the base one, as for the other steps.

### 3. `is_clean`

`Host.is_clean` and `ProjectActions.is_clean` add `staged_files_absent` (owner-scoped in the project's case, inside the loop that already passes over stopped containers). In one command per host it asks whether any path section 2 would `rm` still has a directory entry, without following a final symlink (a dangling symlink is present, since `rm -f` would remove it). Only confirmed absence counts as absent: a path it cannot look at (an unsearchable parent) or a probe that did not answer raises, using the coverage sweep's marker framing and its care about inaccessible ancestors. It refuses under a dry run.

It asks about files only. Directory removal is opportunistic housekeeping that happens when cleanup runs: an empty declared stage directory does not make a host unclean, so `ensure("clean")` does not run cleanup for a directory alone. The docs state that.

### 4. What stays (#838)

No product code changes for #838. Its three items are answered:

- **The docker staging root:** kept (ruling 3).
- **A toolchain tool's `dest`:** kept. On a remote host otto never creates it: the toolchain install issues no `mkdir`, and every remote transfer backend writes into `dest` and fails or misplaces the file when it is missing. (A local host's transfer does create it; the directory is left there too, by the same rule as every other directory otto cannot be sure it owns.) A unit test pins that the toolchain install issues no `mkdir`.
- **Images:** kept (ruling 2).

### 5. The plan

`project/plan.py`'s cleanup plan gains one step for the new verb, built like the coverage step: one line per host naming the files it would remove and the directories it would try, and a gap line, from its own override check, when the host's class replaces `remove_staged_files` (the cleanup plan checks each step itself; the `_HOST_VERBS["cleanup"]` entry is not what it reads, and that dead entry is deleted so it cannot drift). `project/render.py` prints the step in the style of the existing ones. Under a dry run each removal is declined.

### 6. Docs and examples

- `docs/configuration/declared-products-tools.md` (the lifecycle's home): the rule per kind, and `docker_image` uninstall removing only the container.
- `docs/cli/run/defaults.md`, "What `cleanup` leaves behind", is the one home of the list: every image (with `docker image rm` and `docker image prune`), the docker staging root and why, a toolchain tool's directory, a stage directory that still holds something, the default stage directory, parents of a declared stage directory, and the bind-mount parent of a containerised product's `cov_dir`.
- `docs/setup/tracks/docker.md`, `docs/cli/host/index.md`, `docs/cli/host/capabilities/index.md` and the other pages that describe cleanup link there and stop restating it.
- The example settings keep their `uninstall` strings (ruling 4). Their comments and `docs/setup/defining-products-and-tools.md` say that a product with something to stop stops it in `uninstall`, and that cleanup removes the staged file either way.
- Captures whose output changes are refreshed on the bed.

## Breaking changes

- `uninstall` of a tarball-loaded `docker_image` product no longer removes the image, and nothing else does. A script that relied on it adds `docker image rm`. The product's `loaded_ref` attribute goes.
- `cleanup` removes the staged file of every covered product and dev tool (`shell`, a tarball `docker_image`, `kmod`, the kernel-module dev tools, and third-party kinds built on the shell class), by its declared path, and then a declared `stage_dir` that is empty.
- `is_clean` is false while such a file remains, so `ensure("clean")` runs cleanup in that case, and a project or host class that overrides `cleanup` without calling the base one reads not-clean until it removes the file itself.
- The `Host` protocol gains `remove_staged_files` and `staged_files_absent`. A host class that does not derive from `BaseHost` must provide them. The public API snapshot is regenerated.

Additive: the two host verbs `remove-staged-files` and `staged-files-absent`.

Not changed: plain `uninstall` and `uninstall-tools` still leave staged files in place.

## Tests

- Unit, `docker_image`: the uninstall sequence for tarball and reference products (one command each), the plan text, and reinstall after uninstall with the image present. Tests whose subject is gone are deleted and named in the commit: `test_uninstall_resolves_the_image_from_the_container_not_from_memory`, `test_uninstall_skips_rmi_when_the_container_is_already_gone`, `test_uninstall_rmi_failure_names_the_command`, the two dry-run declines on rmi and on inspect, and the plan's image-gap cases. `test_uninstall_removes_the_container_and_only_a_loaded_image` becomes "removes the container and no image" for both forms. The `loaded_ref` pins follow the attribute.
- Unit, the removal step: each covered kind; an entry skipped for each reason in section 2; owner scope; a shared stage directory and a nested one (deepest first); a default stage directory (never tried); a non-empty directory and a refused `rmdir` (kept, no failure, the INFO line); an absent file and an absent directory (success); a file that cannot be removed (error naming it); a dry run; an embedded host (nothing sent); a container host (the removal runs as root); cleanup run twice. At the project layer: a stopped container is passed over.
- Unit, the question: present, absent, a dangling symlink (present), an unsearchable parent (raises), the probe not answering (raises), a failed home discovery (raises), a dry run (refuses).
- Unit: the order of steps in `Host.cleanup` and `ProjectActions.cleanup`; the plan step, its gap line for an overridden verb, and the rendered text; the public API snapshot.
- Unit: the toolchain install issues no `mkdir`.
- Bed: an e2e cleanup test asserts a staged shell artifact is present after `uninstall` and gone after `cleanup`, with its declared stage directory. The docker-image coverage e2e, which asserts the image is gone after teardown, removes the image itself.

## Landing

One pull request: this spec, then one commit per issue. #836 and #837 are marked breaking; #838 is a docs commit with one pinning test.

## Left to the builder

- Where the shared quiet-`rmdir` builder lives.
- The wording of the plan's new lines.
