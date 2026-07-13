# Monitor Plan 5b — live streaming

**Status:** design approved 2026-07-12, awaiting spec review
**Predecessors:** Plan 1 (format:1 contract), Plans 2–4 (review shell, charts, topology),
Plan 5a (sessionized producer — live runs become sessions; `otto monitor <source>` serves review mode)
**Successor:** Plan 5c (event marking)

## Goal

Live mode joins the new shell. A running `otto monitor --live` streams into the same
session-shaped store the review shell already renders from, so every view — charts,
fleet grid, series tree, topology, events, health — works live with no per-view work.
The legacy web data layer, which exists only to feed the retired Plotly dashboard, is
deleted.

## Architecture: one store, two modes

The store stops caring which mode it is in. Both modes **hydrate a format:1 payload and
then optionally grow it**:

```
review:  GET /api/monitor_sessions  -> frozen payload           -> render
live:    GET /api/monitor_sessions  -> snapshot of the run       -> render
         SSE /api/stream            -> fragments appended into the same store
```

`/api/document` currently 404s in live mode. It becomes `/api/monitor_sessions` and, in
live mode, serves `build_live_export()` — which Plan 5a already wrote and the live bed
already exercised. Live boot therefore reuses the *identical* hydration path review uses
today. **A live monitor session is just a session whose `end` is still open** — exactly
what a crashed session already looks like on disk, so no new shape enters the system.

### Naming: the monitor session

`document` was jargon and `session` alone collides with otto's CLI session
(`ensure_cli_session`, the per-invocation output dir). The domain term is **monitor
session**. The payload carries a *list* of them (a `--db` archive appends; live happens
to have one), so the payload is plural:

| Layer | Name |
| --- | --- |
| Endpoint | `GET /api/monitor_sessions` → `{format: 1, sessions: [...]}` |
| Python models | `MonitorSessionRecord`, `read_monitor_sessions()` |
| Web import/fetch | `importMonitorSessions(text)`, `rawMonitorSessions` |
| Web store array | `sessions[]` — **unchanged**, already correct |

This is a rename of code, endpoints and prose. **The on-disk format:1 payload keys
(`format`, `sessions`) do not change**, so existing archives stay readable and the schema
drift guard is unaffected.

## The stream speaks format:1

`/api/stream` today emits its own vocabulary (`{"type": "metric", ...}`) into the legacy
store. That store is being deleted, and **nothing else consumes those payloads** — so the
fragments are redefined to carry the *same field names as the payload they append to*.

A streamed point is a format:1 point; a streamed event is a format:1 event. Ingest becomes
an append, not a translation.

This is deliberate. Plan 5a lost three fix waves to `MonitorMeta.metrics` vs
`SessionMeta.charts` — a rename across a lenient boundary model that no type checker could
see and two reviews missed. The lesson recorded was *"any dump-then-validate across two
models must go through exactly one named mapping function."* The stronger move available
here is to **not have two models**. A test pins each fragment against the format:1 model,
so the wire cannot drift from the payload it appends to.

`/api/stream`'s payload shape is therefore a breaking wire change, whose only consumer is
deleted in the same change.

## Live view: follow, pause and range are one concept

The store already has `range: TimeRange | null`. Live reuses it rather than inventing
live-only view state.

| State | Meaning |
| --- | --- |
| `range === null` | **Follow the tail** — a rolling window (`windowMs`, default 15 min) that advances as points arrive |
| preset chosen | sets `windowMs`; still following |
| explicit from–to | **pins** the view; follow stops |
| **Pause** | freezes the view: snapshot the current derived window into an absolute range. Resume returns to follow |

So "paused" and "user picked a custom range" are the **same state** — one concept, one
indicator, no way for them to disagree.

**Pause is a view control, not a data control.** Ingestion never stops while paused, so
resuming catches up with no gap. This falls out of the keep-everything retention decision
below. Pause is live-only (UX spec §: "freezes the live view for inspection; gone in
historical").

## Chrome, reconnect and unreachable

Per the UX redesign spec (2026-07-05 §): `Live ●` green / `Reviewing ●` blue /
disconnected amber ("Connection lost — reconnecting…"); **the status dot never moves**.
Charts freeze while disconnected; auto-retry with capped exponential backoff.

**Reconnect re-fetches `/api/monitor_sessions`; it does not replay missed deltas.** A full
resync is provably correct and needs no sequence numbers, no server-side replay buffer and
no retention window: the snapshot *is* the truth, and it already contains whatever arrived
during the gap. Delta replay would be the only place in this design where client and
server could silently disagree about history.

**Unreachable dimming needs a clock, not events.** A host going silent produces *no* SSE
messages, so nothing would ever re-render it. A ticking `now` is what actually makes a host
go amber. The rule itself already exists — `healthForHosts()` marks `down` when
`gap > HEALTH_K × cadence` — so live and archive health come from one function, as the
format:1 spec intended ("the same function later drives live mode's unreachable dimming").
A drilled-in unreachable host shows last-known data, frozen and dimmed, with the
"Unreachable for 2m — showing last-known data" banner.

**The clock ticks at the collection interval, not at some fixed rate.** The down threshold
*is* `HEALTH_K × cadence`, so the cadence is the natural check rate: polling faster than the
collector cannot learn anything, because no new information can arrive between polls. A
fixed 1 Hz tick would be an arbitrary constant with no relationship to the data — and 5× to
60× more work than needed.

| | |
| --- | --- |
| Tick rate | the session's collection `interval`, read from `SessionMeta.interval` (Plan 5a persists it — fixing that was one of 5a's four bugs). Multi-cadence archives tick at the **fastest** host cadence, which bounds detection latency for all of them |
| No interval known | fall back to the cadence derived from samples; if that is null too, health is `unknown` and nothing needs to tick |
| Detection latency | a host shows `down` between `K × cadence` and `(K + 1) × cadence` — proportionate by construction: with 60s polling you cannot know sooner than 60s anyway |
| While paused | **liveness keeps ticking.** Pause freezes the chart view, not reality — a host can die while you are paused, and the fleet must say so |

This self-scaling tick also removes a knob: with the ≥1s interval floor below, a tick is at
most one O(hosts) lookup per second, so the "schedule a per-host timer at
`lastSampleAt + K × cadence`" refinement buys nothing and is dropped.

## The interval floor: minimum 1 second

A collection interval below one second is not meaningful in practice — a host must be given
time to answer every query in the interval without being taxed by the polling itself. So the
floor is enforced, loudly, rather than left as a footgun.

**One shared validator, applied where a *human* specifies an interval:**

| Boundary | Behaviour |
| --- | --- |
| `otto monitor --live --interval 0.5` | error, exit 2 |
| `OttoSuite.start_monitor(interval=0.5)` | raises |
| pytest plugin `--monitor-interval 0.5` | error |
| `MetricCollector(interval=0.01)` | **allowed** — the engine is unconstrained |

`MetricCollector` is deliberately exempt: it is the mechanism, not a human-facing knob, and
the monitor tests drive it at 0.01s / 0.05s / 0.2s against *fake* hosts to keep the suite
fast. Enforcing the floor there would force every collector test to spend real seconds per
tick, buying nothing — no real host is polled on that path. The floor guards every route by
which a real host can actually be hit.

Live also gains the **Export** affordance Plan 5a deferred. It serializes the in-memory
payload (no extra fetch); `/api/export/json` is retained as the scriptable download hook
documented in the guide.

## Retention: keep everything, downsample rendering

The store keeps **every** point, so the live payload stays truthful and Export gives the
whole run. Charts render through ECharts LTTB downsampling, so draw cost is flat in run
length.

| Run (7 hosts, ~90 series, 5s) | Points | Approx. heap |
| --- | --- | --- |
| 12h | ~780k | ~40 MB |
| 24h | ~1.5M | ~80 MB |
| 3 days | ~4.7M | ~250 MB — use `--db` and review the archive |

`--db` remains the durable record; a multi-day soak is meant to be reviewed from the
archive, not held in a tab. **Reducing what is collected in the first place** (host and
metric filters, with `[monitor]` defaults in `settings.toml`) is a separate plan — see
Non-goals.

## Performance: the two killers, and how they die

Two costs in the naive design are **O(total run length) per tick**, which is fine in a demo
and fatal at hour six:

| Stage | Naive | Why it hurts | After |
| --- | --- | --- | --- |
| Ingest (re-normalize the payload) | O(all) ≈ 780k | Re-normalizing the whole run every 5s | **O(batch) ≈ 90** |
| `healthForHosts` | O(all) ≈ 780k, **on every clock tick** | Scans every sample for last-sample + cadence — and the *clock* drives it, so it runs even when nothing arrives | **O(hosts) = 7** |
| `buildStackOption` | O(window × series) × every chart × every render | Built inline in JSX (`SubjectPage.tsx:138`); off-screen charts pay too | only charts whose series changed |
| `buildSeriesTree` | O(series) | Memoized on `session` **identity** — busts every tick | rebuilt only when a new series key appears |
| Topology rollup | O(hosts × series) | Recomputed per tick | O(elements), keyed on health |
| Zustand selectors | whole tree re-renders | New array identity per `setState` | only affected subscribers |

### Strategy: derived state on write + identity discipline

The memo keys in the shell are **object identities**, so the append strategy decides
whether every memo in the app survives a tick. Therefore:

1. **The append reducer maintains derived facts incrementally** — `lastSampleAt[host]`,
   `cadence[host]`, `seriesKeys`, the per-host metric index. Health becomes a *lookup, not
   a scan*: O(780k)/sec → O(7)/sec.
2. **Per-series structural sharing.** An append changes the identity of only the series it
   touched, so charts for untouched series keep their memo.
3. **`buildStackOption` moves out of JSX** into a `useMemo` keyed on
   `[seriesRefs, range, theme]`.
4. **The clock lives in its own store** (`useNow()`), ticking at the collection interval, so
   a tick re-renders only health-consuming components — not the whole tree.

A coarse `rev`-counter alternative was rejected: it leaves both O(total-run) costs intact,
which is precisely the problem.

### Proving it — budgets in CI, not a profiling session

"Profile it later" becomes never, and this failure is *gradual* (fine at minute five,
sluggish at hour six), so it gets tolerated rather than reported. It is therefore a
**budget guard**, modelled on the existing `tests/unit/import_budget/` guard.

| Tier | Measures | Runs | Passes if |
| --- | --- | --- | --- |
| **1. Scaling budget** (vitest) | `healthForHosts(now)`, `appendBatch()` against a synthetic session at 12h scale (~780k points) | CI, every push | **Cost is flat in run length** — the 12h session costs ~the same as the 1h one (bounded ratio), plus a generous absolute ceiling as a backstop |
| **2. Render-count guard** (vitest + RTL) | What a clock tick actually re-renders | CI, every push | 5 clock ticks increment the health tile's render count and leave **chart components' render count unchanged** |
| **3. Replay soak** (Playwright) | Real browser, real SSE, real ECharts, under a full run's worth of data | Marker-gated (nightly / on demand) | Frame budget intact, memory bounded, charts still interactive |

Tier 1 asserts the **shape, not the stopwatch** — cost *independent of run length*. Wall-clock
thresholds on a shared CI runner are noise; the thing that kills us is an O(total-run) term
hiding inside the clock tick, and only a ratio test catches that.

**Reaching hour six without waiting six hours.** The soak's job is to stress the *browser*,
so the load generator must not be the real collector — the ≥1s floor exists precisely to stop
us hammering real hosts, and a `--interval 0.1` soak would violate its own rationale. Instead
the harness **replays a Plan 5a live-bed archive through a fake producer at maximum rate**:
a full run's worth of real points (real PIDs, real bridges, real taps) arrives over SSE in
minutes, exercising server → browser → ECharts under load **without touching a VM**. The live
bed then validates the real collection path at a normal interval, as it always has.

**Decision rule, fixed now so it is not a judgment call later:** at 12h scale a clock tick
must cost **< 2 ms of main-thread work and re-render zero chart components**. **The tier-1 and
tier-2 tests are the decision** — if either fails, the append reducer is not maintaining its
derived facts and the fix is there, not in the timer.

## Deletions and ports

Deleted — the legacy layer exists only to feed the retired Plotly dashboard:

| File | Lines |
| --- | --- |
| `web/src/store.ts` | 274 |
| `web/src/plotly.ts` | 378 |
| `web/src/grouping.ts` | 250 |
| `web/src/events.ts` | 136 |
| `web/src/retirement.ts` | 93 → **ported, not deleted** |
| `web/src/api/sse.ts` | 58 |
| `web/src/api/client.ts` | 53 |
| `web/src/logevents.ts` | 52 |

…plus their vitest suites, and the server endpoints they alone consume: **`/api/meta` and
`/api/data`** (and the three wire-contract tests in `test_harness.py` that cover them).

**Ported:** `retirement.ts` → `web/src/data/retirement.ts`. It encodes the legacy
dashboard's fix for its #1 bug — every PID ever seen became a permanent chart trace, so
`proc/*` legends grew without bound. It is applied when building chart series in **both**
modes: a long *archive* has the same runaway-PID problem as a long live run, so review needs
it too. The logic is already pure and vitest'ed; its tests come across with it, retyped
against the format:1 models.

## Error handling

| Condition | Behaviour |
| --- | --- |
| SSE drops | amber "Connection lost — reconnecting…"; charts freeze; auto-retry with capped backoff; on reconnect, full resync via `GET /api/monitor_sessions` |
| Boot fetch fails (transport) | **silent** — leaves the static/air-gapped Import shell exactly as it was (Plan 5a's contract; this is what keeps docs capture and demo serving working) |
| Boot fetch returns 200 with an invalid body | surfaces the `importError` banner (Plan 5a's contract) |
| Host goes silent | derived `down` from `gap > HEALTH_K × cadence`; dimmed, last-known data retained, "Unreachable for Nm" banner |
| A fragment fails format:1 validation | drop the fragment, count it, surface once in the warnings channel — never crash the stream |

## Testing

- **vitest** — the append reducer (incremental, structurally shared); follow/pause/range;
  reconnect resync; the ported retirement policy; health under a live clock; the tier-1 and
  tier-2 budget guards above.
- **python** — `/api/monitor_sessions` in live mode (serves the live snapshot) and in review
  mode; SSE fragments validate against the format:1 models (the anti-drift pin); `/api/meta`
  and `/api/data` are gone.
- **dashboard (Playwright)** — a live harness: boot live, stream points, assert the chart
  grows; pause freezes it; disconnect shows amber and reconnect resyncs; an unreachable host
  dims.
- **interval floor** — the shared validator rejects `< 1s` at the CLI, `start_monitor()` and
  the pytest plugin, and leaves `MetricCollector` free (a test pins that the engine still
  ticks fast, so the suite is not silently slowed).
- **live bed** — `otto monitor --live` against tech1 at a normal interval; the browser soak
  rides the archive replay instead (see Performance).

## Non-goals

- **Event marking** (Mark-now, click/drag spans, edit-in-slide-over) — Plan 5c.
- **Collection scoping** — host *and metric* filters at launch, with `[monitor]` defaults in
  `settings.toml` and CLI overrides. `--hosts` filtering already exists; metric filtering and
  config defaults do not. It reduces load on the polled hosts, the archive and the browser
  alike, but it is orthogonal to streaming and gets its own plan.
- **Multi-day live sessions in one tab** — use `--db` and review the archive.

## Risks

| Risk | Mitigation |
| --- | --- |
| Derived state (`lastSampleAt`, `cadence`) drifts from the points it summarises | It is maintained in exactly one place (the append reducer) and pinned by a test that rebuilds it from scratch and compares |
| Identity discipline erodes; a future append replaces the session object and silently re-busts every memo | The tier-2 render-count guard fails when it does |
| Reconnect storms against a dead server | capped exponential backoff |
| The rename touches a lot of prose | Mechanical; the format:1 payload keys are unchanged, so no archive or drift guard is affected |
