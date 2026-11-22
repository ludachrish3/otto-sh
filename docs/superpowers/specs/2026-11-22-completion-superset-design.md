# Completion never hides a host `--lab` loads (#1185)

**Status:** revision 3, 2026-11-22.

r3 follows the code reviews: the JSON fold keeps main's same-machine decision
unchanged (an attempt to suppress a repeated drop hid a real one), and an
unusable pattern is skipped alone.

r2 follows the Astra and Fable checks of r1. It drops every change to a
summary's address (they needed the same decision in five places and a change
to the backend conformance kit, for a heuristic), and replaces "apply the
patterns once more in two collectors" with one process-wide function that
every by-lab reader calls.

## Why

`--lab X` decides which hosts are in a lab by composing every active project's
sources. Completion (TAB, the cached `hosts_by_lab`) decides it again from
host summaries, one project at a time. Issue #1185 lists eleven inputs where
the two disagree.

Making completion compute exactly what dispatch computes needs either a real
load of every lab at cache rebuild (it builds hosts, runs product providers,
re-reads every file per lab, and raises on the first strict failure) or a new
shared membership layer with a new backend capability. Both add machinery to
serve inputs few users have.

## Ruling

Chris, 2026-11-22: no new mechanism. Completion is a hint and dispatch is the
only judge. The rule:

> Completion may offer a lab or a host that `--lab` then refuses. It must not
> leave a host out of a lab that `--lab` loads it in.

The thread-based deadline of the cache rebuild stays as it is.

The rule is about host ids under a lab. It is not a promise about the
suggestions derived from other summary fields (see "Left as it is").

## Non-goals

- Exact parity. An unavailable lab, a lab whose scoped source failed, and a
  merge dispatch refuses are still offered; dispatch answers with its reason.
- Any new backend capability, any change to `load_lab`, to the backend
  conformance kit, to the deadline, to the drops, to the cache payload's shape
  or to a warm TAB's cost.
- `--list-labs` building each project with its own profiles (already
  documented).

## Design

Two changes, both inside the existing summaries path.

### 1. One id, two machines, in one JSON source

`JsonLabRepository.list_host_summaries_in_view` folds two records that derive
one id. Today a record that is a DIFFERENT machine is dropped whole, so its
labs never reach the summary and the host is not offered under them.

Now the different machine's labs and lab patterns fold into the kept summary
exactly as a same-machine record's do. Every other field of the kept summary
stays the first record's. Which records count as the same machine is decided
exactly as before (only a same-machine record joins the identities a later
record is compared against), so every drop main records is still recorded,
naming both declarations; a record that restates the different machine is one
more drop.

### 2. One process-wide view of the summaries

Completion builds one composite per project, so a summary's patterns are
resolved only against the labs its own project declares, and each by-lab
reader folds the projects its own way (`_summaries_by_lab` keeps the first
project's summary for an id; `collect_host_ids` and `collect_host_ids_by_lab`
each loop the projects themselves).

One private function in `completion_cache.py` now answers "every host summary
of this process":

- it reads each project's summaries and each project's declared lab names from
  the calls the collectors already make (`project_host_summaries`,
  `_project_lab_names`), so it adds no read and no deadline of its own;
- one summary per id: the first project's fields, with the labs and lab
  patterns of every project's summary of that id unioned in first-seen order;
- each summary's patterns are then resolved against the union of every
  project's declared names, by the same function `_merged_summary` uses
  (extracted from it, so patterns are resolved by one piece of code in the
  composite and here);
- a memberless summary stays, memberless;
- when the process inventory could not be resolved, it returns what the
  collectors return today (nothing), without reading names.

`_summaries_by_lab` (the docker default parent per lab), the lab filter of
`collect_host_ids` and the buckets of `collect_host_ids_by_lab` all read it.
Resolving patterns never adds a lab NAME: patterns match declared names only,
and `collect_lab_names` is unchanged.

The pattern step runs in the collector, outside the per-project deadline. It
is `re.fullmatch` of the hosts' patterns over the declared names, the work the
composite already does per project; an uncompilable pattern is skipped as
there.

### Cache

The payload's shape does not change, but a cache written before this change
holds the narrower buckets for up to its lifetime. The schema stamp is bumped
(in `completion_cache.py` and in the shim's copy) so the first TAB after an
upgrade rebuilds.

## The rows of #1185

| Row | After this change |
|---|---|
| 3 | fixed (1): `dut` is offered under `spare` |
| 5 | labs unioned across projects (2); the address tunnel narrowing uses is still one project's: documented |
| 6 | fixed (2) |
| 4, 8 | labs were already unioned; unchanged |
| 7 | unchanged: a backend's own summaries decide; documented |
| 1, 2, 10 | offered, and dispatch refuses with its reason: documented |
| 9 | already refused for Jsonnet sources; another scoped backend over-offers: documented |
| 11 | unchanged, already documented |

## Left as it is, and said so in the docs

- **A summary's other fields for an id that names two machines** (`ip`,
  `docker_capable`, `docker_priority`, `os_type`, `logins`) are one record's.
  They feed suggestions: the `/24` neighbour narrowing of `otto tunnel`, the
  default docker parent and its container ids, `--user`. For such an id a
  suggestion can be narrower or wider than dispatch. Blanking the address was
  considered and dropped: it needs the same decision in the JSON fold, the
  composite merge, the load fallback, the example backend and the tunnel
  completer, and a looser conformance rule, for an input (one id, two
  machines) that is rare and a narrowing that is already a heuristic.
- **A names read that times out** leaves that project's labs, and other
  projects' hosts under them, out until the next rebuild. That is the
  deadline's own limit.

## Docs

`docs/architecture/subsystems/completion-cache.md` states the rule once, with
what completion may over-offer and the two limits above.
`docs/extend/lab-source-backends.md` (the summaries section) links to it.
Docstrings and pages that promise "an id offered by completion is one that
dispatches" are corrected to the rule.

## Compatibility

Additive for users: completion offers a host under more labs, never fewer. No
public signature changes; the API snapshot is untouched (the new function and
the extracted pattern function are private). Not a breaking change.

## Tests

- The property, in `tests/unit/labs/test_host_summaries_equivalence.py`: for
  a corpus that includes the shapes of rows 3 and 6 (and a second project),
  every host `--lab X` loads is in completion's hosts for `X`, for every
  available lab, through `collect_host_ids_by_lab` and through
  `collect_host_ids(lab_names=[X])`. The existing assertions that pin "the
  first record wins" and the dropped second machine's labs move to the new
  behaviour; nothing else in that file is weakened.
- Unit, the JSON fold: labs and patterns unioned; the other fields the first
  record's; one drop per differing record (a restated second machine, a third
  machine).
- Unit, the process-wide function: labs unioned across projects; a pattern
  reaching a lab only another project declares; a memberless host kept; an
  unresolved inventory; the three readers agreeing; the docker default parent
  when the union brings two equally ranked capable hosts into one lab (none,
  as dispatch decides).
- Unit: the schema stamp is the same in both copies and an older cache is
  rebuilt.
- The existing budgets hold unchanged:
  `test_completion_io_does_not_scale_with_corpus_size` and
  `test_cold_rebuild_does_not_scale_with_corpus_size`.
- No bed test: nothing here reaches a host.

## Landing

One pull request: this spec, then one commit, `fix(completion): …` with
`Fixes #1185`.
