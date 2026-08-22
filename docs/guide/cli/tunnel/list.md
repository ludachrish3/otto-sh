# otto tunnel list

```bash
otto --lab veggies tunnel list
```

`otto tunnel list` shows every live tunnel `discover_tunnels` finds right
now — the running, tagged `socat` processes ARE the record; there is no
separate ledger. Each row is:

`ID · ENDPOINTS (a ↔ b) · VIA · PORT · PROTO · AGE · STATUS`

- **VIA** lists the intermediate hops in path order, plus `→ <dest>` when
  the tunnel has a `--dest` override.
- **AGE** is the oldest observed process's age, humanized (`3h`, `2d`, ...).
- **STATUS** is `ok` when every expected process was found; `degraded
  (<present>/<expected>)` when some are missing on hosts that *were*
  reachable; either form gets a trailing `?` when at least one chain host
  couldn't be scanned this pass, so absence there means "unknown," not
  "gone."

