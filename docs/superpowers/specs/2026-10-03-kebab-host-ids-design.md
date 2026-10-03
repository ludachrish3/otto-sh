# Host ids are kebab-case end to end

**Date:** 2026-10-03
**Status:** approved in chat (Chris, 2026-10-03: "Single `-` seam, refuse collisions at load"; containers keep the dot notation with docker's own names; a `-` before the slot too); this document is the written form for review before planning.
**Origin:** first external-user feedback, `todo/user-feedback-design-items-2026-10-02.md` §4.1 — "Element names are normalized to kabob case, board names are normalized to snake case. All host ID portions should be normalized to kabob case." Chris: consistency now, while there is a single test user; a change later is too expensive.

## 1. Intent

Every id otto composes is kebab-case: lower-case letters, digits and single hyphens, nothing else. Every structural seam inside a host id — element to board, board to slot — is a `-`. A service on a host keeps docker's own names and the `.` seam that says "on": `test3.repo1.api`.

The feedback's premise was half right. Element and board are each slugged to kebab already; the `_` between them was the single non-kebab character, chosen so that `a-b` (one element) and `a` + `b` (element plus board) could not produce the same id. That protection moves from the delimiter to the loader: a collision is refused with both sources named.

## 2. The rule

### 2.1 Hosts otto names

`make_host_id` (`src/otto/host/remote_host.py`) stays the single composer; every portion is slugged and the portions are joined by `-`, the slot included:

| host | identity fields | id |
| --- | --- | --- |
| element only | `name="test1"` | `test1` |
| element + board | `name="test1"`, `board="bb"` | `test1-bb` |
| element + board + slot | `name="test1"`, `board="bb"`, `slot=0` | `test1-bb-0` |
| spaced or punctuated names | `name="Edge Router"`, `board="line.card"`, `slot=3` | `edge-router-line-card-3` |

The slot is a portion like the others (`bb-0`, not `bb0`): one rule, one seam, read the same way at every position. `slot` still never appears without a board. `slug()` is unchanged — it is the stability contract behind every id and nothing here needs it to change.

Invariant, asserted once for every id otto composes: `^[a-z0-9]+(-[a-z0-9]+)*$`.

### 2.2 Services docker names

A container host's id is unchanged: `f"{parent_id}.{project}.{service}".lower()` (`src/otto/host/docker_host.py`). The portions are docker's names, not otto's, and docker already guarantees what a slug would: a compose project name is `[a-z0-9][a-z0-9_-]*`, a service name `[a-zA-Z0-9._-]+`, a container name `[a-zA-Z0-9][a-zA-Z0-9_.-]+` — no whitespace, no `/`, no `:`. Because a project name cannot contain `.` and the parent id is kebab, the first two dots are an unambiguous seam even when a service name carries dots. Slugging these would rename what docker reports, and otto relays docker's identifiers exactly (spec `2026-10-02-docker-thin-honest-design.md` §1).

The `.lower()` stays: compose lower-cases project names itself, and otto's id has been lower-case since the container kind landed; this spec does not revisit it.

### 2.3 Everything derived

Nothing in `src/otto` parses a host id back into its parts; ids are opaque strings everywhere they travel — link ids (`make_link_id` hashes them), tunnel sentinels (`echo_sentinel` encodes them), preference selectors (`re.fullmatch(selector, host_id)` in `src/otto/host/capability.py`), coverage and run directories, the completion cache. They need no change of their own; they re-key because their inputs re-key (§4).

## 3. Collision: refusal, naming both

With one seam character, two different hosts can produce one id: element `a-b` without a board and element `a` with board `b`; element `a-b` + board `c` and element `a` + board `b-c`; element `a` + board `b-0` and element `a` + board `b` + slot `0`. The id stays opaque, so the only harm is two hosts under one key, and that is already refused:

- `Lab.add_host` (`src/otto/config/lab.py`) raises on an existing id;
- the lab merge raises `Duplicate host id … for different hosts` when the same id carries two different ips;
- the composite source raises `host id 'X' in lab 'Y': element … collides with element …` across sources.

What changes is the `add_host` message, which today names only the id. It names both declarations — each side's element name, board, slot and ip (no source file is stamped on a host; the loader's own prefix names the file of the entry being added) — so the user sees which two to tell apart:

```text
LabRepositoryError: host id 'a-b' in lab 'bench': element 'a-b' (10.10.200.11)
collides with element 'a' board 'b' (10.10.200.12). Give the elements
distinct names, or set board/slot.
```

A container registered by `otto docker compose up` goes through the same `add_host`; a collision with a declared host names the container as `project/service on <parent>` against the host's declaration. Nothing is auto-suffixed and no id is adjusted to dodge a collision: otto never invents identity.

## 4. What re-keys, and the cutover

Hard cutover. No alias, no compatibility mapping, no warning that recognises the old shape — a removed form is simply gone, the same rule as a removed flag.

For a user of 0.16.x:

- `otto host <id>` arguments and TAB completion offer and accept the new ids only.
- `[host_preferences."<selector>"]` regexes that spelled the `_` seam (`test1_.*`) match nothing; the selector docs say the seam is `-`.
- Link ids (`lnk-<12 hex>`) are hashes over host ids, so every recorded static route id changes; a lab's declared links are re-derived at load, so nothing needs editing unless a link id was written down elsewhere.
- Per-host artifact directories (`<cov_dir>/<host>/…`, run and monitor records keyed by host id) begin afresh under the new names; old ones are not migrated or deleted.
- Tunnel sentinels carry ids but never persist across a run; nothing to migrate.

## 5. Documentation

- `docs/configuration/lab-config.md` "Host identity & naming" is the one home for the rule: `-` at every seam, the four-row table, the collision refusal and its message; the container form in one sentence pointing at the docker pages.
- Every example id in `docs/` that spells the `_` seam or a glued slot is rewritten (`test_top0` → `test-top-0`, `chassis_slot2` → `chassis-slot-2`, `router_linecard3` → `router-linecard-3`, …); board-name examples that are not ids (`mps2_an385` as a Zephyr board string) are untouched because a board is data and its slug is what reaches the id.
- Where selectors are documented (`host-options.md`), one clause: the seam is `-`.
- `otto init` templates and `lab_data/README.md` examples follow.

## 6. Honesty: the tests

- `make_host_id` unit cases for the four rows of §2.1, plus the `-` seam pinned by an element whose name itself carries `-`, and slot `0` rendered as `-0` (a falsy slot is still a slot).
- The invariant: one test builds every host from the shipped lab fixtures (`tests/_fixtures/labdata.py`) and the e2e lab data and asserts every id matches `^[a-z0-9]+(-[a-z0-9]+)*$`; container ids from the docker fixtures match `^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9_-]+\.[a-z0-9._-]+$`.
- The collision refusal: a lab with element `a-b` and element `a` + board `b` fails the load with both declarations named, in `add_host` and in the composite source; a container colliding with a declared host is refused naming both.
- A differential that every id otto prints (`otto --list-hosts`, `--show-lab`, `otto host <TAB>`, `otto docker ps`) is the id the lab holds — the existing CLI/list tests re-pinned to the new shape.
- The ~27 test files that spell `_`-ids migrate; no test keeps an old-shape id alive.

## 7. Out of scope

- Any change to `slug()`.
- Lower-casing or slugging docker names.
- Migrating or deleting old per-host artifact directories.
- The product-variant and custom-product designs (§2.1/§2.2 of the todo); they consume ids and are unaffected.
