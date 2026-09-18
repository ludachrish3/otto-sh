# Reviewing a capture

The positional `<SOURCE>` argument serves a previously captured run without
touching any hosts — no reservation gate, no collection, and no `--lab`:

```bash
otto monitor metrics.db
otto monitor metrics.json
```

That last point matters for a hand-carried archive: a teammate who receives
`metrics.db` can open it with the command above on a machine with no lab
configured anywhere — `SOURCE` is a self-contained document, and review mode
never resolves, loads, or even looks for a lab.

`SOURCE` must be a `.db` session archive written by `--live --db`, or a
`.json` export — either downloaded from a running dashboard's **⋯ →
Export**, or written by `otto test --monitor` (see [Monitoring during a
test run](during-tests.md#monitoring-during-a-test-run)). Anything else is a fast,
clear CLI error — there is no silent partial load:

- An **unrecognized suffix**, or a `.json`/`.db` that **doesn't parse as a
  `format:1` document**, exits **1** with a message naming what was
  expected.
- A **path that doesn't exist** exits **2** with a usage banner — the
  argument is validated before the command body runs, so it fails the same
  way any other bad invocation does.

**Breaking change, no migration.** A `.db`/`.json` written by an otto build
before sessions existed used a different, unversioned shape and is no
longer readable — `otto monitor` on one of those fails loud naming the
expected format rather than misrendering silently. There is no converter;
re-capture with the current build. The `GET /api/export/json` endpoint
changed the same way (it now emits this same `format:1` shape), which is a
breaking change for anything that scraped it directly. One narrower
caveat, specific to this feature's early rollout: a `.db` archive captured
by a pre-release build of `--live --db` (before its session metadata
persistence was corrected) replays with no chart specs and a null
interval — it looks like a valid archive but the dashboard renders it as
one ungrouped, unit-less chart per series. That has no migration either;
re-capture.

**Editing.** A `.db` session archive opened this way is editable — the
dashboard's marking controls write mutations straight back into the same
file. A `.json` export has nowhere to persist a mutation, so it stays
permanently read-only and those controls simply don't render for it; see
[Marking events](dashboard.md#marking-events).

