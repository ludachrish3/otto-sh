# Monitoring during a test run

Pass `--monitor` to `otto test` to collect metrics for the entire run.
Per-test start/end events are emitted automatically and the captured
data is written to `<output_dir>/monitor.json` at exit:

```bash
otto --lab my_lab test --monitor TestPerformance
otto --lab my_lab test --monitor --monitor-interval 2 --monitor-hosts router TestPerformance
otto --lab my_lab test --monitor --monitor-output run.db TestPerformance
```

`otto monitor <path>` opens either output in the same review dashboard
described in [Reviewing a capture](review.md#reviewing-a-capture) — the
document loads automatically the moment the page opens, no Import click
needed.

