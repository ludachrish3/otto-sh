# Host products & lifecycle

Every host carries a list of **products** — units of software-under-test it
deploys. A product is a small injected strategy object; the host orchestrates.

## Defining a product

Subclass `Product` (or `FileProduct` for the single-artifact case) and implement
the project-specific halves:

    from pathlib import Path
    from otto.host import FileProduct
    from otto.utils import Status

    class MyApp(FileProduct):
        async def install(self, host):
            return (await host.run(f"tar xzf {self.artifact.name}", )).status, ""
        async def uninstall(self, host):
            return (await host.run("rm -rf /opt/myapp")).status, ""
        async def is_installed(self, host):
            return (await host.run("test -d /opt/myapp")).status.is_ok

## Injecting products

    host = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"},
                    products=[MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))])

## Lifecycle verbs

| Method | Behavior |
|--------|----------|
| `await host.stage()` | Stage every product (no install). |
| `await host.install(stage_only=False)` | Stage, then install (unless `stage_only`). |
| `await host.uninstall()` | Uninstall every product (best-effort). |
| `await host.is_installed()` | True iff ≥1 product and all installed. |
| `await host.is_uninstalled()` | Inverse of `is_installed()`. |

With no products, `stage`/`install`/`uninstall` are successful no-ops and
`is_installed()` is `False`.

> **Future:** declaring products in lab data (a `ProductSpec` boundary model +
> `register_product` registry) is a planned follow-on; today products are
> injected in code.
