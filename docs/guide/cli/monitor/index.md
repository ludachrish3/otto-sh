# otto monitor

`otto monitor` collects CPU, memory, disk, and network metrics from remote
hosts, and serves a web dashboard for reviewing what it collected.

![The topology map, otto monitor's landing view: a dense lab laid out by
data-plane structure, with element-grouped chassis nodes and a tunnel
overlay showing all three health states — ok, degraded, and
uncertain](../../../_static/generated/dashboard-topology.png)

<!-- Generated AT BUILD TIME by scripts/capture_docs_media.py (hooked from
docs/conf.py): the real review shell, fed the committed
web/fixtures/isp-core.json export document through the Import front
door, captured with headless Chromium. Do not commit media into
docs/_static/generated/. -->

Two commands live under one binary:

- `otto monitor --live [OPTIONS]` — the only hardware-touching path (it runs
  the reservation gate before touching any host, and needs `--lab` to
  resolve which hosts to poll). Collects from lab hosts and serves the
  dashboard against that live collector. Add `--db PATH` to persist the run
  as a **session**; reusing the same `--db` path on a later run appends
  another session to the same archive rather than overwriting it.
- `otto monitor <SOURCE>` — review mode. `SOURCE` is a `.json` export or a
  `.db` session archive; no hosts are touched, no reservation gate runs, and
  no `--lab` is needed — `SOURCE` is a self-contained document, so this
  works for a hand-carried archive on a machine with no lab configured at
  all. The dashboard **auto-loads** the document the moment the page opens —
  no Import click needed — and a multi-session archive gets a session
  picker.

Bare `otto monitor` (neither `--live` nor a source) prints usage and exits 2;
`--live` together with a source is a mutually exclusive error, also exit 2.
See [Web dashboard](dashboard.md#web-dashboard) for what the dashboard shows and
how it gets loaded either way — including live streaming straight into an
*open* dashboard tab, watching a running `--live` session's charts grow in
real time rather than requiring a reload.

See {doc}`../../../architecture/subsystems/monitoring` for how collection,
sessions, the `format:1` producer, and dashboard hydration fit together.

## `otto monitor --help`

```{raw} html
:file: ../../../_static/generated/termynal/help-monitor.html
```

## Synopsis

```text
otto monitor --live [OPTIONS]
otto monitor <SOURCE>
```

## Options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--live` | off | Collect from lab hosts (explicit opt-in; reservation-gated) |
| `--hosts REGEX` | all hosts | Regex FULLY matched against host IDs (`re.fullmatch`) — `sensor` does not select `sensor-1`; write `sensor.*`. Matching nothing is a loud error, not an empty run |
| `--interval, -i SECS` | `5.0` | Collection interval (minimum 1.0) |
| `--db PATH` | | Persist this `--live` run as a session in a SQLite archive; reusing a path appends another session |
| `--label TEXT` | | Human-readable label stored with this session |
| `--note TEXT` | | Free-form note stored with this session (shown as the dashboard's session-picker tooltip) |
| `SOURCE` (argument) | | Review a saved `.json` export or `.db` session archive instead of collecting live |

Which hosts `--hosts` selects from, and what each of the four ways a live run
can come up empty tells you, are on {doc}`live`.


```{toctree}
:caption: Topics
:hidden:

live
review
dashboard
serving
metrics
during-tests
```
