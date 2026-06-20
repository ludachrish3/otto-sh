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

## Registering products from a product repo

Products are **behavior**, so they're customized in code — never declared in lab
data. Lab data stays product-agnostic so it can evolve independently of product
code: reverting a product's behavior must never force a lab change. A product
repo registers its products from a `.otto` init module, and otto applies them to
each host as it is ingested from lab data:

    from pathlib import Path
    from otto.host import register_product_provider

    def _provide(host):
        if host.os_type == "unix":
            return [MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))]
        return None

    register_product_provider(_provide)

The provider runs once per lab-ingested host. Key on product-agnostic host
attributes (`element`, `element_id`, `os_type`, `id`, `ip`, `resources`) to
decide which hosts get which products; source any per-host parameters (versions,
artifact paths) from your own product-repo config. Providers aggregate in
registration order and dedupe by `Product.name`.

Code-constructed hosts (`UnixHost(..., products=[...])`) keep their explicit
list; providers apply only to hosts built from lab data.
