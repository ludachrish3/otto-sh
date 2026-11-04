"""The container-image suites' shared steps: install, exercise and uninstall on test3.

One copy for every project5 suite that drives the ``docker_image`` products
through their own verbs (``test_cov_container.py``, ``test_container_stop.py``).
"""

import shlex
from collections.abc import Sequence
from pathlib import Path

from otto.bootstrap import get_projects
from otto.host.unix_host import UnixHost

PRODUCTS = ("cov_container", "cov_container_ref")
"""The two container products, in install order: the tarball loads the image the reference runs."""

STOP_PRODUCTS = (*PRODUCTS, "cov_container_bind")
"""The stop suites' products: :data:`PRODUCTS`, then the bind-mounted one.

``cov_container_bind`` is declared only by the e2e's opt-in bind overlay
(``OTTOPATH=<project5>:<bind overlay>``), so only the stop suites see it.
"""

BIND_SRC = "/var/cov/otto-bind-src"
"""Where test3 holds ``docker/build-bind/``'s product; ``cov_container_bind`` mounts it at /opt."""


def project5_root() -> Path:
    """This project's root, as otto loaded it (the suite runs inside ``otto test``)."""
    return next(p.root for p in get_projects() if p.name == "project5")


async def exec_in(host: UnixHost, container: str, *args: str) -> str:
    """Run ``/opt/product ARGS`` inside *container* with ``docker exec``; return its output."""
    argv = " ".join(shlex.quote(a) for a in args)
    cmd = f"docker exec {shlex.quote(container)} /opt/product {argv}"
    result = await host.exec(cmd, timeout=30)
    if not result.status.is_ok:
        raise RuntimeError(f"{host.id}: {cmd} failed: {result.value}")
    return result.value


async def install_products(host: UnixHost, names: Sequence[str]) -> None:
    """Prepare each product's ``cov_dir`` and install it, in order, unless it is installed."""
    for name in names:
        product = next(p for p in host.products if p.name == name)
        cov_dir = shlex.quote(product.cov_dir)
        prepare = f"sudo mkdir -p {cov_dir} && sudo chmod 777 {cov_dir}"
        prepared = await host.exec(prepare, timeout=30)
        if not prepared.status.is_ok:
            raise RuntimeError(f"{host.id}: preparing {product.cov_dir} failed: {prepared.value}")
        if not await product.is_installed(host):
            result = await product.stage(host)
            if result.is_ok:
                result = await product.install(host)
            if not result.is_ok:
                raise RuntimeError(f"{host.id}: installing {name} failed: {result.msg}")


async def uninstall_products(host: UnixHost, names: Sequence[str]) -> None:
    """Haul the product logs, then uninstall *names* in reverse order.

    The logs come first, while the containers still exist: ``otto test`` never
    calls ``Host.uninstall()`` itself, so nothing else will haul them, and
    ``docker logs`` needs the container present.
    """
    hauled = await host.get_product_logs()
    if not hauled.is_ok:
        raise RuntimeError(f"{host.id}: hauling product logs failed: {hauled.msg}")
    for name in reversed(names):
        product = next(p for p in host.products if p.name == name)
        result = await product.uninstall(host)
        if not result.is_ok:
            raise RuntimeError(f"{host.id}: uninstalling {name} failed: {result.msg}")
