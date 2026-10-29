"""Run the installed tools the way the tests do: once per call, with coverage on."""

from pathlib import Path
from typing import Any

from otto.host import Host
from otto.lab import get_host

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The build bakes each object's absolute path into the tool. GCOV_PREFIX_STRIP
# drops the project root's own components from it, and GCOV_PREFIX puts what
# is left under the product's cov_dir, where `otto cov get` looks.
STRIP = len(PROJECT_ROOT.parts) - 1
CONTAINER = "docker-host.quickstart.tools"
PARENT = "docker-host"


class ToolFailedError(RuntimeError):
    """A tool failed: a non-zero exit, a timeout, a kill or a lost session."""


def _find(name: str) -> "tuple[str, Host, Any]":
    """Return the host that has product *name*, with its id and the product.

    The product is typed loosely: its fields depend on its kind, and a
    ``docker_image`` product adds ``container_name``, the container it runs in.
    """
    for host_id in (CONTAINER, PARENT):
        host = get_host(host_id)
        product = next((p for p in host.products if p.name == name), None)
        if product is not None:
            return host_id, host, product
    raise LookupError(f"no product {name!r} on {CONTAINER} or {PARENT}")


async def run_tool(name: str, args: str) -> str:
    """Run one tool with coverage on; return what it printed.

    wordcount and shout run in the compose container with both variables set.
    banner runs in its own container through `docker exec` on the parent; that
    container already has GCOV_PREFIX from the product's run_args.
    ``args`` is shell text: quote what needs it.
    """
    host_id, host, product = _find(name)
    if product.kind == "docker_image":
        command = (
            f"docker exec -e GCOV_PREFIX_STRIP={STRIP} {product.container_name} "
            f"/opt/quickstart/{name} {args}"
        )
    else:
        command = (
            f"GCOV_PREFIX={product.cov_dir} GCOV_PREFIX_STRIP={STRIP} /opt/quickstart/{name} {args}"
        )
    result = await host.exec(command)
    if not result.is_ok:
        if result.timed_out:
            why = "timed out"
        elif result.retcode == -1:
            # The tool never exited on its own: a signal killed it, or the session was lost.
            why = f"did not finish: {result.value.strip() or result.status.name}"
        else:
            why = f"exit code {result.retcode}"
        raise ToolFailedError(f"{result.command!r} failed on {host_id}: {why}")
    return result.value.strip()
