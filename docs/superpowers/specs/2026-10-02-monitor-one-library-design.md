# `otto monitor` and `otto test --monitor` share one monitor library

**Status:** approved in chat section by section (Chris, 2026-10-02).
**Issues:** fixes #503, #504; refs #525 (thin-CLI series, item 5).
**Principle:** `docs/architecture/principles.md`, "Input rules live in the
library entry point". Worked examples: the item 2 spec
(`2026-09-29-run-options-own-their-rules-design.md`), the item 3 spec
(`2026-09-30-docker-verbs-align-with-docker-design.md`) and the item 4 spec
(`2026-10-01-cov-verbs-own-their-rules-design.md`).

## 1. Intent

Three producers start a monitor session, and each owns its own copy of the
rules:

| Producer | Code | Serves a dashboard |
|---|---|---|
| `otto monitor --live` | `src/otto/cli/monitor.py` | yes |
| `otto test --monitor` | `OttoPlugin._otto_session_monitor` (`src/otto/suite/plugin.py`) | no: collects and writes an export |
| the `monitor` fixture | `MonitorHandle.start` (`src/otto/suite/monitor_fixture.py`) | yes, on `127.0.0.1` |

The audit found the copies have drifted:

- **Which hosts can be sampled (#504).** `otto monitor` accepts a
  `UnixHost` or any host with an `snmp` block. The plugin accepts `UnixHost`
  only, with a comment ("build_monitor_collector only handles UnixHost")
  that `otto.monitor.factory` contradicts. The fixture bypasses the factory
  and builds `MetricCollector(hosts=, parsers=)`, so an SNMP host handed to
  it is polled over a shell. `_no_monitorable_hosts_message` is defined
  twice, with different text.
- **Dashboard TLS (#503).** `[monitor] tls_cert`/`tls_key` are resolved only
  by `cli/monitor.py:_resolve_monitor_tls`. The issue says the plugin and the
  fixture serve without it; in fact the plugin serves nothing, and the
  fixture's dashboard is the one server without TLS.
- **Session construction.** Frame, lab snapshot, `.db` archive and collector
  are built three times. Only the CLI passes the lab's declared links and a
  full-lab tunnel source; the plugin's "no active lab config" reason for
  `declared=[]` is stale, since it already walks `all_hosts()`, which needs
  one.
- **Session lifecycle.** Open the archive before spawning, stamp the end,
  finalize, close: hand-maintained at three sites. The fixture must call
  `collector.close_db()` and never `close()`, because the test owns the hosts
  and `close()` would disconnect them; nothing but a comment carries that.
- **A latent defect.** `build_monitor_collector` sets `host.log =
  LogMode.NEVER` on every host it targets. Under `otto test --monitor` those
  are the lab's shared host objects, so the tests' own commands on monitored
  hosts stop logging for the rest of the session.
- **CLI-only rules.** `--live`'s whole pipeline (D3 scope gate, selection,
  TLS, archive, tunnel source, serve-and-teardown) lives in the leaf, and so
  does review mode's source loading. The interval floor is enforced only by
  typer's `min=` (both `otto monitor --interval` and `otto test
  --monitor-interval`); `RunOptions` accepts any interval and any
  `monitor_hosts` text, and a bad `--hosts` regex escapes as a raw
  `re.error` traceback.
- **Log noise.** uvicorn's `uvicorn.access` logger writes one INFO line per
  HTTP request through otto's handlers.

## 2. Rulings (Chris, 2026-10-02)

1. **Scope: one shared session builder.** Beyond the #503/#504 fixes, the
   `--live` orchestration moves into the library, and one `MonitorSession`
   owns construction and lifecycle for all three producers. Each caller still
   decides who drives `collector.run()`.
2. **Host selection is its own library step** (`select_monitor_hosts`),
   separate from building the session: selection reads the active lab, while
   building reads no context.
3. **`otto test --monitor` refuses** when the selection holds no host that
   can be sampled (or the lab is empty), the same way an unmatched
   `--monitor-hosts` already stops the run. Today it warns and runs
   unmonitored.
4. **The `monitor` fixture serves no dashboard.** `MonitorHandle.start`
   collects only, returns `None`, and loses `port`/`bind`. `db_path=`
   archives the run for `otto monitor <file>.db`. `otto monitor` is the one
   producer that serves. This resolves #503's fixture half by removal.
5. **The keyed dashboard URLs keep printing** to the terminal through
   `CONSOLE` (never the logger), exactly as today.
6. **Request logging.** uvicorn's access log is turned off. otto logs each
   request at DEBUG; INFO `Dashboard client connected from <ip>` the first
   time an IP passes the access-key gate in a server's life; WARNING
   `Dashboard request without a valid access key from <ip>` the first time an
   IP is refused.
7. **Review mode's bad source exits 2** (a usage error naming `SOURCE`),
   matching click's own `exists=True` refusal; today it exits 1.
8. **The lab-aware layer is its own tach module** (amendment, planning).
   `tach.toml` forbids `otto.monitor` from depending on `otto.config`,
   `otto.context`, `otto.bootstrap` and `otto.tunnel` ("tunnel discovery is
   INJECTED ... Keep it that way"). `select_monitor_hosts`, `run_live` and
   `LiveReport` need all four, so they live in `otto.monitor.live`, declared
   as a nested tach module (the repo's first) that depends on `otto.monitor`
   plus those four. Everything that reads no ambient state stays in the
   engine: `MonitorSession` in `otto.monitor.session` beside `SessionFrame`,
   `otto.monitor.tls` and `otto.monitor.review` with repos passed in.
   Probed: the nested entry validates, and the same file without it fails
   on exactly the two forbidden edges.

## 3. Shape

### 3.1 Errors and pure helpers

A new light module, `otto.monitor.errors` (imports only `otto.errors`), so
the CLI can catch these without importing the monitor runtime:

| Error | Base | Raised by | Carries |
|---|---|---|---|
| `MonitorInputError` | `FieldError` | `MonitorSession.build`, `select_monitor_hosts`, `run_live` | `field`: `"hosts"`, `"targets"`, `"interval"` |
| `NoMonitorableHostsError` | `OttoError` | `select_monitor_hosts` | `walked: list[str]` (selected ids, possibly empty) |
| `MonitorTlsError` | `OttoError` | `resolve_monitor_tls` | `setting`: `"tls_cert"`, `"tls_key"`, or `None` when repos disagree; `repos: list[str]` |
| `ReviewSourceError` | `FieldError` | `load_review_document` | `field="source"` |

`MonitorTlsError` is not a `FieldError`: no flag spells it, it is a settings
refusal. `NoMonitorableHostsError`'s message is built in exactly one place
(the constructor): an empty `walked` says the lab has no hosts to select
(and never mentions a pattern); a non-empty one says "N host(s) selected,
but none of them can be monitored: <first five ids> (+K more)", names both
collection routes (a shell on a Unix host, or SNMP for any host declaring an
`snmp` block), and says the selection is not the problem.

Two pure helpers in `otto.utils` beside `validate_interval`, raising
`ValueError` with a field-free message so each caller wraps it in its own
error type:

- `validate_interval(seconds)` (exists): the floor `MIN_INTERVAL_SECONDS`.
- `compile_host_pattern(text) -> re.Pattern[str]`: compiles, turning
  `re.error` into `ValueError` naming the pattern and the regex error.

### 3.2 `otto.monitor.factory`: what can be sampled

- `is_monitorable(host) -> bool`: a `UnixHost`, or any host whose `snmp` is
  not `None`. The one predicate.
- `monitorable(hosts) -> list[RemoteHost]`: filters by it, order kept.
- `build_monitor_collector(hosts, *, parsers=None, db=None,
  tunnel_source=None)`:
  - **no longer mutates `host.log`**;
  - gains `parsers=`: when given, shell targets use them instead of
    `get_host_parsers(host.id)`; SNMP targets ignore them.
- `MetricCollector._collect_one` passes `log=LogMode.NEVER` to
  `host.run(...)`. `BaseHost._effective_log` combines it with the host's own
  mode, so collection stays silent without touching the shared host.

### 3.3 `otto.monitor.tls`: the declared dashboard TLS

`resolve_monitor_tls(repos: Sequence[Repo]) -> MonitorSettings | None`,
moved from `cli/monitor.py:_resolve_monitor_tls` with its three rules
unchanged, raising `MonitorTlsError` instead of echoing and exiting:

1. No repo declares `tls_cert`: `None` (plain HTTP).
2. Declaring repos disagree on `(tls_cert, tls_key)`: refuse, naming the
   repos.
3. A declared file is missing, or the pair fails
   `ssl.SSLContext(PROTOCOL_TLS_SERVER).load_cert_chain(...)`: refuse,
   naming the setting and path. Never a silent fall-back to HTTP.

### 3.4 `otto.monitor.session`: `MonitorSession`

```python
class MonitorSession:
    frame: SessionFrame
    lab: LabSnapshot
    collector: MetricCollector
    interval: timedelta
    db_path: Path | None       # SQLite archive, written live
    export_path: Path | None   # format:1 JSON, written by finish()
    owns_hosts: bool

    @classmethod
    def build(cls, hosts=None, *, targets=None, parsers=None,
              interval: timedelta | float, db_path=None, export_path=None,
              label=None, note=None, declared=(), tunnel_source=None,
              owns_hosts: bool) -> "MonitorSession": ...
    async def open(self) -> None: ...
    def spawn(self) -> "asyncio.Task[None]": ...
    async def finish(self) -> None: ...
    def export(self) -> MonitorExport: ...
    async def __aenter__(self) -> "MonitorSession": ...   # open()
    async def __aexit__(self, *exc) -> None: ...          # finish()
```

- **`build` validates first and does no I/O.** Field-named
  `MonitorInputError`s:
  - exactly one of `hosts` and `targets` (`field="hosts"`);
  - `interval` at or above the floor (`field="interval"`);
  - every host passes `is_monitorable`, else refuse naming the hosts
    (`field="hosts"`); every target carries an `snmp` source or a `UnixHost`
    host, else refuse naming them (`field="targets"`). The strict rule lives
    here; a caller walking a lab filters with `monitorable()` first.

  `run_live` checks the interval before selection too, through the same
  helper, so its refusal comes before any lab walk; `build`'s check is what
  every other caller relies on.

  Then: the frame (`new_frame(label, note)`), the snapshot
  (`snapshot_lab(hosts, list(declared))`; it keeps only links whose both
  endpoints are in the snapshot, so a whole lab's links are safe to pass),
  the collector (`build_monitor_collector(hosts, parsers=…)`, or
  `MetricCollector(targets=…)`), and, with `db_path`, the unopened archive
  via `build_session_metric_db`. That is the only place the throwaway
  meta-collector is built. `build` reads no context: `declared` and
  `tunnel_source` come from the caller.
- **`open()`** awaits `collector.init_db()` before any collection task can
  exist (the #136 ordering). Idempotent. A locked or unsupported archive
  raises here.
- **`spawn()`** returns `asyncio.create_task(collector.run(interval))`. It
  requires a completed `open()` (the collector's own `run()` precondition
  stays the loud guard). The caller owns the task: it cancels and gathers it
  before `finish()`.
- **`finish()`**, idempotent and safe after a failed or skipped `open()`:
  1. stamp `frame.end`;
  2. with `export_path`, create its parent and write
     `document_json(self.export())`;
  3. with an opened archive, `finalize(frame.end)`;
  4. inside `teardown_step("monitor", "collector close")`:
     `collector.close()` when `owns_hosts`, else `collector.close_db()`.
- **`export()`** is `build_live_export(frame, collector, lab)`.

`collector.run()`'s precondition comment, which names the two suite callers
that open the archive themselves, is rewritten: after this item only the
plugin opens on one loop and drives on others, and it goes through
`open()`.

### 3.5 `otto.monitor.live` (nested tach module): `select_monitor_hosts`

```python
def select_monitor_hosts(pattern: str | re.Pattern[str] | None) -> list[RemoteHost]
```

1. A `str` pattern goes through `compile_host_pattern`; failure raises
   `MonitorInputError(field="hosts")`.
2. Walks `all_hosts(pattern=…)` on the active context, materialized inside
   the guard (the generator raises at the first `next()`). An unmatched
   pattern raises the existing `EmptySelectionError` unchanged.
3. Keeps `monitorable(walked)`. Empty raises
   `NoMonitorableHostsError(walked=[ids])`.

### 3.6 `otto.monitor.live` (nested tach module): `run_live` and `LiveReport`

```python
async def run_live(*, hosts: str | re.Pattern[str] | None = None,
                   interval: float = 5.0, db: Path | None = None,
                   label: str | None = None, note: str | None = None,
                   bind: str = "0.0.0.0", port: int = 0) -> LiveReport

@dataclass(frozen=True)
class LiveReport:
    session_id: str
    hosts: list[str]
    db: Path | None
    start: datetime
    end: datetime
```

Every refusal comes before any file exists (today's "TLS before `--db`"
guarantee, widened):

1. `interval` below the floor: `MonitorInputError(field="interval")`.
2. D3: the driving repo's `[project]` scope. `_enforce_driving_repo_scope`
   moves verbatim (private) from the CLI, its docstring with it.
   `ProjectScopeError`.
3. `select_monitor_hosts(hosts)`.
4. `resolve_monitor_tls(get_repos())`.
5. `MonitorSession.build(selected, interval=…, db_path=db, label=…, note=…,
   declared=get_lab().links, tunnel_source=<discover_tunnel_records over the
   whole active lab>, owns_hosts=True)`, then `MonitorServer(collector,
   bind, port, mode="live", frame=…, lab=…, tls_cert=…, tls_key=…)`.
6. `async with session:` spawn, `await server.serve()`; in `finally`, log
   "Server exiting...", cancel and gather the task. `__aexit__` finishes the
   session.

It returns the report after the server stops. `LiveReport` has **no `ok`
field**: a live run either serves until it is stopped or raises, and a
verdict field would always read true.

### 3.7 `otto.monitor.review`

- `load_review_document(path) -> MonitorExport`: `.json` validated as
  format:1, `.db` through `build_db_export`, any other suffix refused. Each
  failure is `ReviewSourceError(field="source")`, its message unchanged from
  today's leaf.
- `async serve_review(path, *, repos: Sequence[Repo]) -> None`: load,
  `resolve_monitor_tls(repos)`, serve `MonitorServer(MetricCollector(targets=[]),
  mode="review", document=…, source_name=path.name, archive_path=path if .db,
  tls…)`. It returns `None` when the server stops: review has no outcome
  beyond serving (a documented plain return).

### 3.8 The `otto monitor` leaf

It keeps only parse-level rules and the CLI preamble:

- `--live` together with a source is a usage error; neither prints help.
  Both exit 2. That is the shape of one command with two modes.
- The preamble stays: `ensure_cli_session` for review, `ensure_lab_session`
  plus `present_reservation_gate` for `--live` (item 7's territory).
- `--interval` loses typer's `min=`; its help text keeps the floor.
- The `_otto_root_options` guards (which skipped the preamble and TLS when a
  unit test called `monitor()` with a hand-built context) are deleted. The
  library always resolves TLS, and leaf tests invoke through the real root
  app, as the differential does, with the library calls faked.
- Live: `report = run_command(run_live(hosts=…, interval=…, db=…, label=…,
  note=…))`; with `--db`, log `Monitor session <id> archived to <db>`.
- Review: `run_command(serve_review(source, repos=get_repos()))`.
- One translation site:
  - `MonitorInputError` and `ReviewSourceError` go to `usage_error_from(e,
    flags={"hosts": "--hosts", "interval": "--interval", "source":
    "SOURCE"})`: exit 2.
  - `EmptySelectionError`, `ProjectScopeError`, `NoMonitorableHostsError`,
    `MonitorTlsError` go to `fail(e)`: exit 1, one line, no traceback.

Deleted from `cli/monitor.py`: `_resolve_monitor_tls`,
`_enforce_driving_repo_scope`, `_load_review_document`, `_serve_review`,
`_run_monitor`, `_EMPTY_LAB`, `_MAX_NAMED_HOSTS`,
`_no_monitorable_hosts_message`, and the inline host predicate.

### 3.9 Dashboard request logging (`otto.monitor.server`)

- `uvicorn.Config(..., access_log=False)`.
- `_AccessKeyMiddleware` already knows each request's outcome; it logs on
  `otto.monitor.server`'s logger, using the client IP from `scope["client"]`
  (`"unknown"` when absent) and `scope["path"]` only, never the query
  string, so the access key cannot reach a log record:
  - DEBUG per request: method, path, status, client IP (status read by
    wrapping `send`).
  - INFO `Dashboard client connected from <ip>` the first time the gate
    admits that IP during this app's life.
  - WARNING `Dashboard request without a valid access key from <ip>` the
    first time the gate refuses that IP.
  The seen-IP sets live on the middleware instance (one per app, so one per
  server run).
- `_RedactAccessLogQueryString` and its `addFilter` are deleted: their only
  job was scrubbing `?key=` from uvicorn's per-request lines, which no longer
  exist. The `SuppressASGIWarning` filter on `uvicorn.error` stays.
- `otto.logger.management`'s note that `uvicorn.access` INFO is "wanted
  output" is rewritten: the access log is off and otto logs requests itself.
- The keyed URLs printed by `serve()` through `CONSOLE` are unchanged.

### 3.10 `RunOptions` owns the monitor rules

In `RunOptions.__post_init__` (pure), via the §3.1 helpers:

- `monitor_interval` below the floor: `OptionsValidationError` whose message
  names `monitor_interval`.
- `monitor_hosts` not a valid regex: `OptionsValidationError` whose message
  names `monitor_hosts`.

`otto test` spells both through its existing `usage_error_from` site
(`spell_flags` rewrites the field names to `--monitor-interval` and
`--monitor-hosts`). `cli/test.py` drops `min=` from `--monitor-interval`.
`otto.suite.run` gains no `otto.monitor` import: the helpers live in
`otto.utils`.

### 3.11 The plugin (`_otto_session_monitor`)

- Selection: `select_monitor_hosts(self._monitor_hosts)`.
  Both refusals end the run with `pytest.exit(...,
  returncode=pytest.ExitCode.USAGE_ERROR)` (ruling 3):
  `EmptySelectionError` as `f"--monitor-hosts: {exc}"` (today's text), and
  `NoMonitorableHostsError` as `f"--monitor: {exc}"`. A bad regex cannot
  reach here: `RunOptions` refused it.
- SNMP hosts are sampled (#504); the wrong comment goes.
- `MonitorSession.build(selected, interval=self._monitor_interval,
  db_path=output if its suffix is .db, export_path=output otherwise,
  declared=get_lab().links, owns_hosts=True)`. The `.db`-versus-JSON suffix
  rule stays here: it is `--monitor-output`'s meaning.
- `await session.open()`, expose `session.collector` as
  `session_monitor_collector`, `yield`, then in `finally` `await
  session.finish()` and log `Monitor data written to <output>`.
- `_otto_class_monitor_task` is unchanged: it drives `collector.run()` per
  class.
- Tests' commands on monitored hosts log normally again (§3.2).

### 3.12 The fixture (`MonitorHandle`)

```python
async def start(self, hosts: list[RemoteHost] | None = None, *,
                targets: list[MonitorTarget] | None = None,
                interval: timedelta | float = timedelta(seconds=5),
                parsers: list[MetricParser] | None = None,
                db_path: str | None = None) -> None
```

- `MonitorSession.build(hosts, targets=…, parsers=…, interval=…,
  db_path=…, declared=get_lab().links, owns_hosts=False)`; then `await
  session.open()` and `self._task = session.spawn()`. The fixture only loads
  through `run_tests`, which always has a lab, so `get_lab()` is safe here.
- `start` no longer builds a `MonitorServer`, waits on `wait_started()`, or
  returns a URL. With no wrapper `_run()` task, it uses the same
  open-then-spawn sequence as every other caller.
- `stop()`: cancel and gather the task, then `session.finish()` (borrowed
  hosts, so `close_db`). Idempotent, as today.
- `results()`, `events()` and `event()` keep their behaviour, read
  `session.collector`, and stay readable after `stop()`. `event()` still
  falls back to the `otto test --monitor` session collector when the handle
  is not started.
- `started` keeps its meaning: true from the moment the session is built,
  so a failing `open()` still leaves teardown something to finish.
- The old `ValueError("Provide either hosts or targets")` becomes `build`'s
  `MonitorInputError`. An SNMP host goes over SNMP; a host that cannot be
  sampled is refused at `start()`, naming it.

### 3.13 Layering and exports

`tach.toml` gains:

```toml
[[modules]]
path = "otto.monitor.live"
depends_on = ["otto.monitor", "otto.config", "otto.context", "otto.bootstrap", "otto.tunnel", "otto.host", "otto.errors", "otto.utils"]
```

(the exact list is whatever `otto.monitor.live` imports, written by hand;
never `tach sync`). `otto.monitor`'s own entry and its "keep it injected"
note are unchanged. `otto.monitor.tls` and `otto.monitor.review` name
`Repo` only under `TYPE_CHECKING` and read `.name` / `.monitor_settings`
off the objects they are handed, so the engine gains no `otto.config` edge.
`otto.monitor/__init__.py` may name the `otto.monitor.live` exports under
`TYPE_CHECKING` (probed: validates). The `tach.toml` header gains a dated
paragraph recording the first nested module and why, and the strongly
connected component is re-measured with `forbid_circular_dependencies`
(the header's own practice) and its figure updated.

New public names, each lazy in `otto.monitor` (`_LAZY_ATTRS` +
`TYPE_CHECKING` import + `__all__`): `MonitorSession`, `LiveReport`,
`run_live`, `select_monitor_hosts`, `serve_review`, `load_review_document`,
`resolve_monitor_tls`, `is_monitorable`, `monitorable`, and the four errors.
`make api-snapshot` regenerates the golden. Import-budget ceilings are never
regenerated: `otto.cli.monitor` keeps every monitor import inside the
command body, and `otto.monitor.errors` imports only `otto.errors`.

## 4. Testing

**The differential (#525):** `tests/unit/cli/test_monitor_differential.py`,
shaped like `tests/unit/cli/test_test_differential.py`:

- hand-off rows: `--live --hosts/--interval/--db/--label/--note` hand
  `run_live` exactly the parsed values; `otto monitor SOURCE` hands
  `serve_review` the path and the repos;
- refusal rows: `MonitorInputError` reaches the output as `--interval` or
  `--hosts` and `ReviewSourceError` as `SOURCE` (exit 2); each `fail` error
  exits 1 with its message on one line;
- the module docstring records the mutation that turns it red.

`test_test_differential.py` gains `--monitor-interval` and `--monitor-hosts`
refusal rows.

**Library unit tests:**

| Unit | Cases |
|---|---|
| `is_monitorable` / `monitorable` | Unix, SNMP-only, neither; order kept |
| `NoMonitorableHostsError` | empty walk (no pattern wording); fewer and more than five ids; both routes named |
| `resolve_monitor_tls` | ported from `TestResolveMonitorTls`: none, agreement, disagreement, missing file, unloadable pair |
| `select_monitor_hosts` | bad regex; unmatched pattern; SNMP-only host kept; nothing monitorable |
| `MonitorSession.build` | each refusal; snapshot keeps declared links; archive meta carries the interval |
| `MonitorSession` lifecycle | open before spawn; `spawn` without `open` fails loud; `finish` order; `close` for owned vs `close_db` for borrowed hosts; idempotence; `finish` after a failed `open`; JSON export written |
| `run_live` | each refusal leaves no `--db` file; report fields; tunnel source covers the whole lab |
| `load_review_document` / `serve_review` | each refusal; TLS resolved from the given repos |
| collector logging | building leaves `host.log` unchanged; collection calls pass `log=NEVER` |
| server logging | INFO once per admitted IP; WARNING once per refused IP; DEBUG per request; the key in no record at any level; nothing on `uvicorn.access` |
| `RunOptions` | sub-floor interval and invalid regex refused, naming the field |
| plugin | SNMP host sampled; zero sampleable hosts and an empty lab stop the run with the usage-error code; tests' host logging untouched |
| fixture | `start`/`stop` with no server; SNMP route; refusal naming the host; results readable after stop |

`tests/unit/cli/test_monitor.py`'s classes move to library tests by
mechanism (see `reference_verb_rename_changes_the_transport`); the leaf
tests keep only parse-level behaviour.

## 5. Documentation

- `docs/cli/monitor/live.md` and `docs/cli/monitor/serving.md`: the
  connection log lines; TLS applies to `otto monitor` (both modes) only.
- `docs/architecture/subsystems/monitoring.md` and `security.md`: the new
  owners (`otto.monitor.tls`, `.live`, `.review`); `security.md`'s
  `_resolve_monitor_tls` reference retargets.
- The `otto.monitor` package docstring's quick start, rewritten around
  `MonitorSession` and `run_live`.
- Fixture pages (`docs/cookbook/test-recipes.md`,
  `docs/cookbook/authoring/writing-tests.md`, `docs/overview.md`,
  `docs/cli/monitor/live.md`, `docs/cookbook/extending/custom-parsers.md`):
  drop `url = await monitor.start(...)`; one line on archiving with
  `db_path=` and reviewing with `otto monitor <file>.db`, or watching live
  with `otto monitor --live`.
- `otto test --monitor`'s page: the new refusal when nothing can be sampled,
  and that SNMP hosts are sampled.

## 6. BREAKING (migration recipe for the squash message)

- `MonitorHandle.start` takes no `port`/`bind`, returns `None` and serves no
  dashboard: drop the `url =` binding; archive with `db_path=` and run
  `otto monitor <file>.db`, or watch with `otto monitor --live`.
- `otto monitor SOURCE` exits 2 (was 1) on a bad source.
- `otto test --monitor` refuses when no selected host can be sampled (was a
  warning and an unmonitored run).
- `RunOptions` refuses a sub-floor `monitor_interval` and an invalid
  `monitor_hosts` regex.
- `build_monitor_collector` no longer sets `host.log`; collection is
  silenced per call.
- uvicorn's access log is gone; the INFO and WARNING connection lines
  replace it, and per-request lines are DEBUG.
- `otto.cli.monitor`'s private helpers are gone; their library homes are in
  §3.

## 7. Out of scope

- The CLI preamble (lab session, reservation gate): item 7 (#508).
- Dashboard UI and the web bundle.
- Collector internals beyond the per-call `log=`.
- New `--bind`/`--port` flags for `otto monitor`.
