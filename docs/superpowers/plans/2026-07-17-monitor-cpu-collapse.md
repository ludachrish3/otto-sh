# Monitor CPU Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove per-PID CPU tracking and collapse Overall + per-core CPU into a single `/proc/stat`-sourced `"CPU"` chart, and promote the per-chart series cap to a first-class `MetricParser.max_series` API.

**Architecture:** Backend `PerCoreCpuParser` absorbs overall CPU from the `/proc/stat` aggregate line (deleting `TopCpuParser` and the `top` command); a new `max_series` attribute flows parser → `ChartSpec` → wire → frontend, where the CPU chart opts out of the cap (`None`) and the render layer mutes per-core lines into one band past the 8-color palette. All per-process (`proc/*`) awareness is deleted outright.

**Tech Stack:** Python 3 / Pydantic (backend, `src/otto/monitor`, `src/otto/models`), React + TypeScript + ECharts (frontend, `web/`), pytest (backend tests), vitest (frontend tests). Wire TS types are generated from Pydantic via `scripts/gen_web_types.sh`.

## Global Constraints

- **Dev-VM load:** NEVER run the full suite / `make coverage` / `nox` / `pytest -n auto` on this VM (standing rule: no heavy or parallel test load). Every per-task test command below is deliberately scoped to one file or node id. The one full gate (`make web`, and optionally `make coverage`) runs once at the end and is the human's call to time.
- **Commits:** self-commit is allowed in this worktree. Use a conventional-commit subject and end every commit body with the trailer `Assisted-by: Claude (Opus 4.8)`.
- **No `from __future__ import annotations`** anywhere (it breaks Sphinx `-W`).
- **Never widen a cap by cycling the CVD palette** — the 8 colors in `web/src/charts/palette.ts` are validated and ordered; do not add a 9th or reorder. High-core CPU charts use the muted band, not more colors.
- **Wire types are generated, not hand-edited:** after any change to `ChartSpec`/`MonitorMeta`/`MonitorExport` fields, regenerate `web/src/api/{types,export}.gen.ts` via `scripts/gen_web_types.sh` and commit the result; `make web` diffs them and fails on drift.
- **Worktree:** all work happens in `/home/vagrant/otto-sh/.claude/worktrees/worktree-monitor-cpu-collapse` on branch `worktree-monitor-cpu-collapse`. The spec is committed at `docs/superpowers/specs/2026-07-17-monitor-cpu-collapse-design.md`.

---

## File Structure

**Backend (modified):**
- `src/otto/models/monitor.py` — `DEFAULT_MAX_SERIES_PER_CHART` constant + `ChartSpec.max_series` field.
- `src/otto/monitor/parsers.py` — delete `TopCpuParser`; extend `PerCoreCpuParser`; `MetricParser.max_series`; `DEFAULT_PARSERS`; remove `ParseContext.core_count`.
- `src/otto/monitor/collector.py` — `get_meta_model` emits `max_series`; remove the core-count probe/field/arg.

**Frontend (modified/deleted):**
- `web/src/api/types.gen.ts`, `web/src/api/export.gen.ts` — regenerated.
- `web/src/data/seriesTree.ts` — `ChartNode.maxSeries` from the spec.
- `web/src/pages/SubjectPage.tsx` — remove retirement; per-chart cap; hybrid coloring.
- `web/src/charts/options.ts` — `SeriesInput.muted`; `ChartTheme.mutedSeries`; muted styling.
- `web/src/charts/palette.ts` — muted-series color constants.
- `web/src/data/retirement.ts` — **deleted**.
- `web/src/data/exportDoc.ts` — stale comment.

**Tests/fixtures (modified/deleted):**
- `tests/unit/monitor/test_parsers.py`, `tests/unit/monitor/test_export_producer.py`, `tests/unit/monitor/test_collector_db.py` — drop `TopCpuParser`, retest `PerCoreCpuParser`, cap plumbing.
- `tests/_fixtures/_fake_collector.py`, `tests/e2e/monitor/dashboard/conftest.py`, `tests/e2e/monitor/dashboard/test_live_shell.py` — fixtures scrubbed of `proc/*` and the `top` command key.
- `web/src/__tests__/retirement.data.test.ts`, `web/src/__tests__/subjectpage.retirement.test.tsx` — **deleted**; `web/src/__tests__/perf_budget.test.ts`, `web/src/__tests__/importexport.livefragment.test.ts` — de-proc'd; new cap + coloring tests.

**Docs (modified):**
- `docs/guide/monitor.md`, `docs/architecture/subsystems/monitoring.md`.

---

## Task 0: Worktree environment setup

**Files:** none (environment only).

- [ ] **Step 1: Sync Python deps**

Run: `cd /home/vagrant/otto-sh/.claude/worktrees/worktree-monitor-cpu-collapse && uv sync`
Expected: resolves and installs; exit 0.

- [ ] **Step 2: Install web deps**

Run: `cd web && npm ci`
Expected: installs `web/node_modules`; exit 0.

- [ ] **Step 3: Sanity-import otto (no heavy tests)**

Run: `uv run python -c "import otto.monitor.parsers, otto.monitor.collector; print('ok')"`
Expected: prints `ok`.

No commit (environment only).

---

## Task 1: Per-chart cap API (backend, additive)

Add the `max_series` knob end-to-end with the numeric default, so nothing changes behavior yet (every chart stays capped at 8). Regenerate the wire types so `max_series` is on the contract for later frontend tasks.

**Files:**
- Modify: `src/otto/models/monitor.py` (add constant + `ChartSpec.max_series`)
- Modify: `src/otto/monitor/parsers.py` (`MetricParser.max_series`, import constant)
- Modify: `src/otto/monitor/collector.py` (`get_meta_model` passes `max_series`)
- Modify: `web/src/api/types.gen.ts`, `web/src/api/export.gen.ts` (regenerated)
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Produces: `DEFAULT_MAX_SERIES_PER_CHART: int = 8` (in `otto.models.monitor`); `ChartSpec.max_series: int | None`; `MetricParser.max_series: int | None`. `None` = uncapped, a positive int = cap.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/monitor/test_parsers.py` inside `class TestMetricParserExtensibility` (after `test_custom_parser_works`):

```python
    def test_default_max_series_is_the_numeric_default(self):
        from otto.models.monitor import DEFAULT_MAX_SERIES_PER_CHART

        class Plain(MetricParser):
            y_title = "x"
            unit = ""
            command = "echo x"
            chart = "x"

            def parse(self, output, *, ctx):
                return {}

        assert Plain().max_series == DEFAULT_MAX_SERIES_PER_CHART

    def test_parser_can_opt_out_of_the_cap(self):
        class Uncapped(MetricParser):
            y_title = "x"
            unit = ""
            command = "echo y"
            chart = "y"
            max_series = None

            def parse(self, output, *, ctx):
                return {}

        collector = MetricCollector(hosts=[], parsers=[Uncapped()])
        spec = next(c for c in collector.get_meta_model().metrics if c.chart == "y")
        assert spec.max_series is None
```

Add `from otto.monitor.collector import MetricCollector` to the test file's imports if not present (check the top of the file first).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestMetricParserExtensibility -q`
Expected: FAIL — `AttributeError: ... 'max_series'` / `ImportError: cannot import name 'DEFAULT_MAX_SERIES_PER_CHART'`.

- [ ] **Step 3: Add the constant and model field**

In `src/otto/models/monitor.py`, above `class ChartSpec` (near the top of the module's monitor section), add:

```python
DEFAULT_MAX_SERIES_PER_CHART = 8
"""Default per-chart series cap. A chart shows at most this many series (the
frontend truncates the rest with an overflow note). A parser sets
``max_series = None`` to opt its chart out of the cap entirely."""
```

Then add to `class ChartSpec` (after the `interval` field):

```python
    max_series: int | None = DEFAULT_MAX_SERIES_PER_CHART
```

(The default is the numeric cap, not `None`, so a read-back record missing the field is default-capped, never silently uncapped. `ChartSpecRecord` subclasses `ChartSpec`, so it inherits the field automatically.)

- [ ] **Step 4: Add the parser attribute**

In `src/otto/monitor/parsers.py`, add to the imports from `otto.models.monitor` (find the existing `from otto.models.monitor import ...` line):

```python
from otto.models.monitor import DEFAULT_MAX_SERIES_PER_CHART
```

Then add to the `class MetricParser` body (near the other class attributes like `chart`/`tab`):

```python
    max_series: int | None = DEFAULT_MAX_SERIES_PER_CHART
    """Per-chart series cap for this parser's chart; ``None`` = uncapped. See
    :data:`otto.models.monitor.DEFAULT_MAX_SERIES_PER_CHART`."""
```

- [ ] **Step 5: Emit `max_series` from `get_meta_model`**

In `src/otto/monitor/collector.py`, in `get_meta_model`, add `max_series` to the `ChartSpec(...)` construction (the list comprehension building `metrics`). Add this line alongside `interval=getattr(v, "interval", None)`:

```python
                max_series=getattr(v, "max_series", DEFAULT_MAX_SERIES_PER_CHART),
```

Add the import at the top of `collector.py` (with the other `otto.models.monitor` imports):

```python
from otto.models.monitor import DEFAULT_MAX_SERIES_PER_CHART
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestMetricParserExtensibility -q`
Expected: PASS.

- [ ] **Step 7: Regenerate the wire TS types**

Run: `scripts/gen_web_types.sh`
Then: `git diff --stat web/src/api/types.gen.ts web/src/api/export.gen.ts`
Expected: both files change — a `MaxSeries` type and `max_series?: MaxSeries` on the chart-spec interfaces.

- [ ] **Step 8: Commit**

```bash
git add src/otto/models/monitor.py src/otto/monitor/parsers.py src/otto/monitor/collector.py \
        web/src/api/types.gen.ts web/src/api/export.gen.ts tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): per-chart series cap via MetricParser.max_series

Add DEFAULT_MAX_SERIES_PER_CHART + ChartSpec.max_series (None = uncapped),
emitted per view from get_meta_model and carried on the generated wire types.
Additive: every chart still defaults to a cap of 8.

Assisted-by: Claude (Opus 4.8)"
```

---

## Task 2: Backend CPU consolidation onto `/proc/stat`

Atomic backend change: `PerCoreCpuParser` absorbs Overall CPU, `TopCpuParser` and the `core_count` probe are deleted, and every Python consumer of `TopCpuParser` is migrated in the same commit so the backend suite stays green.

**Files:**
- Modify: `src/otto/monitor/parsers.py` (extend `PerCoreCpuParser`; delete `TopCpuParser`; `DEFAULT_PARSERS`; remove `ParseContext.core_count`)
- Modify: `src/otto/monitor/collector.py` (remove core-count probe/field/arg + comment)
- Modify: `src/otto/monitor/export.py` (stale `TopCpuParser` comment at line 252)
- Modify: `tests/unit/monitor/test_parsers.py` (delete `TestTopCpuParser`, retest `PerCoreCpuParser`, extend `_proc_stat_output`, de-`core_count` the `ParseContext` tests, delete `_top_output`)
- Modify: `tests/unit/monitor/test_export_producer.py` (swap `TopCpuParser` → `PerCoreCpuParser`)
- Modify: `tests/unit/monitor/test_collector_db.py` (swap `TopCpuParser` → `PerCoreCpuParser`)
- Modify: `tests/_fixtures/_fake_collector.py` (`CHART_COMMANDS["cpu"]`)
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (drop `proc/*`, push cores)
- Modify: `tests/e2e/monitor/dashboard/test_live_shell.py` (docstring only)

**Interfaces:**
- Consumes: `PerCoreCpuParser` (from Task nothing — pre-existing), now emits `"Overall CPU"` + `"core <N>"` on chart `"CPU"` with `max_series = None`.
- Produces: `DEFAULT_PARSERS` no longer contains the `top -d 0.5 -bn2` key. `ParseContext` no longer has `core_count`. `TopCpuParser` no longer exists.

- [ ] **Step 1: Rewrite the `PerCoreCpuParser` tests (failing)**

In `tests/unit/monitor/test_parsers.py`, first extend the `_proc_stat_output` helper (currently at ~line 105) to accept a varying aggregate line:

```python
def _proc_stat_output(
    cores: list[tuple[int, int]], aggregate: tuple[int, int] | None = None
) -> str:
    """cores = [(busy_jiffies_excluding_idle, idle_plus_iowait_jiffies), ...].

    Emits the aggregate 'cpu' line plus one cpuN line per core:
    user nice system idle iowait irq softirq steal. When *aggregate* is given
    as (busy, idle) the 'cpu' line reflects it (so a two-tick call drives
    Overall CPU); otherwise a constant placeholder aggregate is emitted (zero
    delta -> no Overall CPU point).
    """
    if aggregate is None:
        agg_line = "cpu  99999 0 99999 999999 9999 0 0 0 0 0"
    else:
        busy, idle = aggregate
        user, system = busy // 2, busy - busy // 2
        idle_j, iowait = idle // 2, idle - idle // 2
        agg_line = f"cpu {user} 0 {system} {idle_j} {iowait} 0 0 0 0 0"
    lines = [agg_line]
    for n, (busy, idle) in enumerate(cores):
        user, system = busy // 2, busy - busy // 2
        idle_j, iowait = idle // 2, idle - idle // 2
        lines.append(f"cpu{n} {user} 0 {system} {idle_j} {iowait} 0 0 0 0 0")
    lines += ["intr 12345", "ctxt 6789", "procs_running 3", "procs_blocked 1"]
    return "\n".join(lines) + "\n"
```

Then, in `class TestPerCoreCpuParser`, DELETE `test_aggregate_cpu_line_skipped` and add:

```python
    def test_chart_is_cpu(self):
        assert PerCoreCpuParser().chart == "CPU"

    def test_uncapped(self):
        assert PerCoreCpuParser().max_series is None

    def test_overall_cpu_from_aggregate_deltas(self):
        parser = PerCoreCpuParser()
        parser.parse(
            _proc_stat_output([(100, 900)], aggregate=(100, 900)), ctx=ParseContext()
        )
        points = parser.parse(
            _proc_stat_output([(130, 970)], aggregate=(150, 950)), ctx=ParseContext()
        )
        # aggregate delta: Δtotal=100, Δidle=50 -> 100*(1-50/100) = 50%
        assert points["Overall CPU"].value == 50.0
        assert points["core 0"].value == 30.0

    def test_overall_cpu_absent_when_aggregate_is_flat(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(100, 900), (100, 900)]), ctx=ParseContext())
        points = parser.parse(_proc_stat_output([(130, 970), (180, 920)]), ctx=ParseContext())
        assert "Overall CPU" not in points  # constant placeholder aggregate -> zero delta
        assert set(points) == {"core 0", "core 1"}
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestPerCoreCpuParser -q`
Expected: FAIL — `test_overall_cpu_from_aggregate_deltas` KeyError on `"Overall CPU"`, `test_uncapped`/`test_chart_is_cpu` may already pass or fail on `max_series`.

- [ ] **Step 3: Extend `PerCoreCpuParser`**

In `src/otto/monitor/parsers.py`, replace the `PerCoreCpuParser` class body (docstring, `chart`, and `parse`) with:

```python
class PerCoreCpuParser(MetricParser):
    """Overall + per-core busy % from ``/proc/stat`` jiffies deltas.

    One cheap ``cat /proc/stat`` read yields both the aggregate ``cpu`` line
    (charted as ``Overall CPU``) and each ``cpuN`` line (``core N``); all land
    on one ``"CPU"`` chart. busy% = 100 x (1 - Δ(idle+iowait)/Δtotal) per line.
    Jiffies ratios need no wall clock (time cancels), so state is plain previous
    counters, keyed by the numeric core suffix (the aggregate keys under "").

    ``max_series = None`` uncaps the CPU chart so every core shows regardless of
    core count (the frontend mutes the per-core lines into one band past the
    palette size).
    """

    y_title = "Usage %"
    unit = "%"
    command = "cat /proc/stat"
    tab = "cpu"
    tab_label = "CPU"
    chart = "CPU"
    max_series = None

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}  # core -> (total, idle_all)

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            fields = line.split()
            if not fields or not re.fullmatch(r"cpu\d*", fields[0]) or len(fields) < 9:  # noqa: PLR2004 — cpu/cpuN rows carry 8 jiffies fields
                continue
            try:
                jiffies = [float(f) for f in fields[1:9]]
            except ValueError:
                continue
            total, idle_all = sum(jiffies), jiffies[3] + jiffies[4]
            core = fields[0].removeprefix("cpu")
            prev = self._prev.get(core)
            self._prev[core] = (total, idle_all)
            if prev is None:
                continue
            d_total, d_idle = total - prev[0], idle_all - prev[1]
            if d_total <= 0 or d_idle < 0:
                continue  # counter reset — re-baseline, skip the tick
            label = "Overall CPU" if core == "" else f"core {core}"
            result[label] = MetricDataPoint(round(100.0 * (1 - d_idle / d_total), 2))
        return result
```

- [ ] **Step 4: Run the `PerCoreCpuParser` tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestPerCoreCpuParser -q`
Expected: PASS.

- [ ] **Step 5: Delete `TopCpuParser` and its default-registry entry**

In `src/otto/monitor/parsers.py`:
- Delete the entire `class TopCpuParser(MetricParser): ...` (the `top -d {delay} -bn2` parser).
- In `DEFAULT_PARSERS`, remove the `TopCpuParser(),` line.
- In the `ParseContext` dataclass, remove the `core_count: int = 1` field and its docstring line (the one referencing "TopCpuParser uses it to normalize per-process CPU%").
- Remove `TopCpuParser` from the `TYPE_CHECKING` import block (line ~17: `from otto.monitor.parsers import DEFAULT_PARSERS, TopCpuParser, register_host_parsers`).

- [ ] **Step 6: Remove the core-count probe from the collector**

In `src/otto/monitor/collector.py`:
- Remove the `core_count: int = field(default=1)` field from `MonitorTarget`.
- Remove the `grep -c ^processor /proc/cpuinfo` probe block (the code around line ~400-419 that sets `target.core_count`), including the leading comment referencing `TopCpuParser`.
- Change the `ParseContext(core_count=target.core_count, ts=ts)` call (line ~355) to `ParseContext(ts=ts)`.

- [ ] **Step 7: De-`core_count` the ParseContext tests and delete `_top_output`/`TestTopCpuParser`**

In `tests/unit/monitor/test_parsers.py`:
- Delete the entire `class TestTopCpuParser` (all its methods).
- Delete the `_top_output(...)` helper (no longer referenced).
- Remove `TopCpuParser` from the module imports (the `from otto.monitor.parsers import (...)` block).
- Update `test_parse_context_is_frozen` — change `ParseContext(core_count=4)` to `ParseContext()` and change the frozen-attr assignment from `ctx.core_count = 8` to `ctx.ts = None  # type: ignore[misc]` (assign a real remaining field):

```python
def test_parse_context_is_frozen():
    from datetime import datetime, timezone

    ctx = ParseContext(ts=datetime(2026, 7, 3, tzinfo=timezone.utc))
    with pytest.raises(FrozenInstanceError):
        ctx.ts = None  # type: ignore[misc]
```

- Update `test_parse_context_carries_optional_ts` — remove `core_count=2` from the `ParseContext(...)` call:

```python
    assert ParseContext(ts=ts).ts == ts
```

- [ ] **Step 8: Migrate the remaining Python consumers**

- `tests/unit/monitor/test_export_producer.py`: change the import `from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext, TopCpuParser` to use `PerCoreCpuParser` instead of `TopCpuParser`; replace both `MetricCollector(hosts=[], parsers=[TopCpuParser()])` occurrences (lines ~104, ~135) with `MetricCollector(hosts=[], parsers=[PerCoreCpuParser()])` (both tests only rely on the parser's chart being `"CPU"`, which holds). Update the comment at line ~24 that describes `TopCpuParser` "Overall CPU"/"proc/<pid>" behavior to describe `PerCoreCpuParser` ("Overall CPU"/"core <N>").
- `tests/unit/monitor/test_collector_db.py`: change import `from otto.monitor.parsers import MemParser, TopCpuParser` → `from otto.monitor.parsers import MemParser, PerCoreCpuParser`; change `parsers=[TopCpuParser(), MemParser()]` (line ~76) → `parsers=[PerCoreCpuParser(), MemParser()]` (points are injected manually via `_inject_point`, so parse behavior is irrelevant).
- `tests/_fixtures/_fake_collector.py`: change `"cpu": "top -d 0.5 -bn2",` in `CHART_COMMANDS` to `"cpu": "cat /proc/stat",`.
- `tests/e2e/monitor/dashboard/test_live_shell.py`: in the `_push_tick` docstring (line ~42-46), replace the `TopCpuParser` reference with `PerCoreCpuParser` (chart `"CPU"`); the test body is unchanged.

- [ ] **Step 9: Update the dashboard preload fixture**

In `tests/e2e/monitor/dashboard/conftest.py`, delete the `_PROC_META` constant (lines ~167-174) and rewrite `_preload` (lines ~177-188):

```python
def _preload(harness: DashboardHarness[FakeCollector]) -> None:
    """Three 5s-spaced ticks for two hosts: overall + per-core CPU, memory, load."""
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=15)
    push = harness.collector.push
    for tick in range(3):
        ts = t0 + timedelta(seconds=5 * tick)
        for host in ("host1", "host2"):
            harness.run(push(host, "Overall CPU", 20.0 + tick, ts=ts))
            harness.run(push(host, "core 0", 15.0 + tick, ts=ts))
            harness.run(push(host, "core 1", 25.0 + tick, ts=ts))
            harness.run(push(host, "Memory Usage", 40.0 + tick, chart="memory", ts=ts))
            harness.run(push(host, "Load (1m)", 0.5 + tick, chart="load", ts=ts))
```

- [ ] **Step 10: Update the stale `export.py` comment**

In `src/otto/monitor/export.py` (~line 252), update the comment that references `TopCpuParser` emitting `"Overall CPU"` and `"proc/<pid>"` to reference `PerCoreCpuParser` emitting `"Overall CPU"` and `"core <N>"` (both on chart `"CPU"`).

- [ ] **Step 11: Run the backend monitor unit tests (scoped)**

Run: `uv run pytest tests/unit/monitor/ -q`
Expected: PASS (no `TopCpuParser`/`core_count` import errors; PerCore tests green).

- [ ] **Step 12: Verify no dangling references remain**

Run: `grep -rn "TopCpuParser\|top -d\|core_count\|top_n" src tests --include=*.py`
Expected: no matches. (If any comment/string remains, fix it.)

- [ ] **Step 13: Commit**

```bash
git add src/otto/monitor/parsers.py src/otto/monitor/collector.py src/otto/monitor/export.py \
        tests/unit/monitor/test_parsers.py tests/unit/monitor/test_export_producer.py \
        tests/unit/monitor/test_collector_db.py tests/_fixtures/_fake_collector.py \
        tests/e2e/monitor/dashboard/conftest.py tests/e2e/monitor/dashboard/test_live_shell.py
git commit -m "feat(monitor)!: consolidate CPU onto /proc/stat, drop per-PID tracking

PerCoreCpuParser now emits Overall CPU (from the aggregate cpu line) plus
per-core lines onto one \"CPU\" chart, uncapped (max_series=None). Delete
TopCpuParser, the top -bn2 command, and the per-run core-count probe it needed.

BREAKING CHANGE: the monitor no longer collects per-process CPU; the separate
\"Per-core CPU\" chart is merged into \"CPU\".

Assisted-by: Claude (Opus 4.8)"
```

---

## Task 3: Frontend — remove proc awareness

Delete the retirement subsystem and its tests; simplify the `SubjectPage` chart-render block that fed it.

**Files:**
- Delete: `web/src/data/retirement.ts`
- Delete: `web/src/__tests__/retirement.data.test.ts`, `web/src/__tests__/subjectpage.retirement.test.tsx`
- Modify: `web/src/pages/SubjectPage.tsx` (drop `retireStaleSeries`)
- Modify: `web/src/__tests__/perf_budget.test.ts` (delete the retirement perf test)
- Modify: `web/src/__tests__/importexport.livefragment.test.ts` (neutralize `proc/999`)
- Modify: `web/src/data/exportDoc.ts` (stale comment)

- [ ] **Step 1: Delete the retirement module and its dedicated tests**

```bash
git rm web/src/data/retirement.ts \
       web/src/__tests__/retirement.data.test.ts \
       web/src/__tests__/subjectpage.retirement.test.tsx
```

- [ ] **Step 2: Simplify the `SubjectPage` render block**

In `web/src/pages/SubjectPage.tsx`:
- Remove the import line `import { retireStaleSeries } from "../data/retirement";`.
- Replace the `activeAll` / `byIndexKey` / `retireStaleSeries` block (lines ~221-234) with a direct filter:

```tsx
              const active = chart.series.filter((s) => checked.has(s.key));
              if (active.length === 0) return null;
              const shown = active.slice(0, MAX_SERIES_PER_CHART);
```

(`SeriesNode` and `seriesKey` remain imported — `seriesKey` is still used for `idxKey`/`revKey` below. If TypeScript flags `SeriesNode` as unused after this edit, remove it from the import; otherwise leave it.)

- [ ] **Step 3: Neutralize `proc/999` in the import/export test**

In `web/src/__tests__/importexport.livefragment.test.ts`, rename the label `"proc/999"` to a generic non-proc example everywhere it appears (lines ~76, ~80, ~94, ~95) — use `"net/eth0"`:
- `chart_map: { "net/eth0": "CPU" }`
- the meta chart `{ label: "net/eth0", ... }`
- both `expect(...).toBe(...)` assertions referencing the label.

(The test exercises generic `chart_map` roundtrip, not per-PID semantics — any label works.)

- [ ] **Step 4: Delete the retirement perf test**

In `web/src/__tests__/perf_budget.test.ts`, remove the `import { retireStaleSeries } from "../data/retirement";` line and delete the entire `it("retireStaleSeries does not get slower as the run gets longer", ...)` block (lines ~112-145) and its preceding comment. If that leaves an empty `describe`, remove the `describe` too.

- [ ] **Step 5: Update the stale comment in exportDoc.ts**

In `web/src/data/exportDoc.ts` (~line 252), remove/reword the sentence that references rebuilding `chart_map`/meta "whenever a new `proc/<pid>` series first reports" — per-process series no longer exist. Keep the surrounding explanation of live `chart_map` growth for other new labels.

- [ ] **Step 6: Run the web tests (scoped)**

Run: `cd web && npm run test -- --run`
Expected: PASS; no references to `retirement` remain (the deleted test files are gone, `perf_budget`/`importexport` updated).

- [ ] **Step 7: Typecheck the web app**

Run: `cd web && npx tsc --noEmit`
Expected: no errors (no dangling `retireStaleSeries`/`SeriesNode` references).

- [ ] **Step 8: Commit**

```bash
git add -A web/src
git commit -m "refactor(monitor-web): remove per-process (proc/*) series awareness

Delete the retirement subsystem (dead once no proc/* series are emitted) and
its tests; simplify SubjectPage's chart-series build. De-proc the perf and
import/export tests.

Assisted-by: Claude (Opus 4.8)"
```

---

## Task 4: Frontend — per-chart cap from the wire

Drive the render slice from `chart.maxSeries` (sourced from `spec.max_series`) instead of the hard-coded constant, so the CPU chart (`max_series = null`) shows every series.

**Files:**
- Modify: `web/src/data/seriesTree.ts` (`ChartNode.maxSeries`)
- Modify: `web/src/pages/SubjectPage.tsx` (per-chart cap)
- Test: `web/src/__tests__/seriestree.maxseries.test.ts` (new)

**Interfaces:**
- Consumes: the generated `ChartSpecRecord.max_series?: number | null` (from Task 1's regenerated types).
- Produces: `ChartNode.maxSeries: number | null` — `null` = uncapped, else the cap.

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/seriestree.maxseries.test.ts`. The static `kitchen-sink.json` fixture predates `max_series`, so its specs lack the field — perfect for testing the default-cap fallback. For the `null`/numeric cases, parse a fresh copy and set `max_series` on individual specs (the parsed `session.meta.charts` are plain objects with the wire's snake_case fields).

```ts
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { MAX_SERIES_PER_CHART } from "../charts/palette";
import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree } from "../data/seriesTree";

const HERE = dirname(fileURLToPath(import.meta.url));
function freshKitchen() {
  return parseExportDocument(
    readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
  ).sessions[0];
}

describe("buildSeriesTree maxSeries", () => {
  it("defaults a spec with no max_series to the default cap", () => {
    const tree = buildSeriesTree(freshKitchen(), "chassis-a_lc1");
    expect(tree.length).toBeGreaterThan(0);
    for (const chart of tree) expect(chart.maxSeries).toBe(MAX_SERIES_PER_CHART);
  });

  it("passes through null (uncapped) and an explicit numeric cap", () => {
    const session = freshKitchen();
    const cpu = session.meta.charts.find((c) => c.chart === "cpu");
    const psu = session.meta.charts.find((c) => c.chart === "psu-temp");
    expect(cpu).toBeDefined();
    expect(psu).toBeDefined();
    cpu!.max_series = null; // uncapped
    psu!.max_series = 3; // explicit cap
    const tree = buildSeriesTree(session, "chassis-a_lc1");
    const byKey = Object.fromEntries(tree.map((c) => [c.chartKey, c.maxSeries]));
    expect(byKey["cpu"]).toBeNull();
    expect(byKey["psu-temp"]).toBe(3);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npm run test -- --run seriestree.maxseries`
Expected: FAIL — `maxSeries` is `undefined` on `ChartNode`.

- [ ] **Step 3: Add `maxSeries` to `ChartNode` and populate it**

In `web/src/data/seriesTree.ts`:
- Add to `interface ChartNode`:

```ts
  maxSeries: number | null;
```

- Add the import: `import { MAX_SERIES_PER_CHART } from "../charts/palette";`
- In `buildSeriesTree`, where each `ChartNode` is pushed (the `out.push({ ... })` object), add:

```ts
      maxSeries: spec?.max_series === undefined ? MAX_SERIES_PER_CHART : spec.max_series,
```

Semantics of `spec?.max_series`: `undefined` when the chart has no spec **or** a spec that predates the field → default cap; `null` → uncapped; a number → that cap. This keeps the frontend consistent with the Python model default (a missing field is default-*capped*, never silently uncapped).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npm run test -- --run seriestree.maxseries`
Expected: PASS.

- [ ] **Step 5: Apply the per-chart cap in SubjectPage**

In `web/src/pages/SubjectPage.tsx`, replace the `shown` line from Task 3:

```tsx
              const active = chart.series.filter((s) => checked.has(s.key));
              if (active.length === 0) return null;
              const cap = chart.maxSeries; // number = cap, null = uncapped
              const shown = cap == null ? active : active.slice(0, cap);
```

And change the `overflowCount` prop (line ~278) to:

```tsx
                  overflowCount={cap != null && active.length > cap ? active.length : null}
```

(The overflow-note text at ~line 459 keeps `MAX_SERIES_PER_CHART`: every capped chart in practice uses the default cap of 8, and uncapped charts show no note. Add a brief code comment there noting a non-default cap wouldn't be reflected in the note count — YAGNI, no such chart exists.)

- [ ] **Step 6: Run web tests + typecheck**

Run: `cd web && npm run test -- --run && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/data/seriesTree.ts web/src/pages/SubjectPage.tsx web/src/__tests__/seriestree.maxseries.test.ts
git commit -m "feat(monitor-web): drive per-chart series cap from spec.max_series

ChartNode.maxSeries flows from the wire; SubjectPage caps each chart by its own
value (null = uncapped), so the CPU chart shows Overall + every core.

Assisted-by: Claude (Opus 4.8)"
```

---

## Task 5: Frontend — hybrid CPU coloring

Past the 8-color palette, keep `Overall CPU` a distinct bold line and collapse the per-core lines into one muted band. CPU-chart-specific.

**Files:**
- Modify: `web/src/charts/palette.ts` (muted-series constants)
- Modify: `web/src/charts/options.ts` (`ChartTheme.mutedSeries`, `SeriesInput.muted`, styling)
- Modify: `web/src/pages/SubjectPage.tsx` (mute per-core when CPU chart exceeds palette)
- Test: `web/src/__tests__/chartoptions.coloring.test.ts` (new)

**Interfaces:**
- Consumes: `ChartTheme.mutedSeries: string`; `SeriesInput.muted?: boolean`.
- Produces: a muted series renders in `theme.mutedSeries` at reduced width/opacity instead of `theme.series[slot]`.

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/chartoptions.coloring.test.ts`. `buildStackOption` takes `{ unit, yTitle, series, window, events, theme }` where `window: { from: number; to: number }`:

```ts
import { describe, expect, it } from "vitest";

import { buildStackOption, chartTheme, type SeriesInput } from "../charts/options";

function lineFor(s: SeriesInput) {
  const built = buildStackOption({
    unit: "%",
    yTitle: "Usage %",
    series: [s],
    window: { from: 0, to: 1000 },
    events: [],
    theme: chartTheme(false),
  });
  return (built.series as Array<Record<string, any>>)[0];
}

describe("hybrid CPU coloring", () => {
  it("muted series use the muted color at reduced width", () => {
    const theme = chartTheme(false);
    const line = lineFor({ key: "core 0", name: "core 0", slot: 3, muted: true, points: [] });
    expect(line.itemStyle.color).toBe(theme.mutedSeries);
    expect(line.lineStyle.width).toBeLessThan(2);
  });

  it("non-muted series use the palette slot color at full width", () => {
    const theme = chartTheme(false);
    const line = lineFor({ key: "Overall CPU", name: "Overall CPU", slot: 0, points: [] });
    expect(line.itemStyle.color).toBe(theme.series[0]);
    expect(line.lineStyle.width).toBe(2);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npm run test -- --run chartoptions.coloring`
Expected: FAIL — `theme.mutedSeries` undefined and `muted` not honored.

- [ ] **Step 3: Add muted-series palette constants**

In `web/src/charts/palette.ts`, add below the `SERIES_*` arrays:

```ts
// Single low-emphasis color for "band" series (e.g. per-core CPU past the
// palette size): many faint lines behind one bold aggregate. Not part of the
// CVD-ordered categorical palette — it is deliberately non-distinct.
export const MUTED_SERIES_LIGHT = "#9ca3af"; // gray-400
export const MUTED_SERIES_DARK = "#4b5563"; // gray-600
```

- [ ] **Step 4: Thread `mutedSeries` into the theme and honor `muted`**

In `web/src/charts/options.ts`:
- Import the constants: `import { MUTED_SERIES_DARK, MUTED_SERIES_LIGHT, SERIES_DARK, SERIES_LIGHT } from "./palette";`
- Add to `interface ChartTheme`: `mutedSeries: string;`
- In `chartTheme(dark)`, add to the dark branch `mutedSeries: MUTED_SERIES_DARK,` and to the light branch `mutedSeries: MUTED_SERIES_LIGHT,`.
- Add to `interface SeriesInput` (after `slot`):

```ts
  /** When true, render as a low-emphasis band member (single muted color,
   * thin line) rather than a distinct palette slot. Used for per-core CPU on
   * high-core hosts. */
  muted?: boolean;
```

- In the `series.map((s, i) => ({ ... }))` construction, replace the `lineStyle` and `itemStyle` lines with:

```ts
      lineStyle: { width: s.muted ? 1 : 2, opacity: s.muted ? 0.5 : 1 },
      itemStyle: {
        color: s.muted ? theme.mutedSeries : theme.series[s.slot % theme.series.length],
      },
```

- [ ] **Step 5: Run coloring test to verify it passes**

Run: `cd web && npm run test -- --run chartoptions.coloring`
Expected: PASS.

- [ ] **Step 6: Mute per-core lines in SubjectPage for the CPU chart**

In `web/src/pages/SubjectPage.tsx`, after `const shown = ...` (Task 4) and before building `entries`, add:

```tsx
              const muteCores =
                chart.chartKey === "CPU" && shown.length > MAX_SERIES_PER_CHART;
```

Then in the `entries` map, update the `input` object so `Overall CPU` takes the primary slot and cores are muted when `muteCores`:

```tsx
                  input: {
                    key: s.key,
                    name: s.key === s.label ? s.label : s.host,
                    slot: muteCores && s.label === "Overall CPU" ? 0 : s.slot,
                    muted: muteCores && s.label.startsWith("core "),
                    points: points.get(s.key) ?? [],
                  } satisfies SeriesInput,
```

- [ ] **Step 7: Run web tests + typecheck**

Run: `cd web && npm run test -- --run && npx tsc --noEmit`
Expected: PASS, no type errors. (`chartoptions.test.ts`'s `MAX_SERIES_PER_CHART === 8` assertion is unchanged.)

- [ ] **Step 8: Commit**

```bash
git add web/src/charts/palette.ts web/src/charts/options.ts web/src/pages/SubjectPage.tsx \
        web/src/__tests__/chartoptions.coloring.test.ts
git commit -m "feat(monitor-web): hybrid CPU coloring — bold Overall + muted per-core band

Past the 8-color palette, the CPU chart keeps Overall CPU a distinct bold line
and renders every core in one muted low-emphasis style. Distinct colors are
retained at <= 8 series.

Assisted-by: Claude (Opus 4.8)"
```

---

## Task 6: Documentation

**Files:**
- Modify: `docs/guide/monitor.md`
- Modify: `docs/architecture/subsystems/monitoring.md`

- [ ] **Step 1: Update the user guide**

In `docs/guide/monitor.md`, find the CPU section (search for "per-process", "top", "Per-core", "CPU"). Remove any description of per-process CPU traces. Describe the single CPU chart: Overall CPU plus one line per core, from `/proc/stat`. If the doc lists default graphs, ensure it lists one "CPU" chart (Overall + per-core), not separate "CPU" and "Per-core CPU" charts.

- [ ] **Step 2: Update the architecture subsystem doc**

In `docs/architecture/subsystems/monitoring.md`, find the parser/data-flow description (search for "TopCpuParser", "PerCoreCpuParser", "proc"). Remove `TopCpuParser` and per-PID references. Describe `PerCoreCpuParser` emitting Overall CPU + per-core onto one `"CPU"` chart, and mention the `max_series` chart property (per-chart cap; `None` = uncapped, used by the CPU chart).

- [ ] **Step 3: Verify no stale doc references**

Run: `grep -rn "TopCpuParser\|per-process\|Per-core CPU\|proc/<pid>\|top -d" docs/guide/monitor.md docs/architecture/subsystems/monitoring.md`
Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add docs/guide/monitor.md docs/architecture/subsystems/monitoring.md
git commit -m "docs(monitor): single CPU chart + max_series; drop per-process CPU

Assisted-by: Claude (Opus 4.8)"
```

---

## Final Verification (human-timed — heavy gates)

These run the real gates and MUST be timed by the human (dev-VM load rule). Do not run them repeatedly.

- [ ] **Backend gate (scoped-but-full monitor + models):**

Run: `uv run pytest tests/unit/monitor tests/unit/models -q`
Expected: all pass.

- [ ] **Web build + drift + brand gates:**

Run: `make web`
Expected: `gen_web_types.sh` produces no diff (types already committed in Task 1), vite build succeeds, airgap + brand checks pass.

- [ ] **Full repo gate (optional, human's call — HEAVY):**

Run: `make coverage`
Expected: green. This is the authoritative gate per repo convention; run only when the VM is idle.

- [ ] **Behavioral spot-check (verify skill):** launch the dashboard against the fake collector or a live host and confirm one merged "CPU" chart shows Overall + per-core, no `proc/*` series, and a many-core host renders the muted band. (Use the `verify`/`run` skill.)

---

## Self-Review notes (author)

- **Spec coverage:** parser consolidation (Task 2), per-chart cap API (Task 1 + Task 4), hybrid coloring (Task 5), full proc removal (Task 3), docs (Task 6), fixtures (Task 2). Back-compat/no-schema-bump is inherent (no schema-version touched; `max_series` is an additive optional field). ✔
- **No golden schema snapshot:** `schemas/*.json` is git-ignored; the only committed generated artifacts are the two `web/src/api/*.gen.ts` files (regenerated in Task 1). `test_jsonschema*.py` assert structure, not ChartSpec properties, so they need no update. ✔
- **Type consistency:** `max_series` (Python) ↔ `max_series?` (wire) ↔ `ChartNode.maxSeries` (frontend); `muted?` on `SeriesInput`; `mutedSeries` on `ChartTheme`. ✔
