# Coverage and docker bed batch: #778, #783, #782, #777

**Status:** revision 10, 2026-11-04. Astra r9 approved #782, and r10 adds the one change it required in #777: a mandatory files-only postcondition for every counter reset.

r9: revision 9, 2026-11-04.

Astra r8 resolved the expiry clock and approved the POSIX removal. r9 makes these changes:
- `{} \;` replaces `{} +` (Chris; measured: BusyBox ignores `ARG_MAX` with `+` and exits 0);
- the empty-dir probe's exit status;
- `asyncio.wait_for` for the total scan deadline;
- `compensate(timeout=)` bounding teardown inside the expiry margin.

r8: revision 8, 2026-11-04.

Astra r7 approved #783 and all of #777, and asked for rework of #782 (the expiry clock and the total budget). r8 makes these changes:
- the expiry uses the docker parent's clock, with one enforced `SCAN_TOTAL_BUDGET`;
- #777 removes with POSIX `find -exec` (per Chris's note on #842), shared with the counter reset, which folds #842.

r7: revision 7, 2026-11-04. Astra r6 approved #783 and #777 §2, §3 and §5, and asked for rework of #777 §4 and #782. r7 makes these changes:
- the grouped sweep expression;
- the `find` capability contract and a postcondition re-probe;
- a scoped `collecting_coverage` flag instead of `cov_decision`;
- an expiry-labelled stale-scan sweep that never touches live scans.

r6: revision 6, 2026-11-04.

Astra approved r5 for all four items. The Opus r5 final review approved #778 and #783, and #782 and #777 with changes. r6 applies those changes:
- **#777:** Chris's ruling that cleanup deletes `.gcda` counters plus empty dirs, never `rm -rf`. That drops the protected-path probe. Also: an unelevated-first `is_clean`, `cov_decision` as the `--cov` flag, the full evidence chain, and an opt-in presence probe.
- **#782:** labelled scan resources with a stale-scan sweep, paused taking the snapshot path, removing treated like dead, an `image-missing` kind, the reader's single inspect, and `_fetch` on the same teardown.
- **#783:** `rm -v`.

**Source:** round-2 naive-user study findings (#594). All four are wrong-answer or data-loss bugs.

**Landing:** one bed PR for the tech1 cell, one commit per issue, in this order: #778, #783, #782, #777. Tracks: A = #778; B = #783 then #782, one agent in sequence; C = #777. B rebases on A, and C on B. Shared files: `unit_filter.py` and `tiers.md` (A, B), `docker_image_kind.py` (B, C, separate hunks), `project_hier/.otto/settings.toml` (A, C), `declared-products-tools.md` (B, C) and `public_api.txt` (A, C), which is regenerated on each rebase. The docs captures get one bed refresh at the end.

## Maintainer rulings (Chris, 2026-11-04)

- **#778:** remove `harvest_dirs`. Product views become the only declaration of unit coverage directories. A config that still sets it is refused at load.
- **#777, the principle:** "cleanup removes all tools and traces that otto would normally place on the host during an install and execution of products … Uninstall is only meant to exit or unload a product and otherwise leave tools and data on the host." So cleanup removes coverage data, and uninstall never does.
  - The other deviations the audit found are filed separately: #836 (`docker rmi` in uninstall), #837 (stage dirs, staged artifacts and the examples' uninstall strings), #838 (the staging root, toolchain dest dirs and pulled images) and #839 (compose stacks, a question).
- **#777, `ensure("clean")`:** "I think it's fine for ensure clean to remove coverage counters. Users should ensure uninstalled if they only want to uninstall the products and continue testing." No deferral during `--cov`. The docs steer coverage suites to `ensure("uninstalled")`.
- **#777, how much cleanup deletes (on Opus r5 F2):** only `.gcda` counters and the directories they leave empty, through the existing `gcda_delete_cmd` decision, never `rm -rf` of `cov_dir`.
- **#780:** not planned. otto stays a thin wrapper, and the issue is closed.

## Global constraints

- Python floor 3.10. No `from __future__ import annotations`. AGENTS.md "ONE DECISION, ONE PLACE" binds every item; each item below names the existing site it reuses.
- A user-facing behaviour change a user must act on is breaking: `type(scope)!:` plus a `BREAKING CHANGE:` footer that names the remedy. A bug fix that asks nothing of the user is not breaking.
- Docs keep one home per topic and link to it rather than restate. `make docs` must build without warnings. The captures under `docs/examples/captures/` are refreshed on the bed with `scripts/refresh_docs_captures`, never hand-edited.
- Lab proofs run on the tech1 bed under the bed lease, and docker proofs also hold `docker_host_lease`. Proofs go into existing bed suites where one fits. Line numbers below are as of `f013609f5` and drift; the named symbol is the anchor.

---

## #778: a unit build directory is declared once, under the product it tests (`fix(cov)!:`)

**Problem.** A directory reachable from both `[coverage.tiers.unit] harvest_dirs` and a `[coverage.tiers.unit.products]` view is harvested twice, so lines are counted twice (the study measured 30 against a true 15). lcov's `-d` recurses, so `harvest_dirs = ["build"]` also covers `build/app-tests`.

Both reviewers found that product views alone can do the same thing:
- one view with nested dirs (`app = ["build", "build/app-tests"]`);
- two views whose dirs nest or are equal.

Every capture is loaded and its counts are added (`validity.py:98`, `reporter.py:626`, `store/model.py:100`). So the fix is a check on the declaration, not a dedupe by source line. Independent executions must still accumulate.

**Design.**
1. **Remove the field.** Delete `harvest_dirs` from `CoverageTierSpec` (`src/otto/models/settings.py`, ~715) and from the runtime `TierConfig` (`src/otto/coverage/tiers.py`, ~39/55/75).
   - Add a `mode="before"` removed-key validator on `CoverageTierSpec`, modelled on `LoggingConfigSpec._reject_removed_capture` (`settings.py:~486-509`).
   - The settings-path prefix that validator computes inline is extracted into one `_settings_where(info)` helper, which both validators call.
   - The message echoes the user's own dirs: "`harvest_dirs` was removed: declare each unit build directory under `[coverage.tiers.<tier>.products]`, named for the product it tests (for example `<product> = ["build"]`)."
   - **`load_tiers()` (`tiers.py`)** must not have its own silent reading of a raw dict. Every caller gets the same refusal: if `load_tiers` reads raw TOML rather than a validated `CoverageTierSpec`, it validates through `CoverageTierSpec`. The plan greps the callers and records which path each takes.
2. **Remove the unnamed view.** `plan_unit_views` (`src/otto/coverage/unit_filter.py`, ~318) builds views only from `tier.products`. Delete the dead branches and docstrings that assumed an unnamed view:
   - `not name` branches: `unit_filter.py:~321-323, ~431-446`;
   - docstrings: `unit_filter.py:228, 237, 273`;
   - `reporter.py:596` (`if view.product else ""`) and the docstrings at `reporter.py:528, :1138`.

   The store's `product=""` stays meaningful for synthetic runs (`reporter.py:354`, `validity.py:32`). Leave that alone.
3. **One dir, one view: refused at load.** A `mode="after"` validator on `CoverageTierSpec` refuses a tier in which any dir equals, or sits inside, another dir of the same tier, within one view or across views. It anchors against `info.context["root"]` (`settings.py:~488`), the same anchor `plan_unit_views` uses. The refusal names both views and both dirs.
   - **Normalisation is explicit.** `anchor_path` (`src/otto/utils.py:49`) deliberately leaves `..` intact. So the comparison runs `posixpath.normpath` after anchoring and `~` expansion, and `build/x/../app` equals `build/app`. It does not resolve symlinks: these are runner-side build trees, and the capture reads them lexically too.
   - Because the check sits on the spec, `load_tiers()` gets it through the same validation (step 1). Nothing is added to `plan_unit_views`.
   - `load_tiers` (`tiers.py:71`) has no root today, so it gains a `root` parameter, passed as the validation context. Callers get it from `cov_project` (`coverage/get.py:134`, `report_inputs.py:89`, `collect.py:395`). `_resolve_tier` doesn't hold the root today, so it gains the parameter, threaded through both its calls (`collect.py:401, :459`). It never falls back to the working directory.

   This turns "declared once" from a sentence in the docs into a check. A tier with no overlap behaves exactly as today.
4. **One predicate.** "Does this tier have anything to harvest?" is restated at `unit_filter.py:302` and `reporter.py:535`. Extract one function (`TierConfig.harvests_unit`) and call it from both.
5. **Example, fixture and template config.**
   - Convert each config that sets `harvest_dirs` to products views, each view named for the product whose tests the directory holds:
     - `docs/examples/quickstart-{docker,shell,kmod,embedded}/.otto/settings.toml`;
     - `tests/project_hier/.otto/settings.toml` (~65);
     - `tests/_fixtures/vocabulary.py:130-134`.
   - Where a directory holds tests for a library that is not a product, the example names one product that links it. In `quickstart-{shell,docker}`, the `textlib` static library is linked by both `wordcount` and `shout` (`quickstart-docker/CMakeLists.txt:24-28`). The example carries a comment saying which product it picked and why. Don't invent a pseudo-product.
   - Update the `otto init` template (`src/otto/init/templates.py:~141-161`) so it shows only `products`.
6. **Docs.** `docs/cli/cov/tiers.md` is the home.
   - In `tiers.md`, update :13, :36, :57-58, :122, :144, :187, :211, :248, :292, :310, :378 and :466. Drop the dead "every product … instrumented = false" row of the refusal table (~270).
   - Rewrite `coverage-unit-one-place` (:170-177) as "a unit build directory is declared once, under the product it tests", and document the new refusal there.
   - Rewrite `coverage-unit-shared-library` (:180-188). Its "keep one `harvest_dirs` sweep" option is gone, and the rule is stated plainly. A library's unit build dir is declared once:
     - under the library's own product, when it is one (project_hier's `libcore`);
     - otherwise under one product that links it, in which case the other linking product's pin shows no unit evidence for the library.
   - Other pages: `docs/cli/cov/report.md:38, :337`, `docs/setup/coverage.md:337-340`, `docs/architecture/subsystems/coverage/types.md:27, :244` and `merging.md:131, :136`.
   - Track pages: `docs/setup/tracks/{shell,docker,kmod,embedded}.md`, for example `shell.md:274` and `docker.md:424` ("The `unit unit:` line…").
   - Captures: `docs/examples/captures/qs-*-report.txt`. The `unit unit` label and the `unit_unit_0` file name change, so refresh them on the bed.
   - Leave `CHANGELOG.md` and the historical specs alone.
7. **API snapshot.** Regenerate `tests/unit/api_snapshot/public_api.txt` (drops `TierConfig.harvest_dirs`, gains `harvests_unit`) with the repo's golden tool.

**Tests (RED first).**
- A config with `harvest_dirs` is refused at load, with the pointer message and the settings path. Also refused through `load_tiers()`.
- Overlap refusal, at load and through `load_tiers()`:
  - `build/x/../app` against `build/app` (normalisation);
  - one view with nested dirs;
  - two views sharing one dir;
  - two views with nested dirs, using the issue's `core.cpp:4` shape where both views' sources include `core.cpp`. This test fails today; a disjoint-source variant would not.

  Each refusal names both views. Disjoint dirs are accepted.
- Rework the existing tests that set `harvest_dirs`: `tests/unit/cov/test_pipeline.py` (~15 sites), `test_tiers.py`, `test_settings_coverage.py`, `test_cov.py`, `test_run_api.py`, `test_spa_renderer.py`, `test_report_states.py`, `test_quickstart_embedded_example.py:80`.
  - Delete the ones that test unnamed-run behaviour.
  - Move the path-anchoring and `~`-expansion tests (`test_pipeline.py:708, :747`) onto products views.
- Bed: `tests/e2e/cov/test_hierarchical_coverage_e2e.py` is updated, not just kept green. `:276-284` asserts `unit == [("", "")]`; it now asserts the converted product's name. Fix the docstrings at :276 and :288.

**Breaking footer.** `BREAKING CHANGE: [coverage.tiers.<tier>] harvest_dirs is removed; declare each unit build directory under [coverage.tiers.<tier>.products], named for the product it tests. A config that still sets harvest_dirs is refused at load, and so is a tier where one unit dir equals or sits inside another. A unit view now counts only its named product's sources; declare a shared library's build dir once, under the library's own product or under one product that links it.`

---

## #783: install over a stopped container of the same name (`fix(docker):`)

**Problem.** `docker_image` install runs a plain `docker run -d --name <container_name>` (`src/otto/host/docker_image_kind.py`, ~246). A stopped container with that name makes it fail with "name already in use". `is_installed` counts only a running container (~347-356), so a stopped one reads UNINSTALLED, and `--ensure` doesn't recover it.

**Design.**
1. **When.** Immediately before `docker run`, after the load or pull has succeeded, install runs `docker rm -v <name>`, without `-f`. `-v` removes the stopped container's anonymous volumes, so they don't dangle; named volumes are never touched. A failed load or pull leaves the old container alone. Uninstall's `rm -f` gets `-v` through the same builder.
2. **Which failures are tolerated: only no-such-container.**
   - Docker refuses, without force, to remove a running, paused or restarting container. That refusal ("stop the container before removing or force remove") aborts install with docker's own message. It is clearer than today's "already in use", and it needs no version-coupled list of refusal strings (Opus r2 N6).
   - An active container reads `.State.Running == true`, so `is_installed` reports it installed and `--ensure` never reaches this path. Only an explicit `otto run install` over an active container does, and that fails today too.
   - Any other `rm` failure also aborts with its output.
3. **One no-such-container predicate.** That decision has two homes today:
   - `docker_image_kind.py:64`, case-sensitive and checking the value only;
   - `objects.py:688`, case-insensitive and checking the message and the value.

   Merge them into one predicate in `otto.host`, called by uninstall, the new install step, and #782's reader. The shared core is the existing result-text extraction, `_said`/`_no_such_container` (`objects.py:688-700`), which moves; no third copy is written. #782 parameterises it by resource.
4. **One builder.** `_rm_command` (~181) stays the one remove-by-name builder and gains `force: bool`: uninstall keeps `-f`, install uses the plain form. The error label at ~278, which hard-codes the string "docker rm -f", is rendered from the builder.
5. **Logging.** When a removal happened, install logs one INFO line: "removed stopped container <name> before install". Two side effects go in the docs:
   - the stopped container's `docker logs` go with it;
   - for a tarball product, its previously loaded image is orphaned (#836 tracks image removal).
6. **Plan.** `plan` (~300-345) renders the new line from the same builder, for both the tarball and the reference plan. No new state: "installed" still means running.

**Docs.**
- The kind's home is `docs/configuration/declared-products-tools.md:485-495` ("a re-install then collides … unless the old container is removed first"). It now says install replaces a stopped or never-started container of that name, and refuses an active one. It also names the two side effects.
- `docs/cli/cov/instrumenting/containers.md` step 4 links to that home instead of restating it. Steps 4-5 are rewritten as a whole in #782.
- Refresh `docs/examples/captures/qs-docker-install-dry.txt` and `qs-docker-install.txt`, which gain the `rm` line.

**Tests.**
- Unit, RED first (`tests/unit/host/test_docker_image_kind.py`). Each container state:

  | State | Outcome |
  |---|---|
  | absent | `rm -v` gets no-such-container, which is tolerated; `run` follows |
  | created | removed, then `run` |
  | exited | removed, then `run` |
  | dead | removed, then `run` |
  | running | `rm`'s refusal aborts install with docker's message; no `run` |

  Also:
  - an unrelated `rm` failure aborts install with its output;
  - a failed load or pull never runs `rm`;
  - a dry run declines the `rm`.
- The merged no-such-container predicate has one test, driven through both former call sites.
- Plan: `tests/unit/host/test_plan_docker_image.py` pins the new line in both plans.
- Bed (test3 docker parent): install, `docker stop`, then `otto run install` succeeds and the product reads installed.

**Not breaking.** It fixes a failure. Installing over an active container failed before and still fails, now with a clearer message.

---

## #782: `cov report` reads a stopped docker_image product's objects (`fix(cov):`)

**Problem.** The unit tier's product-source filter scans the object list inside the **running** container (`docker exec`, `src/otto/coverage/objects.py:~271-363`). A stopped container makes `_refuse` raise "not running — install the product first" (`unit_filter.py:~465-470`). But the documented flow (`containers.md`) **stops** the container so it flushes its counters.

**Contract.** Today the scan sees what the running container's exec process sees: image layers, the writable layer, bind mounts and volumes (`objects.py:281-283`, `_SCAN_SCRIPT` ~609, pinned by `test_a_mounted_build_below_a_top_directory_is_scanned`, `test_object_list.py:1046-1070`), and the process's user and supplemental groups (Docker applies `HostConfig.GroupAdd` to exec).

A stopped container's tmpfs mounts and anything its process mounted at runtime no longer exist. Those are stated as limits; there is nothing left to scan.

**Design.** The scan is dispatched by the container's state, read with one inspect.
1. **Running (running, restarting): unchanged.** The existing `docker exec` scan. Nothing is committed, and the product is never paused.
2. **Stopped or paused (created, exited, paused): a tagged snapshot.** `docker exec` fails on a paused container, which is today's failure, and commit works on one, so paused takes this path.
   1. `docker commit --pause=false --change 'LABEL otto.scan=1' --change 'LABEL otto.scan.expires=<epoch>' <container> otto-scan:<tok>`. The snapshot is **named before the commit runs**, the way `_fetch` names its temp dir (`objects.py:~400-402`), so teardown can remove it when the commit completes with its reply lost.
      - A client cancel while the daemon is still committing can land the tag after teardown's `rmi` has run. The same goes for `create`. So both resources carry the label `otto.scan=1` (`create --label otto.scan=1`).
      - Each resource also carries `otto.scan.expires=<epoch>` (Astra r7):
        - **One clock, the docker parent's.** Before the commit, the scan reads the parent's clock (`date +%s`, on the host that runs docker) and stamps `expires = parent_now + SCAN_TOTAL_BUDGET + SCAN_EXPIRY_MARGIN`. The sweep compares against the same parent's `date +%s`. So runner clocks never enter, and two runners sharing a daemon compare against one clock. A parent clock jump is out of scope; a backward jump only delays recovery.
        - **One total deadline.** `SCAN_TOTAL_BUDGET` is enforced as one deadline over the whole stopped/paused or no-container operation: commit, create, start, every sequential `docker cp` and the `host.get`. It is enforced by `asyncio.wait_for` around **one** coroutine that runs the whole sequence, as `src/otto/host/remote_host.py:432` already does. Not `otto.utils.wait_for`, which polls synchronously and rejects awaitables (`utils.py:480`), and not `wait_for_async`, which doesn't bound an await in progress (`:527`). Today `_COPY_TIMEOUT` (`objects.py:~435`) applies to each `docker cp` separately. It stays a per-copy cap inside the total. Exceeding the total aborts the scan, which runs its teardown and yields `unreadable` with a timeout reason.
        - Teardown is bounded explicitly: the one compensated cleanup coroutine (both the temp-dir and the docker steps) runs under `otto.lifecycle.compensate(timeout=SCAN_TEARDOWN_BUDGET)`. `timeout=` is the unconditional bound; `deadline=` applies only on cancellation (`lifecycle.py:614`). `SCAN_EXPIRY_MARGIN` must be at least `SCAN_TEARDOWN_BUDGET`, plus a fixed allowance, asserted where the constants are defined. So a live scan, including its cleanup, always finishes before its resources can expire. The expiry derives from the same constants that bound the scan, so the two can't drift.
      - **One sweep, shared** by the reader and `make bed-reap`: `sweep_stale_scans(host)` in `otto.host`. It lists labelled containers, then images (`--filter label=otto.scan=1`). It reads each one's `otto.scan.expires`, and removes only expired ones: containers first, then images. Concurrent reports against one daemon never remove each other's resources (Astra r6 4).
      - It runs at the start of **every** scan that creates labelled resources: the stopped/paused path and the no-container image path, which labels its temporary container too.
      - The guarantee is stated as "removed at teardown, or by the next scan or reap", not "never leaked".
   2. `docker create --name otto-scan-<tok> --label otto.scan=1 --label otto.scan.expires=<epoch> --network none --no-healthcheck --volumes-from <container> --user <Config.User> [--group-add <g> for each HostConfig.GroupAdd] --entrypoint sh otto-scan:<tok> -c <_SCAN_SCRIPT>`. The user and groups come from the same inspect. `--volumes-from` carries bind mounts and volumes (Moby `daemon/volumes.go`).
   3. `docker start -a otto-scan-<tok>`, with the existing `_SCAN_TIMEOUT`.
   4. The existing `_fetch`/`_copy_out` (`objects.py:~383-486`) from `otto-scan-<tok>`. `docker cp` mounts the container's configured mounts, including on an exited container (Moby `daemon/containerfs_linux.go`).
3. **Dead or removing.** Docker refuses to commit a dead container, and a removing one is on its way out. Both yield `Unavailable(kind="unreadable")` with a reason of their own: "container <name> is <state>; remove it (`docker rm`) and reinstall".
4. **No container.** There are no mounts or runtime files, so the image alone is scanned. Steps 2.2–2.4 run against the image id, with no commit, no `--volumes-from`, and the image's own user.
5. **Teardown: independent steps, cancellation-safe.**
   - A `finally` owns the teardown: **one** coroutine, run under **one** `otto.lifecycle.compensate` (`lifecycle.py:593`). Inside it, the two steps are nested `try`/`finally`: `docker rm -f -v otto-scan-<tok>`, then, in its `finally`, `docker rmi otto-scan:<tok>`. The `rmi` is unconditional and tolerates no-such-image through a missing-image check. #783's no-such-container predicate becomes one resource-parameterised predicate in `otto.host` (`missing(result, resource="container" | "image")`) over one shared extraction of the result text, so a missing tag is quiet while daemon and permission failures stay visible.
   - `compensate` re-raises the cancellation only after its coroutine has finished (`:600-609`). Two separately compensated steps could therefore skip the `rmi` if a cancel lands during the `rm`; one shielded coroutine with a nested `finally` cannot.
   - Each step logs its own failure, as `teardown_step` does, so a failing `rm` is visible and never skips the `rmi`.
   - The nested `_fetch` temp-dir teardown (`objects.py:~400-421`) moves onto the same pattern (one compensated coroutine), so one reader has one teardown policy.
   - `-v` removes anonymous volumes declared for the temporary container only. Volumes shared through `--volumes-from` stay referenced by the product's container.
   - `rm -f` stops a scan in progress, so `_reap`, `_REAP_SCRIPT` and `OTTO_SCAN_ID` (`objects.py:~372-389, ~620`) become dead code on this path. They stay for the exec path only if it still needs them; the plan decides from the code and deletes whatever is dead.
6. **Cache first.** The cache stays keyed by the product's image id (`objects.py:~229-230, ~318-322`). The lookup happens before any commit, so a cached image never pays for a snapshot. That the key ignores mount contents is pre-existing and out of scope.
7. **Image id and inspect: one home each, in `otto.host`.**
   - `image_id_of(host, reference) -> CommandResult-carrying result` is a free function. It is the `docker image inspect --format '{{.Id}}'` that `_read_back` (`src/otto/docker/build.py:~173-184`) spells inline today. `build.py` calls it directly with its freshly built reference, keeping access to the failing result. The `docker_image_kind.py:166` presence check is the same inspect, so it moves onto it too.
   - The container inspect is spelled three times (`objects.py:548` `_INSPECT_COMMAND`, `docker_image_kind.py:262` and `:350`). It becomes one helper in `otto.host` that returns state, the immutable image id (`.Image`), the original image reference (`.Config.Image`), user and group-add, and all three call it.
   - Uninstall keeps reading the reference: `docker rmi app:1` (`test_docker_image_kind.py:323-332`, `declared-products-tools.md:501-506`). Removal semantics don't change in this refactor; #836 owns them.
   - The reader branches on its **one** container inspect. With a container, it uses that inspect's `.Image`. With no container, it calls `image_id_of(host, product.image)` (`docker_image_kind.py:166`). No `DockerImageProduct.image_id` method is added, because it would be a second inspect and a second home for the same decision (Opus r5 F7).
8. **Unavailable kinds.**
   - Remove `"not-running"` from `UnavailableKind` (`objects.py:~83-93`), along with its `_refuse` branch and its docs:
     - `docs/cli/cov/tiers.md:273` (the table row);
     - `containers.md:92-106`;
     - the `plan_unit_views` Raises docstring (`unit_filter.py:~288-292`);
     - `objects.py`'s module docstring;
     - the `_images` docstring ("running image id").
   - Daemon and permission failures stay `unreadable`.
   - "Neither the container nor the reference resolves" gets its own kind, `image-missing`, in `UnavailableKind`. `missing` already means "the local artifact doesn't exist" (`objects.py:245`), and `_refuse` (`unit_filter.py:~458-463`) routes it to the "build with --coverage" remedy. `image-missing` routes to "image <ref> is not on <host>'s docker daemon — install the product (or pull the image) first".
9. **Tarballs.** No change. `_read_objects` (`unit_filter.py:~417-427`) sends only `is_tarball is False` to the container path, and a tarball is read locally.
10. **Naming.** Rename `read_running` to `read_container`. `_read_objects` stays the one dispatch.

**Docs.**
- `containers.md` steps 4-5, and the paragraph at :97-101, are rewritten as a whole: install replaces a stopped container (#783, linked), and a stopped container's objects are read. They also state the limits: tmpfs and runtime-only mounts of a stopped container are gone.
- #783's second half, a refusal that suggests a remedy that fails, goes with it.

**Tests.**
- Unit, RED first (`tests/unit/cov/test_object_list.py`, reworking :493-670, :874-876 and :965-987, plus the `_refuse` tests in `tests/unit/cov/test_unit_filter.py`). Each case:
  - **User:** an empty user, a numeric `uid:gid` and a named user each pass through quoted.
  - **Exited and created:** the scan runs commit (with `--pause=false` and the pre-named tag), then create (with `--volumes-from`, the user, each group-add and `--no-healthcheck`), then start, then cp. `docker exec` is never used.
  - **Running and restarting:** the exec path, with no commit.
  - **Paused:** the snapshot path.
  - **Dead and removing:** the specific `unreadable` reason.
  - **Labels and sweep:**
    - Commit and create carry `otto.scan=1` and `otto.scan.expires`.
    - The sweep removes expired leftovers, containers before images, and leaves unexpired ones alone.
    - In two overlapping scans against one daemon, neither removes the other's snapshot or container. That includes a slow commit and a long multi-file copy-out, each near the budget.
    - With deliberately skewed runner clocks, expiry still follows the parent's clock.
    - Exceeding `SCAN_TOTAL_BUDGET` aborts, tears down and reports `unreadable`.
    - The no-container path sweeps and labels too.
    - `make bed-reap` and the reader call the same `sweep_stale_scans`.
  - **No container:** the image is scanned alone, with no commit and no `--volumes-from`.
  - **Cache hit:** no commit.
  - **Teardown:**
    - Both removals run on success, scan failure, timeout and cancellation.
    - When only `create` succeeded, both still run.
    - When "commit completed, reply lost" (commit raises after the daemon created the tag), the `rmi` of the tag still runs.
    - A failing `rm` never skips the `rmi`.
    - A cancel delivered during the `rm` still reaches the `rmi`.
    - The `_fetch` temp dir is removed under the same cancellation.
  - **Mounted build:** a mounted build below a top directory is still scanned through the stopped path.
  - **Unavailable:** a missing image yields `image-missing` with the new remedy, a missing local artifact keeps `missing`, and a daemon failure stays `unreadable`.
  - **Missing predicate:** a missing `otto-scan:<tok>` tag is quiet in teardown, while a daemon or permission failure on `rmi` is logged. One test drives the container and image resources through the shared predicate.
- `image_id_of` has one test driving both callers (`build.py` and the reader's no-container branch) with the same inspect output. The container-inspect helper has one test driving its three callers.
- Bed (test3, containerd image store), a proof that can fail:
  - `tests/project5` gains a unit view naming its docker reference product, with real counters and known expected sources and counts.
  - The new bed test follows `containers.md` exactly: run, stop the signal-handling product to flush, fetch, then `otto cov report` while the container stays stopped and the image stays present, all under `docker_host_lease`. It asserts the expected sources and counts.
  - A second product variant whose instrumented binary arrives through a `run_args -v` bind mount is listed after it stops. That proves the `--volumes-from` + `cp` claim on the lab.
  - Afterwards, no `otto-scan:*` image and no `otto-scan-*` container remain. That records commit and `rmi` behaviour on the containerd store.
  - The existing fixture at `tests/project5/tests/test_cov_container.py:83` removes containers before reporting, so it cannot be this proof.

**Not breaking.** A refusal on the documented flow becomes a report. The running path is unchanged, and the stopped path reads the same object set less what no longer exists.

**Follow-up issue #841.** Capture-time pairing still answers "a reference, not a local tarball" (`objects.py:240`, `containers.md:112-116`) while the unit tier now reads any container's image. That is two answers to one question.

---

## #777: coverage data belongs to cleanup, never to uninstall (`fix(cov)!:`)

**Problem.** A product whose `cov_dir` is deleted before collection loses all its coverage, and the run still exits 0, with only a DEBUG line (`src/otto/coverage/fetcher/remote.py:~196-198`). Users learned to delete `{cov_dir}` in `uninstall`, and an `--ensure` recovery uninstalls the whole lab. Meanwhile nothing removes `cov_dir` when a lab is cleaned up, which goes against the principle.

**Design.**

### 1. Refuse `{cov_dir}` in a declared `uninstall`, through the one parser

- `substitute_placeholders` (`src/otto/host/shell_kind.py:112`) gains `allowed: Collection[str] = _PLACEHOLDERS`. The field walk (`:145`) checks names against `allowed`, not the global, so a disallowed name raises the named `ValueError` before `format_map` (`:154`) can raise a bare `KeyError`.
- `_placeholder_error` (`:102`) takes `allowed` too, so the "valid:" list follows from it. It also takes an optional per-name hint, for `cov_dir` in uninstall: "`{cov_dir}` in `uninstall` deletes the counters otto collects at run end; uninstall leaves coverage data alone, and `otto run cleanup` removes it."
- `build_declared`'s existing uninstall substitution passes `allowed=_PLACEHOLDERS - {"cov_dir"}`. It replaces that call, not a second pass. kmod and docker keep the default.
- An escaped `{{cov_dir}}` stays legal.
- It covers `kind = "shell"` and `class =` entries. Dev tools have no `cov_dir`.

### 2. `cov_dir` is an absolute, normalized path, owned by one product

- At load, in `apply_providers` (`src/otto/host/factory.py:~340-348`), right after `stamp_cov_dir`. That is the one ingest chokepoint, so it also covers code and compose products.
  - `cov_dir` must be absolute. This reuses `_refuse_relative` (`product.py:207`) for the message.
  - It must equal its `posixpath.normpath` and must not start with `//`, which `normpath` preserves. So no `..`, `.`, `//` or trailing slash.
  - It must have at least two components: `/tmp/x` is accepted, `/` and `/tmp` are not.
- Two products on one host whose `cov_dir`s are equal or nested are refused, naming both. Sharing a dir already let one product's `reset_coverage` wipe the other's counters.
- Every shipped default, example and fixture already satisfies this (Opus r2 grep): `/tmp/<name>`, `/var/cov/*`, `/var/coverage/product` and `/tmp/quickstart-docker/banner`. (kmod's `/sys/kernel/debug/gcov/...` is its `gcov_path`, not a `cov_dir`; `kmod_kind.py:80`.) These refusals are listed in the footer.

### 3. Loud on loss, from evidence

gcov creates `cov_dir` the first time the product exits (`product.py:~571`, reasoning at `remote.py:~96-100`). A missing dir is therefore normal for a product the suite never exercised, and for every product on the first run after a cleanup. That case must stay quiet.

- **The evidence.** `clean_coverage` (`src/otto/coverage/collect.py:94`) gains an opt-in `record_presence: bool = False`. Only the pre-run reset sets it.
  - When set, before resetting, it runs one batched, **unelevated** `test -d` per host over the eligible products. Each product prints `<index> present` or `<index> absent`; no path is echoed. An `error` (non-zero exit, or a malformed or incomplete batch) yields no evidence for that host.
  - `otto cov clean` and the post-fetch reset never pay for the probe.
  - The result is carried on `CleanReport` (`coverage/reports.py:43`) as `present_at_reset: frozenset[tuple[str, str]]` of `(host.id, product.name)`. It defaults to empty, so existing `CleanReport(hosts={})` construction keeps working (`collect.py:157`, `tests/unit/cov/test_collect.py:72`). `hosts` and the result-derived `ok` are unchanged (`reports.py:46-82`).
  - The per-host worker (`collect.py:231-257`) returns its evidence beside its reset results, and the aggregation unions it. Embedded hosts skip the probe but keep their counter reset.
- **The owner and the handoff.** The evidence is never module state.
  - `_pre_run_cov_clean` (`src/otto/suite/run.py:570`) returns its `CleanReport`.
  - The invocation's session object carries it through every hop, each of whose signatures changes:
    1. `_Sessions._before_tests` (`run.py:~1605`);
    2. `_post_run_coverage_beside` (`:~1842`);
    3. `_post_run_coverage` (`:~621, :~629`);
    4. `collect_coverage` (`:~669`);
    5. the fetcher (`GcdaFetcher` and `_take_archive`, `remote.py:~161`).
  - The post-fetch reset inside `collect_coverage` produces its own report and never replaces the invocation's evidence.
- **The warning.** In `_take_archive`'s `_NO_COV_DIR` branch, a product in `present_at_reset` logs WARNING: "cov_dir <path> on <host> for <product> existed at run start and is gone: its coverage is missing from this run (was it removed before collection?)".
- **No evidence, stays DEBUG.** That covers `--no-cov-clean` and a standalone `otto cov get`.
- **No once-guard.** Each collection visits each product once.

### 4. Cleanup removes what coverage placed: counters and the empty dirs that held them

**Ruling (Chris, 2026-11-04, on Opus r5 F2).** Cleanup deletes only the `.gcda` files gcov and otto placed, then the directories left empty. It never runs `rm -rf` on `cov_dir`.

**Spelling:** `find -exec`, which Chris named on #842 as the general solution for every Unix, instead of `-delete`/`-empty`. Using it here, and folding #842 into this commit, is the spec's call: the counter reset and cleanup share the files pass, so they must share its spelling.

Today otto only ever deletes `.gcda` files (`gcda_delete_cmd`, `product.py:565-577`). A misconfigured `cov_dir` such as `/var/log` or `/home/alice` must never become a sudo `rm -rf`. The removal reuses that existing decision, in a portable spelling (below). That is why revision 5's host-wide protected-path plan, walk-down probe and fail-closed records are gone.

- **Instrumented products, one predicate.** Move `instrumented_products` (`src/otto/coverage/instrumentation.py:~113`, `instrumented() is True`) down into `otto.host.product`. The reset, the fetch and cleanup all call it. Cleanup and the presence probe add one filter that the reset doesn't apply: skip `embedded`, which has no host `cov_dir`, because the current predicate doesn't exclude it (`instrumentation.py:113`).
  - `instrumented() is None` ("cannot tell", the custom-`Product` default) is **not** eligible. otto never deletes under a path it can't tell it instrumented.
- **What cleanup removes: POSIX `find -exec`, one helper shared with the reset (folds #842).** `-delete` and `-empty` are GNU/BusyBox extensions that BusyBox may compile out (the dev VM's 1.36.1 lacks `-delete`). `-exec` is POSIX, so `otto.host.product` spells the removal once, used everywhere:
  - **Counters:** `gcda_remove_files(d)` = `find <d> -name '*.gcda' -type f -exec rm -f {} \;`, one `rm` per file. The path is quoted as `product.py:553` quotes it today.
    - **Why not `{} +`** (Chris, 2026-11-04, measured): POSIX requires `{} +` to split its command lines under `ARG_MAX`, and GNU find does: 21,970 paths of 200 bytes went to 51 invocations. BusyBox 1.36.1 doesn't. It built one command line, hit "Argument list too long", passed only 1,822 of the 21,970 paths, and **still exited 0**.
    - `{} \;` can't exceed the limit. A coverage tree's `.gcda` count is hundreds to low thousands, so one process per file is cheap; on BusyBox, `rm` usually runs as an in-process applet anyway.
    - `{} \;` doesn't propagate `rm`'s status, so the postcondition re-probe (below) is the only proof of removal, and it is mandatory.
    - It replaces the `-delete` form in `gcda_delete_cmd` (`product.py:~565-577`), so the counter reset (run start, post-fetch, `otto cov clean`) and cleanup share one spelling, and #842 closes with this commit.
  - **Emptied dirs:** `gcda_remove_dirs(d)` = `find <d> -depth -type d -exec rmdir {} \;`. Cleanup only, never the reset.
    - `rmdir` refuses a non-empty directory. That refusal is expected and ignored, which is why this pass uses `\;` and discards its status.
    - `-depth` removes children first, so a tree that held only coverage data is removed up to and including `cov_dir` when the session can remove `cov_dir`.
    - **An empty `cov_dir` the session cannot remove is kept (maintainer ruling 2026-11-04, review I1).** When `rmdir` of `cov_dir` itself fails (EACCES under a root-owned parent such as `/var/cov`), the sweep still removes every empty dir beneath it and the `cov_dir` stays, empty. The empty-dir postcondition excludes the start path, so cleanup succeeds, logs the INFO "kept" line below, and `is_clean` is true. An empty subdir or a `.gcda` left under `cov_dir` is still a leftover, and a `cov_dir` the session can remove is still removed.
  - **The probe (`is_clean`), POSIX too:**
    - `find <d> -name '*.gcda' -type f -print` lists the counters left;
    - `find <d> -depth -type d -exec sh -c 'entries=$(ls -A "$1") || { printf "otto-ls-failed %s\n" "$1"; exit 1; }; if [ -z "$entries" ]; then printf "%s\n" "$1"; fi' sh {} \;` lists the empty dirs left, one `sh` per dir.
      - Astra r8: the earlier `[ -z … ] && printf` exited 1 on a healthy non-empty dir, and the substitution hid an `ls` error.
      - Because `\;` doesn't propagate status, an `ls` failure is reported by the `otto-ls-failed` marker, which the parser treats as a probe error.

    Any output means not clean.
  - **Postconditions, one per caller.** A zero exit doesn't prove removal under `\;`, so every caller checks afterwards.
    - **Cleanup** re-runs the full probe (counters and empty dirs).
    - **The counter reset**, every caller of it (run start, post-fetch, `otto cov clean`, plain and elevated), re-runs the files-only check, which is the existing `gcda_find_cmd(d) + " -print"` (`product.py:553`). It doesn't re-run the full probe, because the reset deliberately leaves empty dirs.
      - Today the plain reset returns `host.exec`'s status (`product.py:~534`) and the elevated one maps an OK result to success (`:~607`). Both become "the removal ran **and** the files-only check printed nothing".
      - Any leftover counter, or a check error, fails the reset and names what was left, so stale counts never reach the next collection.
      - The check runs through the same elevation as the removal it verifies.
  - **Verified on the dev VM** with GNU findutils and with BusyBox 1.36.1, on a scratch tree holding nested `.gcda` files, a name with a space, and a user file:
    - the files pass removed every `.gcda` through `{} \;`;
    - past `ARG_MAX`, BusyBox's `{} +` silently dropped most paths (above), which is why `{} +` is not used;
    - the dirs pass removed the emptied dirs and kept the one holding the user file, and its parents;
    - the user file survived.

  Behaviour of the sweep:
  - It matches only by name and type: regular files named `*.gcda`, and directories `rmdir` accepts. A user file is safe unless its name ends in `.gcda`; the guarantee is about names and types, not provenance.
  - A dir that also holds other files keeps them. Cleanup logs INFO "cov_dir <d> kept: holds non-coverage files, or the session cannot remove it" (the one line for both kept cases above), and the product counts as clean.
  - `find` doesn't follow a symlinked start path or symlinks below it without `-L`/`-H`. A symlinked parent component is traversed, but only `.gcda` files and dirs `rmdir` accepts can go.
- **Running the commands.**
  - The probe never uses the reset's `! test -d … || find …` guard, because an inaccessible ancestor can make `test -d` false and hide the permission failure. It runs `find` directly, and treats "No such file or directory" for the start path as absent.
  - The batched per-host command preserves every product's exit status, so a later success can't mask an earlier failure.
- **Root-owned, one attribute.** `Product.cov_dir_root_owned: ClassVar[bool] = False`, set to `True` on `docker_image` (the bind-mount source) and `kmod`. The removal and `reset_coverage`'s sudo path both read it.
- **Elevation, one core.** Today three places decide how to wrap a script and run it elevated:
  - `sudo_gcda_delete`'s `sh -c` (`product.py:~583-607`);
  - kmod's `_sudo_sh` (`kmod_kind.py:93`);
  - `KmodProduct._run_sudo` (`:357`), with its decline and error handling.

  They become one core in `otto.host` that consolidates **only the `sh -c` wrapping**, and runs it through `host.run(..., sudo=True)` (`PosixPrivilege._elevate`, `src/otto/host/privilege.py:129`). It doesn't grow a mechanism choice of its own. Small caller-specific error adapters stay where needed. The reset and the removal call it.
- **The probe is unelevated first (Opus r5 F3).**
  - `is_clean` runs the `-print` sweep **without** sudo first. A docker-group user without sudo, common on CI, must still get an answer: the bind-mount source is normally `755`, and its `.gcda` files are readable.
  - Only when that `find` exits non-zero (a permission error) does a root-owned target re-run elevated. If that fails too, `is_clean` raises with the output rather than guessing.
  - The removal of a root-owned target always runs elevated.
- **Where it runs: an owner-scoped host operation.**
  - `Host.remove_coverage_dirs(owner: str | None)` sweeps the host's eligible products for that owner.
  - `ProjectActions.cleanup` (`src/otto/project/actions.py:~558-598`), which is what `otto run cleanup` runs and which never calls `Host.cleanup`, dispatches it through a `_dispatch_…` scoped by `owner=`, exactly as dev-tool removal is. It runs after uninstall.
  - `Host.cleanup` (`host.py:~2798`, host-wide, no owner) calls the same method with `owner=None`.
  - One project never sweeps a neighbour's counters. The step doesn't go in the lab-wide tail (`orchestrator.py:~903-950`).
- **`is_clean` asks the same question.** Both `is_clean`s (`actions.py:~808`, `host.py:~2832`) run the same `-print` sweep, batched into one command per host: any output for an eligible product means not clean. The cleanliness report follows from `ProjectActions.is_clean`. Under a dry run, `is_clean` refuses through `refuse_declined_fact`, as its other facts do.
- **Cleanup during a coverage collection warns (Opus r5 F4, Astra r6 3).**
  - The signal is a dedicated flag, not `ctx.cov` and not `cov_decision`. `cov_decision` caches `ctx.cov`'s auto-detection (`run_context.py:617-619`), and instructions may read `ctx.cov` (`docs/setup/writing-instructions.md:124`), so either could be set with no collection scheduled.
  - The flag is `OttoContext.collecting_coverage: bool`. `otto test` sets it only once it has scheduled the post-run collection (where `_Sessions` receives the pre-run `CleanReport`, `run.py:~1605`).
  - It is scoped by `_session_context` (`suite/run.py:~400`), which already saves and restores invocation state. The once-per-invocation latch lives in that same scoped state, so repeated library calls neither inherit the flag nor the latch.
  - The message: "cleanup removed coverage counters during a coverage run, discarding those collected so far; mark coverage suites ensure("uninstalled") to keep them".
- **The cleanup preview** (`src/otto/project/plan.py:~211`) lists the sweep.
- **What stays the same.**
  - `reset_coverage` stays the counter-clear at run start, after a fetch, and by `otto cov clean` (`collect.py:~102`). It deletes `.gcda` files only, and treats a missing `cov_dir` as success.
  - The `--ensure` recovery runs uninstall, never cleanup (`orchestrator.py:~1545`), so it keeps `cov_dir`.
  - After the ruling, `ensure("clean")` removes counters. After a coverage run the fetch has already cleared the `.gcda` files, but any empty dirs gcov created under `cov_dir` remain, so `is_clean` is false and `ensure("clean")` now does a real cleanup where it used to skip. A flat `cov_dir` left empty, with no subdirectories, is clean, so `ensure("clean")` still skips there.

### 5. Examples, fixtures and docs

- **Fixtures.**
  - `tests/project_hier/.otto/settings.toml:39, :49` drop `{cov_dir}` from their uninstall strings.
  - `tests/unit/host/test_declared_products_tools.py:417` (`uninstall="rm -rf {cov_dir}"`) is reworked.
- **The lifecycle home** states the rule once ("uninstall unloads; cleanup removes what otto placed, coverage counters included"), and the others link to it. That home is `docs/configuration/declared-products-tools.md:124-145`. It also says the sweep removes counters and empty dirs only, and keeps a `cov_dir` that holds other files, or that is empty but the session cannot remove it.

  The table row at :131 currently bundles everything under "neither". It is split (Opus r5):
  - `uninstall`, `--recover-partial`, `ensure("uninstalled")` and `Host.uninstall()` keep "neither";
  - `otto run cleanup`, `ensure("clean")` and `Host.cleanup()` "remove its counters and empty dirs".
- **Fix:**
  - `docs/cli/run/defaults.md:113` and `:482-497` ("What `cleanup` leaves behind");
  - `docs/setup/defining-products-and-tools.md:201`;
  - `docs/setup/coverage.md:63-66`, where the placeholders no longer apply to uninstall;
  - the `cov_dir` path rule from §2 at its field docs.
- **`docs/setup/writing-tests.md`, at both `:~247` and `:~322`** (`ensure("clean", "installed")`): add the ruling. Under `--cov`, `clean` removes the counters collected so far, so a coverage suite marks `ensure("uninstalled", "installed")`.
- **Captures** (`docs/examples/captures.toml`):
  - `:461-467` (`qs-shell-sweep-cov-dirs`, "cleanup leaves each tool's cov_dir");
  - `:1143-1158` ("sweeps what otto leaves by design (… the root-owned cov_dir)").

  Both are updated and refreshed on the bed.
- **The root-owned parent.** docker creates the bind-mount parent (`/tmp/quickstart-docker`) as root. The sweep removes the empty `cov_dir` leaf but not the parent, which is outside it. That remaining trace is added to #838 as a comment, out of scope here. The capture sweep keeps removing it until then.
- **API snapshot.** Regenerate for `remove_coverage_dirs`, `gcda_remove_files`, `gcda_remove_dirs`, `OttoContext.collecting_coverage`, `sweep_stale_scans`, `cov_dir_root_owned`, the moved `instrumented_products`, `CleanReport.present_at_reset`, `clean_coverage(record_presence=)` and the `allowed` parameter, per the freshness rule in `check_breaking_marks`.

**Tests.**
- Unit, RED first:
  - **The uninstall placeholder.**
    - `{cov_dir}` in `uninstall` raises the named `ValueError` (not `KeyError`), with the hint, for both shell and `class=` entries.
    - `{{cov_dir}}` is accepted, and `{cov_dir}` in `install`/`check` is still allowed.
    - The valid list for uninstall omits it.
  - **The path rule.** Each of these is refused at load: a relative path, `..`, a trailing slash, `//tmp/cov`, `/`, `/tmp`, and two products with equal or nested `cov_dir`s.
  - **Loss warning, through the real lifecycle.** Across two complete invocations, a dir present at reset and gone at the fetch gives a WARNING. These stay DEBUG:
    - a dir absent at reset;
    - a probe `error`;
    - `--no-cov-clean`;
    - a standalone `cov get`;
    - a second invocation, which inherits no evidence from the first.

    The post-fetch reset neither probes nor overwrites the evidence, and `otto cov clean` doesn't probe.
  - **The sweep, run for real** under the local `sh` with GNU find, and under `busybox sh` with the dev VM's BusyBox `find` (which lacks `-delete` and must still pass), on a temp tree. Each case:
    - a tree holding only `.gcda` files is fully removed, and the probe printed them beforehand;
    - the counter reset now uses the same `-exec` files pass and works under BusyBox without `-delete` (RED today, #842);
    - an `rm` that fails leaves the file, and the postcondition re-probe reports it as an error, for cleanup and for the counter reset alike, through both the plain and the elevated reset path (RED: today's reset reports success);
    - a tree of more `.gcda` files than fit in one `ARG_MAX` command line (about 22,000 names of 200 bytes) is fully removed under BusyBox;
    - the empty-dir probe exits 0 with no output on a dir holding a user file, and an unreadable dir yields the `otto-ls-failed` marker and a probe error;
    - the exact-command expectations of the existing tests that pin `-delete` move to the new spelling: `tests/unit/host/test_product.py:599, :610, :654`, `test_docker_image_kind.py:407, :501` and `test_kmod_kind.py:137`. The `product.py:557, :569` docstrings are updated;
    - nested `.gcda` files and empty dirs are all removed, and the tree is gone;
    - a tree that also holds a user file keeps that file and its parent dirs, and counts as clean;
    - a symlinked `cov_dir` pointing at a dir of user files removes nothing;
    - an absent `cov_dir` is a no-op.
  - **Eligibility.** `None`-instrumented and embedded products are skipped.
  - **Elevation.**
    - The probe runs unelevated first. It re-runs elevated only on a non-zero exit, and only for a root-owned target. Without sudo, a readable root-owned dir still gets an answer.
    - Removal of docker and kmod targets runs elevated.
    - One test drives the reset, the removal and kmod's former `_run_sudo` callers through the core.
  - **Owner scope (mandatory, pure logic).** Cleanup of project A leaves project B's counters on the same host.
  - **`is_clean`.** It is false while counters or empty coverage dirs exist, and true when only non-coverage files remain. It refuses under a dry run, and raises when the elevated re-probe also fails.
  - **Coverage warning.** It is logged once per invocation while `collecting_coverage` is set. It is not logged for a plain `otto run cleanup` on a coverage lab, nor when an instruction read `ctx.cov` before an ordinary cleanup. A second library invocation inherits neither the flag nor the latch.
  - **Kept semantics.** Partial `--ensure` recovery keeps `cov_dir`. `otto cov clean`, the pre-run clear and the post-fetch clear delete `.gcda` files only.
  - **Preview.** The cleanup preview lists the step.
- Bed:
  - shell product on a Unix VM: run with `--cov`, then `otto run cleanup`. The `cov_dir` is gone and `is_clean` is true.
  - docker_image product on test3: the root-owned bind-mount source's counters and empty dirs are removed through sudo, and `is_clean` answers without sudo.

**Breaking footer.** `BREAKING CHANGE: a declared product's uninstall may no longer use {cov_dir} (refused at load; remove it — uninstall leaves coverage data alone). cov_dir must be an absolute, normalized path of at least two components, unique and un-nested per host (refused at load otherwise). otto run cleanup, and ensure("clean"), now delete each instrumented product's .gcda counters and the directories they leave empty, so coverage data no longer survives a cleanup: collect it (otto test --cov / otto cov get) before cleaning up, and mark coverage suites ensure("uninstalled") instead of ensure("clean").`

---

## Out of scope (filed as issues)

- #840: the kmod counter accumulation across a reinstall in one run, from #777's body. No reviewer found it blocks §4.
- #841: capture-time pairing versus the unit tier's image read (#782 follow-up).
- #836–#839.
- #785, which has its own spec.

## Review focus

- **#777 §4, races.** Cleanup removes `cov_dir` while another run on the same lab still collects. The bed lease and reservations make that a user error; the lifecycle home says so.
- **#777 §4, the elevated sweep.** The only deleting commands are `gcda_remove_files` (`-exec rm -f` on `*.gcda` regular files) and `gcda_remove_dirs` (`-exec rmdir`). A test asserts that no other delete is ever sent for a `cov_dir`, and that a user file under a misconfigured `cov_dir` survives a sudo sweep.
- **#782, the snapshot.**
  - `docker commit` of a large writable layer costs disk and time on the parent. It only happens for a stopped or paused container that missed the cache, with `--pause=false`. The pre-named tag is removed at teardown, or by the next scan's or reap's expiry sweep.
  - A product whose user cannot run `sh` fails the same way the exec path did, and maps to the same unavailable kind.
- **#783, an active container.** `docker rm` without `-f` refuses it, and install aborts with docker's message; no refusal strings are parsed.
- **#778, migration.** A user's config fails loudly at the first command after upgrading, and the message names the replacement precisely enough to fix it without the docs.
