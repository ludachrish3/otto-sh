# Links & tunnels

otto sees the lab's network twice. **Links** are the static topology — the
edges declared in `lab.json` (or derived from each host's management hop)
that {doc}`otto link <link>` inspects and impairs. **Tunnels** are dynamic:
host-resident `socat` chains that {doc}`otto tunnel <tunnel>` builds to
carry a service's traffic across the lab.

Rule of thumb: impair a *link* when you want to shape a path that already
exists; build a *tunnel* when you need a path that doesn't.

```{toctree}
link
tunnel
```
