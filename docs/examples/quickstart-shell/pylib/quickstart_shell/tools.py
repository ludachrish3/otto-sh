"""Run the staged tools on dut the way the tests do: once per call, with coverage on."""

from pathlib import Path

from otto.lab import get_host

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The build bakes each object's absolute path into the tool. GCOV_PREFIX_STRIP
# drops the project root's own components from it, and GCOV_PREFIX puts what
# is left under the product's cov_dir, where `otto cov get` looks.
STRIP = len(PROJECT_ROOT.parts) - 1


class ToolFailedError(RuntimeError):
    """A staged tool failed on dut: a non-zero exit, a timeout, a kill or a lost session."""


async def run_tool(name: str, args: str) -> str:
    """Run one staged tool on dut with coverage on; return what it printed.

    ``args`` is shell text: it reaches dut's shell unquoted, so quote what needs it.
    """
    host = get_host("dut")
    product = next((p for p in host.products if p.name == name), None)
    if product is None:
        raise LookupError(f"no product {name!r} on dut")
    result = await host.exec(
        f"GCOV_PREFIX={product.cov_dir} GCOV_PREFIX_STRIP={STRIP} /tmp/quickstart/{name} {args}"
    )
    if not result.is_ok:
        if result.timed_out:
            why = "timed out"
        elif result.retcode == -1:
            # The tool never exited on its own: a signal killed it, or the session was lost.
            why = f"did not finish: {result.value or result.status.name}"
        else:
            why = f"exit code {result.retcode}"
        raise ToolFailedError(f"{result.command!r} failed on dut: {why}")
    return result.value
