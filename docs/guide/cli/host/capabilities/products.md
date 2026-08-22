# Products and lifecycle

Full signatures: {class}`~otto.host.host.BaseHost` and the `Product` classes.

Every host carries a list of **products** — units of software-under-test it
deploys. A product is a small injected strategy object; the host orchestrates.

## Defining a product

Subclass `Product` (or `FileProduct` for the single-artifact case) and implement
the project-specific halves:

    from pathlib import Path
    from otto.host import FileProduct

    class MyApp(FileProduct):
        async def install(self, host):
            return await host.run(f"tar xzf {self.artifact.name}")
        async def uninstall(self, host):
            return await host.run("rm -rf /opt/myapp")
        async def is_installed(self, host):
            return (await host.run("test -d /opt/myapp")).status.is_ok

`stage`, `install`, and `uninstall` return a `Result` — usually just whatever
`host.run` / `host.put` handed back. The *failing* product's result is
returned whole, so the command's own retcode and output reach the process exit
code; a run where every product succeeds collapses to a bare success, since
with several products there is no single result to hand back.
`is_installed` is a plain predicate.

## Injecting products

    host = UnixHost(ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")],
                    products=[MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))])

## Lifecycle verbs

| Method | `owner=` | Behavior |
|--------|----------|----------|
| `await host.stage()` | yes | Stage every product (no install). |
| `await host.install(stage_only=False)` | yes | Stage, then install (unless `stage_only`). |
| `await host.uninstall()` | yes | Product logs, uninstall every product, debug logs (best-effort). |
| `await host.is_installed()` | yes | True iff ≥1 product and all installed. |
| `await host.is_uninstalled()` | yes | Inverse of `is_installed()`. |
| `await host.cleanup()` | no | `uninstall()`, then remove dev tools and toolchain tools. |
| `await host.is_clean()` | no | True iff no product, dev tool, or toolchain tool is present. |

With no products, `stage`/`install`/`uninstall` are successful no-ops and
`is_installed()` is `False`.

The five product verbs take `owner=` — the name of the repo whose products to
act on (`None`, the default, means all of them). That is what keeps one repo's
`install` off another repo's products on a shared host; the empty-list rule
above applies to the *filtered* list, so a repo with nothing here is not
reported installed by someone else's products. `cleanup()` and `is_clean()`
have no `owner=`: both reach past products into the host's dev tools and
toolchain tools, which are host-wide, so there is no per-repo answer to give.

## Log retrieval

`uninstall()` gathers product logs **before** tearing anything down and debug
logs **after** — teardown activity is usually exactly what debug logs exist to
capture. Both halves are also callable directly:

| Method | Behavior |
|--------|----------|
| `await host.get_logs(product=True, debug=True)` | Both halves; `require_product_logs=True` makes an empty product haul a failure. |
| `await host.get_product_logs()` | Each product's `get_logs(host, dest)` hook (best-effort). |
| `await host.get_debug_logs()` | Fetch the host's `debug_log_globs`. |

Retrieving zero logs is success. Files land in a documented tree, keyed by host
id the way the coverage pipeline keys its own output:

```text
<output-dir>/logs/<host-id>/product/…
<output-dir>/logs/<host-id>/debug/…
```

`<output-dir>` is the active command's output directory unless a caller passes
`dest=`. `debug_log_globs` is a host field (settable per host class, per OS
profile, and per host in `lab.json`) holding remote paths — a pattern entry is
expanded on the device by `glob`, so a host family without that capability
(embedded, today) declares concrete paths or overrides `get_debug_logs`; a
pattern there fails loudly rather than silently retrieving nothing.
## `install` options

```text
otto host <HOST_ID> install [--stage-only]
```

| Option | Description |
| ------ | ----------- |
| `--stage-only / --no-stage-only` | Transfer products but skip the install step |
